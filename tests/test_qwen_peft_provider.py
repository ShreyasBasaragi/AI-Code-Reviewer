"""Offline tests for artifact validation and the lazy CUDA PEFT provider."""

from __future__ import annotations

import json
import ipaddress
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm import provider_factory
from llm.qwen_peft import (
    QwenArtifactError, QwenDeviceError, QwenInferenceError, QwenPEFTProvider,
    QwenRuntimeConfig, _check_cuda, inspect_adapter_against_config,
    inspect_qwen_artifacts,
)
from review_schema import ReviewFinding


TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


@pytest.fixture(autouse=True)
def prohibit_model_network_access(monkeypatch):
    """Keep this provider test module offline even if a code path regresses."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    original_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and address and isinstance(address[0], str):
            try:
                if not ipaddress.ip_address(address[0]).is_loopback:
                    raise AssertionError("Unexpected external network connection in local model tests.")
            except ValueError:
                raise AssertionError("Unexpected hostname connection in local model tests.")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Unexpected URL request in local model tests.")
    ))


def _shapes(layers=36, hidden=2048, inter=11008, rank=16, heads=16, kv_heads=2):
    kv_dim = kv_heads * (hidden // heads)
    dimensions = {
        "q_proj": (hidden, hidden), "k_proj": (hidden, kv_dim), "v_proj": (hidden, kv_dim),
        "o_proj": (hidden, hidden), "gate_proj": (hidden, inter),
        "up_proj": (hidden, inter), "down_proj": (inter, hidden),
    }
    result = {}
    for layer in range(layers):
        for module, (input_dim, output_dim) in dimensions.items():
            branch = "self_attn" if module in {"q_proj", "k_proj", "v_proj", "o_proj"} else "mlp"
            stem = f"base_model.model.model.layers.{layer}.{branch}.{module}"
            result[f"{stem}.lora_A.weight"] = (rank, input_dim)
            result[f"{stem}.lora_B.weight"] = (output_dim, rank)
    return result


def _fixtures(tmp_path, *, layers=36, hidden=2048, inter=11008, rank=16):
    base, adapter = tmp_path / "base", tmp_path / "adapter"
    base.mkdir(parents=True)
    adapter.mkdir()
    config = {
        "model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"],
        "hidden_size": hidden, "intermediate_size": inter, "num_hidden_layers": layers,
        "num_attention_heads": 16, "num_key_value_heads": 2, "vocab_size": 151936,
    }
    (base / "config.json").write_text(json.dumps(config))
    (base / "tokenizer_config.json").write_text(json.dumps({"tokenizer_class": "Qwen2Tokenizer", "chat_template": "fixture"}))
    for name in ("tokenizer.json", "vocab.json", "merges.txt"):
        (base / name).write_text("fixture")
    (base / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"weight": "shard.safetensors"}}))
    (base / "shard.safetensors").write_bytes(b"fixture")
    adapter_config = {
        "base_model_name_or_path": "Qwen/Qwen2.5-Coder-3B-Instruct",
        "peft_type": "LORA", "task_type": "CAUSAL_LM", "inference_mode": True,
        "r": rank, "lora_alpha": 32, "lora_dropout": 0.05, "target_modules": TARGETS,
    }
    (adapter / "adapter_config.json").write_text(json.dumps(adapter_config))
    (adapter / "adapter_model.safetensors").write_bytes(b"header fixture")
    return base, adapter, config, _shapes(layers, hidden, inter, rank)


def test_artifact_report_checks_cached_files_and_all_adapter_tensor_shapes(tmp_path):
    base, adapter, _, shapes = _fixtures(tmp_path)
    report = inspect_qwen_artifacts(base, adapter, tensor_shape_reader=lambda _path: shapes)
    assert report["compatible"]
    assert report["adapter_tensor_count"] == 504
    assert report["adapter_layer_module_pair_count"] == 252
    assert report["chat_template_present"] and report["base_weight_shards"] == 1


def test_artifact_report_rejects_missing_files_incompatible_base_and_bad_shapes(tmp_path):
    base, adapter, config, shapes = _fixtures(tmp_path / "missing")
    (base / "merges.txt").unlink()
    with pytest.raises(QwenArtifactError, match="merges.txt"):
        inspect_qwen_artifacts(base, adapter, tensor_shape_reader=lambda _path: shapes)

    _, adapter, config, shapes = _fixtures(tmp_path / "wrongbase")
    adapter_config = json.loads((adapter / "adapter_config.json").read_text())
    adapter_config["base_model_name_or_path"] = "different/model"
    (adapter / "adapter_config.json").write_text(json.dumps(adapter_config))
    with pytest.raises(QwenArtifactError, match="base-model identifier"):
        inspect_adapter_against_config(adapter, config, tensor_shape_reader=lambda _path: shapes)

    _, adapter, config, shapes = _fixtures(tmp_path / "badshape")
    key = next(key for key in shapes if key.endswith("q_proj.lora_A.weight"))
    shapes[key] = (3, 8)
    with pytest.raises(QwenArtifactError, match="shape mismatch"):
        inspect_adapter_against_config(adapter, config, tensor_shape_reader=lambda _path: shapes)


def test_artifact_report_rejects_unsafe_pickle_only_adapter(tmp_path):
    _, adapter, config, shapes = _fixtures(tmp_path)
    (adapter / "adapter_model.safetensors").unlink()
    (adapter / "adapter_model.bin").write_bytes(b"pickle")
    with pytest.raises(QwenArtifactError, match="safetensors"):
        inspect_adapter_against_config(adapter, config, tensor_shape_reader=lambda _path: shapes)


class FakeCuda:
    def __init__(self, *, available=True, count=1, free=12 * 1024**3, total=16 * 1024**3, bf16=True):
        self.available, self.count, self.free, self.total, self.bf16 = available, count, free, total, bf16
        self.OutOfMemoryError = type("OutOfMemoryError", (RuntimeError,), {})

    def is_available(self): return self.available
    def device_count(self): return self.count
    def mem_get_info(self, _index): return self.free, self.total
    def get_device_properties(self, _index): return SimpleNamespace(name="Mock CUDA", total_memory=self.total)
    def is_bf16_supported(self): return self.bf16


class FakeTorch:
    float16, bfloat16 = "float16", "bfloat16"
    def __init__(self, cuda): self.cuda = cuda
    @staticmethod
    def inference_mode(): return nullcontext()


def test_cuda_gate_rejects_missing_cuda_low_memory_bad_device_and_unsupported_bf16():
    config = QwenRuntimeConfig()
    with pytest.raises(QwenDeviceError, match="CUDA is unavailable"):
        _check_cuda(FakeTorch(FakeCuda(available=False)), config)
    with pytest.raises(QwenDeviceError, match="device cuda:1"):
        _check_cuda(FakeTorch(FakeCuda(count=1)), QwenRuntimeConfig(device="cuda:1"))
    with pytest.raises(QwenDeviceError, match="Insufficient free GPU memory"):
        _check_cuda(FakeTorch(FakeCuda(free=3 * 1024**3)), config)
    with pytest.raises(QwenDeviceError, match="BF16 precision is unsupported"):
        _check_cuda(FakeTorch(FakeCuda(bf16=False)), QwenRuntimeConfig(precision="bfloat16"))
    assert _check_cuda(FakeTorch(FakeCuda()), config)[0] == "cuda:0"


class FakeTokenIds(list):
    @property
    def shape(self):
        return (len(self), len(self[0]))


class FakeInput(dict):
    def to(self, device):
        self.device = device
        return self


class FakeTokenizer:
    pad_token_id, eos_token_id = 0, 2
    pad_token, eos_token = None, "<eos>"

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        text = "\n".join(message["content"] for message in messages)
        return list(range(max(1, len(text) // 8))) if tokenize else "chat-template:" + text

    def __call__(self, prompt, **kwargs):
        assert kwargs["truncation"] is False
        return FakeInput(input_ids=FakeTokenIds([[1, 2, 3, 4]]))

    def decode(self, tokens, *, skip_special_tokens):
        return f"generated:{tokens}"


class FakeModel:
    def __init__(self, delay=0):
        self.device = "cuda:0"
        self.calls, self.active, self.peak_active = [], 0, 0
        self.lock, self.delay = threading.Lock(), delay

    def generate(self, **kwargs):
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            time.sleep(self.delay)
            self.calls.append(kwargs)
            return [[1, 2, 3, 4, 90, 91]]
        finally:
            with self.lock:
                self.active -= 1

    def eval(self):
        self.evaluated = True
        return self


def _provider(config=None, *, model=None):
    tokenizer, model = FakeTokenizer(), model or FakeModel()
    provider = QwenPEFTProvider(
        config or QwenRuntimeConfig(),
        runtime_loader=lambda _config: (tokenizer, model, FakeTorch(FakeCuda())),
    )
    return provider, model


def test_provider_builds_qwen_chat_prompt_and_decodes_only_generated_tokens():
    provider, model = _provider(QwenRuntimeConfig(max_new_tokens=37, temperature=0))
    result = provider.analyze_code("def f(): return 1", ["evidence_id: E1\\n" + "rule text " * 4])
    assert result == "generated:[90, 91]"
    assert provider.last_generation_metadata["prompt_token_count"] == 4
    assert provider.last_generation_metadata["generated_token_count"] == 2
    assert provider.last_generation_metadata["generation_seconds"] >= 0
    assert model.calls[0]["max_new_tokens"] == 37
    assert model.calls[0]["do_sample"] is False and model.calls[0]["temperature"] == 0
    messages = provider._build_messages("x = 1", ["evidence_id: E1\\nrule text"])
    assert "Return exactly one JSON object" in messages[0]["content"]
    assert "<untrusted_reference>" in messages[1]["content"]
    assert "evidence_id: E1" in messages[1]["content"]


def test_provider_refuses_to_silently_truncate_source_or_run_code():
    provider, _ = _provider(QwenRuntimeConfig(max_input_tokens=1024))
    with pytest.raises(QwenInferenceError, match="source was not truncated"):
        provider.analyze_code("x = '" + ("a" * 10000) + "'")
    finding = ReviewFinding(
        title="Unsafe call", issue_type="security", explanation="Unsafe.",
        recommended_fix="Use safe parsing.", finding_id="static-1", origin="static",
    )
    assert provider.generate_fix("value = eval(text)", finding, []) == "generated:[90, 91]"


def test_provider_bounds_concurrent_inference_and_sanitizes_errors():
    model = FakeModel(delay=0.03)
    provider, _ = _provider(QwenRuntimeConfig(inference_concurrency=1), model=model)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(provider.analyze_code, ("x = 1", "x = 2")))
    assert model.peak_active == 1

    class BrokenModel(FakeModel):
        def generate(self, **_kwargs):
            raise RuntimeError("secret/path/credential")

    broken, _ = _provider(model=BrokenModel())
    with pytest.raises(QwenInferenceError, match="RuntimeError") as error:
        broken.analyze_code("x = 1")
    assert "secret/path" not in str(error.value)


def test_provider_factory_uses_hosted_defaults_and_separate_fix_setting(monkeypatch):
    monkeypatch.delenv("AI_CODE_REVIEWER_PROVIDER", raising=False)
    monkeypatch.delenv("AI_CODE_REVIEWER_FIX_PROVIDER", raising=False)
    import llm.provider_settings as settings_module
    monkeypatch.setattr(settings_module, "PROVIDER_SETTINGS", settings_module.ProviderSettingsStore())
    from llm.provider_factory import configured_provider_name

    assert configured_provider_name("review") == "groq"
    assert configured_provider_name("fix") == "groq"
    monkeypatch.setenv("AI_CODE_REVIEWER_PROVIDER", "qwen")
    monkeypatch.setenv("AI_CODE_REVIEWER_FIX_PROVIDER", "groq")
    assert configured_provider_name("review") == "qwen_local"
    assert configured_provider_name("fix") == "groq"
    with pytest.raises(ValueError, match="Provider must be"):
        provider_factory.get_provider("unknown")


def test_provider_factory_initializes_a_selected_provider_once(monkeypatch):
    provider_factory.clear_provider_cache_for_tests()
    import llm.qwen_client as qwen_client
    created = []

    class FakeProvider:
        def __init__(self):
            time.sleep(0.02)
            created.append(self)

    monkeypatch.setattr(qwen_client, "QwenLLM", FakeProvider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        providers = list(pool.map(lambda _i: provider_factory.get_provider("qwen_local"), range(4)))
    assert len(created) == 1 and all(provider is created[0] for provider in providers)
    provider_factory.clear_provider_cache_for_tests()


def test_http_manual_and_github_review_share_explicit_provider_without_model_load(monkeypatch):
    from fastapi.testclient import TestClient
    from mcp_server.github_pr import GitHubPRReviewService
    from mcp_server.http_api import ReviewFixService, create_app
    from mcp_server.state import LifecycleState

    class FakeReviewProvider:
        def __init__(self): self.calls = []
        def analyze_code(self, code, context):
            self.calls.append((code, context))
            return json.dumps({"summary": "No actionable issues.", "findings": []})

    class FakePRClient:
        def get_pull(self, ref):
            return {
                "number": ref.number, "title": "Test", "state": "open", "changed_files": 1,
                "base": {"sha": "base", "repo": {"name": ref.repository, "owner": {"login": ref.owner}}},
                "head": {"sha": "head"},
            }
        def get_files(self, _ref, *, max_files, max_pages):
            return ([{"filename": "src.py", "status": "modified", "additions": 1, "deletions": 1,
                      "patch": "@@ -1,1 +1,1 @@\n-value = 1\n+value = 2"}], False, 1, None)
        def get_file(self, _ref, path, sha, *, max_source_bytes):
            return "value = 2\n" if sha == "head" else "value = 1\n"

    provider = FakeReviewProvider()
    names = []
    monkeypatch.setenv("AI_CODE_REVIEWER_FIX_PROVIDER", "groq")
    monkeypatch.setattr(provider_factory, "get_provider", lambda name: names.append(name) or provider)
    service = ReviewFixService(
        provider_name="qwen_local", retriever=lambda _code: [], state=LifecycleState(),
        github_pr_service=GitHubPRReviewService(FakePRClient()),
    )
    client = TestClient(create_app(service))
    manual = client.post("/api/reviews", json={"code": "value = 2\n"})
    pr = client.post("/api/github/pr-reviews", json={"url": "https://github.com/acme/repo/pull/2"})
    assert manual.status_code == 201 and manual.json()["review"]["valid"]
    assert pr.status_code == 200 and pr.json()["files_reviewed"] == 1
    assert len(provider.calls) == 2
    assert names == ["qwen_local"]


def test_http_service_can_select_review_and_fix_providers_independently(monkeypatch):
    from mcp_server.http_api import ReviewFixService

    names = []
    monkeypatch.setenv("AI_CODE_REVIEWER_FIX_PROVIDER", "groq")
    monkeypatch.setenv("AI_CODE_REVIEWER_SOURCE_SHARING_CONSENT", "true")
    import llm.provider_settings as provider_settings
    monkeypatch.setattr(provider_settings, "PROVIDER_SETTINGS", provider_settings.ProviderSettingsStore())
    monkeypatch.setattr(provider_factory, "get_provider", lambda name: names.append(name) or object())
    service = ReviewFixService(provider_name="qwen_local")
    service._get_provider("review")
    service._get_provider("fix")
    assert names == ["qwen_local", "groq"]


def test_model_loader_uses_local_only_files_and_attaches_peft(monkeypatch):
    import llm.qwen_peft as qwen
    calls, base_model = [], FakeModel()

    class Config:
        model_type, architectures = "qwen2", ["Qwen2ForCausalLM"]
        hidden_size, intermediate_size, num_hidden_layers = 2048, 11008, 36
        num_attention_heads, num_key_value_heads, vocab_size = 16, 2, 151936
        def to_dict(self): return dict(num_hidden_layers=36, hidden_size=2048, intermediate_size=11008, num_attention_heads=16, num_key_value_heads=2)

    class AutoConfig:
        @staticmethod
        def from_pretrained(*_args, **kwargs):
            calls.append(("config", kwargs))
            return Config()

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*_args, **kwargs):
            calls.append(("tokenizer", kwargs))
            return FakeTokenizer()

    class AutoModel:
        @staticmethod
        def from_pretrained(*_args, **kwargs):
            calls.append(("model", kwargs))
            return base_model

    class PeftModel:
        @staticmethod
        def from_pretrained(model, adapter, **kwargs):
            calls.append(("adapter", adapter, kwargs))
            return model

    torch = FakeTorch(FakeCuda())
    torch.float16 = "half"
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoConfig=AutoConfig, AutoTokenizer=AutoTokenizer, AutoModelForCausalLM=AutoModel,
    ))
    monkeypatch.setitem(sys.modules, "peft", SimpleNamespace(PeftModel=PeftModel))
    monkeypatch.setattr(qwen, "inspect_adapter_against_config", lambda *_a, **_kw: {"adapter_tensor_count": 504})
    provider = QwenPEFTProvider(QwenRuntimeConfig(base_model="local/base", adapter_dir=Path("local/adapter")))
    assert provider.model is base_model and base_model.evaluated
    assert all(call[1].get("local_files_only") is True for call in calls if call[0] in {"config", "tokenizer", "model"})
    assert calls[-1][0] == "adapter" and calls[-1][2]["local_files_only"] is True


def test_model_loader_fails_before_loading_when_cuda_is_unavailable(monkeypatch):
    class NoCuda:
        cuda = FakeCuda(available=False)

    monkeypatch.setitem(sys.modules, "torch", NoCuda())
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoConfig=object(), AutoTokenizer=object(), AutoModelForCausalLM=object(),
    ))
    monkeypatch.setitem(sys.modules, "peft", SimpleNamespace(PeftModel=object()))
    with pytest.raises(QwenDeviceError, match="CUDA is unavailable"):
        QwenPEFTProvider(QwenRuntimeConfig())

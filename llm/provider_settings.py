"""Authenticated backend provider settings; secrets remain server-side in memory.

No secure secret vault is configured by default. The optional API-key update is
process-memory-only and disappears on restart; deployments should prefer a
secret manager or environment injection. Public snapshots never contain keys.

Provider roles:
  - review_provider / fix_provider: local Qwen fine-tuned model (DEFAULT)
    Override via AI_CODE_REVIEWER_PROVIDER / AI_CODE_REVIEWER_FIX_PROVIDER env vars.
  - Groq API: used ONLY for Easy/Hard classification (always via GROQ_API_KEY env var).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Mapping

from llm.groq_client import DEFAULT_MODEL
from llm.qwen_peft import DEFAULT_ADAPTER_DIR, DEFAULT_BASE_MODEL_ID


_GROQ_MODELS = frozenset({DEFAULT_MODEL})
_QWEN_QUANTIZATION = frozenset({"none"})


class ProviderSettingsError(ValueError):
    pass


class ProviderSettingsStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._secret = os.getenv("GROQ_API_KEY")
        self._settings: dict[str, Any] = {
            # Default to local Qwen for both review and fix.
            # Groq is used ONLY for Easy/Hard classification (not configurable here).
            "review_provider": _provider_name(os.getenv("AI_CODE_REVIEWER_PROVIDER", "qwen_local")),
            "fix_provider": _provider_name(os.getenv("AI_CODE_REVIEWER_FIX_PROVIDER", "qwen_local")),
            "groq_model": os.getenv("GROQ_MODEL", DEFAULT_MODEL),
            "qwen_base_model": os.getenv("QWEN_BASE_MODEL_ID", DEFAULT_BASE_MODEL_ID),
            "qwen_adapter_path": str(Path(os.getenv("QWEN_ADAPTER_PATH", str(DEFAULT_ADAPTER_DIR))).expanduser()),
            "qwen_quantization": "none",
            "review_temperature": _environment_temperature("AI_CODE_REVIEWER_REVIEW_TEMPERATURE", "QWEN_TEMPERATURE", 0.1),
            "fix_temperature": _environment_temperature("AI_CODE_REVIEWER_FIX_TEMPERATURE", "QWEN_TEMPERATURE", 0.1),
            "max_output_tokens": None,
            "source_sharing_consent": os.getenv("AI_CODE_REVIEWER_SOURCE_SHARING_CONSENT", "false").casefold() == "true",
        }
        self._settings = self._validate(self._settings)

    def public_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._settings,
                    "groq_api_key_configured": bool(self._secret),
                    "groq_connectivity": "not_checked",
                    "qwen_readiness": self._qwen_readiness(),
                    "generation": self._generation,
                    "secret_storage": "process_memory_only"}

    def task_snapshot(self, task: str) -> dict[str, Any]:
        if task not in {"review", "fix"}:
            raise ProviderSettingsError("invalid_provider_task")
        with self._lock:
            result = dict(self._settings)
            result["provider"] = result[f"{task}_provider"]
            result["temperature"] = result[f"{task}_temperature"]
            result["api_key"] = self._secret
            result["generation"] = self._generation
            return result

    def update(self, values: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise ProviderSettingsError("settings_object_required")
        allowed = set(self._settings) | {"groq_api_key"}
        if set(values) - allowed:
            raise ProviderSettingsError("unknown_provider_setting")
        with self._lock:
            candidate = dict(self._settings)
            candidate.update({key: value for key, value in values.items() if key != "groq_api_key"})
            validated = self._validate(candidate)
            secret = values.get("groq_api_key", self._secret)
            if secret is not None and secret != "" and (not isinstance(secret, str) or not 20 <= len(secret) <= 512 or "\n" in secret):
                raise ProviderSettingsError("invalid_groq_api_key")
            self._settings = validated
            self._secret = None if secret == "" else secret
            self._generation += 1
        return self.public_snapshot()

    def _validate(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(values)
        result["review_provider"] = _provider_name(result.get("review_provider"))
        result["fix_provider"] = _provider_name(result.get("fix_provider"))
        if not isinstance(result.get("source_sharing_consent"), bool):
            raise ProviderSettingsError("source_sharing_consent_must_be_boolean")
        model = result.get("groq_model")
        if not isinstance(model, str) or model not in _GROQ_MODELS:
            raise ProviderSettingsError("groq_model_not_approved")
        if result.get("qwen_base_model") != DEFAULT_BASE_MODEL_ID:
            raise ProviderSettingsError("qwen_base_model_not_approved")
        if result.get("qwen_quantization") not in _QWEN_QUANTIZATION:
            raise ProviderSettingsError("qwen_quantization_not_supported")
        path = result.get("qwen_adapter_path")
        if not isinstance(path, str):
            raise ProviderSettingsError("qwen_adapter_path_invalid")
        resolved = Path(path).expanduser().resolve(strict=False)
        configured_adapter = os.getenv("QWEN_ADAPTER_PATH", str(DEFAULT_ADAPTER_DIR))
        roots = [Path(item).expanduser().resolve(strict=False) for item in
                 os.getenv("AI_CODE_REVIEWER_QWEN_ADAPTER_ROOTS", configured_adapter).split(os.pathsep)
                 if item.strip()]
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise ProviderSettingsError("qwen_adapter_path_not_approved")
        result["qwen_adapter_path"] = str(resolved)
        for name in ("review_temperature", "fix_temperature"):
            value = result.get(name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 1:
                raise ProviderSettingsError("temperature_out_of_range")
            result[name] = float(value)
        tokens = result.get("max_output_tokens")
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= 4096):
            raise ProviderSettingsError("max_output_tokens_out_of_range")
        result["qwen_quantization"] = "none"
        return result

    def _qwen_readiness(self) -> str:
        adapter = Path(self._settings["qwen_adapter_path"])
        return "artifacts_present_not_loaded" if adapter.is_dir() else "adapter_artifacts_missing"


def _provider_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ProviderSettingsError("provider_name_invalid")
    normalized = value.strip().casefold()
    if normalized == "qwen":
        normalized = "qwen_local"
    if normalized not in {"groq", "qwen_local"}:
        raise ProviderSettingsError("provider_name_not_approved")
    return normalized


def _environment_temperature(primary: str, legacy: str, default: float) -> float:
    try:
        value = float(os.getenv(primary, os.getenv(legacy, str(default))))
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= 1 else default


PROVIDER_SETTINGS = ProviderSettingsStore()

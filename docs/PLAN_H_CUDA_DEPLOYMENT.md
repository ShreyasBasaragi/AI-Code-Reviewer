# Plan H — CUDA deployment and validation

The application defaults to the hosted `groq` provider. The local LoRA provider
is selected only with `AI_CODE_REVIEWER_PROVIDER=qwen_local` (legacy `qwen` is
an alias). It requires NVIDIA CUDA, defaults to FP16, checks for 8 GiB free GPU
memory, and always uses local-only model loading. It does not quantize, fall
back to CPU/MPS, or download model files at application startup.

## 1. Provision model artifacts

Run provisioning separately from application startup on the target machine.
The cached model revision inspected for this adapter is
`488639f1ff808d1d3d0ba301aef8c11461451ec5`; use that same revision to keep the
base artifact fixed:

```sh
hf download Qwen/Qwen2.5-Coder-3B-Instruct \
  --revision 488639f1ff808d1d3d0ba301aef8c11461451ec5 \
  --local-dir /opt/ai-code-reviewer/models/qwen2.5-coder-3b-instruct
```

Place the supplied, extracted adapter directory at
`/opt/ai-code-reviewer/adapter`. Copy the original adapter artifact as supplied;
do not merge, rewrite, quantize, or commit it. The adapter and archive are
ignored by this repository's Git configuration.

## 2. Install a CUDA-compatible environment

Use Python 3.11 or newer and a compatible NVIDIA driver. Check the target first:

```sh
nvidia-smi
python3 --version
```

Create the environment and install the PyTorch wheel selected for the target's
OS and supported CUDA runtime:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Select **Linux / Pip / Python / the CUDA version supported by the installed
driver** in the official [PyTorch installation selector](https://pytorch.org/get-started/locally/),
then run its generated `pip install torch ... --index-url ...` command. Do not
reuse a CPU-only, macOS, or mismatched CUDA wheel. Install the remaining project
dependencies after PyTorch:

```sh
python -m pip install -r requirements.txt
```

Confirm CUDA support without loading model weights:

```sh
python - <<'PY'
import torch
print("torch", torch.__version__)
print("CUDA build", torch.version.cuda)
print("CUDA available", torch.cuda.is_available())
if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    print("GPU", p.name)
    print("VRAM GiB", round(p.total_memory / 1024**3, 2))
    print("BF16 supported", torch.cuda.is_bf16_supported())
PY
```

## 3. Verify files and adapter compatibility

Set paths and run the metadata-only checker. It reads JSON and safetensors
headers; it does not load model weights or contact Hugging Face:

```sh
export QWEN_BASE_MODEL_PATH=/opt/ai-code-reviewer/models/qwen2.5-coder-3b-instruct
export QWEN_ADAPTER_PATH=/opt/ai-code-reviewer/adapter
python scripts/verify_qwen_artifacts.py \
  --base-model-dir "$QWEN_BASE_MODEL_PATH" \
  --adapter-dir "$QWEN_ADAPTER_PATH"
```

Require `"compatible": true` and `"weights_loaded": false` before continuing.
The report includes the base architecture, tokenizer/chat template, adapter
rank, target-module coverage, tensor count, shard count, and installed package
versions. It fails closed on a missing base shard or a mismatched A/B shape.

## 4. Configure and start the local review provider

Set these values in the backend process environment before startup:

```sh
export AI_CODE_REVIEWER_PROVIDER=qwen_local
export QWEN_BASE_MODEL_PATH=/opt/ai-code-reviewer/models/qwen2.5-coder-3b-instruct
export QWEN_BASE_MODEL_ID=Qwen/Qwen2.5-Coder-3B-Instruct
export QWEN_ADAPTER_PATH=/opt/ai-code-reviewer/adapter
export QWEN_DEVICE=cuda:0
export QWEN_PRECISION=float16
export QWEN_MIN_FREE_GPU_MEMORY_GB=8
export QWEN_MAX_INPUT_TOKENS=8192
export QWEN_MAX_NEW_TOKENS=512
export QWEN_MAX_FIX_NEW_TOKENS=2048
export QWEN_TEMPERATURE=0.1
export QWEN_TOP_P=0.9
export QWEN_INFERENCE_CONCURRENCY=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
uvicorn mcp_server.http_api:app --host 127.0.0.1 --port 8000
```

The hosted `groq` provider remains the default when `AI_CODE_REVIEWER_PROVIDER`
is unset. `AI_CODE_REVIEWER_FIX_PROVIDER` independently selects fix generation
and defaults to `groq`; no fix-provider call occurs during a review. Local Qwen
fix generation must be separately selected and remains subject to the existing
schema, syntax, static verification, human approval, and export gates.

## 5. Run explicit CUDA validation

In a second terminal using the same environment, record GPU state and run the
validation script. This is the point at which weights are loaded and local model
inference is performed:

```sh
nvidia-smi
python scripts/validate_qwen_cuda.py | tee qwen-cuda-validation.json
nvidia-smi
```

Success requires JSON `"status": "passed"`, `"adapter_attached": true`, and
both standalone and orchestrator schema-valid flags set to `true`. The report
records GPU model/VRAM, precision, model-load time, inference and orchestrator
latency, and peak allocated/reserved GPU memory. This script runs the existing
orchestrator with the local provider and its normal RAG retrieval; offline
flags prevent automatic Hugging Face downloads if a retrieval model is absent.

## 6. Plan I offline and target validation commands

On the development Mac, run the offline backend and frontend validation and
mocked benchmark. These commands do not initialize a provider or load Qwen:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest -q
cd frontend && npm test && npm run build && cd ..
python -m evaluation.security_corpus --output /tmp/static-security-report.json
python -m evaluation.performance --warmups 2 --runs 10
```

The security corpus report covers only the existing deterministic PYSEC rules.
The performance report uses a mocked provider, so its model-call timing is not
an inference benchmark. Neither is a behavioral test.

On the NVIDIA target after provisioning and the section 5 smoke validation,
run the local quality fixture explicitly. Select `qwen_local` in the backend
environment, keep model paths local, and use `--rag-enabled` only if the
existing RAG database and embedding cache are provisioned on that host:

```sh
export AI_CODE_REVIEWER_PROVIDER=qwen_local
export AI_CODE_REVIEWER_FIX_PROVIDER=qwen_local
export QWEN_BASE_MODEL_PATH=/opt/ai-code-reviewer/models/qwen2.5-coder-3b-instruct
export QWEN_ADAPTER_PATH=/opt/ai-code-reviewer/adapter
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
python -m evaluation.quality --mode live-model --allow-live-model
```

This makes local CUDA inference requests. It does not call Groq unless the
provider is deliberately changed to Groq. For a repeated, warm-up-aware CUDA
performance report, choose a new output filename on every run:

```sh
python scripts/benchmark_qwen_cuda.py --warmups 2 --runs 10 \
  --output "evaluation/results/qwen_cuda_$(date -u +%Y%m%dT%H%M%SZ).json"
```

The script synchronizes CUDA before and after timing, reports model load,
prompt/generated token counts, generation throughput, end-to-end review
latency, and peak allocated/reserved memory. It runs a fixed synthetic prompt
with RAG disabled; it measures neither model quality nor behavior. Do not run
it on the development Mac.

Share only the JSON/CSV/Markdown reports after reviewing them for sensitive
source content and environment details. Never include credentials, adapter
files, or base-model weights. Preserve each timestamped report; do not replace
earlier benchmark outputs.

You can also submit a local manual review through the frontend or API after the
script passes. A private-repository PR review additionally needs a backend
`GITHUB_TOKEN` with read permissions, and must be explicitly authorized. The PR
workflow remains read-only and never publishes comments.

## 7. Interpreting failures

- **CUDA unavailable / wrong device:** install the CUDA build matching the
  target driver and check `CUDA_VISIBLE_DEVICES` and `QWEN_DEVICE`.
- **Insufficient VRAM:** close other GPU workloads or use a GPU with more free
  memory. The provider does not enable quantization or offload to CPU.
- **Artifact error:** rerun the metadata checker and compare the pinned base
  revision, adapter configuration, target coverage, and tensor dimensions.
- **Model load / generation error:** record Python and package versions, GPU
  name, and the sanitized exception type. Do not remove `local_files_only`.
- **Schema-invalid output:** the provider is loaded but has not passed the
  application's review-output contract; do not treat it as operational.
- **Pipeline retrieval failure:** the review orchestrator reports retrieval
  errors and continues without evidence; provision the existing RAG embedding
  cache if evidence retrieval is part of the deployment acceptance run.

Do not call the model operational until the target machine passes section 5.

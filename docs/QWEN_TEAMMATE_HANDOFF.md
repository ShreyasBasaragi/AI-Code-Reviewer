# Qwen CUDA Validation — Teammate Handoff

This task is owned by the teammate with the NVIDIA RTX 3050. No model weights were loaded and no Qwen inference was run during final release verification. The root `adapter/` is teammate-owned and was not edited. The Docker build context excludes `adapter/` and `llm/model` so those artifacts are not copied into the API image.

## Current state

- Provider implementation: `llm/qwen_peft.py::QwenPEFTProvider`, lazily loaded and CUDA-only.
- Configured base: `Qwen/Qwen2.5-Coder-3B-Instruct`; this differs from the FF-180 synopsis model alternatives and must be disclosed in results.
- Local adapter directory: `adapter/` (safetensors adapter and tokenizer metadata). Preserve the supplied files byte-for-byte.
- Provider requires local-only paths and defaults to an 8 GiB free-GPU-memory gate. An RTX 3050 configuration with less than that free memory may be rejected. Do not bypass the gate without a separately reviewed change.
- No automatic weight download, CPU/MPS fallback, quantization or offload is enabled by the current provider.
- The project defaults to Groq for hosted review, but this handoff uses only the Qwen local path. Never switch to Groq for these checks.

## Ordered validation

1. **Prepare the NVIDIA host:** record OS, Python, NVIDIA driver, GPU model, total/free VRAM, CUDA-enabled PyTorch build, and installed package versions. Do not share credentials or private source in the report.
2. **Check local artifacts without loading weights:** use `scripts/verify_qwen_artifacts.py` against the exact pinned local base directory and `adapter/`. Require `compatible: true` and `weights_loaded: false`. Preserve its JSON output after review for sensitive paths.
3. **Resolve compatibility first:** check base revision, tokenizer/chat template, LoRA rank/target modules, tensor shapes and package versions. If incompatible, stop and report; do not rewrite or merge adapter artifacts.
4. **Run CUDA smoke only on the intended GPU:** follow the explicit environment setup and `scripts/validate_qwen_cuda.py` instructions in [Plan H](PLAN_H_CUDA_DEPLOYMENT.md). This step loads weights and performs local inference. Require the script's passed status, adapter attached, and schema-valid standalone plus orchestrated outputs. Record load time, latency, peak allocated/reserved VRAM and sanitized failure type.
5. **Run quality/performance only after the smoke passes:** follow Plan H's separately marked local quality and benchmark commands. Record dataset/version, case count, labels, model/base revision, RAG on/off, warmups, runs, latency and quality metrics. Do not overwrite existing evaluation reports; choose a new output file.
6. **Review and send results:** share the sanitized reports with the project owner. Do not include adapter weights, base weights, credentials, raw private code, or unsanitized environment output. Do not commit, upload, publish, or otherwise transmit artifacts without separate authorization.

## Acceptance evidence

The handoff is complete only when a teammate provides:

- Metadata verification JSON with `compatible: true`, `weights_loaded: false` from the preflight.
- CUDA smoke JSON with passed status, correct GPU identity, adapter attached, schema-valid outputs, and resource/latency measurements.
- A separate quality report with documented data provenance and denominators; mocked-provider scores do not count as Qwen results.
- Explicit disclosure that this is the configured 3B model and a statement whether it satisfies the project's approved model choice.

A failure or insufficient VRAM is a valid result. Do not lower safety gates, change model settings, alter RAG strategy, or modify adapter artifacts to force a pass. The main project remains in provider-lazy state and must not claim Qwen as operational until this evidence is reviewed.

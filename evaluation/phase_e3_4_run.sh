#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ -z "${GROQ_API_KEY:-}" ]]; then
  echo "GROQ_API_KEY is not configured; no requests were made." >&2
  exit 2
fi
if [[ -n "${GROQ_MODEL:-}" && "$GROQ_MODEL" != "openai/gpt-oss-20b" ]]; then
  echo "GROQ_MODEL must be openai/gpt-oss-20b for this controlled comparison." >&2
  exit 2
fi
export GROQ_MODEL=openai/gpt-oss-20b
export AI_CODE_REVIEWER_PROVIDER=groq
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

COPY_ROOT="${PHASE_E3_4_COPY_ROOT:-${TMPDIR:-/tmp}/phase_e3_4_chroma_copies}"
V2_COPY="$COPY_ROOT/chroma_db_v2"
V3_COPY="$COPY_ROOT/chroma_db_v3"
if [[ ! -d "$V2_COPY" || ! -d "$V3_COPY" ]]; then
  echo "Expected verified non-canonical copies at $V2_COPY and $V3_COPY; no requests were made." >&2
  exit 2
fi

# Validate the v2 snapshot before any remaining evaluation calls. Legacy runs
# are accepted only when the checkpoint hash, current bytes, collection UUID,
# and count match; preflight reports that initial provenance is unavailable.
V2_CHECKPOINT="evaluation/results/phase_e3_4_groq_v2_baseline.checkpoint.json"
V2_PREFLIGHT_ARGS=(--copy-root "$COPY_ROOT" --rag-version v2
  --output "${TMPDIR:-/tmp}/phase_e3_4_preflight_v2_resume.json")
if [[ -f "$V2_CHECKPOINT" ]]; then
  V2_PREFLIGHT_ARGS+=(--resume-checkpoint "$V2_CHECKPOINT")
fi
.venv/bin/python -m evaluation.phase_e3_4_preflight "${V2_PREFLIGHT_ARGS[@]}"

run_config() {
  local suffix="$1"
  shift
  local output="evaluation/results/phase_e3_4_groq_${suffix}.json"
  local checkpoint="evaluation/results/phase_e3_4_groq_${suffix}.checkpoint.json"
  local resume_args=()
  if [[ -f "$checkpoint" ]]; then
    if [[ "${PHASE_E3_4_RESUME:-0}" != "1" ]]; then
      echo "Checkpoint exists for $suffix. Resolve the cause, then explicitly set PHASE_E3_4_RESUME=1 to resume." >&2
      exit 2
    fi
    resume_args+=(--resume)
  fi
  local rag_version=""
  case "$suffix" in
    v2_baseline) rag_version="v2" ;;
    v3_baseline|v3_optimized) rag_version="v3" ;;
  esac
  if [[ -n "$rag_version" ]]; then
    local preflight_args=(--copy-root "$COPY_ROOT" --rag-version "$rag_version"
      --output "${TMPDIR:-/tmp}/phase_e3_4_preflight_${suffix}.json")
    if [[ -f "$checkpoint" ]]; then
      preflight_args+=(--resume-checkpoint "$checkpoint")
    fi
    .venv/bin/python -m evaluation.phase_e3_4_preflight "${preflight_args[@]}"
  fi
  .venv/bin/python -m evaluation.runner \
    --mode groq --enable-live-groq --max-cases 30 \
    --request-interval-seconds 12 \
    --output "$output" --checkpoint-path "$checkpoint" \
    --stop-on-provider-error --fail-on-incomplete "${resume_args[@]+"${resume_args[@]}"}" "$@"
}

# Sequential execution: an incomplete configuration exits nonzero before the next begins.
run_config no_rag
run_config v2_baseline --rag-copy-dir "$V2_COPY" --rag-version v2 --retrieval-strategy v2_baseline
run_config v3_baseline --rag-copy-dir "$V3_COPY" --rag-version v3 --retrieval-strategy v3_baseline \
  --disable-v3-parent-aware --disable-v3-language-aware --disable-v3-source-diversity \
  --disable-v3-applicability-diagnostics --disable-v3-context-budgeting
run_config v3_optimized --rag-copy-dir "$V3_COPY" --rag-version v3 --retrieval-strategy v3_optimized

.venv/bin/python -m evaluation.phase_e3_4_analysis

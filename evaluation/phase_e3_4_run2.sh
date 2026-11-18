#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ -z "${GROQ_API_KEY:-}" ]]; then
  echo "GROQ_API_KEY is not configured; no Groq calls were made." >&2
  exit 2
fi
if [[ "${GROQ_MODEL:-openai/gpt-oss-20b}" != "openai/gpt-oss-20b" ]]; then
  echo "GROQ_MODEL must be openai/gpt-oss-20b for Run 2." >&2
  exit 2
fi
if [[ -z "${GROQ_REMAINING_TPD:-}" || ! "$GROQ_REMAINING_TPD" =~ ^[0-9]+$ ]]; then
  echo "Set GROQ_REMAINING_TPD to the current remaining daily-token value from the provider dashboard." >&2
  echo "No API key value is requested or printed." >&2
  exit 2
fi

export GROQ_MODEL=openai/gpt-oss-20b
export AI_CODE_REVIEWER_PROVIDER=groq
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

RUN2_ARMS="${PHASE_E3_4_RUN2_ARMS-v3_baseline,v3_optimized}"
SAFETY_FRACTION="${PHASE_E3_4_RUN2_SAFETY_FRACTION-0.8}"
MAX_NEW_PAIRS="${PHASE_E3_4_RUN2_MAX_NEW_PAIRS-15}"
SELECTED_ARMS="$(.venv/bin/python -m evaluation.phase_e3_4_run2 select-arms --value "$RUN2_ARMS")"
IFS=, read -r -a ARM_LIST <<< "$SELECTED_ARMS"

SNAPSHOT_ROOT="${PHASE_E3_4_RUN2_SNAPSHOT_ROOT:-${TMPDIR:-/tmp}/phase_e3_4_run2_chroma_snapshots}"
WORKING_ROOT="${PHASE_E3_4_RUN2_WORKING_ROOT:-${TMPDIR:-/tmp}/phase_e3_4_run2_working}"
RUN_DIR="$PROJECT_ROOT/evaluation/results/phase_e3_4_run2"
STATE="$RUN_DIR/rate_limit_usage.json"
RUN2_INCOMPLETE=0

write_partial_report() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && -d "$RUN_DIR" ]]; then
    .venv/bin/python -m evaluation.phase_e3_4_analysis --run2 --results-dir "$RUN_DIR" || true
  fi
  exit "$status"
}
trap write_partial_report EXIT

if [[ ! -f "$RUN_DIR/snapshot_manifest.json" ]]; then
  echo "Run 2 snapshot manifest is missing. Run evaluation/phase_e3_4_prepare_run2.sh first." >&2
  exit 2
fi
for arm in "${ARM_LIST[@]}"; do
  if [[ "$arm" != "no_rag" && ! -f "$WORKING_ROOT/$arm/chroma.sqlite3" ]]; then
    echo "Working copy for selected arm $arm is missing. Recreate the selected Run 2 snapshot set first." >&2
    exit 2
  fi
done

PREFLIGHT_ARGS=(preflight --snapshot-root "$SNAPSHOT_ROOT" --working-root "$WORKING_ROOT"
  --run-dir "$RUN_DIR" --remaining-daily-tokens "$GROQ_REMAINING_TPD"
  --arms "$SELECTED_ARMS" --safety-fraction "$SAFETY_FRACTION"
  --max-new-pairs "$MAX_NEW_PAIRS")
CONFIRM_RESET=0
if [[ "${PHASE_E3_4_RUN2_CONFIRM_DAILY_RESET:-0}" == "1" ]]; then
  PREFLIGHT_ARGS+=(--confirm-daily-quota-reset)
  CONFIRM_RESET=1
fi
if [[ "${PHASE_E3_4_RUN2_RESUME:-0}" == "1" ]]; then
  PREFLIGHT_ARGS+=(--resume)
fi

# This preflight is local-only. It verifies the pinned plan, snapshots, and
# quota projection; it does not call Groq.
.venv/bin/python -m evaluation.phase_e3_4_run2 "${PREFLIGHT_ARGS[@]}"
PREFLIGHT_STATUS="$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$RUN_DIR/preflight.json")"
if [[ "$PREFLIGHT_STATUS" == "completed" ]]; then
  echo "All selected experiment cases are already complete. No Groq calls were made."
  exit 0
fi
if [[ "$PREFLIGHT_STATUS" != "ready" ]]; then
  echo "Run 2 preflight is $PREFLIGHT_STATUS. No Groq calls were made." >&2
  exit 2
fi
BATCH_CASE_IDS="$(.venv/bin/python -c 'import json,sys; print(",".join(json.load(open(sys.argv[1]))["next_batch"]["selected_pending_case_ids"]))' "$RUN_DIR/preflight.json")"
if [[ -z "$BATCH_CASE_IDS" ]]; then
  echo "Preflight found no quota-fitting pending case pairs. No Groq calls were made." >&2
  exit 2
fi

run_config() {
  local arm="$1"
  local output="$RUN_DIR/groq_${arm}.json"
  local checkpoint="$RUN_DIR/groq_${arm}.checkpoint.json"
  local has_checkpoint=0
  if [[ -f "$checkpoint" ]]; then
    if [[ "${PHASE_E3_4_RUN2_RESUME:-0}" != "1" ]]; then
      echo "Run 2 checkpoint already exists for $arm. Set PHASE_E3_4_RUN2_RESUME=1 after reviewing the cause." >&2
      exit 2
    fi
    has_checkpoint=1
  fi

  local run_args=(--mode groq --enable-live-groq --max-cases 30
    --batch-case-ids "$BATCH_CASE_IDS"
    --request-interval-seconds 20 --stop-on-provider-error --fail-on-incomplete
    --output "$output" --checkpoint-path "$checkpoint"
    --rate-limit-state-path "$STATE" --rate-limit-arm "$arm"
    --remaining-daily-tokens "$GROQ_REMAINING_TPD"
    --groq-rpm 30 --groq-rpd 1000 --groq-tpm 8000 --groq-tpd 200000
    --rate-limit-safety-fraction "$SAFETY_FRACTION"
    --rate-limit-max-wait-seconds 3600)
  if [[ "$has_checkpoint" == "1" ]]; then run_args+=(--resume); fi

  if [[ "$arm" != "no_rag" ]]; then
    local version="v2"; [[ "$arm" == v3_* ]] && version="v3"
    local identity
    identity="$(.venv/bin/python -m evaluation.phase_e3_4_run2 identity --run-dir "$RUN_DIR" --arm "$arm")"
    local ref_sha manifest_sha work_sha work_path
    ref_sha="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["reference_snapshot_sha256"])' <<<"$identity")"
    manifest_sha="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["reference_manifest_sha256"])' <<<"$identity")"
    work_sha="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["working_copy_manifest_sha256"])' <<<"$identity")"
    work_path="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["working_copy_path"])' <<<"$identity")"
    run_args+=(--rag-copy-dir "$work_path" --rag-version "$version"
      --rag-snapshot-identity "$ref_sha"
      --rag-snapshot-manifest-sha256 "$manifest_sha"
      --working-copy-manifest-sha256 "$work_sha")
    if [[ "$arm" == "v2_baseline" ]]; then
      run_args+=(--retrieval-strategy v2_baseline)
    elif [[ "$arm" == "v3_baseline" ]]; then
      run_args+=(--retrieval-strategy v3_baseline --disable-v3-parent-aware
        --disable-v3-language-aware --disable-v3-source-diversity
        --disable-v3-applicability-diagnostics --disable-v3-context-budgeting)
    else
      run_args+=(--retrieval-strategy v3_optimized)
    fi
    .venv/bin/python -m evaluation.phase_e3_4_run2 verify-working \
      --working-root "$WORKING_ROOT" --run-dir "$RUN_DIR" --arm "$arm" \
      > "${TMPDIR:-/tmp}/phase_e3_4_run2_${arm}_integrity_before.json"
  fi

  if [[ "$CONFIRM_RESET" == "1" ]]; then
    run_args+=(--confirm-daily-quota-reset)
    CONFIRM_RESET=0
  fi
  local runner_log="${TMPDIR:-/tmp}/phase_e3_4_run2_${arm}_runner.log"
  local runner_status=0
  .venv/bin/python -m evaluation.runner "${run_args[@]}" > "$runner_log" 2>&1 || runner_status=$?
  cat "$runner_log"
  if [[ "$runner_status" -ne 0 ]]; then
    if grep -q "Evaluation incomplete; results/checkpoint were saved." "$runner_log"; then
      # A failed/invalid case is checkpointed and retryable. Preserve the
      # incomplete signal, but let the other selected arm run this batch.
      RUN2_INCOMPLETE=1
      echo "Arm $arm has incomplete case results; continuing to the next selected arm." >&2
    else
      return "$runner_status"
    fi
  fi

  if [[ "$arm" != "no_rag" ]]; then
    .venv/bin/python -m evaluation.phase_e3_4_run2 verify-working \
      --working-root "$WORKING_ROOT" --run-dir "$RUN_DIR" --arm "$arm" \
      > "${TMPDIR:-/tmp}/phase_e3_4_run2_${arm}_integrity_after.json"
  fi
}

# Only explicitly selected arms are run. Each selected arm has an independent
# result/checkpoint. All arms receive the same deterministic pending case-pair
# batch; completed cases are skipped by each arm's checkpoint.
for arm in "${ARM_LIST[@]}"; do run_config "$arm"; done

.venv/bin/python -m evaluation.phase_e3_4_analysis --run2 --results-dir "$RUN_DIR"
if [[ "$RUN2_INCOMPLETE" == "1" ]]; then
  echo "Run 2 batch finished with incomplete cases. Both arms were attempted where possible; checkpoints are preserved for resume." >&2
  exit 2
fi

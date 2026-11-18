#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

RUN2_ARMS="${PHASE_E3_4_RUN2_ARMS-v3_baseline,v3_optimized}"
SELECTED_ARMS="$(.venv/bin/python -m evaluation.phase_e3_4_run2 select-arms --value "$RUN2_ARMS")"
IFS=, read -r -a ARM_LIST <<< "$SELECTED_ARMS"
NEED_V2=0
NEED_V3=0
for arm in "${ARM_LIST[@]}"; do
  case "$arm" in
    v2_baseline) NEED_V2=1 ;;
    v3_baseline|v3_optimized) NEED_V3=1 ;;
  esac
done

SNAPSHOT_ROOT="${PHASE_E3_4_RUN2_SNAPSHOT_ROOT:-${TMPDIR:-/tmp}/phase_e3_4_run2_chroma_snapshots}"
WORKING_ROOT="${PHASE_E3_4_RUN2_WORKING_ROOT:-${TMPDIR:-/tmp}/phase_e3_4_run2_working}"
RUN_DIR="$PROJECT_ROOT/evaluation/results/phase_e3_4_run2"

if [[ -e "$SNAPSHOT_ROOT" || -e "$WORKING_ROOT" ]]; then
  echo "Run 2 snapshot/working root already exists. Refusing reuse or overwrite." >&2
  exit 2
fi
if [[ -e "$RUN_DIR" ]]; then
  echo "Run 2 result namespace already exists. Refusing to overwrite prior artifacts." >&2
  exit 2
fi

if pgrep -fl 'uvicorn|mcp_server\.(http_api|server)|rag\.(ingest|build_v3)|evaluation\.(runner|phase_e3_4_run2)' >/dev/null; then
  echo "A likely application, API/MCP, ingestion, or evaluation process is active. Stop it before copying." >&2
  pgrep -fl 'uvicorn|mcp_server\.(http_api|server)|rag\.(ingest|build_v3)|evaluation\.(runner|phase_e3_4_run2)' >&2 || true
  exit 2
fi

mkdir -p "$SNAPSHOT_ROOT"
if [[ "$NEED_V2" == "1" ]]; then
  ditto "$PROJECT_ROOT/rag/data/chroma_db_v2" "$SNAPSHOT_ROOT/chroma_db_v2"
  .venv/bin/python -m evaluation.chroma_snapshot \
    --create-initial-manifest \
    --canonical "$PROJECT_ROOT/rag/data/chroma_db_v2" \
    --copy "$SNAPSHOT_ROOT/chroma_db_v2" \
    --copy-root "$SNAPSHOT_ROOT" \
    --version v2 --collection critique_code_context_v2 --expected-count 6100 \
    --quiescence-evidence "phase-e3-4-run2-copy-script-writer-guard-passed"
fi
if [[ "$NEED_V3" == "1" ]]; then
  ditto "$PROJECT_ROOT/rag/data/chroma_db_v3" "$SNAPSHOT_ROOT/chroma_db_v3"
  .venv/bin/python -m evaluation.chroma_snapshot \
    --create-initial-manifest \
    --canonical "$PROJECT_ROOT/rag/data/chroma_db_v3" \
    --copy "$SNAPSHOT_ROOT/chroma_db_v3" \
    --copy-root "$SNAPSHOT_ROOT" \
    --version v3 --collection critique_code_context_v3 --expected-count 19418 \
    --quiescence-evidence "phase-e3-4-run2-copy-script-writer-guard-passed"
fi

# Reference snapshots and manifests become read-only before disposable working copies are made.
chmod -R a-w "$SNAPSHOT_ROOT"

HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -m evaluation.phase_e3_4_run2 \
  prepare-working --snapshot-root "$SNAPSHOT_ROOT" --working-root "$WORKING_ROOT" \
  --run-dir "$RUN_DIR" --arms "$SELECTED_ARMS"

cat <<EOF
Selected Run 2 arms: $SELECTED_ARMS
Verified immutable references: $SNAPSHOT_ROOT
Disposable working copies: $WORKING_ROOT
Run 2 artifact namespace: $RUN_DIR
No Groq request was made. Canonical ChromaDB directories were only read during copy/verification.
EOF

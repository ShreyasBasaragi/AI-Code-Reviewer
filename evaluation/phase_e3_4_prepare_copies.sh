#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_ROOT="${1:-${TMPDIR:-/tmp}/phase_e3_4_chroma_copies}"
DEST_ROOT="$(mkdir -p "$DEST_ROOT" && cd "$DEST_ROOT" && pwd)"

if [[ -e "$DEST_ROOT/chroma_db_v2" || -e "$DEST_ROOT/chroma_db_v3" ]]; then
  echo "Refusing to overwrite an existing temporary database copy under $DEST_ROOT" >&2
  exit 2
fi

# Stop the application, MCP/API servers, ingestion, and other Chroma writers before running.
if pgrep -fl 'uvicorn|mcp_server\.(http_api|server)|rag\.(ingest|build_v3)|evaluation\.runner' >/dev/null; then
  echo "A likely Chroma writer or evaluation process is running. Stop it before copying." >&2
  pgrep -fl 'uvicorn|mcp_server\.(http_api|server)|rag\.(ingest|build_v3)|evaluation\.runner' >&2 || true
  exit 2
fi

ditto "$PROJECT_ROOT/rag/data/chroma_db_v2" "$DEST_ROOT/chroma_db_v2"
ditto "$PROJECT_ROOT/rag/data/chroma_db_v3" "$DEST_ROOT/chroma_db_v3"

cd "$PROJECT_ROOT"
for version in v2 v3; do
  if [[ "$version" == "v2" ]]; then collection="critique_code_context_v2"; else collection="critique_code_context_v3"; fi
  .venv/bin/python -m evaluation.chroma_snapshot \
    --create-initial-manifest \
    --canonical "$PROJECT_ROOT/rag/data/chroma_db_${version}" \
    --copy "$DEST_ROOT/chroma_db_${version}" \
    --copy-root "$DEST_ROOT" \
    --version "$version" \
    --collection "$collection" \
    --quiescence-evidence "copy-script-pgrep-writer-guard-passed"
done

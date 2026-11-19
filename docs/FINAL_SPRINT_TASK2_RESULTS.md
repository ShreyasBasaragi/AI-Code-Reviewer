# CodeMentor Final Sprint — Task 2 Results

## Summary

Task 2's confirmed CI defect is fixed, and a RAG error-transparency defect found during validation is fixed. Offline Python and frontend verification passed. The live Groq smoke test was not run because no `GROQ_API_KEY` was configured and source-sharing consent was false. Qwen inference was not run.

## Files changed

- `tests/test_phase_e3_4_run2.py` — made the preflight test hermetic with a synthetic presence-only key and added a missing-credential block regression test.
- `rag/retrieve.py` — added strict error signaling for v2 retrieval diagnostics while preserving the legacy empty-list behavior of the compatibility API.
- `mcp_server/orchestrator.py` — uses retrieval diagnostics and records an actionable `retrieval_failure` while allowing review to continue with empty context.
- `tests/test_phase_b_evidence_and_schema.py` — verifies unavailable v2 data is reported without creating a database, and compatibility callers still receive an empty list.
- `rag/README.md` — documents the diagnostics and compatibility behavior.

The existing untracked `docs/FINAL_SPRINT_BASELINE.md` was left untouched. No evaluation manifest, result, checkpoint, dataset output, retrieval configuration, or v3 database was intentionally changed. No commit or push was made.

## Confirmed defects and fixes

### FS-001

`run_preflight` correctly blocks when `GROQ_API_KEY` is absent. Its ready-path unit test had omitted that presence-only prerequisite. The test now sets a synthetic value with `monkeypatch`; it does not instantiate a provider or make a request. A second test calls the real preflight with the key absent and confirms `status="blocked"`, `execution_permitted=false`, and zero network requests. The production gate was not changed.

### Missing v2 retrieval diagnostics

The compatibility v2 retriever returned an empty list when the corpus was missing, empty, or failed to query. That hid the retrieval failure from the structured review pipeline. The new strict path reports `status="errored"` and a safe error type; the orchestrator records `retrieval_failure` and continues with no retrieved context. Existing `retrieve_evidence` and `retrieve_context` callers retain their previous empty-list behavior. v3 selection remains explicit and does not silently fall back to v2.

## Verification

- Focused FS-001 tests, with `GROQ_API_KEY` unset: **2 passed**.
- Focused RAG regression tests: **3 passed**.
- Full offline Python suite, `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ./.venv/bin/python -m pytest -q tests dataset_pipeline/tests rag/tests`: **436 passed**, with one Chroma telemetry deprecation warning.
- Frontend `npm test -- --run`: **14 passed**.
- Frontend `npm run build`: **passed** (TypeScript and Vite).
- Python compilation, shell syntax check, and `git diff --check`: **passed**.

No hosted model or GitHub API calls were made by these checks.

## Groq/provider validation

The configured review and fix provider settings select Groq independently. The client is lazy-created, the configured Groq model is `openai/gpt-oss-20b`, and its structured request uses strict JSON schema validation; fix requests bind the response to the requested finding ID. Existing offline tests cover provider routing, schema/error behavior, and sanitized failures. This is code-and-mock validation only, not live connectivity validation.

Live smoke status: **not run; 0 requests**. The environment had no configured Groq credential and source-sharing consent was false. No key value was printed or changed. No Qwen weights were loaded and no Qwen inference/model code was changed.

## RAG and review observations

The existing v2 and v3 corpora were copied to a temporary directory and queried through those copies. Snapshot verification reported 6,100 v2 records and matched the v2 and v3 directory digests before those validation queries. Retrieval returned structured evidence with IDs from both corpora. Results on the small synthetic query set were mixed: v3 returned SQL-related evidence for parameterized-SQL examples, while the dynamic-`eval` query mostly returned unrelated style guidance; v2 results were also mixed. This is a small relevance spot-check, not a benchmark or a claim of overall retrieval quality.

The fake-provider review pipeline successfully combined v3 retrieved evidence with the existing deterministic static finding (`PYSEC001`) for `eval`, and the response validated without a live model request. Seven representative static-analysis samples returned no static findings for hardcoded credentials, unsafe SQL construction, division by zero, unsafe file handling, mutable defaults, parameterized SQL, and context-managed file handling; the explicit `eval` sample produced `PYSEC001` at line 2. These outcomes describe the current deterministic rules only and do not establish LLM detection quality.

### Canonical v2 database observation

The full offline suite included retrieval tests that call the default v2 path. A post-suite read-only comparison found that `rag/data/chroma_db_v2/chroma.sqlite3` bytes differed from the pre-suite temporary copy. The comparison found the same collection record count (6,100), collection-content digest, vector-index digest, and no difference in journal-file digest; it classified the change as `sqlite_operational_or_metadata_bytes_changed`. The v3 directory digest remained equal to its pinned value `111ade6cc0b50babdc0e93fb0d00e7c03b7a04050efe96fecb0a25a19c7c1ab4`.

This appears to be an operational SQLite-byte change caused during retrieval tests, not a collection-content or vector-index change. No restore was attempted, to avoid overwriting canonical data or possible user changes. Future tests should direct default-v2 retrieval to an isolated copy/fixture before opening the canonical path.

## Remaining limitations and handoff

- Actual Groq connectivity, live latency, and end-to-end quality remain unverified until credentials, explicit source-sharing consent, and quota are available.
- The small retrieval spot-check found relevance misses; broader evaluation is still needed and must not reuse or alter closed E3.4 experiments without authorization.
- Canonical v2 SQLite operational bytes changed during the offline suite, as detailed above. Semantic collection and vector-index digests match the pre-test copy; the canonical database was not restored.
- Qwen inference and hardware validation remain teammate-owned and were not performed.
- No live GitHub comments, autonomous commits, or Task 3 work were performed.

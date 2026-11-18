# Phase E1 baseline evaluation

`benchmark_v1.jsonl` contains 30 original synthetic Python snippets (10 security defects, 10 logic/runtime defects, 10 clean examples). It is separate from RAG and production data. Labels and rationales are preliminary and require independent human review before publication-quality claims. The optional reference corrections are guidance only; the runner does not treat them as behaviorally verified.

The runner calls `mcp_server.orchestrator.run_review_pipeline` with an injected provider and retriever. Offline `scripted` mode emits empty findings to verify transport, schema handling, and metric plumbing; its scores are **not model quality measurements**. Metrics are finding-level, one-to-one exact issue-type matches, with a two-line tolerance when both expected and predicted locations exist. Missing locations do not disqualify an exact type match. Undefined denominators are reported as JSON `null`.

## Offline run

```sh
python -m evaluation.runner --mode scripted --max-cases 30 --output evaluation/results/offline.json
```

## Bounded live Groq run

Live calls require both an explicit `--enable-live-groq` flag and an already configured `GROQ_API_KEY`. The runner never falls back to Qwen. `GROQ_MODEL` selects the model; otherwise the existing provider default is used. The provider currently uses temperature `0.1`, has no application-level max-token setting, and relies on the SDK's timeout behavior. A run of `N` cases performs at most `N` review requests; `--include-fixes` adds at most one fix request for each case with a validated actionable finding. For a small representative-category check:

```sh
python -m evaluation.runner --mode groq --enable-live-groq --max-cases 3 --case-ids SEC-01,LOG-01,CLEAN-01 --output evaluation/results/groq-no-rag.json
```

The selected case IDs are recorded in the output. This three-case run uses at most three review requests, plus up to three fix requests if `--include-fixes` is supplied.

## RAG ablation

The runner only accepts an **existing non-canonical temporary Chroma directory** through `--rag-copy-dir`. It never creates the copy itself and rejects the selected canonical path. For v2, the copy must contain the v2 collection; for v3 it must contain `critique_code_context_v3`. Create a complete copy only while all Chroma writers are stopped, confirm the copy contains the collection and all associated Chroma files, and keep it outside the repository. The runner sets Hugging Face/Transformers offline mode; if the embedding model is not cached, retrieval initialization fails instead of downloading it.

```sh
python -m evaluation.runner --mode groq --enable-live-groq --max-cases 3 --case-ids SEC-01,LOG-01,CLEAN-01 --rag-copy-dir /path/to/temporary/chroma_db_v2 --output evaluation/results/groq-rag.json
```

Compare runs using the same `GROQ_MODEL`, generation settings, case IDs, and source benchmark. Copying a live Chroma directory while it is in use may not be a consistent snapshot; stop if writers cannot be quiesced and a consistent temporary copy cannot be established. The runner doesn't make a safety claim about a copy it did not create.

Results include per-case schema validity, evidence-reference validity, findings, errors, abstention, retrieval record counts, latency, and optional fix/syntax outcomes. Syntax validity is not behavioral correctness. Keep result files private if prompts or findings might contain sensitive source material.

Groq review calls use strict JSON Schema output for the explicitly supported `openai/gpt-oss-20b` model. Unsupported configured models fail explicitly; they do not fall back to plain JSON. Schema version and structured-output mode are stored in evaluation metadata. See [structured_output.md](structured_output.md) for the contract and semantic-validation boundary.

## Plan I offline validation tools

The labeled regression fixtures in `security_regressions_v1.jsonl` are small,
synthetic, manually authored cases for the existing PYSEC001–PYSEC005 rules.
Run the deterministic AST analyzer against them with a new output path:

```sh
python -m evaluation.security_corpus --output /tmp/static-security-report.json
```

The report includes per-case labels, predictions, rule coverage, and explicitly
unsupported alias/boundary patterns. Its measurements are static-rule fixture
results, not real-world detection or model-quality estimates.

`quality_cases_v1.jsonl` is the manually auditable structured-review dataset.
Stored prediction evaluation cannot initialize a provider and requires a
predictions JSONL file supplied by the caller:

```sh
python -m evaluation.quality --mode offline-fixture \
  --predictions-jsonl /path/to/stored-predictions.jsonl
```

The harness uses deterministic one-to-one matching by rule ID or normalized
category and source location (two-line tolerance when both lines exist), then
scores severity separately. Missing stored predictions are marked skipped and
provider/fixture failures do not become false negatives. Reports are written to
a timestamped, non-overwriting directory with JSON, CSV, and Markdown files.

Live evaluation requires the separate explicit `--allow-live-model` flag. It
uses the configured provider via the shared orchestrator; set local Qwen
explicitly on the CUDA machine if desired. This mode has not been run as part
of offline development verification:

```sh
python -m evaluation.quality --mode live-model --allow-live-model
```

The offline mocked-provider performance benchmark measures request validation,
retrieval, mocked provider call, response validation, static analysis, fix
verification, and end-to-end latency. It writes a unique JSON/CSV/Markdown
bundle and does not use RAG storage or a live model:

```sh
python -m evaluation.performance --warmups 2 --runs 10
```

Mocked-provider times do not represent actual inference latency. The existing
Phase E benchmark, scoring policy, results, and checkpoints remain separate.

### Corpus and strategy flags

Use `--rag-version v2 --retrieval-strategy v2_baseline`, `--rag-version v3 --retrieval-strategy v3_baseline`, or `--rag-version v3 --retrieval-strategy v3_optimized`. Defaults preserve the v2 baseline. Evaluation identity includes both values, so a checkpoint cannot resume across strategies. v3 ablation switches are independent: `--disable-v3-parent-aware`, `--disable-v3-language-aware`, `--disable-v3-source-diversity`, and `--disable-v3-applicability-diagnostics`. `--disable-v3-context-budgeting` is an experimental ablation only and must not be used to serve reviews.

The offline retrieval query set and output diagnostics are documented in [RETRIEVAL_EVALUATION.md](RETRIEVAL_EVALUATION.md). Queries are provisional and have no verified evidence relevance labels. Consequently, precision/recall/MRR/nDCG are emitted as unavailable, never as zero. Machine-readable results contain IDs and bounded diagnostics but no retrieved document text.

## Pacing, checkpoints, and resume

Live provider calls are paced by 12 seconds by default. The setting applies between review and fix requests; scripted/offline mode never sleeps. The Groq SDK's built-in retry policy remains enabled. The runner adds no retry loop of its own.

The CLI writes an atomic checkpoint beside the result JSON unless `--checkpoint-path` is supplied. The checkpoint stores one latest result per case, attempt count, sanitized provider errors, last request time, cumulative call counts, and an evaluation identity containing model, strict-output metadata, RAG mode/snapshot digest, benchmark-content digest, schema version, selected case IDs, and other run settings. Existing checkpoints are not overwritten unless `--resume` is given. Resume verifies the identity, skips completed cases, and retries failed/interrupted cases. Use repeated `--retry-case CASE_ID` options with `--resume` to retry only selected failed cases; completed cases cannot be selected for retry.

Safe 30-case no-RAG run:

```sh
.venv/bin/python -m evaluation.runner --mode groq --enable-live-groq --max-cases 30 --request-interval-seconds 12 --output evaluation/results/phase_e2_30_no_rag.json --checkpoint-path evaluation/results/phase_e2_30_no_rag.checkpoint.json
```

Resume after interruption with the same run settings:

```sh
.venv/bin/python -m evaluation.runner --mode groq --enable-live-groq --max-cases 30 --request-interval-seconds 12 --output evaluation/results/phase_e2_30_no_rag.json --checkpoint-path evaluation/results/phase_e2_30_no_rag.checkpoint.json --resume
```

Retry only a failed case such as SEC-09:

```sh
.venv/bin/python -m evaluation.runner --mode groq --enable-live-groq --max-cases 30 --request-interval-seconds 12 --output evaluation/results/phase_e2_30_no_rag.json --checkpoint-path evaluation/results/phase_e2_30_no_rag.checkpoint.json --resume --retry-case SEC-09
```

Provider HTTP status/error details and SDK retry exhaustion are recorded separately from schema-validation errors. Credentials and raw model responses are not stored in checkpoints.

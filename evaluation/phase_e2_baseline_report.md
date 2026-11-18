# Phase E2 — Live baseline report

## Execution summary

The execution environment reported `GROQ_API_KEY` present. The key value was never printed or added to reports. The runner used Groq model `openai/gpt-oss-20b`, temperature `0.1`, with RAG disabled for the first baseline. The benchmark was unchanged: 30 unique cases, 10 each in security, logic/runtime, and clean categories; SHA-256 `d5b5bd81d36971d135e86f482b20bc1f38a711f608d8be4748b2a829abfc2fb5`.

The runner issued 30 review calls. Every call failed with `APIConnectionError`; none returned a model response. The SDK logged retry attempts, but the client report cannot establish how many requests reached Groq or were received by its service. No fix requests were made.

## No-RAG measurements

These results describe request/schema failure behavior, not reviewer quality. Detection metrics are unavailable because there were no valid model responses.

| Measurement | Result |
|---|---:|
| Runner-level review calls | 30 |
| Successful Groq responses | 0 |
| Provider error cases | 30 / 30 (100%) |
| Valid JSON/schema | 0 / 30 (0%) |
| Precision | unavailable |
| Recall | unavailable |
| Clean-case false-positive rate | unavailable |
| Evidence-reference validity | unavailable (no valid responses) |
| Mean wall time per failed review call | 1.337 s |
| Median wall time per failed review call | 1.336 s |
| Maximum wall time per failed review call | 1.474 s |
| Explicit abstentions | 0 (requests failed; this is not a model abstention result) |
| Fix generation / syntax / diff | unavailable; no actionable valid findings |

The recorded case errors were `model_failure: APIConnectionError` and the resulting invalid-JSON marker from the orchestrator fallback. The per-case results are in [phase_e2_no_rag.json](/Users/shreyasbasaragi/AI%20Code%20Reviewer/evaluation/results/phase_e2_no_rag.json).

## RAG baseline and fixes

The RAG run was not attempted. Process-list access is denied in this execution environment, so I could not establish that Chroma writers were stopped before making a consistent database copy. The canonical Chroma store was not opened through Chroma, queried, copied, or modified. RAG metrics are unavailable.

The limited fix run was not attempted because no valid actionable findings were returned. No generated code was executed or applied.

## Benchmark label notes

Benchmark integrity remained unchanged. Labels are preliminary and have not been independently reviewed. Assumption-sensitive cases include SEC-06 (the returned expression is risky only if evaluated downstream), SEC-10 (assumes object-level access control is required), LOG-04 (age threshold policy), LOG-06 (whether a display separator is intended), and LOG-10 (fractional versus whole-minute conversion). These remain as authored; no metrics should be interpreted as definitive until labels and assumptions receive independent review.

## Reproduction commands

Run in the configured local terminal with the key already exported to its environment. The commands do not print the key. Use the same model for both configurations:

```sh
cd "/Users/shreyasbasaragi/AI Code Reviewer"
export GROQ_MODEL=openai/gpt-oss-20b
.venv/bin/python -m evaluation.runner --mode groq --enable-live-groq --max-cases 30 --output evaluation/results/phase_e2_no_rag.json
```

For RAG, first stop Chroma writers, then copy the complete database outside the repository. Run only against that copy:

```sh
tmp_chroma="$(mktemp -d /tmp/phase-e2-chroma.XXXXXX)"
cp -a rag/data/chroma_db_v2 "$tmp_chroma/chroma_db_v2"
.venv/bin/python -m evaluation.runner --mode groq --enable-live-groq --max-cases 30 --rag-copy-dir "$tmp_chroma/chroma_db_v2" --output evaluation/results/phase_e2_rag.json
```

For up to five selected cases, the current runner repeats their review calls before generating fixes, so that follow-up costs up to five additional review calls plus five fix calls:

```sh
FIX_CASE_IDS=SEC-01,LOG-01,CLEAN-01
.venv/bin/python -m evaluation.runner --mode groq --enable-live-groq --max-cases 3 --case-ids "$FIX_CASE_IDS" --include-fixes --output evaluation/results/phase_e2_fix_sample.json
```

The fix report includes syntax status, assumptions, diff, and changed-line counts; target alignment remains for human review.

## Regression checks

- Backend focused suite: 84 passed; one third-party Chroma telemetry deprecation warning.
- Frontend tests: 8 passed.
- Frontend production build: passed.
- `git diff --check`: passed.

The evaluation metrics were corrected so failed/invalid cases count toward error rates but do not become false negatives or clean true negatives. No prompts, production review behavior, benchmark labels, or Chroma data were modified. No RAG-vs-no-RAG quality comparison is possible from this run.

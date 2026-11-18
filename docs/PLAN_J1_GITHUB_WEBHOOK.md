# Plan J1 — Secure GitHub Webhook Intake

## Scope

J1 added authenticated event intake, bounded asynchronous review processing, immutable revision checks, and read-only repository guidance retrieval. J2 extends the same worker with administrator-configured inline comments and durable action records. The manual `POST /api/github/pr-reviews` route remains available and read-only.

J2 may publish inline comments only when repository policy and a separate action token explicitly authorize it. It still does **not** create branches, commits, approvals, merges, or other repository changes. Commit execution is hard-disabled. The configured review provider still controls accepted work: an accepted webhook can call the configured provider (Groq by default, or the explicitly selected local provider). Offline tests inject fake providers and clients; no test makes a live call.

## Event flow

1. GitHub sends `POST /api/github/webhook` with `X-GitHub-Event`, `X-GitHub-Delivery`, and `X-Hub-Signature-256`.
2. The API requires `GITHUB_WEBHOOK_SECRET`, reads a size-bounded raw request body, and verifies the exact bytes with HMAC-SHA256 and constant-time comparison before JSON decoding.
3. The handler accepts only `pull_request` actions `opened`, `synchronize`, `reopened`, and `ready_for_review`. It validates repository identity, PR number, and base/head commit SHAs. It constructs the canonical GitHub URL from validated owner/repository identifiers; it never follows a payload URL.
4. A SQLite transaction reserves the delivery ID. Identical redeliveries return the existing status and do not schedule a second review. Reuse of an ID with a different body returns `409`.
5. A single bounded background worker claims pending rows and invokes the existing read-only PR service. No per-request background task or unbounded in-memory work queue is created. The existing PR service validates the fetched repository/number and event SHAs before review, and rechecks the base/head SHAs after review. A changed or uncheckable revision is marked stale.
6. The PR service fetches changed Python files at the head revision and baseline source at the base revision, then calls the same shared review pipeline used by manual PR reviews.
7. J2 evaluates each validated finding against administrator policy and persists the decision. If inline publication is enabled, the dedicated action token is configured, evidence is validated, the location maps to one added line, and the PR head is still current, the worker publishes a targeted comment. Unmappable findings are recorded as skipped; J2 does not post a summary fallback.
8. Candidate preparation returns a bounded, verified proposal only. It never applies or commits a patch.

### Draft behavior

Draft events are acknowledged and recorded with status `skipped` and code `draft_pr_waiting_for_ready_for_review`; no provider or GitHub review request is made. GitHub should send `pull_request.ready_for_review` with `draft: false` when review is appropriate. Draft `opened`, `synchronize`, or `reopened` deliveries stay skipped; the ready event has its own delivery ID and is processed once.

## Configuration

Required for event intake:

- `GITHUB_WEBHOOK_SECRET`: the secret configured in the GitHub webhook. It is never returned or logged. Missing secret fails closed with `503`.
- `GITHUB_WEBHOOK_STATUS_TOKEN`: separate high-entropy bearer token required by the status/result endpoint. Without it, status retrieval fails closed. Do not reuse or expose the webhook secret.

Optional bounded settings:

| Setting | Default | Hard range / behavior |
|---|---:|---|
| `GITHUB_WEBHOOK_MAX_BODY_BYTES` | 1 MiB | clamped to 1 KiB–4 MiB |
| `GITHUB_WEBHOOK_MAX_IN_FLIGHT` | 32 | clamped to 1–256 pending/running deliveries |
| `GITHUB_WEBHOOK_RETENTION_DAYS` | 30 days | clamped to 1–365 days |
| `AI_CODE_REVIEWER_WEBHOOK_DB_PATH` | `~/.local/state/ai-code-reviewer/github_webhook_deliveries.sqlite3` | SQLite ledger location; ensure the service account can write it and protect it as private application state |

The ledger stores delivery/repository/PR/revision identifiers, status, sanitized failure codes, and the structured review result for bounded retention. Evidence text is omitted from the persisted status result; evidence IDs and metadata remain. The ledger is capped at 1,000 rows by default, pruning older terminal records. Pending/running jobs are not pruned to make room. The review/fix handles remain in-memory and process-local as documented in the API guide.

## Event status and result retrieval

The webhook responds `202` with delivery ID and current status after reservation; an identical redelivery responds `202` with `duplicate: true`. Typical statuses are `pending`, `running`, `completed`, `partial`, `failed`, `stale`, and `skipped`.

`GET /api/github/webhook/deliveries/{delivery_id}` requires:

```http
Authorization: Bearer <GITHUB_WEBHOOK_STATUS_TOKEN>
```

The endpoint returns repository/PR/revision metadata, status, sanitized errors, processing duration, and the stored structured result. It does not return source files, raw event bodies, provider credentials, webhook secrets, or raw model responses. Status/result data is private; protect the bearer token and the SQLite file.

## Idempotency, bounded processing, and restart behavior

- A unique SQLite primary key and `BEGIN IMMEDIATE` transaction serialize concurrent reservations. Only one request reserves a delivery ID.
- Capacity is checked transactionally before insertion. When pending/running capacity is exhausted, intake returns `503` and does not accept new work.
- Pending work is resumed at worker startup. A row left `running` after restart is changed to `failed` with `worker_interrupted_unknown_completion`; it is not automatically replayed because the provider may already have completed. This avoids duplicate review execution at the cost of requiring a new GitHub event/delivery for another attempt.
- Identical delivery IDs are retained for the configured audit period (30 days by default); terminal history is bounded to 1,000 rows. Redeliveries inside that window never re-run a completed, failed, stale, or skipped delivery. The ledger does not claim idempotency after a record has expired/pruned.
- Processing errors store a fixed GitHub error code or an exception type, not exception text, source code, secrets, or raw event content.

Run one API worker for the current application: review/fix lifecycle handles are still process-local, even though webhook delivery reservation is SQLite-backed.

## Repository context retrieval

For each PR, the read-only GitHub client inspects a non-recursive Git tree at the pinned head SHA. Only regular-file modes are eligible; symlinks, traversal paths, binary/non-text extensions, and unapproved paths are omitted. The collector reads root-level `README.md`, `CONTRIBUTING.md`, `AGENTS.md`, style/architecture guidance and a short explicit list of Python configuration files. Under `docs/`, it accepts Markdown, reStructuredText, text, or AsciiDoc files to a bounded depth. It never downloads the repository recursively and never uses a payload-provided `download_url`.

Defaults are at most 100 entries per tree, 2 directory levels below `docs/`, 12 files, 48 KiB per file, 192 KiB total, and 4 selected context files per reviewed source file. Values can be bounded through `GITHUB_REPO_CONTEXT_MAX_ENTRIES`, `GITHUB_REPO_CONTEXT_MAX_DEPTH`, `GITHUB_REPO_CONTEXT_MAX_FILES`, `GITHUB_REPO_CONTEXT_MAX_FILE_BYTES`, `GITHUB_REPO_CONTEXT_MAX_TOTAL_BYTES`, and `GITHUB_REPO_CONTEXT_MAX_SELECTED_FILES`.

Candidate files are selected per source file by deterministic lexical overlap and guidance-file priority, with a small root-guidance fallback for short files. A fetched file carries path, exact commit revision, truncation flag, and a content digest. It becomes a `RetrievedEvidence` item with a stable repository-scoped `repo-…` evidence ID and `source_family=repository_context`. The normal orchestrator combines it with canonical RAG evidence, checks IDs for collisions, and sends both through the existing strict review schema. Retrieved text remains untrusted data; it cannot authorize tool calls or GitHub writes.

No Chroma collection or canonical RAG artifact is created or changed. If GitHub cannot provide repository context, review can continue with canonical evidence and reports context availability/warnings. Selected repository evidence appears in each file's structured evidence list; collection limits, revision, omitted/truncated conditions, and file paths appear in the `repository_context` diagnostics.

## Revision, partial results, and failures

- Initial webhook repository, PR number, base SHA, and head SHA must match GitHub's fetched metadata before any file review.
- PR source reads use the head repository from validated GitHub metadata (including a fork); baseline reads use the base repository. All requests use the fixed `api.github.com` host through the existing bounded GET client.
- The PR's base/head SHAs are fetched again after review. A changed SHA or failed recheck marks the result `stale`; stale findings are explicitly tied to the old `processed_head_sha` and are not labeled latest.
- Existing per-file limits, diff mapping, source failures, missing base files, pagination truncation, and GitHub rate limits stay visible as partial/failed results. No retry silently changes the pinned revision.
- Unsupported event/action, invalid signature, malformed payload, and oversized body are rejected before provider or GitHub review operations. Missing credentials do not cause a provider fallback.

## Manual versus automatic review

`POST /api/github/pr-reviews` remains a manual URL-triggered read-only review. `POST /api/github/webhook` adds authenticated automatic review analysis and the optional policy-gated inline-comment action. Both use `GitHubPRReviewService` and the shared orchestrator. J2 does not modify source branches or execute commits. Existing explicit candidate generation, verification, human approval, rejection, and export workflows remain available.

## Offline tests

With existing project dependencies installed:

```sh
./.venv/bin/python -m pytest -q tests/test_github_webhook.py tests/test_repository_context.py tests/test_github_pr.py tests/test_phase_d1_http_api.py tests/test_plan_i_end_to_end.py tests/test_static_analysis.py
```

Tests use temporary SQLite databases, fake GitHub clients, and injected providers. They do not contact GitHub, Groq, or Qwen and do not load model weights.

## Deferred to Plan J2 or later

- Posting PR review comments or inline comments.
- Branch creation, patch application, commit creation, approvals, merges, or any other GitHub write.
- Risk-based autonomous action, confidence thresholds, line limits, and repository opt-in.
- Running candidate code, behavioral tests, project CI, or a sandbox.
- Multi-process shared review/fix handle state, distributed queues, and production deployment monitoring.

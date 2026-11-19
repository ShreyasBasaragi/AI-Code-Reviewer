# FastAPI service

## Autonomous fix status (2026-11-19)

The current implementation can stage a candidate revision, verify it, and
advance the pull request branch through a narrow non-force GitHub ref update.
This path is disabled by default and fails closed unless repository policy,
separate staging and final-write credentials, signed confidence evidence,
an attested remote behavioral verifier, candidate-specific CI, source/diff
checks, and all runtime gates pass. Configure the independent write switches
`GITHUB_CANDIDATE_STAGING_ENABLED` and `GITHUB_AUTONOMOUS_COMMIT_ENABLED` only
after reviewing the deployment. Tokens can be stored separately as
`github_candidate_staging_token` and `github_autonomous_commit_token`; the
existing action policy must explicitly opt the repository in. The verifier,
calibration artifact, workflow identity, and required CI checks are configured
through `AI_CODE_REVIEWER_SANDBOX_URL` plus
`AI_CODE_REVIEWER_SANDBOX_HMAC_KEY`,
`AI_CODE_REVIEWER_RUNTIME_CALIBRATION_PATH` plus
`AI_CODE_REVIEWER_CALIBRATION_DATASET_ID`, the numeric
`GITHUB_CANDIDATE_WORKFLOW_ID`, and repository `required_github_checks`
respectively. The staging token must also be permitted to dispatch the
configured Actions workflow. The test bundle is selected
by `AI_CODE_REVIEWER_BEHAVIORAL_TEST_BUNDLE`.

After staging a candidate, the adapter dispatches the configured workflow on
that candidate branch and polls exact-SHA check/status evidence for at most 60
seconds by default (bounded to 180 seconds). Set
`GITHUB_CANDIDATE_CI_WAIT_SECONDS` to tune that bounded wait; pending or missing
checks at the deadline block finalization.

`GET /api/verification/capabilities` reports configuration presence without
performing external checks; it does not prove a credential can write, a remote
broker is trustworthy, a calibration artifact passes per-finding validation,
or candidate CI is green. Live GitHub writes, live model requests, and
behavioral execution were not performed for this implementation. The Task
J2/J3 sections below document the earlier implementation state and are
superseded where they say commit execution is permanently disabled.

Start the HTTP API from the repository root:

```sh
uvicorn mcp_server.http_api:app --host 127.0.0.1 --port 8000
```

The hosted `groq` provider is the default. Set `AI_CODE_REVIEWER_PROVIDER=groq`
or explicitly select `qwen_local` for the CUDA-only Transformers + PEFT adapter.
The legacy value `qwen` remains an alias for `qwen_local`. Provider construction
is lazy: importing the app and calling `/api/health` does not load model weights
or instantiate a hosted client. Groq requests require `GROQ_API_KEY`. Provider
failures are returned explicitly; the API never falls back to another provider.

Fix generation uses its own setting, `AI_CODE_REVIEWER_FIX_PROVIDER`, defaulting
to `groq`. This keeps the critique adapter out of fix generation unless it is
explicitly selected. An injected provider supplied by an embedding application
continues to serve both workflows.

For local Qwen inference, configure `QWEN_BASE_MODEL_PATH` (a local directory)
or `QWEN_BASE_MODEL_ID` (a previously provisioned Hugging Face ID),
`QWEN_ADAPTER_PATH`, `QWEN_DEVICE` (default `cuda`), and
`QWEN_PRECISION` (`float16` default or `bfloat16`). Local loading is always
offline-only; there is no automatic model download, CPU/MPS fallback, or
quantization. The default gate requires 8 GiB free CUDA memory before loading.
See [CUDA deployment and validation](../docs/PLAN_H_CUDA_DEPLOYMENT.md).

Set `AI_CODE_REVIEWER_CORS_ORIGINS` to a comma-separated list of exact browser
origins. The development defaults include ports 3000 and 5173 on `localhost`
and `127.0.0.1`; wildcard origins are ignored. Same-origin production
deployments should set this to their exact HTTPS frontend origin.

The MCP and HTTP interfaces use the same bounded, in-memory lifecycle state when
they run in the same process. Handles are limited to the most recent 32 reviews
and 32 candidates. A server restart clears them. Multiple worker processes do
not share state, so run one worker for this phase.

GitHub webhook deliveries use a separate bounded SQLite ledger for deduplication
and event status. Configure `GITHUB_WEBHOOK_SECRET` before enabling the webhook;
without it, the route fails closed. Configure `GITHUB_WEBHOOK_STATUS_TOKEN` to
read private delivery results. The default SQLite path is
`~/.local/state/ai-code-reviewer/github_webhook_deliveries.sqlite3`; override it
with `AI_CODE_REVIEWER_WEBHOOK_DB_PATH`. Use one API worker because the review
and fix handles above remain process-local.

## Routes

- `POST /api/reviews` — JSON body: `{"code":"...", "language":"python"}`
- `GET /api/reviews/{review_id}`
- `POST /api/reviews/{review_id}/findings/{finding_id}/fixes` — optional body:
  `{"language":"python"}`
- `GET /api/fixes/{candidate_id}`
- `POST /api/fixes/{candidate_id}/verify`
- `POST /api/fixes/{candidate_id}/accept`
- `POST /api/fixes/{candidate_id}/reject`
- `GET /api/fixes/{candidate_id}/export`
- `POST /api/github/pr-reviews` — JSON body: `{"url":"https://github.com/owner/repository/pull/123"}`
- `POST /api/github/webhook` — signed GitHub pull-request event; returns `202` with the delivery ID and status
- `GET /api/github/webhook/deliveries/{delivery_id}` — requires `Authorization: Bearer <GITHUB_WEBHOOK_STATUS_TOKEN>`
- `GET /api/github/webhook/deliveries/{delivery_id}/actions` — authenticated action status only; omits full review result
- `POST /api/github/webhook/deliveries/{delivery_id}/actions/{action_id}/reconcile` — authenticated remote-state read for an ambiguous inline or general comment
- `POST /api/admin/setup` — loopback-only first-run bootstrap and password initialization
- `POST /api/admin/session` — password login (or temporary legacy-token exchange) for a 15-minute HttpOnly, SameSite=Strict browser session; HTTP login is loopback-only and remote clients require HTTPS
- `POST /api/admin/password-reset` — loopback-only single-use recovery-token reset
- `GET /api/admin/session` and `DELETE /api/admin/session` — inspect or revoke the browser session
- `GET /api/admin/provider-settings` and `PUT /api/admin/provider-settings` — require either the short-lived browser cookie or the existing Bearer token; keys are redacted and retained in process memory only
- `GET /api/admin/provider-options` — authenticated server-approved provider/model options; does not contact providers
- `POST /api/admin/provider-connectivity` — authenticated and explicitly user-triggered; Groq calls the account model-list endpoint (no inference), while Qwen readiness inspects artifacts without loading weights
- `GET /api/admin/github/status` — credential configured flags and storage availability only
- `PUT /api/admin/github/credentials` and `DELETE /api/admin/github/credentials/{credential_type}` — authenticated GitHub, comment-token, and webhook-secret updates/removal; values are write-only
- `POST /api/admin/github/test` — explicit authenticated GET to GitHub `/user`
- `GET /api/admin/github/repositories` — explicit authenticated repository discovery
- `GET|PUT|DELETE /api/admin/github/repositories/{owner}/{repository}/policy` — validated canonical policy management with revision checks
- `GET /api/admin/github/webhook` — endpoint, signed-delivery, processing, and reachability status without exposing the secret
- `GET /api/admin/audit` — bounded administrator security and configuration events without secret values

Administrator passwords are Argon2id-hashed in an owner-only SQLite file. The path defaults to `~/Library/Application Support/AI Code Reviewer/admin.sqlite3` on macOS, or the XDG state directory elsewhere; override with `AI_CODE_REVIEWER_AUTH_DB_PATH` or `AI_CODE_REVIEWER_STATE_DIR`. Startup initializes authentication, lifecycle, webhook delivery, and GitHub action SQLite schemas before serving requests; a storage initialization failure stops startup. By default startup prints a one-time bootstrap credential locally when first-run setup is needed and prints the recovery command on every launch. Set `AI_CODE_REVIEWER_PRINT_BOOTSTRAP_CREDENTIAL=false` to withhold that one-time value from logs; deployment Compose uses this mode. Use `python -m mcp_server.admin_cli setup-password` or `change-password` from an interactive local terminal when a trusted loopback web route is unavailable. The existing `reset-password` command still issues a 10-minute one-time web recovery credential. `AI_CODE_REVIEWER_ADMIN_TOKEN` remains a temporary migration/emergency Bearer path; remove it from the backend environment after password setup and login have been verified.

Browser sessions are stored as token hashes in the protected state database and expire after 15 minutes. Cookie-authenticated state changes require an allowed `Origin`; setup and password recovery require the effective request peer and host to be loopback. When reverse proxying, run Uvicorn with proxy headers trusted only from the isolated proxy network, and have the proxy overwrite forwarded client/scheme headers. A sanitized forwarded loopback client is accepted; a remote effective peer is still rejected. Configure exact frontend origins with `AI_CODE_REVIEWER_CORS_ORIGINS`. Legacy Bearer authentication remains available for existing non-browser clients while the migration variable is configured.

GitHub and webhook credentials use macOS Keychain persistence where available. Other platforms can opt into process-memory-only storage; persistent writes fail closed if a secure OS store is unavailable. Environment credentials remain available during migration and cannot be removed through the UI. The canonical action-policy file is the existing `GITHUB_ACTION_POLICY_PATH`, or an owner-only file under the application state directory when unset. Updates use the existing policy parser, cross-process file locking, revision checks, and atomic replacement. Autonomous commit execution is disabled by default and requires independent runtime authorization; editable repository policy alone cannot authorize a write.

Review submission, manual PR review, webhook review, and generated fix responses include `provider_identity` when the active provider exposes it. Connectivity status is separate from successful model inference, and must not be presented as proof that a review or fix generation succeeded.
- `GET /api/verification/capabilities` — reports verification gates and blockers
- `GET /api/health`

`/api/health` reports process/provider selection and durable state readiness;
it does not contact Groq, validate API credentials with the provider, or check
the RAG corpus. SQLite stores are initialized during startup, before health
requests can be served.

The verification capabilities response is additive and reports whether
candidate staging, remote behavioral verification, calibration, CI workflow,
and commit credentials are configured. Configuration presence is not a live
health check. Read-only CI evidence must match an exact candidate SHA; a pull
request's pre-change head is not candidate evidence. Behavioral tests are `Not
Run` unless a separately reviewed isolated execution broker is configured.
Candidate code is never executed in the FastAPI process. If the owner enables
the independent gates and all per-action checks pass, an attested remote broker
may execute the configured test bundle and the adapter may advance the PR head
branch with a non-force GitHub ref update.

`POST /api/reviews` includes an additive `timings` object with request
validation, RAG retrieval, provider generation, response validation, static
analysis, pipeline total, and total request durations in monotonic-clock
seconds. These are diagnostics, not service-level performance guarantees.

The manually invoked GitHub pull request route is read-only. It retrieves PR
metadata and changed files, fetches Python source at base/head commits, and sends
eligible head sources through the existing review pipeline. It never writes to
GitHub. No GitHub credentials are sent to the browser.

The webhook route verifies `X-Hub-Signature-256` against the exact raw body
before JSON parsing, then accepts only `pull_request` actions `opened`,
`synchronize`, `reopened`, and `ready_for_review`. It uses
`X-GitHub-Delivery` for SQLite-backed deduplication. Draft PR deliveries are
recorded as skipped until a non-draft `ready_for_review` event. Work runs on one
bounded background worker and invokes the same read-only PR service. The worker
pins the event's base/head revisions, checks them against GitHub before review,
and marks the outcome stale if the PR changes during analysis or cannot be
rechecked. Pending deliveries resume on restart; ambiguous `running` deliveries
are marked failed and are not replayed, avoiding duplicate provider execution.

Webhook request bodies default to 1 MiB (`GITHUB_WEBHOOK_MAX_BODY_BYTES`, hard
range 1 KiB–4 MiB). In-flight work defaults to 32
(`GITHUB_WEBHOOK_MAX_IN_FLIGHT`, hard range 1–256); delivery history defaults to
30 days (`GITHUB_WEBHOOK_RETENTION_DAYS`, hard range 1–365) and 1,000 rows.
Identical redelivery is idempotent while its ledger record is retained; a
delivery ID reused with a different body is rejected. A separate status token
is required to retrieve private PR findings. Persisted evidence text is omitted;
the result keeps findings, evidence IDs, and provenance metadata.

## J2 policy-gated GitHub actions (historical implementation baseline)

The signed webhook worker may publish validated review comments only after
both repository policy and a dedicated action credential authorize it.
Comments are **off by default**. Configure `GITHUB_ACTION_POLICY_PATH` to an
administrator-managed JSON file using version `github-action-policy-v1`; there
is no HTTP or MCP policy mutation endpoint. Unknown fields, invalid settings,
or a policy that removes required checks fail closed. PR files, README text,
descriptions, webhook fields, and model responses cannot modify policy.

Configure `GITHUB_ACTIONS_TOKEN` separately from the read-only `GITHUB_TOKEN`.
For comment publication, grant only fine-grained **Pull requests: write** access
to the intended repositories. Keep `publish_review_comments` false until the
owner/admin has explicitly approved the configuration and credential. Inline
comments require a current open PR, exact repository and head SHA, a validated
finding, and one changed new-side line. Findings without a safe inline location
use a general pull-request issue comment under the same policy, credential,
finding-validation, and current-head checks. Both comment types carry a durable
operation marker and can be reconciled after an ambiguous write; duplicate
delivery IDs do not publish twice. Comment bodies are bounded and include AI or
static-analysis provenance and evidence IDs, never retrieved evidence text.
Ambiguous write failures are recorded as `unknown` and are not retried
automatically. No commit operation is performed.

The action policy allows only style/naming/formatting as possible low-risk
categories, requires independent confidence >= 0.80 and at most three changed
lines, and hard-protects configured/sensitive paths. Syntax and static checks
must pass; behavioral and CI checks are also mandatory and remain `not_run` in
J2, so the current webhook pipeline cannot authorize an autonomous fix. The
existing review schema does not provide independently calibrated confidence;
model-authored confidence is ignored. Manual fixes still require explicit
acceptance and successful syntax/static verification.

Patch preparation re-fetches the changed file at the event-pinned head revision,
requires exact source equality, checks syntax/static-analysis deltas and the
actual diff size, and returns a proposal only. It never executes or imports the
candidate. Commit preparation verifies repository/branch/head/fork identity but
returns no commit SHA. Commit execution is hard-disabled in J2 regardless of
repository settings; there is no force-push or default-branch write path.

The SQLite delivery database adds bounded `github_action_records` with policy
decision, denial reasons, revisions, verification/publication state, sanitized
errors, GitHub comment identifier, and an empty future commit-SHA field. It
stores no credentials, source file, comment body, or retrieved evidence text.
Unique delivery/finding/action keys suppress duplicate execution. Interrupted
or timed-out writes become `requires_reconciliation` or `unknown` and are not
blindly retried. An operation ID is stored and inline comments carry an opaque
marker so remote state can be reconciled against the expected revision. Only
known failures can enter a bounded backoff schedule; no retry worker is enabled. The private
delivery status endpoint includes these records, and the existing MCP server
provides a read-only action-record inspection tool requiring the status token. No model-callable GitHub
write tool is registered; the existing bounded webhook worker is the only
comment dispatcher.

See [`PLAN_J2_AUTONOMOUS_ACTIONS.md`](../docs/PLAN_J2_AUTONOMOUS_ACTIONS.md) for
the policy format, action details, limits, tests, and J3 dependencies.

## J3 verification and provider administration (historical implementation baseline)

The calibration evaluator requires a signed manifest for the exact labeled
dataset. The current model response schema supplies no confidence score or
per-finding calibration map; runtime confidence is therefore available only
from a separately signed independent artifact, and otherwise remains
unavailable. Behavioral verification delegates only to a separately supplied,
attesting sandbox broker and defaults to `not_run`. Never execute candidate
tests inside FastAPI. Required GitHub check results distinguish
success, failure, pending, skipped, missing, and unknown and must match the
candidate SHA. CI on the original PR head is not candidate verification.

Commit execution is disabled by default. The implementation creates a separate
candidate revision, delegates tests to an attesting remote broker, checks
candidate-specific CI, validates signed calibration evidence, and performs a
non-force branch ref update after a final freshness check. It does not have
live staging evidence or a true GitHub compare-and-swap API; concurrent branch
updates are rejected by the non-force update. Production readiness is not
established by offline tests.
Provider settings require the administrator session described above (or the
temporary `AI_CODE_REVIEWER_ADMIN_TOKEN` migration path); they redact keys.
Keys submitted through the provider-settings API remain in process memory only
because no secure secret vault is configured. Terminate TLS at a trusted deployment
edge. Groq identifiers are allowlisted to the existing strict-output model;
Qwen base/adapter settings are constrained to approved local artifacts and
loading remains CUDA-only and local-files-only. Connectivity remains
`not_checked`; readiness checks do not call providers or load weights. See
[`PLAN_J3_SECURITY_VERIFICATION.md`](../docs/PLAN_J3_SECURITY_VERIFICATION.md).

Groq reviews of submitted code require backend `source_sharing_consent`; hosted
PR reviews additionally require repository policy `allow_source_sharing: true`.
An embedding application's injected provider remains governed by that
application's consent controls.

The PR service collects a small allowlisted set of repository guidance at the
pinned head commit. It checks Git tree modes and omits symlinks, traverses only
the repository root and bounded `docs/` subdirectories, and caps directories,
file count, per-file bytes, total bytes, and selected context. Root guidance,
style/architecture documents, and selected Python configuration are eligible;
Markdown, reStructuredText, text, and AsciiDoc under `docs/` are eligible.
Repository text is untrusted prompt data. It is supplied as structured evidence
alongside canonical RAG results with stable repository/revision-scoped IDs and
source path metadata. It does not create or mutate Chroma collections. Limits
and omitted/truncated conditions appear in `repository_context` diagnostics.

Configure an authenticated webhook only after providing a private
`GITHUB_WEBHOOK_SECRET`. If the GitHub repository is private, the backend still
needs `GITHUB_TOKEN` with read-only Pull requests and Contents access. J1 performs
no GitHub writes; accepted events can invoke the configured LLM provider. Invalid
signatures, unsupported actions, and malformed events do not initialize a
provider or make GitHub review requests. See
[`PLAN_J1_GITHUB_WEBHOOK.md`](../docs/PLAN_J1_GITHUB_WEBHOOK.md) for the full
event lifecycle, limits, offline tests, and J2 boundary.

Public repositories can be read anonymously subject to GitHub's public API rate
limits. Set the backend `GITHUB_TOKEN` environment variable for private
repositories. Use a fine-grained token with **Pull requests: read** and
**Contents: read** access to the target repository; write permissions are not
needed. The API reports only whether a token is configured, never its value.

PR analysis is sequential and limited to one active batch per API process. The
default bounds are 50 changed files retrieved, 5 Python files reviewed, 256 KiB
per source file, 1 MiB aggregate source, 512 KiB aggregate diff, and 20 API pages.
These can be lowered or raised within hard caps using `GITHUB_PR_MAX_FILES`,
`GITHUB_PR_MAX_REVIEW_FILES`, `GITHUB_PR_MAX_SOURCE_BYTES`,
`GITHUB_PR_MAX_AGGREGATE_SOURCE_BYTES`, `GITHUB_PR_MAX_DIFF_BYTES`, and
`GITHUB_PR_MAX_PAGES`. Results report pagination truncation, skipped/failed
files, missing or invalid diff mapping, and partial completion explicitly.
Inline eligibility is informational only; this API does not publish comments.

Export returns JSON containing the corrected source and unified diff. It does
not write or apply a file. Reviews and fixes are Python-only, and submitted code
is limited to 200,000 characters and 512 KiB when UTF-8 encoded.

Review findings include `origin` (`static` or `llm`); deterministic findings
also include a `rule_id` and `severity`. Python AST checks run without executing
submitted code. Review `static_analysis_status` is `failed` when findings are
present, `errored` when the source cannot be parsed, and `passed` when no rules
match. Fix verification reports syntax status, static-analysis status,
pre-existing issues, and issues introduced by the candidate. Acceptance and
export require successful syntax and static checks plus explicit human
acceptance. Behavioral tests are always reported as `not_run`; this workflow
does not execute or apply candidate code. Each syntax/static check reports
`passed`, `failed`, `errored`, or `not_run` as applicable; syntax parse errors
are `failed`, while check/input errors are `errored`.

## Durable review lifecycle and operator recovery

Review and fix lifecycle records are stored in a private, local SQLite database so
an administrator can continue a review after an API restart. Source, candidate,
and review payloads are bounded and expire under the configured retention policy;
approval/rejection audit entries retain the actor, timestamp, source digest, and
verification summary without storing raw provider responses. Administrator
session authentication is required for review/fix lifecycle actions and webhook
operations. This store supports the single local application instance; it is not
a multi-tenant or distributed database.

Webhook operations use bounded automatic retries only for known transient review
failures. An authenticated administrator can inspect recent delivery/action
metadata and request an eligible retry. A delivery is not replayed after any
GitHub action has been recorded. Comment retries first reconcile remote GitHub
state, re-check the current repository policy, and obey a bounded attempt count.
Unknown write outcomes require reconciliation and are never blindly retried.
Credentials, source files, comment bodies, and evidence text are excluded from
the operational listing. Each record includes a server-computed
`retry_available` flag; the retry endpoint revalidates eligibility and current
policy. These controls do not enable GitHub comments or commits. Any comment
publication remains subject to the existing explicit repository policy and
bounded webhook workflow.

The authenticated frontend may recover a review with `GET /api/reviews/{id}` or
a candidate with `GET /api/fixes/{id}` after restart. These endpoints require
the administrator session cookie. They return only the requested known record,
not an unauthenticated listing. Review source remains in the backend lifecycle
store and is not copied into browser storage. Missing and expired records both
return 404 (`review_not_found_or_expired` or `candidate_not_found_or_expired`);
the current contract does not distinguish an explicitly deleted item.

# Plan J3 — Security Verification and Provider Configuration

## Scope and safety conclusion

J3 adds offline verification and administration interfaces on top of J1/J2. It
does **not** enable autonomous commits. No isolated execution broker, candidate
revision CI creation mechanism, signed calibration dataset, or live staging
evidence is installed in this workspace. These are hard blockers; syntax and
static analysis alone do not authorize a commit.

`mcp_server/github_policy.py::commit_execution_decision` evaluates explicit
commit prerequisites but always reports `AUTONOMOUS_COMMIT_DISABLED`. The J2
denial identifier is retained for API compatibility. There is no commit
transport or undocumented override.

## Confidence and calibration

`mcp_server/confidence.py` computes Brier score and expected calibration error
from offline labeled examples. A report is not marked calibrated unless the
exact examples, model ID, dataset ID, sample count, and independent human label
source match an HMAC-signed manifest. The signing key is supplied by the
external evaluation authority and must be at least 32 bytes. Model-authored
scores and synthetic/fixture labels are rejected as independent label sources.

The current review response schema does not emit confidence, no verified signed
label manifest is present, and there is no reliable per-finding calibration
mapping. `independently_supported_confidence` therefore returns unavailable;
webhook policy continues to pass no confidence. No confidence value is
fabricated and the 0.80 threshold cannot be met by current production findings.

## Behavioral verification and isolation

`mcp_server/behavioral_verification.py::BehavioralVerifier` checks the expected
source hash, file scope, and bounded resource parameters. It never executes
source itself. It only delegates to an explicitly supplied external broker and
requires that broker to attest to network isolation, credential exclusion,
filesystem restriction, timeout enforcement, process cleanup, resource limits,
candidate hash, and exact changed-file scope. Output is bounded and common
credential patterns are redacted.

No trusted broker implementation is included or configured on this Mac. The
default result is `not_run` with
`isolated_execution_broker_unavailable`. A subprocess, virtual environment,
test fixture, or the broker protocol alone is not a sandbox. No untrusted test
or candidate code was executed. Behavioral verification remains a blocking
check and cannot authorize commits.

## Candidate-revision GitHub CI

`mcp_server/github_ci.py` maps required check runs and commit statuses to
`success`, `failure`, `pending`, `skipped`, `missing`, or `unknown`. Results are
accepted only when their SHA equals the requested candidate SHA; stale results,
missing checks, malformed responses, and API failures fail closed. The
read-only GitHub client has a fixed repository/commit endpoint. Trusted local
policy may list exact required GitHub check names in `required_github_checks`.

`GitHubMCPActionAdapter.verify_candidate_ci` rejects CI on the original PR head
when it is presented as proof for a changed candidate. J3 does not create a
candidate commit/ref or trigger candidate CI, so the actual patch cannot obtain
candidate-revision evidence through this implementation. No CI absence is
interpreted as success. The commit gate remains disabled.

## Commit authorization

The deterministic gate now requires explicit evidence for repository opt-in,
low-risk category, independently supported confidence, a <=3-line scope,
source hashes, exact candidate CI SHA, syntax, static regression, isolated
behavioral verification, no unexpected file changes, write permission, durable
action reservation, idempotency, current head/repository/branch identity, and
non-fork status. Missing or unknown values deny. Even if all mocked gate values
are set to passed, execution remains disabled pending independent review and
live staging.

The adapter still has no commit endpoint. It cannot force-push, modify the base
branch, or bypass branch protection. Atomic branch compare-and-swap is not
implemented because no source-branch write mechanism is authorized or enabled.

## Action lifecycle, retries, and audit

The bounded SQLite action table now records operation ID, lifecycle state,
attempt count, retry time, verification/publication state, policy evidence,
GitHub comment ID, and sanitized failure code. Interrupted writes transition
to `requires_reconciliation`; they are not retried. Inline comments carry an
opaque operation marker, and the adapter can query the fixed comment-list
endpoint to reconcile a timed-out write against the exact expected revision.
The authenticated `POST /api/github/webhook/deliveries/{delivery_id}/actions/{action_id}/reconcile`
route performs that read and stores confirmed remote state. If a complete
bounded history scan finds no marker, it marks the action failed and requires
operator action before retry; the route never repeats the write.
Only known failures can enter a bounded exponential-backoff schedule; ambiguous
and unreconciled writes cannot. No background retry loop is enabled, so an
operator must reconcile before any retry is requested. SQLite uses a local
single-process coordination model; multi-process deployment remains unsupported.
Webhook review results retain the selected provider/model identity alongside
delivery, PR, and revision identifiers; raw credentials and retrieved evidence
text are omitted.

Authenticated status GETs and the MCP action-history tool are read-only; the
MCP tool requires the webhook status token. The reconcile POST performs a
remote read and records its result. Records do not store
provider keys, GitHub tokens, retrieved evidence text, or comment bodies.
Retention remains bounded by the existing webhook database policy. Webhook
delivery rows retain review findings and provenance for status retrieval;
deployments must restrict database access and configure a retention period
appropriate to repository privacy needs.

## Provider configuration API

J3 adds these endpoints; they require `Authorization: Bearer
<AI_CODE_REVIEWER_ADMIN_TOKEN>` and fail closed if the admin token is unset:

- `GET /api/admin/provider-settings`
- `PUT /api/admin/provider-settings`
- `GET /api/verification/capabilities`

Settings are backend-only and never return the Groq key. Provider selection is
independent for review and fix tasks. Groq model identifiers are allowlisted
to the existing strict structured-output model. Qwen base model is fixed to
the approved local 3B model; adapter paths must remain under configured
`AI_CODE_REVIEWER_QWEN_ADAPTER_ROOTS`. Quantization accepts only `none`, since
the current provider does not support quantized loading. Temperature and output
token values are bounded. No arbitrary endpoint, model path, or filesystem
root can be selected. Connectivity is reported as `not_checked`; readiness
does not make a live request or load Qwen weights.

There is no secure secret vault configured in this app. A key sent through the
admin endpoint is held in process memory only, is not persisted, and is removed
from the public response. Environment injection remains supported. Configure
TLS at the trusted deployment edge; do not expose the admin route over an
untrusted network. Settings updates increment a generation and provider
instances are selected per request, so an in-progress review retains its
captured provider. Provider failures do not silently switch to another
provider. GitHub action credentials remain separate from LLM credentials.

Hosted GitHub PR review is blocked unless trusted repository policy sets
`allow_source_sharing: true` and the administrator has enabled
`source_sharing_consent` through the protected settings API or environment.
Hosted review of user-submitted source also requires that global consent. These
settings cannot be changed by PR content. Local Qwen keeps its existing
local-files-only and CUDA-only requirements. An explicitly injected provider
from an embedding application remains under that application's consent policy.

## Repository privacy and access

J1's context fetch remains scoped to the requested repository and pinned head
revision. Action records are queried by authenticated delivery ID; the API
returns only records for that delivery. Source-sharing for hosted PR analysis
requires repository policy. Provider keys are not returned to clients or placed
in URLs. GitHub write credentials stay separate from provider credentials.
Policy remains a trusted local file and webhook/model/repository content cannot
alter it. Retention deletion removes expired delivery and action records; no
separate cross-deployment data-isolation boundary is claimed.

## Offline verification evidence

The J3 tests use local fixtures and injected transports/brokers only. They cover
calibration fail-closed behavior, sandbox-unavailable and attestation checks,
candidate SHA CI validation, required commit gates, retry state restrictions,
and provider settings authorization/redaction/path validation. A mock broker
test validates request contracts; it is not evidence that a production sandbox
exists. See the final implementation report for the complete command and
results. No live GitHub, Groq, or Qwen calls, weights, or commits are used by
these offline tests.

## Live staging acceptance procedure

Use a disposable repository, least-privilege credentials, and explicitly
authorized operator approval. Record timestamps, commit SHAs, delivery IDs,
and action IDs, but never tokens or raw private source. Run these checks
separately from offline tests:

1. Configure a dedicated webhook secret/status token and verify a genuinely
   signed pull-request delivery reaches the local deployment.
2. Confirm an invalid signature is rejected and duplicate delivery IDs do not
   repeat provider work or GitHub writes.
3. Run a real PR review with repository source-sharing consent explicitly
   enabled; verify provider identity and pinned head in the audit record.
4. Enable inline comments only in the disposable repository; verify one
   authorized comment, exact line/side/SHA, and duplicate suppression.
5. Simulate an ambiguous comment response, restart the service, reconcile by
   operation marker and expected revision, and verify no blind duplicate.
6. Prepare a low-risk patch and verify the exact source, diff scope, syntax,
   static-analysis delta, and changed-file set.
7. Verify required CI on a candidate revision containing the proposed change.
   CI on the original PR head does not count.
8. Keep commit execution disabled unless the sandbox, candidate-CI strategy,
   calibration evidence, branch compare-and-swap, permission scope, all gates,
   and operational recovery have separately passed review. This implementation
   does not provide that authorization; do not attempt a live commit.
9. Verify sanitized audit retrieval, retention behavior, status-token access,
   and provider-key redaction.

Live staging must be explicitly authorized and is not claimed as completed by
this plan implementation.

## Remaining J3 blockers and J4 work

J3 blockers: signed independent calibration evidence and per-finding mapping;
trusted isolated execution broker; safe candidate-revision creation and CI;
atomic compare-and-swap branch writing; live staging; production secret-vault
integration; and cross-process coordination/monitoring. The commit gate remains
off until all mandatory checks are independently verified.

J4 may build the frontend AI Settings page, display backend readiness, guide
administrator source-sharing consent, and provide key rotation UX without
storing secrets in browser storage. J4 must not weaken server-side validation
or imply verified connectivity when readiness has only been configured.

# Final Sprint Baseline Audit

| Audit date | 2026-11-19 |
| Workspace | `/Users/shreyasbasaragi/AI Code Reviewer` |
| Branch | `development` |
| Commit | `c4397c9604747e046191c4a736d669f9a8ff2920` |
| Initial working tree | Clean (`git status --short` returned no entries) |

This is a current local implementation audit for final sprint Task 1. It does not
repeat the full FF-180 matrix in `docs/FF180_REQUIREMENTS_AUDIT.md` or
`docs/PLAN_J_FINAL_COMPLETION_AUDIT.md`. It records findings from the current
checkout and fresh offline test runs. No application source, configuration,
dataset, RAG database, Qwen artifact, or Git history was changed. No Groq,
GitHub, or other external request was made.

## Baseline test results

| Check | Result | Notes |
|---|---|---|
| Backend, dataset, and RAG: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ./.venv/bin/python -m pytest -q tests dataset_pipeline/tests rag/tests` | **433 passed, 1 failed** | Failure: `tests/test_phase_e3_4_run2.py::test_resume_preflight_refreshes_dashboard_quota_without_chroma_or_provider_calls`; one Chroma telemetry deprecation warning. No live model call occurred. |
| Isolated failing test with `GROQ_API_KEY=offline-test-key` | **1 passed** | The test’s path is local preflight only; it does not instantiate a Groq client or send a request. This fake value was used only in the command environment and was not printed or persisted. |
| Frontend: `npm test -- --run` | **14 passed** | Vitest; one Node local-storage path warning. |
| Frontend: `npm run build` | **Passed** | TypeScript check and Vite production build. |
| GitHub Actions workflow | **Not triggered** | `.github/workflows/quality.yml` invokes the same Python suite without a `GROQ_API_KEY`; therefore the clean hosted run is expected to hit the confirmed fixture failure. No workflow dispatch or GitHub write was made. |

### Confirmed baseline defect

| ID | Priority | Affected feature | Location and evidence | Confirmed cause | Minimum fix | Proof needed | Proposed task |
|---|---|---|---|---|---|---|---|
| FS-001 | **P1 — CI blocker** | Offline test suite / quality workflow | `evaluation/phase_e3_4_run2.py::run_preflight` checks `GROQ_API_KEY` and adds a blocking reason at lines 508–513. `tests/test_phase_e3_4_run2.py::test_resume_preflight_refreshes_dashboard_quota_without_chroma_or_provider_calls` expects `ready` at line 176 but never sets the required environment variable. The full suite failed there with `status == "blocked"`; rerunning that isolated test with a dummy environment value passed. The workflow at `.github/workflows/quality.yml` provides no such variable. | The fixture uses a no-provider-call preflight but omits a dummy configured-key environment value required by the preflight readiness contract. The production preflight’s credential gate is intentional; do not weaken it to fix the test. | Make the unit test hermetic by setting a dummy `GROQ_API_KEY` with pytest `monkeypatch` (or equivalent test-only fixture). Keep the preflight credential gate intact. | Run the isolated test without real credentials; run the full suite with offline variables and no real secret; confirm no network/provider calls. Confirm the GitHub workflow can pass without repository secrets. | **Task 2 (recommended first target).** The supplied sprint plan does not define the titles or scope for Tasks 2–8; this assignment is proposed by dependency/order, not an existing ticket mapping. |

No other reproducible failing test or concrete React/FastAPI payload mismatch was
found in the inspected pathways. Items below are implementation gaps or live
validation limits, not claims of observed runtime failures.

## Current architecture

- **Backend:** FastAPI routes in `mcp_server/http_api.py` use `ReviewFixService`
  for review, fix generation, verification, approval, and export. Review and
  candidate handles live in a bounded process-local store in
  `mcp_server/state.py`.
- **Review:** `mcp_server/orchestrator.py::run_review_pipeline` collects
  structured RAG evidence, calls the explicitly selected provider, validates
  evidence references and response fields, runs deterministic Python AST
  analysis, and merges findings with provenance.
- **Providers:** `llm/provider_factory.py` and
  `llm/provider_settings.py` route review and fixes independently to Groq or
  local Qwen. Groq uses a strict response schema in `llm/groq_client.py`.
  Qwen stays a selectable, lazy CUDA/PEFT provider; it is excluded from this
  sprint’s execution and ownership.
- **RAG:** `rag/retrieve.py` selects v2 by default and can select v3 through
  explicit configuration. `rag/retrieve_v3.py` implements baseline and
  optimized retrieval over read-only pinned corpus configuration. Repository
  guidance is supplemental evidence via `mcp_server/repository_context.py`.
- **GitHub:** `mcp_server/github_pr.py` reads pinned PR metadata, files, source,
  and repository context. `mcp_server/github_webhook.py` verifies HMAC over the
  raw body, persists/idempotently processes deliveries, and dispatches work to
  a bounded single worker. Optional comments use a separate policy-gated action
  adapter; commit execution is hard-disabled.
- **Fix lifecycle:** `fix_generation.py`, `fix_verification.py`, and
  `fix_lifecycle.py` produce candidate text, check syntax/static-analysis
  changes without executing candidate code, require explicit acceptance after
  required checks, and export source/diff without applying changes. Behavioral
  tests remain `not_run`; `mcp_server/behavioral_verification.py` is a
  fail-closed broker contract with no configured broker.
- **Frontend:** React in `frontend/src/` presents code review, PR review, AI
  settings, GitHub settings, and manual fix approval/export.

## Feature status matrix

| Feature | Status | Current evidence and practical limit |
|---|---|---|
| FastAPI review/fix API and local lifecycle | **VERIFIED** | `tests/test_phase_d1_http_api.py`, `tests/test_phase_d3_integration.py`; process-local review/candidate handles are bounded and do not survive process restart. |
| Request/response compatibility for primary React workflows | **VERIFIED** | The frontend routes, bodies, identifiers, and response fields match current backend definitions. Covered by backend TestClient and frontend mocked tests; no production server/browser session was run. |
| Groq provider routing and strict structured review/fix schemas | **IMPLEMENTED — NOT LIVE TESTED** | `llm/groq_client.py`, `llm/provider_factory.py`, `llm/provider_settings.py`; mocked coverage in `tests/test_groq_strict_outputs.py`, provider routing in `tests/test_provider_settings.py` and `tests/test_qwen_peft_provider.py`. Live connectivity/inference was not authorized or attempted. |
| RAG v2 integration | **IMPLEMENTED — NOT LIVE TESTED** | `rag/retrieve.py`; evidence extraction and orchestration are unit-tested. Retrieval defaults to v2. Canonical database availability and live embedding/retrieval were not probed in this audit. |
| RAG v3 integration | **IMPLEMENTED — NOT LIVE TESTED** | `rag/retrieve_v3.py`, `rag/v3/`; selection is explicit and does not silently fall back. Retrieval/build tests are in `tests/test_rag_retrieval_v3.py`, `tests/test_rag_v3_build.py`, and related RAG tests. No canonical Chroma database was opened or changed by this audit. |
| Deterministic Python static analysis and finding merge | **VERIFIED** | `static_analysis.py`, `mcp_server/orchestrator.py`; `tests/test_static_analysis.py`, review pipeline and PR tests. |
| Fix generation, syntax/static verification, explicit approval and export | **VERIFIED** | `fix_generation.py`, `fix_verification.py`, `fix_lifecycle.py`; `tests/test_phase_c1_fix_generation.py`, `tests/test_phase_c2_fix_verification.py`, `tests/test_phase_c3_fix_approval_export.py`, and API tests. Candidate code is parsed/analyzed, never executed or imported. This is static verification, not behavioral regression testing. |
| Exact-candidate behavioral verification and candidate-revision CI | **PARTIAL** | Exact-SHA CI evaluator and fail-closed broker interface exist in `mcp_server/github_ci.py` and `mcp_server/behavioral_verification.py`; no candidate revision, trusted broker, or executed checks are configured. Required behavior remains `not_run`, so commits stay disabled. |
| Read-only GitHub PR review | **IMPLEMENTED — NOT LIVE TESTED** | `mcp_server/github_pr.py`; URL/path/revision/diff/context tests in `tests/test_github_pr.py` and repository-context tests. No live GitHub API requests or token-scope validation were performed. |
| Signed webhook delivery and duplicate handling | **VERIFIED** | `mcp_server/github_webhook.py` and `/api/github/webhook`; `tests/test_github_webhook.py` covers signatures, raw-body verification, idempotency, worker and restart behavior. Public reachability, TLS, secret rotation, and production worker behavior remain unvalidated. |
| GitHub review-comment publication | **IMPLEMENTED — NOT LIVE TESTED** | `mcp_server/github_actions.py`, `mcp_server/github_action_coordinator.py`, and `mcp_server/github_policy.py`; mocked policy/revision tests in `tests/test_github_actions.py` and related tests. Current webhook comment policy is off by default and requires a separate write token. A legacy manual demo script can publish after explicit confirmation; do not use it for this baseline. |
| Autonomous commit execution | **NOT IMPLEMENTED** | `mcp_server/github_actions.py::execute_commit` and `github_policy.py::commit_execution_decision` fail closed / hard-disable execution. This is an intentional safety boundary until calibration, behavioral verification, exact-candidate CI, and staging authorization exist. |
| Admin authentication and provider/GitHub settings | **VERIFIED** (offline) | `mcp_server/admin_auth.py`, `mcp_server/credential_store.py`, provider settings and API routes; tests in `tests/test_admin_auth.py`, `tests/test_provider_settings.py`, and `tests/test_github_admin_settings.py`. Real Keychain interaction and deployed authorization were not tested. |
| Browser workflow and model selection | **VERIFIED** (mocked frontend) | `frontend/src/App.tsx`, `AISettings.tsx`, `GitHubIntegrationSettings.tsx`; 14 Vitest tests passed and build succeeded. Both Groq and Qwen options remain visible. |
| Current GitHub Actions quality workflow | **BROKEN** on a clean secret-free run | The workflow invokes the full backend command but does not set the dummy key required by the preflight test described in FS-001. It was inspected, not dispatched. |
| Deployment and multi-worker readiness | **PARTIAL** | `mcp_server/API.md` documents a single worker because review/fix handles are process-local. Deployment/TLS and service-recovery evidence are absent. |
| Quantitative end-to-end evaluation | **PARTIAL** | Evaluation and saved benchmark artifacts exist. E3.4 is closed as partially evaluated per prior handoff and was not resumed. No live provider calls or new benchmark run were made. |

## Frontend/backend contract compatibility

No concrete contract mismatch was identified in the inspected primary flows:

- `frontend/src/api.ts::submitReview` sends `POST /api/reviews` with
  `{"code", "language":"python"}`; `ReviewRequest` accepts those fields.
- The review response includes `review_id`, structured `review`, timings, and
  optional provider identity; these are represented by frontend types.
- `generateFix(reviewId, findingId)` sends `POST
  /api/reviews/{review_id}/findings/{finding_id}/fixes` with
  `{"language":"python"}`. The backend route takes the same path identifiers
  and `FixRequest`; prior first-click and subsequent-request regressions are
  covered in `tests/test_phase_d1_http_api.py`.
- Verification, accept, reject, and export use the candidate ID in the path;
  backend payloads correspond to `VerificationReport`, `ApprovalResponse`, and
  `ExportResponse`. The UI only enables acceptance after successful syntax and
  static checks; backend approval/export independently enforce them.
- PR review, provider settings, connectivity, and GitHub settings methods and
  field names line up with their current endpoint definitions and TypeScript
  types. The UI retains both Groq and Qwen choices.

This compatibility finding is supported by local API tests and frontend mocks,
not by a deployed browser-to-server session. Mocked frontend tests cannot
validate CORS, cookies, proxy configuration, or deployed URL origins.

## Groq integration readiness

**Code readiness: implemented. Live readiness: not established.** Groq is the
default provider unless settings explicitly choose Qwen. The configured model
is restricted to the model supported by the code’s strict JSON Schema response
format (`openai/gpt-oss-20b`). Review/fix use independently selected providers;
fix output pins the schema to the requested finding. Hosted source requires
explicit global consent and repository authorization for PR reviews. Client
construction and request methods have mock coverage. No Groq key was printed,
changed, or used; no connectivity or inference endpoint was called. A live
staging check requires separate user authorization and confirmation of consent,
quota, and credential availability.

## GitHub integration readiness

**Code readiness: substantial; production readiness: not established.** Current
PR retrieval and webhook processing are read-only with respect to review
publication unless the separate comment action path is explicitly configured.
Webhook signatures, delivery deduplication, PR head/base pinning, bounded reads,
and per-delivery persistence are covered offline. Comments are off by default
and require a separate GitHub write token plus repository policy. Commits remain
hard-disabled. Live permissions, TLS/public webhook delivery, credential scope,
Keychain persistence, GitHub API behavior, and recovery in deployment were not
tested. The current design uses a single worker; process-local lifecycle handles
are not a multi-worker persistence mechanism.

## Security-critical blockers

1. **No trusted behavioral execution broker or candidate-revision CI run.**
   Keep candidate execution and autonomous commits disabled. The app currently
   does not execute or import proposed code.
2. **No independently calibrated per-finding confidence evidence.** A numerical
   policy threshold alone is not a calibrated risk measure; the gate currently
   fails closed.
3. **Production credential and perimeter checks are unverified.** Actual GitHub
   token scopes, webhook reachability/TLS, external admin access controls, and
   persistent credential behavior need an authorized staging check.
4. **Approval and candidate handles are process-local.** A restart or multiple
   workers can lose or fail to share in-flight review/fix handles. The API docs
   constrain operation to one worker; durable lifecycle storage would be needed
   before claiming horizontal/restart resilience.
5. **CI test fixture currently depends on a configured-key indicator.** Fix the
   hermetic test setup without weakening production credential checks (FS-001).

The older `test_github_pr.py` is explicitly a live demo that can post comments
when run without `--dry-run`; it prompts for confirmation unless `--yes` is
provided. It was not run. The signed webhook path uses the newer policy-gated
action adapter, with comments off by default. Avoid the legacy script during
baseline and until its target repository/token are intentionally authorized.

## Qwen exclusion and teammate handoff

Qwen loading, adapter/LoRA artifacts, CUDA behavior, and inference are
**TEAMMATE-OWNED** and were not audited for operational correctness or changed.
The selected Qwen tests in the full suite are offline mocks: the provider test
injects fake tokenizers/models/runtime loaders and fake `transformers`, `peft`,
and `torch` modules. The loader test verifies `local_files_only=True` using
fakes, and the no-CUDA test fails before model loading. No weights, tokenizer
artifacts, adapter tensors, or CUDA device were used. Preserve the frontend’s
existing Qwen selection and do not add fallback to Groq.

## Defect and integration-gap routing for Tasks 2–8

The supplied Task 1 plan does not define Tasks 2–8. The following is a
**proposed dependency-based mapping**, not a claim about existing task titles:

| Sprint slot (proposed) | Issue / gap | Work boundary |
|---|---|---|
| **Task 2** | FS-001: fixture omission makes clean offline CI fail | Test-only change: set a dummy key in the test process; preserve the production gate; rerun the complete offline suite and frontend build. |
| **Task 3** | Groq integration is mocked, not live-validated | Review readiness, then perform explicitly authorized connectivity/inference checks; no silent fallback. |
| **Task 4** | GitHub PR/webhook flow is not live-validated | Authorized staging verification of read-only token scope, PR reads, signed webhook delivery, TLS, and persistence; keep comments/commits disabled. |
| **Task 5** | Behavioral broker and candidate-ref CI are not configured | Implement/test only a separately attesting isolated broker and exact-candidate CI gates; do not enable commits as part of merely proving the gate. |
| **Task 6** | No calibrated runtime confidence; approvals and handles are not durable across workers/restarts | Establish labeled-data authorization and storage/audit scope before implementation; preserve fail-closed routing. |
| **Task 7** | Deployment credential/perimeter/recovery checks are absent | Validate Keychain/secret handling, TLS, admin perimeter, retention, worker restart and operational monitoring in an authorized staging deployment. |
| **Task 8** | Full end-to-end and product metrics remain incomplete | After Tasks 2–7, run authorized end-to-end checks and report measured quality/safety/performance with denominators; do not resume E3.4 without explicit authorization. |

## Recommended first implementation target for Task 2

Make FS-001 hermetic in the test fixture. This is the only confirmed current
failure, directly blocks a secret-free CI run, requires no new dependency or
external call, and must not be “fixed” by weakening the real credential gate.
Then rerun the exact offline Python command and frontend checks. This report
does not start Task 2.

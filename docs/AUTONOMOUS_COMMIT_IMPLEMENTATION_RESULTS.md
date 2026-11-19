# Autonomous Fix and Commit Implementation Results

**Date:** 2026-11-19
**Scope:** Local implementation and offline verification only. No live GitHub
write, Groq request, other LLM inference, Docker run, or candidate execution in
the API process was performed.

## Delivered behavior

The webhook review path can now route only explicitly opted-in, allowlisted
low-risk findings into a gated autonomous-fix workflow. Existing findings and
the integrity-checked `ReviewResponse` are retained in memory for fix
generation; source and raw provider output are not added to public review
responses or durable action records. The existing fix generator creates a
candidate, and the existing syntax/static verification and diff-size checks
run before staging.

The adapter can stage the corrected file under a deterministic
`codementor-candidates/{action_id}` ref, using a separate candidate-staging
credential. It verifies the repository, PR, source bytes, branch, and pinned
head before constructing a blob, tree, one-parent commit, and candidate ref.
Candidate ref creation is idempotent. It does not delete candidates after an
ambiguous result.

The implementation uses only fixed GitHub REST routes under the validated
repository: `GET /repos/{owner}/{repo}`, `GET /pulls/{number}`, and pinned
Contents reads; candidate creation uses `POST /git/blobs`, `GET /git/commits/{sha}`,
`POST /git/trees`, `POST /git/commits`, and `POST /git/refs`. Candidate CI is
explicitly dispatched through `POST /actions/workflows/{workflow_id}/dispatches`
on the verified temporary branch using the separate staging token, then reads
`GET /actions/workflows/{workflow_id}/runs?head_sha={candidate_sha}`,
`GET /commits/{candidate_sha}/check-runs`, and
`GET /commits/{candidate_sha}/status`; the verifier requires the configured
workflow identity and named checks at that exact SHA, polling for a bounded
period while checks are pending or newly dispatched. Check runs are associated
with successful workflow runs by exact SHA and check-suite ID. Finalization updates only
`PATCH /git/refs/heads/{branch}` with `force: false`. The adapter does not accept
arbitrary URLs or caller-selected HTTP methods.

Before finalizing, the executor requires independent signed calibration
evidence, an attested remote behavioral-verification response, checks tied to
the exact candidate SHA and configured workflow identity, the low-risk and
three-line limits, repository opt-in, safe branch/path checks, and separate
final-write authorization. The final GitHub operation updates only the
configured PR head branch and uses `force: false`. It rereads the PR and branch
immediately before the update. A concurrent branch advance is returned as
stale/non-fast-forward and is not overwritten. This is a freshness check plus
non-force update, not a GitHub compare-and-swap primitive.

Ambiguous candidate and commit outcomes are persisted for explicit
reconciliation; neither is blindly retried. Reconciliation is read-only and
does not delete candidate refs. A durable action reservation and candidate SHA
are recorded before the final update so a synchronize event caused by the
CodeMentor commit can be suppressed even if the original HTTP response was
lost. Semantic action deduplication also suppresses duplicate deliveries for
the same repository, PR head, finding, and action.

## Verification

The offline test suite exercises the complete mocked webhook-to-fix-to-staging-
to-workflow-dispatch-to-CI-to-final-ref-update path, including use of the
existing fix generator and validated review response. It also exercises default-off gates, signed
evidence validation, fork/protected-path/line-limit rejection, workflow and
candidate-SHA mismatch, write-permission and tree errors, stale and concurrent
branch changes, duplicate reservations, ambiguous results, and self-trigger
suppression.

The success-path evidence is
`tests/test_autonomous_commit.py::test_webhook_review_runs_full_mocked_fix_stage_ci_and_nonforce_commit`:
it asserts a validated low-risk review is fixed by the existing generator,
calibration and attested verification pass, the candidate SHA and branch are
created, workflow dispatch is issued with the staging token, exact candidate
CI passes, the final ref update uses the separate commit token with
`force: false`, the result is `published` with the expected commit SHA, and an
idempotent replay does not issue a second PATCH.

The final focused run completed with **76 passed** across:

- `tests/test_autonomous_commit.py`
- `tests/test_github_actions.py`
- `tests/test_j3_verification.py`
- `tests/test_github_pr.py`

The complete requested offline Python run completed with **477 passed** and one
Chroma telemetry deprecation warning (`tests`, `dataset_pipeline/tests`, and
`rag/tests`). Frontend tests completed with **25 passed** across two files;
`npm run build` and `git diff --check` also passed.

These tests use local fakes/mocks. They are not live GitHub staging evidence,
and they do not demonstrate a production commit. The remote behavioral broker
is exercised through a signed protocol fixture; no candidate code ran during
these offline test runs.

## Configuration and default state

The commit/staging switches default off:

- `GITHUB_CANDIDATE_STAGING_ENABLED`
- `GITHUB_AUTONOMOUS_COMMIT_ENABLED`

Candidate staging and final branch updates use distinct credentials,
`github_candidate_staging_token` and `github_autonomous_commit_token`. The
existing owner-managed repository policy must separately set
`enable_autonomous_fixes`, allow a low-risk category, permit the target branch,
and list required checks. Runtime prerequisites are provided by:

- `AI_CODE_REVIEWER_RUNTIME_CALIBRATION_PATH` and
  `AI_CODE_REVIEWER_CALIBRATION_DATASET_ID` for a signed independent
  calibration artifact;
- `AI_CODE_REVIEWER_SANDBOX_URL` and
  `AI_CODE_REVIEWER_SANDBOX_HMAC_KEY` for an attesting remote verifier;
- `AI_CODE_REVIEWER_BEHAVIORAL_TEST_BUNDLE` for the owner-selected test bundle;
- `GITHUB_CANDIDATE_WORKFLOW_ID` and repository `required_github_checks` for
  exact candidate workflow evidence. The workflow ID must be the numeric GitHub
  workflow ID used by the fixed dispatch endpoint; the staging credential also
  needs the repository's workflow-dispatch permission.

`GET /api/verification/capabilities` reports whether relevant configuration is
present. It makes no external request and does not validate actual permissions,
artifact validity for a particular finding, trustworthiness of an operator's
remote verifier, or current CI state. Every action repeats the substantive
checks and fails closed when a prerequisite is absent or invalid.

## Files changed for this implementation

- `mcp_server/autonomous_fixes.py` — new gated workflow coordinator.
- `mcp_server/github_actions.py` — candidate revision creation, independent
  staging/final credentials, non-force commit update, idempotency and
  reconciliation.
- `mcp_server/github_action_coordinator.py` — autonomous fix dispatch and
  durable action integration.
- `mcp_server/github_policy.py` — low-risk routing and explicit execution
  authorization gates.
- `mcp_server/github_ci.py` — candidate SHA and workflow identity checks.
- `mcp_server/behavioral_verification.py` — signed remote verifier protocol.
- `mcp_server/confidence.py` — signed runtime calibration evidence validation.
- `mcp_server/github_pr.py`, `mcp_server/github_webhook.py`,
  `mcp_server/http_api.py`, `mcp_server/credential_store.py` — in-memory
  validated review context, durable action reconciliation/self-trigger
  suppression, API wiring, and separate token handling.
- `fix_generation.py`, `mcp_server/orchestrator.py` — existing validated fix
  generation is reused by the autonomous path.
- `frontend/src/types.ts`, `frontend/src/api.ts`,
  `frontend/src/GitHubIntegrationSettings.tsx`, `frontend/src/App.tsx`,
  `frontend/src/App.test.tsx`, `frontend/src/styles.css` — separate credential settings and display of
  candidate, verification, commit, and reconciliation states.
- `tests/test_autonomous_commit.py`, `tests/test_github_actions.py`,
  `tests/test_j3_verification.py`, `tests/test_github_webhook.py`,
  `tests/test_github_pr.py`, `tests/test_github_policy.py`,
  `tests/test_github_admin_settings.py`, and
  `tests/test_phase_d1_http_api.py` — offline coverage and compatibility
  checks.
- `mcp_server/API.md`, `README.md` — current gated/default-off status and
  prerequisites.

## Remaining validation and limits

### Status classification

1. **Implemented and offline-tested:** the low-risk routing/orchestration, fix
   generation, syntax/static checks, candidate object/ref creation, explicit
   workflow dispatch, exact-SHA CI validation, signed calibration and broker response validation, non-force
   final branch update, durable reservation/reconciliation, UI state, and
   mocked end-to-end path.
2. **Implemented but externally unverified:** real GitHub staging/final REST
   transport, the separately deployed broker protocol, live GitHub Actions
   workflow identity/checks, and deployment credential storage/permissions.
3. **Not implemented:** autonomous candidate-ref cleanup/retention automation,
   production monitoring/alerting, or empirical false-commit-rate measurement.
4. **Disabled by policy:** staging and commit writes both default off; finding
   classes outside the explicit low-risk allowlist, uncertain findings, forks,
   protected/default branches, oversized changes, and unresolved/ambiguous
   actions do not proceed.
5. **Blocked by missing external safety evidence:** no independently reviewed
   signed calibration artifact, real trusted sandbox operator, authorized
   staging repository/workflow, or live least-privilege permission evidence was
   supplied for this task.

- No live GitHub permission check, candidate workflow, ref update, webhook
  delivery, or repository-specific staging was performed.
- No signed calibration artifact or real trusted remote broker was supplied;
  no behaviorally verified real candidate was produced.
- The offline test suite proves only the local decision and mocked transport
  behavior. It does not establish false-commit rates or the stated confidence
  target.
- GitHub's ref API does not expose a true compare-and-swap operation. The
  implementation relies on freshness rechecks and GitHub's rejection of a
  non-fast-forward update.
- Candidate refs are retained after ambiguous outcomes. Cleanup and retention
  policy remain an operator decision and are not automated.
- Comment publication remains controlled by its existing separate policy and
  token. No comments were published during this work.
- The historical `docs/FINAL_SPRINT_RELEASE_REPORT.md` remains a snapshot of
  the earlier release audit; this document records the later implementation
  without rewriting that audit.

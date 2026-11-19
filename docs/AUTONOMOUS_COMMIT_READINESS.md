# Autonomous candidate verification readiness

## Implementation state

The project already had a fail-closed behavioral-verification client, HMAC
attestation validation, candidate branch staging, workflow dispatch, exact-SHA
check retrieval, independent confidence artifacts, repository policy, duplicate
action suppression, and a final non-force ref update behind separate
authorization. The executor previously had no deployable server/runtime, and
the autonomous executor rejected tuple-shaped LLM evidence IDs even though the
review pipeline accepts both lists and tuples.

This change adds:

- A separately hosted HTTPS broker in `deploy/behavioral_executor/service.py`.
  It authenticates fresh HMAC-signed bounded requests, binds the candidate
  digest and test bundle identity, and signs the existing attestation format.
- A runner image definition and test launcher. Candidate code runs only in a
  disposable `runsc` container with no network, no credentials, read-only root
  and input filesystems, bounded memory, one CPU quota, CPU and wall limits,
  process-count limits, temporary storage, and forced cleanup. No fallback to
  local subprocess execution exists.
- A dedicated `workflow_dispatch` candidate workflow. It checks out and
  verifies the exact candidate SHA, reports stable checks named
  `candidate-syntax`, `candidate-static`, and `candidate-regression`, and has
  only `contents: read`. Checkout does not persist credentials, no secrets are
  passed, and staging/final-commit flags are false in the regression job.
- Exact candidate SHA as a required workflow input, checked against the event
  SHA and checked-out revision.
- Independent candidate staging and verification: when final commit is
  disabled or its separate token is absent, a passing candidate remains
  staged and verified for human review, and no final ref update is attempted.
- Tuple/list evidence compatibility with strict evidence-ID type and
  membership validation.

No application setting, repository opt-in, token, or GitHub write switch was
enabled. No live GitHub or model requests were made.

## External prerequisites and current blockers

Controlled staging is **not ready to enable yet**. The implementation can be
tested offline, but no isolated executor has been deployed or tested on a
dedicated Linux host. The current environment does not establish that gVisor
`runsc` is installed, configured, or effective. The runner image has not been
built, reviewed, digest-pinned, or deployed. The dedicated host, TLS certificate,
HMAC secret, and owner-provided ZIP test bundle must be provisioned and tested
before staging is enabled.

macOS development is limited to protocol/unit tests with Docker calls mocked.
Do not run this executor against Docker Desktop or a developer machine. The
`runsc` isolation and resource enforcement claims require the external Linux
deployment procedure in [`../deploy/behavioral_executor/README.md`](../deploy/behavioral_executor/README.md).

## Runtime configuration

API-side environment variables:

- `AI_CODE_REVIEWER_SANDBOX_URL` — HTTPS URL ending in `/v1/execute`.
- `AI_CODE_REVIEWER_SANDBOX_HMAC_KEY` — same secret as the executor-side
  `AI_CODE_REVIEWER_EXECUTOR_HMAC_KEY`; never place its value in logs or docs.
- `AI_CODE_REVIEWER_BEHAVIORAL_TEST_BUNDLE` — owner-controlled, owner-only ZIP
  file with Python test files below `tests/`.
- `AI_CODE_REVIEWER_RUNTIME_CALIBRATION_PATH`,
  `AI_CODE_REVIEWER_CALIBRATION_DATASET_ID`, and
  `AI_CODE_REVIEWER_CALIBRATION_VERIFY_KEY` — required independent signed
  calibration. The existing category and confidence bounds remain in force.
- `GITHUB_CANDIDATE_WORKFLOW_ID` — numeric ID of the dedicated workflow.
- `GITHUB_CANDIDATE_CI_WAIT_SECONDS` — existing bounded wait; pending or absent
  checks remain blocked.
- `GITHUB_CANDIDATE_STAGING_TOKEN` and
  `GITHUB_AUTONOMOUS_COMMIT_TOKEN` — separate credentials, when needed.
- `GITHUB_CANDIDATE_STAGING_ENABLED` and
  `GITHUB_AUTONOMOUS_COMMIT_ENABLED` — both remain `false` by default. Staging
  may later be enabled independently; final commit remains separately disabled.
- `GITHUB_ACTION_POLICY_PATH` — administrator-owned repository opt-in and
  permitted-branch configuration. No repository policy was changed.

Executor-side settings are listed in
[`../deploy/behavioral_executor/executor.env.example`](../deploy/behavioral_executor/executor.env.example):
TLS certificate/key paths, HMAC key, immutable runner-image digest, bind/port,
concurrency, and private temporary directory. The executor host must not hold
GitHub, Groq, model, or API credentials.

GitHub credentials must remain least-privilege and separate. The staging
credential needs `Contents: write` to create a candidate revision and
`Actions: write` to dispatch the workflow. The final-commit credential needs
`Contents: write` and remains unused while final commits are disabled. The
read/check client needs only repository content/check/workflow read access. The
workflow's `GITHUB_TOKEN` is limited to `contents: read`; do not add secrets,
write permissions, or credential persistence to candidate jobs.

Install `.github/workflows/candidate-verification.yml` on the repository's
default branch first, then record its numeric workflow ID. Configure the
repository's `required_github_checks` to exactly the three stable job names
above. Existing checks must be completed successfully, belong to that workflow
identity, and match the exact candidate SHA. Missing, pending, stale, skipped,
cancelled, neutral, failed, or wrong-workflow checks continue to block final
commit.

## Safe testing sequence

1. Run offline Python tests listed below. These mock container and GitHub API
   operations and make no external writes.
2. On a dedicated Linux executor host, provision Docker Engine and gVisor,
   build/review/publish the runner image, set its immutable digest, and verify
   `runsc` is available. Exercise a non-production HMAC key and disposable
   test bundle; verify network isolation, limits, timeout, cleanup, and
   signature verification. The local mock suite does not validate deployed
   isolation.
3. Configure the API to use the HTTPS broker and a private test bundle. Verify
   `/api/verification/capabilities` reports the intended readiness without
   enabling GitHub staging.
4. After separate operator approval, enable only candidate staging for an
   explicitly opted-in test repository/branch. Use a staging-only credential;
   leave `GITHUB_AUTONOMOUS_COMMIT_ENABLED=false` and do not configure the final
   write credential. Confirm exact-SHA candidate checks pass and the operation
   ends in `AUTONOMOUS_COMMIT_DISABLED` with no final ref update.
5. Final commits require a separate explicit decision after review of staged
   behavior, independent calibration evidence, repository policy, token scope,
   audit records, and exact-SHA checks. Do not enable them as part of this
   readiness work.

For a later, separately approved commit test, the operator must first install
and verify the independent calibration artifact and the exact required CI
checks, provide `GITHUB_AUTONOMOUS_COMMIT_TOKEN` with only the required
repository `Contents: write` permission, and set
`GITHUB_AUTONOMOUS_COMMIT_ENABLED=true` in the deployment secret manager. The
administrator must separately opt the test repository and permitted branch in
through its policy file. Keep candidate staging and final-commit tokens
separate. This readiness change makes none of those configuration changes.

## Verification and remaining limits

Offline regression coverage includes request authentication, stale/replayed
requests, digest/path/limit validation, signed attestation compatibility,
container security arguments, cleanup failure, tuple evidence, candidate
staging with commits disabled, exact-SHA CI, missing/stale/failed checks, and a
fully mocked low-risk flow. The focused suite passed **99 tests**; the complete
offline backend/RAG/dataset suite passed **500 tests** with one third-party
deprecation warning. Candidate code is never executed by the Mac test suite.
The dedicated Linux `runsc` deployment and live staging remain unverified
external steps. Behavioral regression testing is only available through that
external service; GitHub Actions regression tests are separate and have not yet
been run by an actual workflow dispatch.

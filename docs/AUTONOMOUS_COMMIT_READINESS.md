# Autonomous candidate verification readiness

## Verification modes

CodeMentor supports two mutually exclusive behavioral-verification modes for
an operation:

- `remote_https`: the existing separately deployed signed executor. Keep the
  gVisor deployment under `deploy/behavioral_executor/` as an optional
  deployment path.
- `github_actions`: a trusted default-branch `workflow_run` verifier runs the
  pinned project test bundle against the exact staged candidate SHA and signs
  a bounded result artifact. It supports staging and human review by default.
  A separate explicit `prototype` policy can also let a fully gated candidate
  reach the existing final-commit adapter.

The GitHub-hosted mode is **not a gVisor equivalent**. Candidate tests run in a
disposable Docker container on an ephemeral GitHub-hosted Linux VM, with no
network, credentials, writable root, added capabilities, or host mounts, plus
CPU, memory, process and time limits. The container shares the VM kernel, so a
container escape could compromise that job's ephemeral VM. GitHub-hosted runner
ephemerality limits persistence across jobs but does not establish a separate
kernel boundary. This prototype policy therefore has a weaker execution trust
boundary than the optional gVisor deployment. It is disabled unless
`GITHUB_HOSTED_AUTONOMOUS_COMMIT_POLICY=prototype` is explicitly selected, and
still requires every repository, confidence, evidence, source identity, change
size, verification, CI, credential, branch, freshness, idempotency and durable
record gate described below. Unknown policy values fail closed. Use the
separately reviewed stronger verifier where a separate kernel boundary is
required.

Neither mode runs candidate code in the API process. The verifier refuses
missing, expired, stale, ambiguous, unsigned, failed, timed-out or mismatched
evidence. Ordinary green workflow checks are not behavioral attestations.

## GitHub-hosted workflow setup

Install both workflow files on the repository's default branch:

- `.github/workflows/candidate-verification.yml` — dispatched on the protected
  default branch, with the candidate SHA as an input. The trusted workflow
  checks out that exact commit separately and reports the named candidate CI
  checks. The candidate branch's copy of workflow files is never executed.
- `.github/workflows/behavioral-attestation.yml` — triggered by completion of
  the candidate workflow. Its verification job checks out trusted test code
  from the default branch and the exact candidate commit separately. Candidate
  source is passed as data to the container. Only the signer job receives the
  HMAC secret; that job does not check out or execute candidate code.

The behavioral test bundle is maintained on the default branch in
`tests/`, `dataset_pipeline/tests/`, and `rag/tests/`. Candidate-provided tests
are not used. The identity includes those files plus Python requirements,
pytest configuration, and the trusted runner image definition and launcher.
After merging/releasing the trusted workflow and test bundle, compute the
identity from that exact default-branch revision:

```sh
python deploy/behavioral_executor/bundle_identity.py
```

Configure repository variables:

- `CODEMENTOR_CANDIDATE_WORKFLOW_ID` — numeric workflow ID for
  `candidate-verification.yml`.
- `CODEMENTOR_BEHAVIORAL_ATTESTATION_WORKFLOW_ID` — numeric workflow ID for
  `behavioral-attestation.yml`.
- `CODEMENTOR_BEHAVIORAL_TEST_BUNDLE_SHA256` — output of the command above.
- `CODEMENTOR_VERIFIER_PYTHON_BASE_IMAGE` — a reviewed, digest-pinned
  `python:3.11-slim@sha256:<64 lowercase hex>` image reference.

After the two workflows are on the default branch, obtain their IDs and set
the public variables with the GitHub CLI (replace the two workflow IDs and
image digest with reviewed values):

```sh
gh workflow list --all --json id,name,path
gh variable set CODEMENTOR_CANDIDATE_WORKFLOW_ID --body '<candidate workflow ID>'
gh variable set CODEMENTOR_BEHAVIORAL_ATTESTATION_WORKFLOW_ID --body '<attestation workflow ID>'
gh variable set CODEMENTOR_BEHAVIORAL_TEST_BUNDLE_SHA256 \
  --body "$(python deploy/behavioral_executor/bundle_identity.py)"
gh variable set CODEMENTOR_VERIFIER_PYTHON_BASE_IMAGE \
  --body 'python:3.11-slim@sha256:<reviewed 64-character digest>'
```

Create `CODEMENTOR_BEHAVIORAL_ATTESTATION_HMAC_KEY` as a repository Actions
secret using a password manager/secret manager, then install the same value in
the API deployment as `GITHUB_BEHAVIORAL_ATTESTATION_HMAC_KEY`. Do not place
the secret in shell arguments, candidate workflow environment, or logs.

Configure repository secret:

- `CODEMENTOR_BEHAVIORAL_ATTESTATION_HMAC_KEY` — random secret of at least 32
  bytes. Store the same value in the API deployment as
  `GITHUB_BEHAVIORAL_ATTESTATION_HMAC_KEY`. Never put it in candidate workflow
  jobs or logs.

The API's read-only GitHub credential must have repository `Actions: read`,
`Contents: read`, and access to workflow runs/artifacts. The separate staging
credential (`GITHUB_CANDIDATE_STAGING_TOKEN`) needs `Contents: write` and
`Actions: write` to create the temporary candidate ref and dispatch candidate
CI. The separate final-write credential (`GITHUB_AUTONOMOUS_COMMIT_TOKEN`)
needs `Contents: write` for the permitted PR branch ref update. The candidate
workflow has only `contents: read`; the trusted `workflow_run` workflow has
`contents: read` and `actions: read`. No Groq, GitHub write or API secrets are
passed to candidate execution. Keep checkout credential persistence disabled.

The three required check-run names in `required_github_checks` must match the
candidate workflow job names: `candidate-syntax`, `candidate-static`, and
`candidate-regression`. The behavioral attestation is verified separately
against the signer workflow and pinned test-bundle identity.

API settings for this provider:

```text
GITHUB_BEHAVIORAL_VERIFICATION_PROVIDER=github_actions
GITHUB_BEHAVIORAL_ATTESTATION_HMAC_KEY=<same secret as the Actions signer>
GITHUB_BEHAVIORAL_ATTESTATION_WORKFLOW_ID=<numeric attestation workflow ID>
GITHUB_BEHAVIORAL_TEST_BUNDLE_SHA256=<pinned bundle identity>
GITHUB_CANDIDATE_WORKFLOW_ID=<numeric candidate workflow ID>
GITHUB_CANDIDATE_CI_WAIT_SECONDS=180
```

Do not configure the remote broker at the same time unless
`GITHUB_BEHAVIORAL_VERIFICATION_PROVIDER` explicitly selects exactly one
provider. An unknown, incomplete or conflicting provider configuration is
unavailable and fails closed.

## Authorization modes

All switches are off by default. Repository-level autonomous-fix opt-in and
the current category, confidence, path, branch and three-line policies remain
required in every mode.

| Mode | Configuration | Result |
| --- | --- | --- |
| Reviews only | `GITHUB_CANDIDATE_STAGING_ENABLED` unset/false; `GITHUB_AUTONOMOUS_COMMIT_ENABLED` unset/false | Reviews only; no candidate staging or final PR-branch write. |
| Staging only | `GITHUB_CANDIDATE_STAGING_ENABLED=true`; autonomous commit switch false | Candidate staging, workflow verification and CI may run; no final PR-branch write. |
| Hosted prototype final commit | Staging enabled, `GITHUB_AUTONOMOUS_COMMIT_ENABLED=true`, final-write credential configured, and `GITHUB_HOSTED_AUTONOMOUS_COMMIT_POLICY=prototype` | Final adapter may update only the authorized PR branch after every gate passes. This accepts the GitHub-hosted Docker trust boundary. |

The final write uses `GITHUB_AUTONOMOUS_COMMIT_TOKEN`, separate from
`GITHUB_CANDIDATE_STAGING_TOKEN`. Do not enable the policy or switches during
staging validation. The prototype policy is not equivalent to gVisor.

## Candidate staging-only test

The repository opt-in and current branch policy must already allow the test
repository and branch. For the first live run, use staging-only mode: set only
`GITHUB_CANDIDATE_STAGING_ENABLED=true` and configure a staging credential
with the narrow scopes above. Keep `GITHUB_AUTONOMOUS_COMMIT_ENABLED=false`,
leave `GITHUB_HOSTED_AUTONOMOUS_COMMIT_POLICY` unset, and do not configure a
final-write token.
No live workflow was dispatched during implementation.

Validate offline first:

```sh
python -m pytest -q tests/test_github_actions.py tests/test_github_pr.py \
  tests/test_autonomous_commit.py tests/test_github_behavioral_attestation.py
python -m pytest -q tests
git --no-pager diff --check
```

After workflow installation and configuration, inspect the resolved API
capability response, then separately authorize the first staging-only test.
The action must create one candidate commit, dispatch the configured workflow
on the default branch, observe its unique successful run created after the
dispatch whose title binds the exact candidate SHA, and obtain exactly one fresh
attestation from the configured default-branch signer workflow. The result
must bind repository, candidate SHA, parent SHA, changed path, source digest,
source workflow ID/run ID, signer workflow ID/run ID, and test bundle identity.
The operation must end in `AUTONOMOUS_COMMIT_DISABLED`; verify there is no
update to the PR branch. A missing or duplicate workflow run/artifact, expired
artifact, stale SHA, wrong workflow ID, test failure, timeout, or infrastructure
error must remain blocked.

## Controlled prototype final-commit setup

For an offline mocked final-write test, set the explicit policy to
`prototype` on the test executor and mock the GitHub write. Do not enable live
workflow dispatches or GitHub writes as part of this implementation. A later
controlled live test requires an owner-approved repository opt-in, reviewed
default-branch workflows and bundle identity, valid signed confidence
calibration, required candidate checks, the staging credential, the separate
final-write credential, `GITHUB_CANDIDATE_STAGING_ENABLED=true`,
`GITHUB_AUTONOMOUS_COMMIT_ENABLED=true`, and
`GITHUB_HOSTED_AUTONOMOUS_COMMIT_POLICY=prototype`. The executor checks the
PR identity and head again before the narrow, non-force ref update. Missing,
stale, ambiguous or mismatched evidence blocks the final write.

## Security assumptions and limitations

- Both workflow files must remain on the default branch and their numeric
  workflow IDs must match the API configuration. Dispatch always selects the
  default-branch workflow definition; it never selects the candidate branch's
  workflow file. Protect changes to those files, the bundle identity, runner
  image inputs and HMAC secret.
- A fresh default-branch test bundle is checked against a repository variable
  before tests run. The candidate checkout is only the exact source being
  tested; its test files and workflow definitions do not supply the trusted
  behavioral tests or signing code.
- The attestation signature proves which trusted workflow produced the result
  and what exact inputs/outcome it reported. It does not prove absence of
  kernel vulnerabilities or independently prove GitHub's infrastructure.
- Docker `--network=none`, read-only filesystems, dropped capabilities,
  `no-new-privileges`, no host mounts, and resource limits restrict the process
  inside the hosted VM. Docker still shares the runner VM kernel. The explicit
  prototype policy accepts this weaker boundary; it does not claim gVisor
  isolation or eliminate hosted-runner/kernel risk.
- GitHub-hosted runners and GitHub artifact availability are external runtime
  dependencies. A missing/ambiguous result is an error, never a pass. The
  artifact retention is one day and the API accepts only a recent attestation.
- No live GitHub writes, workflow dispatches, Groq requests, local model
  inference, or model training are part of offline validation.

## Existing gVisor deployment

The existing `deploy/behavioral_executor/` HTTPS service remains supported as
an optional mode. Its deployment must still be reviewed independently: a
dedicated Linux host, verified `runsc`, digest-pinned image, owner-only test
bundle, TLS, HMAC key, resource limits, and no GitHub/Groq/API credentials on
the executor. See `deploy/behavioral_executor/README.md` for its separate
deployment commands. Do not use Docker Desktop as a substitute for gVisor.

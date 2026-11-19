# Isolated behavioral executor

This service is a separately hosted Linux component. It is deliberately not
added to the local Compose topology and must not be run on the development Mac.
Candidate source and test code execute only in an ephemeral Docker container
using the mandatory gVisor `runsc` runtime. The service never falls back to a
host subprocess or an ordinary container runtime.

## Deployment prerequisites

- Dedicated, patched Linux host for this executor only. Do not colocate the
  CodeMentor API, GitHub credentials, model credentials, or user data on it.
- Docker Engine configured with gVisor `runsc` as a usable runtime, tested by
  the host operator. The service account needs Docker socket access, which is
  effectively root on that host; do not share the host with other workloads.
- TLS certificate and private key for the executor endpoint, an HMAC key of at
  least 32 bytes, and an executor image pushed to a private registry and pinned
  by immutable SHA-256 digest.
- Egress policy allowing the API to reach this HTTPS endpoint. Candidate
  containers themselves use `--network=none`.

Build the runner image on the dedicated Linux host using a digest-pinned Python
base image. Then push the image and configure the resulting image digest in
`AI_CODE_REVIEWER_EXECUTOR_IMAGE`:

```sh
docker build \
  --build-arg PYTHON_BASE_IMAGE='python:3.11-slim@sha256:<verified-base-digest>' \
  -f deploy/behavioral_executor/Dockerfile \
  -t registry.example/codementor-runner:reviewed .
```

Review the image, record its resulting digest, and use that digest rather than
the tag in the service environment. Push and preload that exact image digest
on the executor host; execution uses `--pull=never`. Provision `/var/lib/codementor-executor/requests`
as an owner-only directory and install `executor.env.example` as
`/etc/codementor-executor/executor.env` with mode `0600`. Install the service
unit, review the systemd hardening settings for the host, then enable the
service. `AI_CODE_REVIEWER_EXECUTOR_HMAC_KEY` must match the API's
`AI_CODE_REVIEWER_SANDBOX_HMAC_KEY`; the API URL must be
`https://<executor-host>:8443/v1/execute`.

The Python runner image contains the current project snapshot plus the
dependencies in `requirements.txt`. Candidate source is overlaid at its
validated repository path. The owner-provided test bundle is a ZIP whose Python
test files are all below `tests/`; the runner executes those tests in the
read-only baseline image. The broker validates and safely expands the test ZIP
into a temporary host directory, then bind-mounts that directory read-only.
Only the candidate source overlay and temporary test scratch space are writable
inside the container. No `pip` install, network access, GitHub token, API key,
or model credential is supplied inside candidate execution.

## Offline developer validation

On macOS, run only the protocol tests:

```sh
python -m pytest -q tests/test_behavioral_executor_service.py tests/test_j3_verification.py
```

Those tests mock Docker calls. They do not claim the host has gVisor or prove a
deployed runtime's isolation. On the dedicated Linux host, first verify
`docker info` lists `runsc`, then use a non-production HMAC key and a disposable
test bundle to call `/v1/execute`; confirm network is unavailable in the
candidate container, timeout/resource caps are effective, the container is
removed on both success and timeout, and the API validates the returned HMAC
attestation. Do not configure GitHub write tokens for this exercise.

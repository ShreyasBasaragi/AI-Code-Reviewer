# Local CodeMentor deployment

This Compose topology runs one FastAPI worker and a static React frontend. The
frontend and API share one browser origin; the API is reachable only on the
Compose network, and the published frontend port binds to loopback by default.
For a remote deployment, terminate HTTPS in a trusted edge proxy in front of
that loopback port. Do not publish the API port directly.

## Prepare runtime configuration

1. Copy `deploy/.env.example` to a private deployment configuration file.
2. Set `APP_ORIGIN` to the exact browser origin, and set the three absolute
   read-only mount paths for the existing v2/v3 Chroma stores and the already
   provisioned Hugging Face cache containing `sentence-transformers/all-MiniLM-L6-v2`.
3. Supply `GROQ_API_KEY` and any optional GitHub credentials through the
   deployment secret manager's environment injection. The example leaves these
   empty. Keep source-sharing consent false until its owner explicitly enables
   it. Groq review/fix requests are never made as part of startup or health
   checks.
4. Ensure the trusted HTTPS edge overwrites `X-Forwarded-For` with the real
   client address and sets `X-Forwarded-Proto` to `https`. The Nginx sidecar
   trusts forwarded client addresses only from loopback peers, forwards the
   sanitized address to Uvicorn, and passes only `http`/`https` scheme values.
   Uvicorn trusts forwarded headers because the API has no published host port
   and is reachable only from the web service on the Compose network.

Then validate the resolved configuration and start both services:

```sh
docker compose --env-file /path/to/private-codementor.env -f deploy/compose.yaml config --quiet
docker compose --env-file /path/to/private-codementor.env -f deploy/compose.yaml up --build -d
```

Check the frontend health at `/healthz` and the proxied API at `/api/health`.
Startup creates the admin, lifecycle, webhook-delivery, and GitHub-action
SQLite tables in the named `app_state` volume. Bootstrap credentials are
withheld from container logs; initialize the password using the local CLI
below. The volume is writable by the
non-root API user; application state databases are owner-only. SQLite
connections are opened per operation and closed before the request returns.
Compose restarts unhealthy containers; the single API worker resumes pending
webhook work using the existing queue recovery logic.

The admin setup/recovery routes remain loopback-only. For an initial container
deployment, initialize the password from an interactive local terminal with
`docker compose exec -it api python -m mcp_server.admin_cli setup-password`.
For later recovery, use
`docker compose exec -it api python -m mcp_server.admin_cli change-password`.
Both commands prompt without echoing the password, persist only its existing
Argon2id hash, and do not print a temporary credential. The existing
`reset-bootstrap` and `reset-password` commands remain available for the
loopback web setup flow. Do not weaken those endpoint gates to make a public
setup URL work.

RAG databases and embedding cache are required read-only host mounts and are
not copied into the image, rebuilt, or modified by this deployment setup.
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` prevent runtime model downloads.
The selected Qwen provider remains available in frontend settings but no Qwen
weights or adapter artifacts are copied into the image or initialized at
startup. Selecting Qwen without separately provisioned compatible artifacts
returns an explicit provider error; there is no fallback to Groq.

The Docker image is not a candidate-code sandbox. Candidate code is not
executed, behavioral tests remain `Not Run`, and autonomous GitHub commits
remain hard-disabled. GitHub comments remain off unless separately authorized
by the existing repository policy and write credential.

The Compose files were syntax-validated locally. A Docker daemon is required
to build images or run the services; no cloud deployment or container daemon
is started by the test workflow.

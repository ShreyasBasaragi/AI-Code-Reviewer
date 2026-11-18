# Plan J4B — Administrator Authentication and GitHub Integration

This guide documents the local administrator account, GitHub credential settings,
repository policy UI, and webhook status controls added for J4B. The existing
review, fix, signed-webhook, policy, and comment workflows remain the underlying
services. Autonomous commit execution remains hard-disabled.

## Start the service and create the administrator account

On first startup, the API creates a one-time bootstrap credential when no
administrator password exists. It prints the credential once in the local
server terminal and stores only a hash. Open the frontend's **AI Settings**
page, choose the first-run setup flow, and set the administrator password. The
bootstrap credential expires after 30 minutes and can only be used once. It is
not displayed by the API or written to the audit log.

The authentication database defaults to
`~/Library/Application Support/AI Code Reviewer/admin.sqlite3` on macOS and to
the XDG application-state directory on other platforms. Set
`AI_CODE_REVIEWER_AUTH_DB_PATH` to choose a specific database, or
`AI_CODE_REVIEWER_STATE_DIR` to choose the containing state directory. The
directory and database are owner-only. Passwords use Argon2id; browser sessions
are random, short-lived, HttpOnly, SameSite=Strict cookies whose hashes are
stored in the database. Cookie-authenticated state changes require an allowed
Origin. Setup and password recovery require a loopback client and reject
forwarded-peer headers.

The server startup summary reports the local URLs, administrator setup state,
GitHub credential state, and the recovery command. It never prints saved
passwords, GitHub tokens, or webhook secrets. The optional
`AI_CODE_REVIEWER_ADMIN_TOKEN` compatibility path remains available for
migration; remove it from the environment after password login is confirmed.

## Recover administrator access

From an interactive local terminal, run:

```sh
python -m mcp_server.admin_cli reset-password
```

The CLI displays a one-time reset credential in that terminal. Enter it in the
**Password recovery** flow within 10 minutes. Successful recovery consumes the
credential, replaces the password, and revokes existing sessions. To replace a
lost first-run credential before setup, the CLI also provides
`python -m mcp_server.admin_cli reset-bootstrap`. CLI commands refuse
non-interactive output so the one-time credential is not printed into a
redirected log.

## Configure GitHub credentials

Open **GitHub Integration** after signing in. Credential values are write-only;
status responses expose only whether each value is configured and which storage
mode is available. On macOS, persistent storage uses the user's Keychain.
Choose **session only** when Keychain persistence is unavailable or not wanted;
these values remain in process memory and are lost when the server exits.
Environment-managed `GITHUB_TOKEN`, `GITHUB_ACTIONS_TOKEN`, and
`GITHUB_WEBHOOK_SECRET` values remain supported for migration, and the UI does
not claim it can remove environment-managed values.

Use a read token with only the repository access needed for PR metadata,
contents, and checks. A separate fine-grained token is used for the existing
policy-gated inline-comment feature; grant Pull requests: write only to
repositories where an administrator explicitly enables comment publication.
The application never displays these tokens again after saving them.

The **Test GitHub connection** button makes one authenticated GET to GitHub's
`/user` endpoint. Repository discovery is also explicit and contacts GitHub
only after the administrator requests it. The browser receives repository
names, visibility, default branch, and permission metadata, never a credential.

## Manage repository policy

Select an accessible repository to inspect or update its policy. Policy updates
require a successful authenticated repository lookup and use the existing
fail-closed policy parser, a revision precondition, owner-only file permissions,
an inter-process lock, and atomic file replacement. Protected path defaults and
required verification checks remain enforced. The API records a sanitized
administrator audit event and refreshes the in-process action policy.

Review comments and source sharing are distinct policy choices. Both are off by
default. Comment publication additionally requires the separate action token
and still accepts only validated comments mapped to an added line on the
expected PR revision. Enabling fix preparation does not enable patch application
or commits. **Autonomous commit execution is hard-disabled** and no UI setting
can enable it.

## Configure webhook delivery

Set or rotate the webhook secret in the integration page. The API status view
shows the configured endpoint, whether a signed delivery has been processed,
the latest bounded delivery statuses, and whether external reachability has
been demonstrated by a successful signed delivery. It does not expose the
secret. GitHub must be able to reach the API over HTTPS; configure TLS and any
public ingress or tunnel outside this application, then set the matching
repository webhook URL, secret, and `pull_request` event. This local
implementation does not create a tunnel or prove production reachability.

Signed deliveries continue through the existing bounded SQLite-backed worker,
deduplication, revision checks, review pipeline, and administrator action
policy. Local UI settings do not make webhook event text trusted, bypass
verification, or initiate GitHub writes other than already-authorized inline
comments.

## Verification and limits

Offline tests cover password hashing, bootstrap/recovery use, session expiry,
rate limits, Origin/loopback restrictions, credential redaction, mocked GitHub
connection and repository flows, optimistic policy revisions, webhook status,
and the frontend administrator/settings flows. They do not exercise a real
macOS login Keychain, a live GitHub account, a public webhook delivery, TLS,
Groq inference, or Qwen inference. A real GitHub connection test or provider
connectivity test is an explicit administrator action and must be run
separately when authorized.

The webhook processor is designed for one API process. Browser sessions and
authentication metadata persist, while session-only credentials and existing
review/fix handles remain process-local. Production deployment still needs an
operator-reviewed TLS boundary, GitHub token scoping/rotation, backup and
recovery procedures for the local state database, and a real end-to-end
webhook acceptance check. See [`API.md`](../mcp_server/API.md) for endpoints and
[`FF180_REQUIREMENTS_AUDIT.md`](FF180_REQUIREMENTS_AUDIT.md) for synopsis gaps.

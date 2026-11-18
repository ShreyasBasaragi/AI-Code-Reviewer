# GitHub PR integration audit — Phase E1

This audit is based on local code inspection only. No GitHub API requests or writes were made.

## Existing capabilities

- `rag/github/client.py` has a `GitHubClient` with GET helpers for PR metadata, changed-file records, PR reviews, inline review comments, and recent closed PRs.
- Changed-file responses include GitHub's per-file `patch` when GitHub supplies one. The current client returns raw API dictionaries; there is no changed-line mapping utility.
- `rag/adapters/github_api.py` converts fetched PR/file/review/comment data into canonical RAG records.
- `mcp_server/orchestrator.py` can post a top-level PR conversation comment through `post_review_comment_to_pr`. It does not publish inline review comments or apply fixes.

## Authentication, reliability, and gaps

- Read requests use `GITHUB_TOKEN` if configured; otherwise requests are unauthenticated. The client does not print the token. It logs that no token is configured.
- The publisher also uses `GITHUB_TOKEN` (or an explicit function argument) and sends a `POST` to the Issues comments endpoint. This is a repository write, but the current review workflow defaults `post_to_github=False`.
- Requests use `urllib.request.urlopen` without an explicit timeout. Network and HTTP failures are converted to `RuntimeError`; 404 returns `None`, and 401/403 have specialized messages.
- PR files, reviews, and comments request `per_page=100` but do not follow the `Link` response header. The closed-PR helper likewise requests one page, and allows `max_prs` to be passed directly into the URL.
- `get_pr_files` returns patch text but neither computes old/new changed-line coordinates nor handles missing/truncated patches. Inline comment publication and GitHub's review-comment position requirements are absent.
- The existing code has no dedicated PR review endpoint in FastAPI and no webhook/signature handling. GitHub publishing is currently a helper, not an integrated user-facing lifecycle.
- Review posting interpolates `owner` and `repo` into a URL and returns error response text in exceptions. Any future HTTP integration should validate identifiers, avoid returning upstream bodies, enforce timeouts, and require explicit user authorization before a write.

## Prioritized next-phase checklist

1. Define read-only PR review request/response contracts and validate repository and PR identifiers.
2. Add explicit request timeouts and bounded pagination, with tests against mocked HTTP responses and `Link` headers.
3. Map unified-diff changed lines (including added/deleted files and missing patches) before associating findings with PR lines.
4. Keep analysis read-only by default; make publishing a separate explicit confirmation and use the GitHub Reviews API with validated inline positions.
5. Add least-privilege token documentation, sanitized errors, rate-limit handling, and audit metadata without logging code or credentials.
6. Add offline tests for API pagination, authentication headers, diff mapping, and a mocked non-network publishing request. Do not add live writes to tests.

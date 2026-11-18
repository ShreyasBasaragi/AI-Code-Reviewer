# Critique frontend

React + Vite + TypeScript client for the existing FastAPI review and fix API.
The editor is a lightweight textarea with a safe, React-rendered Python syntax
highlight layer; it adds no editor runtime dependency and never renders model
text as HTML.

## Local development

Install Node.js 20.19+ and npm, then from this directory:

```sh
npm install
cp .env.example .env.local
npm run dev
```

The `.env.example` points to `http://127.0.0.1:8000`. Start the API from the
repository root in another terminal:

```sh
AI_CODE_REVIEWER_CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173 \
  ./.venv/bin/python -m uvicorn mcp_server.http_api:app --host 127.0.0.1 --port 8000
```

The backend provider remains lazily configured through
`AI_CODE_REVIEWER_PROVIDER`; this frontend setup does not start model inference.
The UI accepts Python code only and enforces the backend limits of 200,000
characters and 512 KiB UTF-8. Reviews and fixes are held in the backend's
process-local cache and expire on restart or cache eviction.

The **GitHub pull request** tab accepts canonical `https://github.com/owner/repo/pull/number`
URLs. It performs a read-only analysis of bounded Python PR changes. Public PRs
can be read without authentication; private PR access uses a backend-only
`GITHUB_TOKEN` with Pull requests and Contents read permissions. Analysis never
publishes comments or changes the repository. File results show skipped or
failed work, partial completion, and whether a finding maps to a changed line.

## Build and tests

```sh
npm run build
npm test
```

Tests mock HTTP responses and do not require Qwen, Groq, embeddings, ChromaDB,
or a running API.

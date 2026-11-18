# Plan J4A — Frontend, Provider Settings, and Groq Validation

## Implemented surface

The React application includes an **AI Settings** page. Review and fix providers
can be selected independently. The page exposes the backend-approved Groq
structured-output model, the approved Qwen base model and adapter path, the
supported `none` quantization choice, temperatures, output-token limit, and an
explicit source-sharing consent checkbox.

Provider administration uses the existing `AI_CODE_REVIEWER_ADMIN_TOKEN` only
to establish a short-lived, process-local browser session. The token is held in
the password field only until the sign-in request and is cleared immediately.
The resulting cookie is HttpOnly, SameSite=Strict, scoped to `/api/admin`, and
expires after 15 minutes. Settings and Groq credentials are retained in backend
process memory only. Use a secret manager or environment injection for
production; restarting the process clears browser sessions and UI-entered Groq
key state.

Loading the settings page does not call Groq or load Qwen. **Check provider
connection** is an explicit user action. For Groq it calls the account model
list endpoint and checks only for server-approved strict structured-output
models; it does not run a completion or prove review quality. For Qwen it checks
approved adapter artifact presence without loading model weights or using the
GPU. Completed reviews and generated fixes show their actual provider/model
identity when the backend reports it. Commits remain disabled.

## Local startup

In one terminal, set an administrator token of at least 32 characters without
printing it, then start the API:

```sh
cd "/Users/shreyasbasaragi/AI Code Reviewer"
export AI_CODE_REVIEWER_ADMIN_TOKEN='(set securely in your shell)'
./.venv/bin/uvicorn mcp_server.http_api:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
cd "/Users/shreyasbasaragi/AI Code Reviewer"
npm --prefix frontend run dev
```

The development CORS allowlist includes `http://127.0.0.1:5173` and
`http://localhost:5173`. For a different UI origin, set
`AI_CODE_REVIEWER_CORS_ORIGINS` to a comma-separated exact origin list and
restart the API. Browser admin sessions over non-HTTPS are allowed only for a
loopback client.

## Offline UI check

Open the local frontend, navigate to **AI Settings**, and sign in using the
admin token. Confirm that the settings page reports connectivity as “not
checked” until the connection button is clicked. Review/fix provider selection
is independent. Groq key entry is write-only; leave it blank to preserve the
configured key. Save settings and verify the UI does not put credentials in
browser storage. Run ordinary review/fix workflows only with an explicitly
authorized provider configuration. Qwen model weights are not loaded by this
validation.

## Optional live Groq check — requires explicit authorization

This repository change did not make live Groq calls. Only perform this section
when the project owner explicitly authorizes a live request and has confirmed
current quota availability in the Groq dashboard. Configure `GROQ_API_KEY`
securely in the backend environment or enter it in the password field; never
print, paste into a report, or commit it. Check source-sharing consent before
sending private code. Click **Check provider connection** to perform a
non-inference model-list request. A true end-to-end review/fix validation also
requires an explicitly authorized review request and, separately, an
explicitly authorized fix-generation request; these consume provider quota and
may transmit submitted source. Record the actual provider/model identity and
whether the returned structured result passed validation, without recording
credentials or source code. No Groq live run or completion is required to
verify the offline implementation.

## Offline verification commands

```sh
cd "/Users/shreyasbasaragi/AI Code Reviewer"
./.venv/bin/python -m pytest tests/test_provider_settings.py tests/test_phase_d1_http_api.py -q
npm --prefix frontend test
npm --prefix frontend run build
```

These tests cover the local settings/session contract and mocked UI behavior.
They do not validate network connectivity, provider inference, hosted GitHub
permissions, or live quota. Behavioral regression testing remains `Not Run` in
the patch-verification workflow.

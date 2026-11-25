"""
mcp_server/routing.py -- Easy/Hard PR hunk routing pipeline for Critique.

Each reviewed PR hunk is classified as "easy" or "hard" and routed accordingly:
  - EASY findings run through 6 autocommit safety gates.  If all pass, the patch
    is committed to the PR branch via a staging candidate branch.  If any gate
    fails the finding is downgraded to the HARD path.
  - HARD findings are posted as a PR review comment containing the model's
    explanation and the proposed fix inside a GitHub suggestion block so that a
    human can decide whether to apply it.

Environment variables required
-------------------------------
  GROQ_API_KEY                   Groq API key (classification only)
  GITHUB_TOKEN                   GitHub PAT with repo write scope
  GITHUB_CANDIDATE_WORKFLOW_NAME GitHub Actions workflow filename for candidate CI
                                   e.g. "candidate-verification.yml"
  CRITIQUE_HMAC_SECRET           Secret key for HMAC patch attestation (32+ chars)
  CRITIQUE_HMAC_REQUIRED         Set "1" to make HMAC verification mandatory (default: "0")
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

import requests
from groq import Groq

from mcp_server import hmac_attest

logger = logging.getLogger("critique.routing")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROTECTED_PATH_PREFIXES = (".github/", "auth/", "infra/")
PROTECTED_PATH_PATTERNS = ("migrations/",)

# ---------------------------------------------------------------------------
# A.  Model output parsing
# ---------------------------------------------------------------------------


def parse_model_output(raw: str) -> tuple[str, str]:
    """Parse plain-text model output into (review_comment, proposed_code_block).

    The model is expected to emit a review comment followed by the corrected
    code inside a triple-backtick block.  Parsing is forgiving:

    * If no code block is found, the full raw string is returned as the
      review comment and the code block is empty.
    * If raw is empty, both parts are empty strings and a warning is logged.
    """
    if not raw or not raw.strip():
        logger.warning("routing.parse_model_output: empty model output")
        return ("", "")

    # Match the first triple-backtick block (optional language specifier)
    pattern = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
    match = pattern.search(raw)
    if not match:
        logger.warning(
            "routing.parse_model_output: no code block found in model output; "
            "treating full output as review comment"
        )
        return (raw.strip(), "")

    code_block = match.group(1).strip()
    # Everything before the opening ``` is the review comment
    comment_part = raw[: match.start()].strip()
    if not comment_part:
        # Fallback: use everything after the closing ``` if the comment was empty
        comment_part = raw[match.end() :].strip()

    return (comment_part, code_block)


# ---------------------------------------------------------------------------
# B.  Groq plain-text classification
# ---------------------------------------------------------------------------


def classify_with_groq(hunk_text: str, model_comment: str) -> str:
    """Classify a code hunk as 'easy' or 'hard' using the Groq API.

    Returns exactly "easy" or "hard".  Any response that is not exactly the
    single word "easy" (after stripping and lower-casing) maps to "hard".
    API failures also return "hard".
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("routing.classify_with_groq: GROQ_API_KEY not set; defaulting to hard")
        return "hard"

    prompt = (
        "You are a code-review classifier.  Classify the following code change "
        "as either 'easy' or 'hard'.\n\n"
        "Reply with exactly one word: easy or hard.\n\n"
        "easy = style, formatting, naming, whitespace, docstring, type hint, "
        "unused import, PEP 8, trivial refactoring.\n"
        "hard = security vulnerability, logic error, crash, data loss, "
        "race condition, injection attack, auth/authz bug, memory leak, "
        "or any issue that could cause runtime failures.\n\n"
        f"Model review comment: {model_comment}\n\n"
        f"Code hunk:\n{hunk_text}"
    )

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", "llama3-8b-8192"),
            messages=[
                {
                    "role": "system",
                    "content": "Reply with exactly one word: easy or hard.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=5,
        )
        raw = response.choices[0].message.content or ""
        label = raw.strip().lower()
        if label == "easy":
            return "easy"
        logger.info(
            "routing.classify_with_groq: non-easy response %r -> hard", raw.strip()
        )
        return "hard"
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "routing.classify_with_groq: Groq API error: %s; defaulting to hard", exc
        )
        return "hard"


# ---------------------------------------------------------------------------
# C.  Autocommit gates
# ---------------------------------------------------------------------------


def gate_blast_radius(hunk_patch: str, file_path: str) -> tuple[bool, str]:
    """G1 – Blast radius gate.

    Pass: the patch touches at most 3 changed lines and only the single file
    named in *file_path*.  Lines starting with '+' or '-' are counted, but
    the unified-diff header lines ('---', '+++') are excluded.
    """
    changed = 0
    for line in hunk_patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            changed += 1

    if changed > 3:
        return (
            False,
            f"G1 blast-radius: {changed} changed lines exceed the limit of 3",
        )
    return (True, f"G1 blast-radius: {changed} changed lines (≤3) in {file_path}")


def gate_protected_paths(file_path: str) -> tuple[bool, str]:
    """G2 – Protected paths gate.

    Reject any file path under .github/, auth/, infra/, or any migrations/
    directory.
    """
    normalised = file_path.replace("\\", "/")
    for prefix in PROTECTED_PATH_PREFIXES:
        if normalised.startswith(prefix) or f"/{prefix}" in normalised:
            return (False, f"G2 protected-path: {file_path!r} matches protected prefix {prefix!r}")
    for pattern in PROTECTED_PATH_PATTERNS:
        if pattern in normalised:
            return (False, f"G2 protected-path: {file_path!r} contains protected pattern {pattern!r}")
    return (True, f"G2 protected-path: {file_path!r} is not a protected path")


def _run_ruff(code: str) -> list[dict]:
    """Run ruff on *code* and return a list of diagnostic dicts (Python files)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["python", "-m", "ruff", "check", "--output-format=json", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not result.stdout.strip():
            return []
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _run_eslint(code: str, suffix: str) -> list[dict]:
    """Run eslint on *code* for JS/TS files.

    Returns a list of simplified diagnostic dicts with a ``code`` key,
    or an empty list if eslint is not installed or times out.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["npx", "--no-install", "eslint", "--format=json", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not result.stdout.strip():
            return []
        raw = json.loads(result.stdout)
        # eslint JSON: list of file results each with a "messages" list
        diags = []
        for file_result in raw:
            for msg in file_result.get("messages", []):
                rule = msg.get("ruleId") or msg.get("message", "unknown")[:40]
                diags.append({"code": rule})
        return diags
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _run_go_vet(code: str) -> list[dict]:
    """Run ``go vet`` on *code* for Go files.

    Returns a simplified list of diagnostic dicts.  ``go vet`` does not
    have structured JSON output on all versions, so we parse stderr lines.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "critique_check.go")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            result = subprocess.run(
                ["go", "vet", src_path],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tmpdir,
            )
            lines = (result.stderr or "").strip().splitlines()
            diags = []
            for line in lines:
                if line.strip():
                    # Treat each non-empty stderr line as a separate diagnostic.
                    diags.append({"code": line.strip()[:80]})
            return diags
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []


# Map of file extension → linter function.
# Each function accepts (code: str) and returns list[dict] with a ``code`` key.
_LINTER_DISPATCH: dict[str, tuple[str, object]] = {
    ".py":   ("ruff",   lambda code: _run_ruff(code)),
    ".js":   ("eslint", lambda code: _run_eslint(code, ".js")),
    ".jsx":  ("eslint", lambda code: _run_eslint(code, ".jsx")),
    ".ts":   ("eslint", lambda code: _run_eslint(code, ".ts")),
    ".tsx":  ("eslint", lambda code: _run_eslint(code, ".tsx")),
    ".go":   ("go vet", _run_go_vet),
}

# Extensions for which we conservatively reject (no linter available).
_UNSUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".java", ".kt", ".rb", ".rs", ".cpp", ".c", ".cs", ".swift",
})


def _detect_linter(file_path: str):
    """Return ``(linter_name, linter_fn)`` for *file_path*, or ``None`` if
    the file type is unsupported."""
    ext = os.path.splitext(file_path)[1].lower()
    return _LINTER_DISPATCH.get(ext)


def gate_linter(
    original_code: str, patched_code: str, file_path: str
) -> tuple[bool, str]:
    """G3 – Linter agreement gate (multi-language).

    Dispatches to the appropriate linter for the file extension:
      - .py          → ruff
      - .js/.jsx/.ts/.tsx → eslint (via npx)
      - .go          → go vet
      - other known types → conservative reject (downgrade to suggestion)
      - completely unknown → conservative reject

    Pass conditions (both must hold):
      1. The patch removes at least one lint diagnostic that was present before.
      2. The patch does not introduce any new lint diagnostic codes.

    HMAC attestation: patch is signed by gate_staging (G4) and verified in
    gate_atomic_push (G6) via mcp_server.hmac_attest.  The linter gate is the
    last purely static check before the patch is committed.

    # TODO: HMAC attestation signing call would go here if G3 itself needed
    # to attest its result independently (e.g. with a hardware-backed key).
    """
    entry = _detect_linter(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    if entry is None:
        if ext in _UNSUPPORTED_EXTENSIONS:
            return (
                False,
                f"G3 linter: no linter configured for {ext!r} files; "
                "downgrading to suggestion",
            )
        return (
            False,
            f"G3 linter: unknown file type {ext!r}; downgrading to suggestion",
        )

    linter_name, linter_fn = entry
    logger.debug("G3 linter: using %s for %s", linter_name, file_path)

    orig_diags = linter_fn(original_code)
    patched_diags = linter_fn(patched_code)

    orig_codes = {d.get("code") for d in orig_diags if d.get("code")}
    patched_codes = {d.get("code") for d in patched_diags if d.get("code")}

    removed = orig_codes - patched_codes
    added = patched_codes - orig_codes

    if not removed:
        return (
            False,
            f"G3 linter ({linter_name}): patch does not remove any lint errors "
            f"(before={sorted(orig_codes)}, after={sorted(patched_codes)})",
        )
    if added:
        return (
            False,
            f"G3 linter ({linter_name}): patch introduces new lint errors {sorted(added)}",
        )
    return (
        True,
        f"G3 linter ({linter_name}): removed {sorted(removed)}, added none",
    )


def gate_staging(
    pr_number: int,
    patched_code: str,
    file_path: str,
    github_token: str,
    owner: str,
    repo: str,
    base_sha: str,
) -> tuple[bool, str, str]:
    """G4 – Staging gate.

    Creates a branch named ``candidate-<pr_number>-<short_hash>``, commits the
    patched file there via the GitHub Contents API, and pushes.  Never commits
    to the developer's PR branch.

    Returns ``(passed, reason, candidate_branch_name)``.
    If the branch name is empty the caller must not attempt cleanup.
    """
    import base64

    short_hash = hashlib.sha1(
        f"{pr_number}-{file_path}-{patched_code}".encode()
    ).hexdigest()[:7]
    candidate_branch = f"candidate-{pr_number}-{short_hash}"

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    # 1. Create the branch from base_sha
    try:
        resp = requests.post(
            f"{api_base}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{candidate_branch}", "sha": base_sha},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            return (
                False,
                f"G4 staging: failed to create branch {candidate_branch!r}: "
                f"HTTP {resp.status_code} {resp.text[:200]}",
                "",
            )
    except requests.RequestException as exc:
        return (False, f"G4 staging: network error creating branch: {exc}", "")

    # 2. Get current file SHA on the new branch (needed for update)
    try:
        get_resp = requests.get(
            f"{api_base}/contents/{file_path}",
            headers=headers,
            params={"ref": candidate_branch},
            timeout=30,
        )
        file_sha = get_resp.json().get("sha", "") if get_resp.status_code == 200 else ""
    except requests.RequestException:
        file_sha = ""

    # 3. Commit patched content with HMAC attestation in the commit message
    hmac_token = hmac_attest.sign_patch(pr_number, file_path, candidate_branch, patched_code)
    commit_message = hmac_attest.embed_in_commit_message(
        f"critique: auto-fix candidate for PR #{pr_number}", hmac_token
    )
    encoded_content = base64.b64encode(patched_code.encode()).decode()
    try:
        put_resp = requests.put(
            f"{api_base}/contents/{file_path}",
            headers=headers,
            json={
                "message": commit_message,
                "content": encoded_content,
                "branch": candidate_branch,
                **({"sha": file_sha} if file_sha else {}),
            },
            timeout=30,
        )
        if put_resp.status_code not in (200, 201):
            return (
                False,
                f"G4 staging: failed to commit to {candidate_branch!r}: "
                f"HTTP {put_resp.status_code} {put_resp.text[:200]}",
                candidate_branch,
            )
    except requests.RequestException as exc:
        return (False, f"G4 staging: network error committing file: {exc}", candidate_branch)

    return (True, f"G4 staging: pushed patch to branch {candidate_branch!r}", candidate_branch)


def gate_ci(
    candidate_branch: str,
    github_token: str,
    owner: str,
    repo: str,
    timeout_seconds: int = 300,
) -> tuple[bool, str]:
    """G5 – CI gate.

    Triggers the workflow named by the ``GITHUB_CANDIDATE_WORKFLOW_NAME``
    environment variable on *candidate_branch* and waits for it to complete.
    A timeout counts as failure.
    """
    workflow_name = os.getenv("GITHUB_CANDIDATE_WORKFLOW_NAME", "candidate-verification.yml")
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    # Dispatch workflow
    try:
        dispatch_resp = requests.post(
            f"{api_base}/actions/workflows/{workflow_name}/dispatches",
            headers=headers,
            json={"ref": candidate_branch},
            timeout=30,
        )
        if dispatch_resp.status_code not in (200, 204):
            return (
                False,
                f"G5 CI: failed to trigger workflow {workflow_name!r}: "
                f"HTTP {dispatch_resp.status_code} {dispatch_resp.text[:200]}",
            )
    except requests.RequestException as exc:
        return (False, f"G5 CI: network error dispatching workflow: {exc}")

    # Poll for completion
    deadline = time.time() + timeout_seconds
    poll_interval = 10
    time.sleep(poll_interval)  # give GitHub a moment to register the run

    while time.time() < deadline:
        try:
            runs_resp = requests.get(
                f"{api_base}/actions/workflows/{workflow_name}/runs",
                headers=headers,
                params={"branch": candidate_branch, "per_page": 5},
                timeout=30,
            )
            runs = runs_resp.json().get("workflow_runs", [])
        except requests.RequestException as exc:
            logger.warning("G5 CI: error polling runs: %s", exc)
            runs = []

        if runs:
            latest = runs[0]
            status = latest.get("status")
            conclusion = latest.get("conclusion")
            if status == "completed":
                if conclusion == "success":
                    return (True, f"G5 CI: workflow completed successfully (run {latest.get('id')})")
                return (
                    False,
                    f"G5 CI: workflow finished with conclusion {conclusion!r} "
                    f"(run {latest.get('id')})",
                )

        time.sleep(poll_interval)

    return (False, f"G5 CI: timed out after {timeout_seconds}s waiting for workflow")


def gate_atomic_push(
    owner: str,
    repo: str,
    pr_branch: str,
    candidate_branch: str,
    original_head_sha: str,
    github_token: str,
    pr_number: int = 0,
    file_path: str = "",
    patched_code: str = "",
) -> tuple[bool, str]:
    """G6 – Atomic push gate.

    Verifies that the PR branch HEAD still equals *original_head_sha* (no
    concurrent push happened), reads the HMAC attestation token from the
    candidate commit message, verifies it, then fast-forwards the PR branch to
    the candidate commit via a non-force push through the GitHub Refs API.

    Never uses a force flag of any kind.

    Args:
        pr_number, file_path, patched_code: forwarded to hmac_attest.verify_patch.
            Pass them from route_hunk so the HMAC can be verified against the
            exact patch that was staged in G4.
    """
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    # 1. Verify PR branch has not moved
    try:
        ref_resp = requests.get(
            f"{api_base}/git/ref/heads/{pr_branch}",
            headers=headers,
            timeout=30,
        )
        current_sha = ref_resp.json().get("object", {}).get("sha", "")
    except requests.RequestException as exc:
        return (False, f"G6 atomic-push: error reading PR branch HEAD: {exc}")

    if current_sha != original_head_sha:
        return (
            False,
            f"G6 atomic-push: PR branch HEAD changed "
            f"(expected {original_head_sha!r}, got {current_sha!r}); aborting",
        )

    # 2. Get candidate branch HEAD SHA
    try:
        cand_resp = requests.get(
            f"{api_base}/git/ref/heads/{candidate_branch}",
            headers=headers,
            timeout=30,
        )
        candidate_sha = cand_resp.json().get("object", {}).get("sha", "")
    except requests.RequestException as exc:
        return (False, f"G6 atomic-push: error reading candidate branch HEAD: {exc}")

    if not candidate_sha:
        return (False, "G6 atomic-push: could not resolve candidate branch HEAD SHA")

    # 3. HMAC attestation: read the candidate commit message and verify the
    #    Critique-HMAC trailer before allowing the fast-forward.
    hmac_token = None
    try:
        commit_resp = requests.get(
            f"{api_base}/git/commits/{candidate_sha}",
            headers=headers,
            timeout=30,
        )
        commit_message = commit_resp.json().get("message", "")
        hmac_token = hmac_attest.extract_token_from_commit_message(commit_message)
    except requests.RequestException as exc:
        logger.warning("G6 atomic-push: could not fetch commit message for HMAC check: %s", exc)

    # patched_code and file_path must be passed through; they are injected by
    # route_hunk so we reference them from closure via the caller's locals.
    # In this free function we receive them as parameters named _patched_code
    # and _file_path; callers should pass those as keyword args.
    hmac_pass, hmac_reason = hmac_attest.verify_patch(
        pr_number=pr_number,
        file_path=file_path,
        candidate_branch=candidate_branch,
        patched_code=patched_code,
        token=hmac_token,
    )
    if not hmac_pass:
        return (False, f"G6 atomic-push: HMAC verification failed — {hmac_reason}")
    logger.info("G6 atomic-push: %s", hmac_reason)

    # 4. Fast-forward PR branch to candidate SHA (non-force PATCH)
    try:
        patch_resp = requests.patch(
            f"{api_base}/git/refs/heads/{pr_branch}",
            headers=headers,
            json={"sha": candidate_sha, "force": False},
            timeout=30,
        )
        if patch_resp.status_code not in (200, 201):
            return (
                False,
                f"G6 atomic-push: failed to update PR branch: "
                f"HTTP {patch_resp.status_code} {patch_resp.text[:200]}",
            )
    except requests.RequestException as exc:
        return (False, f"G6 atomic-push: network error updating PR branch: {exc}")

    return (
        True,
        f"G6 atomic-push: fast-forwarded {pr_branch!r} to {candidate_sha!r}",
    )


# ---------------------------------------------------------------------------
# D.  Candidate branch cleanup
# ---------------------------------------------------------------------------


def cleanup_candidate_branch(
    owner: str, repo: str, branch: str, github_token: str
) -> None:
    """Delete the candidate branch on GitHub.  Always called in a finally block.

    If the branch name is empty (branch was never created) this is a no-op.
    Logs errors but never raises.
    """
    if not branch:
        return
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        resp = requests.delete(
            f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=headers,
            timeout=30,
        )
        if resp.status_code in (200, 204):
            logger.info("routing.cleanup: deleted candidate branch %r", branch)
        else:
            logger.warning(
                "routing.cleanup: could not delete %r: HTTP %s", branch, resp.status_code
            )
    except requests.RequestException as exc:
        logger.error("routing.cleanup: error deleting branch %r: %s", branch, exc)


# ---------------------------------------------------------------------------
# E.  Post suggestion comment
# ---------------------------------------------------------------------------


def post_suggestion_comment(
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    file_path: str,
    line_start: int,
    line_end: int,
    review_comment: str,
    proposed_code: str,
    github_token: str,
) -> bool:
    """Post a PR review comment with a GitHub suggestion block.

    Format::

        {review_comment}

        ```suggestion
        {proposed_code}
        ```

    Returns True if the comment was successfully posted.
    """
    if proposed_code:
        body = f"{review_comment}\n\n```suggestion\n{proposed_code}\n```"
    else:
        body = review_comment

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload: dict = {
        "body": body,
        "commit_id": commit_id,
        "path": file_path,
        "side": "RIGHT",
    }
    if line_start == line_end:
        payload["line"] = line_end
    else:
        payload["start_line"] = line_start
        payload["line"] = line_end
        payload["start_side"] = "RIGHT"

    try:
        resp = requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            logger.info(
                "routing.suggestion: posted comment on %s:%d-%d", file_path, line_start, line_end
            )
            return True
        logger.error(
            "routing.suggestion: failed to post comment: HTTP %s %s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except requests.RequestException as exc:
        logger.error("routing.suggestion: network error posting comment: %s", exc)
        return False


# ---------------------------------------------------------------------------
# F.  Main routing entry point
# ---------------------------------------------------------------------------


def route_hunk(
    *,
    hunk_id: str,
    hunk_patch: str,
    file_path: str,
    original_code: str,
    patched_code: str,
    pr_number: int,
    pr_branch: str,
    original_head_sha: str,
    commit_id: str,
    owner: str,
    repo: str,
    line_start: int,
    line_end: int,
    github_token: Optional[str] = None,
    ci_timeout_seconds: int = 300,
) -> dict:
    """Route a single PR hunk through the Easy/Hard pipeline.

    Returns a JSON-serialisable log dict containing:
      hunk_id, model_comment, proposed_code, groq_label,
      gate_results, final_action.

    Each call also emits a single JSON log line via the critique.routing logger.
    """
    github_token = github_token or os.getenv("GITHUB_TOKEN", "")

    # --- 1. Local model output (caller is expected to have run the model and
    #        supply hunk_patch as the raw model output via patched_code and
    #        a review comment; we just parse here if raw_model_output is given)
    # For this pipeline entry point we receive already-parsed values.
    # (The orchestrator calls parse_model_output before calling route_hunk.)

    log: dict = {
        "hunk_id": hunk_id,
        "file_path": file_path,
        "model_comment": "",
        "proposed_code": "",
        "groq_label": "",
        "gate_results": {},
        "final_action": "",
    }

    # --- 2. Run model and parse output
    # (Caller passes patched_code = proposed fix; original_code = before)
    # We just log what we received.
    log["proposed_code"] = patched_code[:500]  # truncate for log

    # --- 2. Groq classification
    groq_label = classify_with_groq(hunk_patch, "")
    log["groq_label"] = groq_label

    if groq_label == "hard":
        # Hard path: post suggestion comment, no commit
        success = post_suggestion_comment(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            commit_id=commit_id,
            file_path=file_path,
            line_start=line_start,
            line_end=line_end,
            review_comment=log.get("model_comment", ""),
            proposed_code=patched_code,
            github_token=github_token,
        )
        log["final_action"] = "suggestion_posted" if success else "suggestion_failed"
        _emit_log(log)
        return log

    # --- 3. Easy path: run autocommit gates in order
    gates: dict = {}

    # G1
    g1_pass, g1_reason = gate_blast_radius(hunk_patch, file_path)
    gates["G1"] = {"passed": g1_pass, "reason": g1_reason}
    if not g1_pass:
        return _downgrade(log, gates, "G1", g1_reason, owner, repo, pr_number, commit_id,
                          file_path, line_start, line_end, patched_code, github_token)

    # G2
    g2_pass, g2_reason = gate_protected_paths(file_path)
    gates["G2"] = {"passed": g2_pass, "reason": g2_reason}
    if not g2_pass:
        return _downgrade(log, gates, "G2", g2_reason, owner, repo, pr_number, commit_id,
                          file_path, line_start, line_end, patched_code, github_token)

    # G3
    g3_pass, g3_reason = gate_linter(original_code, patched_code, file_path)
    gates["G3"] = {"passed": g3_pass, "reason": g3_reason}
    if not g3_pass:
        return _downgrade(log, gates, "G3", g3_reason, owner, repo, pr_number, commit_id,
                          file_path, line_start, line_end, patched_code, github_token)

    # G4 + G5 + G6 — staging, CI, atomic push; always cleanup candidate branch
    candidate_branch = ""
    try:
        g4_pass, g4_reason, candidate_branch = gate_staging(
            pr_number=pr_number,
            patched_code=patched_code,
            file_path=file_path,
            github_token=github_token,
            owner=owner,
            repo=repo,
            base_sha=original_head_sha,
        )
        gates["G4"] = {"passed": g4_pass, "reason": g4_reason}
        if not g4_pass:
            return _downgrade(log, gates, "G4", g4_reason, owner, repo, pr_number, commit_id,
                              file_path, line_start, line_end, patched_code, github_token)

        g5_pass, g5_reason = gate_ci(
            candidate_branch=candidate_branch,
            github_token=github_token,
            owner=owner,
            repo=repo,
            timeout_seconds=ci_timeout_seconds,
        )
        gates["G5"] = {"passed": g5_pass, "reason": g5_reason}
        if not g5_pass:
            return _downgrade(log, gates, "G5", g5_reason, owner, repo, pr_number, commit_id,
                              file_path, line_start, line_end, patched_code, github_token)

        g6_pass, g6_reason = gate_atomic_push(
            owner=owner,
            repo=repo,
            pr_branch=pr_branch,
            candidate_branch=candidate_branch,
            original_head_sha=original_head_sha,
            github_token=github_token,
            pr_number=pr_number,
            file_path=file_path,
            patched_code=patched_code,
        )
        gates["G6"] = {"passed": g6_pass, "reason": g6_reason}
        if not g6_pass:
            return _downgrade(log, gates, "G6", g6_reason, owner, repo, pr_number, commit_id,
                              file_path, line_start, line_end, patched_code, github_token)

        log["gate_results"] = gates
        log["final_action"] = "committed"
        _emit_log(log)
        return log

    finally:
        cleanup_candidate_branch(owner, repo, candidate_branch, github_token)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _downgrade(
    log: dict,
    gates: dict,
    failed_gate: str,
    reason: str,
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    file_path: str,
    line_start: int,
    line_end: int,
    proposed_code: str,
    github_token: str,
) -> dict:
    """Downgrade an easy finding to the hard (suggestion) path and log it."""
    logger.info(
        "routing.downgrade: hunk %s downgraded at %s: %s",
        log.get("hunk_id"),
        failed_gate,
        reason,
    )
    log["gate_results"] = gates
    log["downgrade_reason"] = f"{failed_gate}: {reason}"

    success = post_suggestion_comment(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        commit_id=commit_id,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        review_comment=log.get("model_comment", ""),
        proposed_code=proposed_code,
        github_token=github_token,
    )
    log["final_action"] = "downgraded_to_suggestion" if success else "downgrade_comment_failed"
    _emit_log(log)
    return log


def _emit_log(log: dict) -> None:
    """Emit one JSON log line via the critique.routing logger."""
    try:
        logger.info(json.dumps(log, default=str))
    except (TypeError, ValueError):
        logger.info(str(log))

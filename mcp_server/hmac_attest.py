"""
mcp_server/hmac_attest.py -- HMAC attestation for Critique autocommit patches.

Every patch that is auto-committed through the staging pipeline is signed with
an HMAC-SHA256 digest so that the G6 atomic-push gate can confirm that only
Critique-generated patches have been fast-forwarded onto a PR branch.

Design
------
* The secret key is read exclusively from the ``CRITIQUE_HMAC_SECRET`` environment
  variable.  It is never hardcoded.
* The signed payload is a canonical JSON string containing:
      pr_number, file_path, candidate_branch, patched_code_sha256
  Using the SHA-256 of the patched code rather than the raw code keeps the
  attestation token compact while still binding it to the exact byte content.
* The HMAC token is embedded in the Git commit message on the candidate branch
  (as a ``Critique-HMAC: <hex>`` trailer), where it can be read back by the
  verification step without any additional API calls.
* If ``CRITIQUE_HMAC_SECRET`` is not set, signing is skipped and a warning is
  logged.  Verification also returns a "skipped" status so that the absence of
  the secret is a soft failure (the gate still passes) rather than a hard block,
  preserving backward compatibility with deployments that have not yet configured
  the secret.  Set ``CRITIQUE_HMAC_REQUIRED=1`` to make verification mandatory.

Environment variables
---------------------
  CRITIQUE_HMAC_SECRET   32+ character secret key (required for signing/verifying)
  CRITIQUE_HMAC_REQUIRED Set to "1" to make verification failures block G6.
                          Default: "0" (soft failure, logged but not blocking).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re

logger = logging.getLogger("critique.hmac_attest")

# Regex that matches the trailer line embedded in commit messages.
_TRAILER_RE = re.compile(r"^Critique-HMAC:\s*([0-9a-f]{64})\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sign_patch(
    pr_number: int,
    file_path: str,
    candidate_branch: str,
    patched_code: str,
) -> str | None:
    """Return a hex HMAC-SHA256 token for the given patch, or None if the
    secret is not configured.

    Args:
        pr_number:        The pull request number being reviewed.
        file_path:        Path of the file being patched (repo-relative).
        candidate_branch: Name of the staging branch (``candidate-<pr>-<hash>``).
        patched_code:     The full content of the file after patching.

    Returns:
        A 64-character lowercase hex string, or ``None`` if
        ``CRITIQUE_HMAC_SECRET`` is not set.
    """
    secret = _load_secret()
    if secret is None:
        logger.warning(
            "hmac_attest.sign_patch: CRITIQUE_HMAC_SECRET not set; "
            "skipping attestation for PR #%d / %s",
            pr_number,
            file_path,
        )
        return None

    payload = _canonical_payload(pr_number, file_path, candidate_branch, patched_code)
    token = _compute_hmac(secret, payload)
    logger.debug(
        "hmac_attest.sign_patch: signed patch for PR #%d / %s -> %s…",
        pr_number,
        file_path,
        token[:16],
    )
    return token


def embed_in_commit_message(base_message: str, token: str | None) -> str:
    """Append a ``Critique-HMAC: <hex>`` trailer to *base_message*.

    If *token* is None the message is returned unchanged.
    """
    if token is None:
        return base_message
    # Ensure a blank line separates the body from the trailer (Git convention).
    separator = "\n\n" if not base_message.endswith("\n\n") else ""
    return f"{base_message}{separator}Critique-HMAC: {token}"


def extract_token_from_commit_message(message: str) -> str | None:
    """Parse the ``Critique-HMAC`` trailer from a commit message.

    Returns the hex token string, or ``None`` if no trailer is present.
    """
    match = _TRAILER_RE.search(message)
    return match.group(1) if match else None


def verify_patch(
    pr_number: int,
    file_path: str,
    candidate_branch: str,
    patched_code: str,
    token: str | None,
) -> tuple[bool, str]:
    """Verify that *token* matches the expected HMAC for the given patch.

    Returns ``(verified: bool, reason: str)``.

    Verification behaviour:
    * If ``CRITIQUE_HMAC_SECRET`` is not set → returns ``(True, "skipped: …")``
      unless ``CRITIQUE_HMAC_REQUIRED=1``.
    * If ``CRITIQUE_HMAC_REQUIRED=1`` and the secret is missing → returns
      ``(False, "required but secret not configured")``.
    * If the token is missing from the commit message → fails.
    * If the token does not match → fails.
    """
    secret = _load_secret()

    if secret is None:
        required = os.getenv("CRITIQUE_HMAC_REQUIRED", "0") == "1"
        if required:
            return (
                False,
                "HMAC attestation: CRITIQUE_HMAC_SECRET not set but CRITIQUE_HMAC_REQUIRED=1",
            )
        logger.warning(
            "hmac_attest.verify_patch: CRITIQUE_HMAC_SECRET not set; "
            "attestation skipped for PR #%d / %s",
            pr_number,
            file_path,
        )
        return (True, "HMAC attestation skipped: CRITIQUE_HMAC_SECRET not configured")

    if token is None:
        return (
            False,
            "HMAC attestation: no Critique-HMAC trailer found in commit message",
        )

    payload = _canonical_payload(pr_number, file_path, candidate_branch, patched_code)
    expected = _compute_hmac(secret, payload)

    # Constant-time comparison to prevent timing attacks.
    if hmac.compare_digest(expected, token.lower()):
        return (True, f"HMAC attestation: verified (token {token[:16]}…)")

    return (
        False,
        "HMAC attestation: token mismatch — patch may have been tampered with",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_secret() -> bytes | None:
    """Read CRITIQUE_HMAC_SECRET from the environment.

    Returns the secret as bytes, or None if not set / empty.
    """
    raw = os.getenv("CRITIQUE_HMAC_SECRET", "")
    if not raw:
        return None
    return raw.encode("utf-8")


def _canonical_payload(
    pr_number: int,
    file_path: str,
    candidate_branch: str,
    patched_code: str,
) -> bytes:
    """Build the canonical JSON payload that is signed/verified.

    Uses the SHA-256 of *patched_code* (not the raw content) so the payload
    is compact and consistent regardless of encoding variations.
    """
    code_sha256 = hashlib.sha256(patched_code.encode("utf-8")).hexdigest()
    payload_dict = {
        "pr_number": pr_number,
        "file_path": file_path,
        "candidate_branch": candidate_branch,
        "patched_code_sha256": code_sha256,
    }
    # sort_keys=True ensures the JSON is deterministic across Python runs.
    return json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compute_hmac(secret: bytes, payload: bytes) -> str:
    """Return the lowercase hex HMAC-SHA256 of *payload* signed with *secret*."""
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()

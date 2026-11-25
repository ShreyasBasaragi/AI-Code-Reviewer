"""
tests/test_hmac_attest.py -- Pytest tests for mcp_server/hmac_attest.py

All tests are purely in-process; no network or subprocess calls are made.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server import hmac_attest


# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

_SECRET = "super-secret-key-for-testing-only-32chars!"
_PR = 42
_FILE = "src/utils.py"
_BRANCH = "candidate-42-abc1234"
_CODE = "x = 1\nprint(x)\n"


# ===========================================================================
# sign_patch
# ===========================================================================


class TestSignPatch:
    def test_returns_64_char_hex_when_secret_set(self):
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            token = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, _CODE)
        assert token is not None
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_returns_none_when_no_secret(self):
        env = {k: v for k, v in os.environ.items() if k != "CRITIQUE_HMAC_SECRET"}
        with patch.dict(os.environ, env, clear=True):
            token = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, _CODE)
        assert token is None

    def test_different_code_gives_different_token(self):
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            t1 = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, "x = 1")
            t2 = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, "x = 2")
        assert t1 != t2

    def test_different_pr_gives_different_token(self):
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            t1 = hmac_attest.sign_patch(1, _FILE, _BRANCH, _CODE)
            t2 = hmac_attest.sign_patch(2, _FILE, _BRANCH, _CODE)
        assert t1 != t2

    def test_deterministic_for_same_inputs(self):
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            t1 = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, _CODE)
            t2 = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, _CODE)
        assert t1 == t2


# ===========================================================================
# embed_in_commit_message / extract_token_from_commit_message
# ===========================================================================


class TestCommitMessageRoundtrip:
    def test_embed_and_extract_roundtrip(self):
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            token = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, _CODE)
        msg = hmac_attest.embed_in_commit_message("critique: auto-fix for PR #42", token)
        extracted = hmac_attest.extract_token_from_commit_message(msg)
        assert extracted == token

    def test_embed_none_token_leaves_message_unchanged(self):
        base = "critique: auto-fix for PR #42"
        result = hmac_attest.embed_in_commit_message(base, None)
        assert result == base

    def test_extract_returns_none_when_no_trailer(self):
        msg = "just a plain commit message"
        assert hmac_attest.extract_token_from_commit_message(msg) is None

    def test_extract_ignores_malformed_trailer(self):
        msg = "some commit\n\nCritique-HMAC: not-hex!"
        assert hmac_attest.extract_token_from_commit_message(msg) is None

    def test_trailer_separator_added(self):
        base = "first line"
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            token = hmac_attest.sign_patch(_PR, _FILE, _BRANCH, _CODE)
        msg = hmac_attest.embed_in_commit_message(base, token)
        # Git convention: blank line before trailer
        assert "\n\nCritique-HMAC:" in msg


# ===========================================================================
# verify_patch
# ===========================================================================


class TestVerifyPatch:
    def _valid_token(self) -> str:
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            return hmac_attest.sign_patch(_PR, _FILE, _BRANCH, _CODE)

    def test_valid_token_passes(self):
        token = self._valid_token()
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            ok, reason = hmac_attest.verify_patch(_PR, _FILE, _BRANCH, _CODE, token)
        assert ok is True
        assert "verified" in reason.lower()

    def test_wrong_token_fails(self):
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            ok, reason = hmac_attest.verify_patch(_PR, _FILE, _BRANCH, _CODE, "a" * 64)
        assert ok is False
        assert "mismatch" in reason.lower()

    def test_missing_token_fails(self):
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            ok, reason = hmac_attest.verify_patch(_PR, _FILE, _BRANCH, _CODE, None)
        assert ok is False
        assert "no" in reason.lower() or "missing" in reason.lower() or "trailer" in reason.lower()

    def test_no_secret_skips_soft(self):
        """Without CRITIQUE_HMAC_SECRET, verification is skipped (soft pass)."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("CRITIQUE_HMAC_SECRET", "CRITIQUE_HMAC_REQUIRED")}
        with patch.dict(os.environ, env, clear=True):
            ok, reason = hmac_attest.verify_patch(_PR, _FILE, _BRANCH, _CODE, None)
        assert ok is True
        assert "skip" in reason.lower()

    def test_no_secret_with_required_flag_fails(self):
        """CRITIQUE_HMAC_REQUIRED=1 without a secret must block G6."""
        env = {k: v for k, v in os.environ.items() if k != "CRITIQUE_HMAC_SECRET"}
        env["CRITIQUE_HMAC_REQUIRED"] = "1"
        with patch.dict(os.environ, env, clear=True):
            ok, reason = hmac_attest.verify_patch(_PR, _FILE, _BRANCH, _CODE, None)
        assert ok is False
        assert "required" in reason.lower()

    def test_tampered_code_fails(self):
        """A token signed for original code must NOT verify against tampered code."""
        token = self._valid_token()
        tampered = _CODE + "\nos.system('rm -rf /')\n"
        with patch.dict(os.environ, {"CRITIQUE_HMAC_SECRET": _SECRET}):
            ok, reason = hmac_attest.verify_patch(_PR, _FILE, _BRANCH, tampered, token)
        assert ok is False


# ===========================================================================
# Multi-language linter dispatch (gate_linter extension)
# ===========================================================================


class TestMultiLanguageLinter:
    """Test that gate_linter dispatches correctly per file extension."""

    def test_py_dispatches_to_ruff(self):
        """Python files use ruff; mock _run_ruff directly."""
        import mcp_server.routing as routing
        with patch.object(routing, "_run_ruff") as mock_ruff:
            mock_ruff.side_effect = [[{"code": "E501"}], []]
            passed, reason = routing.gate_linter("orig", "patched", "foo.py")
        assert passed is True
        assert "ruff" in reason.lower()

    def test_js_dispatches_to_eslint(self):
        """JS files use eslint; mock _run_eslint via _LINTER_DISPATCH."""
        import mcp_server.routing as routing
        fake_eslint = lambda code: [{"code": "no-unused-vars"}] if "orig" in code else []
        with patch.dict(routing._LINTER_DISPATCH, {".js": ("eslint", fake_eslint)}):
            passed, reason = routing.gate_linter("orig code", "patched code", "app.js")
        assert passed is True
        assert "eslint" in reason.lower()

    def test_ts_dispatches_to_eslint(self):
        """TypeScript files should also route via eslint entry."""
        import mcp_server.routing as routing
        fake_eslint = lambda code: [{"code": "@typescript-eslint/no-explicit-any"}] if "orig" in code else []
        with patch.dict(routing._LINTER_DISPATCH, {".ts": ("eslint", fake_eslint)}):
            passed, reason = routing.gate_linter("orig", "patched", "types.ts")
        assert passed is True
        assert "eslint" in reason.lower()

    def test_go_dispatches_to_go_vet(self):
        """Go files should route via go vet entry."""
        import mcp_server.routing as routing
        fake_vet = lambda code: [{"code": "fmt.Println used"}] if "orig" in code else []
        with patch.dict(routing._LINTER_DISPATCH, {".go": ("go vet", fake_vet)}):
            passed, reason = routing.gate_linter("orig", "patched", "main.go")
        assert passed is True
        assert "go vet" in reason.lower()

    def test_unsupported_extension_rejects(self):
        """Files with unsupported extensions (e.g. .java) are rejected."""
        import mcp_server.routing as routing
        passed, reason = routing.gate_linter("orig", "patched", "Main.java")
        assert passed is False
        assert "downgrading" in reason.lower() or "no linter" in reason.lower()

    def test_unknown_extension_rejects(self):
        """Completely unknown extensions are rejected conservatively."""
        import mcp_server.routing as routing
        passed, reason = routing.gate_linter("orig", "patched", "data.xyz")
        assert passed is False
        assert "unknown" in reason.lower() or "downgrading" in reason.lower()

    def test_linter_name_in_pass_reason(self):
        """The linter name must appear in the reason for traceability."""
        import mcp_server.routing as routing
        with patch.object(routing, "_run_ruff") as mock_ruff:
            mock_ruff.side_effect = [[{"code": "F401"}], []]
            passed, reason = routing.gate_linter("orig", "patched", "util.py")
        assert passed is True
        assert "ruff" in reason.lower()

    def test_linter_name_in_fail_reason(self):
        """Failure reason must also include the linter name."""
        import mcp_server.routing as routing
        with patch.object(routing, "_run_ruff") as mock_ruff:
            mock_ruff.side_effect = [[{"code": "E501"}], [{"code": "E501"}]]
            passed, reason = routing.gate_linter("orig", "patched", "foo.py")
        assert passed is False
        assert "ruff" in reason.lower()

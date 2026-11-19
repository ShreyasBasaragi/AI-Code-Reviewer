import json
import io
import sys
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from mcp_server.admin_auth import AdminAuthStore
from mcp_server.credential_store import CredentialStore
from mcp_server.http_api import ReviewFixService, create_app
from mcp_server import admin_cli
from mcp_server.lifecycle_store import SQLiteLifecycleStore
from mcp_server.state import LifecycleState


def test_deployment_build_context_excludes_qwen_adapter_artifacts():
    dockerignore = Path(__file__).resolve().parents[1] / ".dockerignore"
    patterns = {line.strip() for line in dockerignore.read_text().splitlines()}

    # The API Dockerfile copies the repository build context. Keep the local
    # teammate-owned adapter out of that context, along with the base model.
    assert "adapter/" in patterns
    assert "llm/model" in patterns


class OfflineProvider:
    """Deterministic provider used only by this local HTTP integration test."""

    model = "offline-task7-fixture"

    def analyze_code(self, code, context):
        assert context == []
        return json.dumps({
            "summary": "The sample contains one deterministic test finding.",
            "findings": [{
                "title": "Incorrect return value",
                "issue_type": "bug",
                "explanation": "The function returns a value that does not match its contract.",
                "recommended_fix": "Return 2.",
                "affected_file": "submitted.py",
                "line_start": 2,
                "line_end": 2,
                "evidence_ids": [],
                "uncertain": True,
                "abstained": False,
            }],
        })

    def generate_fix(self, code, finding, context):
        assert context == []
        return json.dumps({
            "finding_ref": finding.finding_id,
            "corrected_code": "def value():\n    return 2\n",
            "explanation": "Return the expected value.",
            "assumptions": [],
            "evidence_ids": [],
        })


class OfflineCredentialBackend:
    def get(self, _name):
        return None

    def set(self, _name, _value):
        raise AssertionError("The integration test must not write credentials.")

    def delete(self, _name):
        return None


def _app(state_dir, auth, provider=None):
    state = LifecycleState(
        persistence_store=SQLiteLifecycleStore(state_dir / "review_lifecycle.sqlite3")
    )
    service = ReviewFixService(
        llm=provider,
        retriever=lambda _code: [],
        state=state,
    )
    credentials = CredentialStore(OfflineCredentialBackend())
    app = create_app(service, admin_auth=auth, credentials=credentials)
    return app, service


def test_local_http_workflow_starts_persists_recovers_and_shuts_down(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    lifecycle_path = state_dir / "review_lifecycle.sqlite3"
    webhook_path = state_dir / "github_webhook_deliveries.sqlite3"
    auth = AdminAuthStore(state_dir / "admin.sqlite3")
    bootstrap = auth.startup()
    monkeypatch.setenv("AI_CODE_REVIEWER_WEBHOOK_DB_PATH", str(webhook_path))
    monkeypatch.setenv("AI_CODE_REVIEWER_PROVIDER", "groq")
    monkeypatch.setenv("AI_CODE_REVIEWER_FIX_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from llm.provider_settings import PROVIDER_SETTINGS
    monkeypatch.setattr(PROVIDER_SETTINGS, "_settings", {
        **PROVIDER_SETTINGS._settings,
        "review_provider": "groq",
        "fix_provider": "groq",
        "source_sharing_consent": False,
    })
    monkeypatch.setattr(PROVIDER_SETTINGS, "_secret", None)
    monkeypatch.setattr(PROVIDER_SETTINGS, "_generation", 0)
    app, service = _app(state_dir, auth)
    origin = {"Origin": "http://127.0.0.1:5173"}

    with TestClient(app) as client:
        # Lifespan initializes all durable stores without constructing the
        # configured Groq provider or importing/loading the Qwen runtime.
        assert lifecycle_path.exists()
        assert webhook_path.exists()
        assert not service.provider_initialized
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["provider"] == "groq"
        assert health.json()["provider_initialized"] is False
        assert health.json()["state"] == "sqlite_durable"
        assert health.json()["webhook_storage"] == "sqlite_durable"

        with sqlite3.connect(webhook_path) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert {"github_webhook_deliveries", "github_action_records"} <= tables

        # Invalid credentials fail locally and do not contact Groq.
        assert client.post("/api/admin/session", json={"password": "wrong password"},
                           headers=origin).status_code == 503
        setup = client.post("/api/admin/setup", headers=origin, json={
            "bootstrap_credential": bootstrap,
            "password": "task7 integration password",
            "password_confirmation": "task7 integration password",
        })
        assert setup.status_code == 200
        assert "httponly" in setup.headers["set-cookie"].casefold()

        admin_cookie = client.cookies.get("acr_admin_session")
        assert admin_cookie

    provider = OfflineProvider()
    workflow_app, _workflow_service = _app(state_dir, auth, provider)
    with TestClient(workflow_app) as client:
        client.cookies.set("acr_admin_session", admin_cookie, path="/")
        client.headers.update(origin)
        review_response = client.post("/api/reviews", json={
            "code": "def value():\n    return 1\n",
        })
        assert review_response.status_code == 201
        review_id = review_response.json()["review_id"]
        finding_id = review_response.json()["review"]["findings"][0]["finding_id"]

        fix_response = client.post(
            f"/api/reviews/{review_id}/findings/{finding_id}/fixes", json={}
        )
        assert fix_response.status_code == 201
        candidate_id = fix_response.json()["candidate_id"]
        assert client.post(f"/api/fixes/{candidate_id}/verify").status_code == 200
        accepted = client.post(f"/api/fixes/{candidate_id}/accept")
        assert accepted.status_code == 200
        assert accepted.json()["outcome"] == "accepted"
        exported = client.get(f"/api/fixes/{candidate_id}/export")
        assert exported.status_code == 200
        assert exported.json()["corrected_code"] == "def value():\n    return 2\n"
    assert provider.model == "offline-task7-fixture"
    assert lifecycle_path.stat().st_mode & 0o777 == 0o600
    assert webhook_path.stat().st_mode & 0o777 == 0o600

    # A new app instance and lifecycle cache can recover the stored records
    # after a clean shutdown. The authentication session is also persisted.
    restarted_app, restarted_service = _app(state_dir, auth)
    with TestClient(restarted_app) as restarted:
        restarted.cookies.set("acr_admin_session", admin_cookie, path="/")
        restarted.headers.update(origin)
        assert restarted.get("/api/admin/session").json()["authenticated"]
        restored_review = restarted.get(f"/api/reviews/{review_id}")
        restored_candidate = restarted.get(f"/api/fixes/{candidate_id}")
        assert restored_review.status_code == 200
        assert restored_candidate.status_code == 200
        assert restored_candidate.json()["approval_status"] == "accepted"
        assert restarted_service.provider_initialized is False


def test_forwarded_loopback_setup_is_allowed_only_for_effective_loopback_peer(tmp_path, monkeypatch):
    auth = AdminAuthStore(tmp_path / "admin.sqlite3")
    bootstrap = auth.startup()
    # Each store path is explicit and isolated to pytest's temporary directory.
    monkeypatch.setenv("AI_CODE_REVIEWER_WEBHOOK_DB_PATH", str(tmp_path / "webhook.sqlite3"))
    app, _service = _app(tmp_path, auth)
    payload = {
        "bootstrap_credential": bootstrap,
        "password": "task7 integration password",
        "password_confirmation": "task7 integration password",
    }
    headers = {
        "Origin": "http://127.0.0.1:5173",
        "X-Forwarded-For": "127.0.0.1",
    }
    # This scope models uvicorn after it has accepted a sanitized forwarded
    # loopback address from the deployment's trusted reverse proxy.
    local = TestClient(app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 1234))
    assert local.post("/api/admin/setup", headers=headers, json=payload).status_code == 200

    remote_auth = AdminAuthStore(tmp_path / "remote-admin.sqlite3")
    remote_bootstrap = remote_auth.startup()
    monkeypatch.setenv("AI_CODE_REVIEWER_WEBHOOK_DB_PATH", str(tmp_path / "remote-webhook.sqlite3"))
    remote_app, _ = _app(tmp_path, remote_auth)
    remote = TestClient(remote_app, client=("203.0.113.8", 1234))
    payload["bootstrap_credential"] = remote_bootstrap
    assert remote.post("/api/admin/setup", headers=headers, json=payload).status_code == 403


def test_admin_cli_can_initialize_and_recover_deployment_password_without_printing_it(tmp_path, monkeypatch):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    password_values = iter([
        "initial deployment password",
        "initial deployment password",
        "replacement deployment password",
        "replacement deployment password",
    ])
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda _prompt: next(password_values))
    monkeypatch.setenv("AI_CODE_REVIEWER_STATE_DIR", str(tmp_path / "admin-state"))
    output = TTY()
    monkeypatch.setattr(sys, "stdout", output)

    monkeypatch.setattr(sys, "argv", ["admin_cli", "setup-password"])
    assert admin_cli.main() == 0
    assert "initial deployment password" not in output.getvalue()
    configured = AdminAuthStore(tmp_path / "admin-state" / "admin.sqlite3")
    configured.login("initial deployment password", "local-test")

    monkeypatch.setattr(sys, "argv", ["admin_cli", "change-password"])
    assert admin_cli.main() == 0
    assert "replacement deployment password" not in output.getvalue()
    configured.login("replacement deployment password", "local-test")

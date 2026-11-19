import os
import sqlite3
import stat
import threading

import pytest
from fastapi.testclient import TestClient

from mcp_server.admin_auth import AdminAuthError, AdminAuthStore
from mcp_server.credential_store import CredentialStore
from mcp_server.http_api import create_app


class FakeKeychain:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


def create_admin(path):
    store = AdminAuthStore(path)
    bootstrap = store.startup()
    assert bootstrap
    store.complete_setup(bootstrap, "correct horse battery staple")
    return store


def test_first_run_bootstrap_is_hashed_single_use_and_session_survives_restart(tmp_path):
    path = tmp_path / "state" / "admin.sqlite3"
    store = AdminAuthStore(path)
    bootstrap = store.startup()
    assert bootstrap and not store.setup_complete()
    with sqlite3.connect(path) as db:
        salt, digest, algorithm, memory, iterations, lanes = db.execute(
            "SELECT password_salt,password_hash,password_algorithm,argon_memory_kib,argon_iterations,argon_lanes FROM admin_auth"
        ).fetchone()
        assert salt is None and digest is None
        boot_hash = db.execute("SELECT bootstrap_hash FROM admin_auth").fetchone()[0]
    assert boot_hash != bootstrap.encode()
    store.complete_setup(bootstrap, "correct horse battery staple")
    with sqlite3.connect(path) as db:
        salt, digest, algorithm, memory, iterations, lanes = db.execute(
            "SELECT password_salt,password_hash,password_algorithm,argon_memory_kib,argon_iterations,argon_lanes FROM admin_auth"
        ).fetchone()
    assert len(salt) == 16 and len(digest) == 32 and algorithm == "argon2id"
    assert memory == 65536 and iterations == 3 and lanes == 4
    with pytest.raises(AdminAuthError, match="admin_setup_already_complete"):
        store.complete_setup(bootstrap, "another correct password")
    session, ttl = store.login("correct horse battery staple", "127.0.0.1")
    restarted = AdminAuthStore(path)
    assert ttl == 900 and restarted.valid_session(session)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_login_rate_limit_and_session_expiry(tmp_path):
    store = create_admin(tmp_path / "auth.sqlite3")
    for _ in range(5):
        with pytest.raises(AdminAuthError, match="admin_login_invalid"):
            store.login("incorrect password", "127.0.0.1")
    with pytest.raises(AdminAuthError, match="admin_login_rate_limited"):
        store.login("correct horse battery staple", "127.0.0.1")
    with store._connect() as db:
        db.execute("UPDATE admin_login_attempts SET window_started=0")
    session, _ = store.login("correct horse battery staple", "127.0.0.1")
    with store._connect() as db:
        db.execute("UPDATE admin_sessions SET expires_at=0")
    assert not store.valid_session(session)


def test_recovery_is_expiring_single_use_and_revokes_every_session(tmp_path):
    store = create_admin(tmp_path / "auth.sqlite3")
    session, _ = store.login("correct horse battery staple", "127.0.0.1")
    recovery = store.issue_recovery()
    store.reset_password(recovery, "new correct horse battery staple")
    assert not store.valid_session(session)
    with pytest.raises(AdminAuthError, match="recovery_credential_invalid_expired_or_used"):
        store.reset_password(recovery, "replay recovery password")
    replacement, _ = store.login("new correct horse battery staple", "127.0.0.1")
    assert store.valid_session(replacement)
    assert any(event["event"] == "administrator_password_reset" for event in store.audit_events())


def test_expired_recovery_credential_is_rejected(tmp_path):
    store = create_admin(tmp_path / "auth.sqlite3")
    recovery = store.issue_recovery()
    with store._connect() as db:
        db.execute("UPDATE admin_auth SET recovery_expires=0")
    with pytest.raises(AdminAuthError, match="recovery_credential_invalid_expired_or_used"):
        store.reset_password(recovery, "new correct horse battery staple")


def test_recovery_credential_cannot_be_consumed_concurrently(tmp_path):
    path = tmp_path / "auth.sqlite3"
    store = create_admin(path)
    recovery = store.issue_recovery()
    stores = [AdminAuthStore(path), AdminAuthStore(path)]
    outcomes = []

    def consume(index):
        try:
            stores[index].reset_password(recovery, f"concurrent replacement password {index}")
            outcomes.append("used")
        except AdminAuthError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=consume, args=(index,)) for index in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["rejected", "used"]


def test_startup_prints_configured_urls_recovery_and_bootstrap_only_once(tmp_path, monkeypatch, capsys):
    from mcp_server.credential_store import CredentialStore
    from mcp_server.lifecycle_store import SQLiteLifecycleStore
    from mcp_server.state import PROCESS_STATE

    monkeypatch.setenv("AI_CODE_REVIEWER_API_URL", "http://127.0.0.1:9123")
    monkeypatch.setenv("AI_CODE_REVIEWER_FRONTEND_URL", "http://localhost:4173")
    monkeypatch.setenv("AI_CODE_REVIEWER_WEBHOOK_DB_PATH", str(tmp_path / "webhooks.sqlite3"))
    monkeypatch.setattr(PROCESS_STATE, "persistence_store",
                        SQLiteLifecycleStore(tmp_path / "lifecycle.sqlite3"))
    auth = AdminAuthStore(tmp_path / "admin.sqlite3")
    app = create_app(admin_auth=auth, credentials=CredentialStore(FakeKeychain()))
    with TestClient(app):
        first = capsys.readouterr().out
    assert "API: http://127.0.0.1:9123" in first
    assert "Admin Login: http://localhost:4173/settings" in first
    assert "Recovery: python -m mcp_server.admin_cli reset-password" in first
    assert "One-time bootstrap credential" in first
    bootstrap = first.split("One-time bootstrap credential (expires in 30 minutes): ", 1)[1].splitlines()[0]
    with TestClient(app):
        second = capsys.readouterr().out
    assert "Authentication: Setup required" in second
    assert "reset-bootstrap" in second
    assert bootstrap not in second


def test_bootstrap_credential_can_be_withheld_from_deployment_logs(tmp_path, monkeypatch, capsys):
    from mcp_server.http_api import _print_startup_summary

    auth = AdminAuthStore(tmp_path / "auth.sqlite3")
    bootstrap = auth.startup()
    monkeypatch.setenv("AI_CODE_REVIEWER_PRINT_BOOTSTRAP_CREDENTIAL", "false")
    _print_startup_summary(auth, CredentialStore(FakeKeychain()), bootstrap)
    output = capsys.readouterr().out
    assert "Bootstrap credential withheld from logs" in output
    assert bootstrap not in output


def test_setup_login_logout_and_csrf_use_cookie_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", "legacy-token-for-migration-0123456789")
    auth = AdminAuthStore(tmp_path / "auth.sqlite3")
    bootstrap = auth.startup()
    client = TestClient(create_app(admin_auth=auth, credentials=CredentialStore(FakeKeychain())))
    origin = {"Origin": "http://127.0.0.1:5173"}
    bad = client.post("/api/admin/setup", json={"bootstrap_credential": "invalid-bootstrap-credential-long",
                     "password": "correct horse battery staple", "password_confirmation": "correct horse battery staple"}, headers=origin)
    assert bad.status_code == 401
    setup = client.post("/api/admin/setup", json={"bootstrap_credential": bootstrap,
                       "password": "correct horse battery staple", "password_confirmation": "correct horse battery staple"}, headers=origin)
    assert setup.status_code == 200 and "httponly" in setup.headers["set-cookie"].casefold()
    assert client.get("/api/admin/session").json()["authenticated"]
    no_csrf = client.put("/api/admin/provider-settings", json={"review_provider": "qwen_local"})
    assert no_csrf.status_code == 403
    assert client.put("/api/admin/provider-settings", json={"review_provider": "qwen_local"}, headers=origin).status_code == 200
    assert client.delete("/api/admin/session", headers=origin).status_code == 200
    assert not client.get("/api/admin/session").json()["authenticated"]
    assert client.post("/api/admin/session", json={"password": "correct horse battery staple"}, headers=origin).status_code == 200
    assert any(event["event"] == "administrator_setup_completed" for event in auth.audit_events())


def test_first_run_setup_rejects_remote_peer_and_forwarded_peer_headers(tmp_path):
    auth = AdminAuthStore(tmp_path / "auth.sqlite3")
    bootstrap = auth.startup()
    app = create_app(admin_auth=auth, credentials=CredentialStore(FakeKeychain()))
    payload = {"bootstrap_credential": bootstrap, "password": "correct horse battery staple",
               "password_confirmation": "correct horse battery staple"}
    remote = TestClient(app, client=("203.0.113.10", 1234))
    headers = {"Origin": "https://review.example", "X-Forwarded-For": "127.0.0.1"}
    denied = remote.post("/api/admin/setup", json=payload, headers=headers)
    assert denied.status_code == 403
    local = TestClient(app)
    forwarded = local.post("/api/admin/setup", json=payload,
                           headers={"Origin": "http://127.0.0.1:5173", "X-Forwarded-For": "127.0.0.1"})
    assert forwarded.status_code == 403
    assert not auth.setup_complete()


def test_legacy_token_remains_available_only_as_documented_migration_path(tmp_path, monkeypatch):
    legacy = "legacy-admin-token-minimum-32-characters"
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", legacy)
    client = TestClient(create_app(admin_auth=AdminAuthStore(tmp_path / "auth.sqlite3"),
                                   credentials=CredentialStore(FakeKeychain())))
    response = client.post("/api/admin/session", json={"admin_token": legacy},
                           headers={"Origin": "http://127.0.0.1:5173"})
    assert response.status_code == 200
    assert client.get("/api/admin/provider-settings").status_code == 200
    assert legacy not in response.text

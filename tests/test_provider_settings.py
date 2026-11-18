from fastapi.testclient import TestClient

from llm.provider_settings import ProviderSettingsStore, ProviderSettingsError
from mcp_server.http_api import create_app


def test_settings_store_redacts_secret_and_rejects_unapproved_values(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CODE_REVIEWER_QWEN_ADAPTER_ROOTS", str(tmp_path))
    monkeypatch.setenv("QWEN_ADAPTER_PATH", str(tmp_path))
    store = ProviderSettingsStore()
    store.update({"qwen_adapter_path": str(tmp_path), "groq_api_key": "gsk_" + "x" * 40})
    public = store.public_snapshot()
    assert public["groq_api_key_configured"] is True
    assert "gsk_" not in str(public)
    assert store.task_snapshot("review")["api_key"].startswith("gsk_")
    try:
        store.update({"groq_model": "https://attacker.invalid/model"})
    except ProviderSettingsError as exc:
        assert str(exc) == "groq_model_not_approved"
    else:
        raise AssertionError("unapproved model accepted")
    try:
        store.update({"qwen_adapter_path": str(tmp_path.parent / "outside")})
    except ProviderSettingsError as exc:
        assert str(exc) == "qwen_adapter_path_not_approved"
    else:
        raise AssertionError("arbitrary adapter path accepted")


def test_provider_settings_api_requires_admin_and_never_returns_key(monkeypatch):
    admin_token = "admin-secret-value-32-characters-minimum"
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", admin_token)
    import llm.provider_settings as settings_module

    monkeypatch.setattr(settings_module, "PROVIDER_SETTINGS", ProviderSettingsStore())
    client = TestClient(create_app())
    denied = client.get("/api/admin/provider-settings")
    assert denied.status_code == 401
    denied_update = client.put("/api/admin/provider-settings", json={"review_provider": "qwen_local"})
    assert denied_update.status_code == 401
    updated = client.put("/api/admin/provider-settings", headers={"Authorization": "Bearer " + admin_token},
                         json={"groq_api_key": "gsk_" + "z" * 40, "review_provider": "groq"})
    assert updated.status_code == 200
    assert updated.json()["groq_api_key_configured"] is True
    assert "gsk_" not in updated.text
    fetched = client.get("/api/admin/provider-settings", headers={"Authorization": "Bearer " + admin_token})
    assert fetched.status_code == 200 and "groq_api_key" not in fetched.json()
    assert fetched.json()["groq_connectivity"] == "not_checked"


def test_invalid_admin_payload_does_not_echo_rejected_secret(monkeypatch):
    admin_token = "admin-secret-value-32-characters-minimum"
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", admin_token)
    client = TestClient(create_app())
    secret = "z" * 600
    response = client.put("/api/admin/provider-settings",
                          headers={"Authorization": "Bearer " + admin_token},
                          json={"groq_api_key": secret})
    assert response.status_code == 422
    assert secret not in response.text


def test_unconsented_hosted_review_is_blocked_before_provider_construction(monkeypatch):
    import llm.provider_settings as settings_module

    monkeypatch.setenv("AI_CODE_REVIEWER_SOURCE_SHARING_CONSENT", "false")
    monkeypatch.setattr(settings_module, "PROVIDER_SETTINGS", ProviderSettingsStore())
    client = TestClient(create_app())
    response = client.post("/api/reviews", json={"code": "value = 1\n"})
    assert response.status_code == 403
    assert response.json()["detail"] == "source_sharing_not_authorized"


def test_browser_admin_session_is_httponly_short_lived_and_allows_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("AI_CODE_REVIEWER_AUTH_DB_PATH", str(tmp_path / "admin.sqlite3"))
    import llm.provider_settings as settings_module

    monkeypatch.setattr(settings_module, "PROVIDER_SETTINGS", ProviderSettingsStore())
    client = TestClient(create_app())
    assert client.get("/api/admin/session").json()["authenticated"] is False
    login = client.post("/api/admin/session", json={"admin_token": "a" * 40},
                        headers={"Origin": "http://127.0.0.1:5173"})
    assert login.status_code == 200
    assert login.json() == {"authenticated": True, "expires_in_seconds": 900}
    cookie = login.headers["set-cookie"].casefold()
    assert "httponly" in cookie and "samesite=strict" in cookie and "acr_admin_session" in cookie
    assert "a" * 40 not in login.text
    assert client.get("/api/admin/provider-settings").status_code == 200
    assert client.get("/api/admin/provider-options").json()["groq_models"]
    assert client.put("/api/admin/provider-settings", json={"review_provider": "qwen_local"},
                      headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.put("/api/admin/provider-settings", json={"review_provider": "qwen_local"},
                      headers={"Origin": "http://127.0.0.1:5173"}).status_code == 200
    assert client.delete("/api/admin/session", headers={"Origin": "http://127.0.0.1:5173"}).status_code == 200
    assert client.get("/api/admin/provider-settings").status_code == 401


def test_browser_admin_session_rejects_bad_token_and_untrusted_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", "b" * 40)
    monkeypatch.setenv("AI_CODE_REVIEWER_AUTH_DB_PATH", str(tmp_path / "admin.sqlite3"))
    client = TestClient(create_app())
    assert client.post("/api/admin/session", json={"admin_token": "c" * 40}).status_code == 401
    assert client.post("/api/admin/session", json={"admin_token": "b" * 40},
                       headers={"Origin": "https://attacker.invalid"}).status_code == 403


def test_connectivity_check_is_explicit_and_never_returns_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", "d" * 40)
    monkeypatch.setenv("AI_CODE_REVIEWER_AUTH_DB_PATH", str(tmp_path / "admin.sqlite3"))
    import llm.provider_settings as settings_module

    store = ProviderSettingsStore()
    store.update({"groq_api_key": "gsk_" + "q" * 40})
    monkeypatch.setattr(settings_module, "PROVIDER_SETTINGS", store)
    client = TestClient(create_app())
    client.post("/api/admin/session", json={"admin_token": "d" * 40})
    # Loading settings is local and leaves connectivity untested.
    assert client.get("/api/admin/provider-settings").json()["groq_connectivity"] == "not_checked"
    assert "gsk_" not in client.get("/api/admin/provider-options").text


def test_explicit_groq_connectivity_uses_models_list_without_inference(monkeypatch):
    monkeypatch.setenv("AI_CODE_REVIEWER_ADMIN_TOKEN", "e" * 40)
    import groq
    import llm.provider_settings as settings_module

    store = ProviderSettingsStore()
    store.update({"groq_api_key": "gsk_" + "w" * 40, "source_sharing_consent": True})
    monkeypatch.setattr(settings_module, "PROVIDER_SETTINGS", store)
    calls = []

    class FakeGroq:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))
            self.models = self

        def list(self):
            calls.append(("models.list", None))
            return type("Response", (), {"data": [type("Model", (), {"id": "openai/gpt-oss-20b"})()]})()

    monkeypatch.setattr(groq, "Groq", FakeGroq)
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer " + "e" * 40}
    assert calls == []
    result = client.post("/api/admin/provider-connectivity", headers=headers)
    assert result.status_code == 200
    assert result.json()["groq"]["status"] == "passed"
    assert calls[-1] == ("models.list", None)
    assert "gsk_" not in result.text

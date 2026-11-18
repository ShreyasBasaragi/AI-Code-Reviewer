import json

from fastapi.testclient import TestClient

from mcp_server.admin_auth import AdminAuthStore
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


class FakeGitHubClient:
    calls = []

    def __init__(self, token=None):
        self.token = token
        self.calls.append(("init", token))

    def get_authenticated_user(self):
        self.calls.append(("user", self.token))
        return {"login": "review-admin", "id": 7, "scopes": "repo", "accepted_permissions": "pull_requests=read"}

    def list_accessible_repositories(self):
        self.calls.append(("repositories", self.token))
        return [{"id": 8, "full_name": "acme/service", "owner": "acme", "name": "service",
                 "private": True, "permissions": {"pull": True, "push": False, "admin": False}}]

    def get_repository(self, owner, repository):
        self.calls.append(("repository", self.token, owner, repository))
        return {"full_name": f"{owner}/{repository}"}


def make_client(tmp_path, monkeypatch):
    for name in ("GITHUB_TOKEN", "GITHUB_ACTIONS_TOKEN", "GITHUB_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_CODE_REVIEWER_WEBHOOK_DB_PATH", str(tmp_path / "webhooks.sqlite3"))
    monkeypatch.setenv("GITHUB_ACTION_POLICY_PATH", str(tmp_path / "policy.json"))
    auth = AdminAuthStore(tmp_path / "admin.sqlite3")
    bootstrap = auth.startup()
    auth.complete_setup(bootstrap, "correct horse battery staple")
    token, _ = auth.create_session()
    keychain = FakeKeychain()
    credentials = CredentialStore(keychain)
    client = TestClient(create_app(admin_auth=auth, credentials=credentials))
    client.cookies.set("acr_admin_session", token, path="/api/admin")
    return client, auth, credentials, {"Origin": "http://127.0.0.1:5173"}


def test_github_credentials_are_redacted_and_connection_test_is_explicit(tmp_path, monkeypatch):
    import mcp_server.http_api as http_api
    FakeGitHubClient.calls = []
    monkeypatch.setattr(http_api, "GitHubRESTClient", FakeGitHubClient)
    client, auth, credentials, headers = make_client(tmp_path, monkeypatch)
    assert client.get("/api/admin/github/status").json()["github_token_configured"] is False
    saved = client.put("/api/admin/github/credentials", headers=headers,
                       json={"credential_type": "github_token", "value": "ghp_" + "a" * 36,
                             "persistent": True})
    assert saved.status_code == 200 and "ghp_" not in saved.text
    assert credentials.get("github_token") == "ghp_" + "a" * 36
    assert FakeGitHubClient.calls == []
    status = client.get("/api/admin/github/status").json()
    assert status["github_token_configured"] is True and "ghp_" not in str(status)
    checked = client.post("/api/admin/github/test", headers=headers)
    assert checked.status_code == 200 and checked.json()["account"]["login"] == "review-admin"
    assert FakeGitHubClient.calls[-1][0] == "user"
    assert "ghp_" not in checked.text
    assert client.delete("/api/admin/github/credentials/github_token", headers=headers).status_code == 200
    assert credentials.get("github_token") is None
    assert all("ghp_" not in event["details"] for event in auth.audit_events())


def test_connection_and_repository_discovery_do_not_call_github_without_a_token(tmp_path, monkeypatch):
    import mcp_server.http_api as http_api
    FakeGitHubClient.calls = []
    monkeypatch.setattr(http_api, "GitHubRESTClient", FakeGitHubClient)
    client, _auth, _credentials, headers = make_client(tmp_path, monkeypatch)
    result = client.post("/api/admin/github/test", headers=headers)
    assert result.status_code == 200
    assert result.json()["error_code"] == "github_token_not_configured"
    assert client.get("/api/admin/github/repositories").status_code == 409
    assert FakeGitHubClient.calls == []


def test_repository_discovery_and_policy_update_use_validated_canonical_policy(tmp_path, monkeypatch):
    import mcp_server.http_api as http_api
    FakeGitHubClient.calls = []
    monkeypatch.setattr(http_api, "GitHubRESTClient", FakeGitHubClient)
    client, _auth, _credentials, headers = make_client(tmp_path, monkeypatch)
    client.put("/api/admin/github/credentials", headers=headers,
               json={"credential_type": "github_token", "value": "fine-grained-token", "persistent": True})
    repositories = client.get("/api/admin/github/repositories").json()["repositories"]
    assert repositories[0]["full_name"] == "acme/service"
    assert repositories[0]["policy"]["allow_source_sharing"] is False
    initial = client.get("/api/admin/github/repositories/acme/service/policy").json()
    update = {"revision": initial["revision"], "allow_source_sharing": True,
              "publish_review_comments": False, "enable_autonomous_fixes": False,
              "allowed_low_risk_categories": ["style"], "confidence_threshold": 0.8,
              "max_changed_lines": 3, "protected_paths": [".github/", "migrations/"],
              "permitted_branches": ["main"],
              "required_checks": ["syntax", "static", "behavioral", "ci"],
              "required_github_checks": ["build"]}
    saved = client.put("/api/admin/github/repositories/acme/service/policy", headers=headers, json=update)
    assert saved.status_code == 200 and saved.json()["policy"]["allow_source_sharing"] is True
    assert saved.json()["autonomous_commits"] == "unavailable_disabled_hard_gate"
    assert client.get("/api/admin/github/repositories/acme/service/policy").json()["policy"]["publish_review_comments"] is False
    update["revision"] = initial["revision"]
    conflict = client.put("/api/admin/github/repositories/acme/service/policy", headers=headers, json=update)
    assert conflict.status_code == 409
    update["revision"] = saved.json()["revision"]
    update["max_changed_lines"] = 99
    invalid = client.put("/api/admin/github/repositories/acme/service/policy", headers=headers, json=update)
    assert invalid.status_code == 422
    policy = json.loads((tmp_path / "policy.json").read_text())
    assert policy["version"] == "github-action-policy-v1"
    assert policy["repositories"]["acme/service"]["protected_paths"][:5] == [".github/", "migrations/", "infra/", "security/", "auth/"]


def test_webhook_configuration_does_not_claim_external_reachability(tmp_path, monkeypatch):
    client, _auth, _credentials, headers = make_client(tmp_path, monkeypatch)
    config = client.get("/api/admin/github/webhook").json()
    assert config["endpoint_url"].endswith("/api/github/webhook")
    assert config["public_reachability"] == "localhost_only"
    assert config["secret_configured"] is False
    assert config["last_authenticated_delivery"] is None
    saved = client.put("/api/admin/github/credentials", headers=headers,
                       json={"credential_type": "webhook_secret", "value": "secret-webhook-value",
                             "persistent": False})
    assert saved.status_code == 200 and "secret-webhook-value" not in saved.text
    updated = client.get("/api/admin/github/webhook").json()
    assert updated["secret_configured"] is True
    assert updated["public_reachability"] == "localhost_only"
    assert client.delete("/api/admin/github/credentials/webhook_secret", headers=headers).status_code == 200

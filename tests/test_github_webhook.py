"""Offline tests for signed, idempotent, read-only PR webhook intake."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from mcp_server.github_webhook import (
    GitHubWebhookProcessor,
    PullRequestDelivery,
    WebhookDeliveryStore,
    WebhookError,
    parse_delivery,
    verify_signature,
)
from mcp_server.http_api import create_app
from mcp_server.credential_store import CredentialStore


SECRET = "offline-webhook-test-secret"
STATUS_TOKEN = "offline-status-token"
_ACTIVE_PROCESSORS = []


class OfflineKeychain:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


@pytest.fixture(autouse=True)
def stop_test_workers():
    yield
    while _ACTIVE_PROCESSORS:
        _ACTIVE_PROCESSORS.pop().stop()
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def payload(*, action="opened", draft=False, head_sha=HEAD_SHA):
    return {
        "action": action,
        "repository": {"name": "demo", "full_name": "acme/demo", "owner": {"login": "acme"}},
        "pull_request": {
            "number": 7, "draft": draft,
            "base": {"sha": BASE_SHA}, "head": {"sha": head_sha},
        },
    }


def raw_payload(**kwargs):
    return json.dumps(payload(**kwargs), separators=(",", ":")).encode()


def signed_headers(body, *, delivery_id=None, event="pull_request", signature_body=None):
    signed = body if signature_body is None else signature_body
    signature = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": delivery_id or str(uuid.uuid4()),
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }


class FakeLifecycle:
    def __init__(self):
        self.calls = []
        self.provider_initialized = False

    def configured_provider_name(self, task="review"):
        return "offline-fake"

    def review_github_pr(self, url, **expected):
        self.calls.append((url, expected))
        return {
            "repository": "acme/demo", "number": 7,
            "base_sha": expected["expected_base_sha"],
            "head_sha": expected["expected_head_sha"],
            "completion_status": "complete", "files_reviewed": 1,
            "files": [], "stale": False,
        }


def make_test_client(tmp_path, monkeypatch, *, lifecycle=None, max_body="1048576"):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_WEBHOOK_STATUS_TOKEN", STATUS_TOKEN)
    monkeypatch.setenv("GITHUB_WEBHOOK_MAX_BODY_BYTES", max_body)
    store = WebhookDeliveryStore(tmp_path / "deliveries.sqlite3", max_in_flight=8)
    review_lifecycle = lifecycle or FakeLifecycle()
    processor = GitHubWebhookProcessor(
        store,
        lambda event: review_lifecycle.review_github_pr(
            f"https://github.com/{event['owner']}/{event['repository']}/pull/{event['number']}",
            expected_owner=event["owner"], expected_repository=event["repository"],
            expected_number=event["number"], expected_base_sha=event["base_sha"],
            expected_head_sha=event["head_sha"],
        ),
        poll_seconds=0.05,
    )
    _ACTIVE_PROCESSORS.append(processor)
    client = TestClient(create_app(review_lifecycle, webhook_processor=processor,
                                   credentials=CredentialStore(keychain=OfflineKeychain())))
    return client, processor, review_lifecycle


def wait_delivery(client, delivery_id, expected, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/github/webhook/deliveries/{delivery_id}",
            headers={"Authorization": f"Bearer {STATUS_TOKEN}"},
        )
        assert response.status_code == 200
        if response.json()["status"] in expected:
            return response.json()
        time.sleep(0.02)
    raise AssertionError(f"delivery did not reach {expected}")


def test_valid_signature_authenticates_exact_raw_body_and_parses_delivery():
    body = raw_payload()
    headers = signed_headers(body)
    assert verify_signature(body, headers["X-Hub-Signature-256"], SECRET)
    delivery = parse_delivery(body, delivery_id=headers["X-GitHub-Delivery"], event="pull_request")
    assert delivery.action == "opened"
    assert delivery.repository_full_name == "acme/demo"
    assert delivery.base_sha == BASE_SHA and delivery.head_sha == HEAD_SHA
    assert not verify_signature(body + b" ", headers["X-Hub-Signature-256"], SECRET)


@pytest.mark.parametrize("signature", [None, "sha256=bad", "sha1=" + "0" * 40])
def test_invalid_or_missing_signature_never_processes_event(tmp_path, monkeypatch, signature):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    body = raw_payload()
    headers = signed_headers(body)
    if signature is None:
        headers.pop("X-Hub-Signature-256")
    else:
        headers["X-Hub-Signature-256"] = signature
    response = client.post("/api/github/webhook", content=body, headers=headers)
    assert response.status_code == 401
    assert lifecycle.calls == []


def test_missing_webhook_secret_fails_closed_without_state_or_review(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    lifecycle = FakeLifecycle()
    client = TestClient(create_app(lifecycle))
    body = raw_payload()
    response = client.post("/api/github/webhook", content=body, headers=signed_headers(body))
    assert response.status_code == 503
    assert lifecycle.calls == []


def test_modified_body_is_rejected_before_json_or_review_processing(tmp_path, monkeypatch):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    original = raw_payload()
    headers = signed_headers(original)
    response = client.post("/api/github/webhook", content=original + b" ", headers=headers)
    assert response.status_code == 401
    assert lifecycle.calls == []


def test_oversized_webhook_body_is_rejected_before_signature_or_processing(tmp_path, monkeypatch):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch, max_body="1024")
    body = b"{" + b" " * 2048 + b"}"
    response = client.post("/api/github/webhook", content=body, headers=signed_headers(body))
    assert response.status_code == 413
    assert lifecycle.calls == []


@pytest.mark.parametrize(
    ("event", "action", "body", "status"),
    [
        ("issues", "opened", payload(), 422),
        ("pull_request", "edited", payload(action="edited"), 422),
        ("pull_request", "opened", b"not-json", 400),
        ("pull_request", "opened", {"action": "opened"}, 400),
    ],
)
def test_unsupported_or_malformed_events_are_rejected_offline(tmp_path, monkeypatch, event, action, body, status):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    response = client.post("/api/github/webhook", content=content,
                           headers=signed_headers(content, event=event))
    assert response.status_code == status
    assert lifecycle.calls == []


def test_supported_event_runs_existing_review_callback_and_exposes_private_status(tmp_path, monkeypatch):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    body = raw_payload()
    delivery_id = str(uuid.uuid4())
    response = client.post("/api/github/webhook", content=body,
                           headers=signed_headers(body, delivery_id=delivery_id))
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert wait_delivery(client, delivery_id, {"completed"})["result"]["processed_head_sha"] == HEAD_SHA
    assert len(lifecycle.calls) == 1
    url, expected = lifecycle.calls[0]
    assert url == "https://github.com/acme/demo/pull/7"
    assert expected["expected_head_sha"] == HEAD_SHA


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_each_supported_action_is_accepted_once_when_pr_is_not_draft(tmp_path, monkeypatch, action):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    body = raw_payload(action=action)
    response = client.post("/api/github/webhook", content=body, headers=signed_headers(body))
    assert response.status_code == 202
    result = wait_delivery(client, response.json()["delivery_id"], {"completed"})["result"]
    assert result["event_action"] == action
    assert len(lifecycle.calls) == 1


def test_duplicate_delivery_is_not_processed_twice_and_status_is_authenticated(tmp_path, monkeypatch):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    body = raw_payload()
    delivery_id = str(uuid.uuid4())
    headers = signed_headers(body, delivery_id=delivery_id)
    first = client.post("/api/github/webhook", content=body, headers=headers)
    second = client.post("/api/github/webhook", content=body, headers=headers)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["duplicate"] is True
    assert wait_delivery(client, delivery_id, {"completed"})["status"] == "completed"
    assert len(lifecycle.calls) == 1
    assert client.get(f"/api/github/webhook/deliveries/{delivery_id}").status_code == 401


def test_action_status_endpoint_is_authenticated_and_returns_only_action_records(tmp_path, monkeypatch):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    body = raw_payload()
    delivery_id = str(uuid.uuid4())
    response = client.post("/api/github/webhook", content=body,
                           headers=signed_headers(body, delivery_id=delivery_id))
    assert response.status_code == 202
    wait_delivery(client, delivery_id, {"completed"})
    denied = client.get(f"/api/github/webhook/deliveries/{delivery_id}/actions")
    assert denied.status_code == 401
    actions = client.get(
        f"/api/github/webhook/deliveries/{delivery_id}/actions",
        headers={"Authorization": f"Bearer {STATUS_TOKEN}"},
    )
    assert actions.status_code == 200
    assert actions.json()["delivery_id"] == delivery_id
    assert actions.json()["actions"] == []
    assert "result" not in actions.json()
    assert lifecycle.calls


def test_delivery_id_with_changed_body_is_rejected_and_original_job_is_preserved(tmp_path, monkeypatch):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    delivery_id = str(uuid.uuid4())
    original = raw_payload()
    first = client.post("/api/github/webhook", content=original,
                        headers=signed_headers(original, delivery_id=delivery_id))
    changed = raw_payload(action="synchronize")
    conflict = client.post("/api/github/webhook", content=changed,
                           headers=signed_headers(changed, delivery_id=delivery_id))
    assert first.status_code == 202
    assert conflict.status_code == 409
    assert wait_delivery(client, delivery_id, {"completed"})["result"]["event_action"] == "opened"
    assert len(lifecycle.calls) == 1


def test_concurrent_duplicate_reservation_has_single_winner(tmp_path):
    body = raw_payload()
    delivery_id = str(uuid.uuid4())
    delivery = parse_delivery(body, delivery_id=delivery_id, event="pull_request")
    processor = GitHubWebhookProcessor(WebhookDeliveryStore(tmp_path / "concurrent.sqlite3"), lambda _event: {})
    barrier = threading.Barrier(8)

    def submit():
        barrier.wait()
        return processor.accept(delivery)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        dispositions = list(pool.map(lambda _i: submit(), range(8)))
    assert dispositions.count("accepted") == 1
    assert dispositions.count("duplicate") == 7


def test_draft_open_event_is_recorded_but_waits_for_ready_for_review(tmp_path, monkeypatch):
    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch)
    body = raw_payload(draft=True)
    delivery_id = str(uuid.uuid4())
    response = client.post("/api/github/webhook", content=body,
                           headers=signed_headers(body, delivery_id=delivery_id))
    assert response.status_code == 202
    row = wait_delivery(client, delivery_id, {"skipped"})
    assert row["failure_code"] == "draft_pr_waiting_for_ready_for_review"
    assert lifecycle.calls == []


def test_failed_provider_review_is_recorded_without_leaking_exception_text(tmp_path, monkeypatch):
    class FailingLifecycle(FakeLifecycle):
        def review_github_pr(self, url, **expected):
            self.calls.append((url, expected))
            raise RuntimeError("private source and token should not be persisted")

    client, _processor, lifecycle = make_test_client(tmp_path, monkeypatch, lifecycle=FailingLifecycle())
    body = raw_payload()
    delivery_id = str(uuid.uuid4())
    response = client.post("/api/github/webhook", content=body,
                           headers=signed_headers(body, delivery_id=delivery_id))
    assert response.status_code == 202
    row = wait_delivery(client, delivery_id, {"failed"})
    assert row["failure_code"] == "review_failed_RuntimeError"
    assert "private source" not in json.dumps(row)
    retry = client.post("/api/github/webhook", content=body,
                        headers=signed_headers(body, delivery_id=delivery_id))
    assert retry.status_code == 202 and retry.json()["duplicate"] is True
    assert len(lifecycle.calls) == 1


def test_running_delivery_after_restart_fails_closed_instead_of_being_retried(tmp_path):
    body = raw_payload()
    delivery = parse_delivery(body, delivery_id=str(uuid.uuid4()), event="pull_request")
    first = WebhookDeliveryStore(tmp_path / "restart.sqlite3")
    first.reserve(delivery)
    assert first.claim_next()["status"] == "running"
    restarted = WebhookDeliveryStore(tmp_path / "restart.sqlite3")
    restarted.recover_after_restart()
    disposition, record = restarted.reserve(delivery)
    assert disposition == "duplicate"
    assert record["status"] == "failed"
    assert record["failure_code"] == "worker_interrupted_unknown_completion"


def test_stale_and_partial_completion_statuses_are_preserved(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "results.sqlite3")
    delivery = parse_delivery(raw_payload(), delivery_id=str(uuid.uuid4()), event="pull_request")
    store.reserve(delivery)
    store.complete(delivery.delivery_id, "stale", {"completion_status": "stale"})
    assert store.get(delivery.delivery_id)["status"] == "stale"
    delivery2 = parse_delivery(raw_payload(action="synchronize"), delivery_id=str(uuid.uuid4()), event="pull_request")
    store.reserve(delivery2)
    store.complete(delivery2.delivery_id, "partial", {"completion_status": "partial"})
    assert store.get(delivery2.delivery_id)["status"] == "partial"


def test_repeated_webhook_store_reads_do_not_accumulate_file_descriptors(tmp_path):
    fd_directory = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
    if not os.path.isdir(fd_directory):
        pytest.skip("platform does not expose an inspectable process file-descriptor directory")

    store = WebhookDeliveryStore(tmp_path / "descriptor-regression.sqlite3")
    delivery = parse_delivery(raw_payload(), delivery_id=str(uuid.uuid4()), event="pull_request")
    store.reserve(delivery)
    store.complete(delivery.delivery_id, "completed", {"completion_status": "complete"})
    before = len(os.listdir(fd_directory))

    for _ in range(250):
        assert store.claim_next() is None
        assert store.get(delivery.delivery_id)["status"] == "completed"
        assert store.get_actions(delivery.delivery_id) == []
        assert len(store.list_recent(limit=10)) == 1

    after = len(os.listdir(fd_directory))
    assert after <= before + 2


def test_delivery_payload_validation_blocks_urls_bad_paths_and_invalid_sha():
    bad = payload()
    bad["repository"]["full_name"] = "evil.test/acme/demo"
    with pytest.raises(WebhookError, match="invalid_pull_request_identity"):
        parse_delivery(json.dumps(bad).encode(), delivery_id=str(uuid.uuid4()), event="pull_request")
    bad = payload(head_sha="not-a-sha")
    with pytest.raises(WebhookError, match="invalid_pull_request_identity"):
        parse_delivery(json.dumps(bad).encode(), delivery_id=str(uuid.uuid4()), event="pull_request")

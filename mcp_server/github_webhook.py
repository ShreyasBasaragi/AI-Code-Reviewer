"""Authenticated, bounded and read-only GitHub pull-request webhook intake."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from mcp_server.github_pr import GitHubPRException

logger = logging.getLogger(__name__)

SUPPORTED_EVENT = "pull_request"
SUPPORTED_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SIGNATURE = re.compile(r"^sha256=[0-9a-fA-F]{64}$")


class WebhookError(Exception):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class PullRequestDelivery:
    delivery_id: str
    payload_sha256: str
    event: str
    action: str
    owner: str
    repository: str
    number: int
    base_sha: str
    head_sha: str
    draft: bool
    created_at: float

    @property
    def repository_full_name(self) -> str:
        return f"{self.owner}/{self.repository}"

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def verify_signature(raw_body: bytes, signature: Optional[str], secret: Optional[str]) -> bool:
    """Authenticate the exact raw request bytes before decoding or parsing."""
    if not isinstance(secret, str) or not secret:
        raise WebhookError("webhook_secret_not_configured", 503)
    if not isinstance(raw_body, bytes) or not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_delivery(raw_body: bytes, *, delivery_id: str, event: str,
                   now: Optional[float] = None) -> PullRequestDelivery:
    try:
        normalized_delivery = str(uuid.UUID(delivery_id))
        if normalized_delivery.casefold() != delivery_id.casefold():
            raise ValueError("noncanonical UUID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise WebhookError("invalid_delivery_id", 400) from exc
    if event != SUPPORTED_EVENT:
        raise WebhookError("unsupported_event", 422)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError("malformed_webhook_json", 400) from exc
    if not isinstance(payload, dict):
        raise WebhookError("malformed_webhook_payload", 400)
    action = payload.get("action")
    if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
        raise WebhookError("unsupported_pull_request_action", 422)
    pull = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull, dict) or not isinstance(repository, dict):
        raise WebhookError("malformed_webhook_payload", 400)
    owner_data = repository.get("owner")
    owner = owner_data.get("login") if isinstance(owner_data, dict) else None
    repo_name = repository.get("name")
    full_name = repository.get("full_name")
    number = pull.get("number")
    base = pull.get("base")
    head = pull.get("head")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    draft = pull.get("draft", False)
    if (
        not isinstance(owner, str) or not _safe_repo_part(owner)
        or not isinstance(repo_name, str) or not _safe_repo_part(repo_name)
        or (full_name is not None and (not isinstance(full_name, str) or full_name.casefold() != f"{owner}/{repo_name}".casefold()))
        or not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 2**31 - 1
        or not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha)
        or not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha)
        or not isinstance(draft, bool)
    ):
        raise WebhookError("invalid_pull_request_identity", 400)
    return PullRequestDelivery(
        delivery_id=normalized_delivery,
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        event=event,
        action=action,
        owner=owner,
        repository=repo_name,
        number=number,
        base_sha=base_sha,
        head_sha=head_sha,
        draft=draft,
        created_at=time.time() if now is None else now,
    )


def _safe_repo_part(value: str) -> bool:
    return bool(
        _REPO_PART.fullmatch(value)
        and value not in {".", ".."}
        and not value.casefold().endswith(".git")
    )


class WebhookDeliveryStore:
    """SQLite delivery ledger with bounded in-flight work and retention."""

    def __init__(self, path: Path | str, *, max_in_flight: int = 32,
                 retention_seconds: int = 30 * 24 * 60 * 60,
                 max_records: int = 1000) -> None:
        self.path = Path(path).expanduser()
        self.max_in_flight = max(1, int(max_in_flight))
        self.retention_seconds = max(3600, int(retention_seconds))
        self.max_records = max(100, int(max_records))
        self._lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> "WebhookDeliveryStore":
        try:
            max_in_flight = min(max(int(os.getenv("GITHUB_WEBHOOK_MAX_IN_FLIGHT", "32")), 1), 256)
        except ValueError:
            max_in_flight = 32
        try:
            retention_days = min(max(int(os.getenv("GITHUB_WEBHOOK_RETENTION_DAYS", "30")), 1), 365)
        except ValueError:
            retention_days = 30
        default_path = Path.home() / ".local" / "state" / "ai-code-reviewer" / "github_webhook_deliveries.sqlite3"
        path = Path(os.getenv("AI_CODE_REVIEWER_WEBHOOK_DB_PATH", str(default_path)))
        return cls(path, max_in_flight=max_in_flight, retention_seconds=retention_days * 86400)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self) -> None:
        with self._lock, self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS github_webhook_deliveries (
                delivery_id TEXT PRIMARY KEY,
                payload_sha256 TEXT NOT NULL,
                event TEXT NOT NULL,
                action TEXT NOT NULL,
                owner TEXT NOT NULL,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                base_sha TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                draft INTEGER NOT NULL,
                status TEXT NOT NULL,
                failure_code TEXT,
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_webhook_pending ON github_webhook_deliveries(status, created_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS github_action_records (
                action_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                base_sha TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                finding_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                candidate_id TEXT,
                verification_status TEXT NOT NULL,
                publication_status TEXT NOT NULL,
                github_comment_id INTEGER,
                commit_sha TEXT,
                failure_code TEXT,
                lifecycle_state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL,
                operation_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(delivery_id, finding_id, action_type)
            )""")
            self._ensure_column(db, "github_action_records", "lifecycle_state", "TEXT NOT NULL DEFAULT 'pending'")
            self._ensure_column(db, "github_action_records", "attempts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "github_action_records", "next_retry_at", "REAL")
            self._ensure_column(db, "github_action_records", "operation_id", "TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_github_actions_delivery ON github_action_records(delivery_id, created_at)")

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        names = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in names:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def recover_after_restart(self) -> None:
        """Resume never-started pending rows; fail ambiguous running work closed."""
        self.initialize()
        now = time.time()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE github_webhook_deliveries SET status='failed', failure_code='worker_interrupted_unknown_completion', updated_at=? WHERE status='running'",
                (now,),
            )
            db.execute(
                "UPDATE github_action_records SET publication_status='requires_reconciliation', lifecycle_state='requires_reconciliation', failure_code='worker_interrupted_unknown_completion', updated_at=? WHERE publication_status='running'",
                (now,),
            )
            db.execute("DELETE FROM github_webhook_deliveries WHERE created_at < ? AND status NOT IN ('pending','running')",
                       (now - self.retention_seconds,))
            db.execute("DELETE FROM github_action_records WHERE created_at < ? AND publication_status NOT IN ('pending','running')",
                       (now - self.retention_seconds,))
            db.execute("""DELETE FROM github_action_records WHERE action_id IN (
                SELECT action_id FROM github_action_records
                WHERE publication_status NOT IN ('pending','running') ORDER BY created_at ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM github_action_records) - ?)
            )""", (self.max_records * 20,))
            db.execute("COMMIT")

    def reserve(self, delivery: PullRequestDelivery) -> tuple[str, dict[str, Any]]:
        self.initialize()
        now = time.time()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM github_webhook_deliveries WHERE created_at < ? AND status NOT IN ('pending','running')",
                       (now - self.retention_seconds,))
            db.execute("DELETE FROM github_action_records WHERE created_at < ? AND publication_status NOT IN ('pending','running')",
                       (now - self.retention_seconds,))
            existing = db.execute(
                "SELECT * FROM github_webhook_deliveries WHERE delivery_id=?", (delivery.delivery_id,)
            ).fetchone()
            if existing:
                db.execute("COMMIT")
                if existing["payload_sha256"] != delivery.payload_sha256:
                    raise WebhookError("delivery_id_payload_conflict", 409)
                return "duplicate", dict(existing)
            active = db.execute(
                "SELECT COUNT(*) FROM github_webhook_deliveries WHERE status IN ('pending','running')"
            ).fetchone()[0]
            if active >= self.max_in_flight:
                db.execute("ROLLBACK")
                raise WebhookError("webhook_capacity_reached", 503)
            # Retain a bounded audit window while preserving active jobs.
            db.execute("""DELETE FROM github_webhook_deliveries WHERE delivery_id IN (
                SELECT delivery_id FROM github_webhook_deliveries
                WHERE status NOT IN ('pending','running') ORDER BY created_at ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM github_webhook_deliveries) - ? + 1)
            )""", (self.max_records,))
            initial_status = "skipped" if delivery.draft else "pending"
            initial_failure = "draft_pr_waiting_for_ready_for_review" if delivery.draft else None
            db.execute("""INSERT INTO github_webhook_deliveries
                (delivery_id,payload_sha256,event,action,owner,repository,pr_number,base_sha,head_sha,draft,status,failure_code,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (delivery.delivery_id, delivery.payload_sha256, delivery.event, delivery.action,
                 delivery.owner, delivery.repository, delivery.number, delivery.base_sha,
                 delivery.head_sha, int(delivery.draft), initial_status, initial_failure, now, now),
            )
            db.execute("COMMIT")
            return "accepted", {**delivery.to_record(), "status": initial_status,
                                 "failure_code": initial_failure, "result_json": None}

    def mark_skipped(self, delivery_id: str, code: str) -> None:
        self._set_status(delivery_id, "skipped", failure_code=code)

    def claim_next(self) -> Optional[dict[str, Any]]:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM github_webhook_deliveries WHERE status='pending' ORDER BY created_at, delivery_id LIMIT 1"
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            updated = db.execute(
                "UPDATE github_webhook_deliveries SET status='running', updated_at=? WHERE delivery_id=? AND status='pending'",
                (time.time(), row["delivery_id"]),
            ).rowcount
            db.execute("COMMIT")
            if not updated:
                return None
            result = dict(row)
            result["status"] = "running"
            return result

    def complete(self, delivery_id: str, status: str, result: dict[str, Any]) -> None:
        if status not in {"completed", "partial", "failed", "stale"}:
            raise ValueError("unsupported terminal webhook status")
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("webhook_result_exceeds_storage_limit")
        self._set_status(delivery_id, status, result_json=payload)

    def fail(self, delivery_id: str, code: str) -> None:
        safe_code = code if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", code or "") else "webhook_processing_failed"
        self._set_status(delivery_id, "failed", failure_code=safe_code)

    def _set_status(self, delivery_id: str, status: str, *, failure_code: Optional[str] = None,
                    result_json: Optional[str] = None) -> None:
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE github_webhook_deliveries SET status=?, failure_code=?, result_json=?, updated_at=? WHERE delivery_id=?",
                (status, failure_code, result_json, time.time(), delivery_id),
            )

    def get(self, delivery_id: str) -> Optional[dict[str, Any]]:
        try:
            normalized = str(uuid.UUID(delivery_id))
        except (ValueError, TypeError, AttributeError):
            return None
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM github_webhook_deliveries WHERE delivery_id=?", (normalized,)).fetchone()
            if row is None:
                return None
            record = dict(row)
        result = None
        if record.get("result_json"):
            try:
                result = json.loads(record["result_json"])
            except json.JSONDecodeError:
                result = None
        record.pop("payload_sha256", None)
        record.pop("result_json", None)
        record["draft"] = bool(record["draft"])
        record["result"] = result
        record["actions"] = self.get_actions(normalized)
        return record

    def record_action(self, *, delivery_id: str, repository: str, pr_number: int,
                      base_sha: str, head_sha: str, finding_id: str, action_type: str,
                      policy_decision: dict[str, Any], publication_status: str,
                      candidate_id: Optional[str] = None, verification_status: str = "not_run",
                      failure_code: Optional[str] = None) -> tuple[bool, str]:
        """Insert one durable action decision; unique key prevents duplicate writes."""
        self.initialize()
        action_id = hashlib.sha256(f"{delivery_id}\0{finding_id}\0{action_type}".encode()).hexdigest()[:32]
        policy_json = json.dumps(policy_decision, ensure_ascii=False, separators=(",", ":"))
        if len(policy_json.encode("utf-8")) > 64 * 1024:
            policy_json = json.dumps({"status": "DENIED", "denial_reasons": ["policy_record_too_large"]})
            publication_status = "denied"
            failure_code = "policy_record_too_large"
        now = time.time()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute("""INSERT OR IGNORE INTO github_action_records
                (action_id,delivery_id,repository,pr_number,base_sha,head_sha,finding_id,action_type,
                 policy_json,candidate_id,verification_status,publication_status,failure_code,lifecycle_state,operation_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (action_id, delivery_id, repository, pr_number, base_sha, head_sha, finding_id,
                 action_type, policy_json, candidate_id, verification_status, publication_status,
                 failure_code, _lifecycle_for(publication_status), action_id, now, now)).rowcount
            row = db.execute("SELECT action_id FROM github_action_records WHERE delivery_id=? AND finding_id=? AND action_type=?",
                             (delivery_id, finding_id, action_type)).fetchone()
            db.execute("""DELETE FROM github_action_records WHERE action_id IN (
                SELECT action_id FROM github_action_records
                WHERE publication_status NOT IN ('pending','running') ORDER BY created_at ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM github_action_records) - ?)
            )""", (self.max_records * 20,))
            db.execute("COMMIT")
            return bool(inserted), row["action_id"]

    def claim_action(self, action_id: str) -> bool:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            updated = db.execute("UPDATE github_action_records SET publication_status='running',lifecycle_state='publishing',attempts=attempts+1,updated_at=? "
                                 "WHERE action_id=? AND publication_status='pending' AND (next_retry_at IS NULL OR next_retry_at<=?)",
                                 (now, action_id, now)).rowcount
            db.execute("COMMIT")
            return bool(updated)

    def finish_action(self, action_id: str, status: str, *, github_comment_id: Optional[int] = None,
                      commit_sha: Optional[str] = None, failure_code: Optional[str] = None,
                      verification_status: Optional[str] = None) -> None:
        allowed = {"published", "failed", "unknown", "skipped", "stale", "pending_required_verification", "denied", "AUTONOMOUS_COMMIT_DISABLED", "requires_reconciliation", "blocked"}
        if status not in allowed:
            raise ValueError("unsupported GitHub action status")
        safe_code = failure_code if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", failure_code or "") else None
        with self._lock, self._connection() as db:
            db.execute("""UPDATE github_action_records SET publication_status=?,lifecycle_state=?,github_comment_id=?,commit_sha=?,
                failure_code=?,verification_status=COALESCE(?,verification_status),updated_at=? WHERE action_id=?""",
                (status, _lifecycle_for(status), github_comment_id, commit_sha, safe_code,
                 verification_status, time.time(), action_id))

    def schedule_action_retry(self, action_id: str, *, retryable: bool,
                              max_attempts: int = 3) -> tuple[bool, Optional[float]]:
        """Schedule bounded retries only for known failures; ambiguity never retries."""
        if not retryable:
            return False, None
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT publication_status,attempts FROM github_action_records WHERE action_id=?",
                             (action_id,)).fetchone()
            if (row is None or row["publication_status"] != "failed"
                    or row["attempts"] >= min(max_attempts, 3)):
                db.execute("COMMIT")
                return False, None
            delay = min(2 ** max(row["attempts"] - 1, 0), 60)
            run_at = time.time() + delay
            db.execute("UPDATE github_action_records SET publication_status='pending',lifecycle_state='pending',next_retry_at=?,updated_at=? WHERE action_id=?",
                       (run_at, time.time(), action_id))
            db.execute("COMMIT")
            return True, run_at

    def mark_reconciled(self, action_id: str, *, status: str,
                        github_comment_id: Optional[int] = None,
                        failure_code: Optional[str] = "reconciled_remote_state") -> None:
        if status not in {"published", "failed", "stale"}:
            raise ValueError("reconciliation_status_invalid")
        self.finish_action(action_id, status, github_comment_id=github_comment_id,
                           failure_code=failure_code)

    def transition_action(self, action_id: str, target_state: str, *,
                          verification_status: Optional[str] = None,
                          failure_code: Optional[str] = None) -> bool:
        """Apply one allowed durable lifecycle transition atomically."""
        transitions = {
            "pending": {"preparing", "awaiting_verification", "publishing", "blocked"},
            "preparing": {"awaiting_verification", "verified", "blocked", "failed"},
            "awaiting_verification": {"verified", "blocked", "failed"},
            "verified": {"publishing", "completed", "blocked"},
            "publishing": {"completed", "failed", "requires_reconciliation"},
            "failed": {"pending", "blocked"},
            "requires_reconciliation": {"completed", "failed", "blocked"},
            "blocked": set(),
            "completed": set(),
        }
        if target_state not in transitions:
            raise ValueError("action_lifecycle_state_invalid")
        safe_code = failure_code if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", failure_code or "") else None
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT lifecycle_state FROM github_action_records WHERE action_id=?",
                             (action_id,)).fetchone()
            if row is None or target_state not in transitions.get(row["lifecycle_state"], set()):
                db.execute("COMMIT")
                return False
            publication = {
                "pending": "pending", "preparing": "preparing",
                "awaiting_verification": "pending_required_verification",
                "verified": "verified", "publishing": "running",
                "completed": "published", "failed": "failed",
                "requires_reconciliation": "requires_reconciliation", "blocked": "blocked",
            }[target_state]
            db.execute("UPDATE github_action_records SET lifecycle_state=?,publication_status=?,"
                       "verification_status=COALESCE(?,verification_status),failure_code=?,updated_at=? WHERE action_id=?",
                       (target_state, publication, verification_status, safe_code, time.time(), action_id))
            db.execute("COMMIT")
            return True

    def get_actions(self, delivery_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT * FROM github_action_records WHERE delivery_id=? ORDER BY created_at, action_id",
                              (delivery_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["policy_decision"] = json.loads(item.pop("policy_json"))
            except json.JSONDecodeError:
                item["policy_decision"] = {"status": "unavailable"}
            result.append(item)
        return result

    def list_recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return bounded delivery metadata without source payloads or tokens."""
        with self._lock, self._connection() as db:
            rows = db.execute("""SELECT delivery_id,event,action,owner,owner || '/' || repository AS repository,pr_number,
                base_sha,head_sha,status,failure_code,created_at,updated_at
                FROM github_webhook_deliveries ORDER BY created_at DESC LIMIT ?""",
                (max(1, min(limit, 50)),)).fetchall()
        return [dict(row) for row in rows]


def _lifecycle_for(status: str) -> str:
    if status in {"pending", "pending_required_verification"}:
        return "awaiting_verification" if status == "pending_required_verification" else "pending"
    if status in {"running", "publishing"}:
        return "publishing"
    if status in {"published", "completed", "AUTONOMOUS_COMMIT_DISABLED"}:
        return "completed" if status != "AUTONOMOUS_COMMIT_DISABLED" else "blocked"
    if status in {"unknown", "requires_reconciliation"}:
        return "requires_reconciliation"
    if status in {"failed", "errored"}:
        return "failed"
    if status in {"denied", "blocked", "skipped", "stale"}:
        return "blocked"
    return "pending"


class GitHubWebhookProcessor:
    """One bounded worker drains the durable SQLite queue; no task per request."""

    def __init__(self, store: WebhookDeliveryStore,
                 review: Callable[[dict[str, Any]], Any], *, poll_seconds: float = 1.0) -> None:
        self.store = store
        self.review = review
        self.poll_seconds = min(max(float(poll_seconds), 0.05), 5.0)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._submission_lock = threading.RLock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.recover_after_restart()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="github-pr-webhook-worker", daemon=True)
        self._thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        if not thread or not thread.is_alive():
            self._thread = None

    def accept(self, delivery: PullRequestDelivery) -> tuple[str, dict[str, Any]]:
        with self._submission_lock:
            disposition, record = self.store.reserve(delivery)
            if disposition == "accepted":
                if not delivery.draft:
                    self._wake.set()
            return disposition, record

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
            while not self._stop.is_set():
                record = self.store.claim_next()
                if record is None:
                    break
                self._process(record)

    def _process(self, row: dict[str, Any]) -> None:
        started = time.perf_counter()
        delivery_id = row["delivery_id"]
        event = {
            "delivery_id": delivery_id,
            "event": row["event"],
            "action": row["action"],
            "owner": row["owner"],
            "repository": row["repository"],
            "number": row["pr_number"],
            "base_sha": row["base_sha"],
            "head_sha": row["head_sha"],
            "draft": bool(row["draft"]),
        }
        try:
            result_obj = self.review(event)
            result = result_obj.to_dict() if callable(getattr(result_obj, "to_dict", None)) else result_obj
            if not isinstance(result, dict):
                raise TypeError("review_result_invalid")
            result.update({
                "delivery_id": delivery_id,
                "event_action": row["action"],
                "provider_identity": str(result.get("provider_identity") or "configured_provider"),
                "processed_head_sha": row["head_sha"],
                "processed_base_sha": row["base_sha"],
                "processing_duration_seconds": max(0.0, time.perf_counter() - started),
            })
            for file_result in result.get("files", []):
                if isinstance(file_result, dict):
                    for evidence in file_result.get("evidence", []):
                        if isinstance(evidence, dict):
                            evidence["text"] = ""
            completion = result.get("completion_status")
            status = (
                "stale" if result.get("stale") or completion == "stale"
                else "failed" if completion == "failed"
                else "partial" if completion == "partial"
                else "completed"
            )
            self.store.complete(delivery_id, status, result)
        except Exception as exc:
            if isinstance(exc, GitHubPRException):
                code = exc.code
            else:
                code = f"review_failed_{type(exc).__name__}"
            if code == "stale_pr_revision":
                self.store.complete(delivery_id, "stale", {
                    "delivery_id": delivery_id,
                    "repository": row["owner"] + "/" + row["repository"],
                    "number": row["pr_number"],
                    "event_action": row["action"],
                    "processed_base_sha": row["base_sha"],
                    "processed_head_sha": row["head_sha"],
                    "completion_status": "stale",
                    "stale": True,
                    "stale_reason": code,
                    "processing_duration_seconds": max(0.0, time.perf_counter() - started),
                })
            else:
                self.store.fail(delivery_id, code)
            logger.warning("GitHub webhook review failed for delivery %s (%s)", delivery_id, code)


def verify_status_token(authorization: Optional[str], configured_token: Optional[str]) -> bool:
    if not isinstance(configured_token, str) or not configured_token:
        raise WebhookError("webhook_status_token_not_configured", 503)
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:]
    return hmac.compare_digest(supplied, configured_token)

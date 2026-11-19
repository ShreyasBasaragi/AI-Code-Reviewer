"""Owner-only SQLite persistence for bounded review and fix lifecycle records."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 512 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_LIFECYCLE_RECORDS = 128
MAX_AUDIT_RECORDS = 100_000
AUDIT_RETENTION_SECONDS = 365 * 24 * 60 * 60


def lifecycle_db_path() -> Path:
    override = os.getenv("AI_CODE_REVIEWER_LIFECYCLE_DB_PATH")
    if override:
        return Path(override).expanduser()
    state_dir = os.getenv("AI_CODE_REVIEWER_STATE_DIR")
    if state_dir:
        root = Path(state_dir).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "AI Code Reviewer"
    else:
        root = Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ai-code-reviewer"
    return root / "review_lifecycle.sqlite3"


def lifecycle_retention_days() -> int:
    try:
        return min(max(int(os.getenv("AI_CODE_REVIEWER_LIFECYCLE_RETENTION_DAYS", "7")), 1), 30)
    except ValueError:
        return 7


class SQLiteLifecycleStore:
    """Small transactional store; private source is bounded and retained briefly."""

    def __init__(self, path: str | Path, *, retention_days: int = 7,
                 max_records: int = MAX_LIFECYCLE_RECORDS,
                 clock=time.time) -> None:
        self.path = Path(path).expanduser()
        self.retention_seconds = min(max(int(retention_days), 1), 30) * 86400
        self.max_records = min(max(int(max_records), 1), MAX_LIFECYCLE_RECORDS)
        self._clock = clock
        self._lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> "SQLiteLifecycleStore":
        return cls(lifecycle_db_path(), retention_days=lifecycle_retention_days())

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            parent = self.path.parent.lstat()
        except OSError as exc:
            raise RuntimeError("lifecycle_state_directory_unavailable") from exc
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
            raise RuntimeError("lifecycle_state_directory_owner_invalid")
        if parent.st_mode & 0o077:
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError as exc:
                raise RuntimeError("lifecycle_state_directory_permissions_invalid") from exc
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise RuntimeError("lifecycle_state_file_owner_invalid")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("lifecycle_state_file_unavailable") from exc
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

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
            db.execute("BEGIN IMMEDIATE")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                db.execute("ROLLBACK")
                raise RuntimeError("lifecycle_state_schema_newer_than_service")
            db.execute("""CREATE TABLE IF NOT EXISTS lifecycle_reviews (
                review_id TEXT PRIMARY KEY,
                source_text TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                response_json TEXT NOT NULL,
                repository TEXT,
                pr_number INTEGER,
                base_sha TEXT,
                head_sha TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS lifecycle_candidates (
                candidate_id TEXT PRIMARY KEY,
                review_id TEXT,
                finding_ref TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                corrected_sha256 TEXT,
                candidate_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS lifecycle_decisions (
                event_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                review_id TEXT,
                finding_ref TEXT NOT NULL,
                actor_identity TEXT NOT NULL,
                decision TEXT NOT NULL,
                outcome TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                occurred_at REAL NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_review_expiry ON lifecycle_reviews(expires_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_candidate_expiry ON lifecycle_candidates(expires_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_decision_time ON lifecycle_decisions(occurred_at)")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute("COMMIT")

    def save_review(self, review_id: str, source_text: str, response: Mapping[str, Any], *,
                    repository: Optional[str] = None, pr_number: Optional[int] = None,
                    base_sha: Optional[str] = None, head_sha: Optional[str] = None,
                    created_at: Optional[float] = None) -> None:
        self.initialize()
        source_bytes = source_text.encode("utf-8")
        if len(source_bytes) > MAX_SOURCE_BYTES:
            raise ValueError("lifecycle_source_exceeds_storage_limit")
        response_json = _bounded_json(response)
        now = self._clock() if created_at is None else float(created_at)
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO lifecycle_reviews
                (review_id,source_text,source_sha256,response_json,repository,pr_number,base_sha,head_sha,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(review_id) DO UPDATE SET
                source_text=excluded.source_text,source_sha256=excluded.source_sha256,
                response_json=excluded.response_json,repository=excluded.repository,
                pr_number=excluded.pr_number,base_sha=excluded.base_sha,head_sha=excluded.head_sha,
                expires_at=excluded.expires_at""",
                (review_id, source_text, hashlib.sha256(source_bytes).hexdigest(), response_json,
                 repository, pr_number, base_sha, head_sha, now, now + self.retention_seconds))
            self._cleanup(db, now)
            self._trim(db, "lifecycle_reviews", "created_at")
            self._trim(db, "lifecycle_candidates", "created_at")
            db.execute("COMMIT")

    def load_review(self, review_id: str) -> Optional[dict[str, Any]]:
        self.initialize()
        now = self._clock()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM lifecycle_reviews WHERE review_id=? AND expires_at> ?",
                             (review_id, now)).fetchone()
        if row is None:
            return None
        try:
            response = json.loads(row["response_json"])
            if not isinstance(response, dict):
                return None
            source = row["source_text"]
            if hashlib.sha256(source.encode("utf-8")).hexdigest() != row["source_sha256"]:
                return None
            return {**dict(row), "source_text": source, "response": response}
        except (TypeError, UnicodeError, json.JSONDecodeError):
            return None

    def save_candidate(self, candidate: Mapping[str, Any], *, created_at: Optional[float] = None) -> None:
        self.initialize()
        candidate_id = candidate.get("candidate_id")
        original = candidate.get("original_code")
        corrected = candidate.get("corrected_code")
        if not isinstance(candidate_id, str) or not candidate_id or not isinstance(original, str):
            raise ValueError("lifecycle_candidate_invalid")
        if len(original.encode("utf-8")) > MAX_SOURCE_BYTES or (
                isinstance(corrected, str) and len(corrected.encode("utf-8")) > MAX_SOURCE_BYTES):
            raise ValueError("lifecycle_candidate_source_exceeds_storage_limit")
        candidate_data = dict(candidate)
        # Raw provider output can duplicate private source or contain credentials.
        candidate_data["raw_response"] = ""
        candidate_json = _bounded_json(candidate_data)
        now = self._clock() if created_at is None else float(created_at)
        source_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        corrected_hash = hashlib.sha256(corrected.encode("utf-8")).hexdigest() if isinstance(corrected, str) else None
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT created_at FROM lifecycle_candidates WHERE candidate_id=?",
                                  (candidate_id,)).fetchone()
            created = existing["created_at"] if existing else now
            db.execute("""INSERT INTO lifecycle_candidates
                (candidate_id,review_id,finding_ref,source_sha256,corrected_sha256,candidate_json,created_at,updated_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET
                review_id=excluded.review_id,finding_ref=excluded.finding_ref,
                source_sha256=excluded.source_sha256,corrected_sha256=excluded.corrected_sha256,
                candidate_json=excluded.candidate_json,updated_at=excluded.updated_at,
                expires_at=excluded.expires_at""",
                (candidate_id, candidate.get("review_id"), str(candidate.get("finding_ref", "")),
                 source_hash, corrected_hash, candidate_json, created, now,
                 now + self.retention_seconds))
            for event in candidate.get("approval_history", ()):
                if isinstance(event, Mapping):
                    self._save_decision(db, candidate_data, event, source_hash, now)
            self._cleanup(db, now)
            self._trim(db, "lifecycle_reviews", "created_at")
            self._trim(db, "lifecycle_candidates", "created_at")
            db.execute("COMMIT")

    def load_candidate(self, candidate_id: str) -> Optional[dict[str, Any]]:
        self.initialize()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM lifecycle_candidates WHERE candidate_id=? AND expires_at> ?",
                             (candidate_id, self._clock())).fetchone()
        if row is None:
            return None
        try:
            candidate = json.loads(row["candidate_json"])
            if not isinstance(candidate, dict):
                return None
            if (candidate.get("candidate_id") != candidate_id
                    or hashlib.sha256(str(candidate.get("original_code", "")).encode()).hexdigest() != row["source_sha256"]
                    or (candidate.get("corrected_code") is not None and
                        hashlib.sha256(str(candidate["corrected_code"]).encode()).hexdigest() != row["corrected_sha256"])):
                return None
            return candidate
        except (TypeError, UnicodeError, json.JSONDecodeError):
            return None

    def decisions(self, candidate_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT * FROM lifecycle_decisions WHERE candidate_id=? ORDER BY occurred_at,event_id",
                              (candidate_id,)).fetchall()
        return [dict(row) for row in rows]

    def cleanup(self, *, now: Optional[float] = None) -> dict[str, int]:
        current_time = self._clock() if now is None else float(now)
        self.initialize()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            before_reviews = db.execute("SELECT COUNT(*) FROM lifecycle_reviews").fetchone()[0]
            before_candidates = db.execute("SELECT COUNT(*) FROM lifecycle_candidates").fetchone()[0]
            before_events = db.execute("SELECT COUNT(*) FROM lifecycle_decisions").fetchone()[0]
            self._cleanup(db, current_time)
            db.execute("COMMIT")
            return {
                "reviews_removed": before_reviews - db.execute("SELECT COUNT(*) FROM lifecycle_reviews").fetchone()[0],
                "candidates_removed": before_candidates - db.execute("SELECT COUNT(*) FROM lifecycle_candidates").fetchone()[0],
                "audit_events_removed": before_events - db.execute("SELECT COUNT(*) FROM lifecycle_decisions").fetchone()[0],
            }

    def _save_decision(self, db: sqlite3.Connection, candidate: Mapping[str, Any],
                       event: Mapping[str, Any], source_hash: str, now: float) -> None:
        occurred = event.get("timestamp")
        if isinstance(occurred, bool) or not isinstance(occurred, (float, int)):
            occurred = now
        payload = json.dumps(dict(event), sort_keys=True, separators=(",", ":"), default=str)
        event_id = hashlib.sha256((str(candidate.get("candidate_id")) + "\0" + payload).encode()).hexdigest()
        db.execute("""INSERT OR IGNORE INTO lifecycle_decisions
            (event_id,candidate_id,review_id,finding_ref,actor_identity,decision,outcome,
             source_sha256,verification_status,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event_id, candidate.get("candidate_id"), candidate.get("review_id"),
             str(candidate.get("finding_ref", "")), str(event.get("actor_id", "unattributed"))[:128],
             str(event.get("action", "unknown"))[:32], str(event.get("outcome", "unknown"))[:32],
             source_hash, str(event.get("verification_status", "not_checked"))[:64], float(occurred)))

    def _trim(self, db: sqlite3.Connection, table: str, timestamp_column: str) -> None:
        if table == "lifecycle_reviews":
            db.execute("""DELETE FROM lifecycle_reviews WHERE rowid IN (
                SELECT rowid FROM lifecycle_reviews ORDER BY created_at DESC LIMIT -1 OFFSET ?)""",
                (self.max_records,))
            return
        db.execute("""DELETE FROM lifecycle_candidates WHERE candidate_id IN (
            SELECT candidate_id FROM lifecycle_candidates WHERE json_extract(candidate_json,'$.approval_status') IN ('accepted','rejected')
            ORDER BY created_at ASC LIMIT MAX(0,(SELECT COUNT(*) FROM lifecycle_candidates)-?))""",
            (self.max_records,))
        if db.execute("SELECT COUNT(*) FROM lifecycle_candidates").fetchone()[0] > self.max_records:
            raise RuntimeError("lifecycle_active_candidate_capacity_reached")

    def _cleanup(self, db: sqlite3.Connection, now: float) -> None:
        db.execute("DELETE FROM lifecycle_reviews WHERE expires_at<=?", (now,))
        db.execute("DELETE FROM lifecycle_candidates WHERE expires_at<=?", (now,))
        db.execute("DELETE FROM lifecycle_decisions WHERE occurred_at<?", (now - AUDIT_RETENTION_SECONDS,))
        db.execute("""DELETE FROM lifecycle_decisions WHERE rowid IN (
            SELECT rowid FROM lifecycle_decisions ORDER BY occurred_at DESC LIMIT -1 OFFSET ?)""",
            (MAX_AUDIT_RECORDS,))


def _bounded_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise ValueError("lifecycle_record_not_serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("lifecycle_record_exceeds_storage_limit")
    return encoded

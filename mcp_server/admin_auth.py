"""Persistent local administrator authentication using Argon2id and SQLite."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id


SESSION_TTL_SECONDS = 900
BOOTSTRAP_TTL_SECONDS = 30 * 60
RECOVERY_TTL_SECONDS = 10 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURE_LIMIT = 5
ARGON_MEMORY_KIB = 64 * 1024
ARGON_ITERATIONS = 3
ARGON_LANES = 4


class AdminAuthError(ValueError):
    pass


def auth_state_directory() -> Path:
    override = os.getenv("AI_CODE_REVIEWER_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys_platform_is_darwin():
        return Path.home() / "Library" / "Application Support" / "AI Code Reviewer"
    return Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "ai-code-reviewer"


def sys_platform_is_darwin() -> bool:
    import sys
    return sys.platform == "darwin"


def auth_db_path() -> Path:
    return Path(os.getenv("AI_CODE_REVIEWER_AUTH_DB_PATH", str(auth_state_directory() / "admin.sqlite3"))).expanduser()


def _password_hash(value: str, salt: bytes, *, memory_kib: int = ARGON_MEMORY_KIB,
                   iterations: int = ARGON_ITERATIONS, lanes: int = ARGON_LANES) -> bytes:
    return Argon2id(salt=salt, length=32, iterations=iterations,
                   lanes=lanes, memory_cost=memory_kib).derive(value.encode("utf-8"))


def _hash_fields(value: str) -> tuple[bytes, bytes]:
    salt = secrets.token_bytes(16)
    return salt, _password_hash(value, salt)


def _verify(value: str, salt: bytes, expected: bytes, *, memory_kib: int = ARGON_MEMORY_KIB,
            iterations: int = ARGON_ITERATIONS, lanes: int = ARGON_LANES) -> bool:
    try:
        return hmac.compare_digest(_password_hash(value, salt, memory_kib=memory_kib,
                                                  iterations=iterations, lanes=lanes), expected)
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> None:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise AdminAuthError("password_must_be_12_to_1024_characters")
    if "\x00" in password:
        raise AdminAuthError("password_contains_invalid_character")


def _restrict_state(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent = path.parent.stat()
    except OSError as exc:
        raise AdminAuthError("authentication_state_directory_unavailable") from exc
    if (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and parent.st_uid != os.getuid()))))) or not stat.S_ISDIR(parent.st_mode):
        raise AdminAuthError("authentication_state_directory_owner_invalid")
    if parent.st_mode & 0o077:
        try:
            os.chmod(path.parent, 0o700)
        except OSError as exc:
            raise AdminAuthError("authentication_state_directory_permissions_invalid") from exc
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and (hasattr(os, 'getuid') and info.st_uid != os.getuid()))))):
            raise AdminAuthError("authentication_state_file_owner_invalid")
        if hasattr(os, 'fchmod'):
            if hasattr(os, 'fchmod'):
            if hasattr(os, 'fchmod'):
            if hasattr(os, 'fchmod'):
            if hasattr(os, 'fchmod'):
            os.fchmod(descriptor, 0o600)
    except OSError as exc:
        raise AdminAuthError("authentication_state_file_unavailable") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


class AdminAuthStore:
    """Password, single-use bootstrap/recovery tokens, hashed sessions and audit."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else auth_db_path()
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            _restrict_state(self.path)
            with self._connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS admin_auth (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        password_salt BLOB, password_hash BLOB, password_algorithm TEXT,
                        argon_memory_kib INTEGER, argon_iterations INTEGER, argon_lanes INTEGER,
                        bootstrap_salt BLOB, bootstrap_hash BLOB, bootstrap_expires REAL,
                        recovery_salt BLOB, recovery_hash BLOB, recovery_expires REAL,
                        updated_at REAL NOT NULL
                    );
                    INSERT OR IGNORE INTO admin_auth(singleton, updated_at) VALUES(1, 0);
                    CREATE TABLE IF NOT EXISTS admin_sessions (
                        token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL, created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS admin_login_attempts (
                        peer TEXT PRIMARY KEY, window_started REAL NOT NULL, failures INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS admin_audit (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
                        occurred_at REAL NOT NULL, details TEXT NOT NULL DEFAULT '{}'
                    );
                """)
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA secure_delete=ON")
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        return db

    def startup(self) -> Optional[str]:
        """Create a one-time bootstrap hash on first launch and return its clear token once."""
        self.initialize()
        token: Optional[str] = None
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM admin_auth WHERE singleton=1").fetchone()
            if row["password_hash"] is None and (not row["bootstrap_hash"] or row["bootstrap_expires"] <= time.time()):
                token = secrets.token_urlsafe(32)
                salt, digest = _hash_fields(token)
                db.execute("UPDATE admin_auth SET bootstrap_salt=?,bootstrap_hash=?,bootstrap_expires=?,updated_at=? WHERE singleton=1",
                           (salt, digest, time.time() + BOOTSTRAP_TTL_SECONDS, time.time()))
                self._audit(db, "bootstrap_credential_created")
        return token

    def setup_complete(self) -> bool:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT password_hash FROM admin_auth WHERE singleton=1").fetchone()
            return bool(row and row["password_hash"])

    def bootstrap_available(self) -> bool:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT bootstrap_hash,bootstrap_expires FROM admin_auth WHERE singleton=1").fetchone()
            return bool(row and row["bootstrap_hash"] and row["bootstrap_expires"] > time.time())

    def issue_bootstrap(self, credential: str | None = None) -> str:
        self.initialize()
        token = credential or secrets.token_urlsafe(32)
        salt, digest = _hash_fields(token)
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("UPDATE admin_auth SET bootstrap_salt=?,bootstrap_hash=?,bootstrap_expires=?,updated_at=? WHERE singleton=1",
                       (salt, digest, now + BOOTSTRAP_TTL_SECONDS, now))
            self._audit(db, "bootstrap_credential_created")
        return token

    def complete_setup(self, credential: str, password: str) -> None:
        validate_password(password)
        self.initialize()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM admin_auth WHERE singleton=1").fetchone()
            if row["password_hash"] is not None:
                raise AdminAuthError("admin_setup_already_complete")
            if (not row["bootstrap_hash"] or row["bootstrap_expires"] <= time.time()
                    or not _verify(credential, row["bootstrap_salt"], row["bootstrap_hash"])):
                raise AdminAuthError("bootstrap_credential_invalid_or_expired")
            salt, digest = _hash_fields(password)
            db.execute("UPDATE admin_auth SET password_salt=?,password_hash=?,password_algorithm='argon2id',argon_memory_kib=?,argon_iterations=?,argon_lanes=?,bootstrap_salt=NULL,bootstrap_hash=NULL,bootstrap_expires=NULL,updated_at=? WHERE singleton=1",
                       (salt, digest, ARGON_MEMORY_KIB, ARGON_ITERATIONS, ARGON_LANES, time.time()))
            db.execute("DELETE FROM admin_sessions")
            self._audit(db, "administrator_setup_completed")

    def login(self, password: str, peer: str) -> tuple[str, int]:
        self.initialize()
        now = time.time()
        invalid = False
        created: tuple[str, int] | None = None
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM admin_auth WHERE singleton=1").fetchone()
            if not row["password_hash"]:
                raise AdminAuthError("admin_setup_required")
            attempts = db.execute("SELECT * FROM admin_login_attempts WHERE peer=?", (peer,)).fetchone()
            if attempts and now - attempts["window_started"] < LOGIN_WINDOW_SECONDS and attempts["failures"] >= LOGIN_FAILURE_LIMIT:
                raise AdminAuthError("admin_login_rate_limited")
            valid = _verify(password, row["password_salt"], row["password_hash"],
                            memory_kib=row["argon_memory_kib"] or ARGON_MEMORY_KIB,
                            iterations=row["argon_iterations"] or ARGON_ITERATIONS,
                            lanes=row["argon_lanes"] or ARGON_LANES)
            if not valid:
                if attempts and now - attempts["window_started"] < LOGIN_WINDOW_SECONDS:
                    failures = attempts["failures"] + 1
                    started = attempts["window_started"]
                else:
                    failures, started = 1, now
                db.execute("INSERT INTO admin_login_attempts(peer,window_started,failures) VALUES(?,?,?) ON CONFLICT(peer) DO UPDATE SET window_started=excluded.window_started,failures=excluded.failures",
                           (peer, started, failures))
                self._audit(db, "administrator_login_failed")
                invalid = True
            else:
                db.execute("DELETE FROM admin_login_attempts WHERE peer=?", (peer,))
                token = secrets.token_urlsafe(32)
                expires = now + SESSION_TTL_SECONDS
                db.execute("INSERT INTO admin_sessions(token_hash,expires_at,created_at) VALUES(?,?,?)",
                           (hashlib.sha256(token.encode()).hexdigest(), expires, now))
                db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now,))
                self._audit(db, "administrator_login_succeeded")
                created = (token, SESSION_TTL_SECONDS)
        if invalid:
            raise AdminAuthError("admin_login_invalid")
        assert created is not None
        return created

    def create_session(self, *, event: str = "administrator_legacy_session") -> tuple[str, int]:
        self.initialize()
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO admin_sessions(token_hash,expires_at,created_at) VALUES(?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), now + SESSION_TTL_SECONDS, now))
            self._audit(db, event)
        return token, SESSION_TTL_SECONDS

    def valid_session(self, token: str | None) -> bool:
        if not token:
            return False
        self.initialize()
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now,))
            return db.execute("SELECT 1 FROM admin_sessions WHERE token_hash=? AND expires_at>?",
                              (digest, now)).fetchone() is not None

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        self.initialize()
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (digest,))
            self._audit(db, "administrator_logout")

    def issue_recovery(self) -> str:
        self.initialize()
        token = secrets.token_urlsafe(32)
        salt, digest = _hash_fields(token)
        with self._lock, self._connect() as db:
            db.execute("UPDATE admin_auth SET recovery_salt=?,recovery_hash=?,recovery_expires=?,updated_at=? WHERE singleton=1",
                       (salt, digest, time.time() + RECOVERY_TTL_SECONDS, time.time()))
            self._audit(db, "password_recovery_credential_created")
        return token

    def reset_password(self, credential: str, password: str) -> None:
        validate_password(password)
        self.initialize()
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM admin_auth WHERE singleton=1").fetchone()
            if (not row["recovery_hash"] or row["recovery_expires"] <= now
                    or not _verify(credential, row["recovery_salt"], row["recovery_hash"])):
                raise AdminAuthError("recovery_credential_invalid_expired_or_used")
            salt, digest = _hash_fields(password)
            db.execute("UPDATE admin_auth SET password_salt=?,password_hash=?,password_algorithm='argon2id',argon_memory_kib=?,argon_iterations=?,argon_lanes=?,recovery_salt=NULL,recovery_hash=NULL,recovery_expires=NULL,updated_at=? WHERE singleton=1",
                       (salt, digest, ARGON_MEMORY_KIB, ARGON_ITERATIONS, ARGON_LANES, now))
            db.execute("DELETE FROM admin_sessions")
            db.execute("DELETE FROM admin_login_attempts")
            self._audit(db, "administrator_password_reset")

    def audit_events(self, limit: int = 50) -> list[dict[str, object]]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute("SELECT audit_id,event,occurred_at,details FROM admin_audit ORDER BY audit_id DESC LIMIT ?",
                              (max(1, min(limit, 200)),)).fetchall()
        return [dict(row) for row in rows]

    def record_audit(self, event: str, details: dict[str, object] | None = None) -> None:
        if not event or len(event) > 100:
            raise AdminAuthError("audit_event_invalid")
        self.initialize()
        with self._lock, self._connect() as db:
            self._audit(db, event, json.dumps(details or {}, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _audit(db: sqlite3.Connection, event: str, details: str = "{}") -> None:
        db.execute("INSERT INTO admin_audit(event,occurred_at,details) VALUES(?,?,?)",
                   (event, time.time(), details))

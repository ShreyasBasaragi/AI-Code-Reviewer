"""HTTPS broker for disposable gVisor-backed candidate test containers.

This service is intended for a dedicated Linux executor host. It never imports
or executes candidate code in the service process; Docker is used only to start
the pinned runner image under the mandatory ``runsc`` runtime.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TEST_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = 6 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")


def _canonical(value: Mapping[str, Any]) -> bytes:
    # Match RemoteHTTPSExecutionBroker's standard JSON canonicalization.
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_path(value: Any) -> bool:
    if (not isinstance(value, str) or not value.endswith(".py") or "\\" in value
            or "\x00" in value or value.startswith("/")):
        return False
    parts = value.split("/")
    return (all(part not in {"", ".", ".."} for part in parts)
            and PurePosixPath(value).as_posix() == value)


def _extract_test_bundle(bundle: bytes, root: Path) -> None:
    """Safely unpack Python test files for a read-only container bind mount."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("test_bundle_invalid") from exc
    with archive:
        members = archive.infolist()
        if not members or len(members) > 500:
            raise ValueError("test_bundle_file_count_invalid")
        total = 0
        seen: set[str] = set()
        for item in members:
            is_directory = item.is_dir()
            name = item.filename.rstrip("/") if is_directory else item.filename
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            allowed_type = {0, stat.S_IFDIR} if is_directory else {0, stat.S_IFREG}
            if (not name or name in seen or name.startswith("/") or "\\" in name
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or not path.parts or path.parts[0] != "tests"
                    or (not is_directory and path.suffix != ".py")
                    or file_type not in allowed_type):
                raise ValueError("test_bundle_path_invalid")
            seen.add(name)
            destination = root.joinpath(*path.parts)
            if is_directory:
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            total += item.file_size
            if item.file_size > 2 * 1024 * 1024 or total > 8 * 1024 * 1024:
                raise ValueError("test_bundle_uncompressed_size_invalid")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_bytes(archive.read(item))


class Executor:
    """Validate signed requests and execute only inside a constrained container."""

    def __init__(self, *, key: str, image: str, docker: str = "docker",
                 max_concurrent: int = 2, clock=time.time) -> None:
        if len(key.encode("utf-8")) < 32:
            raise ValueError("executor_hmac_key_too_short")
        if not isinstance(image, str) or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("executor_image_must_be_digest_pinned")
        if not isinstance(max_concurrent, int) or not 1 <= max_concurrent <= 8:
            raise ValueError("executor_concurrency_invalid")
        self.key = key.encode("utf-8")
        self.image = image
        self.docker = docker
        self.clock = clock
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def _validate(self, request: Any) -> tuple[dict[str, Any], bytes, bytes]:
        if not isinstance(request, dict):
            raise ValueError("request_invalid")
        signature = request.get("request_signature")
        unsigned = {key: value for key, value in request.items() if key != "request_signature"}
        expected = hmac.new(self.key, _canonical(unsigned), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ValueError("request_authentication_invalid")
        request_id = request.get("request_id")
        issued_at = request.get("issued_at")
        if (not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
                or not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool)
                or abs(self.clock() - float(issued_at)) > 120):
            raise ValueError("request_identity_or_age_invalid")
        with self._lock:
            now = self.clock()
            self._seen = {identity: stamp for identity, stamp in self._seen.items()
                          if now - stamp <= 300}
            if request_id in self._seen:
                raise ValueError("request_replay_rejected")
            self._seen[request_id] = now

        source = _decode(request.get("source_base64"), MAX_SOURCE_BYTES, "source")
        tests = _decode(request.get("tests_base64"), MAX_TEST_BYTES, "tests")
        candidate_sha = request.get("candidate_sha256")
        changed_files = request.get("changed_files")
        timeout = request.get("timeout_seconds")
        memory = request.get("memory_limit_bytes")
        cpu = request.get("cpu_limit_seconds")
        if (not isinstance(candidate_sha, str) or not _SHA256.fullmatch(candidate_sha)
                or not hmac.compare_digest(hashlib.sha256(source).hexdigest(), candidate_sha)):
            raise ValueError("candidate_digest_invalid")
        if (not isinstance(changed_files, list) or len(changed_files) != 1
                or not _safe_path(changed_files[0])):
            raise ValueError("candidate_file_scope_invalid")
        if (not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120
                or not isinstance(memory, int) or isinstance(memory, bool)
                or not 64 * 1024 * 1024 <= memory <= 2 * 1024 * 1024 * 1024
                or not isinstance(cpu, int) or isinstance(cpu, bool) or not 1 <= cpu <= timeout):
            raise ValueError("execution_limits_invalid")
        if request.get("network") is not False or request.get("credentials") != {}:
            raise ValueError("network_or_credentials_policy_invalid")
        return request, source, tests

    def execute(self, request: Any) -> dict[str, Any]:
        request_id = request.get("request_id") if isinstance(request, dict) else ""
        try:
            request, source, tests = self._validate(request)
        except (ValueError, TypeError, UnicodeError):
            raise
        if not self._slots.acquire(blocking=False):
            return self._signed_report(request, status="errored", executed=False,
                                       exit_code=None, output="executor_busy",
                                       process_cleanup=True)
        started = time.monotonic()
        outcome: dict[str, Any]
        try:
            outcome = self._run_container(request, source, tests)
        except Exception as exc:
            outcome = {"status": "errored", "executed": False, "exit_code": None,
                       "output": f"executor_{type(exc).__name__}", "process_cleanup": False}
        finally:
            self._slots.release()
        outcome["duration_seconds"] = round(time.monotonic() - started, 3)
        return self._signed_report(request, **outcome)

    def _run_container(self, request: Mapping[str, Any], source: bytes,
                       tests: bytes) -> dict[str, Any]:
        request_id = request["request_id"]
        name = "codementor-" + request_id
        workspace = Path(tempfile.mkdtemp(prefix="codementor-exec-"))
        try:
            os.chmod(workspace, 0o700)
            payload = dict(request)
            payload.pop("request_signature", None)
            payload.pop("tests_base64", None)
            payload.pop("network", None)
            payload.pop("credentials", None)
            (workspace / "tests").mkdir(mode=0o700)
            _extract_test_bundle(tests, workspace / "tests")
            input_path = workspace / "request.json"
            input_path.write_bytes(_canonical(payload))
            os.chmod(input_path, 0o400)
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        command = [
            self.docker, "run", "--pull=never", "--name", name,
            "--runtime=runsc", "--network=none",
            "--log-driver=none",
            "--memory", str(request["memory_limit_bytes"]),
            "--memory-swap", str(request["memory_limit_bytes"]), "--cpus=1",
            "--pids-limit=64", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=65532:65532",
            "--ulimit", f"cpu={request['cpu_limit_seconds']}:{request['cpu_limit_seconds'] + 1}",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=268435456",
            "--mount", f"type=bind,src={workspace},dst=/input,readonly",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "HF_HUB_OFFLINE=1",
            "--env", "TRANSFORMERS_OFFLINE=1", self.image,
            "python", "-I", "/opt/runner/runner.py", "/input/request.json",
        ]
        result = None
        timed_out = False
        cleanup_ok = False
        workspace_cleanup_ok = False
        try:
            # At one CPU of quota, an equal or lower wall cap bounds aggregate
            # CPU consumed by the whole process tree without trusting children
            # to report their own resource use.
            result = subprocess.run(command, capture_output=True,
                                    timeout=min(request["timeout_seconds"], request["cpu_limit_seconds"]),
                                    check=False, close_fds=True,
                                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            output = "candidate_execution_timeout"
        except (OSError, subprocess.SubprocessError) as exc:
            output = f"executor_runtime_{type(exc).__name__}"
        else:
            output = "candidate_tests_passed" if result.returncode == 0 else "candidate_tests_failed"
        finally:
            try:
                cleanup = subprocess.run([self.docker, "rm", "--force", name],
                                         capture_output=True, timeout=15, check=False,
                                         close_fds=True,
                                         env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
                cleanup_ok = cleanup.returncode == 0
            except (OSError, subprocess.SubprocessError):
                cleanup_ok = False
            try:
                shutil.rmtree(workspace)
                workspace_cleanup_ok = not workspace.exists()
            except OSError:
                workspace_cleanup_ok = False
        if not cleanup_ok or not workspace_cleanup_ok:
            return {"status": "errored", "executed": result is not None,
                    "exit_code": None, "output": "executor_cleanup_failed", "process_cleanup": False}
        if timed_out:
            return {"status": "timeout", "executed": True, "exit_code": None,
                    "output": output, "process_cleanup": True}
        if result is None:
            return {"status": "errored", "executed": False, "exit_code": None,
                    "output": output, "process_cleanup": True}
        if result.returncode == 0:
            status = "passed"
        elif result.returncode == 124:
            status = "timeout"
        else:
            status = "failed"
        return {"status": status, "executed": True, "exit_code": result.returncode,
                "output": output, "process_cleanup": True}

    def _signed_report(self, request: Mapping[str, Any], *, status: str,
                       executed: bool, exit_code: int | None, output: str,
                       process_cleanup: bool, duration_seconds: float = 0.0) -> dict[str, Any]:
        report = {
            "request_id": request["request_id"], "status": status,
            "executed": executed, "exit_code": exit_code,
            "candidate_sha256": request["candidate_sha256"],
            "changed_files": request["changed_files"],
            "network_isolated": True, "credentials_injected": False,
            "filesystem_restricted": True, "process_cleanup": process_cleanup,
            "timeout_enforced": True, "timeout_seconds": request["timeout_seconds"],
            "memory_limit_bytes": request["memory_limit_bytes"],
            "cpu_limit_seconds": request["cpu_limit_seconds"],
            "test_identity": hashlib.sha256(base64.b64decode(request["tests_base64"])).hexdigest(),
            "verifier_id": "codementor-runsc-executor-v1",
            "timestamp": self.clock(), "duration_seconds": duration_seconds,
            "output_excerpt": _output(output),
        }
        report["signature"] = hmac.new(self.key, _canonical(report), hashlib.sha256).hexdigest()
        return report


def _decode(value: Any, limit: int, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4 + 4:
        raise ValueError(f"{label}_payload_invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label}_payload_invalid") from exc
    if len(decoded) > limit:
        raise ValueError(f"{label}_payload_oversized")
    return decoded


def _output(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = value if isinstance(value, str) else ""
    text = text[:4000]
    for pattern, replacement in ((r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]"),
                                 (r"\b(?:ghp|github_pat|gsk|sk)-[A-Za-z0-9_-]{12,}\b", "[REDACTED]"),
                                 (r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]")):
        text = re.sub(pattern, replacement, text)
    return text


class _Handler(BaseHTTPRequestHandler):
    server_version = "CodeMentorExecutor"
    sys_version = ""

    def do_POST(self) -> None:  # noqa: N802
        executor: Executor = self.server.executor  # type: ignore[attr-defined]
        if self.path != "/v1/execute":
            self.send_error(404)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or int(length) < 1 or int(length) > MAX_BODY_BYTES:
            self.send_error(413)
            return
        try:
            raw = self.rfile.read(int(length))
            request = json.loads(raw.decode("utf-8"))
            report = executor.execute(request)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            self.send_error(401)
            return
        body = json.dumps(report, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Avoid logging paths, payloads, or candidate-controlled values.
        return


def serve() -> None:
    executor = Executor(
        key=os.environ.get("AI_CODE_REVIEWER_EXECUTOR_HMAC_KEY", ""),
        image=os.environ.get("AI_CODE_REVIEWER_EXECUTOR_IMAGE", ""),
        max_concurrent=int(os.environ.get("AI_CODE_REVIEWER_EXECUTOR_CONCURRENCY", "2")),
    )
    address = os.environ.get("AI_CODE_REVIEWER_EXECUTOR_BIND", "0.0.0.0")
    port = int(os.environ.get("AI_CODE_REVIEWER_EXECUTOR_PORT", "8443"))
    cert = os.environ["AI_CODE_REVIEWER_EXECUTOR_TLS_CERT"]
    key = os.environ["AI_CODE_REVIEWER_EXECUTOR_TLS_KEY"]
    server = ThreadingHTTPServer((address, port), _Handler)
    server.executor = executor  # type: ignore[attr-defined]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()

"""Trusted in-container test launcher. Candidate files stay in disposable /tmp."""
from __future__ import annotations

import base64
import json
import os
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


def _pytest_result_status(return_code: int, output: bytes, completion_token: str) -> str:
    """Require the trusted launcher to return after pytest, not just exit zero."""
    expected = f"CODEMENTOR_PYTEST_COMPLETE:{completion_token}:{return_code}".encode()
    if expected not in output.splitlines():
        return "errored"
    return "passed" if return_code == 0 else "failed"


def main(request_path: str) -> int:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    cpu = int(request["cpu_limit_seconds"])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    scratch = Path(tempfile.mkdtemp(prefix="codementor-tests-", dir="/tmp"))
    try:
        overlay = scratch / "candidate"
        overlay.mkdir(mode=0o700)
        relative = PurePosixPath(request["changed_files"][0])
        candidate = overlay.joinpath(*relative.parts)
        candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate.write_bytes(base64.b64decode(request["source_base64"], validate=True))
        baseline = Path("/opt/baseline")
        directory = candidate.parent
        while directory != overlay:
            relative = directory.relative_to(overlay)
            trusted_init = baseline.joinpath(*relative.parts, "__init__.py")
            overlay_init = directory / "__init__.py"
            if trusted_init.is_file() and not overlay_init.exists():
                overlay_init.write_bytes(
                    trusted_init.read_bytes()
                    + b"\nfrom pkgutil import extend_path as _codementor_extend_path\n"
                      b"__path__ = _codementor_extend_path(__path__, __name__)\n"
                )
            directory = directory.parent
        if request.get("test_suite") == "repository_tests_v1":
            test_directory = baseline / "tests"
            test_args = [str(test_directory), str(baseline / "dataset_pipeline/tests"),
                         str(baseline / "rag/tests")]
        else:
            tests = Path("/input/tests")
            if not (tests / "tests").is_dir():
                raise ValueError("test_bundle_tests_directory_missing")
            test_args = [str(tests / "tests")]
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "HOME": str(scratch),
        }
        completion_token = secrets.token_hex(32) if request.get("suppress_output") is True else ""
        if completion_token:
            env["CODEMENTOR_PYTEST_COMPLETION_TOKEN"] = completion_token
        process = subprocess.Popen(
            [sys.executable, "-I", "/opt/runner/pytest_launcher.py",
             str(overlay), str(baseline), *test_args],
            cwd=baseline, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
        try:
            output, _ = process.communicate(timeout=int(request["timeout_seconds"]))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate()
            if request.get("suppress_output") is not True:
                sys.stdout.buffer.write(output[-4000:])
            else:
                print("CODEMENTOR_STATUS:timeout")
            return 124
        if request.get("suppress_output") is not True:
            sys.stdout.buffer.write(output[-4000:])
        else:
            print("CODEMENTOR_STATUS:" +
                  _pytest_result_status(process.returncode, output, completion_token))
        return process.returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))

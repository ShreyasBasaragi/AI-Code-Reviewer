"""Trusted in-container test launcher. Candidate files stay in disposable /tmp."""
from __future__ import annotations

import base64
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


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
        tests = Path("/input/tests")
        if not (tests / "tests").is_dir():
            raise ValueError("test_bundle_tests_directory_missing")
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "HOME": str(scratch),
        }
        process = subprocess.Popen(
            [sys.executable, "-I", "/opt/runner/pytest_launcher.py",
             str(overlay), str(baseline), str(tests / "tests")],
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
            sys.stdout.buffer.write(output[-4000:])
            return 124
        sys.stdout.buffer.write(output[-4000:])
        return process.returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))

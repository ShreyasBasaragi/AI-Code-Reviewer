import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import deploy.behavioral_executor.pytest_launcher as pytest_launcher
from deploy.behavioral_executor.pytest_launcher import _SafeFailureReporter
from deploy.behavioral_executor.runner import _safe_pytest_diagnostics


def test_failure_reporter_keeps_only_trusted_test_location(tmp_path):
    baseline = tmp_path / "baseline"
    test_root = baseline / "tests"
    test_root.mkdir(parents=True)
    test_file = test_root / "test_example.py"
    test_file.write_text("def test_example(): pass\n", encoding="utf-8")
    reporter = _SafeFailureReporter(baseline, [str(test_root)])

    reporter.pytest_runtest_logreport(SimpleNamespace(
        failed=True, location=("tests/test_example.py", 8, "test_example[token-with-secret]"),
        when="call",
    ))
    reporter.pytest_runtest_logreport(SimpleNamespace(
        failed=True, location=("/tmp/private.py", 1, "test_secret"), when="call",
    ))

    assert reporter.failures == {"tests/test_example.py::test_example:9:call"}


def test_runner_diagnostics_require_completion_token_and_discard_pytest_text():
    token = "a" * 64
    output = b"""FAILED tests/test_private.py::test_failure - assertion contains candidate data
Traceback: do not print this
CODEMENTOR_PYTEST_FAILURE:""" + token.encode() + b""":tests/test_policy.py::test_denied:42:call
CODEMENTOR_PYTEST_SUMMARY:""" + token.encode() + b""":failed=1:collected=520:exit=1
CODEMENTOR_PYTEST_FAILURE:""" + ("b" * 64).encode() + b""":tests/test_fake.py::test_forged:1:call
CODEMENTOR_PYTEST_FAILURE:""" + token.encode() + b""":../../secret.py::test_leak:1:call
"""

    summary, failures = _safe_pytest_diagnostics(output, token)

    assert summary == "failed=1:collected=520:exit=1"
    assert failures == ["tests/test_policy.py::test_denied:42:call"]
    assert "candidate data" not in " ".join(failures)
    assert _safe_pytest_diagnostics(output, "short-token") == (None, [])


def test_runner_diagnostics_bound_failure_count_and_reject_untrusted_names():
    token = "c" * 64
    valid = f"CODEMENTOR_PYTEST_FAILURE:{token}:rag/tests/test_rag.py::test_example:4:setup".encode()
    unsafe = f"CODEMENTOR_PYTEST_FAILURE:{token}:tests/test_rag.py::test_bad-name:4:call".encode()
    traversal = f"CODEMENTOR_PYTEST_FAILURE:{token}:tests/../../secret.py::test_leak:4:call".encode()
    output = b"\n".join([valid] * 25 + [unsafe, traversal])

    summary, failures = _safe_pytest_diagnostics(output, token)

    assert summary is None
    assert failures == ["rag/tests/test_rag.py::test_example:4:setup"]


def test_pytest_launcher_reports_safe_failure_location_without_forwarding_details(tmp_path):
    baseline = tmp_path / "baseline"
    test_root = baseline / "tests"
    test_root.mkdir(parents=True)
    (baseline / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (test_root / "test_failure.py").write_text(
        "def test_failure():\n"
        "    assert False, 'untrusted candidate output marker'\n",
        encoding="utf-8",
    )
    overlay = tmp_path / "candidate"
    overlay.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    token = "d" * 64
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HOME": str(home),
        "CODEMENTOR_PYTEST_COMPLETION_TOKEN": token,
    }
    process = subprocess.run(
        [sys.executable, "-I", str(Path(pytest_launcher.__file__).resolve()),
         str(overlay), str(baseline), str(test_root)],
        cwd=baseline, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )

    assert process.returncode == 1
    summary, failures = _safe_pytest_diagnostics(process.stdout, token)
    assert summary == "failed=1:collected=1:exit=1"
    assert failures == ["tests/test_failure.py::test_failure:1:call"]
    assert b"untrusted candidate output marker" in process.stdout
    assert b"untrusted candidate output marker" not in " ".join(failures).encode()

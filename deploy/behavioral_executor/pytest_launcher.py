"""Load the trusted test runner before exposing the candidate import overlay."""
from __future__ import annotations

import sys
import os
import re
from pathlib import Path, PurePosixPath

import pytest


_SAFE_TEST_PATH = re.compile(
    r"^(?:tests|dataset_pipeline/tests|rag/tests)/(?:[A-Za-z0-9_.-]+/)*"
    r"[A-Za-z0-9_.-]+\.py$"
)
_SAFE_TEST_NAME = re.compile(r"^test_[A-Za-z0-9_]+$")


class _SafeFailureReporter:
    """Collect only locations inside the trusted test bundle, never messages."""

    def __init__(self, baseline: Path, test_directories: list[str]) -> None:
        self.baseline = baseline.resolve()
        self.test_roots = tuple(Path(path).resolve() for path in test_directories)
        self.failures: set[str] = set()
        self.failed = 0
        self.collected = 0

    def _record(self, path_value: object, name_value: object, line_value: object,
                phase_value: object) -> None:
        try:
            path = Path(str(path_value))
            if not path.is_absolute():
                path = self.baseline / path
            path = path.resolve()
            if not any(path.is_relative_to(root) for root in self.test_roots):
                return
            relative_path = PurePosixPath(path.relative_to(self.baseline).as_posix()).as_posix()
        except (OSError, RuntimeError, ValueError):
            return
        if not _SAFE_TEST_PATH.fullmatch(relative_path):
            return

        raw_name = str(name_value).split("[", 1)[0]
        name = raw_name if _SAFE_TEST_NAME.fullmatch(raw_name) else "test_unknown"
        try:
            line = int(line_value) + 1
        except (TypeError, ValueError):
            line = 0
        line = line if 0 <= line <= 999999 else 0
        phase = str(phase_value)
        if phase not in {"setup", "call", "teardown", "collect"}:
            phase = "unknown"
        self.failures.add(f"{relative_path}::{name}:{line}:{phase}")

    def pytest_runtest_logreport(self, report) -> None:
        if report.failed:
            self.failed += 1
            path, line, name = report.location
            self._record(path, name, line, report.when)

    def pytest_collectreport(self, report) -> None:
        if report.failed:
            self.failed += 1
            path = report.nodeid.split("::", 1)[0]
            self._record(path, "collection_error", 0, "collect")

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.collected = min(max(int(session.testscollected), 0), 999999)
        self.failed = min(len(self.failures), 999999)


def main() -> int:
    if len(sys.argv) < 4:
        return 2
    candidate_overlay, baseline, *test_directories = sys.argv[1:]
    # -I has already excluded PYTHONPATH and the working directory. Keep
    # stdlib/site-packages ahead of candidate imports and baseline source.
    sys.path.extend((candidate_overlay, baseline))
    completion_token = os.environ.pop("CODEMENTOR_PYTEST_COMPLETION_TOKEN", "")
    reporter = _SafeFailureReporter(Path(baseline), test_directories)
    result = int(pytest.main(["-q", "-p", "no:cacheprovider", *test_directories],
                             plugins=[reporter]))
    if re.fullmatch(r"[0-9a-f]{64}", completion_token):
        for failure in sorted(reporter.failures)[:20]:
            print(f"CODEMENTOR_PYTEST_FAILURE:{completion_token}:{failure}")
        print(f"CODEMENTOR_PYTEST_SUMMARY:{completion_token}:failed={reporter.failed}:"
              f"collected={reporter.collected}:exit={result}")
        print(f"CODEMENTOR_PYTEST_COMPLETE:{completion_token}:{result}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())

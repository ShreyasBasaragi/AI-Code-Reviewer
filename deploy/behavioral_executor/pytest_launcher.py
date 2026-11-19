"""Load the trusted test runner before exposing the candidate import overlay."""
from __future__ import annotations

import sys
import os
import re

import pytest


def main() -> int:
    if len(sys.argv) < 4:
        return 2
    candidate_overlay, baseline, *test_directories = sys.argv[1:]
    # -I has already excluded PYTHONPATH and the working directory. Keep
    # stdlib/site-packages ahead of candidate imports and baseline source.
    sys.path.extend((candidate_overlay, baseline))
    completion_token = os.environ.pop("CODEMENTOR_PYTEST_COMPLETION_TOKEN", "")
    result = int(pytest.main(["-q", "-p", "no:cacheprovider", *test_directories]))
    if re.fullmatch(r"[0-9a-f]{64}", completion_token):
        print(f"CODEMENTOR_PYTEST_COMPLETE:{completion_token}:{result}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())

"""Load the trusted test runner before exposing the candidate import overlay."""
from __future__ import annotations

import sys

import pytest


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    candidate_overlay, baseline, test_directory = sys.argv[1:]
    # -I has already excluded PYTHONPATH and the working directory. Keep
    # stdlib/site-packages ahead of candidate imports and baseline source.
    sys.path.extend((candidate_overlay, baseline))
    return int(pytest.main(["-q", "-p", "no:cacheprovider", test_directory]))


if __name__ == "__main__":
    raise SystemExit(main())

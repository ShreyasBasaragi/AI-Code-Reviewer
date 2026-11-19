#!/usr/bin/env python3
"""Read-only configuration preflight for the disposable autonomous-commit PR."""

from __future__ import annotations

import argparse
import json

from mcp_server.autonomous_preflight import run_autonomous_commit_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub owner/name")
    parser.add_argument("--branch", required=True, help="Disposable test/* branch")
    args = parser.parse_args()
    result = run_autonomous_commit_preflight(args.repository, args.branch)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

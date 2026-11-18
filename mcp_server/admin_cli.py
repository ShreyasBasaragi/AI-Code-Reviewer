"""Local-only administrator recovery commands."""

from __future__ import annotations

import argparse
import secrets
import sys

from mcp_server.admin_auth import AdminAuthStore


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m mcp_server.admin_cli")
    parser.add_argument("command", choices=("reset-password", "reset-bootstrap"))
    args = parser.parse_args()
    if not sys.stdout.isatty():
        parser.error("recovery credentials are displayed only in an interactive local terminal")
    store = AdminAuthStore()
    store.initialize()
    if args.command == "reset-password":
        if not store.setup_complete():
            parser.error("administrator setup is not complete; use the one-time bootstrap credential")
        credential = store.issue_recovery()
        print("One-time password recovery credential (expires in 10 minutes):")
        print(credential)
        print("Enter it only at the local AI Code Reviewer password-reset page.")
        return 0
    if store.setup_complete():
        parser.error("administrator is already configured; use reset-password instead")
    credential = secrets.token_urlsafe(32)
    store.issue_bootstrap(credential)
    print("One-time administrator bootstrap credential (expires in 30 minutes):")
    print(credential)
    print("Complete setup at the local AI Code Reviewer setup page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

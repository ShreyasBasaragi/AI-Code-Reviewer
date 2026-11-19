"""Local-only administrator recovery commands."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from mcp_server.admin_auth import AdminAuthError, AdminAuthStore


def _read_new_password(parser: argparse.ArgumentParser) -> str:
    password = getpass.getpass("New administrator password: ")
    confirmation = getpass.getpass("Confirm administrator password: ")
    if password != confirmation:
        parser.error("administrator passwords did not match")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m mcp_server.admin_cli")
    parser.add_argument("command", choices=("reset-password", "reset-bootstrap",
                                             "setup-password", "change-password"))
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
    if args.command == "setup-password":
        if store.setup_complete():
            parser.error("administrator is already configured; use change-password instead")
        password = _read_new_password(parser)
        try:
            bootstrap = store.issue_bootstrap(secrets.token_urlsafe(32))
            store.complete_setup(bootstrap, password)
        except AdminAuthError as exc:
            parser.error(str(exc))
        print("Administrator password configured. The temporary setup credential was consumed.")
        return 0
    if args.command == "change-password":
        if not store.setup_complete():
            parser.error("administrator is not configured; use setup-password first")
        password = _read_new_password(parser)
        try:
            recovery = store.issue_recovery()
            store.reset_password(recovery, password)
        except AdminAuthError as exc:
            parser.error(str(exc))
        print("Administrator password changed. Existing browser sessions were revoked.")
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

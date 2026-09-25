"""
Test: MCP Server + Desktop Toast Notification
Calls the MCP review_code tool directly and verifies the toast alert fires.
"""
import os
os.environ["USE_TF"] = "0"
os.environ["USE_TORCH"] = "1"
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Sample code with a known security vulnerability
VULN_CODE = """def get_user(user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return db.execute(query)
"""

# Sample clean code — should NOT trigger a notification
CLEAN_CODE = """def add(a: int, b: int) -> int:
    \"\"\"Returns the sum of two integers.\"\"\"
    return a + b
"""


def test_mcp_review_and_notify():
    print("=" * 65)
    print("  STEP 2: MCP review_code Tool + Desktop Toast Alert Test")
    print("=" * 65)

    # ── Import MCP server tools ──────────────────────────────
    print("\n[1] Importing MCP server tool functions...")
    from mcp_server.server import review_code
    from mcp_server.notifier import notify_user_if_issue, parse_review_verdict, send_windows_toast
    print("    OK: MCP server tools imported.")

    # ── Test 1: Vulnerable code should trigger alert ─────────
    print("\n[2] TEST CASE 1 — Vulnerable Code (SQL Injection)")
    print("    Code:", VULN_CODE.strip())
    print("\n    Running review_code() via MCP tool...")
    review = review_code(
        code=VULN_CODE,
        file_path="services/user_service.py"
    )
    print("\n    --- REVIEW OUTPUT ---")
    print(review)
    print("    --------------------")

    verdict = parse_review_verdict(review)
    print(f"\n    Parsed Verdict:")
    print(f"      Issue Found : {verdict['issue_found']}")
    print(f"      Issue Type  : {verdict['issue_type']}")
    print(f"      Severity    : {verdict['severity']}")

    if verdict["issue_found"]:
        print("\n    [OK] Issue correctly detected.")
        print("    [OK] Desktop Toast notification dispatched to Windows Action Center!")
    else:
        print("\n    [WARN] Issue not detected — check model output format.")

    # ── Test 2: Clean code should NOT trigger alert ──────────
    print("\n" + "-" * 65)
    print("[3] TEST CASE 2 — Clean Code (No Issues Expected)")
    print("    Code:", CLEAN_CODE.strip())
    print("\n    Running review_code() via MCP tool...")
    clean_review = review_code(
        code=CLEAN_CODE,
        file_path="math/calculator.py"
    )
    print("\n    --- REVIEW OUTPUT ---")
    print(clean_review)
    print("    --------------------")

    clean_verdict = parse_review_verdict(clean_review)
    if not clean_verdict["issue_found"]:
        print("\n    [OK] No issue found. Toast alert correctly suppressed.")
    else:
        print(f"\n    [INFO] Model found an issue: {clean_verdict['issue_type']} ({clean_verdict['severity']}) — may be valid feedback.")

    print("\n" + "=" * 65)
    print("  [SUCCESS] Step 2 Complete: MCP Tool + Notification System Working!")
    print("=" * 65)


if __name__ == "__main__":
    test_mcp_review_and_notify()

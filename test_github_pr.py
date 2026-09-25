"""
CRITIQUE  -- Live GitHub PR Integration Test
============================================

Tests the full end-to-end pipeline against a REAL GitHub pull request:
  1. Fetches the PR's changed files and diffs from GitHub API
  2. Runs each file through RAG  -> LLM  -> Classification
  3. Posts a formatted review comment back to the PR

Prerequisites:
  - Set GITHUB_TOKEN environment variable with a PAT that has
    repo write access (to post PR comments)
  - Have an open PR on a test repository

Usage:
  # Set your token
  set GITHUB_TOKEN=ghp_your_token_here

  # Run against a specific PR
  python test_github_pr.py --owner ShreyasBasaragi --repo AI-Code-Reviewer --pr 1

  # Or use defaults (edit DEFAULT_* constants below)
  python test_github_pr.py
"""

import os
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")

import sys
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_github_pr")

# Default test repository  -- change these to your test repo
DEFAULT_OWNER = "ShreyasBasaragi"
DEFAULT_REPO = "AI-Code-Reviewer"
DEFAULT_PR = 1


def main():
    parser = argparse.ArgumentParser(
        description="Test Critique pipeline against a real GitHub PR"
    )
    parser.add_argument("--owner", default=DEFAULT_OWNER, help="GitHub repo owner")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo name")
    parser.add_argument("--pr", type=int, default=DEFAULT_PR, help="PR number")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and review the PR but do NOT post comments (safe mode)"
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt"
    )
    args = parser.parse_args()

    # Check for GitHub token
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        try:
            import subprocess
            proc = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True
            )
            for line in proc.stdout.splitlines():
                if line.startswith("password="):
                    token = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass

    if not token:
        print("ERROR: GITHUB_TOKEN environment variable is not set and no credentials found.")
        print("Set it with:  set GITHUB_TOKEN=ghp_your_token_here")
        sys.exit(1)

    from mcp_server.orchestrator import handle_pr_event, save_pipeline_results

    print("=" * 70)
    print("  CRITIQUE  -- Live GitHub PR Integration Test")
    print("=" * 70)
    print(f"  Repository:  {args.owner}/{args.repo}")
    print(f"  PR Number:   #{args.pr}")
    print(f"  Post Mode:   {'DRY RUN (no comments posted)' if args.dry_run else 'LIVE (will post comments!)'}")
    print("=" * 70)

    if not args.dry_run and not args.yes:
        confirm = input(
            "\n  [!]  This will POST real comments to the PR. Continue? [y/N]: "
        ).strip().lower()
        if confirm != "y":
            print("  Aborted.")
            sys.exit(0)

    # Build the PR event payload (same format handle_pr_event expects)
    pr_data = {
        "owner": args.owner,
        "repo": args.repo,
        "pr_number": args.pr,
        "post_to_github": not args.dry_run,
        # files=None  -> orchestrator will fetch from GitHub API automatically
    }

    # Run the full pipeline
    results = handle_pr_event(
        pr_data=pr_data,
        github_token=token,
        notify_desktop=True,
    )

    if not results:
        print("\n  No reviewable code files found in this PR.")
        sys.exit(0)

    # Print summary
    print(f"\n{'=' * 70}")
    print(f"  RESULTS  -- {len(results)} file(s) reviewed")
    print(f"{'=' * 70}")

    for i, r in enumerate(results, start=1):
        posted = "[OK] Posted to PR" if r.comment_posted else "[*] Local only"
        error = f" (Error: {r.comment_post_error})" if r.comment_post_error else ""
        print(f"\n  [{i}] {r.file_path}")
        print(f"      Classification: {r.classification}  -- {r.classification_reason}")
        print(f"      Status:         {posted}{error}")
        print(f"      LLM Output:     {r.llm_output[:150]}...")

    # Save log
    log_path = save_pipeline_results(results, label=f"pr-{args.pr}")
    print(f"\n  Full pipeline log saved to: {log_path}")
    print(f"{'=' * 70}")

    if args.dry_run:
        print("\n  This was a dry run  -- no comments were posted.")
        print(f"  To post for real, run without --dry-run:")
        print(f"    python test_github_pr.py --owner {args.owner} --repo {args.repo} --pr {args.pr}")


if __name__ == "__main__":
    main()

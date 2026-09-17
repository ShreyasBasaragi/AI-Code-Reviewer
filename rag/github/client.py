import os
import json
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)


def parse_repo_string(repo_str: str) -> Tuple[str, str]:
    """
    Parse and validate an 'owner/repository' string.

    Args:
        repo_str: String formatted as 'owner/repo' (e.g. 'microsoft/vscode').

    Returns:
        Tuple of (owner, repo).

    Raises:
        ValueError: If format is invalid.
    """
    if not repo_str or "/" not in repo_str:
        raise ValueError(f"Invalid repository format '{repo_str}'. Must be formatted as 'owner/repository' (e.g. 'microsoft/vscode').")

    parts = [p.strip() for p in repo_str.split("/") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"Invalid repository format '{repo_str}'. Must contain exactly one slash 'owner/repository'.")

    return parts[0], parts[1]


class GitHubClient:
    """REST API client for fetching pull request and code review data from GitHub."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.base_url = "https://api.github.com"

        if not self.token:
            logger.warning(
                "No GITHUB_TOKEN provided or found in environment variables. "
                "GitHub API requests will be unauthenticated and subject to strict rate limits (60 req/hr)."
            )

    def _make_request(self, endpoint: str) -> Any:
        """Send HTTP GET request to GitHub REST API endpoint."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Critique-RAG-CodeReviewer"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, headers=headers)

        try:
            with urllib.request.urlopen(req) as response:
                content = response.read().decode("utf-8")
                return json.loads(content)
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise RuntimeError("GitHub API 401 Unauthorized: Invalid or expired GITHUB_TOKEN.") from err
            elif err.code == 403:
                raise RuntimeError("GitHub API 403 Forbidden: API rate limit exceeded or access denied. Provide GITHUB_TOKEN.") from err
            elif err.code == 404:
                logger.warning(f"GitHub API 404 Not Found for endpoint: {endpoint}")
                return None
            else:
                raise RuntimeError(f"GitHub API HTTP error {err.code}: {err.reason} for {endpoint}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"Network connection failed accessing GitHub API: {err.reason}") from err

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> Optional[Dict[str, Any]]:
        """Fetch metadata for a single pull request."""
        endpoint = f"repos/{owner}/{repo}/pulls/{pr_number}"
        return self._make_request(endpoint)

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Fetch list of changed files and diff patches for a pull request."""
        endpoint = f"repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100"
        res = self._make_request(endpoint)
        return res if isinstance(res, list) else []

    def get_pr_reviews(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Fetch pull request reviews."""
        endpoint = f"repos/{owner}/{repo}/pulls/{pr_number}/reviews?per_page=100"
        res = self._make_request(endpoint)
        return res if isinstance(res, list) else []

    def get_pr_comments(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Fetch line-level code review comments for a pull request."""
        endpoint = f"repos/{owner}/{repo}/pulls/{pr_number}/comments?per_page=100"
        res = self._make_request(endpoint)
        return res if isinstance(res, list) else []

    def get_closed_merged_prs(self, owner: str, repo: str, max_prs: int = 10) -> List[Dict[str, Any]]:
        """Fetch recent closed/merged pull requests for a repository."""
        endpoint = f"repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={max_prs}"
        res = self._make_request(endpoint)
        return res if isinstance(res, list) else []

import os
import subprocess
import urllib.request
import urllib.error
import json
import base64

def get_github_token() -> str:
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""

TOKEN = get_github_token()
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "Critique-Setup",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json",
}

OWNER = "4EdmunPeyton21"


def get_main_sha(repo: str) -> str:
    url = f"https://api.github.com/repos/{OWNER}/{repo}/git/refs/heads/main"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["object"]["sha"]


def create_branch(repo: str, branch_name: str, sha: str):
    url = f"https://api.github.com/repos/{OWNER}/{repo}/git/refs"
    payload = json.dumps({"ref": f"refs/heads/{branch_name}", "sha": sha}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Created branch '{branch_name}' on {repo}")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")
        if "Reference already exists" in err:
            print(f"Branch '{branch_name}' already exists on {repo}")
        else:
            raise


def add_file(repo: str, branch_name: str, file_path: str, content: str, commit_msg: str):
    url = f"https://api.github.com/repos/{OWNER}/{repo}/contents/{file_path}"
    b64_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = json.dumps({
        "message": commit_msg,
        "content": b64_content,
        "branch": branch_name
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=HEADERS, method="PUT")
    with urllib.request.urlopen(req) as resp:
        print(f"Added '{file_path}' to {branch_name} on {repo}")


def open_pr(repo: str, branch_name: str, title: str, body: str) -> str:
    url = f"https://api.github.com/repos/{OWNER}/{repo}/pulls"
    payload = json.dumps({
        "title": title,
        "body": body,
        "head": branch_name,
        "base": "main"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["html_url"]
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")
        if "A pull request already exists" in err:
            print(f"A PR already exists for {branch_name} on {repo}")
            return f"https://github.com/{OWNER}/{repo}/pulls"
        else:
            raise


SAMPLE_CODE_LIVE = '''import time
import json
import sqlite3

def get_user_by_id(db_conn, user_id):
    """Fetch user record from database."""
    # SQL query built with direct string concatenation
    query = "SELECT id, username, email FROM users WHERE id = '" + user_id + "'"
    cursor = db_conn.cursor()
    cursor.execute(query)
    return cursor.fetchone()

def log_user_activity(event_name, details={}):
    """Log user activity events."""
    details["timestamp"] = time.time()
    details["event"] = event_name
    return details
'''

SAMPLE_CODE_TOMORROW = '''import os
import json

def read_app_config(config_path):
    """Load application configuration."""
    f = open(config_path, "r")
    data = json.loads(f.read())
    # Resource leak: file descriptor is not closed via context manager
    return data

def calculate_average_score(scores):
    """Calculate average of test scores."""
    total = sum(scores)
    # Potential ZeroDivisionError if scores list is empty
    return total / len(scores)
'''


def main():
    print("Setting up demo repositories...")

    # Repo 1: critique-demo-live (Test Now)
    print("\n--- Setting up critique-demo-live ---")
    sha1 = get_main_sha("critique-demo-live")
    create_branch("critique-demo-live", "feature/auth-service", sha1)
    add_file(
        repo="critique-demo-live",
        branch_name="feature/auth-service",
        file_path="services/user_service.py",
        content=SAMPLE_CODE_LIVE,
        commit_msg="Add user lookup and event logger functions"
    )
    pr1_url = open_pr(
        repo="critique-demo-live",
        branch_name="feature/auth-service",
        title="Add User Service with lookup and event logging",
        body="This PR introduces `get_user_by_id` and `log_user_activity` for the user management service."
    )
    print("Demo Repo 1 PR URL:", pr1_url)

    # Repo 2: critique-demo-midsem (Test Tomorrow)
    print("\n--- Setting up critique-demo-midsem ---")
    sha2 = get_main_sha("critique-demo-midsem")
    create_branch("critique-demo-midsem", "feature/config-and-calc", sha2)
    add_file(
        repo="critique-demo-midsem",
        branch_name="feature/config-and-calc",
        file_path="utils/config_manager.py",
        content=SAMPLE_CODE_TOMORROW,
        commit_msg="Add configuration reader and scoring utilities"
    )
    pr2_url = open_pr(
        repo="critique-demo-midsem",
        branch_name="feature/config-and-calc",
        title="Add Config Manager and Score Calculations for Midsem Demo",
        body="This PR adds configuration parsing and score analytics utilities ready for tomorrow's midsem presentation."
    )
    print("Demo Repo 2 PR URL:", pr2_url)

    print("\nSetup complete! Both repositories and PRs are ready.")


if __name__ == "__main__":
    main()

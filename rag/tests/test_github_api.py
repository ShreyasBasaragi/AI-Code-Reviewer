import hashlib

import pytest
from rag.github.client import parse_repo_string, GitHubClient
from rag.adapters.github_api import convert_github_api_pr_to_canonical
from rag.adapters.base import CanonicalRecord
from rag.ingest_github import upsert_github_records_to_chromadb, get_chromadb_source_breakdown
from rag.retrieve import retrieve_context
import rag.ingest_github as github_ingest


def test_parse_repo_string():
    owner, repo = parse_repo_string("microsoft/vscode")
    assert owner == "microsoft"
    assert repo == "vscode"

    owner, repo = parse_repo_string(" psf / requests ")
    assert owner == "psf"
    assert repo == "requests"

    with pytest.raises(ValueError):
        parse_repo_string("invalid_no_slash")

    with pytest.raises(ValueError):
        parse_repo_string("a/b/c")


def test_convert_github_api_pr_to_canonical():
    pr_info = {
        "number": 101,
        "title": "Fix null pointer in user authentication service"
    }
    files = [
        {
            "filename": "src/auth.py",
            "patch": "@@ -10,3 +10,5 @@\n-def login(user):\n+def login(user: Optional[User]):\n+    if user is None: return False"
        }
    ]
    reviews = [{"id": 1, "state": "COMMENTED"}]
    comments = [
        {
            "id": 998811,
            "path": "src/auth.py",
            "body": "Great addition of null check here before calling methods.",
            "diff_hunk": "@@ -10,3 +10,5 @@\n+    if user is None: return False",
            "user": {"login": "octocat"}
        }
    ]

    recs = convert_github_api_pr_to_canonical(
        owner="testowner",
        repo="testrepo",
        pr_info=pr_info,
        files=files,
        reviews=reviews,
        comments=comments
    )

    assert len(recs) == 1
    rec = recs[0]
    assert isinstance(rec, CanonicalRecord)
    assert rec.source == "github_api"
    assert rec.language == "python"
    assert rec.id == "github_api_testowner_testrepo_pr101_comment_998811"
    assert rec.metadata["repository"] == "testowner/testrepo"
    assert rec.metadata["pr_number"] == 101


def test_github_api_idempotency_and_chromadb_preservation(tmp_path, monkeypatch):
    # This test intentionally exercises upsert; keep the writable Chroma
    # client inside pytest's disposable directory rather than the app corpus.
    monkeypatch.setattr(github_ingest, "CHROMA_PERSIST_DIR", tmp_path / "github-ingest-db")

    # Chroma's SentenceTransformer embedding function downloads/loads the
    # all-MiniLM model. The test checks persistence and idempotent upsert, so
    # use stable local vectors and keep the real Chroma database operations.
    class DeterministicEmbeddingFunction:
        def __init__(self, model_name=None):
            self.model_name = model_name

        def __call__(self, input):
            return [
                [value / 255.0 for value in hashlib.sha256(text.encode("utf-8")).digest()[:8]]
                for text in input
            ]

        @staticmethod
        def name():
            return "github-api-test-deterministic"

        @staticmethod
        def build_from_config(_config):
            return DeterministicEmbeddingFunction()

        def get_config(self):
            return {}

        def is_legacy(self):
            return False

        def supported_spaces(self):
            return ["cosine", "l2", "ip"]

    monkeypatch.setattr(
        github_ingest.embedding_functions,
        "SentenceTransformerEmbeddingFunction",
        DeterministicEmbeddingFunction,
    )

    # Create test canonical record
    test_rec = CanonicalRecord(
        id="github_api_unittest_repo_pr1_comment_123456",
        text="Live GitHub Code Review (unittest/repo PR #1):\nReviewer Comment: Use explicit error handling.\nFile: app.py\nCode Patch: try: pass",
        source="github_api",
        language="python",
        type="code_review",
        tags=["python", "github_api"],
        metadata={"repository": "unittest/repo", "pr_number": 1, "file_path": "app.py"}
    )

    # First upsert
    cnt1 = upsert_github_records_to_chromadb([test_rec])
    assert cnt1 == 1

    # Second upsert (idempotency test - should not duplicate)
    cnt2 = upsert_github_records_to_chromadb([test_rec])
    assert cnt2 == 1

    # Post breakdown check
    after_breakdown = get_chromadb_source_breakdown()
    assert after_breakdown.get("github_api", 0) >= 1, \
        "New github_api records must be present in ChromaDB"


def test_retrieve_context_works_with_github_api_records(isolated_v2_rag):
    snippets = retrieve_context("def login(user: Optional[User]):")
    assert isinstance(snippets, list)
    for item in snippets:
        assert isinstance(item, str)

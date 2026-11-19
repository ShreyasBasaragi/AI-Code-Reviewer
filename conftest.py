"""Opt-in isolation helpers for tests that query the canonical RAG v2 corpus."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest


def _directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(value for value in path.rglob("*") if value.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


@pytest.fixture(scope="session", autouse=True)
def canonical_v2_integrity_guard():
    """Cover the whole test session, including tests that forgot to opt in."""
    from rag.config import CHROMA_PERSIST_DIR

    canonical = Path(CHROMA_PERSIST_DIR)
    if not canonical.is_dir():
        yield
        return
    before = _directory_sha256(canonical)
    yield
    assert _directory_sha256(canonical) == before, (
        "RAG tests modified canonical v2 corpus bytes; retrieval/upsert tests must use the isolated copy"
    )


@pytest.fixture(autouse=True)
def _identify_per_test_canonical_writes(request):
    """Pin a detected write to the test that opened the production database."""
    from rag.config import CHROMA_PERSIST_DIR

    canonical = Path(CHROMA_PERSIST_DIR)
    if not canonical.is_dir():
        yield
        return

    def stats():
        return {
            item.relative_to(canonical).as_posix(): (item.stat().st_mtime_ns, item.stat().st_size)
            for item in canonical.rglob("*") if item.is_file()
        }

    before = stats()
    yield
    after = stats()
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    if changed:
        raise AssertionError(
            f"{request.node.nodeid} modified canonical v2 RAG files: {', '.join(changed[:5])}"
        )


@pytest.fixture(scope="session")
def isolated_v2_rag_path(tmp_path_factory, canonical_v2_integrity_guard):
    """Make one disposable corpus copy for tests that exercise real retrieval."""
    from rag.config import CHROMA_PERSIST_DIR

    canonical = Path(CHROMA_PERSIST_DIR)
    if not canonical.is_dir():
        pytest.skip("canonical v2 RAG corpus is unavailable")
    isolated = tmp_path_factory.mktemp("rag-v2-isolated") / canonical.name
    shutil.copytree(canonical, isolated)
    return isolated


@pytest.fixture
def isolated_v2_rag(monkeypatch, isolated_v2_rag_path):
    """Point legacy v2 readers at the session copy for one test."""
    import rag.retrieve as retrieve

    previous_path = retrieve.CHROMA_PERSIST_DIR
    previous_client = retrieve._chroma_client
    previous_collection = retrieve._collection
    previous_embedding = retrieve._embedding_function
    retrieve.CHROMA_PERSIST_DIR = isolated_v2_rag_path
    retrieve._chroma_client = None
    retrieve._collection = None
    retrieve._embedding_function = None
    yield isolated_v2_rag_path
    retrieve._chroma_client = None
    retrieve._collection = None
    retrieve._embedding_function = None
    monkeypatch.setattr(retrieve, "CHROMA_PERSIST_DIR", previous_path)
    retrieve._chroma_client = previous_client
    retrieve._collection = previous_collection
    retrieve._embedding_function = previous_embedding

"""Bounded, read-only retrieval of allowlisted repository guidance files."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from review_schema import RetrievedEvidence


ROOT_FILES = frozenset({
    "readme.md", "contributing.md", "agents.md", "style_guide.md",
    "styleguide.md", "code_style.md", "architecture.md", "pyproject.toml",
    "setup.cfg", "tox.ini", ".flake8", "mypy.ini", ".editorconfig",
})
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})
_WORD = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")


@dataclass(frozen=True)
class RepositoryContextLimits:
    max_files: int = 12
    max_directory_entries: int = 100
    max_directory_depth: int = 2
    max_file_bytes: int = 48 * 1024
    max_total_bytes: int = 192 * 1024
    max_selected_files: int = 4

    @classmethod
    def from_environment(cls) -> "RepositoryContextLimits":
        import os

        def bounded(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError:
                value = default
            return min(max(value, low), high)

        return cls(
            max_files=bounded("GITHUB_REPO_CONTEXT_MAX_FILES", 12, 1, 30),
            max_directory_entries=bounded("GITHUB_REPO_CONTEXT_MAX_ENTRIES", 100, 1, 100),
            max_directory_depth=bounded("GITHUB_REPO_CONTEXT_MAX_DEPTH", 2, 0, 2),
            max_file_bytes=bounded("GITHUB_REPO_CONTEXT_MAX_FILE_BYTES", 48 * 1024, 1024, 256 * 1024),
            max_total_bytes=bounded("GITHUB_REPO_CONTEXT_MAX_TOTAL_BYTES", 192 * 1024, 4096, 1024 * 1024),
            max_selected_files=bounded("GITHUB_REPO_CONTEXT_MAX_SELECTED_FILES", 4, 1, 8),
        )


@dataclass(frozen=True)
class RepositoryContextFile:
    path: str
    revision: str
    text: str
    truncated: bool = False

    def to_evidence(self, repository: str) -> RetrievedEvidence:
        content_digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        stable = "\0".join((repository.casefold(), self.revision, self.path, content_digest))
        evidence_id = "repo-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
        return RetrievedEvidence(
            evidence_id=evidence_id,
            source_family="repository_context",
            source_url=None,
            rule_identifier=None,
            text=self.text,
            language=None,
            metadata={
                "origin": "repository_context",
                "repository": repository,
                "path": self.path,
                "revision": self.revision,
                "content_sha256": content_digest,
                "truncated": self.truncated,
            },
        )


@dataclass(frozen=True)
class RepositoryContextResult:
    revision: str
    status: str
    files: tuple[RepositoryContextFile, ...] = ()
    warnings: tuple[str, ...] = ()
    total_bytes: int = 0

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "revision": self.revision,
            "files": [
                {"path": item.path, "revision": item.revision,
                 "bytes": len(item.text.encode("utf-8")), "truncated": item.truncated}
                for item in self.files
            ],
            "warnings": list(self.warnings),
            "total_bytes": self.total_bytes,
        }

    def evidence_for(self, code: str, file_path: str, repository: str,
                     limits: RepositoryContextLimits) -> tuple[RetrievedEvidence, ...]:
        chosen = select_relevant_context(self.files, code, file_path, limits.max_selected_files)
        return tuple(item.to_evidence(repository) for item in chosen)


def _is_safe_repo_path(path: Any) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} and not any(ord(ch) < 32 for ch in part) for part in parts)


def _allowlisted_file(path: str) -> bool:
    if not _is_safe_repo_path(path):
        return False
    parts = path.split("/")
    name = parts[-1].casefold()
    if len(parts) == 1:
        return name in ROOT_FILES
    # Only documentation beneath the explicit docs/ root is traversed. The
    # traversal caller also enforces depth and only descends into real dirs.
    return parts[0].casefold() == "docs" and name.rsplit(".", 1)[-1] in {
        "md", "rst", "txt", "adoc"
    }


def collect_repository_context(client: Any, ref: Any, revision: str,
                               limits: RepositoryContextLimits | None = None) -> RepositoryContextResult:
    """Fetch a small allowlisted documentation set at one immutable commit.

    The client interface is deliberately narrow and injectable. Directory
    entries only select fixed names and docs files; server-provided URLs,
    symlinks, scripts, and arbitrary file paths are never followed.
    """
    limits = limits or RepositoryContextLimits.from_environment()
    if not callable(getattr(client, "list_directory", None)) or not callable(
        getattr(client, "get_context_text_file", None)
    ):
        return RepositoryContextResult(revision, "unavailable", warnings=("repository_context_client_unavailable",))

    warnings: list[str] = []
    candidates: dict[str, dict[str, Any]] = {}
    pending_directories: list[tuple[str, int]] = [("", 0)]
    while pending_directories:
        directory, depth = pending_directories.pop(0)
        if directory and directory.casefold() != "docs" and not directory.casefold().startswith("docs/"):
            continue
        try:
            entries, truncated = client.list_directory(
                ref, directory, revision, max_entries=limits.max_directory_entries
            )
        except Exception:
            warnings.append("repository_context_directory_unavailable")
            continue
        if truncated:
            warnings.append("repository_context_directory_truncated")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path, kind = entry.get("path"), entry.get("type")
            if not _is_safe_repo_path(path):
                warnings.append("repository_context_unsafe_entry_omitted")
                continue
            if kind == "symlink" or entry.get("mode") == "120000":
                warnings.append("repository_context_symlink_omitted")
                continue
            parts = path.split("/")
            if kind == "dir" and parts[0].casefold() == "docs" and depth < limits.max_directory_depth:
                # Do not follow names that are not a single child beneath docs.
                if len(parts) == depth + 1 and parts[0].casefold() == "docs":
                    pending_directories.append((path, depth + 1))
                continue
            if kind != "file" or entry.get("mode") not in {None, "100644", "100755"} or not _allowlisted_file(path):
                continue
            if path.casefold() == "docs" or len(parts) > limits.max_directory_depth + 1:
                continue
            candidates.setdefault(path, entry)

    ordered = sorted(candidates)
    if len(ordered) > limits.max_files:
        warnings.append("repository_context_file_count_truncated")
        ordered = ordered[:limits.max_files]
    files: list[RepositoryContextFile] = []
    total_bytes = 0
    for path in ordered:
        if total_bytes >= limits.max_total_bytes:
            warnings.append("repository_context_total_bytes_truncated")
            break
        declared_size = candidates[path].get("size")
        if isinstance(declared_size, int) and declared_size < 0:
            warnings.append("repository_context_invalid_size_omitted")
            continue
        remaining = limits.max_total_bytes - total_bytes
        per_file_limit = min(limits.max_file_bytes, remaining)
        try:
            text, truncated = client.get_context_text_file(
                ref, path, revision, max_bytes=per_file_limit
            )
        except Exception:
            warnings.append("repository_context_file_unavailable")
            continue
        if not isinstance(text, str) or not text:
            continue
        byte_count = len(text.encode("utf-8"))
        if byte_count > per_file_limit:
            text = text.encode("utf-8")[:per_file_limit].decode("utf-8", errors="ignore")
            byte_count = len(text.encode("utf-8"))
            truncated = True
        if truncated:
            warnings.append("repository_context_content_truncated")
        total_bytes += byte_count
        files.append(RepositoryContextFile(path, revision, text, bool(truncated)))
    status = "available" if files else "unavailable"
    if not files and not warnings:
        warnings.append("repository_context_no_allowlisted_files")
    return RepositoryContextResult(revision, status, tuple(files), tuple(dict.fromkeys(warnings)), total_bytes)


def select_relevant_context(files: Iterable[RepositoryContextFile], code: str,
                            file_path: str, limit: int) -> tuple[RepositoryContextFile, ...]:
    """Choose a deterministic per-file subset by lexical overlap and path cues."""
    code_terms = set(_WORD.findall((code + " " + file_path).casefold()))
    scored: list[tuple[int, str, RepositoryContextFile]] = []
    for item in files:
        text_terms = set(_WORD.findall((item.path + " " + item.text).casefold()))
        score = len(code_terms & text_terms)
        basename = item.path.rsplit("/", 1)[-1].casefold()
        if basename in {"agents.md", "contributing.md", "style_guide.md", "styleguide.md", "architecture.md"}:
            score += 2
        if basename == "readme.md":
            score += 1
        scored.append((score, item.path, item))
    ordered = sorted(scored, key=lambda row: (-row[0], row[1]))
    selected = [row for row in ordered if row[0] > 0][:max(0, limit)]
    if not selected:
        # General repository guidance is still relevant to a Python review
        # when identifiers do not overlap (for example, a short new module).
        safe_defaults = {"readme.md", "contributing.md", "agents.md", "style_guide.md", "styleguide.md"}
        selected = [row for row in ordered if row[2].path.rsplit("/", 1)[-1].casefold() in safe_defaults][:max(0, limit)]
    return tuple(row[2] for row in selected)

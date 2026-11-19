"""Prepare an opaque request for the isolated trusted test runner."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

MAX_SOURCE_BYTES = 2 * 1024 * 1024


def prepare(repository: Path, candidate_sha: str) -> tuple[dict, dict[str, str]]:
    if not re.fullmatch(r"[0-9a-f]{40,64}", candidate_sha or ""):
        raise ValueError("candidate_sha_invalid")
    actual = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    if actual != candidate_sha:
        raise ValueError("candidate_checkout_sha_mismatch")
    parents = subprocess.check_output(["git", "-C", str(repository), "show", "-s", "--format=%P", "HEAD"], text=True).split()
    if len(parents) != 1 or not re.fullmatch(r"[0-9a-f]{40,64}", parents[0]):
        raise ValueError("candidate_parent_invalid")
    changed = subprocess.check_output(
        ["git", "-C", str(repository), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        text=True,
    ).splitlines()
    if len(changed) != 1:
        raise ValueError("candidate_changed_file_scope_invalid")
    relative = PurePosixPath(changed[0])
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or "." in relative.parts or relative.suffix != ".py" or "\\" in changed[0]
            or any(ord(character) < 32 for character in changed[0])):
        raise ValueError("candidate_changed_file_invalid")
    path = repository
    for component in relative.parts[:-1]:
        path = path / component
        path.lstat()
        if path.is_symlink() or not path.is_dir():
            raise ValueError("candidate_parent_path_invalid")
    path = path / relative.parts[-1]
    info = path.lstat()
    if (not path.is_file() or path.is_symlink() or info.st_size > MAX_SOURCE_BYTES
            or not path.resolve(strict=True).is_relative_to(repository.resolve(strict=True))):
        raise ValueError("candidate_source_invalid_or_oversized")
    source = path.read_bytes()
    request = {
        "test_suite": "repository_tests_v1", "suppress_output": True,
        "source_base64": base64.b64encode(source).decode("ascii"),
        "changed_files": [relative.as_posix()], "timeout_seconds": 300,
        "memory_limit_bytes": 2 * 1024 * 1024 * 1024,
        "cpu_limit_seconds": 180,
    }
    outputs = {
        "candidate_sha": candidate_sha, "parent_sha": parents[0],
        "changed_file": relative.as_posix(),
        "source_sha256": hashlib.sha256(source).hexdigest(),
    }
    return request, outputs


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: make_request.py CANDIDATE_DIRECTORY CANDIDATE_SHA OUTPUT_JSON")
    request, outputs = prepare(Path(sys.argv[1]).resolve(), sys.argv[2])
    Path(sys.argv[3]).write_text(json.dumps(request, separators=(",", ":")), encoding="utf-8")
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with Path(output_file).open("a", encoding="utf-8") as stream:
            for name, value in outputs.items():
                stream.write(f"{name}={value}\n")

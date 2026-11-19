"""Stable identity for the independently maintained repository test bundle."""
from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ROOTS = ("tests", "dataset_pipeline/tests", "rag/tests")
IDENTITY_FILES = (
    "pytest.ini", "conftest.py", "requirements.txt",
    "deploy/behavioral_executor/Dockerfile",
    "deploy/behavioral_executor/runner.py",
    "deploy/behavioral_executor/pytest_launcher.py",
    "deploy/behavioral_executor/bundle_identity.py",
    "deploy/behavioral_executor/make_request.py",
    "deploy/behavioral_executor/sign_attestation.py",
)


def bundle_sha256(root: Path = ROOT) -> str:
    files: list[Path] = []
    for name in BUNDLE_ROOTS + IDENTITY_FILES:
        target = root / name
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(path for path in target.rglob("*") if path.is_file()
                         and not any(part.startswith(".") for part in path.relative_to(root).parts)
                         and "__pycache__" not in path.parts)
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


if __name__ == "__main__":
    print(bundle_sha256())

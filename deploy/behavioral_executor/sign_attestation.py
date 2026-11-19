"""Create the trusted GitHub Actions behavioral attestation artifact."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from pathlib import PurePosixPath


def create_attestation(values: dict[str, str], key: str) -> dict:
    required = ("repository", "source_workflow_id", "source_workflow_run_id", "candidate_sha",
                "parent_sha", "changed_file", "source_sha256", "test_identity", "status",
                "exit_code", "duration_seconds", "attestation_workflow_id", "attestation_run_id")
    if not isinstance(values, dict) or any(not isinstance(values.get(item), str) for item in required):
        raise ValueError("attestation_inputs_invalid")
    if len(key.encode()) < 32:
        raise ValueError("attestation_key_invalid")
    for field in ("candidate_sha", "parent_sha"):
        if not re.fullmatch(r"[0-9a-f]{40,64}", values[field]):
            raise ValueError("attestation_sha_invalid")
    if (not re.fullmatch(r"[0-9]{1,20}", values["source_workflow_id"])
            or not re.fullmatch(r"[0-9]{1,20}", values["source_workflow_run_id"])
            or not re.fullmatch(r"[0-9]{1,20}", values["attestation_workflow_id"])
            or not re.fullmatch(r"[0-9]{1,20}", values["attestation_run_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", values["source_sha256"])
            or not re.fullmatch(r"[0-9a-f]{64}", values["test_identity"])
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", values["repository"])
            or not _safe_python_path(values["changed_file"])
            or values["status"] not in {"passed", "failed", "timeout", "errored"}
            or not re.fullmatch(r"-?[0-9]{1,4}", values["exit_code"])
            or not re.fullmatch(r"[0-9]{1,8}", values["duration_seconds"])):
        raise ValueError("attestation_claim_invalid")
    document = {
        "version": "codementor-github-behavioral-v1",
        "repository": values["repository"],
        "source_workflow_id": values["source_workflow_id"],
        "source_workflow_run_id": int(values["source_workflow_run_id"]),
        "candidate_sha": values["candidate_sha"],
        "parent_sha": values["parent_sha"],
        "changed_files": [values["changed_file"]],
        "source_sha256": values["source_sha256"],
        "test_identity": values["test_identity"],
        "status": values["status"],
        "exit_code": int(values["exit_code"]),
        "duration_seconds": int(values["duration_seconds"]),
        "issued_at": int(time.time()),
        "attestation_workflow_id": values["attestation_workflow_id"],
        "attestation_run_id": int(values["attestation_run_id"]),
        "execution_mode": "github_hosted_docker",
        "network_isolated": True,
        "credentials_injected": False,
        "filesystem_restricted": True,
        "timeout_enforced": True,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    document["signature"] = hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()
    return document


def _safe_python_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (len(value) <= 240 and not path.is_absolute() and bool(path.parts)
            and ".." not in path.parts and "." not in path.parts and path.suffix == ".py"
            and "\\" not in value and not any(ord(character) < 32 for character in value))


if __name__ == "__main__":
    fields = ("repository", "source_workflow_id", "source_workflow_run_id", "candidate_sha",
              "parent_sha", "changed_file", "source_sha256", "test_identity", "status",
              "exit_code", "duration_seconds", "attestation_workflow_id", "attestation_run_id")
    result = create_attestation({name: os.environ.get("ATTEST_" + name.upper(), "") for name in fields},
                                os.environ.get("CODEMENTOR_BEHAVIORAL_ATTESTATION_HMAC_KEY", ""))
    Path(os.environ["GITHUB_OUTPUT_FILE"]).write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")

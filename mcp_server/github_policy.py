"""Fail-closed repository policy for narrowly scoped GitHub actions.

Policy is loaded only from an administrator-selected local JSON file. Nothing in
the PR, repository contents, webhook body, or model response can change it.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import hashlib
import json
import tempfile
import threading
import fcntl
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


POLICY_VERSION = "github-action-policy-v1"
AUTONOMOUS_COMMIT_EXECUTION_ENABLED = False  # J2 hard gate; J3 review required.
_CATEGORIES = frozenset({"style", "naming", "formatting"})
_PROTECTED_DEFAULTS = (".github/", "migrations/", "infra/", "security/", "auth/")
_SENSITIVE_WORDS = frozenset({
    "security", "authentication", "authorization", "cryptography", "crypto",
    "business_logic", "performance", "architecture", "bug", "other", "injection",
    "sql", "xss", "csrf", "ssrf", "unsafe", "vulnerability", "vulnerable",
    "rce", "command execution", "path traversal", "deserialization",
})
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
_POLICY_LOCK = threading.RLock()


def policy_file_path(path: Optional[str] = None) -> Path:
    configured = path or os.getenv("GITHUB_ACTION_POLICY_PATH")
    if configured:
        return Path(configured).expanduser()
    state = Path(os.getenv("AI_CODE_REVIEWER_STATE_DIR", str(
        Path.home() / "Library" / "Application Support" / "AI Code Reviewer" if sys_platform_is_darwin()
        else Path.home() / ".local" / "state" / "ai-code-reviewer"))).expanduser()
    return state / "github-action-policy.json"


def sys_platform_is_darwin() -> bool:
    import sys
    return sys.platform == "darwin"


@dataclass(frozen=True)
class RepositoryActionConfig:
    publish_review_comments: bool = False
    enable_autonomous_fixes: bool = False
    allowed_low_risk_categories: tuple[str, ...] = ()
    confidence_threshold: float = 0.80
    max_changed_lines: int = 3
    protected_paths: tuple[str, ...] = _PROTECTED_DEFAULTS
    permitted_branches: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ("syntax", "static", "behavioral", "ci")
    allow_source_sharing: bool = False
    required_github_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyInput:
    repository: str
    finding: Mapping[str, Any]
    expected_head_sha: str
    current_head_sha: Optional[str]
    changed_lines: Optional[int]
    file_path: str
    confidence: Optional[float] = None
    confidence_source: Optional[str] = None
    confidence_calibrated: bool = False
    syntax_status: str = "not_run"
    static_status: str = "not_run"
    behavioral_status: str = "not_run"
    ci_status: str = "not_run"
    action_authorized: bool = False
    branch: Optional[str] = None
    is_fork: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    eligible: bool
    status: str
    denial_reasons: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_repository_action_config(path: Optional[str] = None) -> dict[str, RepositoryActionConfig]:
    """Load an administrator-owned policy file; any malformed policy disables all writes."""
    policy_path = policy_file_path(path)
    try:
        info = policy_path.stat()
        # Refuse group/world writable config; the service account must own it.
        if not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return {}
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or set(raw) != {"version", "repositories"}:
        return {}
    if raw.get("version") != POLICY_VERSION or not isinstance(raw.get("repositories"), dict):
        return {}
    configs: dict[str, RepositoryActionConfig] = {}
    for identity, value in raw["repositories"].items():
        if not isinstance(identity, str) or not _REPO.fullmatch(identity) or not isinstance(value, dict):
            continue
        config = _parse_repository_config(value)
        if config is not None:
            configs[identity.casefold()] = config
    return configs


def read_policy_document(path: Optional[str] = None) -> tuple[dict[str, Any], str]:
    """Return validated raw policy and a SHA revision for optimistic writes."""
    target = policy_file_path(path)
    try:
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("policy_file_permissions_invalid")
        raw_bytes = target.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
    except FileNotFoundError:
        raw_bytes = b""
        raw = {"version": POLICY_VERSION, "repositories": {}}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("policy_file_unavailable_or_invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"version", "repositories"} or raw.get("version") != POLICY_VERSION or not isinstance(raw.get("repositories"), dict):
        raise ValueError("policy_document_invalid")
    canonical: dict[str, Any] = {}
    seen: set[str] = set()
    for identity, value in raw["repositories"].items():
        if not isinstance(identity, str) or not _REPO.fullmatch(identity) or not isinstance(value, dict) or _parse_repository_config(value) is None:
            raise ValueError("policy_repository_invalid")
        key = identity.casefold()
        if key in seen:
            raise ValueError("policy_repository_duplicate")
        seen.add(key)
        canonical[key] = value
    raw["repositories"] = canonical
    return raw, hashlib.sha256(raw_bytes).hexdigest()


def update_policy_repository(repository: str, config: Mapping[str, Any], *, expected_revision: str,
                             delete: bool = False, path: Optional[str] = None) -> tuple[dict[str, Any], str]:
    """Validate and atomically update one repository in the canonical policy file."""
    if not isinstance(repository, str) or not _REPO.fullmatch(repository):
        raise ValueError("repository_identity_invalid")
    normalized = "/".join(part.lower() for part in repository.split("/"))
    if not delete and (not isinstance(config, Mapping) or _parse_repository_config(dict(config)) is None):
        raise ValueError("repository_policy_invalid")
    target = policy_file_path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = target.parent.stat()
    if parent.st_uid != os.getuid() or not stat.S_ISDIR(parent.st_mode):
        raise ValueError("policy_directory_owner_invalid")
    if parent.st_mode & 0o077:
        os.chmod(target.parent, 0o700)
    lock_path = target.with_suffix(target.suffix + ".lock")
    with _POLICY_LOCK:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        try:
            lock_info = os.fstat(lock_fd)
            if lock_info.st_uid != os.getuid() or not stat.S_ISREG(lock_info.st_mode):
                raise ValueError("policy_lock_owner_invalid")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            raw, current_revision = read_policy_document(str(target))
            if not hmac_compare(current_revision, expected_revision):
                raise ValueError("policy_revision_conflict")
            if delete:
                raw["repositories"].pop(normalized, None)
            else:
                parsed = _parse_repository_config(dict(config))
                assert parsed is not None
                raw["repositories"][normalized] = {
                    "publish_review_comments": parsed.publish_review_comments,
                    "enable_autonomous_fixes": parsed.enable_autonomous_fixes,
                    "allowed_low_risk_categories": list(parsed.allowed_low_risk_categories),
                    "confidence_threshold": parsed.confidence_threshold,
                    "max_changed_lines": parsed.max_changed_lines,
                    "protected_paths": list(parsed.protected_paths),
                    "permitted_branches": list(parsed.permitted_branches),
                    "required_checks": list(parsed.required_checks),
                    "allow_source_sharing": parsed.allow_source_sharing,
                    "required_github_checks": list(parsed.required_github_checks),
                }
            data = (json.dumps(raw, sort_keys=True, indent=2) + "\n").encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(prefix=".github-policy-", dir=target.parent)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                if target.exists():
                    existing = target.lstat()
                    if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
                        raise ValueError("policy_file_owner_invalid")
                os.replace(temporary, target)
                dir_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            revision = hashlib.sha256(data).hexdigest()
            return raw, revision
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left, right)


def _parse_repository_config(raw: dict[str, Any]) -> Optional[RepositoryActionConfig]:
    allowed = {
        "publish_review_comments", "enable_autonomous_fixes", "allowed_low_risk_categories",
        "confidence_threshold", "max_changed_lines", "protected_paths",
        "permitted_branches", "required_checks", "allow_source_sharing", "required_github_checks",
    }
    if set(raw) - allowed:
        return None
    for key in ("publish_review_comments", "enable_autonomous_fixes", "allow_source_sharing"):
        if key in raw and not isinstance(raw[key], bool):
            return None
    categories = raw.get("allowed_low_risk_categories", [])
    if (not isinstance(categories, list) or any(not isinstance(v, str) for v in categories)
            or not set(categories).issubset(_CATEGORIES)):
        return None
    threshold = raw.get("confidence_threshold", 0.80)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0.80 <= threshold <= 1:
        return None
    line_limit = raw.get("max_changed_lines", 3)
    if isinstance(line_limit, bool) or not isinstance(line_limit, int) or not 1 <= line_limit <= 3:
        return None
    def strings(name: str, default: tuple[str, ...]) -> Optional[tuple[str, ...]]:
        value = raw.get(name, list(default))
        if not isinstance(value, list) or any(not isinstance(item, str) or not item or len(item) > 200 for item in value):
            return None
        if any(".." in item.split("/") or "\\" in item or item.startswith("/") for item in value):
            return None
        return tuple(value)
    protected = strings("protected_paths", _PROTECTED_DEFAULTS)
    branches = strings("permitted_branches", ())
    checks = strings("required_checks", ("syntax", "static", "behavioral", "ci"))
    github_checks = strings("required_github_checks", ())
    if protected is None or branches is None or checks is None or github_checks is None:
        return None
    if not set(checks).issubset({"syntax", "static", "behavioral", "ci"}):
        return None
    if not {"syntax", "static", "behavioral", "ci"}.issubset(checks):
        return None
    return RepositoryActionConfig(
        publish_review_comments=raw.get("publish_review_comments", False),
        enable_autonomous_fixes=raw.get("enable_autonomous_fixes", False),
        allowed_low_risk_categories=tuple(categories),
        confidence_threshold=float(threshold),
        max_changed_lines=line_limit,
        protected_paths=tuple(dict.fromkeys((*_PROTECTED_DEFAULTS, *protected))),
        permitted_branches=branches,
        required_checks=checks,
        allow_source_sharing=raw.get("allow_source_sharing", False),
        required_github_checks=github_checks,
    )


def evaluate_finding(configs: Mapping[str, RepositoryActionConfig], item: PolicyInput) -> PolicyDecision:
    """Evaluate comment/fix eligibility; never authorizes autonomous commit execution."""
    repo_key = item.repository.casefold() if isinstance(item.repository, str) else ""
    config = configs.get(repo_key, RepositoryActionConfig())
    finding = item.finding if isinstance(item.finding, Mapping) else {}
    reasons: list[str] = []
    category = finding.get("issue_type")
    origin = finding.get("origin")
    evidence_ids = finding.get("evidence_ids", [])
    finding_id = finding.get("finding_id")
    valid = (
        bool(_REPO.fullmatch(item.repository or ""))
        and isinstance(finding_id, str) and bool(finding_id)
        and isinstance(finding.get("title"), str) and bool(finding["title"].strip())
        and isinstance(finding.get("explanation"), str) and bool(finding["explanation"].strip())
        and isinstance(finding.get("recommended_fix"), str)
        and origin in {"llm", "static"}
        and category in {"bug", "security", "performance", "style", "other", "naming", "formatting"}
        and isinstance(evidence_ids, (list, tuple))
        and all(isinstance(value, str) for value in evidence_ids)
        and (origin != "static" or bool(finding.get("rule_id")))
        and not finding.get("abstained", False)
        and not finding.get("uncertain", False)
    )
    if not valid:
        return PolicyDecision("no_external_action", False, "DENIED", ("invalid_or_insufficient_finding_evidence",),
                              evidence={"finding_id": finding_id, "origin": origin})

    if item.expected_head_sha and not _SHA.fullmatch(item.expected_head_sha):
        reasons.append("invalid_expected_head_sha")
    if not item.current_head_sha or item.current_head_sha != item.expected_head_sha:
        reasons.append("stale_or_unverified_head_sha")
    if _is_protected(item.file_path, (*_PROTECTED_DEFAULTS, *config.protected_paths)):
        reasons.append("protected_path")
    rule_id = finding.get("rule_id")
    finding_text = " ".join(str(finding.get(name, "")) for name in ("title", "explanation", "recommended_fix")).casefold()
    sensitive_mention = any(token.replace("_", " ") in finding_text for token in _SENSITIVE_WORDS)
    severity = finding.get("severity")
    if ((isinstance(severity, str) and severity.casefold() in {"critical", "high"}) or category == "security"
            or (isinstance(rule_id, str) and rule_id.startswith("PYSEC")) or sensitive_mention):
        reasons.append("security_or_high_severity")

    comment_enabled = config.publish_review_comments and item.action_authorized
    if not comment_enabled:
        reasons.append("review_comment_publication_not_authorized")
    if reasons and any(reason in reasons for reason in ("invalid_expected_head_sha", "stale_or_unverified_head_sha", "protected_path")):
        return PolicyDecision("no_external_action", False, "DENIED", tuple(dict.fromkeys(reasons)),
                              evidence=_decision_evidence(item, category, origin, config))

    fix_reasons = list(reasons)
    if not config.enable_autonomous_fixes:
        fix_reasons.append("repository_autonomous_fix_opt_in_disabled")
    if category not in _CATEGORIES or category not in config.allowed_low_risk_categories:
        fix_reasons.append("category_not_allowlisted_low_risk")
    if category in _SENSITIVE_WORDS:
        fix_reasons.append("high_risk_category")
    if not item.confidence_calibrated:
        fix_reasons.append("independently_calibrated_confidence_required")
    if item.confidence is None:
        fix_reasons.append("confidence_unavailable")
    elif isinstance(item.confidence, bool) or not isinstance(item.confidence, (int, float)) or item.confidence < config.confidence_threshold:
        fix_reasons.append("confidence_below_threshold")
    if item.confidence_source not in {"independent_calibration", "validated_static_measurement"}:
        fix_reasons.append("untrusted_confidence_source")
    if item.changed_lines is None or item.changed_lines > config.max_changed_lines:
        fix_reasons.append("changed_line_limit_exceeded_or_unknown")
    if not item.branch or item.branch not in config.permitted_branches:
        fix_reasons.append("branch_not_permitted")
    if item.is_fork:
        fix_reasons.append("fork_write_not_explicitly_authorized")
    if item.syntax_status != "passed":
        fix_reasons.append("syntax_verification_not_passed")
    if item.static_status != "passed":
        fix_reasons.append("static_verification_not_passed")
    for check_name in config.required_checks:
        status = {"behavioral": item.behavioral_status, "ci": item.ci_status,
                  "syntax": item.syntax_status, "static": item.static_status}[check_name]
        if status != "passed":
            fix_reasons.append(f"required_{check_name}_check_not_passed")
    if not item.action_authorized:
        fix_reasons.append("github_action_not_authorized")

    evidence = _decision_evidence(item, category, origin, config)
    if not fix_reasons:
        return PolicyDecision(
            "potential_autonomous_fix", True, "PENDING_REQUIRED_VERIFICATION",
            ("autonomous_commit_execution_disabled_in_j2",), config.required_checks, evidence,
        )
    if comment_enabled:
        return PolicyDecision("review_comment", True, "COMMENT_ELIGIBLE", tuple(dict.fromkeys(fix_reasons)),
                              config.required_checks, evidence)
    return PolicyDecision("human_review", False, "DENIED", tuple(dict.fromkeys(fix_reasons)),
                          config.required_checks, evidence)


def commit_execution_decision(*, repository: str, branch: Optional[str], expected_head_sha: str,
                              current_head_sha: Optional[str], is_fork: bool,
                              protected_branches: tuple[str, ...] = ("main", "master"),
                              gates: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Evaluate commit prerequisites while retaining the permanent J3 hard gate.

    `gates` is explicit evidence from independent verifiers. Missing, unknown,
    or mismatched fields deny eligibility; no LLM may call this function with
    its own confidence claim as evidence.
    """
    reasons: list[str] = []
    if not _REPO.fullmatch(repository or ""):
        reasons.append("invalid_repository_identity")
    if not branch:
        reasons.append("branch_identity_unavailable")
    elif branch in protected_branches:
        reasons.append("protected_branch")
    if not _SHA.fullmatch(expected_head_sha or "") or current_head_sha != expected_head_sha:
        reasons.append("stale_or_unverified_head_sha")
    if is_fork:
        reasons.append("fork_write_not_explicitly_authorized")
    supplied = gates if isinstance(gates, Mapping) else {}
    required_true = (
        "repository_opt_in", "eligible_low_risk_category", "calibration_supported",
        "source_hashes_valid", "syntax_passed", "static_passed",
        "behavioral_passed", "candidate_ci_passed", "scope_valid",
        "write_permission_valid", "durable_record_reserved", "idempotency_reserved",
    )
    for key in required_true:
        if supplied.get(key) is not True:
            reasons.append(f"required_gate_{key}_not_passed")
    confidence = supplied.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence)) or not 0.80 <= confidence <= 1.0):
        reasons.append("independent_confidence_threshold_not_met")
    if supplied.get("changed_lines") is None or isinstance(supplied.get("changed_lines"), bool) or not isinstance(supplied.get("changed_lines"), int) or not 1 <= supplied.get("changed_lines", 0) <= 3:
        reasons.append("three_line_limit_not_verified")
    if supplied.get("candidate_sha") is None or supplied.get("ci_sha") != supplied.get("candidate_sha"):
        reasons.append("candidate_revision_ci_not_verified")
    if supplied.get("unexpected_files") not in ([], (), None):
        reasons.append("unexpected_files_modified")
    # Keep the J2 reason for older consumers while adding the precise J3 gate.
    reasons.extend(("autonomous_commit_execution_disabled_in_j2",
                    "autonomous_commit_execution_hard_disabled_pending_live_staging"))
    return {"status": "AUTONOMOUS_COMMIT_DISABLED", "eligible": False,
            "execution_enabled": AUTONOMOUS_COMMIT_EXECUTION_ENABLED,
            "denial_reasons": list(dict.fromkeys(reasons)), "force_push": False,
            "candidate_revision_required": True}


def _is_protected(path: str, prefixes: tuple[str, ...]) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return True
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return True
    lowered = path.casefold()
    sensitive_suffixes = (".pem", ".key", ".p12", ".pfx", ".env", ".lock")
    basename = parts[-1].casefold()
    if (lowered.endswith(sensitive_suffixes) or basename.startswith((".env", "secret"))
            or basename in {"dockerfile", "makefile", "pyproject.toml", "requirements.txt"}):
        return True
    return any(lowered == prefix.casefold().rstrip("/") or lowered.startswith(prefix.casefold())
               for prefix in prefixes)


def _decision_evidence(item: PolicyInput, category: Any, origin: Any,
                       config: RepositoryActionConfig) -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "repository": item.repository,
        "finding_id": item.finding.get("finding_id") if isinstance(item.finding, Mapping) else None,
        "category": category,
        "origin": origin,
        "expected_head_sha": item.expected_head_sha,
        "current_head_sha": item.current_head_sha,
        "file_path": item.file_path,
        "changed_lines": item.changed_lines,
        "confidence_source": item.confidence_source,
        "confidence_calibrated": item.confidence_calibrated,
        "configured_threshold": config.confidence_threshold,
        "configured_line_limit": config.max_changed_lines,
        "repository_opt_in": config.enable_autonomous_fixes,
    }

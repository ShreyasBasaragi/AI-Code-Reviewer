"""Read-only preflight for an explicitly scoped autonomous-commit test."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

from llm.groq_client import DEFAULT_MODEL, STRICT_STRUCTURED_OUTPUT_MODELS
from llm.provider_settings import PROVIDER_SETTINGS
from mcp_server.credential_store import CREDENTIALS, CredentialStoreError
from mcp_server.github_policy import RepositoryActionConfig, load_repository_action_config


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_REQUIRED_CHECKS = frozenset({"syntax", "static", "behavioral", "ci"})
_CREDENTIAL_NAMES = {
    "github_read_token": "github_token",
    "github_actions_token": "github_actions_token",
    "candidate_staging_token": "github_candidate_staging_token",
    "autonomous_commit_token": "github_autonomous_commit_token",
    "webhook_secret": "webhook_secret",
}


def run_autonomous_commit_preflight(
    repository: str,
    branch: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    config: Optional[RepositoryActionConfig] = None,
    credentials: Any = CREDENTIALS,
    provider_settings: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Report each local prerequisite without exposing secrets or making writes."""
    env = os.environ if environ is None else environ
    if config is None:
        config = load_repository_action_config().get(repository.casefold())
    config = config or RepositoryActionConfig()
    settings = (PROVIDER_SETTINGS.public_snapshot() if provider_settings is None
                else provider_settings)

    checks: list[dict[str, str]] = []

    def record(name: str, passed: bool, failure_code: str) -> None:
        checks.append({"check": name, "status": "passed" if passed else "failed",
                       "failure_code": "" if passed else failure_code})

    record("repository_identity", bool(re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository or "")),
        "repository_identity_invalid")
    record("disposable_branch", isinstance(branch, str) and branch.startswith("test/"),
        "branch_must_be_disposable_test_branch")
    record("repository_autonomous_opt_in", config.enable_autonomous_fixes,
        "repository_autonomous_fixes_not_enabled")
    record("formatting_category_allowlisted", "formatting" in config.allowed_low_risk_categories,
        "formatting_category_not_allowlisted")
    record("branch_permitted_by_repository_policy", branch in config.permitted_branches,
        "branch_not_permitted_by_repository_policy")
    record("source_sharing_policy", config.allow_source_sharing
           and settings.get("source_sharing_consent") is True,
        "groq_source_sharing_not_authorized")

    model = settings.get("groq_model")
    record("groq_review_provider", settings.get("review_provider") == "groq",
        "groq_review_provider_not_selected")
    record("groq_fix_provider", settings.get("fix_provider") == "groq",
        "groq_fix_provider_not_selected")
    record("groq_strict_output_model", model in STRICT_STRUCTURED_OUTPUT_MODELS,
        "groq_model_lacks_approved_strict_schema_support")
    record("groq_api_key", settings.get("groq_api_key_configured") is True,
        "groq_api_key_not_configured")
    record("expected_groq_model", model == DEFAULT_MODEL, "unexpected_groq_model")

    record("prototype_policy_opt_in",
        env.get("GITHUB_MODEL_CONFIDENCE_POLICY", "").strip().casefold() == "academic_prototype",
        "prototype_confidence_policy_not_enabled")
    record("prototype_repository_scope",
        env.get("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_REPOSITORY", "").strip().casefold()
        == repository.casefold(), "prototype_repository_scope_mismatch")
    record("prototype_branch_scope",
        env.get("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_BRANCH", "").strip() == branch,
        "prototype_branch_scope_mismatch")
    record("github_hosted_commit_policy",
        env.get("GITHUB_HOSTED_AUTONOMOUS_COMMIT_POLICY", "").strip().casefold() == "prototype",
        "github_hosted_prototype_policy_not_selected")

    record("candidate_staging_enabled",
        env.get("GITHUB_CANDIDATE_STAGING_ENABLED", "").strip().casefold() in _TRUE_VALUES,
        "candidate_staging_write_switch_disabled")
    record("final_commit_enabled",
        env.get("GITHUB_AUTONOMOUS_COMMIT_ENABLED", "").strip().casefold() in _TRUE_VALUES,
        "final_commit_write_switch_disabled")
    record("candidate_workflow_identity",
        bool(re.fullmatch(r"[0-9]+", env.get("GITHUB_CANDIDATE_WORKFLOW_ID", ""))),
        "candidate_workflow_id_missing_or_invalid")
    record("behavioral_attestation_workflow_identity",
        bool(re.fullmatch(r"[0-9]+", env.get("GITHUB_BEHAVIORAL_ATTESTATION_WORKFLOW_ID", ""))),
        "behavioral_attestation_workflow_id_missing_or_invalid")
    record("behavioral_bundle_identity",
        bool(re.fullmatch(r"[0-9a-f]{64}", env.get("GITHUB_BEHAVIORAL_TEST_BUNDLE_SHA256", ""))),
        "behavioral_test_bundle_sha256_missing_or_invalid")
    record("behavioral_attestation_hmac_key",
        len(env.get("GITHUB_BEHAVIORAL_ATTESTATION_HMAC_KEY", "")) >= 32,
        "behavioral_attestation_hmac_key_missing_or_too_short")
    record("required_candidate_checks",
        bool(config.required_checks) and _REQUIRED_CHECKS.issubset(config.required_checks)
        and bool(config.required_github_checks), "required_candidate_checks_incomplete")

    for check_name, credential_name in _CREDENTIAL_NAMES.items():
        try:
            configured = bool(credentials.get(credential_name))
        except CredentialStoreError:
            record(check_name, False, "macos_keychain_read_failed")
        else:
            record(check_name, configured, f"{credential_name}_not_configured")

    failures = [item for item in checks if item["status"] != "passed"]
    return {
        "ready": not failures,
        "repository": repository,
        "branch": branch,
        "checks": checks,
        "failure_codes": [item["failure_code"] for item in failures],
        "writes_performed": False,
    }

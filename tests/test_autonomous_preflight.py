from mcp_server.autonomous_preflight import run_autonomous_commit_preflight
from mcp_server.credential_store import CredentialStoreError
from mcp_server.github_policy import RepositoryActionConfig


REPOSITORY = "ShreyasBasaragi/AI-Code-Reviewer"
BRANCH = "test/autocommit-final"


class FakeCredentials:
    def get(self, name):
        return f"configured-{name}"


class KeychainReadFailure:
    def get(self, _name):
        raise CredentialStoreError("secure_credential_store_read_failed")


def _environment():
    return {
        "GITHUB_MODEL_CONFIDENCE_POLICY": "academic_prototype",
        "GITHUB_MODEL_CONFIDENCE_PROTOTYPE_REPOSITORY": REPOSITORY,
        "GITHUB_MODEL_CONFIDENCE_PROTOTYPE_BRANCH": BRANCH,
        "GITHUB_HOSTED_AUTONOMOUS_COMMIT_POLICY": "prototype",
        "GITHUB_CANDIDATE_STAGING_ENABLED": "true",
        "GITHUB_AUTONOMOUS_COMMIT_ENABLED": "true",
        "GITHUB_CANDIDATE_WORKFLOW_ID": "423879581",
        "GITHUB_BEHAVIORAL_ATTESTATION_WORKFLOW_ID": "423879573",
        "GITHUB_BEHAVIORAL_TEST_BUNDLE_SHA256": "9a03cb58ad6752b490185ad5da558beb824624e2d0a33efd92ccebcdbcf7e4cb",
        "GITHUB_BEHAVIORAL_ATTESTATION_HMAC_KEY": "h" * 32,
    }


def _config():
    return RepositoryActionConfig(
        enable_autonomous_fixes=True,
        allowed_low_risk_categories=("formatting",),
        confidence_threshold=0.80,
        max_changed_lines=3,
        permitted_branches=(BRANCH,),
        required_checks=("syntax", "static", "behavioral", "ci"),
        required_github_checks=("candidate-syntax", "candidate-static", "candidate-regression"),
        allow_source_sharing=True,
    )


def _provider_settings():
    return {
        "review_provider": "groq",
        "fix_provider": "groq",
        "groq_model": "openai/gpt-oss-20b",
        "groq_api_key_configured": True,
        "source_sharing_consent": True,
    }


def test_preflight_reports_each_prerequisite_and_does_not_write():
    result = run_autonomous_commit_preflight(
        REPOSITORY, BRANCH, environ=_environment(), config=_config(),
        credentials=FakeCredentials(), provider_settings=_provider_settings(),
    )
    assert result["ready"] is True
    assert result["failure_codes"] == []
    assert result["writes_performed"] is False
    assert all(item["status"] == "passed" for item in result["checks"])


def test_preflight_returns_specific_failures_and_reports_keychain_errors():
    env = _environment()
    env["GITHUB_MODEL_CONFIDENCE_PROTOTYPE_BRANCH"] = "development"
    env["GITHUB_BEHAVIORAL_TEST_BUNDLE_SHA256"] = "bad"
    result = run_autonomous_commit_preflight(
        REPOSITORY, BRANCH, environ=env, config=_config(),
        credentials=KeychainReadFailure(), provider_settings=_provider_settings(),
    )
    assert result["ready"] is False
    assert "prototype_branch_scope_mismatch" in result["failure_codes"]
    assert "behavioral_test_bundle_sha256_missing_or_invalid" in result["failure_codes"]
    assert result["failure_codes"].count("macos_keychain_read_failed") == 5
    assert result["writes_performed"] is False

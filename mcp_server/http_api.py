"""FastAPI transport for the existing review and fix lifecycle services."""

from __future__ import annotations

import os
import time
import uuid
import hmac
import stat
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from mcp_server.admin_auth import AdminAuthError, AdminAuthStore, SESSION_TTL_SECONDS
from mcp_server.credential_store import CREDENTIALS, CredentialStore, CredentialStoreError

from fix_lifecycle import record_verification
from mcp_server import orchestrator
from mcp_server.github_pr import GitHubPRException, GitHubPRReviewService
from mcp_server.github_pr import GitHubRESTClient
from mcp_server.github_webhook import (
    GitHubWebhookProcessor,
    WebhookDeliveryStore,
    WebhookError,
    parse_delivery,
    verify_signature,
    verify_status_token,
)
from mcp_server.github_action_coordinator import GitHubActionCoordinator
from mcp_server.state import LifecycleState, PROCESS_STATE
from review_schema import ReviewResponse

MAX_CODE_BYTES = 512 * 1024
DEFAULT_WEBHOOK_BODY_LIMIT = 1024 * 1024


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=200_000)
    language: Literal["python"] = "python"


class FixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["python"] = "python"


class GitHubPRReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class ProviderSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_provider: Optional[Literal["groq", "qwen_local", "qwen"]] = None
    fix_provider: Optional[Literal["groq", "qwen_local", "qwen"]] = None
    groq_model: Optional[str] = Field(default=None, max_length=120)
    groq_api_key: Optional[str] = Field(default=None, max_length=512)
    qwen_base_model: Optional[str] = Field(default=None, max_length=120)
    qwen_adapter_path: Optional[str] = Field(default=None, max_length=1024)
    qwen_quantization: Optional[Literal["none"]] = None
    review_temperature: Optional[float] = None
    fix_temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    source_sharing_consent: Optional[bool] = None


class AdminSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: Optional[str] = Field(default=None, min_length=1, max_length=1024)
    admin_token: Optional[str] = Field(default=None, min_length=32, max_length=512)


class AdminSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bootstrap_credential: str = Field(min_length=24, max_length=256)
    password: str = Field(min_length=12, max_length=1024)
    password_confirmation: str = Field(min_length=12, max_length=1024)


class AdminRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_credential: str = Field(min_length=24, max_length=256)
    password: str = Field(min_length=12, max_length=1024)
    password_confirmation: str = Field(min_length=12, max_length=1024)


class GitHubCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_type: Literal["github_token", "github_actions_token", "github_candidate_staging_token", "github_autonomous_commit_token", "webhook_secret"]
    value: str = Field(min_length=1, max_length=4096)
    persistent: bool = True


class RepositoryPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    publish_review_comments: bool = False
    enable_autonomous_fixes: bool = False
    allowed_low_risk_categories: list[str] = Field(default_factory=list, max_length=3)
    confidence_threshold: float = Field(default=0.80, ge=0.80, le=1.0)
    max_changed_lines: int = Field(default=3, ge=1, le=3)
    protected_paths: list[str] = Field(default_factory=lambda: [".github/", "migrations/", "infra/", "security/", "auth/"], max_length=100)
    permitted_branches: list[str] = Field(default_factory=list, max_length=100)
    required_checks: list[str] = Field(default_factory=lambda: ["syntax", "static", "behavioral", "ci"], max_length=4)
    allow_source_sharing: bool = False
    required_github_checks: list[str] = Field(default_factory=list, max_length=100)


_PR_REVIEW_SLOT = BoundedSemaphore(1)


class ReviewFixService:
    """Thin adapter around the shared orchestrator and shared process cache."""

    def __init__(
        self,
        *,
        llm=None,
        retriever=None,
        state: LifecycleState = PROCESS_STATE,
        provider_name: Optional[str] = None,
        github_pr_service: Optional[GitHubPRReviewService] = None,
    ) -> None:
        self._injected_llm = llm
        self.retriever = retriever
        self.state = state
        self.provider_name = provider_name
        self._provider = llm
        self._fix_provider = llm
        self.github_pr_service = github_pr_service or GitHubPRReviewService()

    def configured_provider_name(self, task: str = "review") -> str:
        if task not in {"review", "fix"}:
            raise ValueError("Provider task must be 'review' or 'fix'.")
        if task == "review" and self.provider_name:
            name = self.provider_name.strip().casefold()
        elif task == "fix" and self.provider_name and not os.getenv("AI_CODE_REVIEWER_FIX_PROVIDER"):
            name = self.provider_name.strip().casefold()
        else:
            from llm.provider_settings import PROVIDER_SETTINGS

            name = PROVIDER_SETTINGS.public_snapshot()[f"{task}_provider"]
        return "qwen_local" if name == "qwen" else name

    @property
    def provider_initialized(self) -> bool:
        return self._provider is not None or self._fix_provider is not None

    def _get_provider(self, task: str = "review"):
        attribute = "_provider" if task == "review" else "_fix_provider"
        if self._injected_llm is not None:
            return self._injected_llm
        from llm.provider_factory import get_configured_provider, get_provider
        from llm.provider_settings import PROVIDER_SETTINGS, ProviderSettingsError

        if (self.configured_provider_name(task) == "groq"
                and not PROVIDER_SETTINGS.public_snapshot()["source_sharing_consent"]):
            raise ProviderSettingsError("source_sharing_not_authorized")

        if self.provider_name:
            current = getattr(self, attribute)
            if current is not None:
                return current
            provider = get_provider(self.configured_provider_name(task))
        else:
            # Resolve one immutable settings snapshot per request. The factory
            # caches by settings generation so later requests see authorized
            # changes while an in-flight provider call keeps its own instance.
            provider = get_configured_provider(task)
        setattr(self, attribute, provider)
        return provider

    def provider_identity(self, task: str = "review") -> str:
        provider = getattr(self, "_provider" if task == "review" else "_fix_provider")
        if self._injected_llm is not None:
            return f"injected:{type(provider).__name__}" if provider is not None else "injected:configured"
        if provider is not None:
            model = getattr(provider, "model", None)
            if isinstance(model, str) and model:
                return f"{self.configured_provider_name(task)}:{model}"
            config = getattr(provider, "config", None)
            model_id = getattr(config, "base_model_id", None)
            if isinstance(model_id, str) and model_id:
                return f"{self.configured_provider_name(task)}:{model_id}"
        return self.configured_provider_name(task)

    def submit_review(self, code: str) -> tuple[str, ReviewResponse]:
        review_id, review, _timings = self.submit_review_with_timings(code)
        return review_id, review

    def submit_review_with_timings(self, code: str) -> tuple[str, ReviewResponse, dict[str, float]]:
        request_started = time.perf_counter()
        self._validate_code(code)
        timings = {"request_validation_seconds": time.perf_counter() - request_started}
        provider = self._get_provider("review")
        result = orchestrator.run_review_pipeline(
            code=code,
            file_path="submitted.py",
            notify_desktop=False,
            llm=provider,
            retriever=self.retriever,
            phase_timings=timings,
        )
        review_id = uuid.uuid4().hex
        self.state.put_review(review_id, code, result.review_response)
        timings["total_request_seconds"] = time.perf_counter() - request_started
        return review_id, result.review_response, dict(timings)

    def review_github_pr(self, url: str, *, expected_owner: Optional[str] = None,
                         expected_repository: Optional[str] = None,
                         expected_number: Optional[int] = None,
                         expected_base_sha: Optional[str] = None,
                         expected_head_sha: Optional[str] = None):
        # Preserve an explicitly injected PR reviewer for offline tests and
        # deployments that supply their own review adapter. Production defaults
        # to the existing orchestrator path through _review_pr_file.
        from mcp_server.github_policy import load_repository_action_config
        from mcp_server.github_pr import parse_pull_request_url

        injected = self.github_pr_service.reviewer
        ref = parse_pull_request_url(url)
        repository = f"{ref.owner}/{ref.repository}".casefold()
        provider_name = self.configured_provider_name("review")
        configs = load_repository_action_config()
        if not injected and self._injected_llm is None and provider_name == "groq" and not bool(
                configs.get(repository) and configs[repository].allow_source_sharing):
            raise GitHubPRException("source_sharing_not_authorized", 403,
                                    "Administrator source-sharing consent is required for hosted PR review.")
        # One provider object is captured for the full PR review so an admin
        # settings change cannot split files from one review across providers.
        provider = None if injected else self._get_provider("review")
        callback = injected or (lambda code, path: self._review_pr_file(code, path, provider))
        result = self.github_pr_service.analyze(
            url, reviewer=callback,
            context_reviewer=None if injected else (
                lambda code, path, evidence: self._review_pr_file_with_context(code, path, evidence, provider)
            ),
            expected_owner=expected_owner, expected_repository=expected_repository,
            expected_number=expected_number, expected_base_sha=expected_base_sha,
            expected_head_sha=expected_head_sha,
        )
        if injected:
            result.provider_identity = "injected:github_reviewer"
        else:
            result.provider_identity = self.provider_identity("review")
        return result

    def _review_pr_file(self, code: str, file_path: str, provider=None) -> ReviewResponse:
        result = orchestrator.run_review_pipeline(
            code=code,
            file_path=file_path,
            notify_desktop=False,
            llm=provider or self._get_provider("review"),
            retriever=self.retriever,
        )
        return result.review_response

    def _review_pr_file_with_context(self, code: str, file_path: str,
                                     supplemental_evidence, provider=None) -> ReviewResponse:
        result = orchestrator.run_review_pipeline(
            code=code,
            file_path=file_path,
            notify_desktop=False,
            llm=provider or self._get_provider("review"),
            retriever=self.retriever,
            supplemental_evidence=supplemental_evidence,
        )
        return result.review_response

    def get_review(self, review_id: str):
        return self.state.get_review(review_id)

    def generate_fix(self, review_id: str, finding_id: str, language: str = "python"):
        handle = self.state.get_review(review_id)
        if handle is None:
            raise LookupError("review_not_found")
        candidate = orchestrator.generate_fix_candidate(
            review_response=handle.response,
            finding_ref=finding_id,
            original_code=handle.code,
            language=language,
            llm=self._get_provider("fix"),
        )
        if candidate.generation_status == "generated":
            from dataclasses import replace
            candidate = replace(candidate, provider_identity=self.provider_identity("fix"), review_id=review_id)
        if candidate.generation_status == "generated":
            self.state.put_candidate(candidate)
        return candidate

    def get_candidate(self, candidate_id: str):
        return self.state.get_candidate(candidate_id)

    def verify(self, candidate_id: str):
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError("candidate_not_found")
        report = orchestrator.verify_fix_candidate(
            candidate, expected_candidate_id=candidate_id
        )
        self.state.put_candidate(record_verification(candidate, report))
        return report

    def accept(self, candidate_id: str, *, actor_id: str):
        return self._approval(candidate_id, orchestrator.accept_fix, actor_id=actor_id)

    def reject(self, candidate_id: str, *, actor_id: str):
        return self._approval(candidate_id, orchestrator.reject_fix, actor_id=actor_id)

    def _approval(self, candidate_id: str, action, *, actor_id: str):
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError("candidate_not_found")
        result = action(candidate, actor_id=actor_id)
        self.state.put_candidate(result.candidate)
        return result

    def export(self, candidate_id: str):
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError("candidate_not_found")
        return orchestrator.export_fix(candidate)

    @staticmethod
    def _validate_code(code: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code_required")
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise OverflowError("code_too_large")


def _review_payload(review: ReviewResponse) -> dict:
    # Exclude raw model text and internal exception messages from HTTP responses.
    return {
        "valid": review.valid,
        "summary": review.summary,
        "findings": [asdict(item) for item in review.findings],
        "evidence": [asdict(item) for item in review.evidence],
        "processing_errors": ["review_validation_failed"] if not review.valid else [],
        "static_analysis_status": review.static_analysis_status,
        "static_analysis_error": review.static_analysis_error,
    }


def _candidate_payload(candidate) -> dict:
    payload = candidate.to_dict()
    payload.pop("raw_response", None)
    return payload


def create_app(service: Optional[ReviewFixService] = None,
               webhook_processor: Optional[GitHubWebhookProcessor] = None,
               admin_auth: Optional[AdminAuthStore] = None,
               credentials: Optional[CredentialStore] = None) -> FastAPI:
    """Create the HTTP API; provider/model construction remains lazy."""
    lifecycle = service or ReviewFixService()
    auth_store = admin_auth or AdminAuthStore()
    credential_store = credentials or CREDENTIALS
    delivery_store = WebhookDeliveryStore.from_environment()
    action_coordinator = GitHubActionCoordinator(
        delivery_store,
        read_client=getattr(getattr(lifecycle, "github_pr_service", None), "client", None),
    )
    from mcp_server.autonomous_fixes import AutonomousFixExecutor
    from mcp_server.behavioral_verification import (
        BehavioralVerifier, GitHubActionsBehavioralVerifier, RemoteHTTPSExecutionBroker,
    )

    def _confidence_for_finding(finding):
        from mcp_server.confidence import load_runtime_confidence
        calibration_path = os.getenv("AI_CODE_REVIEWER_RUNTIME_CALIBRATION_PATH")
        dataset_id = os.getenv("AI_CODE_REVIEWER_CALIBRATION_DATASET_ID")
        model_id = lifecycle.provider_identity("review")
        if not calibration_path or not dataset_id:
            return None
        return load_runtime_confidence(
            calibration_path, model_id=model_id, dataset_id=dataset_id,
            finding_category=str(finding.get("issue_type", "")),
        )

    def _generate_autonomous_fix(review_response, finding_id, source):
        from mcp_server import orchestrator
        return orchestrator.generate_fix_candidate(
            review_response, finding_id, source, llm=lifecycle._get_provider("fix"),
        )

    def _assess_static_finding_confidence(finding, source):
        provider = lifecycle._get_provider("review")
        assess = getattr(provider, "assess_static_finding_confidence", None)
        if not callable(assess):
            return None
        return assess(source, finding)

    def _behavioral_tests(_path):
        test_path = os.getenv("AI_CODE_REVIEWER_BEHAVIORAL_TEST_BUNDLE")
        if not test_path:
            return None
        target = Path(test_path).expanduser()
        try:
            info = target.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 2 * 1024 * 1024):
                return None
            return target.read_bytes()
        except OSError:
            return None

    broker = (RemoteHTTPSExecutionBroker()
              if os.getenv("AI_CODE_REVIEWER_SANDBOX_URL") and os.getenv("AI_CODE_REVIEWER_SANDBOX_HMAC_KEY")
              else None)
    github_attestation_workflow = os.getenv("GITHUB_BEHAVIORAL_ATTESTATION_WORKFLOW_ID", "")
    github_bundle_identity = os.getenv("GITHUB_BEHAVIORAL_TEST_BUNDLE_SHA256", "")
    github_hmac_key = os.getenv("GITHUB_BEHAVIORAL_ATTESTATION_HMAC_KEY", "")
    github_candidate_workflow = os.getenv("GITHUB_CANDIDATE_WORKFLOW_ID", "")
    provider_choice = os.getenv("GITHUB_BEHAVIORAL_VERIFICATION_PROVIDER", "").strip().casefold()
    github_configured = bool(github_attestation_workflow and github_bundle_identity and github_hmac_key
                             and github_candidate_workflow)
    remote_configured = broker is not None
    selected_provider = provider_choice or (
        "github_actions" if github_configured and not remote_configured else
        "remote_https" if remote_configured and not github_configured else "")
    github_verifier = None
    if selected_provider == "github_actions" and github_configured:
        def _fetch_behavioral_attestation(**kwargs):
            client = getattr(action_coordinator.adapter, "read_client", None)
            fetch_attestation = getattr(client, "get_behavioral_attestation", None)
            if not callable(fetch_attestation):
                raise RuntimeError("github_behavioral_attestation_reader_unavailable")
            owner_name, separator, repository_name = str(kwargs.get("repository", "")).partition("/")
            if not separator or not owner_name or not repository_name:
                raise ValueError("github_behavioral_repository_identity_invalid")
            return fetch_attestation(
                owner=owner_name, repository=repository_name,
                source_workflow_id=kwargs["source_workflow_id"],
                source_workflow_run_id=kwargs["source_workflow_run_id"],
                candidate_commit_sha=kwargs["candidate_commit_sha"],
                attestation_workflow_id=kwargs["attestation_workflow_id"],
            )
        github_verifier = GitHubActionsBehavioralVerifier(
            _fetch_behavioral_attestation,
            key=github_hmac_key,
            expected_test_identity=github_bundle_identity,
            attestation_workflow_id=github_attestation_workflow,
        )
    remote_verifier = (BehavioralVerifier(broker)
                       if selected_provider == "remote_https" and remote_configured else None)
    action_coordinator.autonomous_executor = AutonomousFixExecutor(
        store=delivery_store, adapter=action_coordinator.adapter,
        fix_generator=_generate_autonomous_fix,
        confidence_resolver=_confidence_for_finding,
        confidence_assessor=_assess_static_finding_confidence,
        behavioral_verifier=remote_verifier,
        github_behavioral_verifier=github_verifier,
        github_hosted_commit_policy=os.getenv("GITHUB_HOSTED_AUTONOMOUS_COMMIT_POLICY", ""),
        behavioral_tests=_behavioral_tests,
    )

    def process_webhook_event(event):
        _apply_github_credentials(lifecycle, credential_store, action_coordinator, include_action=True)
        from mcp_server.github_policy import load_repository_action_config
        action_coordinator.configs = load_repository_action_config()
        repository = f"{event.get('owner', '')}/{event.get('repository', '')}"
        if delivery_store.is_codementor_commit(repository, str(event.get("head_sha", ""))):
            # A confirmed CodeMentor-authored commit may generate a
            # synchronize event. Persist a terminal suppression result rather
            # than recursively reviewing and generating another fix.
            return {"repository": repository, "number": event.get("number"),
                    "head_sha": event.get("head_sha"), "completion_status": "complete",
                    "self_generated_commit_suppressed": True, "files": [],
                    "action_processing": {"mode": "suppressed_self_generated_commit", "actions": []}}
        return _run_webhook_review(lifecycle, event, action_coordinator)

    processor = webhook_processor or GitHubWebhookProcessor(
        delivery_store,
        process_webhook_event,
    )

    @asynccontextmanager
    async def lifespan(_app):
        bootstrap = auth_store.startup()
        # Fail startup before accepting traffic if durable application state is
        # unavailable. Both stores open SQLite connections per operation and
        # close them in their connection context managers; no connection is
        # retained by the application lifespan.
        if lifecycle.state.persistence_store is not None:
            lifecycle.state.persistence_store.initialize()
        delivery_store.initialize()
        try:
            _apply_github_credentials(lifecycle, credential_store, action_coordinator, include_action=True)
        except HTTPException:
            # Keychain read errors must not prevent the read-only application
            # from starting, and must never leave environment-initialized write
            # tokens usable. A later successful credential update/webhook can
            # populate the adapter through the normal credential path.
            _clear_github_credentials(lifecycle, action_coordinator)
        _print_startup_summary(auth_store, credential_store, bootstrap)
        try:
            webhook_configured = bool(credential_store.get("webhook_secret"))
        except CredentialStoreError:
            webhook_configured = False
        if webhook_configured:
            processor.start()
        try:
            yield
        finally:
            processor.stop()

    app = FastAPI(title="AI Code Reviewer API", version="1.0.0", lifespan=lifespan)
    login_attempts: dict[str, list[float]] = {}
    login_lock = RLock()

    @app.exception_handler(RequestValidationError)
    async def sanitize_admin_validation_errors(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/admin/"):
            # Pydantic's default validation detail may echo the rejected input,
            # which could include a credential sent with this request.
            return JSONResponse(status_code=422, content={"detail": "provider_settings_invalid"})
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    origins = [
        origin.strip()
        for origin in os.getenv(
            "AI_CODE_REVIEWER_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip() and origin.strip() != "*"
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

    def require_admin(request: Request) -> str:
        cookie = request.cookies.get("acr_admin_session")
        if auth_store.valid_session(cookie):
            if request.method not in {"GET", "HEAD"} and request.headers.get("origin") not in origins:
                raise HTTPException(status_code=403, detail="admin_origin_not_allowed")
            return "administrator_session"
        configured = os.getenv("AI_CODE_REVIEWER_ADMIN_TOKEN")
        supplied = request.headers.get("authorization", "")
        if configured and len(configured) >= 32 and hmac.compare_digest(supplied, "Bearer " + configured):
            return "administrator_legacy_bearer"
        raise HTTPException(status_code=401, detail="provider_settings_unauthorized")

    def require_loopback_peer(request: Request) -> str:
        host = (request.client.host if request.client else "").casefold()
        # With uvicorn's proxy-header middleware enabled, a trusted reverse
        # proxy's sanitized X-Forwarded-For value is reflected in
        # request.client.host. Allow it only when the effective peer is still
        # loopback; an untrusted forwarded header cannot turn a remote peer into
        # a loopback peer because uvicorn ignores headers from untrusted peers.
        # This keeps first-run setup usable behind the loopback-bound deployment
        # proxy while rejecting remote clients and spoofed headers in tests.
        has_forwarded_peer = any(
            request.headers.get(header)
            for header in ("forwarded", "x-forwarded-for", "x-real-ip")
        )
        if has_forwarded_peer and host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(status_code=403, detail="forwarded_peer_headers_not_allowed_for_local_setup")
        request_host = (request.url.hostname or "").casefold()
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail="local_administrator_action_required")
        if host != "testclient" and request_host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(status_code=403, detail="local_administrator_action_required")
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            raise HTTPException(status_code=403, detail="admin_origin_not_allowed")
        return host or "loopback"

    def set_admin_cookie(request: Request, token: str, ttl: int = SESSION_TTL_SECONDS) -> JSONResponse:
        result = JSONResponse({"authenticated": True, "expires_in_seconds": ttl})
        result.set_cookie("acr_admin_session", token, max_age=ttl, httponly=True,
                          secure=request.url.scheme == "https", samesite="strict",
                          path="/")
        return result

    @app.post("/api/admin/setup")
    def complete_admin_setup(payload: AdminSetupRequest, request: Request):
        peer = require_loopback_peer(request)
        if payload.password != payload.password_confirmation:
            raise HTTPException(status_code=422, detail="password_confirmation_mismatch")
        try:
            auth_store.complete_setup(payload.bootstrap_credential, payload.password)
            token, ttl = auth_store.login(payload.password, peer)
            return set_admin_cookie(request, token, ttl)
        except AdminAuthError as exc:
            code = str(exc)
            status = 409 if code == "admin_setup_already_complete" else 401 if code.startswith("bootstrap_") else 422
            raise HTTPException(status_code=status, detail=code) from exc

    @app.post("/api/admin/password-reset")
    def reset_admin_password(payload: AdminRecoveryRequest, request: Request):
        peer = require_loopback_peer(request)
        if payload.password != payload.password_confirmation:
            raise HTTPException(status_code=422, detail="password_confirmation_mismatch")
        try:
            auth_store.reset_password(payload.recovery_credential, payload.password)
            token, ttl = auth_store.login(payload.password, peer)
            return set_admin_cookie(request, token, ttl)
        except AdminAuthError as exc:
            code = str(exc)
            status = 401 if code.startswith("recovery_") else 422
            raise HTTPException(status_code=status, detail=code) from exc
        return "bearer"

    @app.post("/api/admin/session")
    def create_admin_session(payload: AdminSessionRequest, request: Request):
        if bool(payload.password) == bool(payload.admin_token):
            raise HTTPException(status_code=422, detail="provide_exactly_one_admin_credential")
        peer = (request.client.host if request.client else "unknown").casefold()
        loopback = peer in {"127.0.0.1", "::1", "localhost", "testclient"}
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            raise HTTPException(status_code=403, detail="admin_origin_not_allowed")
        if request.url.scheme != "https" and not loopback:
            raise HTTPException(status_code=403, detail="admin_session_requires_https")
        now = time.monotonic()
        key = peer
        with login_lock:
            recent = [stamp for stamp in login_attempts.get(key, []) if now - stamp < 60]
            if len(recent) >= 5:
                raise HTTPException(status_code=429, detail="admin_login_rate_limited")
            recent.append(now)
            login_attempts[key] = recent
        if payload.password:
            try:
                token, ttl = auth_store.login(payload.password, peer)
                return set_admin_cookie(request, token, ttl)
            except AdminAuthError as exc:
                code = str(exc)
                status = 429 if code == "admin_login_rate_limited" else 503 if code == "admin_setup_required" else 401
                raise HTTPException(status_code=status, detail=code) from exc
        configured = os.getenv("AI_CODE_REVIEWER_ADMIN_TOKEN")
        if payload.admin_token and configured and len(configured) >= 32 and hmac.compare_digest(payload.admin_token, configured):
            # Time-limited compatibility path during migration only.
            token, ttl = auth_store.create_session()
            return set_admin_cookie(request, token, ttl)
        raise HTTPException(status_code=401, detail="provider_settings_unauthorized")

    @app.get("/api/admin/session")
    def get_admin_session(request: Request):
        authenticated = auth_store.valid_session(request.cookies.get("acr_admin_session"))
        configured = os.getenv("AI_CODE_REVIEWER_ADMIN_TOKEN")
        return {"authenticated": authenticated, "expires_in_seconds": SESSION_TTL_SECONDS if authenticated else 0,
                "setup_required": not auth_store.setup_complete(),
                "legacy_token_available": bool(configured and len(configured) >= 32)}

    @app.delete("/api/admin/session")
    def delete_admin_session(request: Request):
        if request.cookies.get("acr_admin_session") and request.headers.get("origin") not in origins:
            raise HTTPException(status_code=403, detail="admin_origin_not_allowed")
        auth_store.revoke_session(request.cookies.get("acr_admin_session"))
        result = JSONResponse({"authenticated": False})
        result.delete_cookie("acr_admin_session", path="/", httponly=True,
                             secure=request.url.scheme == "https", samesite="strict")
        return result

    @app.get("/api/admin/provider-settings")
    def get_provider_settings(request: Request):
        require_admin(request)
        from llm.provider_settings import PROVIDER_SETTINGS

        return PROVIDER_SETTINGS.public_snapshot()

    @app.put("/api/admin/provider-settings")
    def update_provider_settings(settings: ProviderSettingsRequest, request: Request):
        require_admin(request)
        from llm.provider_settings import PROVIDER_SETTINGS, ProviderSettingsError

        try:
            snapshot = PROVIDER_SETTINGS.update(settings.model_dump(exclude_unset=True))
            from llm.provider_factory import invalidate_provider_cache

            invalidate_provider_cache()
            return snapshot
        except ProviderSettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/admin/provider-options")
    def provider_options(request: Request):
        require_admin(request)
        from llm.groq_client import DEFAULT_MODEL, STRICT_STRUCTURED_OUTPUT_MODELS
        from llm.qwen_peft import DEFAULT_ADAPTER_DIR, DEFAULT_BASE_MODEL_ID
        from llm.provider_settings import PROVIDER_SETTINGS
        return {
            "groq_models": sorted(STRICT_STRUCTURED_OUTPUT_MODELS),
            "groq_model_source": "server_approved_structured_output_models",
            "qwen_base_model": DEFAULT_BASE_MODEL_ID,
            "qwen_adapter_path": PROVIDER_SETTINGS.public_snapshot()["qwen_adapter_path"] or str(DEFAULT_ADAPTER_DIR),
            "qwen_quantization": ["none"],
            "review_providers": ["groq", "qwen_local"],
            "fix_providers": ["groq", "qwen_local"],
            "default_groq_model": DEFAULT_MODEL,
        }

    @app.post("/api/admin/provider-connectivity")
    def provider_connectivity(request: Request):
        require_admin(request)
        from llm.provider_settings import PROVIDER_SETTINGS
        settings = PROVIDER_SETTINGS.task_snapshot("review")
        if settings["review_provider"] == "groq" or settings["fix_provider"] == "groq":
            if not settings.get("api_key"):
                groq_result = {"status": "failed", "message": "Groq API key is not configured."}
                snapshot = PROVIDER_SETTINGS.public_snapshot()
                return {"groq": groq_result,
                        "qwen": {"status": "passed" if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "failed",
                                 "message": "Approved adapter artifacts are present; weights were not loaded." if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "Approved adapter artifacts are missing."}}
            try:
                from groq import Groq
                response = Groq(api_key=settings["api_key"], max_retries=0, timeout=10).models.list()
                available = {item.id for item in getattr(response, "data", []) if isinstance(getattr(item, "id", None), str)}
                from llm.groq_client import STRICT_STRUCTURED_OUTPUT_MODELS
                approved = sorted(available.intersection(STRICT_STRUCTURED_OUTPUT_MODELS))
                selected_available = settings["groq_model"] in approved
                snapshot = PROVIDER_SETTINGS.public_snapshot()
                return {"groq": {"status": "passed" if selected_available else "failed",
                                 "message": "Groq account reachable; selected approved structured-output model is available." if selected_available else "Groq account reachable, but the selected approved structured-output model was not listed.",
                                 "available_approved_models": approved},
                        "qwen": {"status": "passed" if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "failed",
                                 "message": "Approved adapter artifacts are present; weights were not loaded." if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "Approved adapter artifacts are missing."}}
            except Exception:
                snapshot = PROVIDER_SETTINGS.public_snapshot()
                return {"groq": {"status": "failed", "message": "Groq connectivity check failed. Check the server key and network."},
                        "qwen": {"status": "passed" if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "failed",
                                 "message": "Approved adapter artifacts are present; weights were not loaded." if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "Approved adapter artifacts are missing."}}
        from llm.provider_settings import PROVIDER_SETTINGS
        snapshot = PROVIDER_SETTINGS.public_snapshot()
        return {"groq": {"status": "not_checked", "message": "Groq is not selected."},
                "qwen": {"status": "passed" if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "failed",
                         "message": "Approved Qwen adapter artifacts are present; model weights were not loaded." if snapshot["qwen_readiness"] == "artifacts_present_not_loaded" else "Approved Qwen adapter artifacts are missing."}}

    @app.get("/api/admin/github/status")
    def get_github_status(request: Request):
        require_admin(request)
        return credential_store.status()

    @app.put("/api/admin/github/credentials")
    def set_github_credential(payload: GitHubCredentialRequest, request: Request):
        require_admin(request)
        try:
            storage = credential_store.set(payload.credential_type, payload.value, persistent=payload.persistent)
            _apply_github_credentials(lifecycle, credential_store, action_coordinator, include_action=True)
            auth_store.record_audit("github_credential_updated", {
                "credential_type": payload.credential_type, "storage": storage,
            })
            if payload.credential_type == "webhook_secret":
                processor.start()
            return {"updated": True, "credential_type": payload.credential_type,
                    "storage": storage, **credential_store.status()}
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503 if "unavailable" in str(exc) else 422,
                                detail=str(exc)) from exc

    @app.delete("/api/admin/github/credentials/{credential_type}")
    def remove_github_credential(credential_type: str, request: Request):
        require_admin(request)
        try:
            result = credential_store.remove(credential_type)
            _apply_github_credentials(lifecycle, credential_store, action_coordinator, include_action=True)
            auth_store.record_audit("github_credential_removed", {"credential_type": credential_type})
            if credential_type == "webhook_secret":
                processor.stop()
            return {"removed": result == "removed", **credential_store.status()}
        except CredentialStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/admin/github/test")
    def test_github_connection(request: Request):
        require_admin(request)
        try:
            token = credential_store.get("github_token")
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail="github_credential_store_unavailable") from exc
        if not token:
            return {"status": "failed", "error_code": "github_token_not_configured",
                    "message": "Configure a GitHub read token before testing the connection."}
        client = GitHubRESTClient(token=token)
        try:
            account = client.get_authenticated_user()
            return {"status": "passed", "account": account}
        except GitHubPRException as exc:
            return {"status": "failed", "error_code": exc.code, "message": exc.message}

    @app.get("/api/admin/github/repositories")
    def list_github_repositories(request: Request):
        require_admin(request)
        try:
            token = credential_store.get("github_token")
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail="github_credential_store_unavailable") from exc
        if not token:
            raise HTTPException(status_code=409, detail="github_token_not_configured")
        client = GitHubRESTClient(token=token)
        try:
            repositories = client.list_accessible_repositories()
            configs = __import__("mcp_server.github_policy", fromlist=["load_repository_action_config"]).load_repository_action_config()
            return {"repositories": [{**repo,
                    "policy_configured": repo["full_name"].casefold() in configs,
                    "policy": _policy_config_payload(configs.get(repo["full_name"].casefold()))}
                    for repo in repositories]}
        except GitHubPRException as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    @app.get("/api/admin/github/repositories/{owner}/{repository}/policy")
    def get_repository_policy(owner: str, repository: str, request: Request):
        require_admin(request)
        identity = _validate_repository_identity(owner, repository)
        from mcp_server.github_policy import read_policy_document, _parse_repository_config
        try:
            document, revision = read_policy_document()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        config = _policy_config_payload(_parse_repository_config(document["repositories"].get(identity.casefold(), {})))
        return {"repository": identity, "revision": revision, "policy": config,
                "autonomous_commits": "implemented_off_by_default_requires_independent_gates"}

    @app.put("/api/admin/github/repositories/{owner}/{repository}/policy")
    def update_repository_policy(owner: str, repository: str, payload: RepositoryPolicyRequest,
                                 request: Request):
        require_admin(request)
        identity = _validate_repository_identity(owner, repository)
        try:
            token = credential_store.get("github_token")
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail="github_credential_store_unavailable") from exc
        if not token:
            raise HTTPException(status_code=409, detail="github_token_not_configured")
        github = GitHubRESTClient(token=token)
        try:
            github.get_repository(owner, repository)
        except GitHubPRException as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        from mcp_server.github_policy import load_repository_action_config, update_policy_repository
        values = payload.model_dump(exclude={"revision"})
        try:
            _document, revision = update_policy_repository(identity, values, expected_revision=payload.revision)
            action_coordinator.configs = load_repository_action_config()
            auth_store.record_audit("github_repository_policy_updated", {
                "repository": identity.casefold(), "policy_revision": revision,
                "allow_source_sharing": payload.allow_source_sharing,
                "publish_review_comments": payload.publish_review_comments,
            })
            return {"repository": identity, "revision": revision, "policy": values,
                    "autonomous_commits": "implemented_off_by_default_requires_independent_gates"}
        except ValueError as exc:
            code = str(exc)
            raise HTTPException(status_code=409 if code.endswith("conflict") else 422, detail=code) from exc

    @app.delete("/api/admin/github/repositories/{owner}/{repository}/policy")
    def delete_repository_policy(owner: str, repository: str, revision: str, request: Request):
        require_admin(request)
        identity = _validate_repository_identity(owner, repository)
        try:
            token = credential_store.get("github_token")
            if not token:
                raise HTTPException(status_code=409, detail="github_token_not_configured")
            GitHubRESTClient(token=token).get_repository(owner, repository)
        except GitHubPRException as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail="github_credential_store_unavailable") from exc
        from mcp_server.github_policy import load_repository_action_config, update_policy_repository
        try:
            _document, new_revision = update_policy_repository(identity, {}, expected_revision=revision, delete=True)
            action_coordinator.configs = load_repository_action_config()
            auth_store.record_audit("github_repository_policy_removed", {"repository": identity.casefold()})
            return {"repository": identity, "revision": new_revision, "removed": True}
        except ValueError as exc:
            code = str(exc)
            raise HTTPException(status_code=409 if code.endswith("conflict") else 422, detail=code) from exc

    @app.get("/api/admin/github/webhook")
    def get_webhook_settings(request: Request):
        require_admin(request)
        from urllib.parse import urlsplit
        endpoint = os.getenv("GITHUB_WEBHOOK_PUBLIC_URL")
        if not endpoint:
            base = os.getenv("AI_CODE_REVIEWER_API_URL", "http://127.0.0.1:8000").rstrip("/")
            endpoint = base + "/api/github/webhook"
        host = (urlsplit(endpoint).hostname or "").casefold()
        localhost = host in {"127.0.0.1", "::1", "localhost"}
        try:
            recent = delivery_store.list_recent(limit=20)
        except Exception:
            recent = []
        last_authenticated = recent[0] if recent else None
        last_successful = next((item for item in recent if item.get("status") in {"completed", "partial"}), None)
        try:
            secret_status = bool(credential_store.get("webhook_secret"))
        except CredentialStoreError:
            secret_status = None
        reachability = ("localhost_only" if localhost else
                        "reachable_signed_delivery_received" if last_authenticated else "not_verified")
        return {"endpoint_url": endpoint, "secret_configured": secret_status,
                "required_events": ["pull_request"],
                "public_reachability": reachability,
                "last_authenticated_delivery": last_authenticated,
                "last_successful_delivery": last_successful,
                "recent_deliveries": recent,
                "delivery_authentication": "HMAC SHA-256 signature required"}

    @app.get("/api/admin/audit")
    def get_admin_audit(request: Request):
        require_admin(request)
        return {"events": auth_store.audit_events(100)}

    @app.get("/api/admin/webhook/deliveries")
    def get_admin_webhook_deliveries(request: Request, limit: int = 25):
        require_admin(request)
        rows = processor.store.list_recent(limit=max(1, min(limit, 50)))
        retryable_delivery_codes = {
            "github_timeout", "github_network_error", "github_rate_limited",
            "github_unavailable", "worker_interrupted_unknown_completion",
        }
        retryable_action_codes = {
            "github_rate_limited", "reconciled_comment_absent_retry_requires_operator",
        }
        for row in rows:
            actions = processor.store.get_actions(row["delivery_id"])
            safe_actions = []
            for item in actions:
                status = item["publication_status"]
                failure = item.get("failure_code")
                retry_after = item.get("next_retry_at")
                pending_operator_retry = (
                    status == "pending" and bool(item.get("last_operator"))
                    and isinstance(retry_after, (int, float))
                )
                retry_available = bool(
                    item["action_type"] == "review_comment" and item["attempts"] < 3
                    and (
                        status == "failed" and failure in retryable_action_codes
                        or pending_operator_retry and retry_after <= time.time()
                    )
                )
                safe_actions.append({
                    "action_id": item["action_id"], "action_type": item["action_type"],
                    "publication_status": status, "attempts": item["attempts"],
                    "failure_code": failure, "github_comment_id": item["github_comment_id"],
                    "commit_sha": item.get("commit_sha"),
                    "verification_status": item.get("verification_status"),
                    "lifecycle_state": item.get("lifecycle_state"),
                    "execution_evidence": (item.get("policy_decision") or {}).get("execution_evidence", {}),
                    "retry_available": retry_available,
                    "retry_after": retry_after if pending_operator_retry else None,
                })
            row["retry_available"] = bool(
                row["status"] in {"failed", "dead_letter"}
                and row["failure_code"] in retryable_delivery_codes
                and row["attempts"] < 3 and not actions
            )
            row["actions"] = [{
                **item,
            } for item in safe_actions]
        return {"deliveries": rows}

    @app.post("/api/admin/webhook/deliveries/{delivery_id}/retry", status_code=202)
    def retry_admin_webhook_delivery(delivery_id: str, request: Request):
        actor_id = require_admin(request)
        scheduled, next_attempt_at, reason = processor.store.schedule_delivery_retry(delivery_id)
        if not scheduled:
            status_code = 404 if reason == "delivery_not_found" else 409
            raise HTTPException(status_code=status_code, detail=reason)
        try:
            auth_store.record_audit("webhook_delivery_retry_scheduled", {
                "delivery_id": delivery_id, "actor_id": actor_id,
                "next_attempt_at": next_attempt_at,
            })
        except Exception:
            pass
        processor.start()
        processor._wake.set()
        return {"delivery_id": delivery_id, "status": "pending",
                "next_attempt_at": next_attempt_at, "reason": reason}

    @app.get("/api/health")
    def health():
        from llm.provider_settings import PROVIDER_SETTINGS

        settings = PROVIDER_SETTINGS.public_snapshot()
        return {
            "status": "ready",
            "provider": settings["review_provider"],
            "fix_provider": settings["fix_provider"],
            "provider_initialized": lifecycle.provider_initialized,
            "state": "sqlite_durable" if lifecycle.state.persistence_store else "in_memory_process_local",
            "webhook_storage": "sqlite_durable",
            "github_token_configured": bool(os.getenv("GITHUB_TOKEN")),
        }

    @app.get("/api/verification/capabilities")
    def verification_capabilities():
        adapter = getattr(action_coordinator, "adapter", None)
        executor = getattr(action_coordinator, "autonomous_executor", None)
        staging_ready = bool(adapter and adapter.staging_enabled and adapter.staging_token)
        commit_ready = bool(adapter and adapter.commit_enabled and adapter.commit_token)
        has_remote = bool(executor and executor.behavioral_verifier and executor.behavioral_tests)
        has_github_hosted = bool(executor and executor.github_behavioral_verifier)
        behavioral_ready = has_remote ^ has_github_hosted
        workflow_ready = bool(executor and executor.workflow_identity)
        calibration_ready = bool(os.getenv("AI_CODE_REVIEWER_RUNTIME_CALIBRATION_PATH")
                                and os.getenv("AI_CODE_REVIEWER_CALIBRATION_DATASET_ID"))
        prototype_confidence_configured = (
            os.getenv("GITHUB_MODEL_CONFIDENCE_POLICY", "").strip().casefold()
            == "academic_prototype"
            and bool(os.getenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_REPOSITORY", "").strip())
            and os.getenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_BRANCH", "").strip().startswith("test/")
        )
        if has_github_hosted:
            hosted_policy = getattr(executor, "github_hosted_commit_policy", "")
            behavioral_commit_scope = (
                "github_hosted_prototype_policy_selected" if hosted_policy == "prototype" else
                "candidate_staging_only_github_hosted_docker_boundary"
            )
        elif has_remote:
            behavioral_commit_scope = "existing_independent_commit_gates"
        else:
            behavioral_commit_scope = "unavailable"
        return {
            "syntax": "available_ast_parse_only",
            "static": "available_deterministic_ast_rules",
            "behavioral": ("configured_remote_gvisor_or_compatible_broker" if has_remote else
                           "configured_github_hosted_attested_workflow" if has_github_hosted else
                           "not_run_isolated_execution_broker_unavailable"
                           if not remote_configured and not github_configured and not provider_choice else
                           "provider_configuration_ambiguous_or_unavailable"),
            "behavioral_provider": "remote_https" if has_remote else "github_actions" if has_github_hosted else "none",
            "behavioral_commit_scope": behavioral_commit_scope,
            "github_hosted_commit_policy": (
                "prototype_selected" if has_github_hosted
                and getattr(executor, "github_hosted_commit_policy", "") == "prototype"
                else "disabled_or_unavailable"
            ),
            "github_ci": "read_only_exact_sha_verification_available",
            "candidate_revision_ci": "configured_workflow_and_checks_required" if workflow_ready else "requires_candidate_revision_and_workflow_identity",
            "confidence_calibration": "artifact_configured_validation_occurs_per_finding" if calibration_ready else "unavailable_without_signed_independent_labels",
            "model_confidence_estimates": (
                "explicit_owner_opt_in_configured_for_disposable_test_branch"
                if prototype_confidence_configured else "strictly_validated_but_disabled_for_autonomous_actions"
            ),
            "candidate_staging": "enabled" if staging_ready else "disabled_or_credential_missing",
            "autonomous_fix": "implemented_off_by_default_requires_supported_confidence_and_independent_gates",
            "commit_execution": "enabled_but_fail_closed_gates_required" if commit_ready else "disabled_by_default_or_credential_missing",
            "commit_execution_reason": "explicit_owner_configuration_and_per_finding_independent_gates_required",
        }

    @app.post("/api/github/pr-reviews", status_code=200)
    def review_github_pr(request: GitHubPRReviewRequest):
        if not _PR_REVIEW_SLOT.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="github_pr_review_busy")
        try:
            _apply_github_credentials(lifecycle, credential_store, action_coordinator)
            from mcp_server.github_policy import load_repository_action_config
            action_coordinator.configs = load_repository_action_config()
            result = lifecycle.review_github_pr(request.url)
            payload = result.to_dict()
            payload["action_policy"] = action_coordinator.describe_repository(result.repository)
            from llm.provider_settings import PROVIDER_SETTINGS
            if result.provider_identity and result.provider_identity.startswith("groq:"):
                payload["action_policy"]["source_sharing_authorization"] = (
                    "authorized" if PROVIDER_SETTINGS.public_snapshot()["source_sharing_consent"]
                    and payload["action_policy"]["repository_source_sharing"] == "allowed"
                    else "not_authorized")
            else:
                payload["action_policy"]["source_sharing_authorization"] = "local_provider_no_external_sharing"
            return payload
        except GitHubPRException as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        except Exception as exc:
            from llm.provider_settings import ProviderSettingsError

            if isinstance(exc, ProviderSettingsError) and str(exc) == "source_sharing_not_authorized":
                raise HTTPException(status_code=403, detail="source_sharing_not_authorized") from exc
            raise HTTPException(status_code=503, detail="github_pr_review_unavailable") from exc
        finally:
            _PR_REVIEW_SLOT.release()

    @app.post("/api/github/webhook", status_code=202)
    async def github_webhook(request: Request):
        try:
            secret = credential_store.get("webhook_secret")
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail="webhook_credential_store_unavailable") from exc
        if not secret:
            raise HTTPException(status_code=503, detail="webhook_secret_not_configured")
        limit = _webhook_body_limit()
        raw_body = await _read_bounded_body(request, limit)
        if not verify_signature(raw_body, request.headers.get("x-hub-signature-256"), secret):
            raise HTTPException(status_code=401, detail="invalid_webhook_signature")
        try:
            delivery = parse_delivery(
                raw_body,
                delivery_id=request.headers.get("x-github-delivery", ""),
                event=request.headers.get("x-github-event", ""),
            )
            # Some ASGI test/development clients invoke requests without a
            # lifespan manager; starting is idempotent and happens only after
            # authentication and payload validation.
            processor.start()
            disposition, record = processor.accept(delivery)
        except WebhookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        except Exception:
            raise HTTPException(status_code=503, detail="webhook_intake_unavailable")
        status = record.get("status", "pending")
        return {
            "delivery_id": delivery.delivery_id,
            "status": status,
            "duplicate": disposition == "duplicate",
            "failure_code": record.get("failure_code"),
        }

    @app.get("/api/github/webhook/deliveries/{delivery_id}")
    def get_webhook_delivery(delivery_id: str, request: Request):
        try:
            authorized = verify_status_token(
                request.headers.get("authorization"), os.getenv("GITHUB_WEBHOOK_STATUS_TOKEN")
            )
        except WebhookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        if not authorized:
            raise HTTPException(status_code=401, detail="webhook_status_unauthorized")
        if not credential_store.get("webhook_secret"):
            raise HTTPException(status_code=503, detail="webhook_secret_not_configured")
        processor.start()
        record = processor.store.get(delivery_id)
        if record is None:
            raise HTTPException(status_code=404, detail="webhook_delivery_not_found")
        return record

    @app.get("/api/github/webhook/deliveries/{delivery_id}/actions")
    def get_webhook_actions(delivery_id: str, request: Request):
        try:
            authorized = verify_status_token(
                request.headers.get("authorization"), os.getenv("GITHUB_WEBHOOK_STATUS_TOKEN")
            )
        except WebhookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        if not authorized:
            raise HTTPException(status_code=401, detail="webhook_status_unauthorized")
        if not credential_store.get("webhook_secret"):
            raise HTTPException(status_code=503, detail="webhook_secret_not_configured")
        processor.start()
        record = processor.store.get(delivery_id)
        if record is None:
            raise HTTPException(status_code=404, detail="webhook_delivery_not_found")
        return {"delivery_id": delivery_id, "status": record["status"],
                "repository": record["owner"] + "/" + record["repository"],
                "pr_number": record["pr_number"], "base_sha": record["base_sha"],
                "head_sha": record["head_sha"], "failure_code": record.get("failure_code"),
                "actions": record["actions"]}

    @app.post("/api/github/webhook/deliveries/{delivery_id}/actions/{action_id}/reconcile")
    def reconcile_webhook_action(delivery_id: str, action_id: str, request: Request):
        try:
            authorized = verify_status_token(
                request.headers.get("authorization"), os.getenv("GITHUB_WEBHOOK_STATUS_TOKEN")
            )
        except WebhookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        if not authorized:
            raise HTTPException(status_code=401, detail="webhook_status_unauthorized")
        record = processor.store.get(delivery_id)
        if record is None:
            raise HTTPException(status_code=404, detail="webhook_delivery_not_found")
        action = next((item for item in record["actions"] if item["action_id"] == action_id), None)
        if action is None:
            raise HTTPException(status_code=404, detail="webhook_action_not_found")
        if action.get("action_type") not in {"review_comment", "autonomous_fix"} or action.get("publication_status") not in {
                "unknown", "requires_reconciliation"}:
            raise HTTPException(status_code=409, detail="action_does_not_require_reconciliation")
        owner, separator, repository = str(action.get("repository", "")).partition("/")
        if not separator:
            raise HTTPException(status_code=409, detail="action_repository_identity_invalid")
        from mcp_server.github_pr import PullRequestRef
        if action.get("action_type") == "autonomous_fix":
            policy = action.get("policy_decision") or {}
            evidence = policy.get("execution_evidence") or {}
            candidate = evidence.get("candidate") or {}
            action_ref = PullRequestRef(owner, repository, int(action["pr_number"]))
            if evidence.get("commit_attempted") is True:
                result = action_coordinator.adapter.reconcile_commit(
                    action_ref, repository=action["repository"], branch=str(evidence.get("branch", "")),
                    expected_head_sha=action["head_sha"], candidate_sha=str(candidate.get("candidate_sha", "")),
                )
                if result.get("status") == "published":
                    processor.store.mark_commit_reconciled(action_id, status="published",
                                                           commit_sha=result.get("commit_sha"))
                elif result.get("status") == "not_published":
                    processor.store.mark_commit_reconciled(action_id, status="failed",
                                                           failure_code="reconciled_commit_absent_operator_review")
                elif result.get("status") == "stale":
                    processor.store.mark_commit_reconciled(action_id, status="stale",
                                                           failure_code="reconciled_commit_head_changed")
            else:
                result = action_coordinator.adapter.reconcile_candidate_revision(
                    action_ref, repository=action["repository"], operation_id=action_id,
                    expected_candidate_sha=candidate.get("candidate_sha"),
                )
                if result.get("status") == "candidate_staged":
                    processor.store.finish_action(action_id, "blocked",
                                                  failure_code="candidate_stage_confirmed_manual_review_required")
                elif result.get("status") == "not_staged":
                    processor.store.finish_action(action_id, "failed",
                                                  failure_code="candidate_stage_absence_confirmed_operator_review")
        else:
            result = action_coordinator.adapter.reconcile_inline_comment(
                PullRequestRef(owner, repository, int(action["pr_number"])),
                repository=action["repository"], expected_head_sha=action["head_sha"],
                operation_id=action.get("operation_id") or action_id,
                comment_kind=(action.get("policy_decision") or {}).get("comment_kind", "inline"),
            )
            if result.get("status") == "published":
                processor.store.mark_reconciled(action_id, status="published",
                                                github_comment_id=result.get("github_comment_id"))
            elif result.get("status") == "not_published":
                processor.store.mark_reconciled(action_id, status="failed",
                                                failure_code="reconciled_comment_absent_retry_requires_operator")
            elif result.get("status") == "stale":
                processor.store.mark_reconciled(action_id, status="stale",
                                                failure_code="reconciled_expected_revision_changed")
        return {"action_id": action_id, **result}

    @app.post("/api/admin/webhook/deliveries/{delivery_id}/actions/{action_id}/retry", status_code=202)
    def retry_failed_webhook_action(delivery_id: str, action_id: str, request: Request):
        actor_id = require_admin(request)
        delivery = processor.store.get(delivery_id)
        if delivery is None:
            raise HTTPException(status_code=404, detail="webhook_delivery_not_found")
        action = next((item for item in delivery["actions"] if item["action_id"] == action_id), None)
        if action is None:
            raise HTTPException(status_code=404, detail="webhook_action_not_found")
        if action.get("action_type") not in {"review_comment", "autonomous_fix"}:
            raise HTTPException(status_code=409, detail="action_type_not_retryable")
        if action.get("action_type") == "autonomous_fix":
            # Unknown commit writes are reconciled only; no automatic or manual
            # blind retry is offered by this endpoint.
            raise HTTPException(status_code=409, detail="commit_reconciliation_required_no_blind_retry")
        retryable_codes = {"github_rate_limited", "reconciled_comment_absent_retry_requires_operator"}
        if action.get("publication_status") == "failed":
            if action.get("failure_code") not in retryable_codes:
                raise HTTPException(status_code=409, detail="action_failure_not_retryable")
            owner, separator, repository_name = str(action.get("repository", "")).partition("/")
            if not separator:
                raise HTTPException(status_code=409, detail="action_repository_identity_invalid")
            policy_record = action.get("policy_decision") or {}
            comment_kind = policy_record.get("comment_kind", "inline")
            from mcp_server.github_pr import PullRequestRef
            reconciliation = action_coordinator.adapter.reconcile_inline_comment(
                PullRequestRef(owner, repository_name, int(action["pr_number"])),
                repository=action["repository"], expected_head_sha=action["head_sha"],
                operation_id=action.get("operation_id") or action_id,
                comment_kind=comment_kind,
            )
            if reconciliation.get("status") == "published":
                processor.store.mark_reconciled(action_id, status="published",
                                                github_comment_id=reconciliation.get("github_comment_id"))
                return {"action_id": action_id, **reconciliation}
            if reconciliation.get("status") != "not_published" or reconciliation.get("retry_safe") is not True:
                raise HTTPException(status_code=409, detail="action_remote_state_not_safe_to_retry")
            scheduled, next_attempt = processor.store.schedule_action_retry(
                action_id, retryable=True, actor_id=actor_id,
            )
            if not scheduled:
                raise HTTPException(status_code=409, detail="action_retry_limit_reached_or_state_changed")
            try:
                auth_store.record_audit("webhook_action_retry_scheduled", {
                    "delivery_id": delivery_id, "action_id": action_id, "actor_id": actor_id,
                })
            except Exception:
                pass
            return {"action_id": action_id, "status": "pending",
                    "next_attempt_at": next_attempt, "reason": "remote_absence_confirmed_retry_scheduled"}
        if (action.get("publication_status") != "pending"
                or not action.get("last_operator")
                or not isinstance(action.get("next_retry_at"), (int, float))):
            raise HTTPException(status_code=409, detail="action_retry_not_scheduled")
        if action["next_retry_at"] > time.time():
            raise HTTPException(status_code=409, detail="action_retry_backoff_active")
        result_payload = delivery.get("result")
        policy_evidence = (action.get("policy_decision") or {}).get("evidence", {})
        file_path = policy_evidence.get("file_path") if isinstance(policy_evidence, dict) else None
        finding = None
        changed_paths = set()
        if isinstance(result_payload, dict):
            for file_result in result_payload.get("files", []):
                if not isinstance(file_result, dict):
                    continue
                if (isinstance(file_result.get("path"), str)
                        and file_result.get("status") in {"added", "modified", "renamed"}):
                    changed_paths.add(file_result["path"])
                if file_result.get("path") == file_path:
                    finding = next((item for item in file_result.get("findings", [])
                                    if isinstance(item, dict) and item.get("finding_id") == action.get("finding_id")), None)
        if not isinstance(finding, dict):
            processor.store.finish_action(action_id, "blocked", failure_code="retry_finding_payload_unavailable")
            raise HTTPException(status_code=409, detail="retry_finding_payload_unavailable")
        from mcp_server.github_policy import PolicyInput, evaluate_finding, load_repository_action_config
        from mcp_server.github_pr import PullRequestRef
        repository = str(action["repository"])
        action_coordinator.configs = load_repository_action_config()
        current_policy = evaluate_finding(action_coordinator.configs, PolicyInput(
            repository=repository, finding=finding,
            expected_head_sha=str(action["head_sha"]), current_head_sha=str(action["head_sha"]),
            changed_lines=None, file_path=str(file_path or ""),
            action_authorized=bool(getattr(action_coordinator.adapter, "token", None)),
            branch=result_payload.get("head_branch") if isinstance(result_payload, dict) else None,
            is_fork=(result_payload.get("head_repository") or repository).casefold() != repository.casefold()
            if isinstance(result_payload, dict) else True,
        ))
        if current_policy.action != "review_comment" or not current_policy.eligible:
            processor.store.finish_action(action_id, "denied", failure_code="current_policy_denied_retry")
            raise HTTPException(status_code=409, detail="current_policy_denied_retry")
        if not processor.store.claim_action(action_id):
            raise HTTPException(status_code=409, detail="action_retry_claim_failed")
        ref = PullRequestRef(owner=str(action["repository"]).split("/", 1)[0],
                             repository=str(action["repository"]).split("/", 1)[1],
                             number=int(action["pr_number"]))
        if (action.get("policy_decision") or {}).get("comment_kind") == "inline":
            publication = action_coordinator.adapter.publish_inline_comment(
                ref, repository=repository, expected_head_sha=action["head_sha"],
                finding=finding, changed_paths=changed_paths, operation_id=action_id,
            )
        else:
            publication = action_coordinator.adapter.publish_general_comment(
                ref, repository=repository, expected_head_sha=action["head_sha"],
                finding=finding, operation_id=action_id,
            )
        processor.store.finish_action(action_id, publication.status,
                                      github_comment_id=publication.github_id,
                                      failure_code=publication.error_code)
        try:
            auth_store.record_audit("webhook_action_retry_completed", {
                "delivery_id": delivery_id, "action_id": action_id,
                "actor_id": actor_id, "status": publication.status,
            })
        except Exception:
            pass
        return {"action_id": action_id, **publication.to_dict()}

    @app.post("/api/reviews", status_code=201)
    def submit_review(request: ReviewRequest):
        try:
            review_id, review, timings = lifecycle.submit_review_with_timings(request.code)
        except OverflowError:
            raise HTTPException(status_code=413, detail="code_too_large")
        except ValueError as exc:
            if str(exc) == "source_sharing_not_authorized":
                raise HTTPException(status_code=403, detail="source_sharing_not_authorized") from exc
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            from llm.provider_settings import ProviderSettingsError

            if isinstance(exc, ProviderSettingsError) and str(exc) == "source_sharing_not_authorized":
                raise HTTPException(status_code=403, detail="source_sharing_not_authorized") from exc
            raise HTTPException(status_code=503, detail="review_provider_unavailable") from exc
        if not review.valid:
            raise HTTPException(status_code=502, detail="review_provider_or_validation_failed")
        return {"review_id": review_id, "review": _review_payload(review), "timings": timings,
                "provider_identity": lifecycle.provider_identity("review")}

    @app.get("/api/reviews/{review_id}")
    def get_review(review_id: str, request: Request):
        require_admin(request)
        handle = lifecycle.get_review(review_id)
        if handle is None:
            raise HTTPException(status_code=404, detail="review_not_found_or_expired")
        return {"review_id": review_id, "review": _review_payload(handle.response)}

    @app.post("/api/reviews/{review_id}/findings/{finding_id}/fixes", status_code=201)
    def generate_fix(review_id: str, finding_id: str, http_request: Request,
                     request: FixRequest = FixRequest()):
        require_admin(http_request)
        try:
            candidate = lifecycle.generate_fix(review_id, finding_id, request.language)
        except LookupError:
            raise HTTPException(status_code=404, detail="review_not_found_or_expired")
        except Exception as exc:
            from llm.provider_settings import ProviderSettingsError

            if isinstance(exc, ProviderSettingsError) and str(exc) == "source_sharing_not_authorized":
                raise HTTPException(status_code=403, detail="source_sharing_not_authorized") from exc
            raise HTTPException(status_code=503, detail="fix_provider_unavailable") from exc
        if candidate.generation_status != "generated":
            if any(error.startswith("fix_generation_failed") for error in candidate.generation_errors):
                raise HTTPException(status_code=503, detail="fix_provider_unavailable")
            error = candidate.generation_errors[0] if candidate.generation_errors else ""
            if error.startswith("finding_not_found"):
                raise HTTPException(status_code=404, detail="finding_not_found")
            if error.startswith("finding_abstained"):
                raise HTTPException(status_code=409, detail="finding_abstained")
            if error.startswith("invalid_review_response"):
                raise HTTPException(status_code=409, detail="review_handle_invalid")
            # The request passed FastAPI validation; a rejected generated
            # candidate is an upstream response failure, not a client 422.
            raise HTTPException(status_code=502, detail="fix_provider_response_invalid")
        return _candidate_payload(candidate)

    @app.get("/api/fixes/{candidate_id}")
    def get_fix(candidate_id: str, request: Request):
        require_admin(request)
        candidate = lifecycle.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="candidate_not_found_or_expired")
        return _candidate_payload(candidate)

    @app.post("/api/fixes/{candidate_id}/verify")
    def verify_fix(candidate_id: str, request: Request):
        require_admin(request)
        try:
            return lifecycle.verify(candidate_id).to_dict()
        except LookupError:
            raise HTTPException(status_code=404, detail="candidate_not_found_or_expired")

    @app.post("/api/fixes/{candidate_id}/accept")
    def accept_fix(candidate_id: str, request: Request):
        try:
            actor_id = require_admin(request)
            result = lifecycle.accept(candidate_id, actor_id=actor_id)
            if result.outcome != "already_applied":
                try:
                    auth_store.record_audit("fix_candidate_decision", {
                        "candidate_id": candidate_id, "finding_ref": result.candidate.finding_ref,
                        "decision": "accepted", "outcome": result.outcome,
                        "actor_id": actor_id,
                    })
                except Exception:
                    pass
            return result.to_dict()
        except LookupError:
            raise HTTPException(status_code=404, detail="candidate_not_found_or_expired")

    @app.post("/api/fixes/{candidate_id}/reject")
    def reject_fix(candidate_id: str, request: Request):
        try:
            actor_id = require_admin(request)
            result = lifecycle.reject(candidate_id, actor_id=actor_id)
            if result.outcome != "already_applied":
                try:
                    auth_store.record_audit("fix_candidate_decision", {
                        "candidate_id": candidate_id, "finding_ref": result.candidate.finding_ref,
                        "decision": "rejected", "outcome": result.outcome,
                        "actor_id": actor_id,
                    })
                except Exception:
                    pass
            return result.to_dict()
        except LookupError:
            raise HTTPException(status_code=404, detail="candidate_not_found_or_expired")

    @app.get("/api/fixes/{candidate_id}/export")
    def export_fix(candidate_id: str, request: Request):
        require_admin(request)
        try:
            return lifecycle.export(candidate_id).to_dict()
        except LookupError:
            raise HTTPException(status_code=404, detail="candidate_not_found_or_expired")
        except ValueError:
            raise HTTPException(status_code=409, detail="candidate_not_accepted_or_unverified")

    return app


def _print_startup_summary(auth_store: AdminAuthStore, credentials: CredentialStore,
                           bootstrap_credential: Optional[str]) -> None:
    from urllib.parse import urlsplit, urlunsplit
    from llm.provider_settings import PROVIDER_SETTINGS

    def safe_base(value: str) -> str:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return "(invalid URL configuration)"
            host = parsed.hostname
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
        except ValueError:
            return "(invalid URL configuration)"

    api_url = safe_base(os.getenv("AI_CODE_REVIEWER_API_URL", "http://127.0.0.1:8000"))
    frontend_url = safe_base(os.getenv("AI_CODE_REVIEWER_FRONTEND_URL", "http://127.0.0.1:5173"))
    setup = auth_store.setup_complete()
    provider_status = PROVIDER_SETTINGS.public_snapshot()
    github_status = credentials.status()
    print("\nAI Code Reviewer — Backend Started")
    print(f"API: {api_url}")
    print(f"Frontend: {frontend_url}")
    print(f"Admin Login: {frontend_url}/settings")
    print(f"Authentication: {'Configured' if setup else 'Setup required'}")
    print("Recovery: python -m mcp_server.admin_cli reset-password")
    if bootstrap_credential:
        print(f"Admin Setup: {frontend_url}/setup")
        if os.getenv("AI_CODE_REVIEWER_PRINT_BOOTSTRAP_CREDENTIAL", "true").casefold() == "true":
            print(f"One-time bootstrap credential (expires in 30 minutes): {bootstrap_credential}")
        else:
            print("Bootstrap credential withheld from logs; initialize with the local admin CLI.")
    elif not setup:
        print("Setup credential was previously issued or expired; run python -m mcp_server.admin_cli reset-bootstrap locally.")
    print(f"Groq: {'Configured' if provider_status['groq_api_key_configured'] else 'Not configured'}")
    github_configured = github_status["github_token_configured"]
    github_label = "Configured" if github_configured is True else "Not configured" if github_configured is False else "Status unavailable"
    print(f"GitHub: {github_label}")
    print()


def _validate_repository_identity(owner: str, repository: str) -> str:
    import re
    if (not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner)
            or not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
            or owner in {".", ".."} or repository in {".", ".."}):
        raise HTTPException(status_code=422, detail="repository_identity_invalid")
    return f"{owner}/{repository}"


def _policy_config_payload(config) -> dict:
    from mcp_server.github_policy import RepositoryActionConfig
    values = asdict(config or RepositoryActionConfig())
    return {key: list(value) if isinstance(value, tuple) else value for key, value in values.items()}


def _apply_github_credentials(lifecycle: ReviewFixService, credentials: CredentialStore,
                              action_coordinator: GitHubActionCoordinator, *, include_action: bool = False) -> None:
    client = getattr(getattr(lifecycle, "github_pr_service", None), "client", None)
    try:
        github_token = credentials.get("github_token") if isinstance(client, GitHubRESTClient) else None
        action_tokens = None
        if include_action:
            action_tokens = (
                credentials.get("github_actions_token"),
                credentials.get("github_candidate_staging_token"),
                credentials.get("github_autonomous_commit_token"),
            )
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail="github_credential_store_unavailable") from exc
    if isinstance(client, GitHubRESTClient):
        client.token = github_token
    adapter = getattr(action_coordinator, "adapter", None)
    if include_action and adapter is not None:
        adapter.token, adapter.staging_token, adapter.commit_token = action_tokens


def _clear_github_credentials(lifecycle: ReviewFixService,
                              action_coordinator: GitHubActionCoordinator) -> None:
    """Clear tokens after a failed startup read, preserving fail-closed writes."""
    client = getattr(getattr(lifecycle, "github_pr_service", None), "client", None)
    if isinstance(client, GitHubRESTClient):
        client.token = None
    adapter = getattr(action_coordinator, "adapter", None)
    if adapter is not None:
        adapter.token = None
        adapter.staging_token = None
        adapter.commit_token = None


app = create_app()


def _webhook_body_limit() -> int:
    try:
        value = int(os.getenv("GITHUB_WEBHOOK_MAX_BODY_BYTES", str(DEFAULT_WEBHOOK_BODY_LIMIT)))
    except ValueError:
        value = DEFAULT_WEBHOOK_BODY_LIMIT
    return min(max(value, 1024), 4 * 1024 * 1024)


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0:
                raise ValueError
            if int(length) > limit:
                raise HTTPException(status_code=413, detail="webhook_body_too_large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_content_length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="webhook_body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _run_webhook_review(lifecycle: ReviewFixService, event: dict,
                        action_coordinator: Optional[GitHubActionCoordinator] = None):
    # Values are validated identifiers, never payload-supplied URLs.
    url = f"https://github.com/{event['owner']}/{event['repository']}/pull/{event['number']}"
    result = lifecycle.review_github_pr(
        url,
        expected_owner=event["owner"],
        expected_repository=event["repository"],
        expected_number=event["number"],
        expected_base_sha=event["base_sha"],
        expected_head_sha=event["head_sha"],
    )
    payload = result.to_dict()
    payload["provider_identity"] = lifecycle.provider_identity("review")
    if action_coordinator is not None:
        runtime_files = {
            item.path: {"source_code": item._source_code,
                        "review_response": item._validated_response}
            for item in result.files
            if item._source_code is not None and item._validated_response is not None
        }
        payload["action_processing"] = action_coordinator.process_review(
            event, payload, runtime_files=runtime_files,
        )
    return payload

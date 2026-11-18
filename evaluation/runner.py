"""Bounded evaluation harness reusing the shared review orchestrator."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from evaluation.metrics import aggregate_metrics, match_findings
from evaluation.rate_limit import (
    GroqRateLimitController, RateLimitBudgetError, estimate_tokens, exception_metadata,
)
from mcp_server import orchestrator
from review_schema import REVIEW_SCHEMA_VERSION

BENCHMARK_VERSION = "synthetic-python-v1"
BENCHMARK_PATH = Path(__file__).with_name("benchmark_v1.jsonl")
CHECKPOINT_VERSION = 1
DIAGNOSTICS_VERSION = 1
PREVIEW_MAX_CHARS = 240
SCORING_VERSION = "exact_issue_type_line_tolerance_2_v1"


def prompt_configuration_digest() -> str:
    """Fingerprint the active Groq review prompt and strict response contract."""
    from llm.groq_client import GroqLLM
    from review_schema import REVIEW_OUTPUT_INSTRUCTIONS, groq_review_response_format

    payload = "\n".join((
        inspect.getsource(GroqLLM._build_prompt),
        inspect.getsource(GroqLLM.analyze_code),
        REVIEW_OUTPUT_INSTRUCTIONS,
        json.dumps(groq_review_response_format(), sort_keys=True, separators=(",", ":")),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scoring_configuration_digest() -> str:
    """Fingerprint the actual finding-matching and aggregation implementation."""
    from evaluation import metrics

    payload = "\n".join(inspect.getsource(getattr(metrics, name))
                        for name in ("_matches", "match_findings", "aggregate_metrics"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CheckpointMismatchError(ValueError):
    """Raised when a checkpoint does not match the requested evaluation config."""


def _safe_error_text(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value).split())
    secret = os.getenv("GROQ_API_KEY")
    if secret:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"\bgsk_[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:limit]


def _provider_error_details(exc: Exception, *, phase: str, max_retries: int) -> dict[str, Any]:
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    body = getattr(exc, "body", None)
    error_body = body.get("error", body) if isinstance(body, dict) else {}
    code = None
    message = None
    if isinstance(error_body, dict):
        code = error_body.get("code") or error_body.get("type")
        message = error_body.get("message")
    error_type = type(exc).__name__
    if not message:
        if error_type in {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout"}:
            message = "Connection failed before a complete provider response was received."
        elif status is not None:
            message = f"Groq returned HTTP {status} without a structured error message."
        else:
            message = "Provider call failed without a structured error message."
    retryable = status == 429 or (status is not None and status >= 500) or error_type in {
        "APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout"
    }
    rate_metadata = exception_metadata(exc)
    sdk_retry_count = rate_metadata.get("sdk_retries")
    sdk_retry_count_estimated = False
    if sdk_retry_count is None and retryable and max_retries:
        sdk_retry_count = max_retries
        sdk_retry_count_estimated = True
    return {
        "phase": phase,
        "error_type": error_type,
        "http_status": status,
        "provider_error_code": _safe_error_text(code) if code is not None else None,
        "sanitized_message": _safe_error_text(message),
        "sdk_max_retries": max_retries,
        "retry_exhausted": bool(retryable and max_retries > 0),
        "retry_after_seconds": rate_metadata.get("retry_after_seconds"),
        "rate_limit_headers": rate_metadata.get("headers", {}),
        "sdk_retry_count": sdk_retry_count,
        "sdk_retry_count_estimated": sdk_retry_count_estimated,
    }


class PacedProvider:
    """Inject a live-only delay and preserve sanitized provider errors."""

    def __init__(self, provider: Any, *, interval_seconds: float, enabled: bool,
                 last_request_at: Optional[str] = None,
                 rate_limit_controller: GroqRateLimitController | None = None,
                 clock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        if interval_seconds < 0:
            raise ValueError("request interval must be non-negative")
        self.provider = provider
        self.rate_limit_controller = rate_limit_controller
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self.clock = clock
        self.sleeper = sleeper
        self.last_request_epoch = None
        if last_request_at:
            try:
                self.last_request_epoch = datetime.fromisoformat(last_request_at).timestamp()
            except (TypeError, ValueError, OverflowError):
                raise ValueError("checkpoint has invalid last_request_at")
        self.last_request_at = last_request_at
        self.request_counts = {"review": 0, "fix": 0}
        self.provider_errors: list[dict[str, Any]] = []
        self.case_pacing_wait_seconds = 0.0
        self.case_provider_inference_seconds = 0.0
        self.rate_limit_errors: list[dict[str, Any]] = []
        self.structured_output_metadata = getattr(
            provider, "structured_output_metadata", {
                "mode": "provider_unspecified", "strict": None,
                "schema_name": None, "schema_version": None,
            }
        )
        self.model = getattr(provider, "model", None)
        self.temperature = getattr(provider, "temperature", None)

    def clear_case_errors(self) -> None:
        self.provider_errors = []
        self.case_pacing_wait_seconds = 0.0
        self.case_provider_inference_seconds = 0.0
        self.rate_limit_errors = []

    def _call(self, phase: str, method_name: str, *args, **kwargs):
        estimated_request: dict[str, Any] | None = None
        if self.rate_limit_controller is not None:
            if method_name == "analyze_code" and hasattr(self.provider, "estimate_request_tokens"):
                estimated_request = self.provider.estimate_request_tokens(
                    kwargs.get("code", args[0] if args else ""),
                    kwargs.get("context", args[1] if len(args) > 1 else []),
                )
            else:
                estimated_request = estimate_tokens(" ".join(str(value) for value in args))
            try:
                delay = self.rate_limit_controller.before_request(
                    int(estimated_request["total_tokens_reserved"]),
                    estimated_input_tokens=estimated_request.get("input_tokens_estimated"),
                    output_allowance=estimated_request.get("output_tokens_reserved"),
                )
                self.case_pacing_wait_seconds += delay
            except RateLimitBudgetError as exc:
                self.rate_limit_errors.append({
                    "phase": phase, "error_type": type(exc).__name__,
                    "sanitized_message": _safe_error_text(exc), "request_sent": False,
                })
                raise
        now = self.clock()
        if self.enabled and self.rate_limit_controller is None and self.last_request_epoch is not None:
            remaining = self.interval_seconds - max(0.0, now - self.last_request_epoch)
            if remaining > 0:
                self.sleeper(remaining)
                self.case_pacing_wait_seconds += remaining
                now = self.clock()
        self.last_request_epoch = now
        self.last_request_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        self.request_counts[phase] += 1
        inference_started = time.perf_counter()
        try:
            method = getattr(self.provider, method_name)
            result = method(*args, **kwargs)
            if self.rate_limit_controller is not None:
                self.rate_limit_controller.record_result(
                    success=True,
                    metadata=getattr(self.provider, "last_response_metadata", {}),
                    estimated_tokens=int((estimated_request or {}).get("total_tokens_reserved", 0)),
                )
            return result
        except Exception as exc:
            client = getattr(self.provider, "client", None)
            max_retries = getattr(client, "max_retries", 0)
            if not isinstance(max_retries, int) or isinstance(max_retries, bool):
                max_retries = 0
            self.provider_errors.append(
                _provider_error_details(exc, phase=phase, max_retries=max_retries)
            )
            if self.rate_limit_controller is not None:
                metadata = exception_metadata(exc)
                if metadata.get("http_status") == 429 and metadata.get("sdk_retries") is None:
                    metadata["sdk_retries"] = max_retries
                    metadata["sdk_retries_estimated_from_client_policy"] = True
                self.rate_limit_controller.record_result(
                    success=False, metadata=metadata,
                    estimated_tokens=int((estimated_request or {}).get("total_tokens_reserved", 0)),
                    error=exc,
                )
            raise
        finally:
            # The provider method duration includes SDK-managed retries.
            self.case_provider_inference_seconds += time.perf_counter() - inference_started

    def analyze_code(self, code: str, context: list[str]) -> str:
        return self._call("review", "analyze_code", code=code, context=context)

    def generate_fix(self, code, finding, context):
        return self._call("fix", "generate_fix", code, finding, context)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _cases_digest(cases: list[dict[str, Any]]) -> str:
    content = json.dumps(cases, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _directory_digest(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(item.relative_to(path).as_posix().encode("utf-8"))
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:gsk|gh[pousr]?)_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def _sanitize_preview(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    """Return a bounded preview with common credential forms redacted."""
    preview = str(text).replace("\x00", " ")
    preview = " ".join(preview.split())
    for pattern in _SECRET_PATTERNS:
        preview = pattern.sub("[redacted]", preview)
    secret = os.getenv("GROQ_API_KEY")
    if secret:
        preview = preview.replace(secret, "[redacted]")
    return preview[:limit]


def _source_category(item: Any) -> str:
    family = str(getattr(item, "source_family", "") or "").lower()
    metadata = getattr(item, "metadata", {}) or {}
    details = " ".join([family, *(str(v) for v in metadata.values())]).lower()
    if any(marker in details for marker in (
        "pull request", "pull_request", "historical_pr", "pr_review",
        "microsoft_historical_reviews", "microsoft_codereviewer",
    )):
        return "historical_pr"
    if any(marker in details for marker in ("cwe", "owasp", "vulnerability", "security")):
        return "cwe_owasp"
    if any(marker in details for marker in ("style", "static", "lint", "rule")):
        return "style_static"
    return "other"


_SAFE_IDENTIFIER_KEYS = {
    "document_id", "record_id", "parent_id", "repository", "repo", "pr_number",
    "pull_request_number", "rule_id", "cwe_id", "source_id", "document", "path",
}


def _safe_source_identifiers(item: Any) -> dict[str, str]:
    metadata = getattr(item, "metadata", {}) or {}
    identifiers: dict[str, str] = {}
    for key, value in metadata.items():
        if str(key).lower() in _SAFE_IDENTIFIER_KEYS and isinstance(value, (str, int)):
            identifiers[str(key)] = _sanitize_preview(str(value), limit=100)
    rule_identifier = getattr(item, "rule_identifier", None)
    if rule_identifier and "rule_identifier" not in identifiers:
        identifiers["rule_identifier"] = _sanitize_preview(str(rule_identifier), limit=100)
    return identifiers


def _retrieval_diagnostics(evidence: list[Any], *, include_text_previews: bool) -> list[dict[str, Any]]:
    diagnostics = []
    for rank, item in enumerate(evidence, start=1):
        text = getattr(item, "text", "") or ""
        distance = getattr(item, "distance", None)
        metadata = getattr(item, "metadata", {}) or {}
        diagnostic = {
            "retrieval_rank": rank,
            "chunk_id": (_sanitize_preview(str(getattr(item, "evidence_id")), 160)
                         if getattr(item, "evidence_id", None) is not None else None),
            "evidence_id": (_sanitize_preview(str(getattr(item, "evidence_id")), 160)
                            if getattr(item, "evidence_id", None) is not None else None),
            "source_category": _source_category(item),
            "source_family": (_sanitize_preview(str(getattr(item, "source_family")), 100)
                              if getattr(item, "source_family", None) else None),
            "source_identifiers": _safe_source_identifiers(item),
            "source_url": (_sanitize_preview(
                re.sub(r"[?#].*$", "", str(getattr(item, "source_url", None))), 200
            ) if getattr(item, "source_url", None) else None),
            "distance": distance,
            "similarity": metadata.get("similarity"),
            "character_length": len(text),
            "token_count_estimate": len(text.split()),
            "token_count_method": "whitespace_split_estimate",
            "passed_to_model": True,
        }
        if include_text_previews:
            diagnostic["text_preview"] = _sanitize_preview(text)
        diagnostics.append(diagnostic)
    return diagnostics


def _raw_finding_diagnostics(raw_response: str, evidence_ids: set[str]) -> list[dict[str, Any]]:
    """Extract only bounded citation metadata, including parser-rejected JSON."""
    try:
        payload = json.loads(raw_response)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        return []
    output = []
    for index, finding in enumerate(payload["findings"]):
        if not isinstance(finding, dict):
            output.append({"finding_index": index, "reference_status": "invalid_reference_shape",
                           "cited_evidence_ids": [], "reference_outcomes": []})
            continue
        raw_ids = finding.get("evidence_ids")
        if not isinstance(raw_ids, list) or any(not isinstance(value, str) for value in raw_ids):
            status = "invalid_reference_shape"
            cited = []
        else:
            raw_cited = raw_ids[:20]
            cited = [_sanitize_preview(value, 120) for value in raw_cited]
            if finding.get("abstained") is True:
                status = "abstained"
            elif cited:
                status = "unknown_reference" if any(value not in evidence_ids for value in raw_cited) else "valid_reference"
            elif finding.get("uncertain") is True:
                status = "uncited_but_explicitly_uncertain"
            else:
                status = "missing_reference"
        row = {
            "finding_index": index,
            "finding_id": _sanitize_preview(str(finding.get("finding_id", "")), 80) or None,
            "title": _sanitize_preview(str(finding.get("title", "")), 120),
            "cited_evidence_ids": cited,
            "reference_status": status,
            "reference_outcomes": [
                {"evidence_id": value, "outcome": "valid_reference" if value in evidence_ids else "unknown_reference"}
                for value in raw_cited
            ],
        }
        output.append(row)
    return output


class ScriptedProvider:
    """Offline transport/schema smoke provider; outputs are not model results."""

    structured_output_metadata = {
        "mode": "scripted_json", "strict": False,
        "schema_name": None, "schema_version": None,
    }

    def analyze_code(self, code: str, context: list[str]) -> str:
        return json.dumps({"summary": "Scripted offline run; no model assessment.", "findings": []})


def load_cases(path: Path = BENCHMARK_PATH) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seen = set()
    for case in cases:
        required = {"case_id", "category", "source_code", "expected_issue_type", "expected_line",
                    "rationale", "assumptions", "reference_correction"}
        if set(case) != required or case["case_id"] in seen:
            raise ValueError("benchmark schema invalid or case IDs are not unique")
        seen.add(case["case_id"])
        if case["category"] not in {"security", "logic_runtime", "clean"}:
            raise ValueError("unsupported benchmark category")
        if (case["category"] == "clean") != (case["expected_issue_type"] is None):
            raise ValueError("clean labels must have no expected issue type")
    expected = {"security": 10, "logic_runtime": 10, "clean": 10}
    counts = {category: sum(case["category"] == category for case in cases) for category in expected}
    if len(cases) != 30 or counts != expected:
        raise ValueError("benchmark must contain exactly 10 cases in each requested category")
    return cases


def _copy_retriever(copy_dir: Path, *, version: str = "v2",
                    strategy: str | None = None,
                    retrieval_config: Any = None) -> Callable[[str], list]:
    """Build a retriever over an already-created, non-canonical Chroma copy."""
    from rag.config import CHROMA_PERSIST_DIR, PROJECT_ROOT
    v3_canonical = PROJECT_ROOT / "rag" / "data" / "chroma_db_v3"
    if not copy_dir.is_dir():
        raise ValueError("RAG requires an existing Chroma database copy")
    resolved_copy = copy_dir.resolve()
    if version == "v2":
        if resolved_copy == CHROMA_PERSIST_DIR.resolve():
            raise ValueError("RAG evaluation requires a non-canonical temporary v2 Chroma copy")
        strategy = strategy or "v2_baseline"
        if strategy != "v2_baseline":
            raise ValueError("v2 supports only v2_baseline")
    elif version == "v3":
        if resolved_copy == v3_canonical.resolve():
            raise ValueError("RAG evaluation requires a non-canonical temporary v3 Chroma copy")
        from rag.retrieve_v3 import RetrievalConfig, V3Retriever
        strategy = strategy or "v3_optimized"
        config = retrieval_config or RetrievalConfig(strategy=strategy)
        retriever = V3Retriever(db_path=resolved_copy, config=config)
        return retriever.retrieve
    else:
        raise ValueError("RAG version must be v2 or v3")
    from chromadb import PersistentClient
    from chromadb.utils import embedding_functions
    from rag.config import COLLECTION_NAME, DEFAULT_K, EMBEDDING_MODEL_NAME
    from rag.retrieve import _evidence_from_query_results, _split_code_windows

    # Prevent transformers/huggingface from fetching an uncached model.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    client = PersistentClient(path=str(copy_dir))
    embedding = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL_NAME)
    collection = client.get_collection(name=COLLECTION_NAME, embedding_function=embedding)
    count = collection.count()

    def retrieve(code: str) -> list:
        if not code.strip() or not count:
            return []
        text = code.strip()
        windows = [text] if len(text) <= 1200 else _split_code_windows(text)[:5]
        result = collection.query(query_texts=windows, n_results=min(DEFAULT_K, count),
                                  include=["documents", "metadatas", "distances"])
        if len(windows) == 1:
            return _evidence_from_query_results(result)
        best = {}
        for index in range(len(windows)):
            for item in _evidence_from_query_results(result, index):
                key = item.evidence_id or item.text
                score = item.distance if item.distance is not None else float("inf")
                if key not in best or score < best[key][0]:
                    best[key] = (score, item)
        return [item for _, item in sorted(best.values(), key=lambda pair: pair[0])[:DEFAULT_K]]

    return retrieve


def run_evaluation(*, provider: Any, mode: str, rag_enabled: bool = False,
                   retriever: Optional[Callable[[str], list]] = None,
                   include_fixes: bool = False, cases: Optional[list[dict[str, Any]]] = None,
                   max_cases: int = 30, model_id: str = "scripted-provider",
                   case_ids: Optional[list[str]] = None,
                   batch_case_ids: Optional[list[str]] = None,
                   checkpoint_path: Optional[Path] = None, resume: bool = False,
                   retry_case_ids: Optional[list[str]] = None,
                   request_interval_seconds: float = 12.0,
                   rag_snapshot_sha256: Optional[str] = None,
                   include_retrieval_text_previews: bool = True,
                   rag_version: str = "v2", retrieval_strategy: str | None = None,
                   rag_snapshot_manifest_sha256: str | None = None,
                   working_copy_manifest_sha256: str | None = None,
                   stop_on_provider_error: bool = False,
                   rate_limit_controller: GroqRateLimitController | None = None,
                   rate_limit_arm: str | None = None,
                   clock: Callable[[], float] = time.time,
                   sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    if not 1 <= max_cases <= 30:
        raise ValueError("max_cases must be between 1 and 30")
    if request_interval_seconds < 0:
        raise ValueError("request interval must be non-negative")
    if rag_enabled and retriever is None:
        raise ValueError("RAG-enabled runs require an injected retriever over a temporary copy")
    if rag_version not in {"v2", "v3"}:
        raise ValueError("RAG version must be v2 or v3")
    if rag_enabled:
        expected_strategy = retrieval_strategy or ("v2_baseline" if rag_version == "v2" else "v3_optimized")
        allowed_strategies = {"v2_baseline"} if rag_version == "v2" else {"v3_baseline", "v3_optimized"}
        if expected_strategy not in allowed_strategies:
            raise ValueError(f"strategy {expected_strategy!r} is incompatible with RAG version {rag_version}")
    else:
        expected_strategy = "disabled"
    all_cases = cases if cases is not None else load_cases()
    benchmark_sha256 = _cases_digest(all_cases)
    if case_ids is not None:
        by_id = {case["case_id"]: case for case in all_cases}
        if any(case_id not in by_id for case_id in case_ids):
            raise ValueError("unknown benchmark case ID")
        all_cases = [by_id[case_id] for case_id in case_ids]
        if len(all_cases) > max_cases:
            raise ValueError("selected case IDs exceed max_cases")
    selected = all_cases[:max_cases]
    selected_ids = [case["case_id"] for case in selected]
    execution_ids = selected_ids
    if batch_case_ids is not None:
        if len(set(batch_case_ids)) != len(batch_case_ids):
            raise ValueError("duplicate batch case IDs are not allowed")
        unknown_batch = [case_id for case_id in batch_case_ids if case_id not in selected_ids]
        if unknown_batch:
            raise ValueError("batch case ID is not part of the pinned benchmark case set")
        execution_ids = list(batch_case_ids)
    structured_metadata = dict(getattr(provider, "structured_output_metadata", {
        "mode": "provider_unspecified", "strict": None,
        "schema_name": None, "schema_version": None,
    }))
    provider_client = getattr(provider, "client", None)
    sdk_max_retries = getattr(provider_client, "max_retries", None)
    if not isinstance(sdk_max_retries, int) or isinstance(sdk_max_retries, bool):
        sdk_max_retries = None
    prompt_digest = prompt_configuration_digest() if mode == "groq" else None
    scoring_digest = scoring_configuration_digest()
    config = {
        "mode": mode,
        "model_id": model_id,
        "structured_output": structured_metadata,
        "strict_output_mode": structured_metadata.get("mode"),
        "rag_enabled": rag_enabled,
        "rag_version": rag_version if rag_enabled else None,
        "retrieval_strategy": expected_strategy,
        "rag_snapshot_sha256": rag_snapshot_sha256,
        "rag_snapshot_manifest_sha256": rag_snapshot_manifest_sha256,
        "working_copy_manifest_sha256": working_copy_manifest_sha256,
        "benchmark_version": BENCHMARK_VERSION,
        "benchmark_sha256": benchmark_sha256,
        "schema_version": structured_metadata.get("schema_version") or REVIEW_SCHEMA_VERSION,
        "prompt_configuration_sha256": prompt_digest,
        "scoring_version": SCORING_VERSION,
        "scoring_configuration_sha256": scoring_digest,
        "provider_sdk_max_retries": sdk_max_retries,
        "stop_on_provider_error": stop_on_provider_error,
        "case_ids": selected_ids,
        "includes_fix_generation": include_fixes,
        "temperature": getattr(provider, "temperature", None),
        "request_interval_seconds": request_interval_seconds if mode == "groq" else 0,
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "include_retrieval_text_previews": include_retrieval_text_previews,
        "rate_limit_policy": rate_limit_controller.policy if rate_limit_controller else None,
        "rate_limit_arm": rate_limit_arm if rate_limit_controller else None,
    }
    if retry_case_ids and not resume:
        raise ValueError("individual case retry requires --resume")

    if resume:
        if checkpoint_path is None or not checkpoint_path.is_file():
            raise FileNotFoundError("resume checkpoint does not exist")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("checkpoint_version") != CHECKPOINT_VERSION:
            raise CheckpointMismatchError("unsupported checkpoint version")
        stored_config = checkpoint.get("config")
        if stored_config != config:
            keys = sorted(set(config) | set(stored_config or {}))
            changed = [key for key in keys if config.get(key) != (stored_config or {}).get(key)]
            raise CheckpointMismatchError(
                "evaluation configuration differs from checkpoint: " + ", ".join(changed)
            )
        if rate_limit_controller is not None:
            saved_rate_state = checkpoint.get("rate_limit_state")
            if saved_rate_state is not None:
                current_rate_state = rate_limit_controller.state_snapshot()
                monotonic_totals = (
                    "total_requests_attempted", "total_successful_requests",
                    "total_failed_requests", "total_retries", "total_tokens_accounted",
                )
                regressed = [name for name in monotonic_totals
                             if int(current_rate_state.get(name, 0)) < int(saved_rate_state.get(name, 0))]
                if regressed:
                    raise CheckpointMismatchError(
                        "rate-limit ledger is missing or behind the saved checkpoint: "
                        + ", ".join(regressed)
                    )
            elif checkpoint.get("provider_request_counts", {}).get("total", 0):
                raise CheckpointMismatchError(
                    "checkpoint contains provider requests but no resumable rate-limit state"
                )
    else:
        if checkpoint_path is not None and checkpoint_path.exists():
            raise FileExistsError("checkpoint already exists; use --resume or choose a new path")
        checkpoint = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "config": config,
            "case_results": {},
            "provider_request_counts": {"review": 0, "fix": 0, "total": 0},
            "last_request_at": None,
        }

    stored_cases = checkpoint.setdefault("case_results", {})
    if retry_case_ids:
        unknown = [case_id for case_id in retry_case_ids if case_id not in selected_ids]
        if unknown:
            raise ValueError("retry case is not part of this checkpoint configuration")
        for case_id in retry_case_ids:
            previous = stored_cases.get(case_id)
            if not previous or previous.get("status") == "completed":
                raise ValueError(f"case is not retryable: {case_id}")
        cases_to_run = [case for case in selected if case["case_id"] in retry_case_ids
                        and case["case_id"] in execution_ids]
    elif resume:
        cases_to_run = [case for case in selected
                        if case["case_id"] in execution_ids
                        and stored_cases.get(case["case_id"], {}).get("status") != "completed"]
    else:
        cases_to_run = [case for case in selected if case["case_id"] in execution_ids]

    if checkpoint_path is not None:
        _atomic_write_json(checkpoint_path, checkpoint)
    paced = PacedProvider(
        provider,
        interval_seconds=request_interval_seconds,
        enabled=(mode == "groq"),
        last_request_at=checkpoint.get("last_request_at"),
        rate_limit_controller=rate_limit_controller,
        clock=clock,
        sleeper=sleeper,
    )
    interrupted = False
    stopped_on_provider_error = False
    evaluation_started = time.perf_counter()
    for case in cases_to_run:
        case_id = case["case_id"]
        previous = stored_cases.get(case_id, {})
        attempt_count = int(previous.get("attempt_count", 0)) + 1
        previous_call_counts = dict(paced.request_counts)
        paced.clear_case_errors()
        start = time.perf_counter()
        retrieval_seconds = 0.0
        phase_timings: dict[str, float] = {}
        captured_retrieval: list[Any] = []

        def timed_retriever(code: str):
            nonlocal retrieval_seconds, captured_retrieval
            retrieval_started = time.perf_counter()
            try:
                retrieved = list((retriever(code) if retriever is not None else []) or [])
                captured_retrieval = retrieved
                return retrieved
            finally:
                retrieval_seconds += time.perf_counter() - retrieval_started

        try:
            result = orchestrator.run_review_pipeline(
                code=case["source_code"], file_path=f"benchmark/{case['case_id']}.py",
                notify_desktop=False, post_to_github=False, llm=paced,
                retriever=timed_retriever if rag_enabled else (lambda _code: []),
                phase_timings=phase_timings,
            )
            latency = time.perf_counter() - start
            review = result.review_response
            findings = [
                {"finding_id": f.finding_id, "issue_type": f.issue_type, "title": f.title,
                 "line_start": f.line_start, "line_end": f.line_end,
                 "evidence_ids": list(f.evidence_ids), "uncertain": f.uncertain, "abstained": f.abstained}
                for f in review.findings
            ]
            evidence_ids = {item.evidence_id for item in review.evidence if item.evidence_id}
            references_valid = all(set(item["evidence_ids"]).issubset(evidence_ids) for item in findings)
            raw_finding_diagnostics = _raw_finding_diagnostics(review.raw_response, evidence_ids)
            elapsed_total = time.perf_counter() - start
            record = {
                "case_id": case["case_id"], "category": case["category"],
                "schema_valid": bool(review.valid), "evidence_references_valid": references_valid,
                "findings": findings, "abstained": any(item["abstained"] for item in findings),
                "processing_errors": list(review.processing_errors),
                "retrieved_evidence_count": len(review.evidence),
                "review_latency_seconds": latency,
                "retrieval_diagnostics": _retrieval_diagnostics(
                    list(result.retrieved_evidence),
                    include_text_previews=include_retrieval_text_previews,
                ),
                "evidence_reference_diagnostics": raw_finding_diagnostics,
                "timings": {
                    "request_pacing_seconds": paced.case_pacing_wait_seconds,
                    "retrieval_seconds": retrieval_seconds,
                    "provider_inference_seconds": paced.case_provider_inference_seconds,
                    "validation_seconds": phase_timings.get("validation_seconds", 0.0),
                    "total_case_duration_seconds": elapsed_total,
                },
                "matching": match_findings(case, findings),
                "fix_attempted": False, "fix_generated": False, "fix_syntax_status": None,
                "fix_latency_seconds": None,
                "provider_errors": list(paced.provider_errors),
            "rate_limit_errors": list(paced.rate_limit_errors),
                "schema_errors": ([] if paced.provider_errors else [
                    error for error in review.processing_errors
                    if not error.startswith(("model_failure:", "retrieval_failure:"))
                ]),
            }
            if include_fixes and review.valid:
                actionable = next((item for item in review.findings if not item.abstained), None)
                if actionable is not None:
                    record["fix_attempted"] = True
                    fix_start = time.perf_counter()
                    candidate = orchestrator.generate_fix_candidate(
                        review_response=review, finding_ref=actionable.finding_id,
                        original_code=case["source_code"], language="python", llm=paced)
                    record["fix_latency_seconds"] = time.perf_counter() - fix_start
                    record["fix_generated"] = candidate.generation_status == "generated"
                    record["fix_generation_errors"] = list(candidate.generation_errors)
                    if record["fix_generated"]:
                        verification = orchestrator.verify_fix_candidate(
                            candidate, expected_candidate_id=candidate.candidate_id)
                        record["fix_syntax_status"] = verification.syntax_check.status
                        diff_lines = verification.unified_diff.splitlines()
                        record["fix_explanation"] = candidate.explanation
                        record["fix_assumptions"] = list(candidate.assumptions)
                        record["fix_diff"] = verification.unified_diff
                        record["fix_diff_added_lines"] = sum(
                            line.startswith("+") and not line.startswith("+++") for line in diff_lines
                        )
                        record["fix_diff_deleted_lines"] = sum(
                            line.startswith("-") and not line.startswith("---") for line in diff_lines
                        )
                        record["fix_targeting_assessment"] = (
                            "not_automatically_assessed; requires human review"
                        )
            record["timings"]["request_pacing_seconds"] = paced.case_pacing_wait_seconds
            record["timings"]["provider_inference_seconds"] = paced.case_provider_inference_seconds
            record["provider_errors"] = list(paced.provider_errors)
            fix_failed = record["fix_attempted"] and not record["fix_generated"]
            quota_deferred = bool(record.get("rate_limit_errors")) and not record.get("provider_errors") and any(
                item.get("request_sent") is False for item in record.get("rate_limit_errors", [])
            )
            record["case_status"] = (
                "quota_deferred" if quota_deferred else
                "completed" if review.valid and not review.processing_errors
                and not record["provider_errors"] and not fix_failed else "failed"
            )
        except KeyboardInterrupt:
            latency = time.perf_counter() - start
            record = {
                "case_id": case["case_id"], "category": case["category"],
                "schema_valid": False, "evidence_references_valid": False, "findings": [],
                "abstained": False, "processing_errors": ["evaluation_interrupted"],
                "retrieved_evidence_count": 0, "review_latency_seconds": latency,
                "retrieval_diagnostics": _retrieval_diagnostics(
                    captured_retrieval, include_text_previews=include_retrieval_text_previews
                ),
                "evidence_reference_diagnostics": [],
                "timings": {
                    "request_pacing_seconds": paced.case_pacing_wait_seconds,
                    "retrieval_seconds": retrieval_seconds,
                    "provider_inference_seconds": paced.case_provider_inference_seconds,
                    "validation_seconds": phase_timings.get("validation_seconds", 0.0),
                    "total_case_duration_seconds": latency,
                },
                "matching": match_findings(case, []), "fix_attempted": False,
                "fix_generated": False, "fix_syntax_status": None, "fix_latency_seconds": None,
                "provider_errors": list(paced.provider_errors), "schema_errors": [],
                "rate_limit_errors": list(paced.rate_limit_errors),
                "case_status": "interrupted",
            }
            interrupted = True
        except Exception as exc:
            latency = time.perf_counter() - start
            record = {
                "case_id": case_id, "category": case["category"],
                "schema_valid": False, "evidence_references_valid": False, "findings": [],
                "abstained": False, "processing_errors": [f"evaluation_error:{type(exc).__name__}"],
                "retrieved_evidence_count": 0, "review_latency_seconds": latency,
                "retrieval_diagnostics": _retrieval_diagnostics(
                    captured_retrieval, include_text_previews=include_retrieval_text_previews
                ),
                "evidence_reference_diagnostics": [],
                "timings": {
                    "request_pacing_seconds": paced.case_pacing_wait_seconds,
                    "retrieval_seconds": retrieval_seconds,
                    "provider_inference_seconds": paced.case_provider_inference_seconds,
                    "validation_seconds": phase_timings.get("validation_seconds", 0.0),
                    "total_case_duration_seconds": latency,
                },
                "matching": match_findings(case, []), "fix_attempted": False,
                "fix_generated": False, "fix_syntax_status": None, "fix_latency_seconds": None,
                "provider_errors": list(paced.provider_errors),
                "rate_limit_errors": list(paced.rate_limit_errors),
                "schema_errors": [], "case_status": "failed",
            }
        record["timings"]["total_case_duration_seconds"] = time.perf_counter() - start
        record["attempt_count"] = attempt_count
        stored_cases[case_id] = {
            "status": record["case_status"],
            "attempt_count": attempt_count,
            "result": record,
        }
        for name in ("review", "fix"):
            checkpoint["provider_request_counts"][name] += (
                paced.request_counts[name] - previous_call_counts[name]
            )
        checkpoint["provider_request_counts"]["total"] = sum(
            checkpoint["provider_request_counts"][key] for key in ("review", "fix")
        )
        checkpoint["last_request_at"] = paced.last_request_at
        if checkpoint_path is not None:
            if rate_limit_controller is not None:
                checkpoint["rate_limit_state"] = rate_limit_controller.state_snapshot()
            _atomic_write_json(checkpoint_path, checkpoint)
        if interrupted:
            break
        if stop_on_provider_error and (record.get("provider_errors") or record.get("rate_limit_errors")):
            stopped_on_provider_error = True
            break

    output = [stored_cases[case_id]["result"] for case_id in selected_ids if case_id in stored_cases]
    fix_latencies = [item["fix_latency_seconds"] for item in output
                     if isinstance(item.get("fix_latency_seconds"), (int, float))]
    failed_ids = [item["case_id"] for item in output if item.get("case_status") in {"failed", "interrupted"}]
    deferred_ids = [item["case_id"] for item in output if item.get("case_status") == "quota_deferred"]
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode, "model_id": model_id, "rag_enabled": rag_enabled,
        "structured_output": structured_metadata,
        "prompt_configuration_sha256": prompt_digest,
        "scoring_version": SCORING_VERSION,
        "scoring_configuration_sha256": scoring_digest,
        "provider_sdk_max_retries": sdk_max_retries,
        "case_limit": max_cases, "cases_run": len(output), "includes_fix_generation": include_fixes,
        "case_ids": [item["case_id"] for item in output],
        "batch_case_ids": execution_ids,
        "batch_case_count": len(execution_ids),
        "provider_request_counts": dict(checkpoint["provider_request_counts"]),
        "request_pacing_seconds": request_interval_seconds if mode == "groq" else 0,
        "run_duration_seconds": time.perf_counter() - evaluation_started,
        "interrupted": interrupted,
        "stopped_on_provider_error": stopped_on_provider_error,
        "failed_case_ids": failed_ids,
        "quota_deferred_case_ids": deferred_ids,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path is not None else None,
        "fix_latency_seconds": {"count": len(fix_latencies),
                                 "mean": sum(fix_latencies) / len(fix_latencies) if fix_latencies else None,
                                 "max": max(fix_latencies) if fix_latencies else None},
        "evaluation_notice": "Scripted mode validates harness mechanics only; synthetic labels are preliminary. No behavioral correctness is measured.",
        "case_results": output, "metrics": aggregate_metrics(output),
        "rate_limit_usage": rate_limit_controller.arm_usage() if rate_limit_controller else None,
        "rate_limit_state_path": str(rate_limit_controller.state_path) if rate_limit_controller else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scripted", "groq"), default="scripted")
    parser.add_argument("--enable-live-groq", action="store_true", help="Explicitly authorize bounded live Groq requests")
    parser.add_argument("--max-cases", type=int, default=30)
    parser.add_argument("--case-ids", help="Comma-separated benchmark IDs for a representative bounded run")
    parser.add_argument("--batch-case-ids", help="Execution-only case subset; checkpoint identity retains the full --case-ids benchmark set")
    parser.add_argument("--rag-copy-dir", type=Path,
                        help="Existing non-canonical consistent Chroma copy for the selected --rag-version")
    parser.add_argument("--rag-snapshot-identity", help="Pinned immutable reference snapshot directory SHA-256")
    parser.add_argument("--rag-snapshot-manifest-sha256", help="Pinned immutable snapshot manifest SHA-256")
    parser.add_argument("--working-copy-manifest-sha256", help="Pinned initial disposable working-copy identity")
    parser.add_argument("--rag-version", choices=("v2", "v3"), default="v2",
                        help="Corpus version in --rag-copy-dir (default: v2)")
    parser.add_argument("--retrieval-strategy", choices=("v2_baseline", "v3_baseline", "v3_optimized"),
                        help="Retrieval strategy; defaults to the baseline for the selected version")
    parser.add_argument("--disable-v3-parent-aware", action="store_true")
    parser.add_argument("--disable-v3-language-aware", action="store_true")
    parser.add_argument("--disable-v3-source-diversity", action="store_true")
    parser.add_argument("--disable-v3-applicability-diagnostics", action="store_true")
    parser.add_argument("--disable-v3-context-budgeting", action="store_true",
                        help="Experimental ablation only; do not use for application serving")
    parser.add_argument("--include-fixes", action="store_true", help="Allow at most one fix request per case that has a finding")
    parser.add_argument("--no-retrieval-text-previews", action="store_true",
                        help="Do not store retrieved-text previews in local diagnostics")
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/latest.json"))
    parser.add_argument("--request-interval-seconds", type=float, default=12.0,
                        help="Minimum delay between live provider calls (default: 12; offline mode ignores it)")
    parser.add_argument("--checkpoint-path", type=Path,
                        help="Atomic per-case checkpoint path; defaults beside --output")
    parser.add_argument("--resume", action="store_true",
                        help="Resume a matching checkpoint, skipping completed cases and retrying failed ones")
    parser.add_argument("--retry-case", action="append", dest="retry_case_ids",
                        help="On resume, retry only this failed case ID; may be repeated")
    parser.add_argument("--fail-on-incomplete", action="store_true",
                        help="Exit nonzero after writing results if any selected case is incomplete")
    parser.add_argument("--stop-on-provider-error", action="store_true",
                        help="Checkpoint and stop after the first provider/API error")
    parser.add_argument("--rate-limit-state-path", type=Path,
                        help="Shared account-level atomic token/request ledger (Groq mode only)")
    parser.add_argument("--rate-limit-arm", choices=("no_rag", "v2_baseline", "v3_baseline", "v3_optimized"))
    parser.add_argument("--remaining-daily-tokens", type=int,
                        help="Provider dashboard's current remaining daily-token budget; never a credential")
    parser.add_argument("--groq-rpm", type=int, default=30)
    parser.add_argument("--groq-rpd", type=int, default=1000)
    parser.add_argument("--groq-tpm", type=int, default=8000)
    parser.add_argument("--groq-tpd", type=int, default=200000)
    parser.add_argument("--rate-limit-max-wait-seconds", type=float, default=3600)
    parser.add_argument("--rate-limit-safety-fraction", type=float, default=0.8,
                        help="Fraction of provider daily/minute quotas reserved as the usable budget")
    parser.add_argument("--confirm-daily-quota-reset", action="store_true",
                        help="Explicitly start a new daily accounting window after confirming provider reset")
    args = parser.parse_args()
    if args.mode == "groq" and not args.enable_live_groq:
        parser.error("Groq mode requires --enable-live-groq")
    if args.mode == "groq" and not os.getenv("GROQ_API_KEY"):
        parser.error("GROQ_API_KEY is not configured; no live requests were made")
    if not 1 <= args.max_cases <= 30:
        parser.error("--max-cases must be between 1 and 30")
    if args.request_interval_seconds < 0:
        parser.error("--request-interval-seconds must be non-negative")
    if args.rate_limit_state_path and (args.mode != "groq" or not args.rate_limit_arm
                                       or args.remaining_daily_tokens is None):
        parser.error("rate-limit mode requires Groq mode, --rate-limit-arm, and --remaining-daily-tokens")
    if args.retry_case_ids and not args.resume:
        parser.error("--retry-case requires --resume")
    checkpoint_path = args.checkpoint_path or args.output.with_suffix(".checkpoint.json")
    rag_enabled = args.rag_copy_dir is not None
    if rag_enabled:
        selected_strategy = args.retrieval_strategy or (
            "v2_baseline" if args.rag_version == "v2" else "v3_optimized"
        )
        if args.rag_version == "v3":
            from rag.retrieve_v3 import RetrievalConfig
            retrieval_config = RetrievalConfig(
                strategy=selected_strategy,
                parent_aware=not args.disable_v3_parent_aware,
                language_aware=not args.disable_v3_language_aware,
                source_diversity=not args.disable_v3_source_diversity,
                applicability_diagnostics=not args.disable_v3_applicability_diagnostics,
                context_budgeting=not args.disable_v3_context_budgeting,
            )
        else:
            retrieval_config = None
        retriever = _copy_retriever(args.rag_copy_dir, version=args.rag_version,
                                    strategy=selected_strategy, retrieval_config=retrieval_config)
    else:
        retriever = None
    if args.mode == "groq":
        from llm.groq_client import GroqLLM
        provider = GroqLLM()
        model_id = provider.model
    else:
        provider = ScriptedProvider()
        model_id = "scripted-empty-findings-v1"
    try:
        rate_limit_controller = None
        if args.rate_limit_state_path:
            rate_limit_controller = GroqRateLimitController(
                state_path=args.rate_limit_state_path, arm=args.rate_limit_arm,
                remaining_daily_tokens=args.remaining_daily_tokens,
                rpm=args.groq_rpm, rpd=args.groq_rpd, tpm=args.groq_tpm, tpd=args.groq_tpd,
                request_interval_seconds=args.request_interval_seconds,
                safety_fraction=args.rate_limit_safety_fraction,
                max_wait_seconds=args.rate_limit_max_wait_seconds,
                confirm_daily_quota_reset=args.confirm_daily_quota_reset,
            )
        report = run_evaluation(
            provider=provider, mode=args.mode, rag_enabled=rag_enabled,
            retriever=retriever, include_fixes=args.include_fixes,
            max_cases=args.max_cases, model_id=model_id,
            case_ids=[value.strip() for value in args.case_ids.split(",") if value.strip()]
            if args.case_ids else None,
            batch_case_ids=[value.strip() for value in args.batch_case_ids.split(",") if value.strip()]
            if args.batch_case_ids else None,
            checkpoint_path=checkpoint_path, resume=args.resume,
            retry_case_ids=args.retry_case_ids,
            request_interval_seconds=args.request_interval_seconds,
            rag_snapshot_sha256=(args.rag_snapshot_identity or _directory_digest(args.rag_copy_dir)) if rag_enabled else None,
            include_retrieval_text_previews=not args.no_retrieval_text_previews,
            rag_version=args.rag_version,
            retrieval_strategy=(args.retrieval_strategy if rag_enabled else None),
            rag_snapshot_manifest_sha256=args.rag_snapshot_manifest_sha256 if rag_enabled else None,
            working_copy_manifest_sha256=args.working_copy_manifest_sha256 if rag_enabled else None,
            stop_on_provider_error=args.stop_on_provider_error,
            rate_limit_controller=rate_limit_controller,
            rate_limit_arm=args.rate_limit_arm,
        )
    except (CheckpointMismatchError, FileExistsError, FileNotFoundError,
            RateLimitBudgetError, ValueError) as exc:
        parser.error(str(exc))
    _atomic_write_json(args.output, report)
    print(json.dumps({"output": str(args.output), "cases_run": report["cases_run"],
                      "metrics": report["metrics"]}, indent=2))
    batch_results = {row["case_id"]: row for row in report["case_results"]}
    batch_incomplete = any(batch_results.get(case_id, {}).get("case_status") != "completed"
                           for case_id in report["batch_case_ids"])
    if args.fail_on_incomplete and (report["interrupted"] or report["stopped_on_provider_error"]
                                    or report["failed_case_ids"] or report["quota_deferred_case_ids"]
                                    or batch_incomplete):
        parser.exit(2, "Evaluation incomplete; results/checkpoint were saved. Resume this configuration before starting the next one.\n")


if __name__ == "__main__":
    main()

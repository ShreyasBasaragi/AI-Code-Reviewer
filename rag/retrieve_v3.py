"""Read-only retrieval strategies for the validated v3 Chroma corpus.

This module never creates a Chroma collection. Query vectors use the pinned
SentenceTransformer model only when it is already cached locally.
"""
from __future__ import annotations

import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rag.config import PROJECT_ROOT
from rag.retrieve import _split_code_windows
from rag.v3.build import COLLECTION_NAME, MODEL_ID, _load_local_embedding_model
from rag.v3.language import LanguageResult, classify_language
from review_schema import RetrievedEvidence


class RAGConfigurationError(RuntimeError):
    """Selected RAG corpus or strategy is unavailable or invalid."""


@dataclass(frozen=True)
class RetrievalConfig:
    strategy: str = "v3_optimized"
    top_k: int = 4
    candidate_pool: int = 32
    max_windows: int = 5
    parent_aware: bool = True
    language_aware: bool = True
    source_diversity: bool = True
    applicability_diagnostics: bool = True
    context_budgeting: bool = True
    evidence_token_budget: int = 1600
    prompt_token_reserve: int = 1200
    total_prompt_token_budget: int = 8192
    chars_per_estimated_token: int = 3
    language_adjustment: float = 0.04
    diversity_tie_band: float = 0.025

    def __post_init__(self) -> None:
        if self.strategy not in {"v3_baseline", "v3_optimized"}:
            raise ValueError("v3 strategy must be v3_baseline or v3_optimized")
        if min(self.top_k, self.candidate_pool, self.max_windows,
               self.evidence_token_budget, self.prompt_token_reserve,
               self.total_prompt_token_budget, self.chars_per_estimated_token) < 1:
            raise ValueError("retrieval limits must be positive")


@dataclass
class _Candidate:
    evidence: RetrievedEvidence
    original_rank: int
    original_distance: float
    query_window: int
    language_status: str
    language_penalty: float = 0.0
    selection_reason: str = "vector_distance"
    assembled_chunk_ids: list[str] = field(default_factory=list)
    context_expansion: list[dict[str, Any]] = field(default_factory=list)


_PINNED_EMBEDDER: Any = None
_PINNED_EMBEDDER_REVISION: str | None = None
_READONLY_COLLECTIONS: dict[tuple[str, str], tuple[Any, Any]] = {}


class V3Retriever:
    """Injectable, read-only v3 retriever. ``db_path`` is explicit for isolated copies."""

    def __init__(self, *, db_path: Path | None = None, config: RetrievalConfig | None = None,
                 collection: Any = None, embedder: Any = None,
                 collection_name: str = COLLECTION_NAME,
                 language_classifier: Callable[..., LanguageResult] = classify_language) -> None:
        self.db_path = (db_path or PROJECT_ROOT / "rag" / "data" / "chroma_db_v3").resolve()
        self.config = config or RetrievalConfig()
        self._collection = collection
        self._embedder = embedder
        self.collection_name = collection_name
        self.language_classifier = language_classifier

    def _open(self):
        if self._collection is not None:
            return self._collection
        if not self.db_path.is_dir():
            raise RAGConfigurationError(
                f"v3 Chroma database is missing at {self.db_path}; build or copy the validated v3 corpus first"
            )
        try:
            import chromadb
            key = (str(self.db_path), self.collection_name)
            if key not in _READONLY_COLLECTIONS:
                client = chromadb.PersistentClient(path=str(self.db_path))
                # Deliberately no create_collection/get_or_create_collection and no embedding function.
                collection = client.get_collection(name=self.collection_name)
                _READONLY_COLLECTIONS[key] = (client, collection)
            self._client, self._collection = _READONLY_COLLECTIONS[key]
            return self._collection
        except Exception as exc:
            raise RAGConfigurationError(
                f"cannot open existing v3 collection {self.collection_name!r}: {type(exc).__name__}"
            ) from exc

    def _get_embedder(self):
        global _PINNED_EMBEDDER, _PINNED_EMBEDDER_REVISION
        if self._embedder is None:
            if _PINNED_EMBEDDER is None:
                try:
                    _PINNED_EMBEDDER, _PINNED_EMBEDDER_REVISION = _load_local_embedding_model(MODEL_ID)
                except Exception as exc:
                    raise RAGConfigurationError(
                        "the pinned v3 embedding model is not available in the local cache; no download was attempted"
                    ) from exc
            self._embedder = _PINNED_EMBEDDER
            self._model_revision = _PINNED_EMBEDDER_REVISION
        return self._embedder

    @staticmethod
    def _token_estimate(text: str, chars_per_token: int) -> int:
        # Conservative, reproducible fallback; the Qwen/Groq generation tokenizer is not loaded here.
        return max(1, math.ceil(len(text.encode("utf-8")) / chars_per_token)) if text else 0

    @staticmethod
    def _metadata(results: dict[str, Any], q: int, i: int) -> dict[str, Any]:
        rows = results.get("metadatas") or []
        if q < len(rows) and i < len(rows[q]) and isinstance(rows[q][i], dict):
            return rows[q][i]
        return {}

    def _query(self, collection: Any, windows: list[str], pool: int) -> dict[str, Any]:
        embedder = self._get_embedder()
        import numpy as np
        encoded = np.asarray(embedder.encode(
            windows, batch_size=16, convert_to_numpy=True,
            normalize_embeddings=False, show_progress_bar=False,
        ), dtype=np.float32)
        if encoded.ndim == 1:
            encoded = encoded.reshape(1, -1)
        return collection.query(
            query_embeddings=encoded.tolist(), n_results=pool,
            include=["documents", "metadatas", "distances"],
        )

    def _collect(self, results: dict[str, Any], window_count: int,
                 input_language: LanguageResult) -> list[_Candidate]:
        grouped: dict[str, _Candidate] = {}
        ordinal = 0
        for q in range(window_count):
            ids = (results.get("ids") or [[]])[q] if q < len(results.get("ids") or []) else []
            docs = (results.get("documents") or [[]])[q] if q < len(results.get("documents") or []) else []
            distances = (results.get("distances") or [[]])[q] if q < len(results.get("distances") or []) else []
            for i, text in enumerate(docs):
                if not isinstance(text, str) or not text.strip() or i >= len(ids):
                    continue
                raw_distance = distances[i] if i < len(distances) else float("inf")
                try:
                    distance = float(raw_distance)
                except (TypeError, ValueError):
                    distance = float("inf")
                if not math.isfinite(distance):
                    distance = float("inf")
                metadata = self._metadata(results, q, i)
                chunk_id = str(ids[i])
                evidence = RetrievedEvidence(
                    evidence_id=chunk_id,
                    source_family=metadata.get("source_family"),
                    source_url=metadata.get("source_url"),
                    rule_identifier=(metadata.get("rule_id") or metadata.get("cwe_id")),
                    text=text,
                    language=metadata.get("language"), metadata=metadata, distance=distance,
                )
                ordinal += 1
                status = self._language_status(metadata.get("language"), input_language)
                candidate = _Candidate(evidence, ordinal, distance, q, status,
                                       assembled_chunk_ids=[chunk_id])
                if chunk_id not in grouped or distance < grouped[chunk_id].original_distance:
                    grouped[chunk_id] = candidate
        return sorted(grouped.values(), key=lambda c: (c.original_distance, c.evidence.evidence_id or ""))

    @staticmethod
    def _language_status(candidate_language: Any, input_language: LanguageResult) -> str:
        language = str(candidate_language or "unknown").lower()
        if language in {"", "unknown", "language_agnostic"}:
            return "unknown_or_agnostic"
        if input_language.language == "unknown":
            return "input_unknown"
        return "exact_match" if language == input_language.language else "confident_mismatch"

    def _applicability(self, candidate: _Candidate, code: str) -> dict[str, Any]:
        metadata = candidate.evidence.metadata or {}
        family = str(candidate.evidence.source_family or metadata.get("source_family", ""))
        details: dict[str, Any] = {"status": "unknown", "basis": "no reliable structured applicability signal"}
        if family == "bandit":
            text = candidate.evidence.text.lower()
            code_lower = code.lower()
            if "shell=true" in text or "shell=True" in candidate.evidence.text:
                if "shell=false" in code_lower and "shell=true" not in code_lower:
                    return {"status": "likely_mismatch", "basis": "rule describes shell=True; reviewed code explicitly shows shell=False"}
                if "shell=true" in code_lower:
                    return {"status": "condition_present", "basis": "both rule and code contain shell=True; not proof of a vulnerability"}
            rule = str(metadata.get("rule_id", ""))
            return {"status": "unknown", "basis": "Bandit rule metadata lacks an exact matching API condition", "rule_id": rule or None}
        if family in {"mitre_cwe", "owasp_cheatsheets"}:
            return {"status": "topical_reference_only", "basis": "general guidance does not prove the code has a defect"}
        if family == "microsoft_historical_reviews":
            return {"status": "historical_unverified", "basis": "historical review comment is not a verified defect label"}
        if family in {"pep8", "google_style"}:
            return {"status": "style_guidance", "basis": "style guidance is not security or correctness proof"}
        return details

    def _expand_parent(self, candidate: _Candidate, effective_evidence_budget: int) -> None:
        collection = self._open()
        metadata = dict(candidate.evidence.metadata or {})
        parent_id = metadata.get("record_id")
        if not parent_id or not hasattr(collection, "get"):
            return
        try:
            siblings = collection.get(where={"record_id": parent_id}, include=["documents", "metadatas"])
        except Exception:
            candidate.context_expansion.append({"status": "unavailable", "reason": "sibling_lookup_failed"})
            return
        ids = siblings.get("ids", [])
        docs = siblings.get("documents", [])
        metas = siblings.get("metadatas", [])
        rows = []
        for index, sibling_id in enumerate(ids):
            if index >= len(docs) or index >= len(metas) or sibling_id == candidate.evidence.evidence_id:
                continue
            sibling_meta = metas[index] or {}
            role = str(sibling_meta.get("role", ""))
            source_field = str(sibling_meta.get("source_field", ""))
            primary_role = str(metadata.get("role", ""))
            primary_field = str(metadata.get("source_field", ""))
            # Retain comment links for split review/patch records; otherwise only adjacent same-field spans.
            linked_comment = primary_role == "patch_hunk" and role == "review_comment"
            adjacent = (source_field == primary_field and
                        abs(int(sibling_meta.get("chunk_index", 0)) - int(metadata.get("chunk_index", 0))) == 1)
            if linked_comment or adjacent:
                rows.append((0 if linked_comment else 1, int(sibling_meta.get("chunk_index", 0)), sibling_id,
                             docs[index], sibling_meta))
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        expanded = candidate.evidence.text
        used = [candidate.evidence.evidence_id]
        limit = max(1, effective_evidence_budget // 3)
        for _priority, _index, sibling_id, text, sibling_meta in rows:
            if not isinstance(text, str) or not text.strip():
                continue
            addition = "\n\n[Related source chunk]\n" + text
            if self._token_estimate(expanded + addition, self.config.chars_per_estimated_token) > limit:
                continue
            expanded += addition
            used.append(str(sibling_id))
            candidate.context_expansion.append({"chunk_id": str(sibling_id), "role": sibling_meta.get("role"),
                                                "reason": "linked_review_comment" if _priority == 0 else "adjacent_chunk"})
            if len(used) >= 3:
                break
        if len(used) > 1:
            candidate.evidence = RetrievedEvidence(
                evidence_id=candidate.evidence.evidence_id,
                source_family=candidate.evidence.source_family,
                source_url=candidate.evidence.source_url,
                rule_identifier=candidate.evidence.rule_identifier,
                text=expanded, language=candidate.evidence.language,
                metadata={**metadata, "assembled_chunk_ids": used,
                          "context_expansion": candidate.context_expansion},
                distance=candidate.evidence.distance,
            )
            candidate.assembled_chunk_ids = used

    def retrieve_with_diagnostics(self, code: str, *, language: str | None = None,
                                  file_path: str | None = None) -> tuple[list[RetrievedEvidence], dict[str, Any]]:
        started = time.perf_counter()
        if not isinstance(code, str) or not code.strip():
            return [], {"empty_result_reason": "empty_code", "selected_count": 0}
        collection = self._open()
        count = int(collection.count())
        if count == 0:
            return [], {"empty_result_reason": "empty_collection", "candidate_count": 0, "selected_count": 0}
        config = self.config
        input_language = self.language_classifier(code, explicit_language=language, file_path=file_path)
        clean = code.strip()
        code_token_estimate = self._token_estimate(clean, config.chars_per_estimated_token)
        effective_evidence_budget = min(
            config.evidence_token_budget,
            max(0, config.total_prompt_token_budget - code_token_estimate - config.prompt_token_reserve),
        )
        windows = [clean] if len(clean) <= 1200 else _split_code_windows(clean)[:config.max_windows]
        candidate_limit = min(config.top_k, count) if config.strategy == "v3_baseline" else min(config.candidate_pool, count)
        query_started = time.perf_counter()
        results = self._query(collection, windows, candidate_limit)
        retrieval_seconds = time.perf_counter() - query_started
        candidates = self._collect(results, len(windows), input_language)
        candidates.sort(key=lambda c: (c.original_distance, c.evidence.evidence_id or ""))
        vector_ranks = {id(item): rank for rank, item in enumerate(candidates, 1)}
        diagnostics_candidates = []
        selected: list[_Candidate] = []
        seen_parents: set[str] = set()
        source_counts: Counter[str] = Counter()
        optimized = config.strategy == "v3_optimized"
        if optimized:
            # Small, explicit ordinal tie-break only; never discard mismatched/unknown evidence.
            for item in candidates:
                if config.language_aware:
                    if item.language_status == "exact_match":
                        item.language_penalty = -config.language_adjustment
                    elif item.language_status == "confident_mismatch":
                        item.language_penalty = config.language_adjustment
                    if item.language_penalty:
                        item.selection_reason = "language_tie_adjustment"
            candidates.sort(key=lambda c: (c.original_distance + c.language_penalty,
                                           c.original_distance, c.evidence.evidence_id or ""))
            for item in candidates:
                parent = str((item.evidence.metadata or {}).get("record_id", item.evidence.evidence_id))
                if config.parent_aware and parent in seen_parents:
                    item.selection_reason = "excluded_duplicate_parent"
                    continue
                family = str(item.evidence.source_family or "unknown")
                if (config.source_diversity and selected and
                        source_counts[family] >= 2 and
                        item.original_distance > selected[-1].original_distance + config.diversity_tie_band):
                    item.selection_reason = "excluded_source_redundancy_outside_tie_band"
                    continue
                if config.context_budgeting:
                    token_count = self._token_estimate(item.evidence.to_prompt_text(), config.chars_per_estimated_token)
                    if token_count > effective_evidence_budget:
                        item.selection_reason = "excluded_single_item_over_budget"
                        continue
                selected.append(item)
                seen_parents.add(parent)
                source_counts[family] += 1
                if len(selected) >= config.top_k:
                    break
            if config.parent_aware:
                for item in selected:
                    self._expand_parent(item, effective_evidence_budget)
            # Apply total evidence budget after optional, bounded sibling expansion.
            if config.context_budgeting:
                budgeted, used = [], 0
                for item in selected:
                    cost = self._token_estimate(item.evidence.to_prompt_text(), config.chars_per_estimated_token)
                    if used + cost <= effective_evidence_budget:
                        budgeted.append(item)
                        used += cost
                        item.evidence = RetrievedEvidence(
                            evidence_id=item.evidence.evidence_id,
                            source_family=item.evidence.source_family, source_url=item.evidence.source_url,
                            rule_identifier=item.evidence.rule_identifier, text=item.evidence.text,
                            language=item.evidence.language,
                            metadata={**dict(item.evidence.metadata or {}), "estimated_tokens": cost,
                                      "parent_id": dict(item.evidence.metadata or {}).get("record_id")},
                            distance=item.evidence.distance,
                        )
                    else:
                        item.selection_reason = "excluded_total_context_budget"
                selected = budgeted
        else:
            selected = candidates[:config.top_k]
            for item in selected:
                source_counts[str(item.evidence.source_family or "unknown")] += 1
        selected_ids = {id(item) for item in selected}
        for final_rank, item in enumerate(selected, 1):
            raw_metadata = dict(item.evidence.metadata or {})
            # Only stable provenance needed by the existing prompt renderer is sent to the model.
            md = {key: raw_metadata[key] for key in (
                "record_id", "source_document_id", "source_category", "source_revision",
                "role", "source_field", "chunk_index", "chunk_count",
            ) if key in raw_metadata}
            md.update({"parent_id": raw_metadata.get("record_id"),
                       "assembled_chunk_ids": item.assembled_chunk_ids,
                       "language_match_status": item.language_status})
            item.evidence = RetrievedEvidence(
                evidence_id=item.evidence.evidence_id, source_family=item.evidence.source_family,
                source_url=item.evidence.source_url, rule_identifier=item.evidence.rule_identifier,
                text=item.evidence.text, language=item.evidence.language, metadata=md,
                distance=item.original_distance,
            )
        if optimized and config.context_budgeting:
            # Budget the actual prompt serialization (including provenance metadata), not only source text.
            budgeted: list[_Candidate] = []
            used = 0
            for item in selected:
                cost = self._token_estimate(item.evidence.to_prompt_text(), config.chars_per_estimated_token)
                if used + cost <= effective_evidence_budget:
                    budgeted.append(item)
                    used += cost
                else:
                    item.selection_reason = "excluded_serialized_prompt_context_budget"
            selected = budgeted
        selected_ids = {id(item) for item in selected}
        final_ranks = {id(item): rank for rank, item in enumerate(selected, 1)}
        for item in candidates:
            diagnostics_candidates.append({
                "chunk_id": item.evidence.evidence_id,
                "parent_id": (item.evidence.metadata or {}).get("record_id"),
                "source_family": item.evidence.source_family,
                "vector_rank": vector_ranks.get(id(item)), "distance": item.original_distance,
                "final_rank": final_ranks.get(id(item)),
                "language_status": item.language_status,
                "selected": id(item) in selected_ids,
                "selection_reason": item.selection_reason,
                "assembled_chunk_ids": item.assembled_chunk_ids,
                "context_expansion": item.context_expansion,
                "estimated_tokens": self._token_estimate(item.evidence.to_prompt_text(), config.chars_per_estimated_token),
                "applicability": (self._applicability(item, code)
                                  if config.applicability_diagnostics and optimized else None),
            })
        diagnostics = {
            "corpus_version": "v3", "collection": self.collection_name,
            "embedding_model_id": MODEL_ID,
            "embedding_model_revision": getattr(self, "_model_revision", None),
            "distance_metric": "cosine",
            "strategy": config.strategy, "retrieval_config": {
                "top_k": config.top_k, "candidate_pool": candidate_limit,
                "parent_aware": config.parent_aware if optimized else False,
                "language_aware": config.language_aware if optimized else False,
                "source_diversity": config.source_diversity if optimized else False,
                "applicability_diagnostics": config.applicability_diagnostics if optimized else False,
                "context_budgeting": config.context_budgeting if optimized else False,
                "evidence_token_budget": config.evidence_token_budget,
                "estimated_code_tokens": code_token_estimate,
                "effective_evidence_token_budget": effective_evidence_budget,
                "reserved_prompt_tokens": config.prompt_token_reserve,
                "total_prompt_token_budget": config.total_prompt_token_budget,
                "token_estimator": f"UTF-8 bytes / {config.chars_per_estimated_token}, rounded up",
                "language_adjustment": config.language_adjustment,
                "diversity_tie_band": config.diversity_tie_band,
            },
            "input_language": {"language": input_language.language, "origin": input_language.origin,
                               "ordinal_confidence": input_language.confidence, "evidence": input_language.evidence},
            "query_window_count": len(windows), "collection_count": count,
            "candidate_count": len(candidates), "selected_count": len(selected),
            "parent_deduplication_count": max(0, len(candidates) - len({str((c.evidence.metadata or {}).get('record_id', c.evidence.evidence_id)) for c in candidates})),
            "source_distribution": dict(Counter(str(i.evidence.source_family or "unknown") for i in selected)),
            "language_distribution": dict(Counter(i.language_status for i in candidates)),
            "evidence_tokens_used": sum(self._token_estimate(i.evidence.to_prompt_text(), config.chars_per_estimated_token) for i in selected),
            "retrieval_seconds": retrieval_seconds, "total_seconds": time.perf_counter() - started,
            "candidates": diagnostics_candidates,
            "empty_result_reason": None if selected else ("no_candidates" if not candidates else "all_candidates_excluded"),
        }
        return [item.evidence for item in selected], diagnostics

    def retrieve(self, code: str, *, language: str | None = None,
                 file_path: str | None = None) -> list[RetrievedEvidence]:
        return self.retrieve_with_diagnostics(code, language=language, file_path=file_path)[0]


_DEFAULT_V3: dict[tuple[str, str], V3Retriever] = {}


def retrieve_evidence_v3(code: str, *, strategy: str = "v3_optimized",
                         language: str | None = None, file_path: str | None = None) -> list[RetrievedEvidence]:
    key = (str(PROJECT_ROOT / "rag" / "data" / "chroma_db_v3"), strategy)
    retriever = _DEFAULT_V3.setdefault(key, V3Retriever(config=RetrievalConfig(strategy=strategy)))
    return retriever.retrieve(code, language=language, file_path=file_path)


def retrieval_backend_selection(version: str | None = None,
                                strategy: str | None = None) -> tuple[str, str]:
    selected_version = (version or os.getenv("AI_CODE_REVIEWER_RAG_VERSION") or "v2").strip().lower()
    if selected_version not in {"v2", "v3"}:
        raise RAGConfigurationError(f"unsupported RAG version {selected_version!r}; expected 'v2' or 'v3'")
    if strategy is None:
        strategy = os.getenv("AI_CODE_REVIEWER_RAG_STRATEGY")
    if selected_version == "v2":
        resolved = strategy or "v2_baseline"
        if resolved != "v2_baseline":
            raise RAGConfigurationError("v2 supports only the v2_baseline retrieval strategy")
    else:
        resolved = strategy or "v3_optimized"
        if resolved not in {"v3_baseline", "v3_optimized"}:
            raise RAGConfigurationError("v3 strategy must be v3_baseline or v3_optimized")
    return selected_version, resolved

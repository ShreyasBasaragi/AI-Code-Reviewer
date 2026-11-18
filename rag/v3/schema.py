"""Typed canonical records for the v3 preparation pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from rag.v3 import CORPUS_VERSION, INGESTION_VERSION

SCALAR_TYPES = (str, int, float, bool)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    payload = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_without_none(item) for item in value if item is not None]
    return value


def _contains_none(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_none(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_none(item) for item in value)
    return False


def stable_id(kind: str, source_family: str, source_revision: str, source_identity: str) -> str:
    """Stable, path-independent ID scoped to source and pinned source revision."""
    identity = "\x1f".join((kind, source_family, source_revision, source_identity))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    safe_kind = re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_")
    return f"v3_{safe_kind}_{digest}"


@dataclass(frozen=True)
class V3ParentRecord:
    """Logical source record; ``fields`` retain structured, unabridged content."""

    source_document_id: str
    record_id: str
    source_family: str
    source_category: str
    record_type: str
    source_revision: str
    content_hash: str
    language: str = "unknown"
    language_origin: str = "unknown"
    language_confidence: float = 0.0
    source_url: str | None = None
    source_identity: str | None = None
    source_status: str = "active"
    fields: Mapping[str, Any] = field(default_factory=dict)
    corpus_version: str = CORPUS_VERSION
    ingestion_version: str = INGESTION_VERSION

    def validate(self) -> None:
        required_strings = (
            "source_document_id", "record_id", "source_family", "source_category",
            "record_type", "source_revision", "content_hash", "language", "language_origin",
        )
        for name in required_strings:
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if (isinstance(self.language_confidence, bool)
                or not isinstance(self.language_confidence, (int, float))
                or not math.isfinite(self.language_confidence)
                or not 0.0 <= self.language_confidence <= 1.0):
            raise ValueError("language_confidence must be between 0 and 1")
        if self.source_status not in {"active", "tombstone"}:
            raise ValueError("source_status must be active or tombstone")
        if not isinstance(self.fields, Mapping):
            raise ValueError("fields must be a mapping")
        if _contains_none(self.fields):
            raise ValueError("omit unavailable optional parent fields instead of storing null")
        try:
            json.dumps(self.fields, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("parent fields must contain finite JSON-compatible values") from exc

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return _without_none(asdict(self))


@dataclass(frozen=True)
class V3EmbeddingChunk:
    """Embedding text plus source-preserving span and Chroma-compatible metadata."""

    chunk_id: str
    source_document_id: str
    record_id: str
    source_family: str
    source_category: str
    record_type: str
    source_revision: str
    chunk_index: int
    chunk_count: int
    content_hash: str
    language: str
    language_origin: str
    language_confidence: float
    embedding_text: str
    token_count: int
    source_span: str = ""
    source_field: str = "content"
    source_span_start: int | None = None
    source_span_end: int | None = None
    role: str = "content"
    related_chunk_ids: tuple[str, ...] = ()
    source_url: str | None = None
    corpus_version: str = CORPUS_VERSION
    ingestion_version: str = INGESTION_VERSION

    def validate(self) -> None:
        for name in (
            "chunk_id", "source_document_id", "record_id", "source_family",
            "source_category", "record_type", "source_revision", "content_hash",
            "language", "language_origin", "embedding_text", "role",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if (isinstance(self.chunk_index, bool) or not isinstance(self.chunk_index, int)
                or isinstance(self.chunk_count, bool) or not isinstance(self.chunk_count, int)
                or self.chunk_index < 0 or self.chunk_count < 1
                or self.chunk_index >= self.chunk_count):
            raise ValueError("invalid chunk index/count")
        if isinstance(self.token_count, bool) or not isinstance(self.token_count, int) or self.token_count < 0:
            raise ValueError("token_count cannot be negative")
        if not self.source_field:
            raise ValueError("source_field must be a non-empty string")
        if (isinstance(self.language_confidence, bool)
                or not isinstance(self.language_confidence, (int, float))
                or not math.isfinite(self.language_confidence)
                or not 0.0 <= self.language_confidence <= 1.0):
            raise ValueError("language_confidence must be between 0 and 1")
        if self.source_span_start is not None and self.source_span_start < 0:
            raise ValueError("source_span_start cannot be negative")
        if self.source_span_end is not None and self.source_span_end < 0:
            raise ValueError("source_span_end cannot be negative")
        if (self.source_span_start is None) != (self.source_span_end is None):
            raise ValueError("source span offsets must both be present or both omitted")
        if self.source_span_start is not None and self.source_span_end < self.source_span_start:
            raise ValueError("source span end precedes start")

    def metadata(self) -> dict[str, str | int | float | bool]:
        """Return only supported scalar fields; omit unavailable values."""
        self.validate()
        values: dict[str, Any] = {
            "corpus_version": self.corpus_version,
            "ingestion_version": self.ingestion_version,
            "source_family": self.source_family,
            "source_category": self.source_category,
            "source_document_id": self.source_document_id,
            "record_id": self.record_id,
            "record_type": self.record_type,
            "chunk_index": self.chunk_index,
            "chunk_count": self.chunk_count,
            "content_hash": self.content_hash,
            "language": self.language,
            "language_origin": self.language_origin,
            "language_confidence": self.language_confidence,
            "source_revision": self.source_revision,
            "role": self.role,
            "source_field": self.source_field,
        }
        if self.source_url:
            values["source_url"] = self.source_url
        invalid = [key for key, value in values.items() if not isinstance(value, SCALAR_TYPES)]
        if invalid:
            raise ValueError("Chroma metadata contains unsupported values: " + ", ".join(invalid))
        return values

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        value = asdict(self)
        value["related_chunk_ids"] = list(self.related_chunk_ids)
        return _without_none(value)


def make_parent_record(**kwargs: Any) -> V3ParentRecord:
    """Construct a parent record with a hash over structured source content."""
    structured_fields = kwargs.pop("fields")
    kwargs["content_hash"] = content_hash(structured_fields)
    record = V3ParentRecord(fields=structured_fields, **kwargs)
    record.validate()
    return record


def parent_upsert_action(existing: V3ParentRecord | None, incoming: V3ParentRecord) -> str:
    """Classify a future incremental upsert without performing storage writes."""
    incoming.validate()
    if existing is None:
        return "tombstone" if incoming.source_status == "tombstone" else "insert"
    existing.validate()
    if (existing.record_id != incoming.record_id
            or existing.source_family != incoming.source_family
            or existing.source_identity != incoming.source_identity):
        return "identity_conflict"
    if incoming.source_status == "tombstone":
        return "tombstone"
    if (existing.content_hash == incoming.content_hash
            and existing.source_revision == incoming.source_revision
            and existing.source_status == incoming.source_status):
        return "unchanged"
    return "update"

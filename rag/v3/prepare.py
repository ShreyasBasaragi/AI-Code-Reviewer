"""Build deterministic v3 canonical parents/chunks without embeddings or ChromaDB."""

from __future__ import annotations

import hashlib
import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from rag.v3 import INGESTION_VERSION
from rag.v3.chunking import SourceSpan, chunk_source_text
from rag.v3.schema import V3EmbeddingChunk, V3ParentRecord, canonical_json, content_hash, stable_id
from rag.v3.sources import SOURCE_FAMILIES, load_all_local_sources

SOURCE_ALLOCATIONS = {
    "microsoft_historical_reviews", "mitre_cwe", "owasp_cheatsheets",
    "bandit", "pep8", "google_style",
}


@dataclass(frozen=True)
class PreparationConfig:
    """Allocations are logical parent-record limits, never chunk limits."""

    max_chunk_tokens: int = 200
    parent_limits: dict[str, int] = field(default_factory=dict)
    max_microsoft_source_records: int | None = None
    tokenizer_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    allocation_unit: str = "logical_parent_records"

    def validate(self) -> None:
        if self.max_chunk_tokens < 1:
            raise ValueError("max_chunk_tokens must be positive")
        if self.allocation_unit != "logical_parent_records":
            raise ValueError("v3 allocations must use logical_parent_records")
        unknown = set(self.parent_limits) - SOURCE_ALLOCATIONS
        if unknown:
            raise ValueError("unknown source allocation(s): " + ", ".join(sorted(unknown)))
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in self.parent_limits.values()):
            raise ValueError("parent allocation limits must be non-negative integers")


@dataclass
class PreparedCorpus:
    parents: list[V3ParentRecord]
    chunks: list[V3EmbeddingChunk]
    rejected: list[dict[str, Any]]
    duplicate_candidates: list[dict[str, Any]]
    manifest: dict[str, Any]


def _select_parents(parents: list[V3ParentRecord], limits: dict[str, int]) -> list[V3ParentRecord]:
    """Apply deterministic independent parent limits before chunk expansion."""
    selected = []
    grouped: dict[str, list[V3ParentRecord]] = defaultdict(list)
    for parent in parents:
        grouped[parent.source_family].append(parent)
    for family in sorted(grouped):
        ordered = sorted(grouped[family], key=lambda item: item.record_id)
        selected.extend(ordered[:limits[family]] if family in limits else ordered)
    return sorted(selected, key=lambda item: (item.source_family, item.record_id))


def _token_count(tokenizer, text: str) -> int:
    encoded = tokenizer.encode(text, add_special_tokens=False)
    return len(encoded[0] if encoded and isinstance(encoded[0], list) else encoded)


def _parent_prefix(parent: V3ParentRecord) -> str:
    fields = parent.fields
    if parent.source_family == "mitre_cwe":
        return f"MITRE CWE {fields.get('cwe_id', '')}: {fields.get('weakness_name', '')}\n"
    if parent.source_family == "owasp_cheatsheets":
        return f"OWASP {fields.get('cheatsheet_title', '')} — {fields.get('section_title', '')}\n"
    if parent.source_family == "bandit":
        rule_id = fields.get("rule_id", "")
        prefix = f"Bandit rule {rule_id} " if rule_id else "Bandit rule "
        return prefix + str(fields.get("rule_function_name", "")) + "\n"
    if parent.source_family in {"pep8", "google_style"}:
        return f"{fields.get('guide_id', '')} — {fields.get('section_title', '')}\n"
    return ""


def _body_field(parent: V3ParentRecord) -> tuple[str, str]:
    fields = parent.fields
    if parent.source_family == "mitre_cwe":
        pieces = [str(fields.get("description", ""))]
        if fields.get("extended_description"):
            pieces.extend(["\n\nEXTENDED DETAILS:\n", str(fields["extended_description"])])
        mitigations = fields.get("mitigations", [])
        if mitigations:
            pieces.extend(["\n\nMITIGATIONS:\n", "\n".join(str(item) for item in mitigations)])
        return "description_and_mitigation", "".join(pieces)
    if parent.source_family == "owasp_cheatsheets":
        return "section_text", str(fields.get("section_text", ""))
    if parent.source_family == "bandit":
        return "rule_condition_and_description", str(fields.get("rule_condition_and_description", ""))
    return "rule_text", str(fields.get("rule_text", ""))


def _simhash64(text: str) -> int:
    words = re.findall(r"[a-z0-9_]+", text.casefold())
    shingles = [" ".join(words[index:index + 3]) for index in range(max(1, len(words) - 2))]
    if not shingles:
        shingles = words or [""]
    votes = [0] * 64
    for shingle in shingles:
        value = int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            votes[bit] += 1 if value & (1 << bit) else -1
    return sum(1 << bit for bit, vote in enumerate(votes) if vote >= 0)


def find_duplicate_candidates(parents: list[V3ParentRecord]) -> list[dict[str, Any]]:
    """Return exact/near-duplicate candidates for review; never deletes records."""
    exact: dict[str, list[V3ParentRecord]] = defaultdict(list)
    for parent in parents:
        exact[parent.content_hash].append(parent)
    candidates = []
    seen_pairs: set[tuple[str, str]] = set()
    for digest, group in sorted(exact.items()):
        if len(group) > 1:
            for left, right in itertools.combinations(sorted(group, key=lambda item: item.record_id), 2):
                pair = tuple(sorted((left.record_id, right.record_id)))
                seen_pairs.add(pair)
                candidates.append({
                    "record_id_a": pair[0], "record_id_b": pair[1],
                    "kind": "exact_content_hash", "content_hash": digest,
                    "review_required": True,
                })
    bands: dict[tuple[str, int, int], set[int]] = defaultdict(set)
    hashes = [_simhash64(parent.content_hash + " " + canonical_json(parent.fields)) for parent in parents]
    # Four 16-bit bands generate candidate pairs; Hamming distance filters them.
    for index, (parent, value) in enumerate(zip(parents, hashes)):
        for band in range(4):
            part = (value >> (band * 16)) & 0xFFFF
            bands[(parent.source_family, band, part)].add(index)
    pairs: set[tuple[int, int]] = set()
    for indices in bands.values():
        ordered = sorted(indices)
        for left, right in itertools.combinations(ordered, 2):
            pairs.add((left, right))
    for left_index, right_index in sorted(pairs):
        left, right = parents[left_index], parents[right_index]
        pair_ids = tuple(sorted((left.record_id, right.record_id)))
        if pair_ids in seen_pairs:
            continue
        distance = (hashes[left_index] ^ hashes[right_index]).bit_count()
        if distance <= 3:
            candidates.append({
                "record_id_a": pair_ids[0], "record_id_b": pair_ids[1],
                "kind": "near_duplicate_simhash_candidate", "simhash_hamming_distance": distance,
                "review_required": True,
            })
    return sorted(candidates, key=lambda row: (row["kind"], row["record_id_a"], row["record_id_b"]))


def _build_chunks_for_parent(
    parent: V3ParentRecord, tokenizer, max_tokens: int,
) -> tuple[list[V3EmbeddingChunk], int, int, int]:
    fields = parent.fields
    temporary: list[dict[str, Any]] = []
    if parent.source_family == "microsoft_historical_reviews":
        comment = str(fields.get("reviewer_comment", ""))
        patch = str(fields.get("patch", ""))
        comment_prefix = "Reviewer comment:\n" + comment + "\n\nPatch:\n"
        if _token_count(tokenizer, comment_prefix + patch) <= max_tokens:
            span = SourceSpan(0, 0, len(patch), patch, _token_count(tokenizer, comment_prefix + patch))
            temporary.append({"role": "review_and_patch", "source_field": "patch", "span": span,
                              "embedding_text": comment_prefix + patch})
        else:
            repeat_comment = _token_count(tokenizer, comment_prefix) <= max(1, max_tokens // 3)
            if repeat_comment:
                patch_prefix = lambda _index: comment_prefix
            else:
                patch_prefix = lambda _index: (
                    f"Review parent {parent.record_id}; see linked reviewer comment.\nPatch:\n"
                )
            patch_spans = chunk_source_text(
                patch, tokenizer, max_tokens=max_tokens, kind="diff", prefix_for_chunk=patch_prefix,
            )
            comment_spans = []
            if not repeat_comment:
                comment_spans = chunk_source_text(
                    comment, tokenizer, max_tokens=max_tokens,
                    kind="paragraph", prefix_for_chunk=lambda _index: f"Reviewer comment for {parent.record_id}:\n",
                )
                for span in comment_spans:
                    prefix = f"Reviewer comment for {parent.record_id}:\n"
                    temporary.append({"role": "review_comment", "source_field": "reviewer_comment",
                                      "span": span, "embedding_text": prefix + span.text})
            for span in patch_spans:
                prefix = patch_prefix(span.index)
                temporary.append({"role": "patch_hunk", "source_field": "patch", "span": span,
                                  "embedding_text": prefix + span.text})
    else:
        source_field, body = _body_field(parent)
        prefix = _parent_prefix(parent)
        kind = "documentation" if parent.source_family in {
            "mitre_cwe", "owasp_cheatsheets", "pep8", "google_style", "bandit"
        } else "code"
        spans = chunk_source_text(
            body, tokenizer, max_tokens=max_tokens, kind=kind,
            prefix_for_chunk=lambda _index: prefix,
        )
        for span in spans:
            temporary.append({"role": "record_content", "source_field": source_field,
                              "span": span, "embedding_text": prefix + span.text})

    ids = []
    for index, item in enumerate(temporary):
        span = item["span"]
        identity = f"{parent.record_id}:{item['role']}:{index}:{content_hash(item['embedding_text'])}"
        ids.append(stable_id("chunk", parent.source_family, parent.source_revision, identity))
    comment_ids = [ids[i] for i, item in enumerate(temporary) if item["role"] == "review_comment"]
    chunks = []
    for index, item in enumerate(temporary):
        span: SourceSpan = item["span"]
        related = tuple(comment_ids) if item["role"] == "patch_hunk" and comment_ids else ()
        chunk = V3EmbeddingChunk(
            chunk_id=ids[index], source_document_id=parent.source_document_id,
            record_id=parent.record_id, source_family=parent.source_family,
            source_category=parent.source_category, record_type=parent.record_type,
            source_revision=parent.source_revision, chunk_index=index,
            chunk_count=len(temporary), content_hash=content_hash(item["embedding_text"]),
            language=parent.language, language_origin=parent.language_origin,
            language_confidence=parent.language_confidence,
            embedding_text=item["embedding_text"], token_count=_token_count(tokenizer, item["embedding_text"]),
            source_span=span.text, source_field=item["source_field"],
            source_span_start=span.start, source_span_end=span.end,
            role=item["role"], related_chunk_ids=related, source_url=parent.source_url,
        )
        chunk.validate()
        chunks.append(chunk)
    oversized_lines = sum(1 for item in temporary if item["span"].oversized_indivisible_line)
    semantic_fallbacks = sum(1 for item in temporary if item["span"].semantic_unit_split)
    split_line_fallbacks = sum(1 for item in temporary if item["span"].split_line_fallback)
    return chunks, oversized_lines, semantic_fallbacks, split_line_fallbacks


def _quantiles(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"min": None, "p50": None, "p90": None, "p95": None, "max": None}
    ordered = sorted(values)
    def at(percent: float) -> int:
        return ordered[round((len(ordered) - 1) * percent)]
    return {"min": ordered[0], "p50": at(.50), "p90": at(.90), "p95": at(.95), "max": ordered[-1]}


def prepare_local_sources(
    *, input_root, tokenizer, config: PreparationConfig | None = None,
) -> PreparedCorpus:
    """Prepare in-memory canonical parents and chunks; performs no writes or DB calls."""
    from pathlib import Path
    from rag.v3.sources import load_all_local_sources

    config = config or PreparationConfig()
    config.validate()
    root = Path(input_root).resolve()
    parents, inputs, warnings, source_rejections = load_all_local_sources(
        root, max_microsoft_records=config.max_microsoft_source_records,
    )
    unallocated_counts = Counter(parent.source_family for parent in parents)
    selected = _select_parents(parents, config.parent_limits)
    rejected: list[dict[str, Any]] = list(source_rejections)
    accepted_parents: list[V3ParentRecord] = []
    chunks: list[V3EmbeddingChunk] = []
    oversized_lines = 0
    semantic_fallbacks = 0
    split_line_fallbacks = 0
    for parent in selected:
        try:
            parent.validate()
            built, count, fallback_count, split_line_count = _build_chunks_for_parent(
                parent, tokenizer, config.max_chunk_tokens,
            )
            if not built:
                rejected.append({"record_id": parent.record_id, "reason": "no_embedding_content"})
                continue
            accepted_parents.append(parent)
            chunks.extend(built)
            oversized_lines += count
            semantic_fallbacks += fallback_count
            split_line_fallbacks += split_line_count
        except Exception as exc:
            rejected.append({"record_id": parent.record_id, "reason": type(exc).__name__})

    chunk_counts = Counter(chunk.source_family for chunk in chunks)
    parent_counts = Counter(parent.source_family for parent in accepted_parents)
    language_counts = Counter(parent.language for parent in accepted_parents)
    token_counts = [chunk.token_count for chunk in chunks]
    duplicates = find_duplicate_candidates(accepted_parents)
    hash_rollup = hashlib.sha256("".join(parent.content_hash for parent in accepted_parents).encode("ascii")).hexdigest()
    manifest = {
        "corpus_version": "3",
        "ingestion_version": INGESTION_VERSION,
        "preprocessing_version": "rag-v3-prep-1",
        "tokenizer_name": config.tokenizer_name,
        "tokenizer_count_method": "tokenizer.encode(add_special_tokens=False) on final embedding_text",
        "allocation_unit": config.allocation_unit,
        "parent_limits": dict(sorted(config.parent_limits.items())),
        "max_chunk_tokens": config.max_chunk_tokens,
        "max_microsoft_source_records": config.max_microsoft_source_records,
        "inputs": inputs,
        "source_revisions": dict(sorted({p.source_family: p.source_revision for p in selected}.items())),
        "warnings": warnings,
        "parent_counts_before_allocation": dict(sorted(unallocated_counts.items())),
        "parent_counts_by_source": dict(sorted(parent_counts.items())),
        "chunk_counts_by_source": dict(sorted(chunk_counts.items())),
        "language_distribution": dict(sorted(language_counts.items())),
        "unknown_language_parent_count": language_counts.get("unknown", 0),
        "rejected_record_count": len(rejected),
        "rejections_by_reason": dict(sorted(Counter(row["reason"] for row in rejected).items())),
        "exact_or_near_duplicate_candidate_count": len(duplicates),
        "duplicate_candidates_by_kind": dict(sorted(Counter(row["kind"] for row in duplicates).items())),
        "embedding_token_length_statistics": _quantiles(token_counts),
        "oversized_indivisible_line_chunk_count": oversized_lines,
        "semantic_unit_line_fallback_chunk_count": semantic_fallbacks,
        "line_character_fallback_chunk_count": split_line_fallbacks,
        "parent_content_hashes_rollup_sha256": hash_rollup,
        "parent_content_hash_count": len(accepted_parents),
        "selected_parent_count_before_chunk_validation": len(selected),
        "embedding_generation_performed": False,
        "chromadb_touched": False,
    }
    return PreparedCorpus(accepted_parents, chunks, rejected, duplicates, manifest)

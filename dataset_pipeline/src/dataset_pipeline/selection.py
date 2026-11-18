"""Deterministic coverage-aware compact-corpus selection."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import math
import statistics
from typing import Any, Callable, Iterable, Mapping

from .models import stable_hash


class SelectionError(ValueError):
    pass


@dataclass(frozen=True)
class SelectionConfig:
    objective: str
    max_examples: int | None = None
    max_tokens: int | None = None
    max_input_tokens_per_record: int | None = None
    max_target_tokens_per_record: int | None = None
    max_record_tokens: int | None = None
    max_input_characters_per_record: int | None = 250_000
    max_target_characters_per_record: int | None = 250_000
    seed: str = "dataset-pipeline-v1"
    min_quotas: Mapping[str, Mapping[str, int]] | None = None
    max_quotas: Mapping[str, Mapping[str, int]] | None = None
    allowed_splits: tuple[str, ...] = ("train",)

    def __post_init__(self) -> None:
        if self.max_examples is None and self.max_tokens is None:
            raise SelectionError("At least one of max_examples or max_tokens is required")
        if self.max_examples is not None and self.max_examples < 1:
            raise SelectionError("max_examples must be positive")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise SelectionError("max_tokens must be positive")
        for name in ("max_input_tokens_per_record", "max_target_tokens_per_record", "max_record_tokens",
                     "max_input_characters_per_record", "max_target_characters_per_record"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise SelectionError(f"{name} must be positive")
        for limits in (self.min_quotas or {}, self.max_quotas or {}):
            for values in limits.values():
                if any(int(n) < 0 for n in values.values()):
                    raise SelectionError("quota values must be non-negative integers")


def tokenizer_json_counter(path: str) -> tuple[Callable[[str], int], str]:
    """Load an existing local tokenizer.json; never downloads a tokenizer/model."""
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise SelectionError("Install the optional 'tokenizer' extra to use --tokenizer-json") from exc
    tok = Tokenizer.from_file(path)
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return lambda text: len(tok.encode(text).ids), digest.hexdigest()


def _eligible(record: Mapping[str, Any], objective: str, allowed_splits: tuple[str, ...]) -> tuple[bool, str]:
    if not record.get("eligibility", {}).get(objective, False):
        return False, "no_source_supported_supervision_for_objective"
    if str(record.get("source_split")) not in allowed_splits:
        return False, "not_in_allowed_training_split"
    if record.get("quality_status") and "conflicting_label" in record["quality_status"]:
        return False, "conflicting_label"
    labels = record.get("source_labels") or {}
    if objective == "review_comment_generation" and not record.get("review_comment"):
        return False, "missing_source_comment_target"
    if objective == "fix_recommendation" and not (record.get("after_code") and record.get("review_comment")):
        return False, "missing_source_fix_target_or_instruction"
    if objective == "review_quality_classification" and labels.get("y") not in (0, 1):
        return False, "missing_source_quality_label"
    if objective == "defect_detection" and record.get("defect_status") not in ("defective", "non_defective"):
        return False, "missing_verified_defect_status"
    if objective == "severity_classification" and record.get("severity") is None:
        return False, "missing_source_severity_label"
    if objective == "explanation_generation" and not record.get("explanation"):
        return False, "missing_source_explanation_target"
    return True, ""


def _label_coverage(records: list[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    fields: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        for name, value in (record.get("source_labels") or {}).items():
            if value is not None:
                fields[str(name)][repr(value)] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(fields.items())}


def _atomic_group(record: Mapping[str, Any]) -> str:
    pair = record.get("pair_id")
    if pair:
        return f"pair:{pair}"
    repo, pr = record.get("repository"), record.get("pull_request_id")
    if repo and pr:
        return f"repo-pr:{repo}:{pr}"
    # A known commit can join related buggy/fixed records while not grouping an
    # entire repository into one huge unit.
    commit = record.get("commit_id")
    if repo and commit:
        return f"repo-commit:{repo}:{commit}"
    return f"record:{record.get('record_id')}"


def _leakage_group(record: Mapping[str, Any]) -> str:
    if record.get("leakage_group_id"):
        return str(record["leakage_group_id"])
    return _atomic_group(record)


def _text_for_tokens(record: Mapping[str, Any]) -> str:
    return "\n".join(str(record.get(k) or "") for k in
                     ("before_code", "diff", "review_comment", "after_code"))


def objective_text_parts(record: Mapping[str, Any], objective: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return objective fields without joining, for bounded pre-tokenization checks."""
    before = str(record.get("before_code") or "")
    diff = str(record.get("diff") or "")
    comment = str(record.get("review_comment") or "")
    if objective == "review_comment_generation":
        return tuple(x for x in (before, diff) if x), (comment,)
    if objective == "fix_recommendation":
        return tuple(x for x in (before, diff, comment) if x), (str(record.get("after_code") or ""),)
    if objective == "review_quality_classification":
        label = (record.get("source_labels") or {}).get("y")
        return tuple(x for x in (before, diff) if x), ("" if label is None else str(label),)
    if objective == "defect_detection":
        label = record.get("defect_status")
        return (before,), ("" if label is None else str(label),)
    if objective == "severity_classification":
        return tuple(x for x in (before, diff, comment) if x), ("" if record.get("severity") is None else str(record["severity"]),)
    if objective == "explanation_generation":
        return tuple(x for x in (before, diff) if x), (str(record.get("explanation") or ""),)
    return (_text_for_tokens(record),), ("")


def objective_texts(record: Mapping[str, Any], objective: str) -> tuple[str, str]:
    """Return source-grounded input and target text for token accounting."""
    inputs, targets = objective_text_parts(record, objective)
    return "\n".join(inputs), "\n".join(targets)


def _distribution(records: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    vals = sorted(int(r["token_count"]) for r in records if r.get("token_count") is not None)
    if not vals:
        return None
    def pct(p: float) -> int:
        return vals[min(len(vals) - 1, math.ceil(p * len(vals)) - 1)]
    return {"count": len(vals), "min": vals[0], "median": statistics.median(vals),
            "p90": pct(.90), "p95": pct(.95), "max": vals[-1], "sum": sum(vals)}


def select_records(
    records: Iterable[Mapping[str, Any]], config: SelectionConfig,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select whole related groups under budgets and minimum coverage quotas.

    Token budgets require an exact caller-supplied tokenizer. No heuristic token
    estimate is silently substituted for actual tokens.
    """
    if config.max_tokens is not None and token_counter is None:
        raise SelectionError("max_tokens requires a tokenizer counter; supply a pinned local tokenizer.json")
    materialized: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    all_records = [dict(source_record) for source_record in records]
    group_splits: dict[str, set[str]] = defaultdict(set)
    for rec in all_records:
        split = rec.get("source_split")
        if split is not None:
            group_splits[_leakage_group(rec)].add(str(split))
    cross_split_groups = {group for group, splits in group_splits.items() if len(splits) > 1}
    for rec in all_records:
        rid = str(rec.get("record_id") or "")
        if not rid:
            exclusions.append({"record_id": "", "reason": "missing_record_id"})
            continue
        if rid in seen_ids:
            exclusions.append({"record_id": rid, "reason": "duplicate_record_id"})
            continue
        seen_ids.add(rid)
        if _leakage_group(rec) in cross_split_groups:
            exclusions.append({"record_id": rid, "reason": "related_group_crosses_source_splits"})
            continue
        ok, reason = _eligible(rec, config.objective, config.allowed_splits)
        if not ok:
            exclusions.append({"record_id": rid, "reason": reason})
            continue
        if token_counter:
            rec["token_count"] = int(token_counter(_text_for_tokens(rec)))
            if rec["token_count"] < 0:
                raise SelectionError(f"negative token count for {rid}")
            rec["token_count_method"] = "caller_supplied_exact_tokenizer"
        else:
            rec["token_count"] = None
            rec["token_count_method"] = "not_measured"
        materialized.append(rec)

    candidate_distribution = _distribution(materialized)

    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in materialized:
        by_group[_atomic_group(rec)].append(rec)
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for group, rows in by_group.items():
        splits = {str(r.get("source_split")) for r in rows}
        if len(splits) > 1:
            exclusions.extend({"record_id": str(r["record_id"]), "reason": "related_group_crosses_source_splits"} for r in rows)
            continue
        groups.append((group, rows))

    mins = config.min_quotas or {}
    maxs = config.max_quotas or {}
    chosen: list[dict[str, Any]] = []
    chosen_groups: set[str] = set()
    counts: dict[str, Counter[str]] = {
        dim: Counter() for dim in set(mins) | set(maxs) | {"source", "source_task", "language"}
    }
    used_tokens = 0

    def fits(rows: list[dict[str, Any]]) -> bool:
        if config.max_examples is not None and len(chosen) + len(rows) > config.max_examples:
            return False
        total = sum(int(r.get("token_count") or 0) for r in rows)
        return config.max_tokens is None or used_tokens + total <= config.max_tokens

    def quota_gain(rows: list[dict[str, Any]]) -> tuple[int, int]:
        gain, pressure = 0, 0
        for dim, requirements in mins.items():
            for r in rows:
                value = str(r.get(dim) or "<missing>")
                target = int(requirements.get(value, 0))
                if counts.setdefault(dim, Counter())[value] < target:
                    gain += 1
                    pressure += max(1, target - counts[dim][value])
        return gain, pressure

    def within_max(rows: list[dict[str, Any]]) -> bool:
        for dim, limits in maxs.items():
            add = Counter(str(r.get(dim) or "<missing>") for r in rows)
            for value, amount in add.items():
                if counts.setdefault(dim, Counter())[value] + amount > int(limits.get(value, 10**18)):
                    return False
        return True

    # Quota pass: repeatedly choose the group covering the most unmet quota
    # slots, with deterministic hash tie-breaks. Whole groups are indivisible.
    while mins:
        candidates = []
        for group, rows in groups:
            if group in chosen_groups or not fits(rows) or not within_max(rows):
                continue
            gain, pressure = quota_gain(rows)
            if gain:
                tie = stable_hash([config.seed, group])
                candidates.append((gain, pressure, tie, group, rows))
        if not candidates:
            break
        _, _, _, group, rows = max(candidates, key=lambda x: (x[0], x[1], x[2]))
        chosen_groups.add(group)
        chosen.extend(rows)
        used_tokens += sum(int(r.get("token_count") or 0) for r in rows)
        for dim in counts:
            counts[dim].update(str(r.get(dim) or "<missing>") for r in rows)

    # Coverage fill: favor values with low selected coverage, then deterministic
    # source-group hash. Never exceed budgets or max quotas.
    while True:
        candidates = []
        for group, rows in groups:
            if group in chosen_groups or not fits(rows) or not within_max(rows):
                continue
            dimensions = set(mins) | {"source", "source_task", "language"}
            rarity = 0.0
            for dim in dimensions:
                vals = {str(r.get(dim) or "<missing>") for r in rows}
                rarity += sum(1.0 / (1 + counts.setdefault(dim, Counter())[v]) for v in vals)
            tie = stable_hash([config.seed, group])
            candidates.append((rarity, tie, group, rows))
        if not candidates:
            break
        _, _, group, rows = max(candidates, key=lambda x: (x[0], x[1]))
        chosen_groups.add(group)
        chosen.extend(rows)
        used_tokens += sum(int(r.get("token_count") or 0) for r in rows)
        for dim in counts:
            counts[dim].update(str(r.get(dim) or "<missing>") for r in rows)

    selected_ids = {str(r["record_id"]) for r in chosen}
    for r in materialized:
        if str(r["record_id"]) not in selected_ids:
            exclusions.append({"record_id": str(r["record_id"]), "reason": "budget_or_quota"})
    chosen.sort(key=lambda r: (str(r.get("source")), str(r.get("source_file")), str(r.get("record_id"))))
    unmet = {}
    for dim, requirements in mins.items():
        for value, target in requirements.items():
            actual = counts.setdefault(dim, Counter())[str(value)]
            if actual < int(target):
                unmet[f"{dim}:{value}"] = {"requested": int(target), "selected": actual}
    by_source = Counter(str(r.get("source") or "<missing>") for r in chosen)
    by_task = Counter(str(r.get("source_task") or "<missing>") for r in chosen)
    by_language = Counter(str(r.get("language") or "<missing>") for r in chosen)
    report = {
        "selection_version": "1.0",
        "objective": config.objective,
        "seed": config.seed,
        "budget": {"max_examples": config.max_examples, "max_tokens": config.max_tokens},
        "coverage_quotas": {"minimum": mins, "maximum": maxs},
        "allowed_splits": list(config.allowed_splits),
        "selected_records": len(chosen),
        "selected_tokens": used_tokens if token_counter else None,
        "token_count_status": "exact tokenizer counts" if token_counter else "not measured",
        "eligible_pool_token_distribution": candidate_distribution,
        "selected_token_distribution": _distribution(chosen),
        "reconstruction_ready": bool(chosen) and all(
            r.get("source_revision") and r.get("source_checksum") and
            (r.get("source_record_id") is not None or r.get("source_ordinal") is not None)
            for r in chosen
        ),
        "selected_by_source": dict(sorted(by_source.items())),
        "selected_by_task": dict(sorted(by_task.items())),
        "selected_by_language": dict(sorted(by_language.items())),
        "selected_source_label_coverage": _label_coverage(chosen),
        "unmet_min_quotas": unmet,
        "excluded_records": len(exclusions),
        "exclusions_by_reason": dict(sorted(Counter(x["reason"] for x in exclusions).items())),
        "exclusions": sorted(exclusions, key=lambda x: (x["record_id"], x["reason"])),
        "selected_manifest": [
            {"record_id": r["record_id"], "content_fingerprint": r.get("content_fingerprint"),
             "source": r.get("source"), "source_revision": r.get("source_revision"),
             "source_checksum": r.get("source_checksum"),
             "source_file": r.get("source_file"), "source_record_id": r.get("source_record_id"),
             "source_ordinal": r.get("source_ordinal"), "source_locator": r.get("source_locator"),
             "source_split": r.get("source_split"), "group_id": _atomic_group(r),
             "token_count": r.get("token_count")}
            for r in chosen
        ],
    }
    report["manifest_fingerprint"] = stable_hash(report)
    return chosen, report

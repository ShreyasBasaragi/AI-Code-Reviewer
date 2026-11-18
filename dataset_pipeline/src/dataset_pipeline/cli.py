"""Command-line interface for bounded sample builds and compact selection."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import time
from typing import Any

import yaml
try:
    import resource
except ImportError:  # Windows
    resource = None

from . import __version__
from .adapters.devign import iter_json_array_windows, map_devign
from .adapters.microsoft import TASKS, iter_jsonl_windows, map_microsoft
from .deduplication import audit_duplicates
from .exporting import write_json, write_jsonl, write_parquet
from .manifest import sha256_file, source_manifest
from .models import stable_hash
from .selection import SelectionConfig, SelectionError, tokenizer_json_counter
from .disk_selection import select_jsonl_disk_backed
from .scale_validation import build_prefix_workloads
from .splitting import validate_splits
from .validation import validate_record


def _load_config(path: Path) -> tuple[dict[str, Any], Path]:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("configuration root must be a mapping")
    root = Path(cfg.get("project_root", "../.."))
    if not root.is_absolute():
        root = (path.parent / root).resolve()
    return cfg, root


def _source_path(root: Path, raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else root / p


def _stable_sample(rows: list[dict[str, Any]], limit: int, seed: str) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda r: stable_hash([seed, r["record_id"]]))[:limit]
    return sorted(ranked, key=lambda r: (r["source"], r["source_file"], r["record_id"]))


def _iter_sample_inputs(cfg: dict[str, Any], root: Path, limit: int, seed: str):
    outputs: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    scans: list[dict[str, Any]] = []
    bounded_bytes = int(cfg.get("sample_max_bytes_per_file", 4 * 1024 * 1024))
    revision = str(cfg.get("source_revision", "unversioned-local"))
    for item in cfg.get("microsoft", []):
        p = _source_path(root, item["path"])
        if not p.is_file():
            quarantine.append({"source_file": str(item["path"]), "reason": "source_file_missing"})
            continue
        sample_pool = []
        scanned, bad = 0, 0
        seen_offsets: set[int] = set()
        for offset, row, err in iter_jsonl_windows(p, byte_budget=bounded_bytes,
                                                    seed=f"{seed}:{item['task']}:{item['split']}"):
            if offset in seen_offsets:
                continue
            seen_offsets.add(offset)
            scanned += 1
            if err:
                bad += 1
                quarantine.append({"source_file": str(item["path"]),
                                   "source_locator": {"byte_offset": offset}, "reason": err})
                continue
            sample_pool.append(map_microsoft(row or {}, task=item["task"], split=item["split"],
                                             source_file=str(item["path"]), revision=revision,
                                             ordinal=offset, source_locator={"byte_offset": offset}))
        chosen = _stable_sample(sample_pool, limit, seed)
        outputs.extend(chosen)
        scans.append({"source": "microsoft_codereviewer", "task": item["task"],
                      "source_file": str(item["path"]), "file_bytes": p.stat().st_size,
                      "bounded_read_bytes_max": bounded_bytes, "records_parsed_within_bound": scanned,
                      "malformed_records": bad,
                      "sampled_records": len(chosen),
                      "sampling_note": "deterministic hash-rank from byte windows spread over file; bounded sample, not full scan"})
    dev = cfg.get("devign_candidate")
    if dev:
        p = _source_path(root, dev["path"])
        if p.is_file():
            sample_pool = []
            scanned, bad = 0, 0
            seen_offsets: set[int] = set()
            for offset, row, err in iter_json_array_windows(p, byte_budget=bounded_bytes,
                                                             seed=f"{seed}:devign"):
                if offset in seen_offsets:
                    continue
                seen_offsets.add(offset)
                scanned += 1
                if err:
                    bad += 1
                    quarantine.append({"source_file": str(dev["path"]),
                                       "source_locator": {"byte_offset": offset}, "reason": err})
                    continue
                sample_pool.append(map_devign(row or {}, source_file=str(dev["path"]), revision=revision,
                                              ordinal=offset, source_locator={"byte_offset": offset}))
            chosen = _stable_sample(sample_pool, limit, seed)
            outputs.extend(chosen)
            scans.append({"source": "devign_candidate", "source_file": str(dev["path"]),
                          "file_bytes": p.stat().st_size, "bounded_read_bytes_max": bounded_bytes,
                          "records_parsed_within_bound": scanned, "malformed_or_partial_records": bad,
                          "sampled_records": len(chosen),
                          "sampling_note": "deterministic hash-rank from byte windows spread over array; source split remains unknown"})
        else:
            quarantine.append({"source_file": str(dev["path"]), "reason": "source_file_missing"})
    return outputs, quarantine, scans


def _quality(rows: list[dict[str, Any]], quarantine: list[dict[str, Any]]) -> dict[str, Any]:
    issues = []
    for row in rows:
        found = validate_record(row)
        if found:
            issues.extend({"record_id": row.get("record_id"), "reason": x} for x in found)
    duplicates = audit_duplicates(rows)
    return {"records_seen": len(rows), "valid_records": len(rows) - len(issues),
            "validation_issue_count": len(issues), "validation_issues": issues,
            "preexisting_quarantine_count": len(quarantine), "duplicate_audit": duplicates,
            "split_audit": validate_splits(rows)}


def _disk_preflight(output: Path, estimated_bytes: int) -> dict[str, int]:
    output.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output).free
    if free < estimated_bytes * 2 + 1024 * 1024:
        raise OSError(f"disk preflight failed: need about {estimated_bytes * 2 + 1048576} free bytes, have {free}")
    return {"free_bytes": free, "estimated_output_bytes": estimated_bytes,
            "required_with_safety_factor_bytes": estimated_bytes * 2 + 1048576}


def _build(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).resolve()
    cfg, root = _load_config(cfg_path)
    started = time.perf_counter()
    rows, quarantine, scans = _iter_sample_inputs(cfg, root, args.sample_per_source, args.seed)
    if not rows and not quarantine:
        raise ValueError("no configured source records found")
    estimated = sum(len(json.dumps(r, ensure_ascii=False).encode("utf-8")) + 1 for r in rows)
    output = Path(args.output_dir).resolve()
    preflight = _disk_preflight(output, estimated)
    validation_quarantine = []
    valid = []
    duplicate_audit = audit_duplicates(rows)
    conflict_ids = {rid for group in duplicate_audit["conflicting_label_groups"] for rid in group["record_ids"]}
    for row in rows:
        if row["record_id"] in conflict_ids:
            row["quality_status"].append("conflicting_label")
            validation_quarantine.append({"record": row, "reasons": ["conflicting_label"]})
            continue
        issues = validate_record(row)
        if issues:
            validation_quarantine.append({"record": row, "reasons": issues})
        else:
            valid.append(row)
    all_quarantine = quarantine + validation_quarantine
    out_canonical = output / "canonical.jsonl"
    out_quarantine = output / "quarantine.jsonl"
    write_jsonl(out_canonical, valid)
    write_jsonl(out_quarantine, all_quarantine)
    if args.parquet:
        write_parquet(output / "canonical.parquet", valid)
    quality = _quality(valid, all_quarantine)
    quality["duplicate_audit"] = duplicate_audit
    quality["valid_records"] = len(valid)
    quality["excluded_records"] = len(all_quarantine)
    quality["valid_counts_by_source_task"] = dict(sorted(Counter(f"{r['source']}:{r['source_task']}" for r in valid).items()))
    write_json(output / "quality_report.json", quality)
    manifest = {
        "pipeline_version": __version__, "schema_version": "1.0", "mode": "bounded_sample",
        "seed": args.seed, "sample_per_source_file": args.sample_per_source,
        "project_root_config_relative": cfg.get("project_root", "../.."),
        "config_sha256": sha256_file(cfg_path),
        "source_scans": scans, "source_checksums": "not computed (would require full-source reads)",
        "valid_records": len(valid), "quarantined_or_excluded_records": len(all_quarantine),
        "output_artifacts": {"canonical.jsonl": sha256_file(out_canonical),
                              "quarantine.jsonl": sha256_file(out_quarantine),
                              "quality_report.json": sha256_file(output / "quality_report.json")},
        "sample_limit_bytes_per_file": int(cfg.get("sample_max_bytes_per_file", 4 * 1024 * 1024)),
        "disk_preflight_passed": True,
        "full_corpus_ready": False,
    }
    if args.parquet:
        manifest["output_artifacts"]["canonical.parquet"] = sha256_file(output / "canonical.parquet")
    write_json(output / "manifest.json", manifest)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None
    peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024) if peak is not None else None
    write_json(output / "run_report.json", {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "process_peak_rss_bytes": peak_bytes,
        "disk_preflight": preflight,
        "memory_bound_note": "bounded by configured per-file read window plus sample pool; OS peak RSS includes Python runtime",
    })
    print(json.dumps(manifest, indent=2))
    return 0


def _inspect(args: argparse.Namespace) -> int:
    cfg, root = _load_config(Path(args.config).resolve())
    items = list(cfg.get("microsoft", [])) + ([cfg["devign_candidate"]] if cfg.get("devign_candidate") else [])
    report = []
    for item in items:
        p = _source_path(root, item["path"])
        report.append({"configured_path": item["path"], "exists": p.is_file(),
                       "size_bytes": p.stat().st_size if p.is_file() else None,
                       "task": item.get("task"), "split": item.get("split")})
    print(json.dumps({"sources": report, "note": "inspect does not hash or read full source files"}, indent=2))
    return 0 if all(x["exists"] for x in report) else 2


def _source_manifest(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).resolve()
    cfg, root = _load_config(cfg_path)
    report = source_manifest(cfg_path, cfg, root, checksum=args.hash)
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, report)
    print(json.dumps(report, indent=2))
    return 0 if all(item["exists"] for item in report["sources"]) else 2


def _verify_source_manifest(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    cfg, root = _load_config(Path(args.config).resolve())
    checks = []
    for entry in manifest.get("sources", []):
        raw = entry.get("configured_path")
        path = _source_path(root, raw) if raw else None
        status = {"configured_path": raw, "exists": bool(path and path.is_file()),
                  "verified": False, "reason": None}
        if not status["exists"]:
            status["reason"] = "source_missing"
        elif not entry.get("sha256"):
            status["reason"] = "manifest_has_no_source_checksum"
        else:
            actual = sha256_file(path)
            status["verified"] = actual == entry["sha256"]
            status["reason"] = None if status["verified"] else "sha256_mismatch"
        checks.append(status)
    verified = bool(checks) and all(x["verified"] for x in checks)
    config_hash_matches = manifest.get("configuration_sha256") == sha256_file(args.config)
    revision_matches = manifest.get("source_revision") == cfg.get("source_revision")
    verified = verified and config_hash_matches and revision_matches
    print(json.dumps({"verified": verified, "checks": checks,
                      "configuration_sha256_matches": config_hash_matches,
                      "source_revision_matches_config": revision_matches}, indent=2))
    return 0 if verified else 1


def _scale_workloads(args: argparse.Namespace) -> int:
    report = build_prefix_workloads(args.source, args.output_dir, task=args.task, split=args.split,
                                    revision=args.revision, stages=tuple(args.stages),
                                    max_source_bytes=args.max_source_bytes,
                                    max_record_bytes=args.max_record_bytes)
    print(json.dumps(report, indent=2))
    return 0


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"malformed JSONL at {path}:{n}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected object at {path}:{n}")
            rows.append(value)
    return rows


def _validate(args: argparse.Namespace) -> int:
    root = Path(args.input_dir)
    rows = _read_jsonl(root / "canonical.jsonl")
    errors = [{"record_id": r.get("record_id"), "reasons": validate_record(r)} for r in rows]
    errors = [x for x in errors if x["reasons"]]
    print(json.dumps({"records": len(rows), "valid": len(rows)-len(errors), "invalid": len(errors), "issues": errors}, indent=2))
    return 1 if errors else 0


def _verify(args: argparse.Namespace) -> int:
    root = Path(args.input_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    actual = {name: sha256_file(root / name) for name in manifest["output_artifacts"]}
    ok = actual == manifest["output_artifacts"]
    rows = _read_jsonl(root / "canonical.jsonl")
    ids = [r.get("record_id") for r in rows]
    unique_ids = len(ids) == len(set(ids))
    print(json.dumps({"artifact_hashes_match": ok, "unique_record_ids": unique_ids,
                      "logical_record_count": len(rows), "verified": ok and unique_ids}, indent=2))
    return 0 if ok and unique_ids else 1


def _quota_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _select(args: argparse.Namespace) -> int:
    if args.max_examples is None and args.max_tokens is None:
        raise ValueError("select requires --max-examples and/or --max-tokens")
    if not args.tokenizer_json:
        raise ValueError("disk-backed selection requires --tokenizer-json for input/target token measurement")
    quotas = _quota_config(args.selection_config)
    token_counter, tokenizer_digest = tokenizer_json_counter(args.tokenizer_json)
    config = SelectionConfig(
        objective=args.objective, max_examples=args.max_examples, max_tokens=args.max_tokens,
        max_input_tokens_per_record=args.max_input_tokens,
        max_target_tokens_per_record=args.max_target_tokens,
        max_record_tokens=args.max_record_tokens,
        max_input_characters_per_record=args.max_input_chars,
        max_target_characters_per_record=args.max_target_chars,
        seed=args.seed, min_quotas=quotas.get("min_quotas", {}),
        max_quotas=quotas.get("max_quotas", {}),
        allowed_splits=tuple(quotas.get("allowed_splits", ["train"])),
    )
    outdir = Path(args.output_dir).resolve() / "compact_training_corpus"
    checkpoint = Path(args.checkpoint_path) if args.checkpoint_path else Path(args.output_dir).resolve() / ".dataset_pipeline_work" / "selection.sqlite3"
    started = time.perf_counter()
    report = select_jsonl_disk_backed(
        args.input, outdir, checkpoint, config, token_counter, tokenizer_digest,
        resume=args.resume, checkpoint_every_records=args.checkpoint_every_records,
        max_temp_bytes=args.max_temp_bytes,
    )
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None
    peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024) if peak is not None else None
    write_json(outdir / "run_report.json", {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "process_peak_rss_bytes": peak_bytes,
        "checkpoint_sqlite_bytes": report["checkpoint"]["sqlite_bytes"],
        "memory_note": "canonical rows streamed; metadata indexed in SQLite; code text not retained in selection tables",
    })
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dataset-pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="list configured source existence and sizes")
    inspect.add_argument("--config", required=True); inspect.set_defaults(func=_inspect)
    source_meta = sub.add_parser("source-manifest", help="write source metadata and optional full-file SHA-256 values")
    source_meta.add_argument("--config", required=True); source_meta.add_argument("--output", required=True)
    source_meta.add_argument("--hash", action="store_true", help="read each complete source file to compute SHA-256")
    source_meta.set_defaults(func=_source_manifest)
    source_verify = sub.add_parser("verify-source-manifest", help="verify pinned local source checksums before reconstruction")
    source_verify.add_argument("--config", required=True); source_verify.add_argument("--manifest", required=True)
    source_verify.set_defaults(func=_verify_source_manifest)
    scale = sub.add_parser("scale-workloads", help="stream real source rows into bounded selector scale fixtures")
    scale.add_argument("--source", required=True); scale.add_argument("--output-dir", required=True)
    scale.add_argument("--task", default="Comment_Generation"); scale.add_argument("--split", default="train")
    scale.add_argument("--revision", default="local-unpinned")
    scale.add_argument("--stages", type=int, nargs="+", default=[1000, 10000, 50000])
    scale.add_argument("--max-source-bytes", type=int, default=1024 * 1024 * 1024)
    scale.add_argument("--max-record-bytes", type=int, default=16 * 1024 * 1024)
    scale.set_defaults(func=_scale_workloads)
    build = sub.add_parser("build", help="build only a bounded deterministic source sample")
    build.add_argument("--config", required=True); build.add_argument("--sample-per-source", type=int, default=100)
    build.add_argument("--seed", default="phase2a-sample-v1"); build.add_argument("--output-dir", required=True)
    build.add_argument("--parquet", action="store_true", help="also export canonical.parquet (requires pyarrow)")
    build.set_defaults(func=_build)
    validate = sub.add_parser("validate", help="validate a sample canonical export")
    validate.add_argument("--input-dir", required=True); validate.set_defaults(func=_validate)
    verify = sub.add_parser("verify", help="verify exported file hashes and stable IDs")
    verify.add_argument("--input-dir", required=True); verify.set_defaults(func=_verify)
    select = sub.add_parser("select", help="select a compact corpus from canonical JSONL")
    select.add_argument("--input", required=True); select.add_argument("--output-dir", required=True)
    select.add_argument("--objective", required=True); select.add_argument("--max-examples", type=int)
    select.add_argument("--max-tokens", type=int); select.add_argument("--tokenizer-json")
    select.add_argument("--max-input-tokens", type=int); select.add_argument("--max-target-tokens", type=int)
    select.add_argument("--max-record-tokens", type=int); select.add_argument("--max-temp-bytes", type=int)
    select.add_argument("--max-input-chars", type=int, default=250000)
    select.add_argument("--max-target-chars", type=int, default=250000)
    select.add_argument("--checkpoint-path"); select.add_argument("--checkpoint-every-records", type=int, default=1000)
    select.add_argument("--resume", action="store_true")
    select.add_argument("--selection-config"); select.add_argument("--seed", default="compact-corpus-v1")
    select.set_defaults(func=_select)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, SelectionError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

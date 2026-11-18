"""Create deterministic, real-record prefix workloads for selector scale checks."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from typing import Any

try:
    import resource
except ImportError:  # Windows
    resource = None

from .adapters.microsoft import iter_jsonl, map_microsoft
from .exporting import write_json
from .manifest import sha256_file
from .validation import validate_record


def build_prefix_workloads(source_path: str | Path, output_dir: str | Path, *,
                           task: str = "Comment_Generation", split: str = "train",
                           revision: str = "local-unpinned", stages: tuple[int, ...] = (1000, 10000, 50000),
                           max_source_bytes: int = 1024 * 1024 * 1024,
                           max_record_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    """Stream real source rows into nested scale fixtures, bounded by records and bytes.

    Prefix workloads are for throughput validation only; they are not claimed to
    be representative samples and are not training-corpus exports.
    """
    src, out = Path(source_path), Path(output_dir)
    if not src.is_file():
        raise FileNotFoundError(src)
    if not stages or any(x < 1 for x in stages) or tuple(sorted(set(stages))) != stages:
        raise ValueError("stages must be unique, positive, and ascending")
    if max_source_bytes < 1:
        raise ValueError("max_source_bytes must be positive")
    out.mkdir(parents=True, exist_ok=True)
    paths = {stage: out / str(stage) / "canonical.jsonl" for stage in stages}
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    handles = {stage: path.open("w", encoding="utf-8", newline="\n") for stage, path in paths.items()}
    started = time.perf_counter()
    scanned = parsed = valid = invalid = 0
    record_scan_cap = stages[-1] + max(100, stages[-1] // 20)
    source_name = str(src)
    try:
        for _, row, error in iter_jsonl(src, max_records=record_scan_cap, max_bytes=max_source_bytes,
                                        max_record_bytes=max_record_bytes):
            scanned += 1
            if error:
                invalid += 1
                continue
            parsed += 1
            record = map_microsoft(row or {}, task=task, split=split, source_file=source_name,
                                   revision=revision, ordinal=scanned)
            if validate_record(record):
                invalid += 1
                continue
            valid += 1
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for stage, handle in handles.items():
                if valid <= stage:
                    handle.write(line)
            if valid >= stages[-1]:
                break
    finally:
        for handle in handles.values():
            handle.close()
    bytes_read_upper_bound = min(src.stat().st_size, max_source_bytes)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None
    peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024) if peak is not None else None
    report = {
        "mode": "real_source_prefix_scale_fixture", "source_file_name": src.name,
        "source_revision": revision, "source_sha256": None,
        "source_file_bytes": src.stat().st_size, "source_read_byte_cap": max_source_bytes,
        "max_record_bytes": max_record_bytes,
        "source_bytes_read_upper_bound": bytes_read_upper_bound,
        "source_checksum_status": "not_computed_full_file_hash_not_requested",
        "task": task, "split": split, "requested_stages": list(stages),
        "record_scan_cap": record_scan_cap,
        "records_scanned": scanned, "records_parsed": parsed, "records_valid": valid,
        "records_invalid_or_malformed": invalid, "stage_records": {},
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "process_peak_rss_bytes": peak_bytes,
        "note": "The workload uses actual source rows at the start of one file; prefix selection is deterministic but not statistically representative.",
    }
    for stage, path in paths.items():
        count = min(stage, valid)
        report["stage_records"][str(stage)] = {
            "records": count, "canonical_path": str(path), "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    write_json(out / "workload_report.json", report)
    return report

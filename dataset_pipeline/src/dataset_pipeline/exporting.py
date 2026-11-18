"""Atomic JSONL/JSON exports, separate from source datasets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


def _atomic_text(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_jsonl(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> int:
    p = Path(path)
    count = 0
    def write(f):
        nonlocal count
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    _atomic_text(p, write)
    return count


def write_json(path: str | Path, value: Any) -> None:
    _atomic_text(Path(path), lambda f: json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2))


def write_parquet(path: str | Path, rows: list[Mapping[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Parquet export requires the optional 'parquet' extra") from exc
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    table = pa.Table.from_pylist(list(rows))
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()

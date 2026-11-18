"""Bounded-memory SQLite-backed compact-corpus selection and resume."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import sys
from typing import Any, Callable, Mapping

from . import __version__
from .exporting import write_json, write_jsonl
from .manifest import sha256_file
from .models import stable_hash
from .selection import (
    SelectionConfig, SelectionError, _atomic_group, _eligible, _leakage_group,
    objective_text_parts,
)
from .validation import validate_record


class SelectionInterrupted(RuntimeError):
    """Raised by the test hook after a committed checkpoint boundary."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _distribution(values: list[int]) -> dict[str, Any] | None:
    if not values:
        return None
    values.sort()
    n = len(values)
    def pct(p: float) -> int:
        return values[min(n - 1, max(0, int((n - 1) * p)))]
    return {"count": n, "min": values[0], "median": statistics.median(values),
            "p90": pct(.90), "p95": pct(.95), "max": values[-1], "sum": sum(values)}


def _set_state(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))


def _get_state(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _schema(con: sqlite3.Connection) -> None:
    con.executescript("""
      CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS seen_ids(record_id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS records(
        seq INTEGER PRIMARY KEY, byte_offset INTEGER NOT NULL, record_id TEXT,
        content_fingerprint TEXT, atomic_group TEXT NOT NULL, leakage_group TEXT NOT NULL,
        source_split TEXT, source TEXT, source_task TEXT, language TEXT,
        labels_json TEXT, input_tokens INTEGER, target_tokens INTEGER, total_tokens INTEGER,
        source_revision TEXT, source_checksum TEXT, source_file TEXT, source_record_id_json TEXT,
        source_ordinal INTEGER, source_locator_json TEXT,
        candidate INTEGER NOT NULL DEFAULT 0, reason TEXT, selected INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS records_offset_idx ON records(byte_offset);
      CREATE INDEX IF NOT EXISTS records_group_idx ON records(atomic_group);
      CREATE INDEX IF NOT EXISTS records_leak_idx ON records(leakage_group,source_split);
      CREATE INDEX IF NOT EXISTS records_fp_idx ON records(content_fingerprint);
      CREATE INDEX IF NOT EXISTS records_input_tokens_idx ON records(input_tokens) WHERE input_tokens IS NOT NULL;
      CREATE INDEX IF NOT EXISTS records_target_tokens_idx ON records(target_tokens) WHERE target_tokens IS NOT NULL;
      CREATE INDEX IF NOT EXISTS records_total_tokens_idx ON records(total_tokens) WHERE total_tokens IS NOT NULL;
      CREATE TABLE IF NOT EXISTS group_splits(leakage_group TEXT NOT NULL, source_split TEXT NOT NULL,
        PRIMARY KEY(leakage_group,source_split));
      CREATE TABLE IF NOT EXISTS groups(
        group_key TEXT PRIMARY KEY, leakage_group TEXT NOT NULL, row_count INTEGER NOT NULL,
        input_tokens INTEGER NOT NULL, target_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
        rank_key TEXT NOT NULL, rarity REAL NOT NULL DEFAULT 0, usable INTEGER NOT NULL DEFAULT 1,
        selected INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS group_strata(
        group_key TEXT NOT NULL, dimension TEXT NOT NULL, value TEXT NOT NULL, count INTEGER NOT NULL,
        PRIMARY KEY(group_key,dimension,value));
      CREATE INDEX IF NOT EXISTS group_strata_value_idx ON group_strata(dimension,value,group_key);
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(records)")}
    if "selection_reason" not in columns:
        con.execute("ALTER TABLE records ADD COLUMN selection_reason TEXT")


def _record_reason(record: Mapping[str, Any], objective: str,
                   allowed_splits: tuple[str, ...]) -> str | None:
    issues = validate_record(record)
    if issues:
        return "invalid:" + issues[0]
    ok, reason = _eligible(record, objective, allowed_splits)
    return None if ok else reason


def _check_limits(input_tokens: int, target_tokens: int, config: SelectionConfig) -> str | None:
    total = input_tokens + target_tokens
    if config.max_input_tokens_per_record is not None and input_tokens > config.max_input_tokens_per_record:
        return "input_token_limit"
    if config.max_target_tokens_per_record is not None and target_tokens > config.max_target_tokens_per_record:
        return "target_token_limit"
    if config.max_record_tokens is not None and total > config.max_record_tokens:
        return "record_token_limit"
    return None


def _build_groups(con: sqlite3.Connection, seed: str) -> None:
    # A relation is atomic: if any member lacks valid objective supervision, none
    # of that relation is eligible for selection.
    con.execute("""
      UPDATE records SET reason='related_group_has_ineligible_member'
      WHERE candidate=1 AND reason IS NULL AND atomic_group IN (
        SELECT atomic_group FROM records GROUP BY atomic_group
        HAVING SUM(CASE WHEN candidate=1 AND reason IS NULL THEN 1 ELSE 0 END) < COUNT(*)
      )
    """)
    con.execute("""
      UPDATE records SET reason='related_group_crosses_source_splits'
      WHERE candidate=1 AND reason IS NULL AND leakage_group IN (
        SELECT leakage_group FROM group_splits GROUP BY leakage_group HAVING COUNT(*) > 1
      )
    """)
    # Identical content with contradictory source labels is quarantined as a unit.
    con.execute("""
      UPDATE records SET reason='conflicting_label'
      WHERE candidate=1 AND reason IS NULL AND content_fingerprint IN (
        SELECT content_fingerprint FROM records WHERE content_fingerprint IS NOT NULL
        GROUP BY content_fingerprint HAVING COUNT(DISTINCT labels_json) > 1
      )
    """)
    con.execute("DELETE FROM groups")
    con.execute("DELETE FROM group_strata")
    con.execute("""
      INSERT INTO groups(group_key,leakage_group,row_count,input_tokens,target_tokens,total_tokens,rank_key)
      SELECT atomic_group,MIN(leakage_group),COUNT(*),SUM(input_tokens),SUM(target_tokens),SUM(total_tokens),''
      FROM records GROUP BY atomic_group
      HAVING SUM(CASE WHEN candidate=1 AND reason IS NULL THEN 1 ELSE 0 END)=COUNT(*)
    """)
    for group_key, in con.execute("SELECT group_key FROM groups"):
        con.execute("UPDATE groups SET rank_key=? WHERE group_key=?",
                    (stable_hash([seed, group_key]), group_key))
    con.execute("""
      INSERT INTO group_strata(group_key,dimension,value,count)
      SELECT g.group_key,'source',COALESCE(r.source,'<missing>'),COUNT(*)
      FROM groups g JOIN records r ON r.atomic_group=g.group_key GROUP BY g.group_key,COALESCE(r.source,'<missing>')
    """)
    con.execute("""
      INSERT INTO group_strata(group_key,dimension,value,count)
      SELECT g.group_key,'source_task',COALESCE(r.source_task,'<missing>'),COUNT(*)
      FROM groups g JOIN records r ON r.atomic_group=g.group_key GROUP BY g.group_key,COALESCE(r.source_task,'<missing>')
    """)
    con.execute("""
      INSERT INTO group_strata(group_key,dimension,value,count)
      SELECT g.group_key,'language',COALESCE(r.language,'<missing>'),COUNT(*)
      FROM groups g JOIN records r ON r.atomic_group=g.group_key GROUP BY g.group_key,COALESCE(r.language,'<missing>')
    """)
    totals = {(d, v): n for d, v, n in con.execute(
        "SELECT dimension,value,SUM(count) FROM group_strata GROUP BY dimension,value")}
    cur = con.execute("SELECT group_key FROM groups")
    for (group_key,) in cur:
        score = sum(1.0 / totals[(d, v)] for d, v, _ in con.execute(
            "SELECT dimension,value,count FROM group_strata WHERE group_key=?", (group_key,)))
        con.execute("UPDATE groups SET rarity=? WHERE group_key=?", (score, group_key))
    con.commit()


def _select_groups(con: sqlite3.Connection, config: SelectionConfig) -> dict[str, Any]:
    con.execute("UPDATE groups SET selected=0")
    con.execute("UPDATE records SET selected=0")
    mins, maxs = config.min_quotas or {}, config.max_quotas or {}
    selected_counts: dict[str, Counter[str]] = defaultdict(Counter)
    chosen = set()
    used_examples = 0
    used_tokens = 0

    def strata(group_key: str) -> list[tuple[str, str, int]]:
        return con.execute("SELECT dimension,value,count FROM group_strata WHERE group_key=?", (group_key,)).fetchall()

    def limits_fit(group_key: str) -> bool:
        for dim, val, count in strata(group_key):
            limit = maxs.get(dim, {}).get(val)
            if limit is not None and selected_counts[dim][val] + count > int(limit):
                return False
        return True

    def budgets_fit(rows: tuple[str, int, int, int, str, float]) -> bool:
        _, n, _, _, tokens, _ = rows
        if config.max_examples is not None and used_examples + n > config.max_examples:
            return False
        if config.max_tokens is not None and used_tokens + int(tokens) > config.max_tokens:
            return False
        return True

    def choose(group_key: str, n: int, tokens: int) -> None:
        nonlocal used_examples, used_tokens
        con.execute("UPDATE groups SET selected=1 WHERE group_key=?", (group_key,))
        for dim, val, amount in strata(group_key):
            selected_counts[dim][val] += amount
        chosen.add(group_key)
        used_examples += n
        used_tokens += tokens

    for dim, requirements in sorted(mins.items()):
        for val, target in sorted(requirements.items()):
            candidates = con.execute("""
              SELECT g.group_key,g.row_count,g.input_tokens,g.target_tokens,g.total_tokens,g.rarity
              FROM groups g JOIN group_strata s ON s.group_key=g.group_key
              WHERE g.usable=1 AND g.selected=0 AND s.dimension=? AND s.value=?
              ORDER BY g.rank_key
            """, (dim, str(val)))
            for group_key, n, inp, tgt, total, rarity in candidates:
                if selected_counts[dim][str(val)] >= int(target):
                    break
                if not budgets_fit((group_key, n, inp, tgt, str(total), rarity)) or not limits_fit(group_key):
                    continue
                choose(group_key, n, total)

    fill = con.execute("""
      SELECT group_key,row_count,input_tokens,target_tokens,total_tokens,rarity
      FROM groups WHERE usable=1 AND selected=0 ORDER BY rarity DESC,rank_key
    """)
    for group_key, n, inp, tgt, total, rarity in fill:
        if not budgets_fit((group_key, n, inp, tgt, str(total), rarity)) or not limits_fit(group_key):
            continue
        choose(group_key, n, total)
    con.execute("UPDATE records SET selected=1 WHERE atomic_group IN (SELECT group_key FROM groups WHERE selected=1)")
    con.execute("UPDATE records SET selection_reason=CASE WHEN selected=1 THEN NULL ELSE 'selection_budget_or_quota' END WHERE candidate=1 AND reason IS NULL")
    con.commit()
    unmet = {}
    for dim, requirements in mins.items():
        for val, target in requirements.items():
            actual = selected_counts[dim][str(val)]
            if actual < int(target):
                unmet[f"{dim}:{val}"] = {"requested": int(target), "selected": actual}
    return {"selected_groups": len(chosen), "selected_records": used_examples,
            "selected_tokens": used_tokens, "selected_by_source": dict(sorted(selected_counts["source"].items())),
            "selected_by_task": dict(sorted(selected_counts["source_task"].items())),
            "selected_by_language": dict(sorted(selected_counts["language"].items())),
            "unmet_min_quotas": unmet}


def _sql_distribution(con: sqlite3.Connection, expression: str, where: str) -> dict[str, Any] | None:
    # Keep percentile calculation bounded: SQLite orders values on disk and we
    # fetch only the handful of requested order statistics.
    count = int(con.execute(f"SELECT COUNT({expression}) FROM records WHERE {where}").fetchone()[0])
    if not count:
        return None
    def at(index: int) -> int:
        return int(con.execute(f"SELECT {expression} FROM records WHERE {where} AND {expression} IS NOT NULL ORDER BY {expression} LIMIT 1 OFFSET ?", (index,)).fetchone()[0])
    lo, hi = at((count - 1) // 2), at(count // 2)
    return {"count": count, "min": at(0), "median": statistics.median((lo, hi)),
            "p90": at(int((count - 1) * .90)), "p95": at(int((count - 1) * .95)),
            "max": at(count - 1),
            "sum": int(con.execute(f"SELECT COALESCE(SUM({expression}),0) FROM records WHERE {where}").fetchone()[0])}


def _eligibility_audit(con: sqlite3.Connection) -> dict[str, Any]:
    result = {}
    for source, task, total, eligible in con.execute("""
      SELECT COALESCE(source,'<missing>'),COALESCE(source_task,'<missing>'),COUNT(*),
      SUM(CASE WHEN candidate=1 AND reason IS NULL THEN 1 ELSE 0 END)
      FROM records GROUP BY source,source_task ORDER BY source,source_task
    """):
        reasons = {reason: count for reason, count in con.execute("""
          SELECT COALESCE(reason,'<eligible>'),COUNT(*) FROM records
          WHERE COALESCE(source,'<missing>')=? AND COALESCE(source_task,'<missing>')=?
          GROUP BY reason ORDER BY reason
        """, (source, task))}
        result[f"{source}:{task}"] = {"records": total, "eligible": eligible, "excluded": total-eligible,
                                       "reasons": reasons}
    return result


def _selected_manifest_rows(con: sqlite3.Connection):
    for row in con.execute("""
      SELECT record_id,content_fingerprint,source,source_revision,source_checksum,source_file,
             source_record_id_json,source_ordinal,source_locator_json,source_split,input_tokens,target_tokens,total_tokens
      FROM records WHERE selected=1 ORDER BY source,source_file,record_id
    """):
        (rid, fp, source, revision, checksum, file, source_id, ordinal, locator, split, inp, tgt, total) = row
        yield {"record_id": rid, "content_fingerprint": fp, "source": source,
               "source_revision": revision, "source_checksum": checksum, "source_file": file,
               "source_record_id": json.loads(source_id) if source_id else None,
               "source_ordinal": ordinal, "source_locator": json.loads(locator) if locator else None,
               "source_split": split, "input_tokens": inp, "target_tokens": tgt,
               "total_tokens": total}


def _selected_records_from_input(input_path: Path, con: sqlite3.Connection):
    selected = iter(con.execute(
        "SELECT byte_offset,input_tokens,target_tokens,total_tokens FROM records WHERE selected=1 ORDER BY byte_offset"))
    wanted = next(selected, None)
    with input_path.open("rb") as f:
        while True:
            offset = f.tell()
            raw = f.readline()
            if not raw:
                break
            while wanted is not None and int(wanted[0]) < offset:
                wanted = next(selected, None)
            if wanted is None or int(wanted[0]) != offset:
                continue
            row = json.loads(raw)
            _, inp, tgt, total = wanted
            row["input_token_count"] = inp
            row["target_token_count"] = tgt
            row["token_count"] = total
            row["token_count_method"] = "pinned_local_tokenizer_json"
            yield row
            wanted = next(selected, None)


def select_jsonl_disk_backed(
    input_path: str | Path, output_dir: str | Path, checkpoint_path: str | Path,
    config: SelectionConfig, token_counter: Callable[[str], int], tokenizer_sha256: str,
    *, resume: bool = False, checkpoint_every_records: int = 1000,
    max_temp_bytes: int | None = None, interrupt_after_records: int | None = None,
) -> dict[str, Any]:
    """Select from a canonical JSONL stream using SQLite metadata, never all rows in RAM."""
    source = Path(input_path)
    output = Path(output_dir)
    checkpoint = Path(checkpoint_path)
    if not source.is_file():
        raise SelectionError(f"canonical input not found: {source}")
    if checkpoint_every_records < 1:
        raise SelectionError("checkpoint_every_records must be positive")
    output.mkdir(parents=True, exist_ok=True)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(source)
    cfg_hash = stable_hash(asdict(config))
    cfg_meta = {"source_sha256": source_hash, "config_sha256": cfg_hash,
                "tokenizer_sha256": tokenizer_sha256, "objective": config.objective}
    existed = checkpoint.exists()
    if existed and not resume:
        raise SelectionError(f"checkpoint already exists; pass --resume after verifying it or choose another path: {checkpoint}")
    con = sqlite3.connect(checkpoint)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    _schema(con)
    if existed:
        for key, expected in cfg_meta.items():
            actual = _get_state(con, key)
            if actual != expected:
                con.close()
                raise SelectionError(f"checkpoint mismatch for {key}; source/config/tokenizer changed")
    else:
        for key, value in cfg_meta.items():
            _set_state(con, key, value)
        _set_state(con, "stage", "scan")
        _set_state(con, "checkpoint_offset", "0")
        _set_state(con, "records_read", "0")
        con.commit()

    stage = _get_state(con, "stage", "scan")
    if stage == "scan":
        offset = int(_get_state(con, "checkpoint_offset", "0") or 0)
        con.execute("BEGIN")
        with source.open("rb") as f:
            f.seek(offset)
            since_checkpoint = 0
            while True:
                row_offset = f.tell()
                raw = f.readline()
                if not raw:
                    break
                seq = int(_get_state(con, "records_read", "0") or 0) + 1
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise ValueError("record_is_not_object")
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                    con.execute("INSERT INTO records(seq,byte_offset,atomic_group,leakage_group,reason) VALUES(?,?,?,?,?)",
                                (seq, row_offset, f"bad-row:{seq}", f"bad-row:{seq}", "malformed_json"))
                else:
                    rid = str(record.get("record_id") or "")
                    reason = None
                    if not rid:
                        reason = "missing_record_id"
                    elif not con.execute("SELECT 1 FROM seen_ids WHERE record_id=?", (rid,)).fetchone():
                        con.execute("INSERT INTO seen_ids(record_id) VALUES(?)", (rid,))
                    else:
                        reason = "duplicate_record_id"
                    atomic = _atomic_group(record) if rid else f"bad-row:{seq}"
                    leakage = _leakage_group(record) if rid else f"bad-row:{seq}"
                    split = record.get("source_split")
                    if split is not None:
                        con.execute("INSERT OR IGNORE INTO group_splits(leakage_group,source_split) VALUES(?,?)",
                                    (leakage, str(split)))
                    labels_json = _json(record.get("source_labels") or {})
                    candidate = 0
                    inp_count = target_count = total_count = None
                    if reason is None:
                        reason = _record_reason(record, config.objective, config.allowed_splits)
                    if reason is None:
                        input_parts, target_parts = objective_text_parts(record, config.objective)
                        input_chars = sum(map(len, input_parts)) + max(0, len(input_parts) - 1)
                        target_chars = sum(map(len, target_parts)) + max(0, len(target_parts) - 1)
                        if config.max_input_characters_per_record is not None and input_chars > config.max_input_characters_per_record:
                            reason = "input_character_limit"
                        elif config.max_target_characters_per_record is not None and target_chars > config.max_target_characters_per_record:
                            reason = "target_character_limit"
                        else:
                            in_text = "\n".join(input_parts)
                            target_text = "\n".join(target_parts)
                            inp_count = int(token_counter(in_text))
                            target_count = int(token_counter(target_text))
                            total_count = inp_count + target_count
                            reason = _check_limits(inp_count, target_count, config)
                        if reason is None:
                            candidate = 1
                    con.execute("""
                      INSERT INTO records(seq,byte_offset,record_id,content_fingerprint,atomic_group,leakage_group,
                        source_split,source,source_task,language,labels_json,input_tokens,target_tokens,total_tokens,
                        source_revision,source_checksum,source_file,source_record_id_json,source_ordinal,source_locator_json,
                        candidate,reason)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (seq, row_offset, rid or None, record.get("content_fingerprint"), atomic, leakage,
                          str(split) if split is not None else None, record.get("source"), record.get("source_task"),
                          record.get("language"), labels_json, inp_count, target_count, total_count,
                          record.get("source_revision"), record.get("source_checksum"), record.get("source_file"),
                          _json(record.get("source_record_id")) if record.get("source_record_id") is not None else None,
                          record.get("source_ordinal"), _json(record.get("source_locator")) if record.get("source_locator") is not None else None,
                          candidate, reason))
                _set_state(con, "records_read", str(seq))
                _set_state(con, "checkpoint_offset", str(f.tell()))
                since_checkpoint += 1
                if since_checkpoint >= checkpoint_every_records:
                    con.commit()
                    since_checkpoint = 0
                    con.execute("BEGIN")
                    if max_temp_bytes is not None:
                        temp_size = sum(Path(str(checkpoint)+suffix).stat().st_size
                                        for suffix in ("", "-wal", "-shm") if Path(str(checkpoint)+suffix).exists())
                        if temp_size > max_temp_bytes:
                            raise SelectionError(f"temporary selection storage exceeded configured cap ({temp_size}>{max_temp_bytes})")
                if interrupt_after_records is not None and seq >= interrupt_after_records:
                    con.rollback()
                    con.close()
                    raise SelectionInterrupted(f"interrupted after {seq} rows; resume using the same checkpoint")
            con.commit()
        _build_groups(con, config.seed)
        _set_state(con, "stage", "prepared")
        con.commit()

    # Rebuild selection deterministically on every resume after scan completion.
    selection = _select_groups(con, config)
    _set_state(con, "stage", "selected")
    con.commit()

    candidates_where = "candidate=1 AND reason IS NULL"
    eligible_count = con.execute(f"SELECT COUNT(*) FROM records WHERE {candidates_where}").fetchone()[0]
    exclusions = {reason: count for reason, count in con.execute(
        "SELECT COALESCE(reason,'<none>'),COUNT(*) FROM records WHERE reason IS NOT NULL GROUP BY reason ORDER BY reason")}
    selection_exclusions = {reason: count for reason, count in con.execute(
        "SELECT selection_reason,COUNT(*) FROM records WHERE selection_reason IS NOT NULL GROUP BY selection_reason ORDER BY selection_reason")}
    reasons_by_source_task = _eligibility_audit(con)
    distributions = {
        "eligible_pool": {
            "input": _sql_distribution(con, "input_tokens", candidates_where),
            "target": _sql_distribution(con, "target_tokens", candidates_where),
            "total": _sql_distribution(con, "total_tokens", candidates_where),
        },
        "selected": {
            "input": _sql_distribution(con, "input_tokens", "selected=1"),
            "target": _sql_distribution(con, "target_tokens", "selected=1"),
            "total": _sql_distribution(con, "total_tokens", "selected=1"),
        },
    }
    selected_label_coverage: dict[str, Counter[str]] = defaultdict(Counter)
    for (labels,) in con.execute("SELECT labels_json FROM records WHERE selected=1"):
        for key, value in json.loads(labels or "{}").items():
            if value is not None:
                selected_label_coverage[key][repr(value)] += 1
    write_jsonl(output / "selection_manifest.jsonl", _selected_manifest_rows(con))
    write_jsonl(output / "records.jsonl", _selected_records_from_input(source, con))
    selected_ids_sha = sha256_file(output / "records.jsonl")
    manifest_sha = sha256_file(output / "selection_manifest.jsonl")
    reconstruction_ready = bool(selection["selected_records"]) and con.execute(
        "SELECT COUNT(*) FROM records WHERE selected=1 AND (source_revision IS NULL OR source_checksum IS NULL OR (source_record_id_json IS NULL AND source_ordinal IS NULL))"
    ).fetchone()[0] == 0
    db_bytes = sum(Path(str(checkpoint)+suffix).stat().st_size
                   for suffix in ("", "-wal", "-shm") if Path(str(checkpoint)+suffix).exists())
    report = {
        "manifest_version": "1.0", "pipeline_version": __version__, "input_file": source.name,
        "input_bytes": source.stat().st_size, "input_sha256": source_hash,
        "selection_config_sha256": cfg_hash, "tokenizer_sha256": tokenizer_sha256,
        "objective": config.objective, "seed": config.seed,
        "budgets": {"max_examples": config.max_examples, "max_tokens": config.max_tokens,
                    "max_input_tokens_per_record": config.max_input_tokens_per_record,
                    "max_target_tokens_per_record": config.max_target_tokens_per_record,
                    "max_record_tokens": config.max_record_tokens,
                    "max_input_characters_per_record": config.max_input_characters_per_record,
                    "max_target_characters_per_record": config.max_target_characters_per_record},
        "quotas": {"minimum": config.min_quotas or {}, "maximum": config.max_quotas or {}},
        "allowed_splits": list(config.allowed_splits), "records_read": int(_get_state(con, "records_read", "0") or 0),
        "validated_records": int(con.execute("SELECT COUNT(*) FROM records WHERE (reason IS NULL OR (reason NOT LIKE 'invalid:%' AND reason NOT IN ('malformed_json','missing_record_id')))").fetchone()[0]),
        "eligible_records": eligible_count, "selected_records": selection["selected_records"],
        "selected_tokens": selection["selected_tokens"], "selected_by_source": selection["selected_by_source"],
        "selected_by_task": selection["selected_by_task"], "selected_by_language": selection["selected_by_language"],
        "source_label_coverage": {k: dict(v) for k,v in sorted(selected_label_coverage.items())},
        "eligibility_by_source_task": reasons_by_source_task,
        "exclusions_by_reason": exclusions, "selection_exclusions_by_reason": selection_exclusions,
        "token_distributions": distributions,
        "unmet_min_quotas": selection["unmet_min_quotas"],
        "reconstruction_ready": reconstruction_ready,
        "artifacts": {"records.jsonl": {"sha256": selected_ids_sha, "bytes": (output/"records.jsonl").stat().st_size},
                      "selection_manifest.jsonl": {"sha256": manifest_sha, "bytes": (output/"selection_manifest.jsonl").stat().st_size}},
        "checkpoint": {"path": checkpoint.name, "records_read": int(_get_state(con,"records_read","0") or 0),
                       "sqlite_bytes": db_bytes, "stage": "selected"},
        "full_source_processing": False,
    }
    report["manifest_fingerprint"] = stable_hash({k: v for k, v in report.items() if k != "checkpoint"})
    write_json(output / "dataset_manifest.json", report)
    con.close()
    return report

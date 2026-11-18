import json

from dataset_pipeline.adapters.devign import (
    iter_json_array, iter_json_array_windows, map_devign, map_devign_official,
)
from dataset_pipeline.adapters.microsoft import iter_jsonl, iter_jsonl_windows
from dataset_pipeline.adapters.microsoft import map_microsoft
from dataset_pipeline.scale_validation import build_prefix_workloads
from dataset_pipeline.models import canonical_record
from dataset_pipeline.training_serialization import qwen_chat_token_counts, training_messages
from dataset_pipeline.validation import validate_record


def test_microsoft_task_mapping_keeps_labels_namespaced():
    row = {"id": 7, "oldf": "old", "patch": "diff", "msg": "comment", "y": 1, "lang": "py"}
    canonical = map_microsoft(row, task="Diff_Quality_Estimation", split="train",
                              source_file="cls.jsonl", revision="r1", ordinal=1)
    assert canonical["source_labels"] == {"y": 1}
    assert canonical["defect_status"] is None and canonical["severity"] is None
    assert canonical["eligibility"]["defect_detection"] is False
    assert not validate_record(canonical)


def test_malformed_jsonl_is_quarantined(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"id":1}\nnope\n', encoding="utf8")
    rows = list(iter_jsonl(p))
    assert rows[0][1]["id"] == 1 and rows[0][2] is None
    assert rows[1][1] is None and rows[1][2].startswith("malformed_json")


def test_missing_required_context_is_reported_not_filled():
    canonical = map_microsoft({"id": 8, "patch": "d", "msg": "c"},
                              task="Comment_Generation", split="train",
                              source_file="msg.jsonl", revision="r1", ordinal=1)
    assert canonical["before_code"] is None
    assert "missing_required:before_code" in validate_record(canonical)


def test_byte_capped_partial_line_is_not_mislabeled_bad_source_data(tmp_path):
    p = tmp_path / "long.jsonl"
    p.write_text('{"id":1,"body":"' + ('x' * 100) + '"}\n', encoding="utf8")
    rows = list(iter_jsonl(p, max_bytes=20))
    assert len(rows) == 1
    assert rows[0][2] == "sample_boundary_partial_record"


def test_max_record_bytes_skips_oversized_line_without_parsing_it(tmp_path):
    p = tmp_path / "bounded.jsonl"
    p.write_text('{"id":1,"v":"' + ('x' * 100) + '"}\n{"id":2}\n', encoding="utf8")
    rows = list(iter_jsonl(p, max_record_bytes=32))
    assert rows[0][1] is None and rows[0][2] == "record_exceeds_max_record_bytes"
    assert rows[1][1] == {"id": 2} and rows[1][2] is None


def test_bounded_jsonl_windows_are_deterministic_and_spread_across_file(tmp_path):
    p = tmp_path / "many.jsonl"
    p.write_text("\n".join(json.dumps({"id": i, "payload": "x" * 80}) for i in range(100)), encoding="utf8")
    a = list(iter_jsonl_windows(p, byte_budget=2400, seed="stable"))
    b = list(iter_jsonl_windows(p, byte_budget=2400, seed="stable"))
    assert [(o, r and r["id"]) for o, r, _ in a] == [(o, r and r["id"]) for o, r, _ in b]
    offsets = [o for o, r, e in a if r]
    assert offsets and max(offsets) > p.stat().st_size // 2


def test_bounded_devign_windows_find_records_across_json_array(tmp_path):
    p = tmp_path / "devign.json"
    rows = [{"project": "p", "commit_id": str(i), "target": i % 2, "func": "x" * 100} for i in range(40)]
    p.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf8")
    sampled = list(iter_json_array_windows(p, byte_budget=3200, seed="stable"))
    offsets = [o for o, row, _ in sampled if row]
    assert offsets and max(offsets) > p.stat().st_size // 2


def test_devign_candidate_target_is_not_promoted_to_defect_field(tmp_path):
    p = tmp_path / "array.json"
    p.write_text(json.dumps([{"project": "p", "commit_id": "c", "target": 1, "func": "f"}]))
    items = list(iter_json_array(p, max_bytes=1000))
    assert items[0][1]["target"] == 1
    mapped = map_devign(items[0][1], source_file="array.json", revision="unverified", ordinal=1)
    assert mapped["source_labels"]["target"] == 1
    assert mapped["defect_status"] is None
    assert mapped["source_split"] is None


def test_official_devign_mapping_preserves_documented_binary_semantics():
    mapped = map_devign_official(
        {"project": "FFmpeg", "commit_id": "abc", "target": 1, "func": "int f() { return 0; }"},
        source_file="official/function.json", revision="codexglue-r1",
        source_checksum="a" * 64, ordinal=42, split="train",
    )
    assert mapped["source_record_id"] == 42
    assert mapped["source_split"] == "train"
    assert mapped["source_labels"]["target"] == 1
    assert mapped["defect_status"] == "defective"
    assert mapped["eligibility"]["defect_detection"] is True
    assert mapped["repository"] == "FFmpeg"
    assert mapped["commit_id"] == "abc"
    assert mapped["leakage_group_id"] is not None
    assert not validate_record(mapped)


def test_official_devign_mapping_does_not_promote_invalid_target():
    mapped = map_devign_official(
        {"project": "FFmpeg", "commit_id": "abc", "target": 2, "func": "int f() { return 0; }"},
        source_file="official/function.json", revision="codexglue-r1",
        source_checksum="a" * 64, ordinal=43, split="validation",
    )
    assert mapped["defect_status"] is None
    assert mapped["eligibility"]["defect_detection"] is False
    assert "invalid_or_missing_official_binary_target" in mapped["exclusion_reasons"]


def test_leakage_group_uses_commit_when_repo_and_commit_are_present():
    common = dict(source="s", revision="r", source_file="f", source_id=1,
                  ordinal=1, task="t", split="train", repository="FFmpeg")
    first = canonical_record(**common, commit_id="commit-a")
    second = canonical_record(**common, commit_id="commit-b")
    assert first["leakage_group_id"] != second["leakage_group_id"]


def test_fix_prompt_never_contains_source_fixed_code():
    messages = training_messages({
        "before_code": "def f(): return 0",
        "diff": "- return 0\n+ return 1",
        "review_comment": "Return the expected value.",
        "after_code": "def f(): return 1",
    }, "fix_recommendation")
    assert messages[2]["content"] == "def f(): return 1"
    assert "def f(): return 1" not in messages[1]["content"]


def test_source_review_comment_is_exact_assistant_target():
    comment = "Please handle the empty input case."
    messages = training_messages({
        "before_code": "def f(x): return x[0]",
        "diff": "+ return x[0]",
        "review_comment": comment,
    }, "review_comment_generation")
    assert messages[2]["content"] == comment


def test_defect_target_uses_only_source_supported_binary_label():
    record = {"before_code": "void f() {}", "defect_status": "defective"}
    messages = training_messages(record, "defect_detection")
    assert json.loads(messages[2]["content"]) == {"defect_status": "defective"}
    record["defect_status"] = None
    try:
        training_messages(record, "defect_detection")
    except ValueError as exc:
        assert "source-supported" in str(exc)
    else:
        raise AssertionError("missing defect label must not be fabricated")


def test_chat_token_count_separates_prompt_and_assistant_suffix():
    class FakeTokenizer:
        def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
            assert tokenize is True
            if messages[-1]["role"] == "assistant":
                return {"input_ids": [1, 2, 3, 4]}
            assert add_generation_prompt is True
            return {"input_ids": [1, 2]}

    counts = qwen_chat_token_counts(FakeTokenizer(), [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ])
    assert counts == {"input_tokens": 2, "target_tokens": 2, "total_tokens": 4}


def test_scale_workload_streams_real_fixture_prefixes(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text("".join(json.dumps({"id": i, "oldf": "old", "patch": "diff", "msg": "comment"}) + "\n"
                             for i in range(5)), encoding="utf8")
    report = build_prefix_workloads(source, tmp_path / "stages", stages=(2, 4), max_source_bytes=10000)
    assert report["records_valid"] == 4
    assert report["stage_records"]["2"]["records"] == 2
    assert report["stage_records"]["4"]["records"] == 4

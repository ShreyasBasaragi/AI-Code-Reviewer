import json

import pytest

from dataset_pipeline.disk_selection import SelectionInterrupted, select_jsonl_disk_backed
from dataset_pipeline.models import canonical_record
from dataset_pipeline.selection import SelectionConfig, SelectionError


def record(i, *, split="train", source="ms", language="py", repo=None, checksum="srcsha"):
    return canonical_record(
        source=source, revision="rev-1", source_checksum=checksum, source_file="source.jsonl",
        source_id=i, ordinal=i, task="comment_generation", split=split, repository=repo,
        language=language, before_code="def f(): return 1", diff="+ return 1",
        review_comment=f"Use clearer name {i}", source_labels={"y": 1},
        eligibility={"review_comment_generation": True}, provenance_status="verified-test-fixture",
    )


def write_rows(path, rows):
    path.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in rows), encoding="utf8")


def test_disk_selector_streams_budgets_and_manifest(tmp_path):
    source = tmp_path / "input.jsonl"
    write_rows(source, [record(i, source="github" if i % 2 else "ms", language="py" if i % 2 else "go") for i in range(8)])
    cfg = SelectionConfig(objective="review_comment_generation", max_examples=4, max_tokens=100,
                          min_quotas={"source": {"github": 1}, "language": {"go": 1}})
    report = select_jsonl_disk_backed(source, tmp_path / "out", tmp_path / "work.sqlite3", cfg,
                                      lambda text: len(text.split()), "toksha", checkpoint_every_records=2)
    assert report["records_read"] == 8
    assert report["selected_records"] <= 4
    assert report["selected_tokens"] <= 100
    assert report["selected_by_source"].get("github", 0) >= 1
    assert report["token_distributions"]["selected"]["input"] is not None
    assert report["token_distributions"]["selected"]["target"] is not None
    assert report["token_distributions"]["selected"]["total"]["sum"] == report["selected_tokens"]
    assert report["reconstruction_ready"]
    exported = [json.loads(x) for x in (tmp_path / "out" / "records.jsonl").read_text().splitlines()]
    assert len(exported) == report["selected_records"]
    assert all("input_token_count" in x and "target_token_count" in x for x in exported)
    repeated = select_jsonl_disk_backed(source, tmp_path / "out", tmp_path / "work.sqlite3", cfg,
                                        lambda text: len(text.split()), "toksha", resume=True,
                                        checkpoint_every_records=2)
    assert repeated["manifest_fingerprint"] == report["manifest_fingerprint"]
    assert repeated["artifacts"] == report["artifacts"]


def test_disk_selector_resumes_from_last_committed_boundary(tmp_path):
    source = tmp_path / "input.jsonl"
    write_rows(source, [record(i) for i in range(6)])
    cfg = SelectionConfig(objective="review_comment_generation", max_examples=4)
    db = tmp_path / "resume.sqlite3"
    with pytest.raises(SelectionInterrupted):
        select_jsonl_disk_backed(source, tmp_path / "out", db, cfg,
                                 lambda text: len(text.split()), "tok", checkpoint_every_records=2,
                                 interrupt_after_records=3)
    report = select_jsonl_disk_backed(source, tmp_path / "out", db, cfg,
                                      lambda text: len(text.split()), "tok", resume=True,
                                      checkpoint_every_records=2)
    assert report["records_read"] == 6
    assert report["selected_records"] == 4
    ids = [json.loads(x)["record_id"] for x in (tmp_path / "out" / "records.jsonl").read_text().splitlines()]
    assert len(ids) == len(set(ids)) == 4


def test_resume_rejects_changed_source_or_configuration(tmp_path):
    source = tmp_path / "input.jsonl"
    write_rows(source, [record(i) for i in range(3)])
    cfg = SelectionConfig(objective="review_comment_generation", max_examples=2)
    db = tmp_path / "resume.sqlite3"
    with pytest.raises(SelectionInterrupted):
        select_jsonl_disk_backed(source, tmp_path / "out", db, cfg,
                                 lambda text: 1, "tok", checkpoint_every_records=1,
                                 interrupt_after_records=1)
    write_rows(source, [record(i) for i in range(4)])
    with pytest.raises(SelectionError, match="checkpoint mismatch for source_sha256"):
        select_jsonl_disk_backed(source, tmp_path / "out", db, cfg,
                                 lambda text: 1, "tok", resume=True)


def test_resume_rejects_changed_configuration(tmp_path):
    source = tmp_path / "input.jsonl"
    write_rows(source, [record(i) for i in range(3)])
    db = tmp_path / "resume.sqlite3"
    with pytest.raises(SelectionInterrupted):
        select_jsonl_disk_backed(source, tmp_path / "out", db,
                                 SelectionConfig(objective="review_comment_generation", max_examples=2),
                                 lambda text: 1, "tok", checkpoint_every_records=1,
                                 interrupt_after_records=1)
    with pytest.raises(SelectionError, match="checkpoint mismatch for config_sha256"):
        select_jsonl_disk_backed(source, tmp_path / "out", db,
                                 SelectionConfig(objective="review_comment_generation", max_examples=1),
                                 lambda text: 1, "tok", resume=True)


def test_character_safety_limit_excludes_without_truncation(tmp_path):
    source = tmp_path / "input.jsonl"
    write_rows(source, [record(1)])
    cfg = SelectionConfig(objective="review_comment_generation", max_examples=1,
                          max_input_characters_per_record=8)
    report = select_jsonl_disk_backed(source, tmp_path / "out", tmp_path / "work.sqlite3", cfg,
                                      lambda text: 1, "tok")
    assert report["eligible_records"] == 0
    assert report["selected_records"] == 0
    assert report["exclusions_by_reason"]["input_character_limit"] == 1

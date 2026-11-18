from dataset_pipeline.adapters.devign import map_devign
from dataset_pipeline.deduplication import audit_duplicates
from dataset_pipeline.models import record_id
from dataset_pipeline.splitting import validate_splits


def test_conflicting_labels_for_same_function_are_reported_not_resolved():
    a = map_devign({"project": "p", "commit_id": "c1", "target": 0, "func": "same"},
                   source_file="f.json", revision="r", ordinal=1)
    b = map_devign({"project": "p", "commit_id": "c2", "target": 1, "func": "same"},
                   source_file="f.json", revision="r", ordinal=2)
    result = audit_duplicates([a, b])
    assert len(result["conflicting_label_groups"]) == 1
    assert a["source_labels"]["target"] == 0 and b["source_labels"]["target"] == 1
    assert a["defect_status"] is None and b["defect_status"] is None


def test_record_id_is_stable_and_disambiguates_reused_source_ids():
    assert record_id("s", "r", "f", 9, 2) == record_id("s", "r", "f", 9, 2)
    assert record_id("s", "r", "f", 9, 2) != record_id("s", "r", "f", 9, 3)


def test_official_source_splits_are_preserved_and_cross_split_groups_reported():
    rows = [
        {"record_id": "a", "repository": "repo", "source_split": "train"},
        {"record_id": "b", "repository": "repo", "source_split": "test"},
    ]
    audit = validate_splits(rows)
    assert audit["split_integrity_verified"] is False
    assert len(audit["cross_split_groups"]) == 1

from dataset_pipeline.selection import SelectionConfig, SelectionError, select_records


def row(i, *, source="ms", task="comment_generation", lang="python", split="train",
        eligible=True, tokens=3, pair=None, leakage=None, checksum="abc"):
    return {
        "record_id": f"r{i}", "content_fingerprint": f"fp{i}", "source": source,
        "source_revision": "rev1", "source_checksum": checksum, "source_file": "s.jsonl",
        "source_record_id": i, "source_ordinal": i, "source_split": split,
        "source_task": task, "language": lang, "repository": None,
        "pull_request_id": None, "commit_id": None, "pair_id": pair,
        "leakage_group_id": leakage, "before_code": "abc", "diff": "d",
        "review_comment": "c", "after_code": None, "token_count": tokens,
        "eligibility": {"comment_generation": eligible}, "quality_status": [],
        "source_labels": {"msg": "c"},
    }


def test_deterministic_quota_coverage_and_budgets():
    rows = [row(1), row(2, source="gh", lang="go"),
            row(3, source="ms", lang="java"), row(4, eligible=False)]
    cfg = SelectionConfig(objective="comment_generation", max_examples=3, max_tokens=9,
                          seed="fixed", min_quotas={"source": {"gh": 1}, "language": {"go": 1}})
    a, ma = select_records(rows, cfg, token_counter=lambda text: 3)
    b, mb = select_records(rows, cfg, token_counter=lambda text: 3)
    assert [r["record_id"] for r in a] == [r["record_id"] for r in b]
    assert ma["manifest_fingerprint"] == mb["manifest_fingerprint"]
    assert len(a) <= 3 and ma["selected_tokens"] <= 9
    assert "gh" in ma["selected_by_source"]
    assert ma["selected_tokens"] == 9
    assert ma["reconstruction_ready"]
    assert any(x["reason"] == "no_source_supported_supervision_for_objective" for x in ma["exclusions"])


def test_token_budget_requires_exact_counter():
    try:
        select_records([row(1)], SelectionConfig(objective="comment_generation", max_tokens=5))
    except SelectionError as exc:
        assert "requires a tokenizer" in str(exc)
    else:
        raise AssertionError("expected token counter requirement")


def test_pair_is_atomic_and_cross_split_leakage_is_excluded():
    paired = [row(1, pair="p"), row(2, pair="p")]
    selected, report = select_records(paired, SelectionConfig(objective="comment_generation", max_examples=1))
    assert selected == []
    assert report["excluded_records"] == 2

    split_rows = [row(3, leakage="repo-x", split="train"), row(4, leakage="repo-x", split="test")]
    selected, report = select_records(split_rows, SelectionConfig(objective="comment_generation", max_examples=5))
    assert selected == []
    assert all(x["reason"] == "related_group_crosses_source_splits" for x in report["exclusions"])


def test_missing_source_checksums_disable_reconstruction():
    selected, report = select_records([row(1, checksum=None)],
                                      SelectionConfig(objective="comment_generation", max_examples=1))
    assert len(selected) == 1
    assert report["reconstruction_ready"] is False


def test_maximum_quotas_are_respected():
    rows = [row(1, source="ms"), row(2, source="ms"), row(3, source="gh")]
    selected, report = select_records(
        rows,
        SelectionConfig(objective="comment_generation", max_examples=3,
                        max_quotas={"source": {"ms": 1}}),
    )
    assert len(selected) == 2
    assert report["selected_by_source"] == {"gh": 1, "ms": 1}

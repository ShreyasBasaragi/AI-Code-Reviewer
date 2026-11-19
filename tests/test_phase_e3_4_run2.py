import json
from types import SimpleNamespace

import pytest

from evaluation import phase_e3_4_run2 as run2
from evaluation.chroma_snapshot import SnapshotVerificationError, directory_sha256


def test_arm_selection_defaults_to_two_v3_arms_and_preserves_four_arm_mode(monkeypatch):
    monkeypatch.delenv("PHASE_E3_4_RUN2_ARMS", raising=False)
    assert run2.select_arms() == ("v3_baseline", "v3_optimized")
    assert run2.select_arms("v3_optimized,v3_baseline") == (
        "v3_baseline", "v3_optimized"
    )
    assert run2.select_arms("v3_optimized,v3_baseline,v2_baseline,no_rag") == (
        "no_rag", "v2_baseline", "v3_baseline", "v3_optimized"
    )


@pytest.mark.parametrize("value", ["", ",", "v3_baseline,", "unknown"])
def test_arm_selection_rejects_empty_or_unknown_names(value):
    with pytest.raises(ValueError):
        run2.select_arms(value)


def test_arm_selection_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicate"):
        run2.select_arms("v3_baseline,v3_baseline")


def test_pinned_run_plan_rejects_selection_changes(tmp_path):
    plan = {"selected_arms": ["v3_baseline", "v3_optimized"], "version": 1}
    run2._pin_plan(tmp_path, plan)
    with pytest.raises(SnapshotVerificationError, match="selected arms or experiment identity"):
        run2._pin_plan(tmp_path, {"selected_arms": ["no_rag"], "version": 1})


def test_pinned_run_plan_rejects_snapshot_identity_changes(tmp_path):
    plan = {"selected_arms": list(run2.DEFAULT_ARMS),
            "snapshot_identities": {"v3": {"snapshot_directory_sha256": "a" * 64}}}
    run2._pin_plan(tmp_path, plan)
    changed = {"selected_arms": list(run2.DEFAULT_ARMS),
               "snapshot_identities": {"v3": {"snapshot_directory_sha256": "b" * 64}}}
    with pytest.raises(SnapshotVerificationError, match="experiment identity"):
        run2._pin_plan(tmp_path, changed)


def test_default_quota_projection_plans_60_requests_only_for_v3(monkeypatch, tmp_path):
    from evaluation import runner
    from llm.groq_client import GroqLLM

    cases = [{"case_id": f"CASE-{index:02}", "source_code": f"value = {index}\n"}
             for index in range(30)]
    monkeypatch.setattr(run2, "load_cases", lambda: cases)
    monkeypatch.setattr(GroqLLM, "_build_prompt",
                        staticmethod(lambda code, context: code + "\n" + " ".join(context)))
    retrieved_arms = []

    def fake_copy(path, *, version, strategy, retrieval_config=None):
        retrieved_arms.append(path.name)
        return lambda _code: [SimpleNamespace(to_prompt_text=lambda: "small evidence")]

    monkeypatch.setattr(runner, "_copy_retriever", fake_copy)
    projection = run2._build_quota_projection(
        snapshot_root=tmp_path / "snapshots", working_root=tmp_path / "working",
        remaining_daily_tokens=200000,
    )

    assert projection["selected_arms"] == ["v3_baseline", "v3_optimized"]
    assert projection["cases_per_arm"] == 30
    assert projection["planned_requests"] == 60
    assert set(projection["per_arm"]) == {"v3_baseline", "v3_optimized"}
    assert retrieved_arms == ["v3_baseline", "v3_optimized"]


def test_preparation_copies_only_v3_and_pins_shared_snapshot_for_both_arms(
    tmp_path, monkeypatch
):
    snapshot_root = tmp_path / "snapshots"
    v3_path = snapshot_root / "chroma_db_v3"
    (v3_path / "vec-1").mkdir(parents=True)
    (v3_path / "chroma.sqlite3").write_bytes(b"sqlite")
    (v3_path / "vec-1" / "data_level0.bin").write_bytes(b"index")
    collection = {
        "collection_id": "collection-v3", "collection_name": "critique_code_context_v3",
        "record_count": 19418, "dimension": 384, "distance_metric": "cosine",
        "corpus_version": "3", "collection_content_sha256": "a" * 64,
        "vector_index_sha256": "b" * 64, "sqlite_sha256": "c" * 64,
        "sqlite_journal_files": [], "sqlite_journal_sha256": "d" * 64,
    }
    reference = {
        "manifest_sha256": "e" * 64,
        "snapshot_directory_sha256": directory_sha256(v3_path),
        "collection": collection,
    }
    manifest_path = snapshot_root / "snapshot_manifests" / "snapshot-v3.json"
    monkeypatch.setattr(run2, "load_snapshot_manifest",
                        lambda _root, version: (manifest_path, reference))
    monkeypatch.setattr(run2, "validate_saved_snapshot", lambda **_kwargs: {
        "status": "manifest_verified"
    })
    monkeypatch.setattr(run2, "inspect_collection_readonly", lambda _path, _name: collection)

    result = run2.prepare_working_copies(
        snapshot_root=snapshot_root, working_root=tmp_path / "working",
        run_dir=tmp_path / "run2",
    )

    assert set(result["references"]) == {"v3"}
    assert set(result["working_copies"]) == {"v3_baseline", "v3_optimized"}
    assert result["working_copies"]["v3_baseline"]["reference_manifest_sha256"] == \
        result["working_copies"]["v3_optimized"]["reference_manifest_sha256"] == "e" * 64
    assert result["working_copies"]["v3_baseline"]["initial_directory_sha256"] == \
        result["working_copies"]["v3_optimized"]["initial_directory_sha256"] == \
        reference["snapshot_directory_sha256"]
    assert not (snapshot_root / "chroma_db_v2").exists()


def test_resume_preflight_refreshes_dashboard_quota_without_chroma_or_provider_calls(
    tmp_path, monkeypatch
):
    # The preflight checks only whether a key is configured. It never creates
    # a provider client or sends a request; keep this fixture hermetic in CI.
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key-no-provider-call")
    run_dir = tmp_path / "phase_e3_4_run2"
    run_dir.mkdir()
    (run_dir / "preflight.json").write_text(json.dumps({
        "selected_arms": list(run2.DEFAULT_ARMS),
        "quota_projection": {
            "selected_arms": list(run2.DEFAULT_ARMS),
            "planned_requests": 60,
            "projected_total_tokens": 50000,
            "remaining_daily_tokens_reported_by_user_from_provider_dashboard": 100000,
            "reserved_daily_budget_after_safety_margin": 80000,
            "projected_within_daily_budget": True,
            "maximum_projected_single_request_tokens": 2000,
            "conservative_minute_token_budget": 6400,
            "projected_requests_fit_minute_token_budget": True,
            "per_arm": {
                arm: {"per_case": [{"case_id": f"CASE-{index:02}",
                                     "total_tokens_reserved": (50000 // 60)}
                                    for index in range(30)]}
                for arm in run2.DEFAULT_ARMS
            },
        }
    }))
    (run_dir / "groq_v3_baseline.checkpoint.json").write_text(json.dumps({
        "case_results": {"CASE-00": {"status": "completed"}},
    }))
    (run_dir / "rate_limit_usage.json").write_text(json.dumps({
        "policy": {"safety_fraction": 0.8},
        "daily_window": {"starting_remaining_tpd": 100000,
                         "last_remaining_tpd_observed": 100000,
                         "daily_token_budget": 80000, "tokens_accounted": 0},
    }))
    run_manifest = {"manifest_sha256": "a" * 64,
                    "selected_arms": list(run2.DEFAULT_ARMS),
                    "references": {"v3": {"manifest_sha256": "c" * 64,
                                           "snapshot_directory_sha256": "d" * 64}}}
    monkeypatch.setattr(run2, "_load_run_manifest", lambda _path: run_manifest)
    monkeypatch.setattr(run2, "_snapshot_component_result", lambda _root, version: {
        "manifest_sha256": run_manifest["references"][version]["manifest_sha256"]
    })
    monkeypatch.setattr(run2, "verify_working_copy", lambda **kwargs: {
        "arm": kwargs["arm"], "safe_to_continue": True
    })
    monkeypatch.setattr(run2, "_build_quota_projection", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("resume should reuse pinned retrieval estimates")
    ))
    monkeypatch.setattr(run2, "load_cases", lambda: [
        {"case_id": f"CASE-{index:02}"} for index in range(30)
    ])

    report = run2.run_preflight(snapshot_root=tmp_path / "snapshots",
                                working_root=tmp_path / "working", run_dir=run_dir,
                                remaining_daily_tokens=40000, resume=True)

    assert report["status"] == "ready"
    assert report["execution_permitted"] is True
    assert report["selected_arms"] == ["v3_baseline", "v3_optimized"]
    assert report["total_planned_requests"] == 59
    assert report["initial_total_planned_requests"] == 60
    assert report["previous_checkpoints"] == {
        "v3_baseline": True, "v3_optimized": False,
    }
    projection = report["quota_projection"]
    assert projection["remaining_daily_tokens_reported_by_user_from_provider_dashboard"] == 40000
    assert projection["reserved_daily_budget_after_safety_margin"] == 32000
    assert projection["projected_within_daily_budget"] is False
    assert report["full_experiment"]["total_planned_requests"] == 60
    assert report["full_experiment"]["projected_within_available_daily_budget"] is False
    assert report["next_batch"]["selected_pending_case_ids"] == [f"CASE-{index:02}" for index in range(15)]
    assert report["next_batch"]["requests_planned_for_next_invocation"] == 29
    assert report["next_batch"]["estimated_batch_tokens"] == (50000 // 60) * 29
    assert report["next_batch"]["permitted"] is True
    assert report["network"] == {"status": "not_probed", "live_requests_made": 0}


def test_preflight_blocks_when_groq_credentials_are_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    run_dir = tmp_path / "phase_e3_4_run2"
    run_dir.mkdir()
    monkeypatch.setattr(run2, "_load_run_manifest", lambda _path: {
        "manifest_sha256": "a" * 64,
        "selected_arms": ["no_rag"],
        "references": {},
    })
    monkeypatch.setattr(run2, "_plan_identity", lambda **_kwargs: {"selected_arms": ["no_rag"]})
    monkeypatch.setattr(run2, "load_cases", lambda: [{"case_id": "CASE-00"}])
    monkeypatch.setattr(run2, "_build_quota_projection", lambda **_kwargs: {
        "selected_arms": ["no_rag"],
        "planned_requests": 1,
        "projected_total_tokens": 100,
        "initial_projected_total_tokens": 100,
        "remaining_daily_tokens_reported_by_user_from_provider_dashboard": 1000,
        "reserved_daily_budget_after_safety_margin": 800,
        "projected_within_daily_budget": True,
        "maximum_projected_single_request_tokens": 100,
        "conservative_minute_token_budget": 6400,
        "projected_requests_fit_minute_token_budget": True,
        "per_arm": {"no_rag": {"per_case": [
            {"case_id": "CASE-00", "total_tokens_reserved": 100},
        ]}},
    })

    report = run2.run_preflight(
        snapshot_root=tmp_path / "unused-snapshots",
        working_root=tmp_path / "unused-working",
        run_dir=run_dir,
        remaining_daily_tokens=1000,
        arms=("no_rag",),
    )

    assert report["status"] == "blocked"
    assert report["execution_permitted"] is False
    assert "GROQ_API_KEY is not configured" in report["blocked_reasons"]
    assert report["provider"]["credentials_present"] is False
    assert report["network"] == {"status": "not_probed", "live_requests_made": 0}


def test_batch_selection_takes_first_fifteen_then_remaining_fifteen():
    case_ids = [f"CASE-{index:02}" for index in range(30)]
    arms = run2.DEFAULT_ARMS
    estimates = {arm: {case_id: 1000 for case_id in case_ids} for arm in arms}
    first = run2._select_pending_batch(
        case_ids=case_ids, arms=arms, completed_by_arm={arm: set() for arm in arms},
        estimates_by_arm=estimates, max_new_pairs=15, available_daily_tokens=100000,
        minute_token_budget=6400,
    )
    assert first["selected_case_ids"] == case_ids[:15]
    assert first["request_count"] == 30
    completed = {arm: set(case_ids[:15]) for arm in arms}
    second = run2._select_pending_batch(
        case_ids=case_ids, arms=arms, completed_by_arm=completed,
        estimates_by_arm=estimates, max_new_pairs=15, available_daily_tokens=100000,
        minute_token_budget=6400,
    )
    assert second["selected_case_ids"] == case_ids[15:]
    assert second["request_count"] == 30


def test_batch_selection_handles_partial_pair_and_quota_fitting_prefix():
    case_ids = ["A", "B", "C"]
    arms = run2.DEFAULT_ARMS
    selected = run2._select_pending_batch(
        case_ids=case_ids, arms=arms,
        completed_by_arm={"v3_baseline": {"A"}, "v3_optimized": set()},
        estimates_by_arm={"v3_baseline": {key: 100 for key in case_ids},
                          "v3_optimized": {key: 100 for key in case_ids}},
        max_new_pairs=3, available_daily_tokens=300, minute_token_budget=1000,
    )
    assert selected["selected_case_ids"] == ["A", "B"]
    assert selected["requests_by_arm"] == {"v3_baseline": ["B"], "v3_optimized": ["A", "B"]}
    assert selected["estimated_tokens"] == 300


def test_batch_size_is_not_part_of_pinned_experiment_identity(tmp_path):
    first = {"model": "same", "case_ids": ["A", "B"]}
    run2._pin_plan(tmp_path, first)
    # Batch size belongs to a per-session selection, never the immutable plan.
    assert run2._pin_plan(tmp_path, first) == first


def test_batch_selection_blocks_when_single_request_exceeds_minute_budget():
    selected = run2._select_pending_batch(
        case_ids=["A"], arms=run2.DEFAULT_ARMS, completed_by_arm={},
        estimates_by_arm={"v3_baseline": {"A": 7000}, "v3_optimized": {"A": 100}},
        max_new_pairs=1, available_daily_tokens=100000, minute_token_budget=6400,
    )
    assert selected["selected_case_ids"] == []
    assert selected["per_request_minute_budget_fit"] is False

import json
from pathlib import Path

import pytest

from evaluation.runner import (
    CheckpointMismatchError,
    PacedProvider,
    prompt_configuration_digest,
    scoring_configuration_digest,
    run_evaluation,
)
from evaluation.rate_limit import GroqRateLimitController


def _case(case_id):
    return {
        "case_id": case_id, "category": "clean",
        "source_code": f"value = {sum(map(ord, case_id))}\n",
        "expected_issue_type": None, "expected_line": None,
        "rationale": "A simple assignment.", "assumptions": "None.",
        "reference_correction": None,
    }


VALID_EMPTY_REVIEW = json.dumps({"summary": "No issues found.", "findings": []})


class CountingProvider:
    model = "fake-model"
    temperature = 0.1
    structured_output_metadata = {
        "mode": "json_schema", "strict": True,
        "schema_name": "code_review", "schema_version": "review-v1",
    }

    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls = []

    def analyze_code(self, code, context):
        self.calls.append(code)
        action = self.behavior.get(code)
        if isinstance(action, BaseException):
            raise action
        return action or VALID_EMPTY_REVIEW

    def generate_fix(self, code, finding, context):
        raise AssertionError("fix generation was not requested")


class FakeHTTPError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": code, "message": message}}


def test_live_pacer_delays_between_calls_and_offline_mode_never_sleeps():
    ticks = [100.0]
    delays = []

    def sleep(seconds):
        delays.append(seconds)
        ticks[0] += seconds

    class Provider:
        def analyze_code(self, code, context):
            return "ok"

    live = PacedProvider(Provider(), interval_seconds=12, enabled=True,
                         clock=lambda: ticks[0], sleeper=sleep)
    live.analyze_code("a", [])
    live.analyze_code("b", [])
    assert delays == [12.0]

    offline = PacedProvider(Provider(), interval_seconds=12, enabled=False,
                             clock=lambda: ticks[0], sleeper=sleep)
    offline.analyze_code("c", [])
    offline.analyze_code("d", [])
    assert delays == [12.0]


def test_checkpoint_resume_skips_completed_and_retries_failed_cases(tmp_path):
    cases = [_case("C-1"), _case("C-2"), _case("C-3")]
    checkpoint = tmp_path / "run.checkpoint.json"
    provider = CountingProvider({_case("C-2")["source_code"]:
                                 FakeHTTPError(429, "rate_limit", "slow down")})
    # First case succeeds; the second fails exactly once; third is left pending
    # by an injected interrupt so resume behavior is deterministic.
    calls = {"count": 0}

    def fail_second_then_interrupt(code, context):
        calls["count"] += 1
        if calls["count"] == 1:
            provider.calls.append(code)
            return VALID_EMPTY_REVIEW
        if calls["count"] == 2:
            provider.calls.append(code)
            raise FakeHTTPError(429, "rate_limit", "slow down")
        raise KeyboardInterrupt()

    provider.analyze_code = fail_second_then_interrupt
    first = run_evaluation(provider=provider, mode="groq", cases=cases, max_cases=3,
                           model_id=provider.model, checkpoint_path=checkpoint,
                           request_interval_seconds=0)
    assert first["interrupted"] is True
    assert first["case_results"][0]["case_status"] == "completed"
    assert first["case_results"][1]["case_status"] == "failed"
    assert first["case_results"][2]["case_status"] == "interrupted"

    retry_provider = CountingProvider()
    resumed = run_evaluation(provider=retry_provider, mode="groq", cases=cases, max_cases=3,
                             model_id=retry_provider.model, checkpoint_path=checkpoint,
                             resume=True, request_interval_seconds=0)
    assert retry_provider.calls == [_case("C-2")["source_code"], _case("C-3")["source_code"]]
    assert [row["case_status"] for row in resumed["case_results"]] == [
        "completed", "completed", "completed"
    ]
    state = json.loads(checkpoint.read_text())
    assert state["case_results"]["C-1"]["attempt_count"] == 1
    assert state["case_results"]["C-2"]["attempt_count"] == 2
    assert state["case_results"]["C-3"]["attempt_count"] == 2


def test_batch_resume_keeps_full_experiment_identity_and_skips_completed_requests(tmp_path):
    cases = [_case("PAIR-1"), _case("PAIR-2"), _case("PAIR-3")]
    checkpoint = tmp_path / "paired.checkpoint.json"
    first_provider = CountingProvider()
    first = run_evaluation(
        provider=first_provider, mode="groq", cases=cases, max_cases=3,
        model_id=first_provider.model, checkpoint_path=checkpoint,
        batch_case_ids=["PAIR-1"], request_interval_seconds=0,
    )
    assert first_provider.calls == [cases[0]["source_code"]]
    assert first["batch_case_ids"] == ["PAIR-1"]

    second_provider = CountingProvider()
    second = run_evaluation(
        provider=second_provider, mode="groq", cases=cases, max_cases=3,
        model_id=second_provider.model, checkpoint_path=checkpoint, resume=True,
        batch_case_ids=["PAIR-2"], request_interval_seconds=0,
    )
    assert second_provider.calls == [cases[1]["source_code"]]
    assert second["case_ids"] == ["PAIR-1", "PAIR-2"]
    assert json.loads(checkpoint.read_text())["config"]["case_ids"] == [
        "PAIR-1", "PAIR-2", "PAIR-3"
    ]

    third_provider = CountingProvider()
    run_evaluation(
        provider=third_provider, mode="groq", cases=cases, max_cases=3,
        model_id=third_provider.model, checkpoint_path=checkpoint, resume=True,
        batch_case_ids=["PAIR-1", "PAIR-2", "PAIR-3"], request_interval_seconds=0,
    )
    assert third_provider.calls == [cases[2]["source_code"]]


def test_batch_case_ids_cannot_change_pinned_experiment_set(tmp_path):
    cases = [_case("PAIR-1"), _case("PAIR-2")]
    checkpoint = tmp_path / "paired.checkpoint.json"
    provider = CountingProvider()
    run_evaluation(provider=provider, mode="groq", cases=cases, max_cases=2,
                   model_id=provider.model, checkpoint_path=checkpoint,
                   batch_case_ids=["PAIR-1"], request_interval_seconds=0)
    changed_cases = [_case("PAIR-1"), _case("PAIR-2")]
    changed_cases[0]["source_code"] += "# changed benchmark\n"
    with pytest.raises(CheckpointMismatchError, match="benchmark_sha256"):
        run_evaluation(provider=CountingProvider(), mode="groq", cases=changed_cases, max_cases=2,
                       model_id=provider.model, checkpoint_path=checkpoint, resume=True,
                       batch_case_ids=["PAIR-2"], request_interval_seconds=0)


def test_insufficient_daily_quota_is_deferred_without_provider_call(tmp_path):
    case = _case("QUOTA-1")
    provider = CountingProvider()
    controller = GroqRateLimitController(
        state_path=tmp_path / "quota.json", arm="v3_baseline",
        remaining_daily_tokens=1, safety_fraction=0.8, request_interval_seconds=0,
    )
    report = run_evaluation(
        provider=provider, mode="groq", cases=[case], max_cases=1,
        model_id=provider.model, checkpoint_path=tmp_path / "quota.checkpoint.json",
        request_interval_seconds=0, stop_on_provider_error=True,
        rate_limit_controller=controller, rate_limit_arm="v3_baseline",
    )
    assert provider.calls == []
    assert report["quota_deferred_case_ids"] == ["QUOTA-1"]
    assert report["failed_case_ids"] == []
    assert report["case_results"][0]["case_status"] == "quota_deferred"
    assert report["case_results"][0]["rate_limit_errors"][0]["request_sent"] is False


def test_semantic_review_failure_is_checkpointed_without_stopping_later_cases(tmp_path):
    cases = [_case("SEC-01"), _case("SEC-02")]
    invalid_review = json.dumps({
        "summary": "Finding with an unsupported reference.",
        "findings": [{"title": "Issue", "issue_type": "bug", "explanation": "Issue.",
                      "recommended_fix": "Fix it.", "evidence_ids": ["unknown-chunk"],
                      "uncertain": False, "abstained": False}],
    })
    provider = CountingProvider({cases[0]["source_code"]: invalid_review})
    checkpoint = tmp_path / "semantic.checkpoint.json"
    report = run_evaluation(
        provider=provider, mode="groq", cases=cases, max_cases=2,
        model_id=provider.model, checkpoint_path=checkpoint,
        request_interval_seconds=0, stop_on_provider_error=True,
    )
    assert provider.calls == [case["source_code"] for case in cases]
    assert [row["case_status"] for row in report["case_results"]] == ["failed", "completed"]
    state = json.loads(checkpoint.read_text())
    assert state["case_results"]["SEC-01"]["status"] == "failed"
    assert state["case_results"]["SEC-01"]["result"]["processing_errors"] == [
        "invalid_finding: finding_0: unknown_evidence_id:unknown-chunk"
    ]


def test_run2_shell_continues_selected_arms_after_checkpointed_incomplete_runner_result():
    script = Path("evaluation/phase_e3_4_run2.sh").read_text()
    assert '.venv/bin/python -m evaluation.runner "${run_args[@]}" > "$runner_log" 2>&1 || runner_status=$?' in script
    assert 'grep -q "Evaluation incomplete; results/checkpoint were saved." "$runner_log"' in script
    assert 'for arm in "${ARM_LIST[@]}"; do run_config "$arm"; done' in script
    assert 'RUN2_INCOMPLETE=1' in script


def test_checkpoint_configuration_mismatch_fails_before_provider_call(tmp_path):
    case = _case("C-1")
    checkpoint = tmp_path / "run.json"
    provider = CountingProvider()
    run_evaluation(provider=provider, mode="groq", cases=[case], max_cases=1,
                   model_id=provider.model, checkpoint_path=checkpoint,
                   request_interval_seconds=0)
    changed_provider = CountingProvider()
    with pytest.raises(CheckpointMismatchError, match="model_id"):
        run_evaluation(provider=changed_provider, mode="groq", cases=[case], max_cases=1,
                       model_id="different-model", checkpoint_path=checkpoint,
                       resume=True, request_interval_seconds=0)
    assert changed_provider.calls == []


def test_resume_refuses_missing_rate_limit_ledger_when_checkpoint_has_requests(tmp_path):
    case = _case("C-LEDGER")
    checkpoint = tmp_path / "ledger.checkpoint.json"
    ledger = tmp_path / "rate_limit_usage.json"
    provider = CountingProvider()
    controller = GroqRateLimitController(
        state_path=ledger, arm="no_rag", remaining_daily_tokens=200000,
        request_interval_seconds=0,
    )
    run_evaluation(
        provider=provider, mode="groq", cases=[case], max_cases=1,
        model_id=provider.model, checkpoint_path=checkpoint,
        request_interval_seconds=0, rate_limit_controller=controller,
        rate_limit_arm="no_rag",
    )
    ledger.unlink()

    resumed_provider = CountingProvider()
    restarted_controller = GroqRateLimitController(
        state_path=ledger, arm="no_rag", remaining_daily_tokens=200000,
        request_interval_seconds=0,
    )
    with pytest.raises(CheckpointMismatchError, match="missing or behind"):
        run_evaluation(
            provider=resumed_provider, mode="groq", cases=[case], max_cases=1,
            model_id=resumed_provider.model, checkpoint_path=checkpoint, resume=True,
            request_interval_seconds=0, rate_limit_controller=restarted_controller,
            rate_limit_arm="no_rag",
        )
    assert resumed_provider.calls == []


def test_checkpoint_pins_schema_mode_rag_mode_and_benchmark_contents(tmp_path):
    checkpoint = tmp_path / "identity.json"
    case = _case("C-1")
    provider = CountingProvider()
    run_evaluation(provider=provider, mode="groq", cases=[case], max_cases=1,
                   model_id=provider.model, checkpoint_path=checkpoint,
                   request_interval_seconds=0)

    strict_changed = CountingProvider()
    strict_changed.structured_output_metadata = {
        **strict_changed.structured_output_metadata, "strict": False,
    }
    with pytest.raises(CheckpointMismatchError, match="structured_output"):
        run_evaluation(provider=strict_changed, mode="groq", cases=[case], max_cases=1,
                       model_id=strict_changed.model, checkpoint_path=checkpoint,
                       resume=True, request_interval_seconds=0)

    rag_provider = CountingProvider()
    with pytest.raises(CheckpointMismatchError, match="rag_enabled"):
        run_evaluation(provider=rag_provider, mode="groq", cases=[case], max_cases=1,
                       model_id=rag_provider.model, checkpoint_path=checkpoint,
                       resume=True, rag_enabled=True, retriever=lambda _code: [],
                       rag_snapshot_sha256="snapshot", request_interval_seconds=0)

    changed_case = {**case, "source_code": "value = 999\n"}
    with pytest.raises(CheckpointMismatchError, match="benchmark_sha256"):
        run_evaluation(provider=rag_provider, mode="groq", cases=[changed_case], max_cases=1,
                       model_id=rag_provider.model, checkpoint_path=checkpoint,
                       resume=True, request_interval_seconds=0)
    assert strict_changed.calls == []
    assert rag_provider.calls == []


def test_evaluation_identity_pins_prompt_scoring_and_sdk_retry_policy(tmp_path):
    checkpoint = tmp_path / "fingerprints.checkpoint.json"
    provider = CountingProvider()
    provider.client = type("Client", (), {"max_retries": 2})()
    report = run_evaluation(provider=provider, mode="groq", cases=[_case("C-1")], max_cases=1,
                            model_id=provider.model, checkpoint_path=checkpoint,
                            request_interval_seconds=0)
    config = json.loads(checkpoint.read_text())["config"]
    assert config["prompt_configuration_sha256"] == prompt_configuration_digest()
    assert config["scoring_configuration_sha256"] == scoring_configuration_digest()
    assert config["scoring_version"] == "exact_issue_type_line_tolerance_2_v1"
    assert config["provider_sdk_max_retries"] == 2
    assert report["run_duration_seconds"] >= 0


@pytest.mark.parametrize(
    ("status", "code", "expected_retry_exhausted"),
    [(400, "invalid_request", False), (429, "rate_limit", True)],
)
def test_http_errors_are_separate_from_schema_errors_and_sanitized(
    status, code, expected_retry_exhausted, tmp_path, monkeypatch
):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret")
    provider = CountingProvider({
        _case("C-1")["source_code"]: FakeHTTPError(
            status, code, "rejected Bearer abc gsk_test_secret"
        )
    })
    provider.client = type("Client", (), {"max_retries": 2})()
    report = run_evaluation(provider=provider, mode="groq", cases=[_case("C-1")],
                            max_cases=1, model_id=provider.model,
                            checkpoint_path=tmp_path / f"{status}.json",
                            request_interval_seconds=0)
    record = report["case_results"][0]
    assert record["provider_errors"][0]["http_status"] == status
    assert record["provider_errors"][0]["provider_error_code"] == code
    assert record["provider_errors"][0]["retry_exhausted"] is expected_retry_exhausted
    assert "gsk_test_secret" not in record["provider_errors"][0]["sanitized_message"]
    assert "Bearer abc" not in record["provider_errors"][0]["sanitized_message"]
    assert record["schema_errors"] == []
    assert "model_failure: FakeHTTPError" in record["processing_errors"]


def test_stop_on_provider_error_checkpoints_and_does_not_continue(tmp_path):
    cases = [_case("FIRST"), _case("SECOND"), _case("THIRD")]
    checkpoint = tmp_path / "stop-on-provider-error.json"
    provider = CountingProvider({
        cases[0]["source_code"]: FakeHTTPError(400, "invalid_request", "bad request")
    })

    report = run_evaluation(
        provider=provider, mode="groq", cases=cases, max_cases=3,
        model_id=provider.model, checkpoint_path=checkpoint,
        request_interval_seconds=0, stop_on_provider_error=True,
    )

    assert provider.calls == [cases[0]["source_code"]]
    assert report["stopped_on_provider_error"] is True
    assert report["failed_case_ids"] == ["FIRST"]
    saved = json.loads(checkpoint.read_text())
    assert saved["case_results"]["FIRST"]["status"] == "failed"
    assert "SECOND" not in saved["case_results"]
    assert "THIRD" not in saved["case_results"]


def test_individual_failed_case_retry_does_not_repeat_completed_case(tmp_path):
    cases = [_case("DONE"), _case("RETRY")]
    checkpoint = tmp_path / "individual.json"
    first = CountingProvider({_case("RETRY")["source_code"]:
                              FakeHTTPError(400, "bad_request", "invalid")})
    run_evaluation(provider=first, mode="groq", cases=cases, max_cases=2,
                   model_id=first.model, checkpoint_path=checkpoint,
                   request_interval_seconds=0)
    retry_provider = CountingProvider()
    report = run_evaluation(provider=retry_provider, mode="groq", cases=cases,
                            max_cases=2, model_id=retry_provider.model,
                            checkpoint_path=checkpoint, resume=True,
                            retry_case_ids=["RETRY"], request_interval_seconds=0)
    assert retry_provider.calls == [_case("RETRY")["source_code"]]
    assert [row["case_status"] for row in report["case_results"]] == ["completed", "completed"]

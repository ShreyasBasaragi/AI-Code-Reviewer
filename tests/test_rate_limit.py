import json

import pytest

from evaluation.rate_limit import GroqRateLimitController, RateLimitBudgetError, parse_duration


class FakeClock:
    def __init__(self):
        self.value = 1000.0
        self.waits = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.value += seconds


def _controller(tmp_path, clock, **kwargs):
    return GroqRateLimitController(
        state_path=tmp_path / "rate_limit_usage.json", arm="v2_baseline",
        remaining_daily_tokens=kwargs.pop("remaining_daily_tokens", 200000),
        clock=clock.now, sleeper=clock.sleep, **kwargs,
    )


def test_retry_after_headers_create_a_single_sdk_aware_cooldown(tmp_path):
    clock = FakeClock()
    controller = _controller(tmp_path, clock, request_interval_seconds=0)
    controller.before_request(1200, estimated_input_tokens=200, output_allowance=1000)
    controller.record_result(success=False, estimated_tokens=1200, error=RuntimeError("429"),
                             metadata={"http_status": 429, "sdk_retries": 2,
                                       "headers": {"retry-after": "7", "x-ratelimit-remaining-requests": "900"},
                                       "usage": {}})

    assert controller.state["http_429_count"] == 1
    assert controller.state["total_requests_attempted"] == 3
    assert controller.state["total_failed_requests"] == 3
    assert controller.state["cooldown_until"] == 1007.0
    assert controller.state["remaining_minute_requests"] == 900
    assert controller.state["remaining_daily_requests"] == 1000
    controller.before_request(1000)
    assert clock.waits == [7.0]


def test_retry_after_missing_uses_bounded_fallback(tmp_path):
    clock = FakeClock()
    controller = _controller(tmp_path, clock, request_interval_seconds=0)
    controller.before_request(1000)
    controller.record_result(success=False, estimated_tokens=1000, error=RuntimeError("429"),
                             metadata={"http_status": 429, "sdk_retries": 1,
                                       "headers": {}, "usage": {}})
    assert controller.state["cooldown_until"] == 1060.0
    controller.before_request(1000)
    assert clock.waits == [60.0]


def test_minute_token_window_waits_before_over_budget_request(tmp_path):
    clock = FakeClock()
    controller = _controller(tmp_path, clock, tpm=100, tpd=10000,
                             remaining_daily_tokens=10000, safety_fraction=0.8,
                             request_interval_seconds=0)
    controller.before_request(70)
    controller.record_result(success=True, estimated_tokens=70, metadata={
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        "headers": {"x-ratelimit-remaining-tokens": "10", "x-ratelimit-reset-tokens": "60s"},
    })
    controller.before_request(20)
    assert clock.waits == [60.0]


def test_provider_request_headers_are_treated_as_minute_quota(tmp_path):
    clock = FakeClock()
    controller = _controller(tmp_path, clock, request_interval_seconds=0)
    controller.before_request(1000)
    controller.record_result(success=True, estimated_tokens=1000, metadata={
        "usage": {"prompt_tokens": 300, "completion_tokens": 100, "total_tokens": 400},
        "headers": {"x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-reset-requests": "5s"},
    })

    assert controller.state["remaining_minute_requests"] == 0
    assert controller.state["remaining_daily_requests"] == 1000
    controller.before_request(1000)
    assert clock.waits == [5.0]
    assert controller.state["remaining_minute_requests"] is None


def test_daily_token_budget_stops_before_sending_excess_request(tmp_path):
    clock = FakeClock()
    controller = _controller(tmp_path, clock, tpm=1000, tpd=100,
                             remaining_daily_tokens=100, safety_fraction=0.8,
                             request_interval_seconds=0)
    controller.before_request(70)
    controller.record_result(success=True, estimated_tokens=70, metadata={
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        "headers": {},
    })
    with pytest.raises(RateLimitBudgetError, match="daily-token budget"):
        controller.before_request(20)
    assert controller.state["total_requests_attempted"] == 1


def test_checkpoint_ledger_survives_restart_and_prevents_duplicate_success_calls(tmp_path):
    clock = FakeClock()
    first = _controller(tmp_path, clock, request_interval_seconds=0)
    first.before_request(1000)
    first.record_result(success=True, estimated_tokens=1000, metadata={
        "usage": {"prompt_tokens": 400, "completion_tokens": 100, "total_tokens": 500},
        "headers": {}, "sdk_retries": 0,
    })
    second = _controller(tmp_path, clock, request_interval_seconds=0)
    assert second.state["total_successful_requests"] == 1
    assert second.state["total_tokens_accounted"] == 500
    assert second.arm_usage()["successful_requests"] == 1


def test_resume_refreshes_lower_dashboard_quota_without_restoring_spent_budget(tmp_path):
    clock = FakeClock()
    first = _controller(tmp_path, clock, remaining_daily_tokens=100000,
                        request_interval_seconds=0, tpd=200000)
    first.before_request(1000)
    first.record_result(success=True, estimated_tokens=1000, metadata={
        "usage": {"prompt_tokens": 700, "completion_tokens": 300, "total_tokens": 1000},
        "headers": {},
    })
    resumed = _controller(tmp_path, clock, remaining_daily_tokens=50000,
                           request_interval_seconds=0, tpd=200000)

    assert resumed.state["daily_window"]["starting_remaining_tpd"] == 100000
    assert resumed.state["daily_window"]["last_remaining_tpd_observed"] == 50000
    assert resumed.state["daily_window"]["tokens_accounted"] == 1000
    assert resumed.state["daily_window"]["daily_token_budget"] == 41000


def test_resume_requires_explicit_reset_when_dashboard_budget_increases(tmp_path):
    clock = FakeClock()
    first = _controller(tmp_path, clock, remaining_daily_tokens=50000, request_interval_seconds=0)
    first.before_request(1000)

    with pytest.raises(RateLimitBudgetError, match="increased"):
        _controller(tmp_path, clock, remaining_daily_tokens=100000,
                    request_interval_seconds=0)


def test_explicit_quota_reset_archives_prior_window_and_preserves_run_totals(tmp_path):
    clock = FakeClock()
    first = _controller(tmp_path, clock, request_interval_seconds=0)
    first.before_request(1000)
    first.record_result(success=True, estimated_tokens=1000, metadata={
        "usage": {"prompt_tokens": 400, "completion_tokens": 100, "total_tokens": 500},
        "headers": {},
    })
    reset = _controller(tmp_path, clock, request_interval_seconds=0,
                         remaining_daily_tokens=150000, confirm_daily_quota_reset=True)
    assert len(reset.state["daily_windows_history"]) == 1
    assert reset.state["daily_window"]["tokens_accounted"] == 0
    assert reset.state["total_tokens_accounted"] == 500
    assert reset.state["total_successful_requests"] == 1


@pytest.mark.parametrize(("value", "expected"), [("3", 3), ("2s", 2), ("1m30s", 90)])
def test_retry_and_reset_duration_parsing(value, expected):
    assert parse_duration(value, now=0) == expected

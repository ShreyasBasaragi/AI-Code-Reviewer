"""Conservative, resumable Groq quota pacing for bounded evaluations."""
from __future__ import annotations

import email.utils
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

RATE_STATE_VERSION = 1
RATE_HEADERS = (
    "retry-after", "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
)


class RateLimitBudgetError(RuntimeError):
    """Raised before sending a request that cannot fit the configured budget."""


def estimate_tokens(text: str, *, output_allowance: int = 1024) -> dict[str, Any]:
    """Conservative fallback: UTF-8 bytes / 2, plus reserved completion tokens.

    This is a budgeting estimate, not a tokenizer result. The factor intentionally
    overestimates ordinary English/code compared with common tokenizers.
    """
    input_estimate = math.ceil(len(text.encode("utf-8")) / 2)
    return {"input_tokens_estimated": input_estimate,
            "output_tokens_reserved": output_allowance,
            "total_tokens_reserved": input_estimate + output_allowance,
            "method": "utf8_bytes_divided_by_2_plus_output_allowance; estimate_not_exact"}


def _header_map(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {}
    wanted = {name.lower() for name in RATE_HEADERS}
    return {str(key).lower(): str(value) for key, value in items if str(key).lower() in wanted}


def parse_duration(value: Any, *, now: float | None = None) -> float | None:
    """Parse Retry-After/reset values as seconds, duration text, or HTTP date."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    current = time.time() if now is None else now
    try:
        parsed_date = email.utils.parsedate_to_datetime(text)
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        return max(0.0, parsed_date.timestamp() - current)
    except (TypeError, ValueError, OverflowError):
        pass
    matches = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", text.lower())
    if not matches:
        return None
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(float(amount) * scale[unit] for amount, unit in matches)


def response_metadata(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    def token_value(name: str):
        value = getattr(usage, name, None) if usage is not None else None
        if value is None and isinstance(usage, Mapping):
            value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    return {"usage": {"prompt_tokens": token_value("prompt_tokens"),
                      "completion_tokens": token_value("completion_tokens"),
                      "total_tokens": token_value("total_tokens")},
            "headers": {}, "sdk_retries": None}


def exception_metadata(exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    headers = _header_map(getattr(response, "headers", None) or getattr(exc, "headers", None))
    usage = getattr(exc, "usage", None)
    if usage is None and isinstance(getattr(exc, "body", None), Mapping):
        usage = exc.body.get("usage")
    if isinstance(usage, Mapping):
        normalized_usage = {name: usage.get(name) for name in
                            ("prompt_tokens", "completion_tokens", "total_tokens")}
    else:
        normalized_usage = {name: getattr(usage, name, None) if usage else None for name in
                            ("prompt_tokens", "completion_tokens", "total_tokens")}
    retry_after = parse_duration(headers.get("retry-after"))
    retryable = getattr(exc, "status_code", None) == 429
    return {"headers": headers, "usage": normalized_usage,
            "sdk_retries": getattr(exc, "retries_taken", None),
            "retry_after_seconds": retry_after, "http_status": getattr(exc, "status_code", None),
            "retry_exhausted": bool(retryable)}


class GroqRateLimitController:
    """Shared, atomic account ledger with rolling TPM and daily quota gates."""

    def __init__(self, *, state_path: Path, arm: str, remaining_daily_tokens: int,
                 rpm: int = 30, rpd: int = 1000, tpm: int = 8000, tpd: int = 200000,
                 request_interval_seconds: float = 20.0, safety_fraction: float = 0.8,
                 max_wait_seconds: float = 3600.0,
                 confirm_daily_quota_reset: bool = False,
                 clock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep,
                 state: dict[str, Any] | None = None) -> None:
        if min(rpm, rpd, tpm, tpd, remaining_daily_tokens) <= 0:
            raise ValueError("Groq limits and remaining daily tokens must be positive")
        if not 0 < safety_fraction <= 1 or request_interval_seconds < 0 or max_wait_seconds < 0:
            raise ValueError("invalid pacing or safety settings")
        self.state_path, self.arm = state_path, arm
        self.limits = {"rpm": rpm, "rpd": rpd, "tpm": tpm, "tpd": tpd,
                       "starting_remaining_tpd": remaining_daily_tokens,
                       "safety_fraction": safety_fraction,
                       "request_interval_seconds": request_interval_seconds,
                       "max_wait_seconds": max_wait_seconds}
        policy = {key: value for key, value in self.limits.items()
                  if key != "starting_remaining_tpd"}
        self.policy = policy
        self.clock, self.sleeper = clock, sleeper
        effective_tpd = min(tpd, remaining_daily_tokens)
        self.daily_budget_tokens = int(effective_tpd * safety_fraction)
        if state is None and state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        if state is not None:
            if state.get("version") != RATE_STATE_VERSION or state.get("policy") != policy:
                raise RateLimitBudgetError("rate-limit ledger policy differs; refusing unsafe resume")
            self.state = state
            stored_remaining = int(self.state.get("daily_window", {}).get(
                "starting_remaining_tpd", remaining_daily_tokens))
            if confirm_daily_quota_reset:
                history = self.state.setdefault("daily_windows_history", [])
                history.append(dict(self.state.get("daily_window", {})))
                self.state["daily_window"] = {
                    "starting_remaining_tpd": remaining_daily_tokens,
                    "last_remaining_tpd_observed": remaining_daily_tokens,
                    "daily_token_budget": int(min(tpd, remaining_daily_tokens) * safety_fraction),
                    "tokens_accounted": 0, "requests_attempted": 0,
                    "reset_confirmed_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                self.state["remaining_daily_requests"] = rpd
                self.state["remaining_minute_requests"] = None
                self.state["reset_requests_at"] = None
                self.state["reset_minute_requests_at"] = None
                self.state["remaining_minute_tokens"] = None
                self.state["reset_tokens_at"] = None
                self.state["cooldown_until"] = None
                self._save()
            else:
                daily_window = self.state["daily_window"]
                last_observed = int(daily_window.get("last_remaining_tpd_observed", stored_remaining))
                if remaining_daily_tokens > last_observed:
                    raise RateLimitBudgetError(
                        "remaining daily-token budget increased since the saved observation; explicit confirmed-reset mode is required"
                    )
                # A lower dashboard value refreshes the remaining budget. The
                # local cumulative ceiling also prevents an old/stale value
                # from restoring quota already consumed by this evaluation.
                accounted = int(daily_window.get("tokens_accounted", 0))
                local_remaining = max(0, int(daily_window["daily_token_budget"]) - accounted)
                dashboard_remaining = int(min(tpd, remaining_daily_tokens) * safety_fraction)
                daily_window["daily_token_budget"] = accounted + min(
                    local_remaining, dashboard_remaining
                )
                daily_window["last_remaining_tpd_observed"] = remaining_daily_tokens
                self._save()
        else:
            self.state = {"version": RATE_STATE_VERSION, "policy": policy,
                          "accounting_note": "daily tokens include exact reported usage or conservative reservation estimates; external use can only be reconciled by a new provider header/dashboard value",
                          "total_requests_attempted": 0, "total_successful_requests": 0,
                          "total_failed_requests": 0, "total_retries": 0,
                          "total_input_tokens": 0, "total_output_tokens": 0,
                          "total_tokens_reported": 0, "total_tokens_accounted": 0,
                          "total_rate_limit_wait_seconds": 0.0,
                          "http_429_count": 0, "remaining_daily_requests": rpd,
                          "remaining_minute_requests": None,
                          "remaining_minute_tokens": None, "reset_requests_at": None,
                          "reset_minute_requests_at": None, "reset_tokens_at": None,
                          "cooldown_until": None,
                          "last_request_at": None, "minute_events": [], "per_arm": {}}
            self.state["daily_windows_history"] = []
            self.state["daily_window"] = {
                "starting_remaining_tpd": remaining_daily_tokens,
                "last_remaining_tpd_observed": remaining_daily_tokens,
                "daily_token_budget": self.daily_budget_tokens,
                "tokens_accounted": 0, "requests_attempted": 0,
                "reset_confirmed_at_utc": None,
            }
        self._current_reservation: dict[str, Any] | None = None
        self.state.setdefault("per_arm", {}).setdefault(arm, self._empty_arm())
        # Persist the initialized/resumed ledger before any quota decision can
        # block a first request and leave a checkpoint without recoverable state.
        self._save()

    @staticmethod
    def _empty_arm() -> dict[str, Any]:
        return {"requests_attempted": 0, "successful_requests": 0, "failed_requests": 0,
                "retries": 0, "input_tokens_estimated": 0, "output_tokens_reserved": 0,
                "input_tokens_reported": 0, "output_tokens_reported": 0,
                "tokens_accounted": 0, "rate_limit_wait_seconds": 0.0,
                "http_429_count": 0, "rate_headers_observed": {}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state_path.parent,
                                         prefix=f".{self.state_path.name}.", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(self.state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, self.state_path)

    def _prune_events(self, now: float) -> None:
        self.state["minute_events"] = [event for event in self.state["minute_events"]
                                       if now - float(event["time"]) < 60]

    def _sleep(self, delay: float) -> None:
        if delay <= 0:
            return
        if delay > self.limits["max_wait_seconds"]:
            raise RateLimitBudgetError(
                f"required quota wait ({delay:.1f}s) exceeds configured maximum; resume after quota recovery"
            )
        self.sleeper(delay)
        self.state["total_rate_limit_wait_seconds"] += delay
        self.state["per_arm"][self.arm]["rate_limit_wait_seconds"] += delay

    def before_request(self, estimated_tokens: int, *, estimated_input_tokens: int | None = None,
                       output_allowance: int | None = None) -> float:
        if estimated_tokens <= 0:
            raise ValueError("estimated request token demand must be positive")
        if estimated_tokens > int(self.limits["tpm"] * self.limits["safety_fraction"]):
            raise RateLimitBudgetError("estimated request exceeds the conservative minute-token budget; no request sent")
        now = self.clock()
        self._prune_events(now)
        daily_window = self.state["daily_window"]
        daily_token_budget = int(daily_window["daily_token_budget"])
        if int(daily_window["tokens_accounted"]) + estimated_tokens > daily_token_budget:
            raise RateLimitBudgetError("projected request exceeds the conservative remaining daily-token budget; no request sent")
        if int(daily_window["requests_attempted"]) >= self.limits["rpd"]:
            raise RateLimitBudgetError("configured daily request limit reached; no request sent")
        remaining_daily = self.state.get("remaining_daily_requests")
        reset_requests_at = self.state.get("reset_requests_at")
        if isinstance(remaining_daily, int) and remaining_daily <= 0:
            if not reset_requests_at or reset_requests_at <= now:
                raise RateLimitBudgetError("provider reports no daily requests remaining; no request sent")
        delay = 0.0
        if self.state.get("last_request_at") is not None:
            delay = max(delay, self.limits["request_interval_seconds"] - (now - self.state["last_request_at"]))
        cooldown = self.state.get("cooldown_until")
        if cooldown is not None:
            delay = max(delay, float(cooldown) - now)
        if isinstance(reset_requests_at, (int, float)) and isinstance(remaining_daily, int) and remaining_daily <= 0:
            delay = max(delay, float(reset_requests_at) - now)
        remaining_minute_requests = self.state.get("remaining_minute_requests")
        reset_minute_requests_at = self.state.get("reset_minute_requests_at")
        if isinstance(remaining_minute_requests, int) and remaining_minute_requests <= 0:
            if isinstance(reset_minute_requests_at, (int, float)) and reset_minute_requests_at > now:
                delay = max(delay, float(reset_minute_requests_at) - now)
            else:
                raise RateLimitBudgetError(
                    "provider reports no minute requests remaining and supplied no future reset; no request sent"
                )

        reserved = sum(int(event["tokens"]) for event in self.state["minute_events"])
        if len(self.state["minute_events"]) >= self.limits["rpm"]:
            delay = max(delay, max(0.0, float(self.state["minute_events"][0]["time"]) + 60 - now))
        token_budget = int(self.limits["tpm"] * self.limits["safety_fraction"])
        remote_remaining = self.state.get("remaining_minute_tokens")
        reset_tokens_at = self.state.get("reset_tokens_at")
        if isinstance(reset_tokens_at, (int, float)) and reset_tokens_at <= now:
            self.state["remaining_minute_tokens"] = None
            remote_remaining = None
        available = token_budget - reserved
        if isinstance(remote_remaining, int):
            available = min(available, remote_remaining)
        if estimated_tokens > available:
            if self.state["minute_events"]:
                delay = max(delay, max(0.0, float(self.state["minute_events"][0]["time"]) + 60 - now))
            elif isinstance(reset_tokens_at, (int, float)) and reset_tokens_at > now:
                delay = max(delay, reset_tokens_at - now)
            else:
                raise RateLimitBudgetError("provider token budget is insufficient and no reset time is available")
        self._sleep(max(0.0, delay))
        now = self.clock()
        if isinstance(self.state.get("reset_minute_requests_at"), (int, float)) \
                and self.state["reset_minute_requests_at"] <= now:
            self.state["remaining_minute_requests"] = None
            self.state["reset_minute_requests_at"] = None
        remote_requests = self.state.get("remaining_minute_requests")
        if isinstance(remote_requests, int) and remote_requests > 0:
            self.state["remaining_minute_requests"] = remote_requests - 1
        self._prune_events(now)
        reservation = {"time": now, "tokens": estimated_tokens, "arm": self.arm, "outcome": "pending"}
        self.state["minute_events"].append(reservation)
        self.state["last_request_at"] = now
        self.state["total_requests_attempted"] += 1
        self.state["per_arm"][self.arm]["requests_attempted"] += 1
        arm = self.state["per_arm"][self.arm]
        arm["input_tokens_estimated"] += int(estimated_input_tokens or estimated_tokens)
        arm["output_tokens_reserved"] += int(output_allowance or 0)
        self.state["total_tokens_accounted"] += estimated_tokens
        daily_window["tokens_accounted"] += estimated_tokens
        daily_window["requests_attempted"] += 1
        self.state["per_arm"][self.arm]["tokens_accounted"] += estimated_tokens
        self._current_reservation = reservation
        self._save()
        return max(0.0, delay)

    def _update_headers(self, headers: Any) -> dict[str, str]:
        normalized = _header_map(headers)
        now = self.clock()
        for header, target in (("x-ratelimit-remaining-requests", "remaining_minute_requests"),
                               ("x-ratelimit-remaining-tokens", "remaining_minute_tokens")):
            try:
                self.state[target] = int(normalized[header])
            except (KeyError, TypeError, ValueError):
                pass
        for header, target in (("x-ratelimit-reset-requests", "reset_minute_requests_at"),
                               ("x-ratelimit-reset-tokens", "reset_tokens_at")):
            duration = parse_duration(normalized.get(header), now=now)
            if duration is not None:
                self.state[target] = now + duration
        self.state["per_arm"][self.arm]["rate_headers_observed"].update(normalized)
        return normalized

    def record_result(self, *, success: bool, metadata: Mapping[str, Any] | None,
                      estimated_tokens: int, error: BaseException | None = None) -> None:
        metadata = metadata or {}
        headers = self._update_headers(metadata.get("headers"))
        usage = metadata.get("usage") or {}
        input_tokens = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
        output_tokens = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
        total_tokens = usage.get("total_tokens") if isinstance(usage, Mapping) else None
        valid_usage = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                          for value in (input_tokens, output_tokens))
        accounted = total_tokens if isinstance(total_tokens, int) and total_tokens >= 0 else (
            input_tokens + output_tokens if valid_usage else estimated_tokens
        )
        if self._current_reservation is not None:
            old = int(self._current_reservation["tokens"])
            self._current_reservation["tokens"] = int(accounted)
            self._current_reservation["outcome"] = "success" if success else "failed"
            self.state["total_tokens_accounted"] += int(accounted) - old
            self.state["daily_window"]["tokens_accounted"] += int(accounted) - old
            arm = self.state["per_arm"][self.arm]
            arm["tokens_accounted"] += int(accounted) - old
            self._current_reservation = None
        retries = metadata.get("sdk_retries")
        if not isinstance(retries, int) or retries < 0:
            status = getattr(error, "status_code", None) if error else None
            retries = (int(getattr(getattr(error, "response", None), "retries_taken", 0) or 0)
                       if error else 0)
            if error and status == 429:
                retries = max(retries, int(getattr(getattr(error, "request", None), "max_retries", 0) or 0))
        request_attempts = 1 + retries
        self.state["total_requests_attempted"] += retries
        self.state["daily_window"]["requests_attempted"] += retries
        self.state["per_arm"][self.arm]["requests_attempted"] += retries
        self.state["total_retries"] += retries
        arm = self.state["per_arm"][self.arm]
        arm["retries"] += retries
        if retries:
            retry_time = self.clock()
            for _ in range(retries):
                self.state["minute_events"].append({
                    "time": retry_time, "tokens": estimated_tokens, "arm": self.arm,
                    "outcome": "sdk_retry_estimate",
                })
                self.state["total_tokens_accounted"] += estimated_tokens
                self.state["daily_window"]["tokens_accounted"] += estimated_tokens
                arm["tokens_accounted"] += estimated_tokens
            self.state["last_request_at"] = retry_time
        if success:
            self.state["total_successful_requests"] += 1
            self.state["total_failed_requests"] += retries
            arm["successful_requests"] += 1
            arm["failed_requests"] += retries
        else:
            self.state["total_failed_requests"] += request_attempts
            arm["failed_requests"] += request_attempts
        if valid_usage:
            self.state["total_input_tokens"] += input_tokens
            self.state["total_output_tokens"] += output_tokens
            self.state["total_tokens_reported"] += accounted
            arm["input_tokens_reported"] += input_tokens
            arm["output_tokens_reported"] += output_tokens
        response_status = metadata.get("http_status")
        if response_status is None and error is not None:
            response_status = getattr(error, "status_code", None)
        if not success and response_status == 429:
            self.state["http_429_count"] += 1
            arm["http_429_count"] += 1
            retry_after = parse_duration(headers.get("retry-after"), now=self.clock())
            if retry_after is None:
                retry_after = 60.0
            self.state["cooldown_until"] = self.clock() + retry_after
        self._save()

    def arm_usage(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.state["per_arm"][self.arm]))

    def state_snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.state))

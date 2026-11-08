from datetime import timedelta
from typing import Any

import httpx
import pytest
from langchain_core.exceptions import ModelAPIError, ModelRateLimitError
from structlog.testing import capture_logs
from support.llm_rows import NOW

from deliverius.llm.errors import ProviderPausedError
from deliverius.llm.gate import (
    DEFAULT_PAUSE_SECONDS,
    MAX_PAUSE_SECONDS,
    MAX_WAIT_SECONDS,
    ProviderGate,
    pause_seconds_from,
)
from deliverius.shared.clock import FixedClock
from deliverius.shared.model_ref import LlmProvider


class FakeResponse:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = httpx.Headers(headers)


class SdkError(Exception):
    """Stands in for the SDK error that carries the HTTP response or the JSON details."""

    def __init__(self, response: Any = None, details: Any = None) -> None:
        super().__init__("sdk error")
        self.response = response
        self.details = details


def rate_limit(retry_after: str | None = None, **sdk: Any) -> ModelRateLimitError:
    """A `429` like the integrations raise: ours on the outside, the SDK error as its cause."""
    error = ModelRateLimitError("rate limited")
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    error.response = FakeResponse(headers) if retry_after is not None else None  # type: ignore[attr-defined]
    if sdk:
        error.__cause__ = SdkError(**sdk)
    return error


class Sleeper:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))


def make_gate() -> tuple[ProviderGate, FixedClock, Sleeper]:
    clock = FixedClock(NOW)
    sleeper = Sleeper(clock)
    return ProviderGate(LlmProvider.GROQ, clock, sleeper), clock, sleeper


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("60", 60.0),
        ("1.5", 1.5),
        (" 7 ", 7.0),
        ("0", 0.0),
        ("3600", 3_600.0),
        ("3601", MAX_PAUSE_SECONDS),
        ("999999999", MAX_PAUSE_SECONDS),
    ],
)
def test_the_pause_follows_the_retry_after_header_up_to_one_hour(
    header: str, expected: float
) -> None:
    assert pause_seconds_from(rate_limit(header)) == expected


@pytest.mark.parametrize(
    "header",
    ["-1", "-0.5", "abc", "", "Wed, 21 Oct 2026 07:28:00 GMT", "nan", "inf", "-inf", "6o"],
)
def test_an_unreadable_negative_or_date_retry_after_falls_back_to_five_seconds(
    header: str,
) -> None:
    assert pause_seconds_from(rate_limit(header)) == DEFAULT_PAUSE_SECONDS == 5.0


def test_a_429_without_any_hint_pauses_five_seconds() -> None:
    assert pause_seconds_from(rate_limit()) == 5.0


@pytest.mark.parametrize(
    ("delay", "expected"),
    [("37s", 37.0), ("0.5s", 0.5), ("60", 60.0), (12, 12.0)],
)
def test_the_pause_follows_the_gemini_retry_delay_in_the_sdk_error_details(
    delay: object, expected: float
) -> None:
    details = {
        "error": {
            "code": 429,
            "details": [
                {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
            ],
        }
    }

    assert pause_seconds_from(rate_limit(details=details)) == expected


def test_a_gemini_retry_delay_that_cannot_be_read_falls_back_to_five_seconds() -> None:
    details = {"error": {"details": [{"retryDelay": "soon"}]}}

    assert pause_seconds_from(rate_limit(details=details)) == 5.0


def test_a_header_on_the_sdk_error_is_found_through_the_cause_chain() -> None:
    error = rate_limit(response=FakeResponse({"retry-after": "30"}))

    assert pause_seconds_from(error) == 30.0


def test_a_cyclic_cause_chain_does_not_loop_forever() -> None:
    error = rate_limit()
    error.__cause__ = error

    assert pause_seconds_from(error) == 5.0


async def test_a_provider_that_was_never_limited_is_not_paused() -> None:
    gate, _, sleeper = make_gate()

    await gate.wait_if_paused()

    assert sleeper.slept == []


async def test_a_429_with_a_long_ask_refuses_the_calls_until_the_clock_moves() -> None:
    gate, clock, sleeper = make_gate()
    gate.note_error(rate_limit("60"))

    with pytest.raises(ProviderPausedError) as refused:
        await gate.wait_if_paused()
    assert refused.value.seconds_left == 60.0
    assert refused.value.provider is LlmProvider.GROQ

    clock.advance(timedelta(seconds=57))
    with pytest.raises(ProviderPausedError):
        await gate.wait_if_paused()

    clock.advance(timedelta(seconds=3))
    await gate.wait_if_paused()
    assert sleeper.slept == []


async def test_a_wait_of_exactly_two_seconds_is_slept_and_the_call_then_goes_out() -> None:
    gate, clock, sleeper = make_gate()
    gate.pause_for(MAX_WAIT_SECONDS)

    await gate.wait_if_paused()

    assert sleeper.slept == [2.0]
    await gate.wait_if_paused()
    assert sleeper.slept == [2.0]
    assert clock.now() == NOW + timedelta(seconds=2)


async def test_a_wait_just_above_two_seconds_is_refused_instead_of_slept() -> None:
    gate, _, sleeper = make_gate()
    gate.pause_for(2.001)

    with pytest.raises(ProviderPausedError):
        await gate.wait_if_paused()

    assert sleeper.slept == []


async def test_what_is_left_of_the_pause_is_what_is_slept() -> None:
    gate, clock, sleeper = make_gate()
    gate.pause_for(10)
    clock.advance(timedelta(seconds=8.5))

    await gate.wait_if_paused()

    assert sleeper.slept == [1.5]


async def test_the_ceiling_of_one_hour_holds_on_the_gate() -> None:
    gate, clock, _ = make_gate()
    gate.note_error(rate_limit("86400"))

    clock.advance(timedelta(seconds=3_597))
    with pytest.raises(ProviderPausedError):
        await gate.wait_if_paused()
    clock.advance(timedelta(seconds=3))
    await gate.wait_if_paused()


async def test_a_shorter_ask_does_not_cut_a_running_pause_short() -> None:
    gate, _, _ = make_gate()
    gate.note_error(rate_limit("60"))
    gate.note_error(rate_limit("5"))

    with pytest.raises(ProviderPausedError) as refused:
        await gate.wait_if_paused()

    assert refused.value.seconds_left == 60.0


async def test_errors_that_are_not_a_429_do_not_open_a_pause() -> None:
    gate, _, _ = make_gate()

    gate.note_error(ModelAPIError("server"))
    gate.note_error(ConnectionError("down"))
    gate.note_error(KeyboardInterrupt())

    await gate.wait_if_paused()


def test_opening_a_pause_is_logged_without_the_error_text() -> None:
    gate, _, _ = make_gate()

    with capture_logs() as logs:
        gate.note_error(rate_limit("60"))

    assert logs == [
        {
            "event": "llm.provider_paused",
            "log_level": "warning",
            "provider": LlmProvider.GROQ,
            "state": "opened",
            "seconds": 60.0,
        }
    ]


def test_the_seconds_left_follow_the_clock_and_stop_at_zero() -> None:
    gate, clock, _ = make_gate()
    assert gate.seconds_left() == 0.0

    gate.pause_for(30)
    assert gate.seconds_left() == 30.0

    clock.advance(timedelta(seconds=12))
    assert gate.seconds_left() == 18.0

    clock.advance(timedelta(seconds=60))
    assert gate.seconds_left() == 0.0

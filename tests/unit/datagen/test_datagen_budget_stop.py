"""The quota runs out in the middle of a run: stop clean, say what is missing, resume cheaply."""

from collections import Counter
from pathlib import Path

import pytest
from structlog.testing import capture_logs
from support.datagen_configs import MESSAGES_CONFIG
from support.datagen_llm import Answer, DatagenHarness, harness, structured, valid_variations
from support.llm_runtime import settings_with

from deliverius.datagen import (
    BudgetExhaustedError,
    MessageGeneration,
    generate_message_scenarios,
    generate_messages,
)
from deliverius.datagen.messages import render_scenario
from deliverius.llm.call import DegradedReason
from deliverius.llm.errors import BudgetRefusal, LlmBudgetExceededError, ProviderPausedError
from deliverius.llm.fake import Fail
from deliverius.llm.ports import CallStatus
from deliverius.shared.model_ref import LlmProvider

CONFIG = MESSAGES_CONFIG
SCENARIOS = generate_message_scenarios(CONFIG)
HUMANS = {render_scenario(s): s for s in SCENARIOS}
ONE_AT_A_TIME = CONFIG.model_copy(
    update={"messages": CONFIG.messages.model_copy(update={"concurrency": 1})}
)


def valid(human: str) -> Answer:
    return structured({"variations": valid_variations(HUMANS[human])})


def stop_after(count: int, error: Exception, cache: Path) -> DatagenHarness:
    """A harness that answers `count` calls and then fails with `error` on every other call."""
    calls: Counter[str] = Counter()

    def respond(human: str) -> Answer:
        calls["total"] += 1
        if calls["total"] > count:
            return 0.0, Fail(error)
        return valid(human)

    return harness(cache, respond)


async def test_the_daily_budget_stops_the_run_and_the_pending_scenarios_never_start(
    tmp_path: Path,
) -> None:
    h = harness(
        tmp_path / "unused.sqlite", valid, settings=settings_with(llm_gemini_daily_requests=3)
    )

    with capture_logs() as logs, pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(SCENARIOS, h.uncached_models(), ONE_AT_A_TIME)

    assert stopped.value.reason is DegradedReason.BUDGET_EXHAUSTED
    assert stopped.value.remaining == len(SCENARIOS) - 3
    assert h.gemini.tracker.calls == 3
    assert [call.status for call in h.store.calls] == [CallStatus.OK] * 3
    [event] = [e for e in logs if e["event"] == "datagen.budget_exhausted"]
    assert event["remaining"] == len(SCENARIOS) - 3
    assert event["reason"] == DegradedReason.BUDGET_EXHAUSTED


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            LlmBudgetExceededError(LlmProvider.GEMINI, BudgetRefusal.EXHAUSTED),
            DegradedReason.BUDGET_EXHAUSTED,
        ),
        (
            LlmBudgetExceededError(LlmProvider.GEMINI, BudgetRefusal.DATABASE_UNAVAILABLE),
            DegradedReason.BUDGET_UNAVAILABLE,
        ),
        (ProviderPausedError(LlmProvider.GEMINI, 30.0), DegradedReason.PROVIDER_PAUSED),
    ],
)
async def test_each_budget_or_pause_degradation_stops_the_run(
    tmp_path: Path, error: Exception, reason: DegradedReason
) -> None:
    h = stop_after(2, error, tmp_path / "cache.sqlite")

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(SCENARIOS, h.models(), ONE_AT_A_TIME)

    assert stopped.value.reason is reason
    assert stopped.value.remaining == len(SCENARIOS) - 2
    assert h.gemini.tracker.calls == 3


async def test_the_calls_in_flight_finish_and_only_the_pending_ones_are_dropped(
    tmp_path: Path,
) -> None:
    first = render_scenario(SCENARIOS[0])
    refusal = LlmBudgetExceededError(LlmProvider.GEMINI, BudgetRefusal.EXHAUSTED)

    def respond(human: str) -> Answer:
        if human == first:
            return 0.0, Fail(refusal)
        return 0.05, valid(human)[1]

    h = harness(tmp_path / "cache.sqlite", respond)

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(SCENARIOS, h.models(), CONFIG)

    in_flight = CONFIG.messages.concurrency
    assert h.gemini.tracker.calls == in_flight
    assert stopped.value.remaining == len(SCENARIOS) - (in_flight - 1)


async def test_a_run_that_is_not_stopped_returns_normally(tmp_path: Path) -> None:
    h = harness(tmp_path / "cache.sqlite", valid)

    result = await generate_messages(SCENARIOS, h.models(), CONFIG)

    assert len(result.messages) == len(SCENARIOS) * CONFIG.messages.variations_per_scenario


async def run_to_completion(cache: Path) -> tuple[MessageGeneration, DatagenHarness]:
    h = harness(cache, valid)
    return await generate_messages(SCENARIOS, h.models(), ONE_AT_A_TIME), h


async def test_resuming_with_the_same_cache_calls_the_model_only_for_what_is_missing(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "shared.sqlite"
    done = 8
    h_stopped = stop_after(done, ProviderPausedError(LlmProvider.GEMINI, 60.0), cache)
    with pytest.raises(BudgetExhaustedError):
        await generate_messages(SCENARIOS, h_stopped.models(), ONE_AT_A_TIME)

    resumed, h_resumed = await run_to_completion(cache)
    fresh, _ = await run_to_completion(tmp_path / "fresh.sqlite")

    assert h_resumed.gemini.tracker.calls == len(SCENARIOS) - done
    assert len(h_resumed.store.calls) == len(SCENARIOS) - done
    assert resumed == fresh


async def test_a_second_run_over_a_finished_cache_makes_no_call_and_gives_the_same_result(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "shared.sqlite"
    first, _ = await run_to_completion(cache)

    second, h_second = await run_to_completion(cache)

    assert h_second.gemini.tracker.calls == 0
    assert h_second.store.calls == []
    assert second == first

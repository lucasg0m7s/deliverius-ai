"""With the response cache on, the governor only counts: the generator checks the budget itself."""

from pathlib import Path

import pytest
from structlog.testing import capture_logs
from support.datagen_configs import MESSAGES_CONFIG
from support.datagen_llm import Answer, harness, structured, valid_variations
from support.llm_doubles import StoreDown
from support.llm_rows import TODAY
from support.llm_runtime import ScriptedProvider, scripted_runtime, settings_with

from deliverius.datagen import (
    BudgetExhaustedError,
    generate_holdout_messages,
    generate_message_scenarios,
    generate_messages,
    generate_rag_pairs,
)
from deliverius.datagen.budget import BudgetGuard, budget_guard
from deliverius.datagen.messages import render_scenario
from deliverius.llm.call import DegradedReason
from deliverius.llm.ports import BudgetLimit, CallStatus
from deliverius.llm.roles import ModelRole
from deliverius.shared.model_ref import LlmProvider

CONFIG = MESSAGES_CONFIG
SCENARIOS = generate_message_scenarios(CONFIG)
HUMANS = {render_scenario(s): s for s in SCENARIOS}
ONE_AT_A_TIME = CONFIG.model_copy(
    update={"messages": CONFIG.messages.model_copy(update={"concurrency": 1})}
)
FORTY_PERCENT = len(SCENARIOS) * 4 // 10
TOKENS_PER_CALL = 1_200


def valid(human: str) -> Answer:
    return structured({"variations": valid_variations(HUMANS[human])})


def always(reason: DegradedReason | None) -> BudgetGuard:
    async def guard() -> DegradedReason | None:
        return reason

    return guard


async def test_the_budget_ends_after_forty_percent_of_the_scenarios_with_the_cache_on(
    tmp_path: Path,
) -> None:
    h = harness(
        tmp_path / "cache.sqlite",
        valid,
        settings=settings_with(llm_gemini_daily_requests=FORTY_PERCENT),
    )

    with capture_logs() as logs, pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(
            SCENARIOS,
            h.models(),
            ONE_AT_A_TIME,
            budget_guard=budget_guard(h.runtime, ModelRole.DATAGEN),
        )

    assert stopped.value.reason is DegradedReason.BUDGET_EXHAUSTED
    assert stopped.value.remaining == len(SCENARIOS) - FORTY_PERCENT
    assert h.gemini.tracker.calls == FORTY_PERCENT
    assert [call.status for call in h.store.calls] == [CallStatus.OK] * FORTY_PERCENT
    [event] = [e for e in logs if e["event"] == "datagen.budget_exhausted"]
    assert event["remaining"] == len(SCENARIOS) - FORTY_PERCENT


async def test_a_spent_token_ceiling_stops_the_run_with_requests_to_spare(tmp_path: Path) -> None:
    h = harness(
        tmp_path / "cache.sqlite",
        valid,
        settings=settings_with(llm_gemini_daily_tokens=2 * TOKENS_PER_CALL),
    )

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(
            SCENARIOS,
            h.models(),
            ONE_AT_A_TIME,
            budget_guard=budget_guard(h.runtime, ModelRole.DATAGEN),
        )

    assert h.gemini.tracker.calls == 2
    assert stopped.value.remaining == len(SCENARIOS) - 2


async def test_without_the_guard_the_cache_lets_the_run_pass_the_ceiling(tmp_path: Path) -> None:
    h = harness(
        tmp_path / "cache.sqlite",
        valid,
        settings=settings_with(llm_gemini_daily_requests=FORTY_PERCENT),
    )

    result = await generate_messages(SCENARIOS, h.models(), ONE_AT_A_TIME)

    assert h.gemini.tracker.calls == len(SCENARIOS)
    assert result.failed_scenarios == ()


async def test_the_next_day_resumes_from_the_cache_and_pays_only_for_what_is_missing(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "shared.sqlite"
    first_day = harness(
        cache, valid, settings=settings_with(llm_gemini_daily_requests=FORTY_PERCENT)
    )
    with pytest.raises(BudgetExhaustedError):
        await generate_messages(
            SCENARIOS,
            first_day.models(),
            ONE_AT_A_TIME,
            budget_guard=budget_guard(first_day.runtime, ModelRole.DATAGEN),
        )

    second_day = harness(cache, valid)
    resumed = await generate_messages(
        SCENARIOS,
        second_day.models(),
        ONE_AT_A_TIME,
        budget_guard=budget_guard(second_day.runtime, ModelRole.DATAGEN),
    )
    fresh = harness(tmp_path / "fresh.sqlite", valid)
    expected = await generate_messages(SCENARIOS, fresh.models(), ONE_AT_A_TIME)

    assert second_day.gemini.tracker.calls == len(SCENARIOS) - FORTY_PERCENT
    assert resumed == expected


async def test_an_unreachable_budget_store_stops_before_any_call() -> None:
    runtime, _, _ = scripted_runtime(
        settings_with(), {LlmProvider.GEMINI: ScriptedProvider([])}, store=StoreDown()
    )

    assert await budget_guard(runtime, ModelRole.DATAGEN)() is DegradedReason.BUDGET_UNAVAILABLE


async def test_a_store_with_room_lets_the_job_start() -> None:
    runtime, store, _ = scripted_runtime(
        settings_with(llm_gemini_daily_requests=3), {LlmProvider.GEMINI: ScriptedProvider([])}
    )
    await store.reserve(LlmProvider.GEMINI, TODAY, BudgetLimit(2, None))

    assert await budget_guard(runtime, ModelRole.DATAGEN)() is None


@pytest.mark.parametrize(
    "reason", [DegradedReason.BUDGET_EXHAUSTED, DegradedReason.BUDGET_UNAVAILABLE]
)
async def test_every_generation_stops_before_its_first_call_when_the_guard_says_so(
    tmp_path: Path, reason: DegradedReason
) -> None:
    h = harness(tmp_path / "cache.sqlite", valid)

    with pytest.raises(BudgetExhaustedError) as main:
        await generate_messages(SCENARIOS, h.models(), CONFIG, budget_guard=always(reason))
    with pytest.raises(BudgetExhaustedError) as holdout:
        await generate_holdout_messages(CONFIG, h.models(), SCENARIOS, budget_guard=always(reason))
    with pytest.raises(BudgetExhaustedError) as rag:
        await generate_rag_pairs(h.models(), CONFIG, budget_guard=always(reason))

    assert main.value.remaining == len(SCENARIOS)
    assert {main.value.reason, holdout.value.reason, rag.value.reason} == {reason}
    assert h.gemini.tracker.calls == 0
    assert h.store.calls == []

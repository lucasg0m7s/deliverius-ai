"""A short pause is waited out; a long one, or one that keeps coming, stops the run."""

from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from structlog.testing import capture_logs
from support.datagen_cli import COMMIT
from support.datagen_configs import CLI_CONFIG, MESSAGES_CONFIG
from support.datagen_llm import Answer, DatagenHarness, harness, structured, valid_variations
from support.llm_runtime import settings_with

from deliverius.datagen import (
    BudgetExhaustedError,
    generate_message_scenarios,
    generate_messages,
)
from deliverius.datagen.generation import (
    MAX_PAUSE_WAIT_SECONDS,
    MAX_PAUSES_IN_A_ROW,
    PauseWatch,
    pause_watch,
)
from deliverius.datagen.messages import render_scenario
from deliverius.datagen.pipeline import Step, llm_access, run_pipeline
from deliverius.llm.call import DegradedReason
from deliverius.llm.errors import BudgetRefusal, LlmBudgetExceededError, ProviderPausedError
from deliverius.llm.fake import Fail
from deliverius.llm.roles import ModelRole
from deliverius.shared.model_ref import LlmProvider

CONFIG = MESSAGES_CONFIG.model_copy(
    update={"messages": MESSAGES_CONFIG.messages.model_copy(update={"concurrency": 1})}
)
SCENARIOS = generate_message_scenarios(CONFIG)
HUMANS = {render_scenario(s): s for s in SCENARIOS}
PAUSED = Fail(ProviderPausedError(LlmProvider.GEMINI, 30.0))
MAX_ATTEMPTS = CONFIG.messages.max_attempts


class Sleeper:
    def __init__(self, h: DatagenHarness | None = None) -> None:
        self.slept: list[float] = []
        self._h = h

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._h is not None:
            self._h.clock.advance(timedelta(seconds=seconds))


def paused_first(counts: dict[int, int], cache: Path) -> DatagenHarness:
    """Scenario number N answers `counts[N]` pauses before its real answer."""
    pauses = {render_scenario(SCENARIOS[index]): count for index, count in counts.items()}
    calls: Counter[str] = Counter()

    def respond(human: str) -> Answer:
        calls[human] += 1
        if calls[human] <= pauses.get(human, 0):
            return 0.0, PAUSED
        return structured({"variations": valid_variations(HUMANS[human])})

    return harness(cache, respond)


def always_paused(cache: Path) -> DatagenHarness:
    return harness(cache, lambda _: (0.0, PAUSED))


def watch(seconds: float, sleeper: Sleeper) -> PauseWatch:
    return PauseWatch(lambda: seconds, sleeper)


async def test_a_short_pause_is_waited_out_and_the_same_job_is_asked_again(
    tmp_path: Path,
) -> None:
    h = paused_first({0: 1}, tmp_path / "cache.sqlite")
    sleeper = Sleeper()

    with capture_logs() as logs:
        result = await generate_messages(SCENARIOS, h.models(), CONFIG, pause=watch(5.0, sleeper))

    assert sleeper.slept == [5.0]
    assert result.failed_scenarios == ()
    assert len(result.messages) == len(SCENARIOS) * CONFIG.messages.variations_per_scenario
    assert h.gemini.tracker.calls == len(SCENARIOS) + 1
    [waited] = [e for e in logs if e["event"] == "datagen.provider_pause_waited"]
    assert waited["seconds"] == 5.0
    assert set(waited) == {"event", "log_level", "scenario_id", "seconds"}


async def test_a_pause_does_not_use_up_an_attempt(tmp_path: Path) -> None:
    h = paused_first({0: MAX_PAUSES_IN_A_ROW - 1}, tmp_path / "cache.sqlite")
    sleeper = Sleeper()

    with capture_logs() as logs:
        result = await generate_messages(SCENARIOS, h.models(), CONFIG, pause=watch(1.0, sleeper))

    assert MAX_PAUSES_IN_A_ROW - 1 > MAX_ATTEMPTS
    assert result.failed_scenarios == ()
    assert sleeper.slept == [1.0] * (MAX_PAUSES_IN_A_ROW - 1)
    assert [e for e in logs if e["event"] == "datagen.scenario_failed"] == []


async def test_a_pause_that_ended_while_the_job_looked_is_retried_without_sleeping(
    tmp_path: Path,
) -> None:
    h = paused_first({0: 1}, tmp_path / "cache.sqlite")
    sleeper = Sleeper()

    result = await generate_messages(SCENARIOS, h.models(), CONFIG, pause=watch(0.0, sleeper))

    assert sleeper.slept == []
    assert result.failed_scenarios == ()


async def test_a_pause_of_exactly_the_limit_is_still_waited_out(tmp_path: Path) -> None:
    h = paused_first({0: 1}, tmp_path / "cache.sqlite")
    sleeper = Sleeper()

    result = await generate_messages(
        SCENARIOS, h.models(), CONFIG, pause=watch(MAX_PAUSE_WAIT_SECONDS, sleeper)
    )

    assert sleeper.slept == [MAX_PAUSE_WAIT_SECONDS]
    assert result.failed_scenarios == ()


async def test_a_pause_longer_than_the_limit_stops_the_run_at_once(tmp_path: Path) -> None:
    h = paused_first({3: 1}, tmp_path / "cache.sqlite")
    sleeper = Sleeper()

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(
            SCENARIOS, h.models(), CONFIG, pause=watch(MAX_PAUSE_WAIT_SECONDS + 0.1, sleeper)
        )

    assert stopped.value.reason is DegradedReason.PROVIDER_PAUSED
    assert stopped.value.remaining == len(SCENARIOS) - 3
    assert sleeper.slept == []
    assert h.gemini.tracker.calls == 4


async def test_pauses_that_keep_coming_without_a_success_stop_the_run(tmp_path: Path) -> None:
    h = always_paused(tmp_path / "cache.sqlite")
    sleeper = Sleeper()

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(SCENARIOS, h.models(), CONFIG, pause=watch(1.0, sleeper))

    assert stopped.value.reason is DegradedReason.PROVIDER_PAUSED
    assert stopped.value.remaining == len(SCENARIOS)
    assert sleeper.slept == [1.0] * MAX_PAUSES_IN_A_ROW
    assert h.gemini.tracker.calls == MAX_PAUSES_IN_A_ROW + 1


async def test_a_success_between_pauses_starts_the_count_again(tmp_path: Path) -> None:
    each = MAX_PAUSES_IN_A_ROW - 2
    paused_calls = {*range(1, each + 1), *range(each + 4, 2 * each + 4)}
    total = 0

    def respond(human: str) -> Answer:
        nonlocal total
        total += 1
        if total in paused_calls:
            return 0.0, PAUSED
        return structured({"variations": valid_variations(HUMANS[human])})

    h = harness(tmp_path / "cache.sqlite", respond)
    sleeper = Sleeper()

    result = await generate_messages(SCENARIOS, h.models(), CONFIG, pause=watch(1.0, sleeper))

    assert len(sleeper.slept) == 2 * each > MAX_PAUSES_IN_A_ROW
    assert result.failed_scenarios == ()


async def test_without_a_pause_watch_any_pause_stops_the_run(tmp_path: Path) -> None:
    h = paused_first({0: 1}, tmp_path / "cache.sqlite")

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(SCENARIOS, h.models(), CONFIG)

    assert stopped.value.reason is DegradedReason.PROVIDER_PAUSED


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        (BudgetRefusal.EXHAUSTED, DegradedReason.BUDGET_EXHAUSTED),
        (BudgetRefusal.DATABASE_UNAVAILABLE, DegradedReason.BUDGET_UNAVAILABLE),
    ],
)
async def test_an_exhausted_or_unreachable_budget_still_stops_without_waiting(
    tmp_path: Path, refusal: BudgetRefusal, reason: DegradedReason
) -> None:
    error = LlmBudgetExceededError(LlmProvider.GEMINI, refusal)
    h = harness(tmp_path / "cache.sqlite", lambda _: (0.0, Fail(error)))
    sleeper = Sleeper()

    with pytest.raises(BudgetExhaustedError) as stopped:
        await generate_messages(SCENARIOS, h.models(), CONFIG, pause=watch(1.0, sleeper))

    assert stopped.value.reason is reason
    assert sleeper.slept == []
    assert h.gemini.tracker.calls == 1


async def test_a_real_pause_of_the_gate_is_slept_through_until_the_provider_is_free(
    tmp_path: Path,
) -> None:
    h = harness(
        tmp_path / "cache.sqlite",
        lambda human: structured({"variations": valid_variations(HUMANS[human])}),
    )
    h.runtime.gate_for(LlmProvider.GEMINI).pause_for(5.0)
    sleeper = Sleeper(h)

    result = await generate_messages(
        SCENARIOS, h.models(), CONFIG, pause=pause_watch(h.runtime, ModelRole.DATAGEN, sleeper)
    )

    assert sleeper.slept == [5.0]
    assert result.failed_scenarios == ()
    assert h.gemini.tracker.calls == len(SCENARIOS)


async def test_the_pipeline_waits_out_a_short_pause_of_the_provider(tmp_path: Path) -> None:
    config = CLI_CONFIG
    humans = {render_scenario(s): s for s in generate_message_scenarios(config)}
    h = harness(
        tmp_path / "cache.sqlite",
        lambda human: structured({"variations": valid_variations(humans[human])}),
    )
    h.runtime.gate_for(LlmProvider.GEMINI).pause_for(5.0)
    sleeper = Sleeper(h)
    access = llm_access(h.runtime, settings_with(), h.cache_path, sleeper)

    dataset = await run_pipeline("messages", [Step.MESSAGES], config, COMMIT, access)

    assert sleeper.slept == [5.0]
    assert dataset.manifest.counts["failed_scenarios.messages"] == 0

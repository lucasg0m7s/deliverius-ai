"""The budget: the estimate, the refusal to start on a spent day and the stop in the middle."""

import asyncio
from pathlib import Path

import pytest
from support.datagen_cli import scripted_harness, wiring_for
from support.datagen_configs import CLI_CONFIG
from support.datagen_llm import DatagenHarness
from support.llm_doubles import StoreDown
from support.llm_rows import TODAY
from support.llm_runtime import settings_with

from deliverius.cli import texts
from deliverius.cli.datagen import main
from deliverius.datagen.errors import BudgetExhaustedError
from deliverius.llm.call import DegradedReason
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import UNLIMITED, BudgetLimit, BudgetStatus, BudgetUsage
from deliverius.llm.roles import ModelRole
from deliverius.shared.model_ref import LlmProvider, ModelRef

MAIN_SCENARIOS = 11 * CLI_CONFIG.messages.scenarios_per_intent
RESUME_ADVICE = "rode de novo depois que o orçamento renovar; o cache guarda o que já foi feito"
GEMINI = ModelRef(LlmProvider.GEMINI, "gemini-3.5-flash-lite")
GROQ = ModelRef(LlmProvider.GROQ, "openai/gpt-oss-120b")


def run_messages(tmp_path: Path, h: DatagenHarness, *extra: str) -> int:
    return main(
        [
            "datagen",
            "messages",
            "--out",
            str(tmp_path / "out"),
            "--cache",
            str(tmp_path / "cache.sqlite"),
            "--concurrency",
            "1",
            *extra,
        ],
        wiring_for(h),
    )


async def spend_the_day(store: InMemoryLlmUsageStore, requests: int) -> None:
    for _ in range(requests):
        await store.reserve(LlmProvider.GEMINI, TODAY, UNLIMITED)


def test_a_day_already_spent_is_refused_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = scripted_harness(
        tmp_path / "cache.sqlite", settings=settings_with(llm_gemini_daily_requests=5)
    )
    asyncio.run(spend_the_day(h.store, 5))

    code = run_messages(tmp_path, h)

    captured = capsys.readouterr()
    assert code == 2
    assert f"Faltam {MAIN_SCENARIOS} chamadas" in captured.err
    assert RESUME_ADVICE in captured.err.lower()
    assert "Estimativa de chamadas" in captured.out
    assert "uso de hoje em gemini: 5 de 5 chamadas" in captured.out
    assert h.gemini.tracker.calls == 0
    assert not (tmp_path / "out").exists()


def test_the_budget_ending_after_forty_percent_stops_with_the_cache_on_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    forty_percent = MAIN_SCENARIOS * 4 // 10
    h = scripted_harness(
        tmp_path / "cache.sqlite", settings=settings_with(llm_gemini_daily_requests=forty_percent)
    )

    code = run_messages(tmp_path, h)

    err = capsys.readouterr().err
    assert code == 2
    assert f"Faltam {MAIN_SCENARIOS - forty_percent} chamadas" in err
    assert RESUME_ADVICE in err.lower()
    assert h.gemini.tracker.calls == forty_percent
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".out.*"))


def test_running_again_when_the_budget_renews_pays_only_for_what_is_missing(
    tmp_path: Path,
) -> None:
    forty_percent = MAIN_SCENARIOS * 4 // 10
    cache = tmp_path / "cache.sqlite"
    limited = scripted_harness(
        cache, settings=settings_with(llm_gemini_daily_requests=forty_percent)
    )
    assert run_messages(tmp_path, limited) == 2

    renewed = scripted_harness(cache)

    assert run_messages(tmp_path, renewed) == 0
    assert renewed.gemini.tracker.calls == MAIN_SCENARIOS - forty_percent
    assert (tmp_path / "out" / "manifest.json").is_file()


def test_an_unreachable_budget_database_is_refused_with_the_database_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = scripted_harness(tmp_path / "cache.sqlite", store=StoreDown())

    code = run_messages(tmp_path, h)

    captured = capsys.readouterr()
    assert code == 2
    assert "docker compose up" in captured.err
    assert RESUME_ADVICE in captured.err.lower()
    assert "uso de hoje: indisponível" in captured.out
    assert h.gemini.tracker.calls == 0
    assert not (tmp_path / "out").exists()


def test_the_stop_is_logged_without_any_message_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = scripted_harness(
        tmp_path / "cache.sqlite", settings=settings_with(llm_gemini_daily_requests=3)
    )

    run_messages(tmp_path, h)

    events = [
        line for line in capsys.readouterr().err.splitlines() if "datagen.budget_exhausted" in line
    ]
    assert len(events) == 1
    assert f'"remaining": {MAIN_SCENARIOS - 3}' in events[0]


def status(provider: LlmProvider, requests: int, tokens: int, limit: BudgetLimit) -> BudgetStatus:
    return BudgetStatus(provider, TODAY, BudgetUsage(requests, tokens), limit)


def test_the_estimate_lists_each_role_with_its_model_and_the_use_of_the_day() -> None:
    lines = texts.estimate_lines(
        {ModelRole.DATAGEN: 345, ModelRole.DATAGEN_HOLDOUT: 55},
        {ModelRole.DATAGEN: str(GEMINI), ModelRole.DATAGEN_HOLDOUT: str(GROQ)},
        {
            ModelRole.DATAGEN: status(LlmProvider.GEMINI, 12, 14_000, BudgetLimit(400, 2_000_000)),
            ModelRole.DATAGEN_HOLDOUT: status(LlmProvider.GROQ, 0, 0, BudgetLimit(None, 150_000)),
        },
    )

    assert lines == [
        "Estimativa de chamadas ao LLM (no mínimo; cada nova tentativa conta mais uma):",
        "  gemini:gemini-3.5-flash-lite (papel datagen): 345 chamadas",
        "    uso de hoje em gemini: 12 de 400 chamadas; 14.000 de 2.000.000 tokens",
        "  groq:openai/gpt-oss-120b (papel datagen_holdout): 55 chamadas",
        "    uso de hoje em groq: 0 de sem limite chamadas; 0 de 150.000 tokens",
    ]


def test_an_estimate_above_what_is_left_today_says_the_run_will_stop_and_can_resume() -> None:
    lines = texts.estimate_lines(
        {ModelRole.DATAGEN: 345},
        {ModelRole.DATAGEN: str(GEMINI)},
        {ModelRole.DATAGEN: status(LlmProvider.GEMINI, 100, 0, BudgetLimit(400, None))},
    )

    assert "Não cabe no que resta hoje (300 chamadas)" in lines[-1]
    assert "o cache guarda o que já foi feito" in lines[-1]


def test_a_step_without_llm_says_so() -> None:
    assert texts.estimate_lines({}, {}, {}) == ["Este passo não chama nenhum LLM."]


@pytest.mark.parametrize(
    "reason", [DegradedReason.PROVIDER_PAUSED, DegradedReason.BUDGET_EXHAUSTED]
)
def test_every_stop_message_says_how_many_are_missing_and_how_to_resume(
    reason: DegradedReason,
) -> None:
    lines = texts.budget_message(BudgetExhaustedError(1234, reason))

    assert "Faltam 1.234 chamadas" in lines[0]
    assert lines[1].lower() == RESUME_ADVICE + "."

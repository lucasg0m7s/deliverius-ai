"""The message generation on the real `LlmRuntime` and the real Postgres budget and usage log.

The scripted models stand in for the providers; everything else is production: the usage store, the
governor, the response cache and the budget check the generator makes itself.
"""

import asyncio
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from support.database import settings_for
from support.datagen_cli import answers_for
from support.datagen_configs import CLI_CONFIG
from support.datagen_llm import PromptDrivenProvider
from support.llm_rows import NOW, TODAY
from support.llm_runtime import FAST_RETRY, settings_with

from deliverius.cli.datagen import main
from deliverius.cli.wiring import Wiring
from deliverius.config import Settings
from deliverius.datagen import verify_dataset
from deliverius.datagen.output import read_records
from deliverius.datagen.pipeline import CommitInfo
from deliverius.datagen.records import MessageRecord, Split
from deliverius.db.engine import create_engine
from deliverius.db.session import create_session_factory
from deliverius.llm.repository import PostgresLlmUsageStore
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime
from deliverius.shared.clock import FixedClock
from deliverius.shared.model_ref import LlmProvider

pytestmark = pytest.mark.integration

SCENARIOS = 11 * CLI_CONFIG.messages.scenarios_per_intent
TOKENS_PER_CALL = 1_200
FORTY_PERCENT = SCENARIOS * 4 // 10

type Providers = dict[LlmProvider, PromptDrivenProvider]


async def _run_sql(url: str, statement: str) -> list[dict[str, Any]]:
    engine = create_engine(settings_for(url))
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement))
            return [dict(row._mapping) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


def sql(url: str, statement: str) -> list[dict[str, Any]]:
    return asyncio.run(_run_sql(url, statement))


@pytest.fixture
def database(migrated_database_url: str) -> Iterator[str]:
    def clean() -> None:
        sql(migrated_database_url, "DELETE FROM llm_calls")
        sql(migrated_database_url, "DELETE FROM llm_budget_usage")

    clean()
    yield migrated_database_url
    clean()


def wiring_on(url: str, settings: Settings, providers: Providers) -> Wiring:
    @asynccontextmanager
    async def runtime(_: Settings, roles: Iterable[ModelRole]) -> AsyncIterator[LlmRuntime]:
        engine = create_engine(settings_for(url))
        try:
            yield LlmRuntime(
                settings,
                PostgresLlmUsageStore(create_session_factory(engine)),
                FixedClock(NOW),
                roles=roles,
                builders=dict(providers),
                retry=FAST_RETRY,
            )
        finally:
            await engine.dispose()

    return Wiring(
        settings=lambda: settings,
        runtime=runtime,
        commit=CommitInfo,
        base_config=CLI_CONFIG,
    )


def scripted_providers() -> Providers:
    gemini, groq = answers_for(CLI_CONFIG)
    return {
        LlmProvider.GEMINI: PromptDrivenProvider(gemini),
        LlmProvider.GROQ: PromptDrivenProvider(groq),
    }


def generate_messages(tmp_path: Path, wiring: Wiring, name: str, concurrency: int = 1) -> int:
    return main(
        [
            "datagen",
            "messages",
            "--out",
            str(tmp_path / name),
            "--cache",
            str(tmp_path / "cache.sqlite"),
            "--concurrency",
            str(concurrency),
        ],
        wiring,
    )


def count_calls(url: str) -> int:
    return int(sql(url, "SELECT count(*) AS n FROM llm_calls")[0]["n"])


def requests_used(url: str) -> int:
    return int(sql(url, "SELECT requests_used FROM llm_budget_usage")[0]["requests_used"])


def test_generating_messages_writes_one_usage_row_per_call_and_counts_the_day(
    database: str, tmp_path: Path
) -> None:
    providers = scripted_providers()
    wiring = wiring_on(database, settings_with(), providers)

    code = generate_messages(tmp_path, wiring, "out")

    assert code == 0
    assert providers[LlmProvider.GEMINI].tracker.calls == SCENARIOS
    calls = sql(
        database, "SELECT role, provider, status, input_tokens, output_tokens FROM llm_calls"
    )
    assert len(calls) == SCENARIOS
    assert {(c["role"], c["provider"], c["status"]) for c in calls} == {("datagen", "gemini", "ok")}
    assert {(c["input_tokens"], c["output_tokens"]) for c in calls} == {(1_000, 200)}
    [usage] = sql(
        database, "SELECT provider, day, requests_used, tokens_used FROM llm_budget_usage"
    )
    assert (usage["provider"], usage["day"]) == ("gemini", TODAY)
    assert (usage["requests_used"], usage["tokens_used"]) == (
        SCENARIOS,
        SCENARIOS * TOKENS_PER_CALL,
    )
    messages = [
        message
        for split in Split
        for message in read_records(
            tmp_path / "out", f"messages/{split.value}.jsonl", MessageRecord
        )
    ]
    assert messages
    assert not any(message.text in str(calls) for message in messages)


def test_a_second_run_on_the_same_cache_adds_no_call_and_no_usage(
    database: str, tmp_path: Path
) -> None:
    assert (
        generate_messages(tmp_path, wiring_on(database, settings_with(), scripted_providers()), "a")
        == 0
    )
    again = scripted_providers()

    code = generate_messages(tmp_path, wiring_on(database, settings_with(), again), "b")

    assert code == 0
    assert again[LlmProvider.GEMINI].tracker.calls == 0
    assert count_calls(database) == SCENARIOS
    assert requests_used(database) == SCENARIOS


def test_the_budget_ending_in_the_middle_stops_with_exit_2_and_writes_no_manifest(
    database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    providers = scripted_providers()
    settings = settings_with(llm_gemini_daily_requests=FORTY_PERCENT)

    code = generate_messages(tmp_path, wiring_on(database, settings, providers), "out")

    assert code == 2
    assert f"Faltam {SCENARIOS - FORTY_PERCENT} chamadas" in capsys.readouterr().err
    assert providers[LlmProvider.GEMINI].tracker.calls == FORTY_PERCENT
    assert count_calls(database) == FORTY_PERCENT
    assert requests_used(database) == FORTY_PERCENT
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".out.*"))


def test_with_parallel_calls_the_run_stops_within_the_calls_already_in_flight(
    database: str, tmp_path: Path
) -> None:
    concurrency = 4
    settings = settings_with(llm_gemini_daily_requests=FORTY_PERCENT)

    code = generate_messages(
        tmp_path, wiring_on(database, settings, scripted_providers()), "out", concurrency
    )

    assert code == 2
    used = requests_used(database)
    assert FORTY_PERCENT <= used <= FORTY_PERCENT + concurrency - 1
    assert count_calls(database) == used
    assert not (tmp_path / "out").exists()


def test_a_spent_day_in_the_database_is_refused_before_the_first_call(
    database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        database,
        "INSERT INTO llm_budget_usage (provider, day, requests_used, tokens_used) "  # noqa: S608
        f"VALUES ('gemini', '{TODAY}', 400, 0)",
    )
    providers = scripted_providers()

    code = generate_messages(tmp_path, wiring_on(database, settings_with(), providers), "out")

    assert code == 2
    assert "uso de hoje em gemini: 400 de 400 chamadas" in capsys.readouterr().out
    assert providers[LlmProvider.GEMINI].tracker.calls == 0
    assert not (tmp_path / "out").exists()


def test_a_finished_run_leaves_a_manifest_that_verifies(database: str, tmp_path: Path) -> None:
    code = generate_messages(
        tmp_path, wiring_on(database, settings_with(), scripted_providers()), "out"
    )

    assert code == 0
    assert verify_dataset(tmp_path / "out").models == {"datagen": "gemini:gemini-3.5-flash-lite"}

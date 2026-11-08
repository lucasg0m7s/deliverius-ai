"""One real scenario through the data generator: Gemini (`datagen`) and Groq (`datagen_holdout`).

Run only on request (`uv run pytest tests/llm/test_datagen_real.py -m llm --no-cov`): each case
costs at least one call of quota. The assembly is the production one (`llm_access` over a runtime on
the Postgres usage store), with the response cache in a temporary file. The texts are synthetic, and
a failure message carries only counts, never the generated text.
"""

from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from support.database import settings_for
from support.datagen_configs import MESSAGES_CONFIG

from deliverius.config import Settings
from deliverius.datagen.messages import ROLE_OF, MessageGeneration, generate_messages
from deliverius.datagen.pipeline import llm_access
from deliverius.datagen.placeholders import PII_KIND_OF, Marker
from deliverius.datagen.scenarios import (
    MessageScenario,
    MessageSet,
    generate_holdout_scenarios,
    generate_message_scenarios,
)
from deliverius.db.engine import create_engine
from deliverius.db.session import create_session_factory
from deliverius.llm.repository import PostgresLlmUsageStore
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import API_KEY_VARIABLES, LlmRuntime
from deliverius.shared.clock import SystemClock
from deliverius.shared.model_ref import LlmProvider, ModelRef

# One event loop for the module: langchain_openai caches one async HTTP client per process, and it
# breaks when a later test runs on a new loop ("Event loop is closed").
pytestmark = [pytest.mark.llm, pytest.mark.asyncio(loop_scope="module")]

MIN_VALID_VARIATIONS = 5

CASES = [
    pytest.param(MessageSet.MAIN, LlmProvider.GEMINI, id="datagen-gemini"),
    pytest.param(MessageSet.HOLDOUT, LlmProvider.GROQ, id="datagen_holdout-groq"),
]


@dataclass(frozen=True, slots=True)
class UsageRow:
    role: str
    provider: str
    model: str
    status: str
    input_tokens: int | None
    output_tokens: int | None
    equivalent_cost_usd_micros: int | None


@dataclass(frozen=True, slots=True)
class Session:
    runtime: LlmRuntime
    settings: Settings
    database_url: str

    async def usage_rows(self) -> list[UsageRow]:
        engine = create_engine(settings_for(self.database_url))
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT role, provider, model, status, input_tokens, output_tokens,"
                        " equivalent_cost_usd_micros FROM llm_calls ORDER BY created_at"
                    )
                )
                return [UsageRow(**row._mapping) for row in result]
        finally:
            await engine.dispose()


async def _clean(database_url: str) -> None:
    engine = create_engine(settings_for(database_url))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM llm_calls"))
            await connection.execute(text("DELETE FROM llm_budget_usage"))
    finally:
        await engine.dispose()


@asynccontextmanager
async def production_session(database_url: str, role: ModelRole) -> AsyncIterator[Session]:
    settings = Settings()  # type: ignore[call-arg]
    _require_key(settings, model_of(settings, role).provider)
    await _clean(database_url)
    engine = create_engine(settings_for(database_url))
    try:
        runtime = LlmRuntime(
            settings,
            PostgresLlmUsageStore(create_session_factory(engine)),
            SystemClock(),
            roles=[role],
        )
        yield Session(runtime, settings, database_url)
    finally:
        await engine.dispose()
        await _clean(database_url)


def model_of(settings: Settings, role: ModelRole) -> ModelRef:
    if role is ModelRole.DATAGEN:
        return settings.llm_datagen_model
    return settings.llm_datagen_holdout_model


def _require_key(settings: Settings, provider: LlmProvider) -> None:
    key = settings.gemini_api_key if provider is LlmProvider.GEMINI else settings.groq_api_key
    if key is None:
        pytest.fail(f"{API_KEY_VARIABLES[provider]} is not set (environment or .env)")


def pick_scenario(message_set: MessageSet) -> MessageScenario:
    """The first scenario that asks for a personal-data marker, so the filling is also checked."""
    main = generate_message_scenarios(MESSAGES_CONFIG)
    scenarios = (
        main
        if message_set is MessageSet.MAIN
        else generate_holdout_scenarios(MESSAGES_CONFIG, main)
    )
    return next(s for s in scenarios if any(marker in PII_KIND_OF for marker in s.markers))


def discards_by_reason(generation: MessageGeneration) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for (reason, _), count in generation.discards.items():
        totals[reason.value] += count
    return dict(totals)


def assert_valid_generation(generation: MessageGeneration, scenario: MessageScenario) -> None:
    summary: dict[str, Any] = {
        "kept": len(generation.messages),
        "failed": len(generation.failed_scenarios),
        "discards": discards_by_reason(generation),
    }
    assert generation.failed_scenarios == (), summary
    assert len(generation.messages) >= MIN_VALID_VARIATIONS, summary
    requested_kinds = {PII_KIND_OF[marker] for marker in scenario.markers if marker in PII_KIND_OF}
    for message in generation.messages:
        assert message.scenario_id == scenario.id
        assert message.intent is scenario.intent
        assert message.text.strip()
        assert "[[" not in message.text, "a marker was left unfilled"
        assert message.has_injection is (Marker.INJECTION in scenario.markers)
        assert {span.kind for span in message.pii_spans} == requested_kinds
        for span in message.pii_spans:
            assert 0 <= span.start < span.end <= len(message.text)


def assert_billed_rows(rows: list[UsageRow], role: ModelRole, model: ModelRef) -> None:
    assert rows, "no usage row was written"
    for row in rows:
        assert row.status == "ok"
        assert row.role == role.value
        assert (row.provider, row.model) == (model.provider.value, model.model)
        assert row.input_tokens is not None
        assert row.input_tokens > 0
        assert row.output_tokens is not None
        assert row.output_tokens > 0
        assert row.equivalent_cost_usd_micros is not None


@pytest.mark.parametrize(("message_set", "provider"), CASES)
async def test_a_real_scenario_gives_valid_variations_and_a_billed_usage_row(
    message_set: MessageSet, provider: LlmProvider, migrated_database_url: str, tmp_path: Path
) -> None:
    role = ROLE_OF[message_set]
    scenario = pick_scenario(message_set)

    async with production_session(migrated_database_url, role) as session:
        settings = session.settings
        model = model_of(settings, role)
        assert model.provider is provider
        access = llm_access(session.runtime, settings, tmp_path / "cache.sqlite")

        generation = await generate_messages(
            [scenario],
            access.models_for(role),
            MESSAGES_CONFIG,
            message_set,
            budget_guard=access.guard_for(role),
        )
        rows = await session.usage_rows()

    assert_valid_generation(generation, scenario)
    assert_billed_rows(rows, role, model)

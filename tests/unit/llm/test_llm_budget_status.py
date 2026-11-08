"""`LlmRuntime.budget_status`: today's use of a role's provider, read without spending anything."""

from datetime import UTC, timedelta

import pytest
from support.llm_doubles import StoreDown
from support.llm_rows import NOW, TODAY
from support.llm_runtime import ScriptedProvider, scripted_runtime, settings_with

from deliverius.db.errors import DatabaseUnavailableError
from deliverius.llm.ports import BudgetLimit, BudgetUsage
from deliverius.llm.roles import ModelRole
from deliverius.shared.model_ref import LlmProvider

PROVIDERS = {LlmProvider.GEMINI: ScriptedProvider([]), LlmProvider.GROQ: ScriptedProvider([])}


async def test_the_status_is_the_main_providers_usage_against_its_ceilings() -> None:
    settings = settings_with(llm_gemini_daily_requests=5, llm_gemini_daily_tokens=900)
    runtime, store, _ = scripted_runtime(settings, PROVIDERS)
    await store.reserve(LlmProvider.GEMINI, TODAY, BudgetLimit(None, None))
    await store.add_tokens(LlmProvider.GEMINI, TODAY, 400)
    await store.reserve(LlmProvider.GROQ, TODAY, BudgetLimit(None, None))

    status = await runtime.budget_status(ModelRole.DATAGEN)

    assert status.provider is LlmProvider.GEMINI
    assert status.day == TODAY
    assert status.usage == BudgetUsage(1, 400)
    assert status.limit == BudgetLimit(5, 900)
    assert not status.exhausted
    assert (await runtime.budget_status(ModelRole.DATAGEN_HOLDOUT)).provider is LlmProvider.GROQ


async def test_reading_the_status_does_not_count_a_request() -> None:
    runtime, store, _ = scripted_runtime(settings_with(), PROVIDERS)

    await runtime.budget_status(ModelRole.DATAGEN)

    assert store.usage(LlmProvider.GEMINI, TODAY) is None


async def test_a_reached_ceiling_is_exhausted() -> None:
    runtime, store, _ = scripted_runtime(settings_with(llm_gemini_daily_tokens=500), PROVIDERS)
    await store.add_tokens(LlmProvider.GEMINI, TODAY, 500)

    assert (await runtime.budget_status(ModelRole.DATAGEN)).exhausted


async def test_the_day_is_the_utc_day_of_the_clock() -> None:
    runtime, store, clock = scripted_runtime(settings_with(), PROVIDERS)
    await store.add_tokens(LlmProvider.GEMINI, TODAY, 10)
    clock.advance(timedelta(days=1))

    status = await runtime.budget_status(ModelRole.DATAGEN)

    assert status.day == (NOW + timedelta(days=1)).astimezone(UTC).date()
    assert status.usage == BudgetUsage(0, 0)


async def test_an_unreachable_store_raises_the_database_error() -> None:
    runtime, _, _ = scripted_runtime(settings_with(), PROVIDERS, store=StoreDown())

    with pytest.raises(DatabaseUnavailableError):
        await runtime.budget_status(ModelRole.DATAGEN)

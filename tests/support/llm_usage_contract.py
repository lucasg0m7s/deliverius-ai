"""The `LlmUsageStore` contract, run against every implementation (Liskov substitution, tested).

A subclass provides the `harness` fixture; the tests here never know which implementation they hit.
"""

from datetime import date, timedelta
from typing import Protocol

import pytest
from support.llm_rows import build_call_record

from deliverius.llm.ports import (
    UNLIMITED,
    BudgetLimit,
    BudgetUsage,
    CallStatus,
    LlmCallRecord,
    LlmUsageStore,
)
from deliverius.llm.pricing import TokenUsage
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import new_id
from deliverius.shared.model_ref import LlmProvider

DAY = date(2026, 11, 7)
GEMINI = LlmProvider.GEMINI
GROQ = LlmProvider.GROQ


class UsageHarness(Protocol):
    store: LlmUsageStore

    async def usage(self, provider: LlmProvider, day: date) -> BudgetUsage | None: ...

    async def calls(self) -> list[LlmCallRecord]: ...


class LlmUsageStoreContract:
    @pytest.fixture
    def harness(self) -> UsageHarness:
        raise NotImplementedError

    async def test_reservations_are_counted_in_order(self, harness: UsageHarness) -> None:
        limit = BudgetLimit(daily_requests=5, daily_tokens=None)

        counts = [await harness.store.reserve(GEMINI, DAY, limit) for _ in range(3)]

        assert counts == [1, 2, 3]
        assert await harness.usage(GEMINI, DAY) == BudgetUsage(3, 0)

    async def test_the_last_request_within_the_limit_is_granted_and_the_next_is_not(
        self, harness: UsageHarness
    ) -> None:
        limit = BudgetLimit(daily_requests=3, daily_tokens=None)

        counts = [await harness.store.reserve(GEMINI, DAY, limit) for _ in range(5)]

        assert counts == [1, 2, 3, None, None]
        assert await harness.usage(GEMINI, DAY) == BudgetUsage(3, 0)

    async def test_a_limit_of_zero_refuses_without_creating_a_counter(
        self, harness: UsageHarness
    ) -> None:
        refused = await harness.store.reserve(GEMINI, DAY, BudgetLimit(0, None))

        assert refused is None
        assert await harness.usage(GEMINI, DAY) is None

    async def test_a_token_limit_of_zero_refuses_even_with_requests_to_spare(
        self, harness: UsageHarness
    ) -> None:
        refused = await harness.store.reserve(GEMINI, DAY, BudgetLimit(100, 0))

        assert refused is None
        assert await harness.usage(GEMINI, DAY) is None

    async def test_no_limit_never_refuses_and_still_counts(self, harness: UsageHarness) -> None:
        counts = [await harness.store.reserve(GROQ, DAY, UNLIMITED) for _ in range(3)]

        assert counts == [1, 2, 3]

    async def test_a_reached_token_limit_refuses_with_requests_to_spare(
        self, harness: UsageHarness
    ) -> None:
        limit = BudgetLimit(daily_requests=100, daily_tokens=1_000)
        assert await harness.store.reserve(GEMINI, DAY, limit) == 1
        await harness.store.add_tokens(GEMINI, DAY, 1_000)

        assert await harness.store.reserve(GEMINI, DAY, limit) is None
        assert await harness.usage(GEMINI, DAY) == BudgetUsage(1, 1_000)

    async def test_one_token_below_the_limit_still_grants(self, harness: UsageHarness) -> None:
        limit = BudgetLimit(daily_requests=100, daily_tokens=1_000)
        assert await harness.store.reserve(GEMINI, DAY, limit) == 1
        await harness.store.add_tokens(GEMINI, DAY, 999)

        assert await harness.store.reserve(GEMINI, DAY, limit) == 2

    async def test_a_new_day_starts_from_zero(self, harness: UsageHarness) -> None:
        limit = BudgetLimit(daily_requests=1, daily_tokens=None)
        assert await harness.store.reserve(GEMINI, DAY, limit) == 1
        assert await harness.store.reserve(GEMINI, DAY, limit) is None

        assert await harness.store.reserve(GEMINI, DAY + timedelta(days=1), limit) == 1
        assert await harness.usage(GEMINI, DAY) == BudgetUsage(1, 0)

    async def test_each_provider_has_its_own_counter(self, harness: UsageHarness) -> None:
        limit = BudgetLimit(daily_requests=1, daily_tokens=None)

        assert await harness.store.reserve(GEMINI, DAY, limit) == 1
        assert await harness.store.reserve(GROQ, DAY, limit) == 1
        assert await harness.store.reserve(GEMINI, DAY, limit) is None

    async def test_tokens_accumulate_and_the_total_comes_back(self, harness: UsageHarness) -> None:
        assert await harness.store.add_tokens(GEMINI, DAY, 120) == 120
        assert await harness.store.add_tokens(GEMINI, DAY, 30) == 150
        assert await harness.store.add_tokens(GEMINI, DAY, 0) == 150

        assert await harness.usage(GEMINI, DAY) == BudgetUsage(0, 150)

    async def test_tokens_added_before_any_reservation_keep_the_request_count(
        self, harness: UsageHarness
    ) -> None:
        await harness.store.add_tokens(GEMINI, DAY, 50)

        assert await harness.store.reserve(GEMINI, DAY, UNLIMITED) == 1
        assert await harness.usage(GEMINI, DAY) == BudgetUsage(1, 50)

    async def test_negative_tokens_are_a_bug(self, harness: UsageHarness) -> None:
        with pytest.raises(InvariantError) as error:
            await harness.store.add_tokens(GEMINI, DAY, -1)

        assert error.value.rule == "llm_usage.negative_tokens"
        assert await harness.usage(GEMINI, DAY) is None

    async def test_reading_the_usage_of_an_untouched_day_gives_zero_and_creates_nothing(
        self, harness: UsageHarness
    ) -> None:
        assert await harness.store.read_usage(GEMINI, DAY) == BudgetUsage(0, 0)
        assert await harness.usage(GEMINI, DAY) is None

    async def test_reading_the_usage_gives_the_requests_and_tokens_of_that_day_and_provider(
        self, harness: UsageHarness
    ) -> None:
        await harness.store.reserve(GEMINI, DAY, UNLIMITED)
        await harness.store.reserve(GEMINI, DAY, UNLIMITED)
        await harness.store.add_tokens(GEMINI, DAY, 700)
        await harness.store.reserve(GROQ, DAY, UNLIMITED)

        assert await harness.store.read_usage(GEMINI, DAY) == BudgetUsage(2, 700)
        assert await harness.store.read_usage(GROQ, DAY) == BudgetUsage(1, 0)
        assert await harness.store.read_usage(GEMINI, DAY + timedelta(days=1)) == BudgetUsage(0, 0)

    async def test_a_recorded_call_keeps_every_field(self, harness: UsageHarness) -> None:
        record = build_call_record(
            conversation_id=new_id(),
            sandbox_id=new_id(),
            prompt_name="compose_reply",
            prompt_version=2,
            prompt_hash="a" * 64,
            trace_id="trace-1",
        )

        await harness.store.record_call(record)

        assert await harness.calls() == [record]

    async def test_a_failed_call_keeps_its_error_type_and_empty_tokens(
        self, harness: UsageHarness
    ) -> None:
        record = build_call_record(
            status=CallStatus.ERROR,
            error_type="ModelRateLimitError",
            usage=TokenUsage(None, None),
            real_cost_usd_micros=None,
            equivalent_cost_usd_micros=None,
        )

        await harness.store.record_call(record)

        [stored] = await harness.calls()
        assert stored == record
        assert stored.usage == TokenUsage(None, None, None)
        assert stored.equivalent_cost_usd_micros is None

    async def test_every_attempt_is_its_own_row(self, harness: UsageHarness) -> None:
        records = [build_call_record() for _ in range(3)]

        for record in records:
            await harness.store.record_call(record)

        assert sorted(c.id for c in await harness.calls()) == sorted(r.id for r in records)

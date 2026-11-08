from datetime import date, datetime

import pytest
from support.llm_rows import build_call_record

from deliverius.llm.ports import BudgetLimit, BudgetStatus, BudgetUsage
from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider


@pytest.mark.parametrize(
    "limits", [(-1, None), (None, -1)], ids=["negative-requests", "negative-tokens"]
)
def test_a_negative_budget_limit_is_a_bug(limits: tuple[int | None, int | None]) -> None:
    with pytest.raises(InvariantError) as error:
        BudgetLimit(*limits)

    assert error.value.rule == "budget_limit.negative"


def test_zero_and_unlimited_are_valid_budget_limits() -> None:
    assert BudgetLimit(0, 0).daily_requests == 0
    assert BudgetLimit(None, None).daily_tokens is None


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"latency_ms": -1}, "llm_call.latency_negative"),
        ({"real_cost_usd_micros": -1}, "llm_call.cost_negative"),
        ({"equivalent_cost_usd_micros": -1}, "llm_call.cost_negative"),
        ({"created_at": datetime(2026, 11, 7, 12, 0, tzinfo=None)}, "naive_datetime.created_at"),  # noqa: DTZ001
    ],
)
def test_an_impossible_call_record_is_a_bug(overrides: dict[str, object], rule: str) -> None:
    with pytest.raises(InvariantError) as error:
        build_call_record(**overrides)

    assert error.value.rule == rule


@pytest.mark.parametrize(
    ("limit", "usage", "reached"),
    [
        (BudgetLimit(10, None), BudgetUsage(9, 999_999), False),
        (BudgetLimit(10, None), BudgetUsage(10, 0), True),
        (BudgetLimit(None, 1_000), BudgetUsage(999_999, 999), False),
        (BudgetLimit(None, 1_000), BudgetUsage(0, 1_000), True),
        (BudgetLimit(100, 1_000), BudgetUsage(1, 1_001), True),
        (BudgetLimit(0, None), BudgetUsage(0, 0), True),
        (BudgetLimit(None, None), BudgetUsage(10**9, 10**12), False),
    ],
)
def test_a_limit_is_reached_when_requests_or_tokens_meet_it(
    limit: BudgetLimit, usage: BudgetUsage, reached: bool
) -> None:
    assert limit.reached_by(usage) is reached
    status = BudgetStatus(LlmProvider.GEMINI, date(2026, 11, 7), usage, limit)
    assert status.exhausted is reached

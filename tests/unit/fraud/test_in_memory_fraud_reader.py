from datetime import datetime, timedelta

import pytest
from support.fraud_builders import CLAIMED_AT, claim_history, prior_claim

from deliverius.fraud.features import MAX_ROWS_PER_WINDOW
from deliverius.fraud.in_memory import InMemoryFraudContextReader
from deliverius.shared.access import CustomerScope
from deliverius.shared.errors import InvariantError, NotFoundError
from deliverius.shared.ids import CustomerId, SandboxId, new_id

SCOPE = CustomerScope(SandboxId(new_id()), CustomerId(new_id()))


async def test_the_reader_returns_only_the_90_days_before_the_claim_newest_first() -> None:
    reader = InMemoryFraudContextReader()
    reader.add(
        SCOPE,
        claim_history(
            order_days_before=(100, 10, 1, 0, -1),
            claims=(prior_claim(95), prior_claim(5), prior_claim(30), prior_claim(0)),
        ),
    )

    history = await reader.history(SCOPE, CLAIMED_AT)

    assert history.order_placed_at == (
        CLAIMED_AT - timedelta(days=1),
        CLAIMED_AT - timedelta(days=10),
    )
    assert [CLAIMED_AT - claim.at for claim in history.claims] == [
        timedelta(days=5),
        timedelta(days=30),
    ]


async def test_the_reader_stops_at_the_row_ceiling() -> None:
    reader = InMemoryFraudContextReader()
    many = tuple(1 + day / 2_000 for day in range(MAX_ROWS_PER_WINDOW + 5))
    reader.add(SCOPE, claim_history(order_days_before=many, claims=tuple(map(prior_claim, many))))

    history = await reader.history(SCOPE, CLAIMED_AT)

    assert len(history.order_placed_at) == MAX_ROWS_PER_WINDOW
    assert len(history.claims) == MAX_ROWS_PER_WINDOW


async def test_another_customer_is_not_found() -> None:
    reader = InMemoryFraudContextReader()
    reader.add(SCOPE, claim_history())
    other = CustomerScope(SCOPE.sandbox_id, CustomerId(new_id()))

    with pytest.raises(NotFoundError):
        await reader.history(other, CLAIMED_AT)


async def test_the_claim_instant_needs_a_time_zone() -> None:
    with pytest.raises(InvariantError):
        await InMemoryFraudContextReader().history(SCOPE, datetime(2026, 10, 1))  # noqa: DTZ001

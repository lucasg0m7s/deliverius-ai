from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from deliverius.shared.clock import FixedClock, SystemClock, require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import (
    ActionId,
    ApprovalRequestId,
    ConversationId,
    HandoffId,
    ReviewerId,
    new_id,
)


def test_new_id_is_a_standard_uuid_version_7() -> None:
    generated = new_id()

    assert type(generated) is UUID
    assert generated.version == 7


def test_ids_generated_in_sequence_are_increasing() -> None:
    ids = [new_id() for _ in range(1_000)]

    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_system_clock_is_utc() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_fixed_clock_stays_put_until_advanced() -> None:
    start = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    clock = FixedClock(start)

    assert clock.now() == start
    assert clock.now() == start

    clock.advance(timedelta(minutes=45))

    assert clock.now() == start + timedelta(minutes=45)


def test_fixed_clock_normalizes_to_utc() -> None:
    sao_paulo = timezone(timedelta(hours=-3))

    clock = FixedClock(datetime(2026, 11, 1, 9, 0, tzinfo=sao_paulo))

    assert clock.now().tzinfo is UTC
    assert clock.now().hour == 12


def test_fixed_clock_rejects_naive_start() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.start"):
        FixedClock(datetime(2026, 11, 1, 12, 0))  # noqa: DTZ001 - the naive value is the point


def test_require_aware_names_the_field() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.placed_at"):
        require_aware(datetime(2026, 11, 1), "placed_at")  # noqa: DTZ001


@pytest.mark.parametrize(
    "id_type",
    [ConversationId, ActionId, ApprovalRequestId, HandoffId, ReviewerId],
    ids=lambda id_type: id_type.__name__,
)
def test_new_entity_ids_wrap_a_plain_uuid(id_type: Callable[[UUID], UUID]) -> None:
    raw = new_id()

    assert id_type(raw) == raw
    assert type(id_type(raw)) is UUID

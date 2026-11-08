from datetime import UTC, datetime, timedelta
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st
from support.datagen_configs import MIXED_CONFIG

from deliverius.datagen import ClaimsDataset, DatagenConfig, split_claims_by_time
from deliverius.datagen.records import ClaimRecord, DecisionKind, ResolutionRecord
from deliverius.policy import ActionKind, ClaimReason

CUTOFF = datetime(2026, 10, 1, tzinfo=UTC)
# Hypothesis wants naive bounds when it is given the time zone separately.
instants = st.datetimes(
    min_value=datetime(2026, 8, 1),  # noqa: DTZ001
    max_value=datetime(2026, 11, 30),  # noqa: DTZ001
    timezones=st.just(UTC),
)
near_the_cutoff = st.builds(
    lambda seconds: CUTOFF + timedelta(seconds=seconds), st.integers(min_value=-5, max_value=5)
)


TEMPLATE = ClaimRecord(
    id=UUID(int=1),
    order_id=UUID(int=2),
    customer_id=UUID(int=3),
    claimed_at=CUTOFF,
    reason=ClaimReason.NOT_DELIVERED,
    action=ActionKind.REFUND,
    claimed_items=(),
    requested_amount_cents=1_000,
    resolution=ResolutionRecord(decision=DecisionKind.ALLOWED),
    is_fraud=False,
)


def claims_at(template: ClaimRecord, moments: list[datetime]) -> list[ClaimRecord]:
    return [template.model_copy(update={"claimed_at": moment}) for moment in moments]


@given(data=st.data(), moments=st.lists(st.one_of(instants, near_the_cutoff), max_size=60))
def test_nothing_in_train_is_at_or_after_the_cutoff_and_nothing_in_test_before_it(
    data: st.DataObject, moments: list[datetime]
) -> None:
    cutoff = data.draw(st.one_of(st.just(CUTOFF), instants))

    split = split_claims_by_time(claims_at(TEMPLATE, moments), cutoff)

    assert all(claim.claimed_at < cutoff for claim in split.train)
    assert all(claim.claimed_at >= cutoff for claim in split.test)
    assert len(split.train) + len(split.test) == len(moments)


@given(moments=st.lists(near_the_cutoff, min_size=1, max_size=20))
def test_a_claim_exactly_at_the_cutoff_goes_to_the_test(moments: list[datetime]) -> None:
    split = split_claims_by_time(claims_at(TEMPLATE, moments), CUTOFF)

    assert [c.claimed_at for c in split.test] == [m for m in moments if m >= CUTOFF]
    assert [c.claimed_at for c in split.train] == [m for m in moments if m < CUTOFF]


def test_the_split_keeps_the_order_of_the_claims(mixed_claims: ClaimsDataset) -> None:
    split = split_claims_by_time(mixed_claims.claims, CUTOFF)

    assert split.train + split.test == mixed_claims.claims


def test_the_generated_history_is_cut_at_the_start_of_the_last_month(
    mixed_claims: ClaimsDataset,
) -> None:
    split = split_claims_by_time(mixed_claims.claims, MIXED_CONFIG.fraud_test_cutoff)

    assert MIXED_CONFIG.fraud_test_cutoff == CUTOFF
    assert split.train
    assert split.test
    assert max(claim.claimed_at for claim in split.train) < CUTOFF
    assert min(claim.claimed_at for claim in split.test) >= CUTOFF
    assert len(split.test) < len(split.train)


def test_the_cutoff_follows_the_reference_instant() -> None:
    config = DatagenConfig(reference_instant=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))

    assert config.fraud_test_cutoff == datetime(2026, 9, 1, tzinfo=UTC)

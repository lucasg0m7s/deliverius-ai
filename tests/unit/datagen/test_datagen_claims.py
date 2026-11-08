import json
from collections import Counter, defaultdict
from datetime import timedelta
from uuid import UUID

import pytest
from support.datagen_configs import MIXED_CONFIG

from deliverius.datagen import ClaimsDataset, DatagenConfig, World, generate_claims, generate_world
from deliverius.datagen.config import ClaimRates, IncidentRates, ProfileMix
from deliverius.datagen.records import (
    ClaimRecord,
    DecisionKind,
    GroundTruthRecord,
    IncidentKind,
    Profile,
)
from deliverius.datagen.world import SimOrder
from deliverius.policy import ClaimReason, DenialReason

REASON_OF_INCIDENT = {
    IncidentKind.MISSING_ITEM: ClaimReason.ITEM_MISSING,
    IncidentKind.WRONG_ITEM: ClaimReason.WRONG_ITEM,
    IncidentKind.QUALITY_ISSUE: ClaimReason.QUALITY_ISSUE,
    IncidentKind.MINOR_DELAY: ClaimReason.LATE_DELIVERY,
    IncidentKind.MAJOR_DELAY: ClaimReason.LATE_DELIVERY,
    IncidentKind.DELIVERY_FAILED: ClaimReason.NOT_DELIVERED,
    IncidentKind.MARKED_DELIVERED_NOT_RECEIVED: ClaimReason.NOT_DELIVERED,
    IncidentKind.DUPLICATE_CHARGE: ClaimReason.DUPLICATE_CHARGE,
    IncidentKind.CUSTOMER_CANCELLATION: ClaimReason.CUSTOMER_CANCELLATION,
}
FRAUD_AND_NOISE_COLUMNS = {"profile", "label_flipped", "incident", "exaggerated"}


def paired(dataset: ClaimsDataset) -> list[tuple[ClaimRecord, GroundTruthRecord]]:
    return list(zip(dataset.claims, dataset.ground_truth, strict=True))


def orders_by_id(world: World) -> dict[UUID, SimOrder]:
    return {sim.order.id: sim for sim in world.orders}


def true_label(truth: GroundTruthRecord) -> bool:
    return truth.incident is None or truth.exaggerated


def test_every_claim_has_its_ground_truth_row(mixed_claims: ClaimsDataset) -> None:
    assert len(mixed_claims.claims) > 500
    assert [claim.id for claim in mixed_claims.claims] == [
        truth.claim_id for truth in mixed_claims.ground_truth
    ]
    assert len({claim.id for claim in mixed_claims.claims}) == len(mixed_claims.claims)


def test_claims_are_in_time_order_with_ids_that_follow_it(mixed_claims: ClaimsDataset) -> None:
    moments = [claim.claimed_at for claim in mixed_claims.claims]
    ids = [claim.id for claim in mixed_claims.claims]

    assert moments == sorted(moments)
    assert ids == sorted(ids)
    assert {identifier.version for identifier in ids} == {7}


def test_claims_come_after_the_order_and_inside_the_period(
    mixed_world: World, mixed_claims: ClaimsDataset
) -> None:
    orders = orders_by_id(mixed_world)

    for claim in mixed_claims.claims:
        assert orders[claim.order_id].order.placed_at < claim.claimed_at
        assert claim.claimed_at <= MIXED_CONFIG.reference_instant
        assert orders[claim.order_id].order.customer_id == claim.customer_id


def test_a_fraudster_who_invents_an_incident_is_labeled_fraud(
    mixed_world: World, mixed_claims: ClaimsDataset
) -> None:
    orders = orders_by_id(mixed_world)
    invented = [
        (claim, truth)
        for claim, truth in paired(mixed_claims)
        if truth.profile is Profile.FRAUDSTER and truth.incident is None
    ]

    assert len(invented) > 50
    for claim, truth in invented:
        happened = {REASON_OF_INCIDENT[i.kind] for i in orders[claim.order_id].incidents}
        assert claim.reason not in happened
        assert claim.is_fraud != truth.label_flipped


def test_an_honest_customer_with_a_real_incident_is_never_fraud(
    mixed_claims: ClaimsDataset,
) -> None:
    honest = [(c, t) for c, t in paired(mixed_claims) if t.profile is Profile.HONEST]

    assert len(honest) > 100
    for claim, truth in honest:
        assert truth.incident is not None
        assert not truth.exaggerated
        assert claim.is_fraud == truth.label_flipped


def test_a_real_incident_claim_matches_what_happened_on_the_order(
    mixed_world: World, mixed_claims: ClaimsDataset
) -> None:
    orders = orders_by_id(mixed_world)

    for claim, truth in paired(mixed_claims):
        if truth.incident is not None:
            kinds = [incident.kind for incident in orders[claim.order_id].incidents]
            assert truth.incident in kinds
            assert claim.reason is REASON_OF_INCIDENT[truth.incident]


def test_an_opportunist_exaggerates_by_asking_for_more_than_really_happened(
    mixed_world: World, mixed_claims: ClaimsDataset
) -> None:
    orders = orders_by_id(mixed_world)
    exaggerated = [(c, t) for c, t in paired(mixed_claims) if t.exaggerated]

    assert len(exaggerated) > 20
    for claim, truth in exaggerated:
        assert truth.profile is Profile.OPPORTUNIST
        assert truth.incident is not None
        if claim.reason is ClaimReason.LATE_DELIVERY:
            assert truth.incident is IncidentKind.MINOR_DELAY
            assert claim.requested_amount_cents == 2_000
            continue
        real = next(i for i in orders[claim.order_id].incidents if i.kind is truth.incident)
        claimed_units = sum(item.quantity for item in claim.claimed_items)
        assert claimed_units > real.quantity


def test_only_opportunists_and_fraudsters_claim_what_did_not_happen(
    mixed_claims: ClaimsDataset,
) -> None:
    for _, truth in paired(mixed_claims):
        if truth.incident is None or truth.exaggerated:
            assert truth.profile is not Profile.HONEST


def test_every_label_is_the_construction_truth_unless_it_was_flipped(
    mixed_claims: ClaimsDataset,
) -> None:
    for claim, truth in paired(mixed_claims):
        assert claim.is_fraud == (true_label(truth) != truth.label_flipped)


def test_nobody_invents_a_claim_on_an_order_where_everything_already_went_wrong() -> None:
    everything_goes_wrong = IncidentRates(
        customer_cancellation=0.0,
        delivery_failed=0.0,
        missing_item=1.0,
        wrong_item=1.0,
        quality_issue=1.0,
        major_delay=1.0,
        marked_delivered_not_received=1.0,
        duplicate_charge=1.0,
    )
    config = DatagenConfig(
        customer_count=20,
        order_count=100,
        profile_mix=ProfileMix(honest=0.0, opportunist=0.0, fraudster=1.0),
        incident_rates=everything_goes_wrong,
        claim_rates=ClaimRates(fraudster_invents=1.0),
    )

    dataset = generate_claims(generate_world(config), config)

    assert dataset.claims
    assert all(truth.incident is not None for truth in dataset.ground_truth)


def test_the_hard_cases_exist(mixed_claims: ClaimsDataset) -> None:
    fraudsters_with_real_incidents = [
        t for t in mixed_claims.ground_truth if t.profile is Profile.FRAUDSTER and t.incident
    ]
    real_claims_per_honest = Counter(
        t.customer_id for t in mixed_claims.ground_truth if t.profile is Profile.HONEST
    )

    assert fraudsters_with_real_incidents
    assert max(real_claims_per_honest.values()) >= 4


def test_the_flipped_share_is_exactly_the_configured_one(mixed_claims: ClaimsDataset) -> None:
    flipped = sum(truth.label_flipped for truth in mixed_claims.ground_truth)
    total = len(mixed_claims.claims)

    assert flipped == round(total * MIXED_CONFIG.claim_rates.label_noise)
    assert mixed_claims.noise_fraction == flipped / total
    assert 0.02 <= mixed_claims.noise_fraction <= 0.03


def test_the_dataset_reports_the_fraud_rate_of_the_labels(mixed_claims: ClaimsDataset) -> None:
    labeled = sum(claim.is_fraud for claim in mixed_claims.claims)

    assert mixed_claims.fraud_rate == labeled / len(mixed_claims.claims)
    assert 0.05 < mixed_claims.fraud_rate < 0.6


def test_flipped_labels_are_spread_over_both_classes(mixed_claims: ClaimsDataset) -> None:
    flipped_to = Counter(
        claim.is_fraud for claim, truth in paired(mixed_claims) if truth.label_flipped
    )

    assert flipped_to[True] > 0
    assert flipped_to[False] > 0


def test_the_training_row_has_neither_profile_nor_flip_mark(mixed_claims: ClaimsDataset) -> None:
    assert not FRAUD_AND_NOISE_COLUMNS & set(ClaimRecord.model_fields)
    for claim in mixed_claims.claims[:200]:
        assert not FRAUD_AND_NOISE_COLUMNS & set(json.loads(claim.model_dump_json()))
    for claim in mixed_claims.claims[:200]:
        assert "reviewer" not in claim.model_dump_json()


def test_some_claims_arrive_after_the_window_and_are_denied_for_it(
    mixed_claims: ClaimsDataset,
) -> None:
    expired = [
        claim
        for claim in mixed_claims.claims
        if claim.resolution.denial_reason is DenialReason.CLAIM_WINDOW_EXPIRED
    ]
    decisions = Counter(claim.resolution.decision for claim in mixed_claims.claims)

    assert len(expired) / len(mixed_claims.claims) == pytest.approx(0.05, abs=0.03)
    assert set(decisions) == set(DecisionKind)


def test_customer_cancellations_are_requested_before_the_order_is_cancelled(
    mixed_world: World, mixed_claims: ClaimsDataset
) -> None:
    orders = orders_by_id(mixed_world)
    cancellations = [
        c for c in mixed_claims.claims if c.reason is ClaimReason.CUSTOMER_CANCELLATION
    ]

    assert cancellations
    for claim in cancellations:
        assert claim.claimed_at == orders[claim.order_id].order.events[-1].occurred_at - timedelta(
            seconds=30
        )


def test_an_item_claimed_again_is_denied_because_the_first_claim_was_remembered(
    mixed_claims: ClaimsDataset,
) -> None:
    earlier: dict[tuple[UUID, UUID], list[ClaimRecord]] = defaultdict(list)
    repeated = []
    for claim in mixed_claims.claims:
        if claim.resolution.denial_reason is DenialReason.ITEM_ALREADY_CLAIMED:
            repeated.append(claim)
        for item in claim.claimed_items:
            earlier[(claim.order_id, item.order_item_id)].append(claim)

    assert repeated
    for claim in repeated:
        first_claims = [
            other
            for item in claim.claimed_items
            for other in earlier[(claim.order_id, item.order_item_id)]
            if other.claimed_at < claim.claimed_at
            and other.resolution.decision is not DecisionKind.DENIED
        ]
        assert first_claims


def test_claims_are_the_same_twice_and_change_with_the_seed() -> None:
    config = MIXED_CONFIG.model_copy(update={"seed": 3, "customer_count": 60, "order_count": 800})
    world = generate_world(config)

    first, second = generate_claims(world, config), generate_claims(world, config)
    other_config = config.model_copy(update={"seed": 4})
    other = generate_claims(generate_world(other_config), other_config)

    assert first == second
    assert first.claims != other.claims

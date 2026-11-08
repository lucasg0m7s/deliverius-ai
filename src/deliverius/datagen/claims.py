"""Layer 2: who complains about what, and the fraud label that follows from it.

The label is true by construction: a claim is fraud when it asks for something that did not
happen (invented from nothing, or inflated past the real incident). Then a small, exact share of
labels is flipped on purpose. The customer profile and the flip mark go to `ground_truth`, never
into the claim rows.
"""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

import structlog

from deliverius.datagen.config import DatagenConfig
from deliverius.datagen.records import ClaimRecord, GroundTruthRecord, IncidentKind, Profile
from deliverius.datagen.resolution import (
    ClaimRequest,
    Resolver,
    claimed_item_records,
    resolution_record,
)
from deliverius.datagen.rng import IdFactory, Layer, count_for_rate, layer_random
from deliverius.datagen.world import Incident, SimOrder, World
from deliverius.orders import Order, OrderStatus
from deliverius.policy import POLICY_LIMITS, ClaimedItem, ClaimReason

logger = structlog.get_logger(__name__)

_REASON_OF: Final[Mapping[IncidentKind, ClaimReason]] = MappingProxyType(
    {
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
)
_ITEM_REASONS: Final = frozenset(
    {ClaimReason.ITEM_MISSING, ClaimReason.WRONG_ITEM, ClaimReason.QUALITY_ISSUE}
)
_EXAGGERABLE: Final = frozenset(
    {
        IncidentKind.MISSING_ITEM,
        IncidentKind.WRONG_ITEM,
        IncidentKind.QUALITY_ISSUE,
        IncidentKind.MINOR_DELAY,
    }
)
# What a made-up claim is about, and how often.
_INVENTED_WEIGHTS: Final = (
    (ClaimReason.ITEM_MISSING, 35),
    (ClaimReason.NOT_DELIVERED, 20),
    (ClaimReason.QUALITY_ISSUE, 20),
    (ClaimReason.WRONG_ITEM, 15),
    (ClaimReason.LATE_DELIVERY, 5),
    (ClaimReason.DUPLICATE_CHARGE, 5),
)
_MAX_CLAIM_DELAY: Final = timedelta(hours=72)
_COMPLAINT_DELAY_SECONDS: Final = (300, 4 * 3_600)
_FAILED_DELAY_SECONDS: Final = (600, 6 * 3_600)
_MARKED_DELIVERED_DELAY_SECONDS: Final = (1_200, 12 * 3_600)
_DUPLICATE_DELAY_SECONDS: Final = (3_600, 72 * 3_600)
_CANCELLATION_LEAD: Final = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class ClaimsDataset:
    claims: tuple[ClaimRecord, ...]
    ground_truth: tuple[GroundTruthRecord, ...]
    noise_fraction: float
    fraud_rate: float


@dataclass(frozen=True, slots=True)
class _Draft:
    request: ClaimRequest
    profile: Profile
    incident: IncidentKind | None
    exaggerated: bool

    @property
    def is_fraud_before_noise(self) -> bool:
        return self.incident is None or self.exaggerated


def generate_claims(world: World, config: DatagenConfig) -> ClaimsDataset:
    rng = layer_random(config.seed, Layer.CLAIMS)
    profiles = {sim.customer.id: sim.profile for sim in world.customers}
    drafts = [
        draft
        for sim in world.orders
        for draft in _drafts_for(rng, config, sim, profiles[sim.order.customer_id])
    ]
    drafts.sort(key=lambda draft: draft.request.claimed_at)
    flipped = _choose_flipped(config, len(drafts))
    resolver = Resolver(
        layer_random(config.seed, Layer.RESOLUTION),
        reviewer_rejects_fraud=config.claim_rates.reviewer_rejects_fraud,
    )
    ids = IdFactory(rng)
    claims: list[ClaimRecord] = []
    truth: list[GroundTruthRecord] = []
    for index, draft in enumerate(drafts):
        request = draft.request
        resolution = resolver.resolve(request, is_fraud=draft.is_fraud_before_noise)
        claim_id = ids.next_id(request.claimed_at)
        label_flipped = index in flipped
        claims.append(
            ClaimRecord(
                id=claim_id,
                order_id=request.order.id,
                customer_id=request.customer_id,
                claimed_at=request.claimed_at,
                reason=request.reason,
                action=resolution.proposal.kind,
                claimed_items=claimed_item_records(request.items),
                requested_amount_cents=resolution.proposal.amount.cents,
                resolution=resolution_record(resolution.decision),
                is_fraud=draft.is_fraud_before_noise != label_flipped,
            )
        )
        truth.append(
            GroundTruthRecord(
                claim_id=claim_id,
                customer_id=request.customer_id,
                profile=draft.profile,
                label_flipped=label_flipped,
                incident=draft.incident,
                exaggerated=draft.exaggerated,
            )
        )
    dataset = _summarize(claims, len(flipped), truth)
    logger.info(
        "datagen.claims_generated",
        claims=len(dataset.claims),
        flipped=len(flipped),
        fraud_rate=round(dataset.fraud_rate, 4),
    )
    return dataset


def _summarize(
    claims: Sequence[ClaimRecord], flipped: int, truth: Sequence[GroundTruthRecord]
) -> ClaimsDataset:
    total = len(claims)
    return ClaimsDataset(
        claims=tuple(claims),
        ground_truth=tuple(truth),
        noise_fraction=flipped / total if total else 0.0,
        fraud_rate=sum(claim.is_fraud for claim in claims) / total if total else 0.0,
    )


def _choose_flipped(config: DatagenConfig, total: int) -> frozenset[int]:
    """An exact share of the claims, so the noise fraction is the configured one, not a draw."""
    rng = layer_random(config.seed, Layer.LABEL_NOISE)
    return frozenset(
        rng.sample(range(total), count_for_rate(total, config.claim_rates.label_noise))
    )


def _drafts_for(
    rng: random.Random, config: DatagenConfig, sim: SimOrder, profile: Profile
) -> list[_Draft]:
    rates = config.claim_rates
    drafts: list[_Draft] = []
    for incident in sim.incidents:
        always = incident.kind is IncidentKind.CUSTOMER_CANCELLATION
        if always or rng.random() < rates.honest_reports_incident:
            drafts.append(_real_draft(rng, config, sim, profile, incident))
    invention_rate = {
        Profile.FRAUDSTER: rates.fraudster_invents,
        Profile.OPPORTUNIST: rates.opportunist_invents,
    }.get(profile, 0.0)
    if sim.order.status is OrderStatus.DELIVERED and rng.random() < invention_rate:
        drafts += _invented_draft(rng, config, sim, profile)
    return drafts


def _real_draft(
    rng: random.Random, config: DatagenConfig, sim: SimOrder, profile: Profile, incident: Incident
) -> _Draft:
    order, kind = sim.order, incident.kind
    items: tuple[ClaimedItem, ...] = ()
    if incident.item_id is not None:
        items = (ClaimedItem(incident.item_id, incident.quantity),)
    asks_for_more = False
    exaggerated = False
    if (
        profile is Profile.OPPORTUNIST
        and kind in _EXAGGERABLE
        and rng.random() < config.claim_rates.opportunist_exaggerates
    ):
        if kind is IncidentKind.MINOR_DELAY:
            asks_for_more = exaggerated = True
        elif (inflated := _inflate(rng, order, items)) is not None:
            items, exaggerated = inflated, True
    reason = _REASON_OF[kind]
    request = ClaimRequest(
        order=order,
        customer_id=order.customer_id,
        reason=reason,
        items=items,
        claimed_at=_claimed_at(rng, config, order, reason, kind),
        asks_for_more=asks_for_more,
    )
    return _Draft(request, profile, kind, exaggerated)


def _invented_draft(
    rng: random.Random, config: DatagenConfig, sim: SimOrder, profile: Profile
) -> list[_Draft]:
    order = sim.order
    happened = {_REASON_OF[incident.kind] for incident in sim.incidents}
    options = [(reason, weight) for reason, weight in _INVENTED_WEIGHTS if reason not in happened]
    if not options:
        return []
    reason = rng.choices([r for r, _ in options], weights=[w for _, w in options])[0]
    items: tuple[ClaimedItem, ...] = ()
    if reason in _ITEM_REASONS:
        item = rng.choice(order.items)
        items = (ClaimedItem(item.id, rng.randint(1, item.quantity)),)
    request = ClaimRequest(
        order=order,
        customer_id=order.customer_id,
        reason=reason,
        items=items,
        claimed_at=_claimed_at(rng, config, order, reason, None),
    )
    return [_Draft(request, profile, None, exaggerated=False)]


def _inflate(
    rng: random.Random, order: Order, items: tuple[ClaimedItem, ...]
) -> tuple[ClaimedItem, ...] | None:
    """Ask for more units or one more item than really had a problem, within what was ordered."""
    claimed = items[0]
    options: list[tuple[ClaimedItem, ...]] = []
    for item in order.items:
        if item.id == claimed.order_item_id and claimed.quantity < item.quantity:
            options.append((ClaimedItem(item.id, item.quantity),))
        elif item.id != claimed.order_item_id:
            options.append((claimed, ClaimedItem(item.id, rng.randint(1, item.quantity))))
    return rng.choice(options) if options else None


def _claimed_at(
    rng: random.Random,
    config: DatagenConfig,
    order: Order,
    reason: ClaimReason,
    kind: IncidentKind | None,
) -> datetime:
    if reason is ClaimReason.CUSTOMER_CANCELLATION:
        return order.events[-1].occurred_at - _CANCELLATION_LEAD
    if reason is ClaimReason.DUPLICATE_CHARGE:
        return order.placed_at + timedelta(seconds=rng.randint(*_DUPLICATE_DELAY_SECONDS))
    anchor = order.events[-1].occurred_at
    window = _window(reason)
    if rng.random() < config.claim_rates.late_claim_share:
        low, high = (
            int((window + timedelta(hours=1)).total_seconds()),
            int(_MAX_CLAIM_DELAY.total_seconds()),
        )
    elif kind is IncidentKind.DELIVERY_FAILED:
        low, high = _FAILED_DELAY_SECONDS
    elif reason is ClaimReason.NOT_DELIVERED:
        low, high = _MARKED_DELIVERED_DELAY_SECONDS
    else:
        low, high = _COMPLAINT_DELAY_SECONDS
    return anchor + timedelta(seconds=rng.randint(low, high))


def _window(reason: ClaimReason) -> timedelta:
    if reason is ClaimReason.NOT_DELIVERED:
        return POLICY_LIMITS.not_delivered_claim_window
    if reason is ClaimReason.LATE_DELIVERY:
        return POLICY_LIMITS.late_claim_window
    return POLICY_LIMITS.item_claim_window

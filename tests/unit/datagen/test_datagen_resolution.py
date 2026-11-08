import ast
import random
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from support.order_builders import BASE_TIME, build_item, build_order_in

from deliverius.datagen.records import DecisionKind
from deliverius.datagen.resolution import (
    ClaimRequest,
    Resolver,
    order_as_of,
    resolution_record,
)
from deliverius.orders import Order, OrderStatus
from deliverius.policy import (
    ActionKind,
    Allowed,
    ApprovalReason,
    ClaimedItem,
    ClaimReason,
    DenialReason,
    Denied,
    NeedsApproval,
    PolicyDocumentId,
)
from deliverius.shared import CustomerId, Money, new_id

CUSTOMER = CustomerId(new_id())
DATAGEN_ROOT = Path(__file__).resolve().parents[3] / "src" / "deliverius" / "datagen"
PUBLIC_API_ONLY = (
    "deliverius.policy",
    "deliverius.orders",
    "deliverius.shared",
    "deliverius.privacy",
)


def resolver(*, rejects_fraud: float = 0.0) -> Resolver:
    return Resolver(random.Random(1), reviewer_rejects_fraud=rejects_fraud)  # noqa: S311


def delivered(*items_cents: int, final_event_at_minutes: int = 40, **kwargs: Money) -> Order:
    items = tuple(build_item(f"Item {n}", cents) for n, cents in enumerate(items_cents))
    return build_order_in(
        OrderStatus.DELIVERED,
        items,
        final_event_at=BASE_TIME + timedelta(minutes=final_event_at_minutes),
        customer_id=CUSTOMER,
        **kwargs,
    )


def request(
    order: Order,
    reason: ClaimReason,
    minutes_after_delivery: int,
    *items: ClaimedItem,
    asks_for_more: bool = False,
) -> ClaimRequest:
    assert order.delivered_at is not None
    return ClaimRequest(
        order=order,
        customer_id=CUSTOMER,
        reason=reason,
        items=items,
        claimed_at=order.delivered_at + timedelta(minutes=minutes_after_delivery),
        asks_for_more=asks_for_more,
    )


def test_a_missing_soda_of_eight_reais_after_twenty_minutes_is_refunded_automatically() -> None:
    order = delivered(800, 4_500)
    soda = ClaimedItem(order.items[0].id, 1)

    resolution = resolver().resolve(
        request(order, ClaimReason.ITEM_MISSING, 20, soda), is_fraud=False
    )

    assert resolution.proposal.kind is ActionKind.REFUND
    assert resolution.proposal.amount == Money(800)
    assert resolution.decision == Allowed(Money(800), PolicyDocumentId.POL_03)
    record = resolution_record(resolution.decision)
    assert record.decision is DecisionKind.ALLOWED
    assert record.document is PolicyDocumentId.POL_03
    assert record.ceiling_cents == 800


def test_the_same_claim_thirty_hours_after_delivery_is_denied_for_the_deadline() -> None:
    order = delivered(800, 4_500)
    soda = ClaimedItem(order.items[0].id, 1)

    resolution = resolver().resolve(
        request(order, ClaimReason.ITEM_MISSING, 30 * 60, soda), is_fraud=False
    )

    assert resolution.decision == Denied(
        DenialReason.CLAIM_WINDOW_EXPIRED, None, PolicyDocumentId.POL_03
    )
    record = resolution_record(resolution.decision)
    assert record.decision is DecisionKind.DENIED
    assert record.denial_reason is DenialReason.CLAIM_WINDOW_EXPIRED


def test_a_second_claim_for_the_same_item_sees_the_first_one() -> None:
    order = delivered(800, 4_500)
    soda = ClaimedItem(order.items[0].id, 1)
    sim = resolver()

    first = sim.resolve(request(order, ClaimReason.ITEM_MISSING, 20, soda), is_fraud=False)
    second = sim.resolve(request(order, ClaimReason.WRONG_ITEM, 40, soda), is_fraud=False)

    assert isinstance(first.decision, Allowed)
    assert isinstance(second.decision, Denied)
    assert second.decision.reason is DenialReason.ITEM_ALREADY_CLAIMED


def test_a_denied_claim_leaves_no_trace_in_the_history() -> None:
    order = delivered(800, 4_500)
    soda = ClaimedItem(order.items[0].id, 1)
    sim = resolver()

    sim.resolve(request(order, ClaimReason.ITEM_MISSING, 30 * 60, soda), is_fraud=False)
    again = sim.resolve(request(order, ClaimReason.ITEM_MISSING, 20, soda), is_fraud=False)

    assert isinstance(again.decision, Allowed)


def test_auto_approved_money_across_orders_counts_toward_the_customer_limit() -> None:
    sim = resolver()
    decisions = []
    for _ in range(3):
        order = delivered(4_000)
        item = ClaimedItem(order.items[0].id, 1)
        decisions.append(
            sim.resolve(request(order, ClaimReason.ITEM_MISSING, 10, item), is_fraud=False)
        )

    assert isinstance(decisions[0].decision, Allowed)
    assert isinstance(decisions[1].decision, Allowed)
    assert isinstance(decisions[2].decision, NeedsApproval)
    assert decisions[2].decision.reasons == (ApprovalReason.EXCEEDS_CUSTOMER_LIMIT,)


def test_the_history_only_counts_the_customers_own_actions() -> None:
    sim = resolver()
    for _ in range(2):
        order = delivered(4_000)
        item = ClaimedItem(order.items[0].id, 1)
        sim.resolve(request(order, ClaimReason.ITEM_MISSING, 10, item), is_fraud=False)
    stranger_order = delivered(4_000)
    stranger_item = ClaimedItem(stranger_order.items[0].id, 1)
    stranger = replace(
        request(stranger_order, ClaimReason.ITEM_MISSING, 10, stranger_item),
        customer_id=CustomerId(new_id()),
    )

    assert isinstance(sim.resolve(stranger, is_fraud=False).decision, Allowed)


def test_a_reviewer_executes_an_honest_case_so_it_counts_as_already_refunded() -> None:
    order = delivered(3_000)
    sim = resolver()

    first = sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 60), is_fraud=False)
    second = sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 120), is_fraud=False)

    assert isinstance(first.decision, NeedsApproval)
    assert first.decision.reasons == (ApprovalReason.MARKED_AS_DELIVERED,)
    assert first.proposal.amount == order.total
    assert second.proposal.amount == Money.zero()
    assert isinstance(second.decision, Denied)
    assert second.decision.reason is DenialReason.AMOUNT_NOT_POSITIVE


def test_a_reviewer_who_rejects_a_fraud_leaves_nothing_executed() -> None:
    order = delivered(3_000)
    sim = resolver(rejects_fraud=1.0)

    sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 60), is_fraud=True)
    second = sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 120), is_fraud=True)

    assert second.proposal.amount == order.total
    assert isinstance(second.decision, NeedsApproval)


def test_a_reviewer_who_misses_a_fraud_executes_it() -> None:
    order = delivered(3_000)
    sim = resolver(rejects_fraud=0.0)

    sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 60), is_fraud=True)
    second = sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 120), is_fraud=True)

    assert second.proposal.amount == Money.zero()


def test_a_reviewer_never_rejects_a_case_that_is_not_fraud() -> None:
    order = delivered(3_000)
    sim = resolver(rejects_fraud=1.0)

    sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 60), is_fraud=False)
    second = sim.resolve(request(order, ClaimReason.NOT_DELIVERED, 120), is_fraud=False)

    assert second.proposal.amount == Money.zero()


@pytest.mark.parametrize(("delay_minutes", "expected_cents"), [(45, 1_500), (90, 2_000)])
def test_a_late_delivery_asks_for_the_coupon_the_delay_justifies(
    delay_minutes: int, expected_cents: int
) -> None:
    order = delivered(20_000, final_event_at_minutes=45 + delay_minutes)

    resolution = resolver().resolve(request(order, ClaimReason.LATE_DELIVERY, 10), is_fraud=False)

    assert resolution.proposal.kind is ActionKind.COUPON
    assert resolution.proposal.amount == Money(expected_cents)
    assert isinstance(resolution.decision, Allowed)


def test_a_minor_delay_coupon_is_ten_percent_of_a_small_order() -> None:
    order = delivered(4_000, final_event_at_minutes=45 + 40)

    resolution = resolver().resolve(request(order, ClaimReason.LATE_DELIVERY, 10), is_fraud=False)

    assert resolution.proposal.amount == order.total.fraction(10, 100)


def test_asking_for_the_big_coupon_on_a_minor_delay_is_denied_for_the_ceiling() -> None:
    order = delivered(20_000, final_event_at_minutes=45 + 40)

    resolution = resolver().resolve(
        request(order, ClaimReason.LATE_DELIVERY, 10, asks_for_more=True), is_fraud=True
    )

    assert resolution.proposal.amount == Money(2_000)
    assert isinstance(resolution.decision, Denied)
    assert resolution.decision.reason is DenialReason.EXCEEDS_CEILING


def test_a_quality_claim_asks_for_half_of_the_item() -> None:
    order = delivered(3_000, 2_000)
    item = ClaimedItem(order.items[0].id, 1)

    resolution = resolver().resolve(
        request(order, ClaimReason.QUALITY_ISSUE, 15, item), is_fraud=False
    )

    assert resolution.proposal.amount == Money(1_500)
    assert resolution.decision == Allowed(Money(1_500), PolicyDocumentId.POL_05)


def test_a_duplicate_charge_asks_for_the_whole_total_and_always_needs_a_person() -> None:
    order = delivered(3_000)

    resolution = resolver().resolve(
        request(order, ClaimReason.DUPLICATE_CHARGE, 15), is_fraud=False
    )

    assert resolution.proposal.amount == order.total
    assert isinstance(resolution.decision, NeedsApproval)
    assert resolution.decision.reasons == (ApprovalReason.DUPLICATE_CHARGE_UNVERIFIED,)


def test_a_cancellation_is_judged_on_the_order_as_it_was_when_asked() -> None:
    placed = build_order_in(
        OrderStatus.CANCELLED,
        (build_item("Pizza", 4_000),),
        customer_id=CUSTOMER,
        final_event_at=BASE_TIME + timedelta(minutes=1),
    )
    asked_at = placed.events[-1].occurred_at - timedelta(seconds=30)

    resolution = resolver().resolve(
        ClaimRequest(placed, CUSTOMER, ClaimReason.CUSTOMER_CANCELLATION, (), asked_at),
        is_fraud=False,
    )

    assert resolution.proposal.kind is ActionKind.CANCELLATION
    assert resolution.proposal.amount == placed.total
    assert isinstance(resolution.decision, Allowed)


def test_a_cancellation_after_the_restaurant_accepted_goes_to_a_reviewer() -> None:
    order = delivered(4_000)
    asked_at = order.events[1].occurred_at + timedelta(minutes=1)
    assert order.events[1].status is OrderStatus.ACCEPTED

    resolution = resolver().resolve(
        ClaimRequest(order, CUSTOMER, ClaimReason.CUSTOMER_CANCELLATION, (), asked_at),
        is_fraud=False,
    )

    assert isinstance(resolution.decision, NeedsApproval)
    assert resolution.decision.reasons == (ApprovalReason.RESTAURANT_ALREADY_ACCEPTED,)


def test_order_as_of_keeps_only_the_events_up_to_the_instant() -> None:
    order = delivered(2_000)
    midway = order.events[2].occurred_at

    snapshot = order_as_of(order, midway)

    assert snapshot.events == order.events[:3]
    assert snapshot.status is OrderStatus.PREPARING
    assert snapshot.delivered_at is None
    assert order_as_of(order, order.delivered_at + timedelta(hours=5)) is order  # type: ignore[operator]


def test_an_unknown_item_becomes_a_denial_not_a_crash() -> None:
    order = delivered(2_000)
    stranger = ClaimedItem(build_item("Outro", 500).id, 1)

    resolution = resolver().resolve(
        request(order, ClaimReason.ITEM_MISSING, 5, stranger), is_fraud=False
    )

    assert isinstance(resolution.decision, Denied)
    assert resolution.decision.reason is DenialReason.AMOUNT_NOT_POSITIVE


def test_the_record_of_a_denial_keeps_the_ceiling_only_when_the_engine_knew_it() -> None:
    order = delivered(20_000, final_event_at_minutes=45 + 40)
    item = ClaimedItem(order.items[0].id, 1)
    sim = resolver()

    over_ceiling = resolution_record(
        sim.resolve(
            request(order, ClaimReason.LATE_DELIVERY, 10, asks_for_more=True), is_fraud=True
        ).decision
    )
    expired = resolution_record(
        sim.resolve(
            request(order, ClaimReason.ITEM_MISSING, 30 * 60, item), is_fraud=False
        ).decision
    )

    assert over_ceiling.denial_reason is DenialReason.EXCEEDS_CEILING
    assert over_ceiling.ceiling_cents == 1_500
    assert expired.denial_reason is DenialReason.CLAIM_WINDOW_EXPIRED
    assert expired.ceiling_cents is None


def test_the_record_of_an_approval_lists_every_reason() -> None:
    order = delivered(8_000)
    resolution = resolver().resolve(request(order, ClaimReason.NOT_DELIVERED, 60), is_fraud=False)

    record = resolution_record(resolution.decision)

    assert record.decision is DecisionKind.NEEDS_APPROVAL
    assert record.approval_reasons == (
        ApprovalReason.MARKED_AS_DELIVERED,
        ApprovalReason.EXCEEDS_ACTION_LIMIT,
    )


def datagen_sources() -> list[Path]:
    return sorted(DATAGEN_ROOT.rglob("*.py"))


@pytest.mark.parametrize("source", datagen_sources(), ids=lambda path: path.name)
def test_datagen_modules_import_the_other_modules_only_through_their_public_api(
    source: Path,
) -> None:
    deep_imports = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        names = (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        deep_imports += [
            name for name in names if any(name.startswith(f"{api}.") for api in PUBLIC_API_ONLY)
        ]

    assert deep_imports == []


def test_the_resolution_module_takes_its_decisions_from_the_policy_engine() -> None:
    imported = {
        alias.name
        for node in ast.walk(
            ast.parse((DATAGEN_ROOT / "resolution.py").read_text(encoding="utf-8"))
        )
        if isinstance(node, ast.ImportFrom) and node.module == "deliverius.policy"
        for alias in node.names
    }

    assert "evaluate" in imported

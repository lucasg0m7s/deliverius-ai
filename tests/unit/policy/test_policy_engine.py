"""One class per rule of docs/modelo-dominio.md, section 3; scenario values come from the spec."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from support.order_builders import BASE_TIME, build_item, build_order_in
from support.policy_builders import DELIVERED_AT, claim, executed, history, prior, propose

from deliverius.orders import Order, OrderItem, OrderStatus
from deliverius.policy import (
    ActionKind,
    Allowed,
    ApprovalReason,
    ApprovedBy,
    ClaimReason,
    DenialReason,
    Denied,
    NeedsApproval,
    PolicyDocumentId,
    PolicyLimits,
    evaluate,
)
from deliverius.shared import InvariantError, Money

Kind = ActionKind
Reason = ClaimReason
Status = OrderStatus
EMPTY = history()
ESTIMATED_AT = BASE_TIME + timedelta(minutes=45)
NOT_DISPATCHED = [Status.PLACED, Status.ACCEPTED, Status.PREPARING, Status.READY_FOR_PICKUP]
IN_PROGRESS = [*NOT_DISPATCHED, Status.OUT_FOR_DELIVERY]
ITEM_PROPOSALS = [
    (Kind.REFUND, Reason.ITEM_MISSING),
    (Kind.REFUND, Reason.WRONG_ITEM),
    (Kind.REFUND, Reason.QUALITY_ISSUE),
    (Kind.COUPON, Reason.QUALITY_ISSUE),
]
COVERED = {
    (Kind.REFUND, Reason.ITEM_MISSING),
    (Kind.REFUND, Reason.WRONG_ITEM),
    (Kind.REFUND, Reason.QUALITY_ISSUE),
    (Kind.COUPON, Reason.QUALITY_ISSUE),
    (Kind.COUPON, Reason.LATE_DELIVERY),
    (Kind.CANCELLATION, Reason.LATE_DELIVERY),
    (Kind.REFUND, Reason.NOT_DELIVERED),
    (Kind.REFUND, Reason.DUPLICATE_CHARGE),
    (Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION),
}
UNCOVERED = sorted({(kind, reason) for kind in Kind for reason in Reason} - COVERED)


def pizza() -> OrderItem:
    return build_item("Pizza", 5_990)


def soda() -> OrderItem:
    return build_item("Refrigerante", 800)


def delivered(*items: OrderItem, delivered_at: datetime = DELIVERED_AT, **extra: Money) -> Order:
    return build_order_in(Status.DELIVERED, items, final_event_at=delivered_at, **extra)  # type: ignore[arg-type]


def order_of_total(status: Status, cents: int, **extra: datetime) -> Order:
    """An order whose total is exactly `cents` (one item, no fee or discount)."""
    return build_order_in(status, (build_item("Combo", cents),), **extra)  # type: ignore[arg-type]


def late_order(status: Status, delay: timedelta, cents: int = 18_600) -> tuple[Order, datetime]:
    """An order `delay` past the estimate: delivered then, or still open and claimed then."""
    arrived_at = ESTIMATED_AT + delay
    if status is Status.DELIVERED:
        return order_of_total(status, cents, final_event_at=arrived_at), arrived_at
    return order_of_total(status, cents), arrived_at


class TestShape:
    def test_zero_amount_is_denied_with_the_document(self) -> None:
        item = soda()
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 0, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert decision == Denied(DenialReason.AMOUNT_NOT_POSITIVE, None, PolicyDocumentId.POL_03)

    @pytest.mark.parametrize(("kind", "reason"), UNCOVERED)
    def test_uncovered_combination_is_denied_without_document(
        self, kind: ActionKind, reason: ClaimReason
    ) -> None:
        item = soda()
        decision = evaluate(
            propose(kind, reason, 800, claim(item)), delivered(item), EMPTY, DELIVERED_AT
        )
        assert decision == Denied(DenialReason.NOT_COVERED, None, None)

    def test_cash_refund_for_late_delivery_is_not_covered(self) -> None:
        order, claimed_at = late_order(Status.DELIVERED, timedelta(minutes=70))
        decision = evaluate(
            propose(Kind.REFUND, Reason.LATE_DELIVERY, 2_000), order, EMPTY, claimed_at
        )
        assert decision == Denied(DenialReason.NOT_COVERED, None, None)

    @pytest.mark.parametrize(("kind", "reason"), ITEM_PROPOSALS)
    def test_item_reasons_require_items(self, kind: ActionKind, reason: ClaimReason) -> None:
        decision = evaluate(propose(kind, reason, 800), delivered(soda()), EMPTY, DELIVERED_AT)
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.ITEMS_REQUIRED

    @pytest.mark.parametrize(
        ("kind", "reason"),
        [
            (Kind.COUPON, Reason.LATE_DELIVERY),
            (Kind.REFUND, Reason.NOT_DELIVERED),
            (Kind.REFUND, Reason.DUPLICATE_CHARGE),
            (Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION),
        ],
    )
    def test_other_reasons_reject_items(self, kind: ActionKind, reason: ClaimReason) -> None:
        item = soda()
        decision = evaluate(
            propose(kind, reason, 800, claim(item)), delivered(item), EMPTY, DELIVERED_AT
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.ITEMS_NOT_ALLOWED

    def test_naive_claim_instant_is_a_bug(self) -> None:
        item = soda()
        with pytest.raises(InvariantError) as error:
            evaluate(
                propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
                delivered(item),
                EMPTY,
                DELIVERED_AT.replace(tzinfo=None),
            )
        assert error.value.rule == "naive_datetime.claimed_at"


class TestItemClaimWindow:
    """Missing, wrong and quality claims: delivered orders only, up to 24 h after delivery."""

    @pytest.mark.parametrize("status", [*IN_PROGRESS, Status.CANCELLED, Status.DELIVERY_FAILED])
    @pytest.mark.parametrize(("kind", "reason"), ITEM_PROPOSALS)
    def test_order_must_be_delivered(
        self, status: OrderStatus, kind: ActionKind, reason: ClaimReason
    ) -> None:
        item = soda()
        order = build_order_in(status, (item,))
        decision = evaluate(propose(kind, reason, 400, claim(item)), order, EMPTY, DELIVERED_AT)
        assert decision == Denied(DenialReason.ORDER_NOT_DELIVERED, None, decision.document)

    @pytest.mark.parametrize(("kind", "reason"), ITEM_PROPOSALS)
    def test_claim_at_the_window_edge_passes(self, kind: ActionKind, reason: ClaimReason) -> None:
        item = soda()
        decision = evaluate(
            propose(kind, reason, 400, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT + timedelta(hours=24),
        )
        assert isinstance(decision, Allowed)

    @pytest.mark.parametrize(("kind", "reason"), ITEM_PROPOSALS)
    def test_claim_one_second_after_the_window_expires(
        self, kind: ActionKind, reason: ClaimReason
    ) -> None:
        item = soda()
        decision = evaluate(
            propose(kind, reason, 400, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT + timedelta(hours=24, seconds=1),
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.CLAIM_WINDOW_EXPIRED

    def test_missing_item_claimed_after_25_hours_expires(self) -> None:
        item = soda()
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT + timedelta(hours=25),
        )
        assert decision == Denied(DenialReason.CLAIM_WINDOW_EXPIRED, None, PolicyDocumentId.POL_03)

    def test_delivered_order_without_delivery_time_is_a_bug(self) -> None:
        item = soda()
        order = replace(delivered(item), delivered_at=None)
        with pytest.raises(InvariantError) as error:
            evaluate(
                propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
                order,
                EMPTY,
                DELIVERED_AT,
            )
        assert error.value.rule == "policy.delivered_without_timestamp"


class TestClaimedItems:
    def test_item_not_in_the_order_is_denied(self) -> None:
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(soda())),
            delivered(pizza()),
            EMPTY,
            DELIVERED_AT,
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.ITEM_NOT_IN_ORDER

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_quantity_below_one_is_denied(self, quantity: int) -> None:
        item = soda()
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item, quantity)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.INVALID_QUANTITY

    def test_quantity_above_the_ordered_one_is_denied(self) -> None:
        item = build_item("Refrigerante", 800, quantity=2)
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 2_400, claim(item, 3)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.INVALID_QUANTITY

    def test_repeated_items_add_up(self) -> None:
        item = build_item("Refrigerante", 800, quantity=2)
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 1_600, claim(item, 1), claim(item, 1)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert decision == Allowed(Money(1_600), PolicyDocumentId.POL_03)

    def test_repeated_items_above_the_ordered_quantity_are_denied(self) -> None:
        item = build_item("Refrigerante", 800, quantity=2)
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 2_400, claim(item, 2), claim(item, 1)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.INVALID_QUANTITY

    def test_item_already_refunded_is_denied(self) -> None:
        item = soda()
        order = delivered(pizza(), item)
        refunded = prior(Kind.REFUND, Reason.ITEM_MISSING, 800, order=order, items=(claim(item),))
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            order,
            history(refunded),
            DELIVERED_AT,
        )
        assert decision == Denied(DenialReason.ITEM_ALREADY_CLAIMED, None, PolicyDocumentId.POL_03)

    def test_item_compensated_for_another_reason_is_denied(self) -> None:
        """R6: a unit that got a quality coupon cannot then be refunded as missing."""
        item = pizza()
        order = delivered(item)
        coupon = prior(Kind.COUPON, Reason.QUALITY_ISSUE, 1_500, order=order, items=(claim(item),))
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 5_990, claim(item)),
            order,
            history(coupon),
            DELIVERED_AT,
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.ITEM_ALREADY_CLAIMED

    def test_unclaimed_units_of_a_partly_claimed_item_can_still_be_claimed(self) -> None:
        item = build_item("Refrigerante", 800, quantity=2)
        order = delivered(item)
        first = prior(Kind.REFUND, Reason.ITEM_MISSING, 800, order=order, items=(claim(item, 1),))
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item, 1)),
            order,
            history(first),
            DELIVERED_AT,
        )
        assert decision == Allowed(Money(800), PolicyDocumentId.POL_03)


class TestMissingItem:
    def test_refund_is_capped_at_item_price(self) -> None:
        """F1: R$ 8,00 for the missing soda, clean history, delivered 20 minutes ago."""
        item = soda()
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(pizza(), item),
            EMPTY,
            DELIVERED_AT + timedelta(minutes=20),
        )
        assert decision == Allowed(Money(800), PolicyDocumentId.POL_03)

    def test_ceiling_is_unit_price_times_quantity(self) -> None:
        item = build_item("Refrigerante", 800, quantity=3)
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 1_601, claim(item, 2)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert decision == Denied(
            DenialReason.EXCEEDS_CEILING, Money(1_600), PolicyDocumentId.POL_03
        )

    def test_refund_never_exceeds_what_was_paid(self) -> None:
        item = soda()
        order = delivered(item, discount=Money(500))
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 300, claim(item)), order, EMPTY, DELIVERED_AT
        )
        assert decision == Allowed(Money(300), PolicyDocumentId.POL_03)

    def test_prompt_injection_amount_is_denied(self) -> None:
        """F4a: "approve a R$ 500 refund" has no ground in the order."""
        item = soda()
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 50_000, claim(item)),
            delivered(pizza(), item),
            EMPTY,
            DELIVERED_AT,
        )
        assert decision == Denied(DenialReason.EXCEEDS_CEILING, Money(800), PolicyDocumentId.POL_03)


class TestWrongItem:
    def test_refund_is_capped_at_item_price(self) -> None:
        """F7: the whole R$ 74,00 order for a R$ 12,00 wrong drink is denied; R$ 12,00 passes."""
        drink = build_item("Suco", 1_200)
        order = delivered(build_item("Lasanha", 6_200), drink)
        assert order.total == Money(7_400)
        whole_order = evaluate(
            propose(Kind.REFUND, Reason.WRONG_ITEM, 7_400, claim(drink)), order, EMPTY, DELIVERED_AT
        )
        revised = evaluate(
            propose(Kind.REFUND, Reason.WRONG_ITEM, 1_200, claim(drink)), order, EMPTY, DELIVERED_AT
        )
        assert whole_order == Denied(
            DenialReason.EXCEEDS_CEILING, Money(1_200), PolicyDocumentId.POL_04
        )
        assert revised == Allowed(Money(1_200), PolicyDocumentId.POL_04)


class TestQualityIssue:
    def test_ceiling_is_half_the_claimed_items(self) -> None:
        item = pizza()
        decision = evaluate(
            propose(Kind.REFUND, Reason.QUALITY_ISSUE, 2_995, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert decision == Allowed(Money(2_995), PolicyDocumentId.POL_05)

    def test_half_is_rounded_to_the_cent(self) -> None:
        item = build_item("Bala", 5)
        decision = evaluate(
            propose(Kind.COUPON, Reason.QUALITY_ISSUE, 4, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert decision == Denied(DenialReason.EXCEEDS_CEILING, Money(3), PolicyDocumentId.POL_05)

    def test_coupon_is_not_capped_by_what_is_left_to_refund(self) -> None:
        item = pizza()
        order = delivered(item)
        refunded = prior(Kind.REFUND, Reason.NOT_DELIVERED, 5_000, order=order)
        decision = evaluate(
            propose(Kind.COUPON, Reason.QUALITY_ISSUE, 1_500, claim(item)),
            order,
            history(refunded),
            DELIVERED_AT,
        )
        assert decision == Allowed(Money(2_995), PolicyDocumentId.POL_05)

    def test_quality_coupon_above_coupon_limit_needs_approval(self) -> None:
        item = pizza()
        decision = evaluate(
            propose(Kind.COUPON, Reason.QUALITY_ISSUE, 2_995, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert decision == NeedsApproval(
            Money(2_995), PolicyDocumentId.POL_05, (ApprovalReason.EXCEEDS_COUPON_LIMIT,)
        )


class TestLateDelivery:
    @pytest.mark.parametrize(
        "delay", [timedelta(minutes=30), timedelta(minutes=45), timedelta(minutes=60)]
    )
    def test_minor_delay_coupon_is_ten_percent_capped(self, delay: timedelta) -> None:
        """45 minutes late on R$ 186,00: 10% would be R$ 18,60, so the cap of R$ 15,00 holds."""
        order, claimed_at = late_order(Status.DELIVERED, delay)
        decision = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 1_860), order, EMPTY, claimed_at
        )
        assert decision == Denied(
            DenialReason.EXCEEDS_CEILING, Money(1_500), PolicyDocumentId.POL_02
        )

    def test_minor_delay_coupon_is_ten_percent_of_the_total(self) -> None:
        order, claimed_at = late_order(Status.DELIVERED, timedelta(minutes=45), cents=9_000)
        decision = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 900), order, EMPTY, claimed_at
        )
        assert decision == Allowed(Money(900), PolicyDocumentId.POL_02)

    @pytest.mark.parametrize("kind", [Kind.COUPON, Kind.CANCELLATION])
    @pytest.mark.parametrize("delay", [timedelta(minutes=20), timedelta(minutes=29, seconds=59)])
    def test_delay_below_minimum_is_denied(self, kind: ActionKind, delay: timedelta) -> None:
        order, claimed_at = late_order(Status.PREPARING, delay)
        decision = evaluate(propose(kind, Reason.LATE_DELIVERY, 500), order, EMPTY, claimed_at)
        assert decision == Denied(DenialReason.DELAY_BELOW_MINIMUM, None, PolicyDocumentId.POL_02)

    def test_cancellation_needs_more_than_the_major_delay(self) -> None:
        order, claimed_at = late_order(Status.PREPARING, timedelta(minutes=60))
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.LATE_DELIVERY, 4_000), order, EMPTY, claimed_at
        )
        assert isinstance(decision, Denied)
        assert decision.reason is DenialReason.DELAY_BELOW_MINIMUM

    def test_major_delay_allows_coupon_or_cancellation_before_dispatch(self) -> None:
        """Still preparing, 70 minutes late: R$ 20,00 coupon, or cancel with the total back."""
        order, claimed_at = late_order(Status.PREPARING, timedelta(minutes=70), cents=4_000)
        coupon = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 2_000), order, EMPTY, claimed_at
        )
        cancel = evaluate(
            propose(Kind.CANCELLATION, Reason.LATE_DELIVERY, 4_000), order, EMPTY, claimed_at
        )
        assert coupon == Allowed(Money(2_000), PolicyDocumentId.POL_02)
        assert cancel == Allowed(Money(4_000), PolicyDocumentId.POL_02)

    def test_major_delay_cancellation_does_not_ask_the_restaurant(self) -> None:
        """Late cancellation of a preparing R$ 186,00 order: the restaurant is not asked."""
        order, claimed_at = late_order(Status.PREPARING, timedelta(minutes=70))
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.LATE_DELIVERY, 18_600), order, EMPTY, claimed_at
        )
        assert isinstance(decision, NeedsApproval)
        assert ApprovalReason.RESTAURANT_ALREADY_ACCEPTED not in decision.reasons
        assert decision.ceiling == Money(18_600)

    @pytest.mark.parametrize("status", [Status.OUT_FOR_DELIVERY, Status.DELIVERED])
    def test_cancellation_after_dispatch_is_denied_but_coupon_still_fits(
        self, status: OrderStatus
    ) -> None:
        order, claimed_at = late_order(status, timedelta(minutes=70))
        cancel = evaluate(
            propose(Kind.CANCELLATION, Reason.LATE_DELIVERY, 18_600), order, EMPTY, claimed_at
        )
        coupon = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 2_000), order, EMPTY, claimed_at
        )
        assert cancel == Denied(
            DenialReason.ORDER_ALREADY_DISPATCHED, None, PolicyDocumentId.POL_02
        )
        assert coupon == Allowed(Money(2_000), PolicyDocumentId.POL_02)

    @pytest.mark.parametrize("status", [Status.CANCELLED, Status.DELIVERY_FAILED])
    def test_order_that_ended_without_delivery_is_not_eligible(self, status: OrderStatus) -> None:
        order, claimed_at = late_order(status, timedelta(minutes=70))
        decision = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 2_000), order, EMPTY, claimed_at
        )
        assert decision == Denied(
            DenialReason.ORDER_STATUS_NOT_ELIGIBLE, None, PolicyDocumentId.POL_02
        )

    def test_claim_window_after_delivery_edge(self) -> None:
        order, delivered_at = late_order(Status.DELIVERED, timedelta(minutes=45))
        proposal = propose(Kind.COUPON, Reason.LATE_DELIVERY, 1_500)
        at_edge = evaluate(proposal, order, EMPTY, delivered_at + timedelta(hours=24))
        after = evaluate(proposal, order, EMPTY, delivered_at + timedelta(hours=24, seconds=1))
        assert isinstance(at_edge, Allowed)
        assert after == Denied(DenialReason.CLAIM_WINDOW_EXPIRED, None, PolicyDocumentId.POL_02)

    def test_open_order_has_no_claim_window(self) -> None:
        order, _ = late_order(Status.OUT_FOR_DELIVERY, timedelta(0))
        decision = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 2_000),
            order,
            EMPTY,
            ESTIMATED_AT + timedelta(days=3),
        )
        assert isinstance(decision, Allowed)

    @pytest.mark.parametrize("earlier_kind", [Kind.COUPON, Kind.CANCELLATION])
    def test_delay_is_compensated_once_per_order(self, earlier_kind: ActionKind) -> None:
        order, claimed_at = late_order(Status.OUT_FOR_DELIVERY, timedelta(minutes=70))
        earlier = prior(earlier_kind, Reason.LATE_DELIVERY, 2_000, order=order)
        decision = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 2_000), order, history(earlier), claimed_at
        )
        assert decision == Denied(
            DenialReason.DELAY_ALREADY_COMPENSATED, None, PolicyDocumentId.POL_02
        )

    def test_delay_on_another_order_does_not_block(self) -> None:
        order, claimed_at = late_order(Status.OUT_FOR_DELIVERY, timedelta(minutes=70))
        elsewhere = prior(Kind.COUPON, Reason.LATE_DELIVERY, 2_000, executed_at=claimed_at)
        decision = evaluate(
            propose(Kind.COUPON, Reason.LATE_DELIVERY, 2_000), order, history(elsewhere), claimed_at
        )
        assert isinstance(decision, Allowed)


class TestNotDelivered:
    @pytest.mark.parametrize("status", IN_PROGRESS)
    def test_order_in_progress_is_a_delay_case(self, status: OrderStatus) -> None:
        order = order_of_total(status, 4_000)
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000), order, EMPTY, ESTIMATED_AT
        )
        assert decision == Denied(DenialReason.ORDER_IN_PROGRESS, None, PolicyDocumentId.POL_06)

    def test_cancelled_order_is_not_eligible(self) -> None:
        order = order_of_total(Status.CANCELLED, 4_000)
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000), order, EMPTY, ESTIMATED_AT
        )
        assert decision == Denied(
            DenialReason.ORDER_STATUS_NOT_ELIGIBLE, None, PolicyDocumentId.POL_06
        )

    def test_failed_delivery_ceiling_is_remaining_total(self) -> None:
        order = order_of_total(Status.DELIVERY_FAILED, 4_000, final_event_at=ESTIMATED_AT)
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000), order, EMPTY, ESTIMATED_AT
        )
        assert decision == Allowed(Money(4_000), PolicyDocumentId.POL_06)

    def test_high_refund_within_ceiling_needs_approval(self) -> None:
        order = order_of_total(Status.DELIVERY_FAILED, 18_600, final_event_at=ESTIMATED_AT)
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 18_600), order, EMPTY, ESTIMATED_AT
        )
        assert decision == NeedsApproval(
            Money(18_600),
            PolicyDocumentId.POL_06,
            (ApprovalReason.EXCEEDS_ACTION_LIMIT, ApprovalReason.EXCEEDS_CUSTOMER_LIMIT),
        )

    def test_partly_refunded_order_leaves_the_rest(self) -> None:
        """R$ 74,00 order with R$ 12,00 already refunded: the ceiling is R$ 62,00."""
        order = order_of_total(Status.DELIVERY_FAILED, 7_400, final_event_at=ESTIMATED_AT)
        refunded = prior(Kind.REFUND, Reason.WRONG_ITEM, 1_200, order=order)
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 7_400),
            order,
            history(refunded),
            ESTIMATED_AT,
        )
        assert decision == Denied(
            DenialReason.EXCEEDS_CEILING, Money(6_200), PolicyDocumentId.POL_06
        )

    def test_failed_delivery_window_edge(self) -> None:
        order = order_of_total(Status.DELIVERY_FAILED, 4_000, final_event_at=ESTIMATED_AT)
        proposal = propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000)
        at_edge = evaluate(proposal, order, EMPTY, ESTIMATED_AT + timedelta(hours=48))
        after = evaluate(proposal, order, EMPTY, ESTIMATED_AT + timedelta(hours=48, seconds=1))
        assert isinstance(at_edge, Allowed)
        assert after == Denied(DenialReason.CLAIM_WINDOW_EXPIRED, None, PolicyDocumentId.POL_06)

    def test_failed_delivery_without_the_event_is_a_bug(self) -> None:
        order = replace(order_of_total(Status.DELIVERY_FAILED, 4_000), events=())
        with pytest.raises(InvariantError) as error:
            evaluate(propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000), order, EMPTY, ESTIMATED_AT)
        assert error.value.rule == "policy.failed_without_event"

    def test_marked_as_delivered_always_needs_approval(self) -> None:
        order = order_of_total(Status.DELIVERED, 4_000, final_event_at=DELIVERED_AT)
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000), order, EMPTY, DELIVERED_AT
        )
        assert decision == NeedsApproval(
            Money(4_000), PolicyDocumentId.POL_06, (ApprovalReason.MARKED_AS_DELIVERED,)
        )

    def test_high_refund_marked_as_delivered_lists_every_reason(self) -> None:
        """F2: R$ 186,00, marked as delivered, three refunds in the last 30 days."""
        order = order_of_total(Status.DELIVERED, 18_600, final_event_at=DELIVERED_AT)
        recent = history(
            *(
                prior(
                    Kind.REFUND,
                    Reason.ITEM_MISSING,
                    2_500,
                    executed_at=DELIVERED_AT - timedelta(days=day),
                )
                for day in (3, 10, 20)
            )
        )
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 18_600), order, recent, DELIVERED_AT
        )
        assert decision == NeedsApproval(
            Money(18_600),
            PolicyDocumentId.POL_06,
            (
                ApprovalReason.MARKED_AS_DELIVERED,
                ApprovalReason.EXCEEDS_ACTION_LIMIT,
                ApprovalReason.EXCEEDS_CUSTOMER_LIMIT,
            ),
        )

    def test_marked_as_delivered_window_edge(self) -> None:
        order = order_of_total(Status.DELIVERED, 4_000, final_event_at=DELIVERED_AT)
        proposal = propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000)
        at_edge = evaluate(proposal, order, EMPTY, DELIVERED_AT + timedelta(hours=48))
        after = evaluate(proposal, order, EMPTY, DELIVERED_AT + timedelta(hours=48, seconds=1))
        assert isinstance(at_edge, NeedsApproval)
        assert after == Denied(DenialReason.CLAIM_WINDOW_EXPIRED, None, PolicyDocumentId.POL_06)


class TestDuplicateCharge:
    def test_always_needs_approval(self) -> None:
        order = order_of_total(Status.DELIVERED, 4_000, final_event_at=DELIVERED_AT)
        decision = evaluate(
            propose(Kind.REFUND, Reason.DUPLICATE_CHARGE, 4_000), order, EMPTY, DELIVERED_AT
        )
        assert decision == NeedsApproval(
            Money(4_000), PolicyDocumentId.POL_07, (ApprovalReason.DUPLICATE_CHARGE_UNVERIFIED,)
        )

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_any_status_can_claim(self, status: OrderStatus) -> None:
        order = order_of_total(status, 4_000)
        decision = evaluate(
            propose(Kind.REFUND, Reason.DUPLICATE_CHARGE, 4_000), order, EMPTY, DELIVERED_AT
        )
        assert isinstance(decision, NeedsApproval)

    def test_window_counts_from_placement(self) -> None:
        order = order_of_total(Status.DELIVERED, 4_000, final_event_at=DELIVERED_AT)
        proposal = propose(Kind.REFUND, Reason.DUPLICATE_CHARGE, 4_000)
        at_edge = evaluate(proposal, order, EMPTY, BASE_TIME + timedelta(days=90))
        after = evaluate(proposal, order, EMPTY, BASE_TIME + timedelta(days=90, seconds=1))
        assert isinstance(at_edge, NeedsApproval)
        assert after == Denied(DenialReason.CLAIM_WINDOW_EXPIRED, None, PolicyDocumentId.POL_07)


class TestCustomerCancellation:
    def test_before_acceptance_is_allowed_whatever_the_amount(self) -> None:
        order = order_of_total(Status.PLACED, 18_600)
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION, 18_600),
            order,
            EMPTY,
            BASE_TIME,
        )
        assert decision == Allowed(Money(18_600), PolicyDocumentId.POL_01)

    def test_before_acceptance_ignores_the_customer_limit(self) -> None:
        order = order_of_total(Status.PLACED, 9_000)
        spent = history(prior(Kind.REFUND, Reason.ITEM_MISSING, 9_500, executed_at=BASE_TIME))
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION, 9_000), order, spent, BASE_TIME
        )
        assert isinstance(decision, Allowed)

    @pytest.mark.parametrize("status", [Status.ACCEPTED, Status.PREPARING, Status.READY_FOR_PICKUP])
    def test_after_acceptance_needs_approval(self, status: OrderStatus) -> None:
        order = order_of_total(status, 4_000)
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION, 4_000), order, EMPTY, BASE_TIME
        )
        assert decision == NeedsApproval(
            Money(4_000), PolicyDocumentId.POL_01, (ApprovalReason.RESTAURANT_ALREADY_ACCEPTED,)
        )

    def test_after_acceptance_does_not_count_toward_the_customer_limit(self) -> None:
        order = order_of_total(Status.PREPARING, 4_000)
        spent = history(prior(Kind.REFUND, Reason.ITEM_MISSING, 9_500, executed_at=BASE_TIME))
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION, 4_000), order, spent, BASE_TIME
        )
        assert isinstance(decision, NeedsApproval)
        assert decision.reasons == (ApprovalReason.RESTAURANT_ALREADY_ACCEPTED,)

    @pytest.mark.parametrize(
        "status", [Status.OUT_FOR_DELIVERY, Status.DELIVERED, Status.DELIVERY_FAILED]
    )
    def test_after_dispatch_is_denied(self, status: OrderStatus) -> None:
        order = order_of_total(status, 4_000)
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION, 4_000), order, EMPTY, BASE_TIME
        )
        assert decision == Denied(
            DenialReason.ORDER_ALREADY_DISPATCHED, None, PolicyDocumentId.POL_01
        )

    def test_cancelled_order_is_not_eligible(self) -> None:
        order = order_of_total(Status.CANCELLED, 4_000)
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION, 4_000), order, EMPTY, BASE_TIME
        )
        assert decision == Denied(
            DenialReason.ORDER_STATUS_NOT_ELIGIBLE, None, PolicyDocumentId.POL_01
        )


class TestAutoApprovalLimits:
    def failed_order(self, cents: int) -> Order:
        return order_of_total(Status.DELIVERY_FAILED, cents, final_event_at=ESTIMATED_AT)

    def test_refund_at_the_action_limit_is_allowed(self) -> None:
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 5_000),
            self.failed_order(5_000),
            EMPTY,
            ESTIMATED_AT,
        )
        assert isinstance(decision, Allowed)

    def test_refund_above_action_limit_needs_approval(self) -> None:
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 5_001),
            self.failed_order(5_001),
            EMPTY,
            ESTIMATED_AT,
        )
        assert decision == NeedsApproval(
            Money(5_001), PolicyDocumentId.POL_06, (ApprovalReason.EXCEEDS_ACTION_LIMIT,)
        )

    def test_late_cancellation_above_action_limit_needs_approval(self) -> None:
        order, claimed_at = late_order(Status.PLACED, timedelta(minutes=70), cents=5_001)
        decision = evaluate(
            propose(Kind.CANCELLATION, Reason.LATE_DELIVERY, 5_001), order, EMPTY, claimed_at
        )
        assert isinstance(decision, NeedsApproval)
        assert decision.reasons == (ApprovalReason.EXCEEDS_ACTION_LIMIT,)

    def test_coupon_above_coupon_limit_needs_approval(self) -> None:
        item = build_item("Pizza grande", 4_002)
        at_limit = evaluate(
            propose(Kind.COUPON, Reason.QUALITY_ISSUE, 2_000, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        above = evaluate(
            propose(Kind.COUPON, Reason.QUALITY_ISSUE, 2_001, claim(item)),
            delivered(item),
            EMPTY,
            DELIVERED_AT,
        )
        assert isinstance(at_limit, Allowed)
        assert above == NeedsApproval(
            Money(2_001), PolicyDocumentId.POL_05, (ApprovalReason.EXCEEDS_COUPON_LIMIT,)
        )

    def test_customer_limit_counts_auto_approved_in_window(self) -> None:
        """R$ 95,00 auto-approved in the last 30 days plus R$ 8,00 passes R$ 100,00."""
        item = soda()
        spent = history(prior(Kind.REFUND, Reason.ITEM_MISSING, 9_500, executed_at=DELIVERED_AT))
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(item),
            spent,
            DELIVERED_AT,
        )
        assert decision == NeedsApproval(
            Money(800), PolicyDocumentId.POL_03, (ApprovalReason.EXCEEDS_CUSTOMER_LIMIT,)
        )

    def test_customer_limit_counts_coupons(self) -> None:
        item = soda()
        spent = history(prior(Kind.COUPON, Reason.LATE_DELIVERY, 9_500, executed_at=DELIVERED_AT))
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(item),
            spent,
            DELIVERED_AT,
        )
        assert isinstance(decision, NeedsApproval)

    def test_customer_limit_reached_exactly_is_allowed(self) -> None:
        item = soda()
        spent = history(prior(Kind.REFUND, Reason.ITEM_MISSING, 9_200, executed_at=DELIVERED_AT))
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(item),
            spent,
            DELIVERED_AT,
        )
        assert isinstance(decision, Allowed)

    @pytest.mark.parametrize(
        ("age", "counts"),
        [
            (timedelta(days=30), True),
            (timedelta(days=30, seconds=1), False),
            (timedelta(days=31), False),
        ],
    )
    def test_customer_limit_window_edge(self, age: timedelta, counts: bool) -> None:
        item = soda()
        spent = history(
            prior(Kind.REFUND, Reason.ITEM_MISSING, 9_500, executed_at=DELIVERED_AT - age)
        )
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(item),
            spent,
            DELIVERED_AT,
        )
        assert isinstance(decision, NeedsApproval) is counts

    def test_reviewer_approved_actions_do_not_count(self) -> None:
        item = soda()
        reviewed = prior(Kind.REFUND, Reason.NOT_DELIVERED, 18_600, approved_by=ApprovedBy.REVIEWER)
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(item),
            history(reviewed),
            DELIVERED_AT,
        )
        assert isinstance(decision, Allowed)

    def test_customer_cancellations_do_not_count(self) -> None:
        item = soda()
        cancelled = prior(Kind.CANCELLATION, Reason.CUSTOMER_CANCELLATION, 18_600)
        decision = evaluate(
            propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item)),
            delivered(item),
            history(cancelled),
            DELIVERED_AT,
        )
        assert isinstance(decision, Allowed)

    def test_every_exceeded_limit_is_listed(self) -> None:
        spent = history(prior(Kind.REFUND, Reason.ITEM_MISSING, 9_500, executed_at=ESTIMATED_AT))
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 6_000),
            self.failed_order(6_000),
            spent,
            ESTIMATED_AT,
        )
        assert decision == NeedsApproval(
            Money(6_000),
            PolicyDocumentId.POL_06,
            (ApprovalReason.EXCEEDS_ACTION_LIMIT, ApprovalReason.EXCEEDS_CUSTOMER_LIMIT),
        )

    def test_limits_can_be_varied_without_touching_the_global(self) -> None:
        decision = evaluate(
            propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000),
            self.failed_order(4_000),
            EMPTY,
            ESTIMATED_AT,
            PolicyLimits(auto_approval_limit=Money(3_000)),
        )
        assert isinstance(decision, NeedsApproval)


class TestReevaluation:
    def test_same_proposal_after_execution_is_denied(self) -> None:
        item = soda()
        order = delivered(pizza(), item)
        proposal = propose(Kind.REFUND, Reason.ITEM_MISSING, 800, claim(item))
        first = evaluate(proposal, order, EMPTY, DELIVERED_AT)
        updated = history(executed(proposal, order, DELIVERED_AT, ApprovedBy.AUTOMATIC))
        second = evaluate(proposal, order, updated, DELIVERED_AT)
        assert isinstance(first, Allowed)
        assert second == Denied(DenialReason.ITEM_ALREADY_CLAIMED, None, PolicyDocumentId.POL_03)

    def test_full_refund_after_execution_leaves_nothing(self) -> None:
        order = order_of_total(Status.DELIVERY_FAILED, 4_000, final_event_at=ESTIMATED_AT)
        proposal = propose(Kind.REFUND, Reason.NOT_DELIVERED, 4_000)
        updated = history(executed(proposal, order, ESTIMATED_AT, ApprovedBy.REVIEWER))
        decision = evaluate(proposal, order, updated, ESTIMATED_AT)
        assert decision == Denied(DenialReason.EXCEEDS_CEILING, Money(0), PolicyDocumentId.POL_06)


class TestSensitiveCases:
    def test_engine_has_no_reason_for_sensitive_cases(self) -> None:
        """Allergy and courier conduct go to a person before any proposal exists (routing)."""
        assert {reason.value for reason in ClaimReason} == {
            "item_missing",
            "wrong_item",
            "quality_issue",
            "late_delivery",
            "not_delivered",
            "duplicate_charge",
            "customer_cancellation",
        }

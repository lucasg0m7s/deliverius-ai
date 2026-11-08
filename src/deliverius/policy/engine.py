"""The policy engine: a pure, deterministic decision on a proposed action for an order.

The steps run in a fixed order (shape, status and window, what was already claimed, ceiling,
always-human cases, auto-approval limits), so the first denial reason is predictable.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import assert_never

from deliverius.orders.models import Order, OrderStatus
from deliverius.policy.limits import POLICY_LIMITS, PolicyLimits
from deliverius.policy.models import (
    ActionKind,
    Allowed,
    ApprovalReason,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    CustomerHistory,
    DenialReason,
    Denied,
    NeedsApproval,
    PolicyDecision,
    PolicyDocumentId,
    PriorAction,
    ProposedAction,
)
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import OrderItemId
from deliverius.shared.money import Money

_Kind = ActionKind
_Reason = ClaimReason
_Status = OrderStatus

_COVERED: Mapping[ClaimReason, frozenset[ActionKind]] = {
    _Reason.ITEM_MISSING: frozenset({_Kind.REFUND}),
    _Reason.WRONG_ITEM: frozenset({_Kind.REFUND}),
    _Reason.QUALITY_ISSUE: frozenset({_Kind.REFUND, _Kind.COUPON}),
    _Reason.LATE_DELIVERY: frozenset({_Kind.COUPON, _Kind.CANCELLATION}),
    _Reason.NOT_DELIVERED: frozenset({_Kind.REFUND}),
    _Reason.DUPLICATE_CHARGE: frozenset({_Kind.REFUND}),
    _Reason.CUSTOMER_CANCELLATION: frozenset({_Kind.CANCELLATION}),
}
_DOCUMENT_OF: Mapping[ClaimReason, PolicyDocumentId] = {
    _Reason.ITEM_MISSING: PolicyDocumentId.POL_03,
    _Reason.WRONG_ITEM: PolicyDocumentId.POL_04,
    _Reason.QUALITY_ISSUE: PolicyDocumentId.POL_05,
    _Reason.LATE_DELIVERY: PolicyDocumentId.POL_02,
    _Reason.NOT_DELIVERED: PolicyDocumentId.POL_06,
    _Reason.DUPLICATE_CHARGE: PolicyDocumentId.POL_07,
    _Reason.CUSTOMER_CANCELLATION: PolicyDocumentId.POL_01,
}
_ITEM_REASONS = frozenset({_Reason.ITEM_MISSING, _Reason.WRONG_ITEM, _Reason.QUALITY_ISSUE})
_IN_PROGRESS = frozenset(
    {
        _Status.PLACED,
        _Status.ACCEPTED,
        _Status.PREPARING,
        _Status.READY_FOR_PICKUP,
        _Status.OUT_FOR_DELIVERY,
    }
)
_ACCEPTED_NOT_DISPATCHED = frozenset(
    {_Status.ACCEPTED, _Status.PREPARING, _Status.READY_FOR_PICKUP}
)
_DISPATCHED = frozenset({_Status.OUT_FOR_DELIVERY, _Status.DELIVERED, _Status.DELIVERY_FAILED})
_ENDED_WITHOUT_DELIVERY = frozenset({_Status.CANCELLED, _Status.DELIVERY_FAILED})


@dataclass(frozen=True, slots=True)
class _Claim:
    action: ProposedAction
    order: Order
    history: CustomerHistory
    claimed_at: datetime
    limits: PolicyLimits

    @property
    def order_actions(self) -> tuple[PriorAction, ...]:
        return tuple(prior for prior in self.history.actions if prior.order_id == self.order.id)


def evaluate(
    action: ProposedAction,
    order: Order,
    history: CustomerHistory,
    claimed_at: datetime,
    limits: PolicyLimits = POLICY_LIMITS,
) -> PolicyDecision:
    """Decide on `action` as of `claimed_at`; the engine never reads the clock."""
    require_aware(claimed_at, "claimed_at")
    if action.kind not in _COVERED[action.reason]:
        return Denied(DenialReason.NOT_COVERED, None, None)
    claim = _Claim(action, order, history, claimed_at, limits)
    document = _DOCUMENT_OF[action.reason]
    denial = _shape_denial(action) or _status_denial(claim) or _already_claimed_denial(claim)
    if denial is not None:
        return Denied(denial, None, document)
    ceiling = _ceiling(claim)
    if isinstance(ceiling, DenialReason):
        return Denied(ceiling, None, document)
    if action.amount > ceiling:
        return Denied(DenialReason.EXCEEDS_CEILING, ceiling, document)
    reasons = (*_always_human_reasons(claim), *_limit_reasons(claim))
    if reasons:
        return NeedsApproval(ceiling, document, reasons)
    return Allowed(ceiling, document)


def _shape_denial(action: ProposedAction) -> DenialReason | None:
    if action.amount == Money.zero():
        return DenialReason.AMOUNT_NOT_POSITIVE
    needs_items = action.reason in _ITEM_REASONS
    if needs_items and not action.items:
        return DenialReason.ITEMS_REQUIRED
    if not needs_items and action.items:
        return DenialReason.ITEMS_NOT_ALLOWED
    return None


def _status_denial(claim: _Claim) -> DenialReason | None:
    limits = claim.limits
    match claim.action.reason:
        case _Reason.ITEM_MISSING | _Reason.WRONG_ITEM | _Reason.QUALITY_ISSUE:
            if claim.order.status is not _Status.DELIVERED:
                return DenialReason.ORDER_NOT_DELIVERED
            return _window_denial(claim, _delivered_at(claim.order), limits.item_claim_window)
        case _Reason.LATE_DELIVERY:
            return _late_status_denial(claim)
        case _Reason.NOT_DELIVERED:
            return _not_delivered_status_denial(claim)
        case _Reason.DUPLICATE_CHARGE:
            return _window_denial(
                claim, claim.order.placed_at, limits.duplicate_charge_claim_window
            )
        case _Reason.CUSTOMER_CANCELLATION:
            return _cancellation_status_denial(claim.order.status)
        case unreachable:  # pragma: no cover - mypy proves every reason is handled
            assert_never(unreachable)


def _late_status_denial(claim: _Claim) -> DenialReason | None:
    status = claim.order.status
    if status in _ENDED_WITHOUT_DELIVERY:
        return DenialReason.ORDER_STATUS_NOT_ELIGIBLE
    if claim.action.kind is _Kind.CANCELLATION and status in _DISPATCHED:
        return DenialReason.ORDER_ALREADY_DISPATCHED
    if status is _Status.DELIVERED:
        return _window_denial(claim, _delivered_at(claim.order), claim.limits.late_claim_window)
    return None


def _not_delivered_status_denial(claim: _Claim) -> DenialReason | None:
    status = claim.order.status
    if status is _Status.CANCELLED:
        return DenialReason.ORDER_STATUS_NOT_ELIGIBLE
    if status in _IN_PROGRESS:
        return DenialReason.ORDER_IN_PROGRESS
    final_event_at = (
        _failed_at(claim.order) if status is _Status.DELIVERY_FAILED else _delivered_at(claim.order)
    )
    return _window_denial(claim, final_event_at, claim.limits.not_delivered_claim_window)


def _cancellation_status_denial(status: OrderStatus) -> DenialReason | None:
    if status is _Status.CANCELLED:
        return DenialReason.ORDER_STATUS_NOT_ELIGIBLE
    if status in _DISPATCHED:
        return DenialReason.ORDER_ALREADY_DISPATCHED
    return None


def _window_denial(claim: _Claim, start: datetime, window: timedelta) -> DenialReason | None:
    if claim.claimed_at - start > window:
        return DenialReason.CLAIM_WINDOW_EXPIRED
    return None


def _delivered_at(order: Order) -> datetime:
    # Deciding money on a delivered order with no delivery time would be a guess.
    if order.delivered_at is None:
        raise InvariantError("policy.delivered_without_timestamp")
    return order.delivered_at


def _failed_at(order: Order) -> datetime:
    failures = [
        event.occurred_at for event in order.events if event.status is _Status.DELIVERY_FAILED
    ]
    if not failures:
        raise InvariantError("policy.failed_without_event")
    return max(failures)


def _already_claimed_denial(claim: _Claim) -> DenialReason | None:
    if claim.action.reason is _Reason.LATE_DELIVERY:
        # A coupon does not lower what is left to refund, so this is what stops a second one.
        compensated = any(prior.reason is _Reason.LATE_DELIVERY for prior in claim.order_actions)
        return DenialReason.DELAY_ALREADY_COMPENSATED if compensated else None
    return _items_denial(claim)


def _items_denial(claim: _Claim) -> DenialReason | None:
    if any(item.quantity < 1 for item in claim.action.items):
        return DenialReason.INVALID_QUANTITY
    ordered = {item.id: item.quantity for item in claim.order.items}
    already_claimed = _quantities(item for prior in claim.order_actions for item in prior.items)
    for item_id, quantity in _quantities(claim.action.items).items():
        if item_id not in ordered:
            return DenialReason.ITEM_NOT_IN_ORDER
        if quantity > ordered[item_id]:
            return DenialReason.INVALID_QUANTITY
        if quantity > ordered[item_id] - already_claimed.get(item_id, 0):
            return DenialReason.ITEM_ALREADY_CLAIMED
    return None


def _quantities(items: Iterable[ClaimedItem]) -> dict[OrderItemId, int]:
    """Repeated items add up: citing one twice is the same as citing it once with the sum."""
    totals: dict[OrderItemId, int] = {}
    for item in items:
        totals[item.order_item_id] = totals.get(item.order_item_id, 0) + item.quantity
    return totals


def _ceiling(claim: _Claim) -> Money | DenialReason:
    match claim.action.reason:
        case _Reason.ITEM_MISSING | _Reason.WRONG_ITEM:
            return _capped_by_remaining(claim, _claimed_items_value(claim))
        case _Reason.QUALITY_ISSUE:
            value = _claimed_items_value(claim).fraction(claim.limits.quality_refund_percent, 100)
            return _capped_by_remaining(claim, value)
        case _Reason.LATE_DELIVERY:
            return _late_ceiling(claim)
        case _Reason.NOT_DELIVERED | _Reason.DUPLICATE_CHARGE | _Reason.CUSTOMER_CANCELLATION:
            return _remaining_refundable(claim)
        case unreachable:  # pragma: no cover - mypy proves every reason is handled
            assert_never(unreachable)


def _late_ceiling(claim: _Claim) -> Money | DenialReason:
    order, limits, kind = claim.order, claim.limits, claim.action.kind
    arrived_at = _delivered_at(order) if order.status is _Status.DELIVERED else claim.claimed_at
    delay = arrived_at - order.estimated_delivery_at
    if delay > limits.major_delay:
        return limits.major_delay_coupon if kind is _Kind.COUPON else _remaining_refundable(claim)
    if delay < limits.minor_delay or kind is _Kind.CANCELLATION:
        return DenialReason.DELAY_BELOW_MINIMUM
    coupon = order.total.fraction(limits.minor_delay_coupon_percent, 100)
    return min(coupon, limits.minor_delay_coupon_cap)


def _claimed_items_value(claim: _Claim) -> Money:
    prices = {item.id: item.unit_price for item in claim.order.items}
    return Money.sum(prices[item.order_item_id] * item.quantity for item in claim.action.items)


def _capped_by_remaining(claim: _Claim, value: Money) -> Money:
    """Money back never exceeds what is left of the order total; a coupon is not money back."""
    if claim.action.kind is _Kind.COUPON:
        return value
    return min(value, _remaining_refundable(claim))


def _remaining_refundable(claim: _Claim) -> Money:
    refunded = Money.sum(
        prior.amount for prior in claim.order_actions if prior.kind is not _Kind.COUPON
    )
    return Money(max(0, claim.order.total.cents - refunded.cents))


def _always_human_reasons(claim: _Claim) -> tuple[ApprovalReason, ...]:
    reason, status = claim.action.reason, claim.order.status
    reasons: list[ApprovalReason] = []
    if reason is _Reason.NOT_DELIVERED and status is _Status.DELIVERED:
        reasons.append(ApprovalReason.MARKED_AS_DELIVERED)
    if reason is _Reason.DUPLICATE_CHARGE:
        reasons.append(ApprovalReason.DUPLICATE_CHARGE_UNVERIFIED)
    if reason is _Reason.CUSTOMER_CANCELLATION and status in _ACCEPTED_NOT_DISPATCHED:
        reasons.append(ApprovalReason.RESTAURANT_ALREADY_ACCEPTED)
    return tuple(reasons)


def _limit_reasons(claim: _Claim) -> tuple[ApprovalReason, ...]:
    action, limits = claim.action, claim.limits
    if action.reason is _Reason.CUSTOMER_CANCELLATION and claim.order.status is _Status.PLACED:
        return ()  # nothing happened yet: no incident to verify and no food made
    reasons: list[ApprovalReason] = []
    if action.kind is _Kind.COUPON:
        if action.amount > limits.coupon_auto_approval_limit:
            reasons.append(ApprovalReason.EXCEEDS_COUPON_LIMIT)
    elif action.amount > limits.auto_approval_limit:
        reasons.append(ApprovalReason.EXCEEDS_ACTION_LIMIT)
    if _counts_toward_customer_limit(action.reason) and (
        _recently_auto_approved(claim) + action.amount > limits.customer_auto_approval_limit
    ):
        reasons.append(ApprovalReason.EXCEEDS_CUSTOMER_LIMIT)
    return tuple(reasons)


def _counts_toward_customer_limit(reason: ClaimReason) -> bool:
    # Before acceptance a cancellation is exempt; after it a reviewer always decides.
    return reason is not _Reason.CUSTOMER_CANCELLATION


def _recently_auto_approved(claim: _Claim) -> Money:
    window_start = claim.claimed_at - claim.limits.customer_auto_approval_window
    return Money.sum(
        prior.amount
        for prior in claim.history.actions
        if prior.approved_by is ApprovedBy.AUTOMATIC
        and _counts_toward_customer_limit(prior.reason)
        and prior.executed_at >= window_start
    )

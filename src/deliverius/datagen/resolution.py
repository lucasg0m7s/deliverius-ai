"""Layer 3: the correct resolution of a claim, always decided by `policy.evaluate`.

This module only turns what the customer asks for into a `ProposedAction`, keeps each customer's
history of executed actions as time goes by, and records the engine's answer. No policy rule is
decided here: not the window, not the ceiling, not the limit.
"""

import random
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import assert_never

from deliverius.datagen.records import ClaimedItemRecord, DecisionKind, ResolutionRecord
from deliverius.orders import Order
from deliverius.policy import (
    POLICY_LIMITS,
    ActionKind,
    Allowed,
    ApprovedBy,
    ClaimedItem,
    ClaimReason,
    CustomerHistory,
    Denied,
    NeedsApproval,
    PolicyDecision,
    PolicyLimits,
    PriorAction,
    ProposedAction,
    evaluate,
)
from deliverius.shared import CustomerId, Money

_REVIEWER_DELAY_MINUTES = (5, 120)


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """What a customer asks for. `asks_for_more` is an inflated delay claim: the biggest coupon."""

    order: Order
    customer_id: CustomerId
    reason: ClaimReason
    items: tuple[ClaimedItem, ...]
    claimed_at: datetime
    asks_for_more: bool = False


@dataclass(frozen=True, slots=True)
class Resolution:
    proposal: ProposedAction
    decision: PolicyDecision


class Resolver:
    """Resolves claims in time order; each executed action enters the customer's next history."""

    def __init__(
        self,
        rng: random.Random,
        *,
        reviewer_rejects_fraud: float,
        limits: PolicyLimits = POLICY_LIMITS,
    ) -> None:
        self._rng = rng
        self._reviewer_rejects_fraud = reviewer_rejects_fraud
        self._limits = limits
        self._executed: defaultdict[CustomerId, list[PriorAction]] = defaultdict(list)

    def resolve(self, request: ClaimRequest, *, is_fraud: bool) -> Resolution:
        """Decide `request` and, when it is executed, remember it for the customer's next claim.

        `is_fraud` is the truth a human reviewer would find, not the (noisy) training label.
        """
        order = order_as_of(request.order, request.claimed_at)
        history = self._history(request, order)
        proposal = self._propose(request, order, history)
        decision = evaluate(proposal, order, history, request.claimed_at, self._limits)
        self._execute(request, proposal, decision, is_fraud=is_fraud)
        return Resolution(proposal, decision)

    def _history(self, request: ClaimRequest, order: Order) -> CustomerHistory:
        """The customer's actions in the auto-approval window, plus every one on this order."""
        window_start = request.claimed_at - self._limits.customer_auto_approval_window
        return CustomerHistory(
            tuple(
                prior
                for prior in self._executed[request.customer_id]
                if prior.order_id == order.id
                or window_start <= prior.executed_at <= request.claimed_at
            )
        )

    def _propose(
        self, request: ClaimRequest, order: Order, history: CustomerHistory
    ) -> ProposedAction:
        reason, items, limits = request.reason, request.items, self._limits
        prices = {item.id: item.unit_price for item in order.items}
        claimed_value = Money.sum(
            prices.get(item.order_item_id, Money.zero()) * item.quantity for item in items
        )
        match reason:
            case ClaimReason.ITEM_MISSING | ClaimReason.WRONG_ITEM:
                return ProposedAction(ActionKind.REFUND, reason, claimed_value, items)
            case ClaimReason.QUALITY_ISSUE:
                amount = claimed_value.fraction(limits.quality_refund_percent, 100)
                return ProposedAction(ActionKind.REFUND, reason, amount, items)
            case ClaimReason.LATE_DELIVERY:
                return ProposedAction(
                    ActionKind.COUPON, reason, self._coupon_amount(request, order), ()
                )
            case ClaimReason.NOT_DELIVERED | ClaimReason.CUSTOMER_CANCELLATION:
                kind = (
                    ActionKind.CANCELLATION
                    if reason is ClaimReason.CUSTOMER_CANCELLATION
                    else ActionKind.REFUND
                )
                return ProposedAction(kind, reason, _not_yet_refunded(order, history), ())
            case ClaimReason.DUPLICATE_CHARGE:
                return ProposedAction(ActionKind.REFUND, reason, order.total, ())
            case unreachable:  # pragma: no cover - mypy proves every reason is handled
                assert_never(unreachable)

    def _coupon_amount(self, request: ClaimRequest, order: Order) -> Money:
        limits = self._limits
        arrived_at = order.delivered_at or request.claimed_at
        delay = arrived_at - order.estimated_delivery_at
        if request.asks_for_more or delay > limits.major_delay:
            return limits.major_delay_coupon
        return min(
            order.total.fraction(limits.minor_delay_coupon_percent, 100),
            limits.minor_delay_coupon_cap,
        )

    def _execute(
        self,
        request: ClaimRequest,
        proposal: ProposedAction,
        decision: PolicyDecision,
        *,
        is_fraud: bool,
    ) -> None:
        match decision:
            case Allowed():
                self._remember(request, proposal, request.claimed_at, ApprovedBy.AUTOMATIC)
            case NeedsApproval():
                if is_fraud and self._rng.random() < self._reviewer_rejects_fraud:
                    return
                delay = timedelta(minutes=self._rng.randint(*_REVIEWER_DELAY_MINUTES))
                self._remember(request, proposal, request.claimed_at + delay, ApprovedBy.REVIEWER)
            case Denied():
                return
            case unreachable:  # pragma: no cover - mypy proves every decision is handled
                assert_never(unreachable)

    def _remember(
        self,
        request: ClaimRequest,
        proposal: ProposedAction,
        executed_at: datetime,
        approved_by: ApprovedBy,
    ) -> None:
        self._executed[request.customer_id].append(
            PriorAction(
                order_id=request.order.id,
                kind=proposal.kind,
                reason=proposal.reason,
                amount=proposal.amount,
                items=proposal.items,
                executed_at=executed_at,
                approved_by=approved_by,
            )
        )


def order_as_of(order: Order, instant: datetime) -> Order:
    """The order as the customer sees it at `instant`: only the timeline up to then."""
    events = tuple(event for event in order.events if event.occurred_at <= instant)
    if len(events) == len(order.events):
        return order
    delivered_at = (
        order.delivered_at if order.delivered_at and order.delivered_at <= instant else None
    )
    return replace(order, status=events[-1].status, delivered_at=delivered_at, events=events)


def resolution_record(decision: PolicyDecision) -> ResolutionRecord:
    match decision:
        case Allowed(ceiling, document):
            return ResolutionRecord(
                decision=DecisionKind.ALLOWED, document=document, ceiling_cents=ceiling.cents
            )
        case NeedsApproval(ceiling, document, reasons):
            return ResolutionRecord(
                decision=DecisionKind.NEEDS_APPROVAL,
                approval_reasons=reasons,
                document=document,
                ceiling_cents=ceiling.cents,
            )
        case Denied(reason, ceiling, document):
            return ResolutionRecord(
                decision=DecisionKind.DENIED,
                denial_reason=reason,
                document=document,
                ceiling_cents=None if ceiling is None else ceiling.cents,
            )
        case unreachable:  # pragma: no cover - mypy proves every decision is handled
            assert_never(unreachable)


def claimed_item_records(items: tuple[ClaimedItem, ...]) -> tuple[ClaimedItemRecord, ...]:
    return tuple(
        ClaimedItemRecord(order_item_id=i.order_item_id, quantity=i.quantity) for i in items
    )


def _not_yet_refunded(order: Order, history: CustomerHistory) -> Money:
    refunded = Money.sum(
        prior.amount
        for prior in history.actions
        if prior.order_id == order.id and prior.kind is not ActionKind.COUPON
    )
    return Money(max(0, order.total.cents - refunded.cents))

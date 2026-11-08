"""What the policy engine receives (a proposed action and the customer's history) and decides."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import OrderId, OrderItemId
from deliverius.shared.money import Money


class ActionKind(StrEnum):
    REFUND = "refund"
    COUPON = "coupon"
    CANCELLATION = "cancellation"


class ClaimReason(StrEnum):
    """Why the customer is claiming; same names as the intents, where one exists."""

    ITEM_MISSING = "item_missing"
    WRONG_ITEM = "wrong_item"
    QUALITY_ISSUE = "quality_issue"
    LATE_DELIVERY = "late_delivery"
    NOT_DELIVERED = "not_delivered"
    DUPLICATE_CHARGE = "duplicate_charge"
    CUSTOMER_CANCELLATION = "customer_cancellation"


class ApprovedBy(StrEnum):
    AUTOMATIC = "automatic"
    REVIEWER = "reviewer"


class PolicyDocumentId(StrEnum):
    POL_01 = "POL-01"
    POL_02 = "POL-02"
    POL_03 = "POL-03"
    POL_04 = "POL-04"
    POL_05 = "POL-05"
    POL_06 = "POL-06"
    POL_07 = "POL-07"
    POL_08 = "POL-08"
    POL_09 = "POL-09"
    POL_10 = "POL-10"
    POL_11 = "POL-11"
    POL_12 = "POL-12"


class DenialReason(StrEnum):
    """Stable codes, stored in the graph state: only ever add values."""

    NOT_COVERED = "not_covered"
    AMOUNT_NOT_POSITIVE = "amount_not_positive"
    ITEMS_REQUIRED = "items_required"
    ITEMS_NOT_ALLOWED = "items_not_allowed"
    ITEM_NOT_IN_ORDER = "item_not_in_order"
    INVALID_QUANTITY = "invalid_quantity"
    ITEM_ALREADY_CLAIMED = "item_already_claimed"
    ORDER_NOT_DELIVERED = "order_not_delivered"
    ORDER_IN_PROGRESS = "order_in_progress"
    ORDER_ALREADY_DISPATCHED = "order_already_dispatched"
    ORDER_STATUS_NOT_ELIGIBLE = "order_status_not_eligible"
    CLAIM_WINDOW_EXPIRED = "claim_window_expired"
    DELAY_BELOW_MINIMUM = "delay_below_minimum"
    DELAY_ALREADY_COMPENSATED = "delay_already_compensated"
    EXCEEDS_CEILING = "exceeds_ceiling"


class ApprovalReason(StrEnum):
    """Stable codes, stored in approval requests: only ever add values."""

    MARKED_AS_DELIVERED = "marked_as_delivered"
    DUPLICATE_CHARGE_UNVERIFIED = "duplicate_charge_unverified"
    RESTAURANT_ALREADY_ACCEPTED = "restaurant_already_accepted"
    EXCEEDS_ACTION_LIMIT = "exceeds_action_limit"
    EXCEEDS_COUPON_LIMIT = "exceeds_coupon_limit"
    EXCEEDS_CUSTOMER_LIMIT = "exceeds_customer_limit"


@dataclass(frozen=True, slots=True)
class ClaimedItem:
    """No invariants on purpose: a bad quantity from the LLM becomes a denial, not an exception."""

    order_item_id: OrderItemId
    quantity: int


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """The domain form of a proposal; the LLM's `ActionProposal` becomes one at the edge."""

    kind: ActionKind
    reason: ClaimReason
    amount: Money
    items: tuple[ClaimedItem, ...] = ()


@dataclass(frozen=True, slots=True)
class PriorAction:
    """An action already executed for the customer."""

    order_id: OrderId
    kind: ActionKind
    reason: ClaimReason
    amount: Money
    items: tuple[ClaimedItem, ...]
    executed_at: datetime
    approved_by: ApprovedBy

    def __post_init__(self) -> None:
        require_aware(self.executed_at, "executed_at")


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """The executed actions the engine weighs a proposal against.

    Contract for the caller: every action executed for the customer within the auto-approval
    window before `claimed_at` (30 days), plus every action ever executed on the evaluated order.
    An incomplete history silently weakens the 30-day limit and the "already claimed" checks.
    """

    actions: tuple[PriorAction, ...] = ()


@dataclass(frozen=True, slots=True)
class Allowed:
    ceiling: Money
    document: PolicyDocumentId


@dataclass(frozen=True, slots=True)
class NeedsApproval:
    """Carries every reason that sent the case to a person: the reviewer needs to see them all."""

    ceiling: Money
    document: PolicyDocumentId
    reasons: tuple[ApprovalReason, ...]

    def __post_init__(self) -> None:
        if not self.reasons:
            raise InvariantError("needs_approval.reasons_empty")


@dataclass(frozen=True, slots=True)
class Denied:
    """Carries the first failing rule only: the agent revises one thing at a time.

    `ceiling` is `None` before the ceiling is known; `document` is `None` when the
    combination of action and reason is not covered.
    """

    reason: DenialReason
    ceiling: Money | None
    document: PolicyDocumentId | None


type PolicyDecision = Allowed | NeedsApproval | Denied

"""The rows the generator writes: the contract of the dataset files.

Money is integer cents and instants are UTC, as in the domain. The antifraude training rows
(`ClaimRecord`) never carry the customer profile or the flipped-label mark: those live only in
`GroundTruthRecord`, in a separate file.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from deliverius.orders import (
    Customer,
    DeliveryEvent,
    EventActor,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
)
from deliverius.policy import (
    ActionKind,
    ApprovalReason,
    ApprovedBy,
    ClaimReason,
    DenialReason,
    PolicyDocumentId,
)
from deliverius.shared import (
    CustomerId,
    DeliveryEventId,
    Money,
    OrderId,
    OrderItemId,
    RestaurantId,
    SandboxId,
)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive_datetime")
    return value.astimezone(UTC)


type UtcInstant = Annotated[datetime, AfterValidator(_require_utc)]
type Cents = Annotated[int, Field(ge=0)]


class Profile(StrEnum):
    HONEST = "honest"
    OPPORTUNIST = "opportunist"
    FRAUDSTER = "fraudster"


class IncidentKind(StrEnum):
    """What really happened on an order, before any customer says anything."""

    MISSING_ITEM = "missing_item"
    WRONG_ITEM = "wrong_item"
    QUALITY_ISSUE = "quality_issue"
    MINOR_DELAY = "minor_delay"
    MAJOR_DELAY = "major_delay"
    DELIVERY_FAILED = "delivery_failed"
    MARKED_DELIVERED_NOT_RECEIVED = "marked_delivered_not_received"
    DUPLICATE_CHARGE = "duplicate_charge"
    CUSTOMER_CANCELLATION = "customer_cancellation"


class DecisionKind(StrEnum):
    ALLOWED = "allowed"
    NEEDS_APPROVAL = "needs_approval"
    DENIED = "denied"


class Intent(StrEnum):
    ITEM_MISSING = "item_missing"
    WRONG_ITEM = "wrong_item"
    QUALITY_ISSUE = "quality_issue"
    LATE_DELIVERY = "late_delivery"
    NOT_DELIVERED = "not_delivered"
    BILLING_ISSUE = "billing_issue"
    CANCEL_ORDER = "cancel_order"
    COUPON_QUESTION = "coupon_question"
    POLICY_QUESTION = "policy_question"
    SENSITIVE = "sensitive"
    OTHER = "other"


INTENT_OF_REASON: Mapping[ClaimReason, Intent] = MappingProxyType(
    {
        ClaimReason.ITEM_MISSING: Intent.ITEM_MISSING,
        ClaimReason.WRONG_ITEM: Intent.WRONG_ITEM,
        ClaimReason.QUALITY_ISSUE: Intent.QUALITY_ISSUE,
        ClaimReason.LATE_DELIVERY: Intent.LATE_DELIVERY,
        ClaimReason.NOT_DELIVERED: Intent.NOT_DELIVERED,
        ClaimReason.DUPLICATE_CHARGE: Intent.BILLING_ISSUE,
        ClaimReason.CUSTOMER_CANCELLATION: Intent.CANCEL_ORDER,
    }
)


class PiiKind(StrEnum):
    NAME = "name"
    CPF = "cpf"
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"


class Split(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class Flow(StrEnum):
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F4 = "F4"
    F5 = "F5"
    F6 = "F6"
    F7 = "F7"


class ExpectedOutcome(StrEnum):
    ACTION_EXECUTED = "action_executed"
    APPROVAL_REQUESTED = "approval_requested"
    HANDOFF = "handoff"
    NO_ACTION = "no_action"


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CustomerRecord(_Record):
    """Fake personal data, written only to test masking."""

    id: UUID
    sandbox_id: UUID
    created_at: UtcInstant
    name: str
    cpf: str
    phone: str
    email: str
    address: str

    @classmethod
    def from_domain(cls, customer: Customer) -> Self:
        return cls(
            id=customer.id,
            sandbox_id=customer.sandbox_id,
            created_at=customer.created_at,
            name=customer.name,
            cpf=customer.cpf,
            phone=customer.phone,
            email=customer.email,
            address=customer.address,
        )

    def to_domain(self) -> Customer:
        return Customer(
            id=CustomerId(self.id),
            sandbox_id=SandboxId(self.sandbox_id),
            created_at=self.created_at,
            name=self.name,
            cpf=self.cpf,
            phone=self.phone,
            email=self.email,
            address=self.address,
        )


class RestaurantRecord(_Record):
    id: UUID
    name: str
    category: str


class OrderItemRecord(_Record):
    id: UUID
    name: str
    unit_price_cents: Cents
    quantity: int = Field(gt=0)
    allergens: tuple[str, ...] = ()


class DeliveryEventRecord(_Record):
    id: UUID
    status: OrderStatus
    actor: EventActor
    occurred_at: UtcInstant


class OrderRecord(_Record):
    id: UUID
    sandbox_id: UUID
    customer_id: UUID
    restaurant_id: UUID
    status: OrderStatus
    payment_method: PaymentMethod
    placed_at: UtcInstant
    estimated_delivery_at: UtcInstant
    delivered_at: UtcInstant | None
    subtotal_cents: Cents
    delivery_fee_cents: Cents
    discount_cents: Cents
    total_cents: Cents
    notes: str | None
    items: tuple[OrderItemRecord, ...]
    events: tuple[DeliveryEventRecord, ...]

    @classmethod
    def from_domain(cls, order: Order) -> Self:
        return cls(
            id=order.id,
            sandbox_id=order.sandbox_id,
            customer_id=order.customer_id,
            restaurant_id=order.restaurant_id,
            status=order.status,
            payment_method=order.payment_method,
            placed_at=order.placed_at,
            estimated_delivery_at=order.estimated_delivery_at,
            delivered_at=order.delivered_at,
            subtotal_cents=order.subtotal.cents,
            delivery_fee_cents=order.delivery_fee.cents,
            discount_cents=order.discount.cents,
            total_cents=order.total.cents,
            notes=order.notes,
            items=tuple(
                OrderItemRecord(
                    id=item.id,
                    name=item.name,
                    unit_price_cents=item.unit_price.cents,
                    quantity=item.quantity,
                    allergens=item.allergens,
                )
                for item in order.items
            ),
            events=tuple(
                DeliveryEventRecord(
                    id=event.id,
                    status=event.status,
                    actor=event.actor,
                    occurred_at=event.occurred_at,
                )
                for event in order.events
            ),
        )

    def to_domain(self) -> Order:
        return Order(
            id=OrderId(self.id),
            sandbox_id=SandboxId(self.sandbox_id),
            customer_id=CustomerId(self.customer_id),
            restaurant_id=RestaurantId(self.restaurant_id),
            status=self.status,
            payment_method=self.payment_method,
            placed_at=self.placed_at,
            estimated_delivery_at=self.estimated_delivery_at,
            delivered_at=self.delivered_at,
            subtotal=Money(self.subtotal_cents),
            delivery_fee=Money(self.delivery_fee_cents),
            discount=Money(self.discount_cents),
            total=Money(self.total_cents),
            notes=self.notes,
            items=tuple(
                OrderItem(
                    id=OrderItemId(item.id),
                    name=item.name,
                    unit_price=Money(item.unit_price_cents),
                    quantity=item.quantity,
                    allergens=item.allergens,
                )
                for item in self.items
            ),
            events=tuple(
                DeliveryEvent(
                    id=DeliveryEventId(event.id),
                    status=event.status,
                    actor=event.actor,
                    occurred_at=event.occurred_at,
                )
                for event in self.events
            ),
        )


class ClaimedItemRecord(_Record):
    order_item_id: UUID
    quantity: int


class ProposedActionRecord(_Record):
    kind: ActionKind
    reason: ClaimReason
    amount_cents: Cents
    items: tuple[ClaimedItemRecord, ...] = ()


class ResolutionRecord(_Record):
    """What the policy engine decided: the reviewer's later choice is deliberately not here.

    A reviewer who rejects a case says almost for sure that it was fraud, so keeping that outcome
    in a training row would leak the label.
    """

    decision: DecisionKind
    denial_reason: DenialReason | None = None
    approval_reasons: tuple[ApprovalReason, ...] = ()
    document: PolicyDocumentId | None = None
    ceiling_cents: Cents | None = None

    @model_validator(mode="after")
    def _reasons_match_the_decision(self) -> Self:
        if (self.decision is DecisionKind.DENIED) != (self.denial_reason is not None):
            raise ValueError("resolution.denial_reason_mismatch")
        if (self.decision is DecisionKind.NEEDS_APPROVAL) != bool(self.approval_reasons):
            raise ValueError("resolution.approval_reasons_mismatch")
        return self


class ClaimRecord(_Record):
    """A row of the antifraude history: no profile, no flipped-label mark."""

    id: UUID
    order_id: UUID
    customer_id: UUID
    claimed_at: UtcInstant
    reason: ClaimReason
    action: ActionKind
    claimed_items: tuple[ClaimedItemRecord, ...]
    requested_amount_cents: Cents
    resolution: ResolutionRecord
    is_fraud: bool


class GroundTruthRecord(_Record):
    """For analysis only, never a training column: who the customer really is and what happened."""

    claim_id: UUID
    customer_id: UUID
    profile: Profile
    label_flipped: bool
    incident: IncidentKind | None
    exaggerated: bool


class PiiSpan(_Record):
    kind: PiiKind
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _span_is_not_empty(self) -> Self:
        if self.end <= self.start:
            raise ValueError("pii_span.empty")
        return self


class MessageRecord(_Record):
    id: UUID
    scenario_id: UUID
    split: Split
    text: str
    intent: Intent
    pii_spans: tuple[PiiSpan, ...] = ()
    has_injection: bool = False
    persona: str


class PolicyQaRecord(_Record):
    id: UUID
    question: str
    document: PolicyDocumentId
    section: str


class PriorActionRecord(_Record):
    order_id: UUID
    kind: ActionKind
    reason: ClaimReason
    amount_cents: Cents
    items: tuple[ClaimedItemRecord, ...] = ()
    executed_at: UtcInstant
    approved_by: ApprovedBy


class E2EInitialState(_Record):
    """`now` is the instant of the conversation: the policy decision depends on it.

    Prior actions point to earlier orders that are not part of the state.
    """

    now: UtcInstant
    customer: CustomerRecord
    order: OrderRecord
    prior_actions: tuple[PriorActionRecord, ...] = ()


class E2EExpected(_Record):
    """`action` is what must be executed or approved; `policy` is the engine's decision on it.

    `is_fraud` is the truth by construction (does the claim have a real incident behind it?),
    `None` where there is no claim. `cited_document` is the policy a FAQ answer must cite.
    """

    outcome: ExpectedOutcome
    action: ProposedActionRecord | None = None
    policy: ResolutionRecord | None = None
    handoff_priority: Literal["high", "normal"] | None = None
    is_fraud: bool | None = None
    cited_document: PolicyDocumentId | None = None


class E2EScenarioRecord(_Record):
    """`variant` tells a direct injection (in the message) from an indirect one (in the note)."""

    id: UUID
    flow: Flow
    variant: Literal["direct", "indirect"] | None = None
    initial_state: E2EInitialState
    goal: str
    expected: E2EExpected


class Manifest(_Record):
    """Lineage of one generation; `files` maps each written file to its SHA-256."""

    datagen_version: str
    seed: int
    commit: str
    # The working tree had changes that were not in `commit` when the files were written.
    dirty: bool = False
    reference_instant: UtcInstant
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    intent_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    discards: dict[str, int] = Field(default_factory=dict)
    fraud_test_cutoff: UtcInstant | None = None
    label_noise_fraction: float | None = None
    fraud_rate: float | None = None
    files: dict[str, str] = Field(default_factory=dict)

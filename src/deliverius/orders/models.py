from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.ids import (
    CustomerId,
    DeliveryEventId,
    OrderId,
    OrderItemId,
    RestaurantId,
    SandboxId,
)
from deliverius.shared.money import Money


class OrderStatus(StrEnum):
    """Order status; each delivery event is the arrival at one of these."""

    PLACED = "placed"
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    READY_FOR_PICKUP = "ready_for_pickup"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    DELIVERY_FAILED = "delivery_failed"


class EventActor(StrEnum):
    CUSTOMER = "customer"
    RESTAURANT = "restaurant"
    COURIER = "courier"
    SYSTEM = "system"


class PaymentMethod(StrEnum):
    """In-app payment only, so every refund goes back through the same method."""

    PIX = "pix"
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    MEAL_VOUCHER = "meal_voucher"


# Forward one step at a time; cancel only before the courier leaves; fail only after.
ALLOWED_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLACED: frozenset({OrderStatus.ACCEPTED, OrderStatus.CANCELLED}),
    OrderStatus.ACCEPTED: frozenset({OrderStatus.PREPARING, OrderStatus.CANCELLED}),
    OrderStatus.PREPARING: frozenset({OrderStatus.READY_FOR_PICKUP, OrderStatus.CANCELLED}),
    OrderStatus.READY_FOR_PICKUP: frozenset({OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED}),
    OrderStatus.OUT_FOR_DELIVERY: frozenset({OrderStatus.DELIVERED, OrderStatus.DELIVERY_FAILED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.DELIVERY_FAILED: frozenset(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


@dataclass(frozen=True, slots=True)
class Customer:
    """Personal fields are hidden from `repr`, so a stray log or traceback does not print them."""

    id: CustomerId
    sandbox_id: SandboxId
    created_at: datetime
    name: str = field(repr=False)
    cpf: str = field(repr=False)
    phone: str = field(repr=False)
    email: str = field(repr=False)
    address: str = field(repr=False)

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class Restaurant:
    id: RestaurantId
    name: str
    category: str


@dataclass(frozen=True, slots=True)
class OrderItem:
    id: OrderItemId
    name: str
    unit_price: Money
    quantity: int
    allergens: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.unit_price.cents <= 0:
            raise InvariantError("order_item.unit_price_not_positive")
        if self.quantity <= 0:
            raise InvariantError("order_item.quantity_not_positive")

    @property
    def total(self) -> Money:
        return self.unit_price * self.quantity


@dataclass(frozen=True, slots=True)
class DeliveryEvent:
    id: DeliveryEventId
    status: OrderStatus
    actor: EventActor
    occurred_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class OrderSummary:
    """What an order list shows: no items, no notes."""

    id: OrderId
    customer_id: CustomerId
    restaurant_id: RestaurantId
    status: OrderStatus
    placed_at: datetime
    total: Money

    def __post_init__(self) -> None:
        require_aware(self.placed_at, "placed_at")


@dataclass(frozen=True, slots=True)
class Order:
    id: OrderId
    sandbox_id: SandboxId
    customer_id: CustomerId
    restaurant_id: RestaurantId
    status: OrderStatus
    payment_method: PaymentMethod
    placed_at: datetime
    estimated_delivery_at: datetime
    delivered_at: datetime | None
    subtotal: Money
    delivery_fee: Money
    discount: Money
    total: Money
    # Free text from outside: never interpreted, delimited whenever it reaches a prompt.
    notes: str | None
    items: tuple[OrderItem, ...]
    events: tuple[DeliveryEvent, ...]

    def __post_init__(self) -> None:
        require_aware(self.placed_at, "placed_at")
        require_aware(self.estimated_delivery_at, "estimated_delivery_at")
        if self.delivered_at is not None:
            require_aware(self.delivered_at, "delivered_at")
        if self.subtotal + self.delivery_fee - self.discount != self.total:
            raise InvariantError("order.total_inconsistent")

    def summary(self) -> OrderSummary:
        return OrderSummary(
            id=self.id,
            customer_id=self.customer_id,
            restaurant_id=self.restaurant_id,
            status=self.status,
            placed_at=self.placed_at,
            total=self.total,
        )

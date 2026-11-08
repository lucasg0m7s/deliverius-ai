"""Builders of valid orders for tests: a full, valid timeline up to the requested status."""

from datetime import UTC, datetime, timedelta

from deliverius.orders.models import (
    DeliveryEvent,
    EventActor,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
)
from deliverius.shared.ids import (
    CustomerId,
    DeliveryEventId,
    OrderId,
    OrderItemId,
    RestaurantId,
    SandboxId,
    new_id,
)
from deliverius.shared.money import Money

BASE_TIME = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
RESTAURANT_ID = RestaurantId(new_id())

HAPPY_PATH = (
    OrderStatus.PLACED,
    OrderStatus.ACCEPTED,
    OrderStatus.PREPARING,
    OrderStatus.READY_FOR_PICKUP,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
)
ACTOR_OF = {
    OrderStatus.PLACED: EventActor.CUSTOMER,
    OrderStatus.ACCEPTED: EventActor.RESTAURANT,
    OrderStatus.PREPARING: EventActor.RESTAURANT,
    OrderStatus.READY_FOR_PICKUP: EventActor.RESTAURANT,
    OrderStatus.OUT_FOR_DELIVERY: EventActor.COURIER,
    OrderStatus.DELIVERED: EventActor.COURIER,
}


def build_order(
    *,
    sandbox_id: SandboxId,
    customer_id: CustomerId,
    placed_at: datetime = BASE_TIME,
    item_count: int = 2,
    delivered: bool = True,
    notes: str | None = None,
) -> Order:
    items = tuple(
        OrderItem(
            id=OrderItemId(new_id()),
            name=f"Item {index}",
            unit_price=Money(1500 + index),
            quantity=1 + index % 2,
            allergens=("gluten",) if index % 3 == 0 else (),
        )
        for index in range(item_count)
    )
    subtotal = Money.sum(item.total for item in items)
    delivery_fee = Money(799)
    discount = Money(500)
    path = HAPPY_PATH if delivered else HAPPY_PATH[:3]
    events = tuple(
        DeliveryEvent(
            id=DeliveryEventId(new_id()),
            status=status,
            actor=ACTOR_OF[status],
            occurred_at=placed_at + timedelta(minutes=7 * step),
        )
        for step, status in enumerate(path)
    )
    return Order(
        id=OrderId(new_id()),
        sandbox_id=sandbox_id,
        customer_id=customer_id,
        restaurant_id=RESTAURANT_ID,
        status=path[-1],
        payment_method=PaymentMethod.CREDIT_CARD,
        placed_at=placed_at,
        estimated_delivery_at=placed_at + timedelta(minutes=45),
        delivered_at=events[-1].occurred_at if delivered else None,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        discount=discount,
        total=subtotal + delivery_fee - discount,
        notes=notes,
        items=items,
        events=events,
    )


def path_to(status: OrderStatus) -> tuple[OrderStatus, ...]:
    """The shortest valid timeline that ends in `status`."""
    if status is OrderStatus.CANCELLED:
        return (OrderStatus.PLACED, OrderStatus.CANCELLED)
    if status is OrderStatus.DELIVERY_FAILED:
        return (*HAPPY_PATH[:5], OrderStatus.DELIVERY_FAILED)
    return HAPPY_PATH[: HAPPY_PATH.index(status) + 1]


def build_order_in(
    status: OrderStatus,
    items: tuple[OrderItem, ...],
    *,
    placed_at: datetime = BASE_TIME,
    estimated_delivery_at: datetime | None = None,
    final_event_at: datetime | None = None,
    delivery_fee: Money = Money(0),
    discount: Money = Money(0),
    customer_id: CustomerId | None = None,
) -> Order:
    """An order in any status, with the given items; the last event happens at `final_event_at`."""
    path = path_to(status)
    times = [placed_at + timedelta(minutes=7 * step) for step in range(len(path))]
    if final_event_at is not None:
        times[-1] = final_event_at
    events = tuple(
        DeliveryEvent(
            id=DeliveryEventId(new_id()),
            status=step_status,
            actor=ACTOR_OF.get(step_status, EventActor.SYSTEM),
            occurred_at=moment,
        )
        for step_status, moment in zip(path, times, strict=True)
    )
    subtotal = Money.sum(item.total for item in items)
    return Order(
        id=OrderId(new_id()),
        sandbox_id=SandboxId(new_id()),
        customer_id=customer_id or CustomerId(new_id()),
        restaurant_id=RESTAURANT_ID,
        status=status,
        payment_method=PaymentMethod.PIX,
        placed_at=placed_at,
        estimated_delivery_at=estimated_delivery_at or placed_at + timedelta(minutes=45),
        delivered_at=times[-1] if status is OrderStatus.DELIVERED else None,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        discount=discount,
        total=subtotal + delivery_fee - discount,
        notes=None,
        items=items,
        events=events,
    )


def build_item(name: str, cents: int, quantity: int = 1) -> OrderItem:
    return OrderItem(
        id=OrderItemId(new_id()), name=name, unit_price=Money(cents), quantity=quantity
    )

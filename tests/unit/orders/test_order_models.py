from datetime import UTC, datetime, timedelta

import pytest

from deliverius.db.tables import AUDIT_ACTOR_TYPES, EVENT_ACTORS, ORDER_STATUSES, PAYMENT_METHODS
from deliverius.orders.models import (
    ALLOWED_TRANSITIONS,
    Customer,
    DeliveryEvent,
    EventActor,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    can_transition,
)
from deliverius.shared.audit import AuditActorType
from deliverius.shared.errors import InvariantError
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

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
S = OrderStatus

# The diagram in the design, edge by edge.
EXPECTED_EDGES = {
    (S.PLACED, S.ACCEPTED),
    (S.ACCEPTED, S.PREPARING),
    (S.PREPARING, S.READY_FOR_PICKUP),
    (S.READY_FOR_PICKUP, S.OUT_FOR_DELIVERY),
    (S.OUT_FOR_DELIVERY, S.DELIVERED),
    (S.OUT_FOR_DELIVERY, S.DELIVERY_FAILED),
    (S.PLACED, S.CANCELLED),
    (S.ACCEPTED, S.CANCELLED),
    (S.PREPARING, S.CANCELLED),
    (S.READY_FOR_PICKUP, S.CANCELLED),
}


def make_order(**overrides: object) -> Order:
    fields: dict[str, object] = {
        "id": OrderId(new_id()),
        "sandbox_id": SandboxId(new_id()),
        "customer_id": CustomerId(new_id()),
        "restaurant_id": RestaurantId(new_id()),
        "status": S.DELIVERED,
        "payment_method": PaymentMethod.PIX,
        "placed_at": NOW,
        "estimated_delivery_at": NOW + timedelta(minutes=40),
        "delivered_at": NOW + timedelta(minutes=38),
        "subtotal": Money(5000),
        "delivery_fee": Money(799),
        "discount": Money(1000),
        "total": Money(4799),
        "notes": None,
        "items": (),
        "events": (),
    }
    return Order(**(fields | overrides))  # type: ignore[arg-type]


def test_transitions_match_the_design_diagram() -> None:
    actual = {
        (source, target) for source, targets in ALLOWED_TRANSITIONS.items() for target in targets
    }

    assert actual == EXPECTED_EDGES
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (S.OUT_FOR_DELIVERY, S.CANCELLED),
        (S.PLACED, S.PREPARING),
        (S.PLACED, S.DELIVERED),
        (S.DELIVERED, S.CANCELLED),
        (S.PREPARING, S.DELIVERY_FAILED),
        (S.CANCELLED, S.PLACED),
    ],
)
def test_invalid_transitions(source: OrderStatus, target: OrderStatus) -> None:
    assert not can_transition(source, target)


def test_cancel_is_valid_before_leaving_for_delivery() -> None:
    assert can_transition(S.READY_FOR_PICKUP, S.CANCELLED)


def test_customer_repr_hides_personal_data() -> None:
    customer = Customer(
        id=CustomerId(new_id()),
        sandbox_id=SandboxId(new_id()),
        created_at=NOW,
        name="Ana Souza",
        cpf="123.456.789-09",
        phone="+55 11 91234-5678",
        email="ana@example.com",
        address="Rua das Flores, 10",
    )

    text = repr(customer)

    for secret in (
        "Ana Souza",
        "123.456.789-09",
        "91234-5678",
        "ana@example.com",
        "Rua das Flores",
    ):
        assert secret not in text
    assert str(customer.id) in text


def test_order_total_must_match_its_parts() -> None:
    with pytest.raises(InvariantError, match=r"order\.total_inconsistent"):
        make_order(total=Money(4800))


def test_order_rejects_discount_above_amount() -> None:
    with pytest.raises(InvariantError, match=r"money\.negative"):
        make_order(subtotal=Money(100), delivery_fee=Money(0), discount=Money(200), total=Money(0))


@pytest.mark.parametrize("field", ["placed_at", "estimated_delivery_at", "delivered_at"])
def test_order_rejects_naive_instants(field: str) -> None:
    with pytest.raises(InvariantError, match=rf"naive_datetime\.{field}"):
        make_order(**{field: datetime(2026, 11, 1, 12, 0)})  # noqa: DTZ001


def test_event_rejects_naive_instant() -> None:
    with pytest.raises(InvariantError, match=r"naive_datetime\.occurred_at"):
        DeliveryEvent(
            id=DeliveryEventId(new_id()),
            status=S.PLACED,
            actor=EventActor.CUSTOMER,
            occurred_at=datetime(2026, 11, 1, 12, 0),  # noqa: DTZ001
        )


@pytest.mark.parametrize(
    ("price", "quantity", "rule"),
    [(0, 1, "unit_price_not_positive"), (100, 0, "quantity_not_positive")],
)
def test_item_rejects_non_positive_price_or_quantity(price: int, quantity: int, rule: str) -> None:
    with pytest.raises(InvariantError, match=rf"order_item\.{rule}"):
        OrderItem(id=OrderItemId(new_id()), name="x", unit_price=Money(price), quantity=quantity)


def test_item_total() -> None:
    item = OrderItem(id=OrderItemId(new_id()), name="x", unit_price=Money(1250), quantity=3)

    assert item.total == Money(3750)


def test_summary_carries_only_list_fields() -> None:
    order = make_order(notes="ring the bell")

    summary = order.summary()

    assert summary.id == order.id
    assert summary.total == order.total
    assert not hasattr(summary, "notes")


def test_database_checks_list_the_same_values_as_the_enums() -> None:
    assert set(ORDER_STATUSES) == {status.value for status in OrderStatus}
    assert set(PAYMENT_METHODS) == {method.value for method in PaymentMethod}
    assert set(EVENT_ACTORS) == {actor.value for actor in EventActor}
    assert set(AUDIT_ACTOR_TYPES) == {actor.value for actor in AuditActorType}

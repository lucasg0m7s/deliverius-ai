"""Layer 1: the simulated world. Customers, restaurants and six months of orders, all from the seed.

Nothing here knows about claims. A customer has a behavior profile, an order has a delivery
timeline that follows `ALLOWED_TRANSITIONS`, and what went wrong with it (the incidents) is drawn at
the configured rates.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import structlog
from faker import Faker

from deliverius.datagen.config import DatagenConfig, IncidentRates
from deliverius.datagen.records import (
    CustomerRecord,
    IncidentKind,
    OrderRecord,
    Profile,
    RestaurantRecord,
)
from deliverius.datagen.rng import IdFactory, Layer, count_for_rate, layer_faker, layer_random
from deliverius.orders import (
    Customer,
    DeliveryEvent,
    EventActor,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    Restaurant,
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

logger = structlog.get_logger(__name__)

_DAY_SECONDS: Final = 86_400
_CUSTOMER_HISTORY: Final = timedelta(days=365)
_RESTAURANT_HISTORY: Final = timedelta(days=500)

HAPPY_PATH: Final = (
    OrderStatus.PLACED,
    OrderStatus.ACCEPTED,
    OrderStatus.PREPARING,
    OrderStatus.READY_FOR_PICKUP,
    OrderStatus.OUT_FOR_DELIVERY,
)
ACTOR_OF: Final = {
    OrderStatus.PLACED: EventActor.CUSTOMER,
    OrderStatus.ACCEPTED: EventActor.RESTAURANT,
    OrderStatus.PREPARING: EventActor.RESTAURANT,
    OrderStatus.READY_FOR_PICKUP: EventActor.RESTAURANT,
    OrderStatus.OUT_FOR_DELIVERY: EventActor.COURIER,
    OrderStatus.DELIVERED: EventActor.COURIER,
    OrderStatus.DELIVERY_FAILED: EventActor.COURIER,
    OrderStatus.CANCELLED: EventActor.CUSTOMER,
}
# Minutes between one status and the next; the last leg absorbs whatever delay was drawn.
_STEP_MINUTES: Final = ((1, 3), (1, 3), (6, 14), (2, 5))
_PAYMENT_WEIGHTS: Final = {
    PaymentMethod.PIX: 4,
    PaymentMethod.CREDIT_CARD: 4,
    PaymentMethod.DEBIT_CARD: 1,
    PaymentMethod.MEAL_VOUCHER: 1,
}
_HOUR_WEIGHTS: Final = {
    11: 3,
    12: 5,
    13: 4,
    14: 2,
    15: 1,
    17: 1,
    18: 3,
    19: 5,
    20: 5,
    21: 4,
    22: 2,
    23: 1,
}
_DELIVERY_FEES: Final = (0, 399, 499, 599, 799, 999)
_DISCOUNTS: Final = (300, 500, 1_000, 1_500)
_NOTES: Final = (
    "sem cebola",
    "tocar a campainha",
    "deixar na portaria",
    "sem talheres",
    "pouco sal",
)
# On-time deliveries land between 5 minutes early and 25 minutes late (below the 30 of the policy).
_ON_TIME_MINUTES: Final = (-5, 25)
_MINOR_DELAY_MINUTES: Final = (30, 60)
_MAJOR_DELAY_MINUTES: Final = (61, 120)

type _MenuEntry = tuple[str, int, int, tuple[str, ...]]

_CATALOG: Final[dict[str, tuple[_MenuEntry, ...]]] = {
    "Pizzaria": (
        ("Pizza de mussarela", 38, 52, ("gluten", "lactose")),
        ("Pizza calabresa", 40, 56, ("gluten", "lactose")),
        ("Pizza portuguesa", 44, 60, ("gluten", "lactose", "ovo")),
        ("Pizza quatro queijos", 46, 62, ("gluten", "lactose")),
        ("Pizza de frango com catupiry", 44, 58, ("gluten", "lactose")),
        ("Borda recheada", 8, 14, ("gluten", "lactose")),
        ("Refrigerante 2 litros", 9, 14, ()),
        ("Suco de laranja", 7, 11, ()),
    ),
    "Hamburgueria": (
        ("Hambúrguer clássico", 24, 34, ("gluten", "lactose")),
        ("Hambúrguer duplo", 32, 44, ("gluten", "lactose")),
        ("Cheeseburger com bacon", 30, 42, ("gluten", "lactose")),
        ("Hambúrguer vegetariano", 26, 36, ("gluten", "soja")),
        ("Batata frita", 12, 20, ()),
        ("Anéis de cebola", 14, 22, ("gluten",)),
        ("Milkshake de chocolate", 16, 24, ("lactose",)),
        ("Refrigerante lata", 5, 8, ()),
    ),
    "Japonesa": (
        ("Combinado de 20 peças", 58, 82, ("peixe", "soja")),
        ("Temaki de salmão", 24, 34, ("peixe", "soja")),
        ("Hot roll", 26, 38, ("peixe", "gluten", "soja")),
        ("Yakisoba", 32, 46, ("gluten", "soja")),
        ("Sashimi de salmão", 36, 52, ("peixe",)),
        ("Missoshiru", 9, 14, ("soja",)),
        ("Guioza", 18, 28, ("gluten", "soja")),
        ("Chá gelado", 6, 10, ()),
    ),
    "Brasileira": (
        ("Prato feito de frango", 24, 34, ()),
        ("Feijoada completa", 38, 54, ()),
        ("Filé à parmegiana", 36, 52, ("gluten", "lactose", "ovo")),
        ("Strogonoff de carne", 30, 44, ("lactose",)),
        ("Marmita fitness", 22, 32, ()),
        ("Pudim de leite", 8, 14, ("lactose", "ovo")),
        ("Suco natural", 7, 12, ()),
        ("Refrigerante lata", 5, 8, ()),
    ),
    "Lanchonete": (
        ("X-burger", 16, 24, ("gluten", "lactose")),
        ("X-salada", 18, 26, ("gluten", "lactose", "ovo")),
        ("Misto quente", 10, 16, ("gluten", "lactose")),
        ("Coxinha", 6, 10, ("gluten", "lactose")),
        ("Pastel de carne", 8, 14, ("gluten",)),
        ("Pão de queijo", 5, 9, ("lactose",)),
        ("Vitamina de banana", 9, 14, ("lactose",)),
        ("Água mineral", 3, 5, ()),
    ),
    "Açaí": (
        ("Açaí 300 ml", 14, 20, ()),
        ("Açaí 500 ml", 20, 28, ()),
        ("Açaí 700 ml", 26, 36, ()),
        ("Tigela de frutas", 16, 24, ()),
        ("Adicional de leite em pó", 3, 5, ("lactose",)),
        ("Adicional de paçoca", 3, 5, ("amendoim",)),
        ("Suco detox", 9, 14, ()),
        ("Água de coco", 6, 9, ()),
    ),
    "Padaria": (
        ("Pão francês (6 unidades)", 6, 10, ("gluten",)),
        ("Sanduíche natural", 12, 18, ("gluten", "lactose")),
        ("Bolo de cenoura (fatia)", 7, 11, ("gluten", "ovo", "lactose")),
        ("Café com leite", 5, 8, ("lactose",)),
        ("Croissant", 8, 13, ("gluten", "lactose", "ovo")),
        ("Torta de frango", 10, 16, ("gluten", "ovo")),
        ("Suco de uva", 6, 10, ()),
        ("Pão de queijo (6 unidades)", 9, 14, ("lactose",)),
    ),
    "Doceria": (
        ("Brigadeiro (caixa com 6)", 16, 24, ("lactose",)),
        ("Bolo no pote", 12, 18, ("gluten", "ovo", "lactose")),
        ("Torta de limão (fatia)", 12, 18, ("gluten", "lactose", "ovo")),
        ("Cookie recheado", 9, 14, ("gluten", "ovo", "lactose", "amendoim")),
        ("Brownie", 10, 16, ("gluten", "ovo", "lactose")),
        ("Cheesecake (fatia)", 14, 22, ("gluten", "lactose", "ovo")),
        ("Sorvete 2 bolas", 12, 18, ("lactose",)),
        ("Café coado", 5, 8, ()),
    ),
    "Italiana": (
        ("Lasanha à bolonhesa", 36, 52, ("gluten", "lactose", "ovo")),
        ("Spaghetti ao sugo", 28, 40, ("gluten",)),
        ("Fettuccine alfredo", 34, 48, ("gluten", "lactose", "ovo")),
        ("Risoto de cogumelos", 38, 54, ("lactose",)),
        ("Nhoque ao molho de tomate", 30, 42, ("gluten", "ovo")),
        ("Tiramisù", 16, 24, ("gluten", "ovo", "lactose")),
        ("Pão de alho", 10, 16, ("gluten", "lactose")),
        ("Água com gás", 4, 7, ()),
    ),
    "Árabe": (
        ("Esfiha de carne (6 unidades)", 20, 30, ("gluten",)),
        ("Quibe frito (6 unidades)", 18, 28, ("gluten",)),
        ("Kafta no prato", 32, 46, ("gluten",)),
        ("Shawarma de frango", 24, 34, ("gluten", "sesamo")),
        ("Homus com pão sírio", 20, 30, ("gluten", "sesamo")),
        ("Falafel", 22, 32, ("gluten", "sesamo")),
        ("Baklava", 14, 22, ("gluten", "castanhas")),
        ("Suco de limão com hortelã", 7, 11, ()),
    ),
    "Saudável": (
        ("Salada caesar com frango", 28, 40, ("lactose", "gluten", "ovo")),
        ("Bowl de quinoa", 30, 42, ()),
        ("Wrap integral de frango", 24, 34, ("gluten",)),
        ("Omelete de claras", 18, 26, ("ovo",)),
        ("Sopa de legumes", 20, 28, ()),
        ("Smoothie verde", 14, 20, ()),
        ("Barra de cereais", 5, 9, ("amendoim", "gluten")),
        ("Água de coco", 6, 9, ()),
    ),
    "Churrascaria": (
        ("Picanha na chapa", 62, 88, ()),
        ("Espetinho misto (3 unidades)", 24, 34, ()),
        ("Costela assada", 56, 78, ()),
        ("Arroz, farofa e vinagrete", 12, 18, ()),
        ("Pão de alho", 8, 12, ("gluten", "lactose")),
        ("Mandioca frita", 14, 20, ()),
        ("Pudim de leite", 8, 14, ("lactose", "ovo")),
        ("Refrigerante 600 ml", 6, 9, ()),
    ),
}


@dataclass(frozen=True, slots=True)
class SimCustomer:
    customer: Customer
    profile: Profile
    # A few honest customers just have bad luck: several real incidents in a row.
    unlucky: bool


@dataclass(frozen=True, slots=True)
class MenuItem:
    name: str
    unit_price: Money
    allergens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimRestaurant:
    restaurant: Restaurant
    menu: tuple[MenuItem, ...]


@dataclass(frozen=True, slots=True)
class Incident:
    """Something that really happened to an order; `item_id` and `quantity` for item incidents."""

    kind: IncidentKind
    item_id: OrderItemId | None = None
    quantity: int = 0


@dataclass(frozen=True, slots=True)
class SimOrder:
    order: Order
    incidents: tuple[Incident, ...]


@dataclass(frozen=True, slots=True)
class World:
    sandbox_id: SandboxId
    customers: tuple[SimCustomer, ...]
    restaurants: tuple[SimRestaurant, ...]
    orders: tuple[SimOrder, ...]

    def customer_records(self) -> tuple[CustomerRecord, ...]:
        return tuple(CustomerRecord.from_domain(sim.customer) for sim in self.customers)

    def restaurant_records(self) -> tuple[RestaurantRecord, ...]:
        return tuple(
            RestaurantRecord(
                id=sim.restaurant.id, name=sim.restaurant.name, category=sim.restaurant.category
            )
            for sim in self.restaurants
        )

    def order_records(self) -> tuple[OrderRecord, ...]:
        return tuple(OrderRecord.from_domain(sim.order) for sim in self.orders)


def generate_world(config: DatagenConfig) -> World:
    customers = generate_customers(config)
    restaurants = generate_restaurants(config)
    orders = generate_orders(config, customers, restaurants)
    logger.info(
        "datagen.world_generated",
        customers=len(customers),
        restaurants=len(restaurants),
        orders=len(orders),
        incidents=sum(len(sim.incidents) for sim in orders),
    )
    return World(sandbox_id_for(config), customers, restaurants, orders)


def sandbox_id_for(config: DatagenConfig) -> SandboxId:
    ids = IdFactory(layer_random(config.seed, Layer.SANDBOX))
    return SandboxId(ids.next_id(config.period_start))


def generate_customers(config: DatagenConfig) -> tuple[SimCustomer, ...]:
    rng = layer_random(config.seed, Layer.CUSTOMERS)
    faker = layer_faker(config.seed, Layer.CUSTOMERS)
    ids = IdFactory(rng)
    sandbox_id = sandbox_id_for(config)
    profiles = _assign_profiles(config, rng)
    honest = [index for index, profile in enumerate(profiles) if profile is Profile.HONEST]
    unlucky = set(
        rng.sample(honest, count_for_rate(len(honest), config.claim_rates.unlucky_honest_share))
    )
    created = sorted(
        config.period_start
        - timedelta(seconds=rng.randrange(60, int(_CUSTOMER_HISTORY.total_seconds())))
        for _ in range(config.customer_count)
    )
    return tuple(
        SimCustomer(
            customer=build_customer(faker, ids, sandbox_id, created_at),
            profile=profiles[index],
            unlucky=index in unlucky,
        )
        for index, created_at in enumerate(created)
    )


def build_customer(
    faker: Faker, ids: IdFactory, sandbox_id: SandboxId, created_at: datetime
) -> Customer:
    """A customer with fake personal data; the ID is drawn before the Faker values."""
    return Customer(
        id=CustomerId(ids.next_id(created_at)),
        sandbox_id=sandbox_id,
        created_at=created_at,
        name=faker.name(),
        cpf=faker.cpf(),
        phone=faker.cellphone_number(),
        email=faker.email(),
        address=faker.address().replace("\n", ", "),
    )


def generate_restaurants(config: DatagenConfig) -> tuple[SimRestaurant, ...]:
    rng = layer_random(config.seed, Layer.RESTAURANTS)
    faker = layer_faker(config.seed, Layer.RESTAURANTS)
    ids = IdFactory(rng)
    categories = sorted(_CATALOG)
    restaurants: list[SimRestaurant] = []
    for index in range(config.restaurant_count):
        category = rng.choice(categories)
        entries = rng.sample(_CATALOG[category], rng.randint(6, len(_CATALOG[category])))
        created_at = config.period_start - _RESTAURANT_HISTORY + timedelta(minutes=index)
        restaurant = Restaurant(
            id=RestaurantId(ids.next_id(created_at)),
            name=f"{category} {faker.last_name()}",
            category=category,
        )
        menu = tuple(
            MenuItem(name, Money(rng.randint(low, high) * 100 - 10), allergens)
            for name, low, high, allergens in entries
        )
        restaurants.append(SimRestaurant(restaurant, menu))
    return tuple(restaurants)


def generate_orders(
    config: DatagenConfig,
    customers: Sequence[SimCustomer],
    restaurants: Sequence[SimRestaurant],
) -> tuple[SimOrder, ...]:
    rng = layer_random(config.seed, Layer.ORDERS)
    context = _OrderContext(config, rng, IdFactory(rng), sandbox_id_for(config))
    placed_times = sorted(_draw_placed_at(rng, config) for _ in range(config.order_count))
    orders: list[SimOrder] = []
    for placed_at in placed_times:
        customer, restaurant = rng.choice(customers), rng.choice(restaurants)
        orders.append(_build_order(context, customer, restaurant, placed_at))
    return tuple(orders)


def _assign_profiles(config: DatagenConfig, rng: random.Random) -> list[Profile]:
    """Exact counts, shuffled: the proportions are the configured ones, not a draw."""
    total, mix = config.customer_count, config.profile_mix
    opportunists = count_for_rate(total, mix.opportunist)
    fraudsters = count_for_rate(total, mix.fraudster)
    honest = total - opportunists - fraudsters
    profiles = (
        [Profile.HONEST] * honest
        + [Profile.OPPORTUNIST] * opportunists
        + [Profile.FRAUDSTER] * fraudsters
    )
    rng.shuffle(profiles)
    return profiles


def _draw_placed_at(rng: random.Random, config: DatagenConfig) -> datetime:
    days = (config.orders_end - config.period_start).days
    day = config.period_start + timedelta(days=rng.randrange(days))
    hour = rng.choices(list(_HOUR_WEIGHTS), weights=list(_HOUR_WEIGHTS.values()))[0]
    return day + timedelta(hours=hour, minutes=rng.randrange(60), seconds=rng.randrange(60))


@dataclass(frozen=True, slots=True)
class _OrderContext:
    config: DatagenConfig
    rng: random.Random
    ids: IdFactory
    sandbox_id: SandboxId


def _build_order(
    context: _OrderContext, customer: SimCustomer, restaurant: SimRestaurant, placed_at: datetime
) -> SimOrder:
    rng, ids = context.rng, context.ids
    rates = _rates_for(context.config, customer)
    items = _draw_items(context, restaurant, placed_at)
    subtotal = Money.sum(item.total for item in items)
    delivery_fee = Money(rng.choice(_DELIVERY_FEES))
    discount = Money(min(rng.choice(_DISCOUNTS), subtotal.cents // 2) if rng.random() < 0.3 else 0)
    estimated_minutes = rng.randint(35, 60)
    fate, delay_minutes = _draw_fate(rng, rates)
    events = _timeline(context, placed_at, fate, estimated_minutes, delay_minutes)
    status = events[-1].status
    order = Order(
        id=OrderId(ids.next_id(placed_at)),
        sandbox_id=context.sandbox_id,
        customer_id=customer.customer.id,
        restaurant_id=restaurant.restaurant.id,
        status=status,
        payment_method=_draw_payment(rng),
        placed_at=placed_at,
        estimated_delivery_at=placed_at + timedelta(minutes=estimated_minutes),
        delivered_at=events[-1].occurred_at if status is OrderStatus.DELIVERED else None,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        discount=discount,
        total=subtotal + delivery_fee - discount,
        notes=rng.choice(_NOTES) if rng.random() < 0.12 else None,
        items=items,
        events=events,
    )
    return SimOrder(order, _draw_incidents(rng, rates, order, delay_minutes))


def _rates_for(config: DatagenConfig, customer: SimCustomer) -> IncidentRates:
    if customer.unlucky:
        return config.incident_rates.scaled(config.claim_rates.unlucky_multiplier)
    return config.incident_rates


def _draw_items(
    context: _OrderContext, restaurant: SimRestaurant, placed_at: datetime
) -> tuple[OrderItem, ...]:
    rng = context.rng
    count = rng.choices((1, 2, 3, 4), weights=(3, 4, 2, 1))[0]
    chosen = rng.sample(restaurant.menu, min(count, len(restaurant.menu)))
    return tuple(
        OrderItem(
            id=OrderItemId(context.ids.next_id(placed_at)),
            name=entry.name,
            unit_price=entry.unit_price,
            quantity=rng.choices((1, 2, 3), weights=(7, 2, 1))[0],
            allergens=entry.allergens,
        )
        for entry in chosen
    )


def _draw_payment(rng: random.Random) -> PaymentMethod:
    return rng.choices(list(_PAYMENT_WEIGHTS), weights=list(_PAYMENT_WEIGHTS.values()))[0]


def _draw_fate(rng: random.Random, rates: IncidentRates) -> tuple[OrderStatus, int]:
    """How the order ends and, if it is delivered, how many minutes after the estimate."""
    roll = rng.random()
    if roll < rates.customer_cancellation:
        return OrderStatus.CANCELLED, 0
    if roll < rates.customer_cancellation + rates.delivery_failed:
        return OrderStatus.DELIVERY_FAILED, 0
    delay_roll = rng.random()
    if delay_roll < rates.major_delay:
        return OrderStatus.DELIVERED, rng.randint(*_MAJOR_DELAY_MINUTES)
    if delay_roll < rates.major_delay + rates.minor_delay:
        return OrderStatus.DELIVERED, rng.randint(*_MINOR_DELAY_MINUTES)
    return OrderStatus.DELIVERED, rng.randint(*_ON_TIME_MINUTES)


def _timeline(
    context: _OrderContext,
    placed_at: datetime,
    fate: OrderStatus,
    estimated_minutes: int,
    delay_minutes: int,
) -> tuple[DeliveryEvent, ...]:
    rng = context.rng
    gaps = [rng.randint(low, high) for low, high in _STEP_MINUTES]
    if fate is OrderStatus.CANCELLED:
        stop = rng.choices(range(4), weights=(3, 3, 2, 1))[0]
        statuses = HAPPY_PATH[: stop + 1]
        offsets = [sum(gaps[:step]) for step in range(len(statuses))]
        statuses = (*statuses, OrderStatus.CANCELLED)
        offsets.append(offsets[-1] + rng.randint(1, 6))
    else:
        statuses = (*HAPPY_PATH, fate)
        offsets = [sum(gaps[:step]) for step in range(len(HAPPY_PATH))]
        if fate is OrderStatus.DELIVERED:
            offsets.append(estimated_minutes + delay_minutes)
        else:
            offsets.append(offsets[-1] + rng.randint(15, 40))
    return tuple(
        DeliveryEvent(
            id=DeliveryEventId(context.ids.next_id(placed_at + timedelta(minutes=offset))),
            status=status,
            actor=ACTOR_OF[status],
            occurred_at=placed_at + timedelta(minutes=offset),
        )
        for status, offset in zip(statuses, offsets, strict=True)
    )


def _draw_incidents(
    rng: random.Random, rates: IncidentRates, order: Order, delay_minutes: int
) -> tuple[Incident, ...]:
    incidents: list[Incident] = []
    match order.status:
        case OrderStatus.CANCELLED:
            incidents.append(Incident(IncidentKind.CUSTOMER_CANCELLATION))
        case OrderStatus.DELIVERY_FAILED:
            incidents.append(Incident(IncidentKind.DELIVERY_FAILED))
        case _:
            incidents += _delivered_incidents(rng, rates, order, delay_minutes)
    if rng.random() < rates.duplicate_charge:
        incidents.append(Incident(IncidentKind.DUPLICATE_CHARGE))
    return tuple(incidents)


def _delivered_incidents(
    rng: random.Random, rates: IncidentRates, order: Order, delay_minutes: int
) -> list[Incident]:
    incidents: list[Incident] = []
    if delay_minutes >= _MAJOR_DELAY_MINUTES[0]:
        incidents.append(Incident(IncidentKind.MAJOR_DELAY))
    elif delay_minutes >= _MINOR_DELAY_MINUTES[0]:
        incidents.append(Incident(IncidentKind.MINOR_DELAY))
    if rng.random() < rates.marked_delivered_not_received:
        incidents.append(Incident(IncidentKind.MARKED_DELIVERED_NOT_RECEIVED))
    for kind, rate in (
        (IncidentKind.MISSING_ITEM, rates.missing_item),
        (IncidentKind.WRONG_ITEM, rates.wrong_item),
        (IncidentKind.QUALITY_ISSUE, rates.quality_issue),
    ):
        if rng.random() < rate:
            item = rng.choice(order.items)
            incidents.append(Incident(kind, item.id, rng.randint(1, item.quantity)))
    return incidents

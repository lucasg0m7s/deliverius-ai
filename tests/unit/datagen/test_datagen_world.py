import math
from collections import Counter
from datetime import timedelta
from itertools import pairwise

import pytest
from support.datagen_configs import SMALL_CONFIG

from deliverius.datagen import DatagenConfig, World, generate_world
from deliverius.datagen.config import ClaimRates, IncidentRates
from deliverius.datagen.records import CustomerRecord, IncidentKind, OrderRecord, Profile
from deliverius.datagen.world import generate_customers
from deliverius.orders import OrderStatus, can_transition
from deliverius.privacy.documents import is_valid_cpf

STATUSES_WITH_NO_DELIVERY = {OrderStatus.CANCELLED, OrderStatus.DELIVERY_FAILED}


def test_every_order_builds_the_domain_models_without_an_invariant_error(
    small_world: World,
) -> None:
    for record in small_world.order_records():
        order = OrderRecord.model_validate(record.model_dump()).to_domain()

        assert order.subtotal + order.delivery_fee - order.discount == order.total
        assert order.items
        assert order.total.cents > 0


def test_every_timeline_follows_the_allowed_transitions(small_world: World) -> None:
    for sim in small_world.orders:
        order = sim.order
        statuses = [event.status for event in order.events]

        assert statuses[0] is OrderStatus.PLACED
        assert order.events[0].occurred_at == order.placed_at
        assert statuses[-1] is order.status
        assert all(can_transition(a, b) for a, b in pairwise(statuses))
        times = [event.occurred_at for event in order.events]
        assert times == sorted(times)
        assert len(set(times)) == len(times)


def test_only_delivered_orders_have_a_delivery_time_and_it_is_the_last_event(
    small_world: World,
) -> None:
    for sim in small_world.orders:
        order = sim.order
        if order.status is OrderStatus.DELIVERED:
            assert order.delivered_at == order.events[-1].occurred_at
        else:
            assert order.delivered_at is None
            assert order.status in STATUSES_WITH_NO_DELIVERY


def test_the_world_has_the_configured_sizes(small_world: World) -> None:
    assert len(small_world.customers) == SMALL_CONFIG.customer_count
    assert len(small_world.restaurants) == SMALL_CONFIG.restaurant_count
    assert len(small_world.orders) == SMALL_CONFIG.order_count
    assert len(small_world.customer_records()) == len(small_world.customers)
    assert len(small_world.restaurant_records()) == len(small_world.restaurants)


def test_customers_have_fake_pt_br_data_and_a_cpf_with_valid_check_digits(
    small_world: World,
) -> None:
    for sim in small_world.customers:
        customer = sim.customer
        digits = "".join(character for character in customer.cpf if character.isdigit())

        assert is_valid_cpf(digits)
        assert len(customer.cpf) == 14
        assert "@" in customer.email
        assert sum(character.isdigit() for character in customer.phone) >= 10
        assert "\n" not in customer.address
        assert customer.name


def test_personal_data_stays_out_of_the_customer_repr(small_world: World) -> None:
    customer = small_world.customers[0].customer

    assert customer.cpf not in repr(customer)
    assert customer.name not in repr(customer)


def test_profiles_are_within_two_points_of_the_configured_proportions() -> None:
    config = DatagenConfig()

    counts = Counter(sim.profile for sim in generate_customers(config))

    shares = {profile: counts[profile] / config.customer_count for profile in Profile}
    assert shares[Profile.HONEST] == pytest.approx(0.90, abs=0.02)
    assert shares[Profile.OPPORTUNIST] == pytest.approx(0.07, abs=0.02)
    assert shares[Profile.FRAUDSTER] == pytest.approx(0.03, abs=0.02)


def test_profile_counts_are_exact_in_a_small_world(small_world: World) -> None:
    counts = Counter(sim.profile for sim in small_world.customers)

    assert counts == {Profile.HONEST: 44, Profile.OPPORTUNIST: 4, Profile.FRAUDSTER: 2}


def test_only_honest_customers_can_be_unlucky_and_their_share_is_exact() -> None:
    customers = generate_customers(DatagenConfig(customer_count=400, order_count=10))
    honest = [sim for sim in customers if sim.profile is Profile.HONEST]

    unlucky = [sim for sim in customers if sim.unlucky]

    assert all(sim.profile is Profile.HONEST for sim in unlucky)
    assert len(unlucky) == round(0.05 * len(honest))


def test_ids_are_uuid_v7_and_follow_time(small_world: World) -> None:
    order_ids = [sim.order.id for sim in small_world.orders]
    customer_ids = [sim.customer.id for sim in small_world.customers]

    assert order_ids == sorted(order_ids)
    assert customer_ids == sorted(customer_ids)
    assert {identifier.version for identifier in order_ids + customer_ids} == {7}
    assert len(set(order_ids)) == len(order_ids)


def test_orders_fall_in_the_period_and_after_the_customer_joined(small_world: World) -> None:
    config = SMALL_CONFIG
    created = {sim.customer.id: sim.customer.created_at for sim in small_world.customers}

    for sim in small_world.orders:
        order = sim.order
        assert config.period_start <= order.placed_at <= config.orders_end
        assert order.placed_at > created[order.customer_id]
        assert order.estimated_delivery_at > order.placed_at
        assert order.events[-1].occurred_at < config.reference_instant


def test_restaurants_have_a_priced_menu_and_orders_use_it(small_world: World) -> None:
    menus = {
        sim.restaurant.id: {item.name: item for item in sim.menu} for sim in small_world.restaurants
    }

    for sim in small_world.restaurants:
        assert len(sim.menu) >= 6
        assert all(item.unit_price.cents > 0 for item in sim.menu)
    for sim in small_world.orders:
        for item in sim.order.items:
            assert item.unit_price == menus[sim.order.restaurant_id][item.name].unit_price
            assert item.allergens == menus[sim.order.restaurant_id][item.name].allergens


def test_incidents_agree_with_the_order(small_world: World) -> None:
    for sim in small_world.orders:
        order = sim.order
        kinds = [incident.kind for incident in sim.incidents]
        delay = (
            order.delivered_at - order.estimated_delivery_at
            if order.delivered_at is not None
            else None
        )

        assert (IncidentKind.CUSTOMER_CANCELLATION in kinds) == (
            order.status is OrderStatus.CANCELLED
        )
        assert (IncidentKind.DELIVERY_FAILED in kinds) == (
            order.status is OrderStatus.DELIVERY_FAILED
        )
        assert (IncidentKind.MAJOR_DELAY in kinds) == (
            delay is not None and delay > timedelta(minutes=60)
        )
        assert (IncidentKind.MINOR_DELAY in kinds) == (
            delay is not None and timedelta(minutes=30) <= delay <= timedelta(minutes=60)
        )
        ordered = {item.id: item.quantity for item in order.items}
        for incident in sim.incidents:
            if incident.item_id is not None:
                assert 1 <= incident.quantity <= ordered[incident.item_id]


def test_incidents_follow_the_configured_rates() -> None:
    config = DatagenConfig(
        customer_count=600,
        order_count=8_000,
        claim_rates=ClaimRates(unlucky_honest_share=0.0),
    )
    world = generate_world(config)
    rates = IncidentRates()
    counts = Counter(incident.kind for sim in world.orders for incident in sim.incidents)
    total = config.order_count
    delivered = total * (1 - rates.customer_cancellation - rates.delivery_failed)
    expected = {
        IncidentKind.CUSTOMER_CANCELLATION: total * rates.customer_cancellation,
        IncidentKind.DELIVERY_FAILED: total * rates.delivery_failed,
        IncidentKind.DUPLICATE_CHARGE: total * rates.duplicate_charge,
        IncidentKind.MISSING_ITEM: delivered * rates.missing_item,
        IncidentKind.WRONG_ITEM: delivered * rates.wrong_item,
        IncidentKind.QUALITY_ISSUE: delivered * rates.quality_issue,
        IncidentKind.MINOR_DELAY: delivered * rates.minor_delay,
        IncidentKind.MAJOR_DELAY: delivered * rates.major_delay,
        IncidentKind.MARKED_DELIVERED_NOT_RECEIVED: delivered * rates.marked_delivered_not_received,
    }
    for kind, mean in expected.items():
        assert abs(counts[kind] - mean) <= 4 * math.sqrt(mean) + 1, kind


def test_unlucky_customers_have_more_incidents_per_order() -> None:
    config = DatagenConfig(
        customer_count=200,
        order_count=6_000,
        claim_rates=ClaimRates(unlucky_honest_share=0.5),
    )
    world = generate_world(config)
    unlucky = {sim.customer.id for sim in world.customers if sim.unlucky}
    per_group: dict[bool, list[int]] = {True: [], False: []}
    for sim in world.orders:
        per_group[sim.order.customer_id in unlucky].append(len(sim.incidents))

    unlucky_rate = sum(per_group[True]) / len(per_group[True])
    lucky_rate = sum(per_group[False]) / len(per_group[False])

    assert unlucky_rate > 2.5 * lucky_rate


def test_the_world_is_the_same_twice_and_different_with_another_seed() -> None:
    config = DatagenConfig(seed=5, customer_count=20, restaurant_count=4, order_count=60)

    first, second = generate_world(config), generate_world(config)
    other = generate_world(config.model_copy(update={"seed": 6}))

    assert first.order_records() == second.order_records()
    assert first.customer_records() == second.customer_records()
    assert first.order_records() != other.order_records()
    assert first.customer_records() != other.customer_records()


def test_customer_records_carry_no_profile() -> None:
    assert "profile" not in CustomerRecord.model_fields
    assert "unlucky" not in CustomerRecord.model_fields

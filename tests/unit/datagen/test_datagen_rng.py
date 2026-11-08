import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deliverius.datagen.rng import (
    IdFactory,
    Layer,
    count_for_rate,
    derive_seed,
    keyed_random,
    keyed_seed,
    layer_faker,
    layer_random,
)
from deliverius.shared import InvariantError

NOON = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def draw(seed: int, layer: Layer) -> list[int]:
    rng = layer_random(seed, layer)
    return [rng.randrange(10**9) for _ in range(20)]


def test_a_layer_seed_is_the_sha256_of_the_seed_and_the_layer_name() -> None:
    expected = int.from_bytes(hashlib.sha256(b"42:orders").digest(), "big")

    assert derive_seed(42, Layer.ORDERS) == expected


def test_the_same_seed_and_layer_give_the_same_sequence() -> None:
    assert draw(42, Layer.ORDERS) == draw(42, Layer.ORDERS)


def test_different_layers_diverge() -> None:
    assert draw(42, Layer.ORDERS) != draw(42, Layer.CLAIMS)


def test_different_seeds_diverge() -> None:
    assert draw(42, Layer.ORDERS) != draw(43, Layer.ORDERS)


def test_faker_follows_the_seed_and_the_layer() -> None:
    def names(seed: int, layer: Layer) -> list[str]:
        faker = layer_faker(seed, layer)
        return [faker.name() for _ in range(5)]

    assert names(42, Layer.CUSTOMERS) == names(42, Layer.CUSTOMERS)
    assert names(42, Layer.CUSTOMERS) != names(42, Layer.RESTAURANTS)
    assert names(42, Layer.CUSTOMERS) != names(43, Layer.CUSTOMERS)


def test_an_id_is_a_uuid_version_7_with_the_instant_in_its_first_48_bits() -> None:
    identifier = IdFactory(layer_random(1, Layer.ORDERS)).next_id(NOON)

    assert identifier.version == 7
    assert identifier.variant == "specified in RFC 4122"
    assert identifier.int >> 80 == int(NOON.timestamp() * 1000)


def test_ids_are_reproducible() -> None:
    first = [IdFactory(layer_random(1, Layer.ORDERS)).next_id(NOON) for _ in range(3)]
    second = [IdFactory(layer_random(1, Layer.ORDERS)).next_id(NOON) for _ in range(3)]

    assert first == second


def test_ids_from_the_same_stream_do_not_repeat_within_a_millisecond() -> None:
    ids = IdFactory(layer_random(1, Layer.ORDERS))

    assert len({ids.next_id(NOON) for _ in range(1_000)}) == 1_000


@given(
    st.integers(min_value=0, max_value=10**9),
    st.integers(min_value=1, max_value=10**6),
)
def test_ids_are_ordered_by_the_instant_they_were_made_for(start: int, gap: int) -> None:
    ids = IdFactory(layer_random(3, Layer.ORDERS))
    earlier = NOON + timedelta(milliseconds=start)
    later = earlier + timedelta(milliseconds=gap)

    assert ids.next_id(earlier) < ids.next_id(later)


def test_an_instant_before_1970_is_refused() -> None:
    ids = IdFactory(layer_random(1, Layer.ORDERS))

    with pytest.raises(InvariantError, match=r"datagen\.instant_out_of_range"):
        ids.next_id(datetime(1969, 12, 31, tzinfo=UTC))


@pytest.mark.parametrize(
    ("total", "rate", "expected"),
    [(2_000, 0.07, 140), (50, 0.07, 4), (50, 0.03, 2), (100, 0.025, 3), (0, 0.5, 0)],
)
def test_a_rate_becomes_an_exact_count_rounded_half_up(
    total: int, rate: float, expected: int
) -> None:
    assert count_for_rate(total, rate) == expected


def test_a_keyed_seed_is_the_sha256_of_the_seed_the_layer_and_the_key() -> None:
    expected = int.from_bytes(hashlib.sha256(b"42:placeholders:abc:3").digest(), "big")

    assert keyed_seed(42, Layer.PLACEHOLDERS, "abc:3") == expected


def test_a_keyed_generator_depends_on_its_key_and_not_on_what_was_drawn_before() -> None:
    first = keyed_random(42, Layer.PLACEHOLDERS, "a")
    keyed_random(42, Layer.PLACEHOLDERS, "b").random()

    assert first.random() == keyed_random(42, Layer.PLACEHOLDERS, "a").random()
    assert (
        keyed_random(42, Layer.PLACEHOLDERS, "a").random()
        != keyed_random(42, Layer.PLACEHOLDERS, "b").random()
    )
    assert keyed_seed(42, Layer.PLACEHOLDERS, "a") != keyed_seed(42, Layer.SPLITS, "a")

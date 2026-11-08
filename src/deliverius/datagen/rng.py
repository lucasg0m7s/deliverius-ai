"""Randomness from the seed only: one generator per layer, so a change moves only its layer.

Nothing here (or anywhere in `datagen`) touches the global `random` module, the real clock or
`uuid_utils.uuid7()`: `tests/unit/datagen` scans the sources for them.
"""

import hashlib
import math
import random
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from faker import Faker

from deliverius.shared import InvariantError

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND: Final = timedelta(milliseconds=1)
_TIMESTAMP_BITS: Final = 48
_RANDOM_BITS: Final = 74
_RAND_B_BITS: Final = 62
_VERSION_7: Final = 0x7
_VARIANT_RFC_4122: Final = 0b10


class Layer(StrEnum):
    SANDBOX = "sandbox"
    CUSTOMERS = "customers"
    RESTAURANTS = "restaurants"
    ORDERS = "orders"
    CLAIMS = "claims"
    LABEL_NOISE = "label_noise"
    RESOLUTION = "resolution"
    SCENARIOS = "scenarios"
    HOLDOUT_SCENARIOS = "holdout_scenarios"
    MESSAGES = "messages"
    HOLDOUT_MESSAGES = "holdout_messages"
    PLACEHOLDERS = "placeholders"
    SPLITS = "splits"
    RAG_PAIRS = "rag_pairs"
    E2E = "e2e"


def derive_seed(seed: int, layer: Layer) -> int:
    digest = hashlib.sha256(f"{seed}:{layer.value}".encode()).digest()
    return int.from_bytes(digest, "big")


def keyed_seed(seed: int, layer: Layer, key: str) -> int:
    """A seed for one item of a layer, independent of the order the items are handled in."""
    digest = hashlib.sha256(f"{seed}:{layer.value}:{key}".encode()).digest()
    return int.from_bytes(digest, "big")


def layer_random(seed: int, layer: Layer) -> random.Random:
    # A reproducible simulation, not a secret: the seeded generator is the point.
    return random.Random(derive_seed(seed, layer))  # noqa: S311


def keyed_random(seed: int, layer: Layer, key: str) -> random.Random:
    return random.Random(keyed_seed(seed, layer, key))  # noqa: S311


def layer_faker(seed: int, layer: Layer) -> Faker:
    faker = Faker("pt_BR")
    faker.seed_instance(derive_seed(seed, layer))
    return faker


def count_for_rate(total: int, rate: float) -> int:
    """How many of `total` a rate stands for, rounded half up: exact counts, not a draw."""
    return math.floor(total * rate + 0.5)


class IdFactory:
    """UUID v7 from a simulated instant plus bits of the layer generator.

    Ordered by time like the application's IDs, and reproducible, which `uuid7()` is not.
    """

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def next_id(self, at: datetime) -> UUID:
        milliseconds = (at - _EPOCH) // _MILLISECOND
        if not 0 <= milliseconds < 1 << _TIMESTAMP_BITS:
            raise InvariantError("datagen.instant_out_of_range")
        random_bits = self._rng.getrandbits(_RANDOM_BITS)
        rand_a = random_bits >> _RAND_B_BITS
        rand_b = random_bits & ((1 << _RAND_B_BITS) - 1)
        value = (
            (milliseconds << 80)
            | (_VERSION_7 << 76)
            | (rand_a << 64)
            | (_VARIANT_RFC_4122 << 62)
            | rand_b
        )
        return UUID(int=value)

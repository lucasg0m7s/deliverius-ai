"""One generated world per session: generating is the slow part, asserting on it is not."""

import pytest
from support.datagen_configs import MIXED_CONFIG, SMALL_CONFIG

from deliverius.datagen import ClaimsDataset, World, generate_claims, generate_world


@pytest.fixture(scope="session")
def mixed_world() -> World:
    return generate_world(MIXED_CONFIG)


@pytest.fixture(scope="session")
def mixed_claims(mixed_world: World) -> ClaimsDataset:
    return generate_claims(mixed_world, MIXED_CONFIG)


@pytest.fixture(scope="session")
def small_world() -> World:
    return generate_world(SMALL_CONFIG)

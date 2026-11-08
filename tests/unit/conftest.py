import logging
from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """`configure_logging` (also run by the app lifespan) changes global state; undo it per test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level
    structlog.reset_defaults()

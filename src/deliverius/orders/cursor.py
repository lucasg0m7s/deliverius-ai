"""Keyset pagination over `(placed_at, id)`, newest first.

The cursor is opaque but not signed: it only says "continue after this order". The access scope
is still applied in the query, so a tampered cursor cannot reveal another owner's data.
"""

import base64
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict

from deliverius.orders.models import OrderSummary
from deliverius.shared.errors import InvalidInputError

MAX_PAGE_SIZE = 50
# Rejected before decoding, so a huge input costs nothing.
MAX_CURSOR_LENGTH = 256

logger = structlog.get_logger(__name__)


class OrderCursor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    placed_at: AwareDatetime
    id: UUID


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int = MAX_PAGE_SIZE
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise InvalidInputError("page_limit_below_one")
        # Above the maximum is clamped rather than refused: asking for more is not an error.
        object.__setattr__(self, "limit", min(self.limit, MAX_PAGE_SIZE))


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None


def encode_cursor(placed_at: datetime, order_id: UUID) -> str:
    payload = OrderCursor(placed_at=placed_at, id=order_id).model_dump_json().encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> OrderCursor:
    # binascii.Error and pydantic's ValidationError are both ValueError subclasses.
    if len(cursor) <= MAX_CURSOR_LENGTH:
        with suppress(ValueError):
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = base64.b64decode(padded, altchars=b"-_", validate=True)
            return OrderCursor.model_validate_json(payload)
    logger.info("orders.invalid_cursor", cursor_length=len(cursor))
    raise InvalidInputError("invalid_cursor")


def page_from_rows(rows: Sequence[OrderSummary], limit: int) -> Page[OrderSummary]:
    """Build a page from up to `limit + 1` rows; the extra row only signals a next page."""
    items = tuple(rows[:limit])
    if len(rows) <= limit:
        return Page(items=items, next_cursor=None)
    last = items[-1]
    return Page(items=items, next_cursor=encode_cursor(last.placed_at, last.id))

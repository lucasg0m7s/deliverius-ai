"""Opaque keyset cursors for the two queues: "continue after this row".

Like the order cursor, it is not signed. The scope stays in the query, so a tampered cursor cannot
reveal another sandbox's rows.
"""

import base64
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime
from typing import Final
from uuid import UUID

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict

from deliverius.orders import Page
from deliverius.shared.errors import InvalidInputError

# Rejected before decoding, so a huge input costs nothing.
MAX_CURSOR_LENGTH: Final = 256

logger = structlog.get_logger(__name__)


class ApprovalCursor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    created_at: AwareDatetime
    id: UUID


class HandoffCursor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    priority_rank: int
    created_at: AwareDatetime
    id: UUID


def encode_cursor(cursor: ApprovalCursor | HandoffCursor) -> str:
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")


def approval_cursor(created_at: datetime, approval_id: UUID) -> str:
    return encode_cursor(ApprovalCursor(created_at=created_at, id=approval_id))


def handoff_cursor(priority_rank: int, created_at: datetime, handoff_id: UUID) -> str:
    return encode_cursor(
        HandoffCursor(priority_rank=priority_rank, created_at=created_at, id=handoff_id)
    )


def decode_approval_cursor(cursor: str) -> ApprovalCursor:
    return _decode(cursor, ApprovalCursor)


def decode_handoff_cursor(cursor: str) -> HandoffCursor:
    return _decode(cursor, HandoffCursor)


def _decode[M: BaseModel](cursor: str, model: type[M]) -> M:
    # binascii.Error and pydantic's ValidationError are both ValueError subclasses.
    if len(cursor) <= MAX_CURSOR_LENGTH:
        with suppress(ValueError):
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = base64.b64decode(padded, altchars=b"-_", validate=True)
            return model.model_validate_json(payload)
    logger.info("approvals.invalid_cursor", cursor_length=len(cursor))
    raise InvalidInputError("invalid_cursor")


def page_of[T](rows: Sequence[T], limit: int, cursor_of: Callable[[T], str]) -> Page[T]:
    """Build a page from up to `limit + 1` rows; the extra row only signals a next page."""
    items = tuple(rows[:limit])
    if len(rows) <= limit:
        return Page(items=items, next_cursor=None)
    return Page(items=items, next_cursor=cursor_of(items[-1]))

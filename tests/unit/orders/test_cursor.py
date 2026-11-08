import base64
import json
from datetime import UTC, datetime

import pytest
from structlog.testing import capture_logs

from deliverius.orders.cursor import (
    MAX_CURSOR_LENGTH,
    MAX_PAGE_SIZE,
    PageRequest,
    decode_cursor,
    encode_cursor,
)
from deliverius.shared.errors import InvalidInputError
from deliverius.shared.ids import new_id

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_cursor_round_trip() -> None:
    order_id = new_id()

    decoded = decode_cursor(encode_cursor(NOW, order_id))

    assert decoded.placed_at == NOW
    assert decoded.id == order_id


def test_cursor_is_url_safe_without_padding() -> None:
    cursor = encode_cursor(NOW, new_id())

    assert "=" not in cursor
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 at all!",
        b64(b"not json"),
        b64(json.dumps({"placed_at": NOW.isoformat()}).encode()),
        b64(json.dumps({"placed_at": "2026-11-01T12:00:00", "id": str(new_id())}).encode()),
        b64(json.dumps({"placed_at": NOW.isoformat(), "id": "nope"}).encode()),
        b64(json.dumps({"placed_at": NOW.isoformat(), "id": str(new_id()), "x": 1}).encode()),
        b64(json.dumps({"placed_at": 12345, "id": str(new_id())}).encode()),
        "",
    ],
    ids=["garbage", "not-json", "missing-id", "naive", "bad-uuid", "extra-key", "number", "empty"],
)
def test_malformed_or_tampered_cursor_is_invalid_input(cursor: str) -> None:
    with capture_logs() as logs, pytest.raises(InvalidInputError):
        decode_cursor(cursor)

    assert logs[0]["event"] == "orders.invalid_cursor"


def test_oversized_cursor_is_rejected_before_decoding() -> None:
    with pytest.raises(InvalidInputError):
        decode_cursor("A" * (MAX_CURSOR_LENGTH + 1))


def test_page_size_above_maximum_is_clamped() -> None:
    assert PageRequest(limit=500).limit == MAX_PAGE_SIZE


@pytest.mark.parametrize("limit", [0, -5])
def test_page_size_below_one_is_invalid(limit: int) -> None:
    with pytest.raises(InvalidInputError):
        PageRequest(limit=limit)

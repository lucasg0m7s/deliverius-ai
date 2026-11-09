"""Rows of conversations, approval requests, handoffs and actions for the table-level tests."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession
from support.order_builders import BASE_TIME, build_order
from support.order_rows import insert_orders

from deliverius.db.tables import (
    ActionRow,
    ApprovalRequestRow,
    ConversationRow,
    HandoffRow,
)
from deliverius.orders.models import Order
from deliverius.shared.ids import CustomerId, SandboxId, new_id

CLAIMED_AT = BASE_TIME + timedelta(hours=1)


def new_order() -> Order:
    return build_order(sandbox_id=SandboxId(new_id()), customer_id=CustomerId(new_id()))


def conversation_values(order: Order, **overrides: Any) -> dict[str, Any]:
    """A conversation of the order's customer; the order and customer rows must already exist."""
    values: dict[str, Any] = {
        "id": new_id(),
        "sandbox_id": order.sandbox_id,
        "customer_id": order.customer_id,
        "order_id": order.id,
        "status": "open",
    }
    return values | overrides


async def insert_conversation(session: AsyncSession, order: Order, **overrides: Any) -> UUID:
    values = conversation_values(order, **overrides)
    await session.execute(insert(ConversationRow).values(**values))
    conversation_id: UUID = values["id"]
    return conversation_id


async def insert_order_and_conversation(session: AsyncSession) -> tuple[Order, UUID]:
    order = new_order()
    await insert_orders(session, [order])
    return order, await insert_conversation(session, order)


def approval_values(order: Order, conversation_id: UUID, **overrides: Any) -> dict[str, Any]:
    """A pending request; add `decision_values(...)` for a decided one."""
    values: dict[str, Any] = {
        "id": new_id(),
        "sandbox_id": order.sandbox_id,
        "customer_id": order.customer_id,
        "conversation_id": conversation_id,
        "order_id": order.id,
        "proposal_key": new_id().hex,
        "proposal": {"kind": "refund", "reason": "item_missing", "amount_cents": 800},
        "claimed_at": CLAIMED_AT,
        "policy_reasons": ["exceeds_action_limit"],
        "policy_ceiling_cents": 18_600,
        "policy_document": "POL-11",
        "status": "pending",
    }
    return values | overrides


def decision_values(status: str = "approved", reason: str = "Looks right") -> dict[str, Any]:
    return {
        "status": status,
        "version": 2,
        "decided_by": new_id(),
        "decided_at": CLAIMED_AT + timedelta(hours=2),
        "decision_reason": reason,
    }


async def insert_approval(
    session: AsyncSession, order: Order, conversation_id: UUID, **overrides: Any
) -> dict[str, Any]:
    values = approval_values(order, conversation_id, **overrides)
    await session.execute(insert(ApprovalRequestRow).values(**values))
    return values


def action_values(order: Order, conversation_id: UUID, **overrides: Any) -> dict[str, Any]:
    """An automatic refund of the order's customer; override the columns a test wants to break."""
    values: dict[str, Any] = {
        "id": new_id(),
        "sandbox_id": order.sandbox_id,
        "customer_id": order.customer_id,
        "order_id": order.id,
        "conversation_id": conversation_id,
        "kind": "refund",
        "reason": "item_missing",
        "amount_cents": 800,
        "items": [{"order_item_id": str(order.items[0].id), "quantity": 1}],
        "idempotency_key": new_id().hex,
        "approved_by": "automatic",
        "approval_request_id": None,
        "approval_status": None,
        "claimed_at": CLAIMED_AT,
        "executed_at": CLAIMED_AT + timedelta(minutes=1),
    }
    return values | overrides


async def insert_action(
    session: AsyncSession, order: Order, conversation_id: UUID, **overrides: Any
) -> dict[str, Any]:
    values = action_values(order, conversation_id, **overrides)
    await session.execute(insert(ActionRow).values(**values))
    return values


def handoff_values(order: Order, conversation_id: UUID, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": new_id(),
        "sandbox_id": order.sandbox_id,
        "customer_id": order.customer_id,
        "conversation_id": conversation_id,
        "priority": "normal",
        "summary": {"intent": "allergy", "facts": [], "actions_taken": []},
        "status": "open",
    }
    return values | overrides


async def insert_handoff(
    session: AsyncSession, order: Order, conversation_id: UUID, **overrides: Any
) -> dict[str, Any]:
    values = handoff_values(order, conversation_id, **overrides)
    await session.execute(insert(HandoffRow).values(**values))
    return values

"""ORM tables: the source of truth for Alembic. Only the repositories see these classes.

Relationships use `lazy="raise"`: async SQLAlchemy cannot lazy-load, and an implicit load would
hide an N+1.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from deliverius.shared.audit import AuditActorType

# Mirrored by the StrEnums in `orders.models` (db cannot import orders); a test keeps them equal.
ORDER_STATUSES = (
    "placed",
    "accepted",
    "preparing",
    "ready_for_pickup",
    "out_for_delivery",
    "delivered",
    "cancelled",
    "delivery_failed",
)
PAYMENT_METHODS = ("pix", "credit_card", "debit_card", "meal_voucher")
EVENT_ACTORS = ("customer", "restaurant", "courier", "system")
AUDIT_ACTOR_TYPES = tuple(actor.value for actor in AuditActorType)
AUDIT_ACTION_REGEX = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
LLM_CALL_STATUSES = ("ok", "error")
# Mirrored by the StrEnums in `policy.models` and `approvals.models`; a test keeps them equal.
ACTION_KINDS = ("refund", "coupon", "cancellation")
CLAIM_REASONS = (
    "item_missing",
    "wrong_item",
    "quality_issue",
    "late_delivery",
    "not_delivered",
    "duplicate_charge",
    "customer_cancellation",
)
APPROVED_BY = ("automatic", "reviewer")
CONVERSATION_STATUSES = ("open", "awaiting_approval", "transferred", "closed")
APPROVAL_STATUSES = ("pending", "approved", "rejected")
HANDOFF_STATUSES = ("open", "assigned")
HANDOFF_PRIORITIES = ("normal", "high")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def one_of(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012 - SQLAlchemy reads it as-is


class CustomerRow(Base):
    __tablename__ = "customers"
    __table_args__ = (
        # Target of the composite FK from orders: no order points at another sandbox's customer.
        UniqueConstraint("id", "sandbox_id"),
        UniqueConstraint("sandbox_id", "cpf"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(Text)
    cpf: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RestaurantRow(Base):
    """Shared catalog across sandboxes: no personal data, nothing a visitor can create."""

    __tablename__ = "restaurants"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_orders_customer_same_sandbox",
        ),
        CheckConstraint(one_of("status", ORDER_STATUSES), name="status_known"),
        CheckConstraint(one_of("payment_method", PAYMENT_METHODS), name="payment_method_known"),
        CheckConstraint(
            "subtotal_cents >= 0 AND delivery_fee_cents >= 0 AND discount_cents >= 0 "
            "AND total_cents >= 0",
            name="amounts_not_negative",
        ),
        CheckConstraint(
            "discount_cents <= subtotal_cents + delivery_fee_cents", name="discount_within_amount"
        ),
        CheckConstraint(
            "total_cents = subtotal_cents + delivery_fee_cents - discount_cents",
            name="total_consistent",
        ),
        Index(
            "ix_orders_customer_listing",
            "sandbox_id",
            "customer_id",
            text("placed_at DESC"),
            text("id DESC"),
        ),
        Index("ix_orders_sandbox_listing", "sandbox_id", text("placed_at DESC"), text("id DESC")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    restaurant_id: Mapped[UUID] = mapped_column(ForeignKey("restaurants.id"), index=True)
    status: Mapped[str] = mapped_column(Text)
    payment_method: Mapped[str] = mapped_column(Text)
    placed_at: Mapped[datetime]
    estimated_delivery_at: Mapped[datetime]
    delivered_at: Mapped[datetime | None]
    subtotal_cents: Mapped[int] = mapped_column(Integer)
    delivery_fee_cents: Mapped[int] = mapped_column(Integer)
    discount_cents: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)
    # Free text from outside: stored as received, never interpreted; delimited in any prompt.
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["OrderItemRow"]] = relationship(
        lazy="raise", order_by=lambda: OrderItemRow.id, passive_deletes=True
    )
    events: Mapped[list["DeliveryEventRow"]] = relationship(
        lazy="raise",
        order_by=lambda: [DeliveryEventRow.occurred_at, DeliveryEventRow.id],
        passive_deletes=True,
    )


class OrderItemRow(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("unit_price_cents > 0", name="unit_price_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    allergens: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")


class DeliveryEventRow(Base):
    __tablename__ = "delivery_events"
    __table_args__ = (
        CheckConstraint(one_of("status", ORDER_STATUSES), name="status_known"),
        CheckConstraint(one_of("actor", EVENT_ACTORS), name="actor_known"),
        Index("ix_delivery_events_order_id_occurred_at", "order_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime]


class AuditLogRow(Base):
    """Insert-only: triggers in migration 0002 reject UPDATE, DELETE and TRUNCATE."""

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(one_of("actor_type", AUDIT_ACTOR_TYPES), name="actor_type_known"),
        CheckConstraint(f"action ~ '{AUDIT_ACTION_REGEX}'", name="action_format"),
        Index("ix_audit_log_sandbox_id_occurred_at", "sandbox_id", "occurred_at"),
        Index("ix_audit_log_target_type_target_id", "target_type", "target_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_id: Mapped[UUID]
    occurred_at: Mapped[datetime]
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[UUID]
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    # Filled from change 14 on; nullable from the start so adding it needs no locking migration.
    trace_id: Mapped[str | None] = mapped_column(Text)


class LlmCallRow(Base):
    """One row per real attempt against a provider. Never holds message text or provider error text.

    `NULL` is not zero: a missing token count or an unknown price stays empty, so sums do not lie.
    Costs are integer micro-dollars: a call costs a fraction of a cent, which `Money` cannot hold.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(one_of("status", LLM_CALL_STATUSES), name="status_known"),
        CheckConstraint("latency_ms >= 0", name="latency_not_negative"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND reasoning_tokens >= 0",
            name="tokens_not_negative",
        ),
        CheckConstraint(
            "real_cost_usd_micros >= 0 AND equivalent_cost_usd_micros >= 0",
            name="costs_not_negative",
        ),
        Index("ix_llm_calls_created_at", "created_at"),
        Index(
            "ix_llm_calls_conversation_id_created_at",
            "conversation_id",
            "created_at",
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # No foreign key yet: the conversations table arrives later and adds it as NOT VALID.
    conversation_id: Mapped[UUID | None]
    sandbox_id: Mapped[UUID | None]
    role: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_name: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    real_cost_usd_micros: Mapped[int | None] = mapped_column(BigInteger)
    equivalent_cost_usd_micros: Mapped[int | None] = mapped_column(BigInteger)
    trace_id: Mapped[str | None] = mapped_column(Text)


class LlmBudgetUsageRow(Base):
    """Requests and tokens spent per provider per UTC day, updated by one atomic statement."""

    __tablename__ = "llm_budget_usage"
    __table_args__ = (
        PrimaryKeyConstraint("provider", "day"),
        CheckConstraint("requests_used >= 0 AND tokens_used >= 0", name="counters_not_negative"),
    )

    provider: Mapped[str] = mapped_column(Text)
    day: Mapped[date] = mapped_column(Date)
    requests_used: Mapped[int] = mapped_column(Integer)
    tokens_used: Mapped[int] = mapped_column(BigInteger)


class PolicyChunkRow(Base):
    """One section of a policy document, with its embedding. About fifty rows, public to everyone.

    `tsv` is generated from `content`; there is no vector index on purpose (an exact scan of fifty
    rows is instant and never misses). A HNSW index is worth it once this grows to thousands.
    """

    __tablename__ = "policy_chunks"
    __table_args__ = (
        Index("ix_policy_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_policy_chunks_document_id", "document_id"),
    )

    chunk_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(SmallInteger)
    content: Mapped[str] = mapped_column(Text)
    document_hash: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(384))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('pt_unaccent', content)", persisted=True)
    )
    indexed_at: Mapped[datetime]


class ConversationRow(Base):
    """The minimum other tables point at: the conversation module adds the rest later."""

    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_conversations_customer_same_sandbox",
        ),
        ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_conversations_order_id_orders"),
        # Target of the composite FKs: an action or approval only points at a conversation of the
        # same customer and the same order.
        UniqueConstraint("id", "customer_id", "order_id"),
        # Target of the composite FK from handoffs, which have no order.
        UniqueConstraint("id", "customer_id"),
        CheckConstraint(one_of("status", CONVERSATION_STATUSES), name="status_known"),
        Index(
            "ix_conversations_sandbox_id_customer_id_created_at",
            "sandbox_id",
            "customer_id",
            "created_at",
        ),
        Index("ix_conversations_order_id", "order_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    order_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ApprovalRequestRow(Base):
    """A proposal waiting for a person. A trigger in migration 0005 freezes it once decided."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_approval_requests_customer_same_sandbox",
        ),
        ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_approval_requests_order_id_orders"
        ),
        ForeignKeyConstraint(
            ["conversation_id", "customer_id", "order_id"],
            ["conversations.id", "conversations.customer_id", "conversations.order_id"],
            name="fk_approval_requests_conversation_matches_customer_order",
        ),
        UniqueConstraint("conversation_id", "proposal_key"),
        # Target of the composite FK from actions: only an approved request of the same
        # conversation and proposal can authorize an action.
        UniqueConstraint("id", "conversation_id", "proposal_key", "status"),
        CheckConstraint(one_of("status", APPROVAL_STATUSES), name="status_known"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("policy_ceiling_cents >= 0", name="ceiling_not_negative"),
        CheckConstraint("fraud_score BETWEEN 0 AND 1", name="fraud_score_in_range"),
        CheckConstraint(
            "char_length(decision_reason) BETWEEN 1 AND 500", name="decision_reason_length"
        ),
        CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL "
            "AND decision_reason IS NULL) "
            "OR (status <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND decision_reason IS NOT NULL)",
            name="decision_complete",
        ),
        Index(
            "uq_approval_requests_pending_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_approval_requests_pending_queue",
            "sandbox_id",
            "created_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        # Covers the foreign key to the customer; the fraud reader uses the next one.
        Index("ix_approval_requests_customer_id", "customer_id"),
        Index(
            "ix_approval_requests_sandbox_id_customer_id_claimed_at",
            "sandbox_id",
            "customer_id",
            "claimed_at",
        ),
        Index("ix_approval_requests_order_id", "order_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    order_id: Mapped[UUID]
    proposal_key: Mapped[str] = mapped_column(Text)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    claimed_at: Mapped[datetime]
    policy_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text))
    policy_ceiling_cents: Mapped[int] = mapped_column(Integer)
    policy_document: Mapped[str] = mapped_column(Text)
    fraud_score: Mapped[float | None] = mapped_column(Float)
    fraud_model_version: Mapped[str | None] = mapped_column(Text)
    fraud_factors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    decided_by: Mapped[UUID | None]
    decided_at: Mapped[datetime | None]
    # Free text from the reviewer: stored as received, never in a log or in the audit data.
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HandoffRow(Base):
    __tablename__ = "handoffs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_handoffs_customer_same_sandbox",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "customer_id"],
            ["conversations.id", "conversations.customer_id"],
            name="fk_handoffs_conversation_matches_customer",
        ),
        CheckConstraint(one_of("priority", HANDOFF_PRIORITIES), name="priority_known"),
        CheckConstraint(one_of("status", HANDOFF_STATUSES), name="status_known"),
        CheckConstraint(
            "(status = 'open' AND assigned_to IS NULL AND assigned_at IS NULL) "
            "OR (status = 'assigned' AND assigned_to IS NOT NULL AND assigned_at IS NOT NULL)",
            name="assignment_complete",
        ),
        Index(
            "uq_handoffs_open_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            "ix_handoffs_open_queue",
            "sandbox_id",
            text("priority_rank DESC"),
            "created_at",
            "id",
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_handoffs_customer_id", "customer_id"),
        Index("ix_handoffs_conversation_id", "conversation_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    priority: Mapped[str] = mapped_column(Text)
    # Structured summary (intent, facts, actions taken), already checked for personal data.
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    assigned_to: Mapped[UUID | None]
    assigned_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    priority_rank: Mapped[int] = mapped_column(
        SmallInteger, Computed("CASE WHEN priority = 'high' THEN 2 ELSE 1 END", persisted=True)
    )


class ActionRow(Base):
    """An executed action. The only writer is the action executor, after the policy re-check."""

    __tablename__ = "actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_actions_customer_same_sandbox",
        ),
        ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_actions_order_id_orders"),
        ForeignKeyConstraint(
            ["conversation_id", "customer_id", "order_id"],
            ["conversations.id", "conversations.customer_id", "conversations.order_id"],
            name="fk_actions_conversation_matches_customer_order",
        ),
        # An action approved by a reviewer points at an approval that is approved, of the same
        # conversation and of the same proposal (the approval's key is the action's key).
        ForeignKeyConstraint(
            ["approval_request_id", "conversation_id", "idempotency_key", "approval_status"],
            [
                "approval_requests.id",
                "approval_requests.conversation_id",
                "approval_requests.proposal_key",
                "approval_requests.status",
            ],
            name="fk_actions_approval_matches_request",
        ),
        UniqueConstraint("idempotency_key"),
        CheckConstraint(one_of("kind", ACTION_KINDS), name="kind_known"),
        CheckConstraint(one_of("reason", CLAIM_REASONS), name="reason_known"),
        CheckConstraint(one_of("approved_by", APPROVED_BY), name="approved_by_known"),
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("jsonb_typeof(items) = 'array'", name="items_is_array"),
        CheckConstraint(
            "approval_status IS NULL OR approval_status = 'approved'", name="approval_is_approved"
        ),
        CheckConstraint(
            "(approved_by = 'reviewer') = (approval_request_id IS NOT NULL)",
            name="reviewer_has_approval",
        ),
        CheckConstraint(
            "(approval_request_id IS NULL) = (approval_status IS NULL)",
            name="approval_columns_together",
        ),
        Index(
            "ix_actions_sandbox_id_customer_id_executed_at",
            "sandbox_id",
            "customer_id",
            "executed_at",
        ),
        Index("ix_actions_order_id_executed_at", "order_id", "executed_at"),
        Index("ix_actions_conversation_id", "conversation_id"),
        Index("ix_actions_approval_request_id", "approval_request_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    order_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(Text)
    approval_request_id: Mapped[UUID | None]
    approval_status: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime]
    executed_at: Mapped[datetime]

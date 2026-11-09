"""Conversations (minimum), approval requests, handoffs and executed actions.

All four tables are new, so nothing holds a lock for long; `lock_timeout` is still set so the
migration never queues the application behind it. Written by hand: autogenerate does not emit
partial indexes with predicates, generated columns or triggers the way they are reviewed here.
CHECK lists are literal on purpose, so this migration keeps meaning the same thing if the
application enums change later. CHECK names go through `op.f`: the naming convention would prefix
the table name a second time.

An action points at its approval by `(approval_request_id, conversation_id, idempotency_key,
approval_status)`, which must match `(id, conversation_id, proposal_key, status)` of an approval
request; with the `CHECK`s on `actions`, an action approved by a reviewer can only be stored if
that approval is approved, of the same conversation and of the same proposal. The trigger keeps it
approved afterwards.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REJECT_DECIDED_APPROVAL_CHANGE = """
CREATE FUNCTION approval_requests_reject_decided_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'approval_requests is immutable once decided: % is not allowed', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _create_conversations()
    _create_approval_requests()
    _create_handoffs()
    _create_actions()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("actions")
    op.drop_table("handoffs")
    op.drop_table("approval_requests")
    op.execute("DROP FUNCTION approval_requests_reject_decided_change()")
    op.drop_table("conversations")


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _create_conversations() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('open', 'awaiting_approval', 'transferred', 'closed')",
            name=op.f("ck_conversations_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_conversations_customer_same_sandbox",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_conversations_order_id_orders"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint(
            "id", "customer_id", "order_id", name="uq_conversations_id_customer_id_order_id"
        ),
        sa.UniqueConstraint("id", "customer_id", name="uq_conversations_id_customer_id"),
    )
    op.create_index(
        "ix_conversations_sandbox_id_customer_id_created_at",
        "conversations",
        ["sandbox_id", "customer_id", "created_at"],
    )
    op.create_index("ix_conversations_order_id", "conversations", ["order_id"])


def _create_approval_requests() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_key", sa.Text(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_reasons", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("policy_ceiling_cents", sa.Integer(), nullable=False),
        sa.Column("policy_document", sa.Text(), nullable=False),
        sa.Column("fraud_score", sa.Float(), nullable=True),
        sa.Column("fraud_model_version", sa.Text(), nullable=True),
        sa.Column("fraud_factors", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_approval_requests_status_known"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_approval_requests_version_positive")),
        sa.CheckConstraint(
            "policy_ceiling_cents >= 0", name=op.f("ck_approval_requests_ceiling_not_negative")
        ),
        sa.CheckConstraint(
            "fraud_score BETWEEN 0 AND 1", name=op.f("ck_approval_requests_fraud_score_in_range")
        ),
        sa.CheckConstraint(
            "char_length(decision_reason) BETWEEN 1 AND 500",
            name=op.f("ck_approval_requests_decision_reason_length"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_by IS NULL AND decided_at IS NULL "
            "AND decision_reason IS NULL) "
            "OR (status <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND decision_reason IS NOT NULL)",
            name=op.f("ck_approval_requests_decision_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_approval_requests_customer_same_sandbox",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_approval_requests_order_id_orders"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "customer_id", "order_id"],
            ["conversations.id", "conversations.customer_id", "conversations.order_id"],
            name="fk_approval_requests_conversation_matches_customer_order",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_requests"),
        sa.UniqueConstraint(
            "conversation_id",
            "proposal_key",
            name="uq_approval_requests_conversation_id_proposal_key",
        ),
        sa.UniqueConstraint(
            "id",
            "conversation_id",
            "proposal_key",
            "status",
            name="uq_approval_requests_id_conversation_id_proposal_key_status",
        ),
    )
    op.create_index(
        "uq_approval_requests_pending_conversation",
        "approval_requests",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_approval_requests_pending_queue",
        "approval_requests",
        ["sandbox_id", "created_at", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_approval_requests_customer_id", "approval_requests", ["customer_id"])
    op.create_index("ix_approval_requests_order_id", "approval_requests", ["order_id"])
    op.execute(REJECT_DECIDED_APPROVAL_CHANGE)
    op.execute(
        "CREATE TRIGGER approval_requests_immutable_once_decided BEFORE UPDATE OR DELETE "
        "ON approval_requests FOR EACH ROW "
        "EXECUTE FUNCTION approval_requests_reject_decided_change()"
    )


def _create_handoffs() -> None:
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("assigned_to", sa.Uuid(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.Column(
            "priority_rank",
            sa.SmallInteger(),
            sa.Computed("CASE WHEN priority = 'high' THEN 2 ELSE 1 END", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "priority IN ('normal', 'high')", name=op.f("ck_handoffs_priority_known")
        ),
        sa.CheckConstraint("status IN ('open', 'assigned')", name=op.f("ck_handoffs_status_known")),
        sa.CheckConstraint(
            "(status = 'open' AND assigned_to IS NULL AND assigned_at IS NULL) "
            "OR (status = 'assigned' AND assigned_to IS NOT NULL AND assigned_at IS NOT NULL)",
            name=op.f("ck_handoffs_assignment_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_handoffs_customer_same_sandbox",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "customer_id"],
            ["conversations.id", "conversations.customer_id"],
            name="fk_handoffs_conversation_matches_customer",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_handoffs"),
    )
    op.create_index(
        "uq_handoffs_open_conversation",
        "handoffs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_handoffs_open_queue",
        "handoffs",
        ["sandbox_id", sa.literal_column("priority_rank DESC"), "created_at", "id"],
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_handoffs_customer_id", "handoffs", ["customer_id"])
    op.create_index("ix_handoffs_conversation_id", "handoffs", ["conversation_id"])


def _create_actions() -> None:
    op.create_table(
        "actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("approval_status", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('refund', 'coupon', 'cancellation')", name=op.f("ck_actions_kind_known")
        ),
        sa.CheckConstraint(
            "reason IN ('item_missing', 'wrong_item', 'quality_issue', 'late_delivery', "
            "'not_delivered', 'duplicate_charge', 'customer_cancellation')",
            name=op.f("ck_actions_reason_known"),
        ),
        sa.CheckConstraint(
            "approved_by IN ('automatic', 'reviewer')", name=op.f("ck_actions_approved_by_known")
        ),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_actions_amount_positive")),
        sa.CheckConstraint("jsonb_typeof(items) = 'array'", name=op.f("ck_actions_items_is_array")),
        sa.CheckConstraint(
            "approval_status IS NULL OR approval_status = 'approved'",
            name=op.f("ck_actions_approval_is_approved"),
        ),
        sa.CheckConstraint(
            "(approved_by = 'reviewer') = (approval_request_id IS NOT NULL)",
            name=op.f("ck_actions_reviewer_has_approval"),
        ),
        sa.CheckConstraint(
            "(approval_request_id IS NULL) = (approval_status IS NULL)",
            name=op.f("ck_actions_approval_columns_together"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_actions_customer_same_sandbox",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_actions_order_id_orders"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "customer_id", "order_id"],
            ["conversations.id", "conversations.customer_id", "conversations.order_id"],
            name="fk_actions_conversation_matches_customer_order",
        ),
        sa.ForeignKeyConstraint(
            ["approval_request_id", "conversation_id", "idempotency_key", "approval_status"],
            [
                "approval_requests.id",
                "approval_requests.conversation_id",
                "approval_requests.proposal_key",
                "approval_requests.status",
            ],
            name="fk_actions_approval_matches_request",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_actions"),
        sa.UniqueConstraint("idempotency_key", name="uq_actions_idempotency_key"),
    )
    op.create_index(
        "ix_actions_sandbox_id_customer_id_executed_at",
        "actions",
        ["sandbox_id", "customer_id", "executed_at"],
    )
    op.create_index("ix_actions_order_id_executed_at", "actions", ["order_id", "executed_at"])
    op.create_index("ix_actions_conversation_id", "actions", ["conversation_id"])
    op.create_index("ix_actions_approval_request_id", "actions", ["approval_request_id"])

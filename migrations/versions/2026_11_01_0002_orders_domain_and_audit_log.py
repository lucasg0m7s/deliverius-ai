"""Orders domain tables and the insert-only audit log.

Written by hand from a reviewed autogenerate draft: autogenerate does not emit triggers.
CHECK lists are literal on purpose, so this migration keeps meaning the same thing if the
application enums change later.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORDER_STATUSES = (
    "status IN ('placed', 'accepted', 'preparing', 'ready_for_pickup', 'out_for_delivery', "
    "'delivered', 'cancelled', 'delivery_failed')"
)

REJECT_AUDIT_CHANGE = """
CREATE FUNCTION audit_log_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is insert-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$
"""


def upgrade() -> None:
    _create_customers_and_restaurants()
    _create_orders()
    _create_order_children()
    _create_audit_log()


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER audit_log_no_update_or_delete ON audit_log")
    op.execute("DROP FUNCTION audit_log_reject_change()")
    op.drop_table("audit_log")
    op.drop_table("order_items")
    op.drop_table("delivery_events")
    op.drop_table("orders")
    op.drop_table("restaurants")
    op.drop_table("customers")


def _create_customers_and_restaurants() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cpf", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
        sa.UniqueConstraint("id", "sandbox_id", name="uq_customers_id_sandbox_id"),
        sa.UniqueConstraint("sandbox_id", "cpf", name="uq_customers_sandbox_id_cpf"),
    )
    op.create_table(
        "restaurants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_restaurants"),
    )


def _create_orders() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("restaurant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payment_method", sa.Text(), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("estimated_delivery_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("delivery_fee_cents", sa.Integer(), nullable=False),
        sa.Column("discount_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(ORDER_STATUSES, name="ck_orders_status_known"),
        sa.CheckConstraint(
            "payment_method IN ('pix', 'credit_card', 'debit_card', 'meal_voucher')",
            name="ck_orders_payment_method_known",
        ),
        sa.CheckConstraint(
            "subtotal_cents >= 0 AND delivery_fee_cents >= 0 AND discount_cents >= 0 "
            "AND total_cents >= 0",
            name="ck_orders_amounts_not_negative",
        ),
        sa.CheckConstraint(
            "discount_cents <= subtotal_cents + delivery_fee_cents",
            name="ck_orders_discount_within_amount",
        ),
        sa.CheckConstraint(
            "total_cents = subtotal_cents + delivery_fee_cents - discount_cents",
            name="ck_orders_total_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "sandbox_id"],
            ["customers.id", "customers.sandbox_id"],
            name="fk_orders_customer_same_sandbox",
        ),
        sa.ForeignKeyConstraint(
            ["restaurant_id"], ["restaurants.id"], name="fk_orders_restaurant_id_restaurants"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
    )
    op.create_index(
        "ix_orders_customer_listing",
        "orders",
        [
            "sandbox_id",
            "customer_id",
            sa.literal_column("placed_at DESC"),
            sa.literal_column("id DESC"),
        ],
    )
    op.create_index(
        "ix_orders_sandbox_listing",
        "orders",
        ["sandbox_id", sa.literal_column("placed_at DESC"), sa.literal_column("id DESC")],
    )
    op.create_index("ix_orders_restaurant_id", "orders", ["restaurant_id"])


def _create_order_children() -> None:
    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("allergens", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.CheckConstraint("unit_price_cents > 0", name="ck_order_items_unit_price_positive"),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_items_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])

    op.create_table(
        "delivery_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(ORDER_STATUSES, name="ck_delivery_events_status_known"),
        sa.CheckConstraint(
            "actor IN ('customer', 'restaurant', 'courier', 'system')",
            name="ck_delivery_events_actor_known",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_delivery_events_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_events"),
    )
    op.create_index(
        "ix_delivery_events_order_id_occurred_at", "delivery_events", ["order_id", "occurred_at"]
    )


def _create_audit_log() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("data", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "actor_type IN ('customer', 'reviewer', 'agent', 'system')",
            name="ck_audit_log_actor_type_known",
        ),
        sa.CheckConstraint(
            r"action ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'", name="ck_audit_log_action_format"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index(
        "ix_audit_log_sandbox_id_occurred_at", "audit_log", ["sandbox_id", "occurred_at"]
    )
    op.create_index("ix_audit_log_target_type_target_id", "audit_log", ["target_type", "target_id"])
    op.execute(REJECT_AUDIT_CHANGE)
    op.execute(
        "CREATE TRIGGER audit_log_no_update_or_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change()"
    )
    # TRUNCATE skips row triggers, so it needs its own statement-level one.
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()"
    )

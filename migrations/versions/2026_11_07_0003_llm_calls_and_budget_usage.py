"""LLM usage log and daily budget counters.

Both tables are new, so there is nothing to lock for long; `lock_timeout` is still set so a
migration never queues the application behind it. `llm_calls.conversation_id` has no foreign key
yet: the conversations table arrives later and adds it as NOT VALID. CHECK lists are literal on
purpose, so this migration keeps its meaning if the application enums change.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _create_llm_calls()
    _create_llm_budget_usage()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("llm_budget_usage")
    op.drop_table("llm_calls")


def _create_llm_calls() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("sandbox_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_name", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("real_cost_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("equivalent_cost_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_llm_calls_status_known"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_llm_calls_latency_not_negative"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND reasoning_tokens >= 0",
            name="ck_llm_calls_tokens_not_negative",
        ),
        sa.CheckConstraint(
            "real_cost_usd_micros >= 0 AND equivalent_cost_usd_micros >= 0",
            name="ck_llm_calls_costs_not_negative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index(
        "ix_llm_calls_conversation_id_created_at",
        "llm_calls",
        ["conversation_id", "created_at"],
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )


def _create_llm_budget_usage() -> None:
    op.create_table(
        "llm_budget_usage",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("requests_used", sa.Integer(), nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "requests_used >= 0 AND tokens_used >= 0",
            name="ck_llm_budget_usage_counters_not_negative",
        ),
        sa.PrimaryKeyConstraint("provider", "day", name="pk_llm_budget_usage"),
    )

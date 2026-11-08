"""Policy search: the `pt_unaccent` text search configuration and the `policy_chunks` table.

Everything here is new, so there is nothing to lock for long; `lock_timeout` is still set so the
migration never queues the application behind it. There is no vector index on purpose: the table
holds about fifty rows, and an exact scan is instant and never misses a result.

`pt_unaccent` is the `portuguese` configuration with `unaccent` in front of the stemmer, so
"nao chegou" finds "não chegou" and "cancelar" finds "cancelamento". The `tsv` column is generated
from `content` with the configuration named literally, which keeps the expression immutable.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE TEXT SEARCH CONFIGURATION pt_unaccent (COPY = portuguese)")
    op.execute(
        "ALTER TEXT SEARCH CONFIGURATION pt_unaccent "
        "ALTER MAPPING FOR hword, hword_part, word WITH unaccent, portuguese_stem"
    )
    op.create_table(
        "policy_chunks",
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("document_hash", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('pt_unaccent', content)", persisted=True),
            nullable=False,
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_policy_chunks"),
    )
    op.create_index("ix_policy_chunks_tsv", "policy_chunks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_policy_chunks_document_id", "policy_chunks", ["document_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("policy_chunks")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS pt_unaccent")
    op.execute("DROP EXTENSION IF EXISTS unaccent")

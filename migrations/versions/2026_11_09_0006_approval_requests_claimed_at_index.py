"""Index of a customer's approval requests by claim instant, for the fraud history reader.

The fraud features count the claims of the 90 days before a new one; the reader filters
`approval_requests` by sandbox, customer and `claimed_at`. `CREATE INDEX CONCURRENTLY` does not
block writes, and it cannot run inside a transaction, hence the autocommit block. If a concurrent
build fails it leaves an invalid index behind: drop it and run the migration again.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_approval_requests_sandbox_id_customer_id_claimed_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX,
            "approval_requests",
            ["sandbox_id", "customer_id", "claimed_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX,
            table_name="approval_requests",
            postgresql_concurrently=True,
            if_exists=True,
        )

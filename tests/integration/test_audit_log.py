from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from deliverius.db.audit import record
from deliverius.db.tables import AuditLogRow
from deliverius.shared.audit import AuditActorType, AuditEntry
from deliverius.shared.ids import SandboxId, new_id

pytestmark = pytest.mark.integration


def make_entry() -> AuditEntry:
    return AuditEntry(
        sandbox_id=SandboxId(new_id()),
        actor_type=AuditActorType.REVIEWER,
        actor_id=new_id(),
        action="approvals.decided",
        target_type="approval_request",
        target_id=new_id(),
        occurred_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        data={"decision": "approved", "amount_cents": 4590},
    )


async def row_of(session: AsyncSession, entry: AuditEntry) -> AuditLogRow | None:
    statement = select(AuditLogRow).where(AuditLogRow.id == entry.id)
    return (await session.execute(statement.execution_options(populate_existing=True))).scalar()


async def test_insertion_keeps_every_field(session: AsyncSession) -> None:
    entry = make_entry()

    await record(session, entry)

    row = await row_of(session, entry)
    assert row is not None
    assert (row.sandbox_id, row.actor_type, row.action, row.target_id) == (
        entry.sandbox_id,
        "reviewer",
        "approvals.decided",
        entry.target_id,
    )
    assert row.data == {"decision": "approved", "amount_cents": 4590}
    assert row.occurred_at == entry.occurred_at
    assert row.trace_id is None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET action = 'approvals.rewritten' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
        "TRUNCATE audit_log",
    ],
    ids=["update", "delete", "truncate"],
)
async def test_change_or_removal_is_refused_and_the_row_stays(
    session: AsyncSession, statement: str
) -> None:
    entry = make_entry()
    await record(session, entry)

    with pytest.raises(DBAPIError, match="insert-only"):
        async with session.begin_nested():
            await session.execute(text(statement), {"id": entry.id})

    row = await row_of(session, entry)
    assert row is not None
    assert row.action == "approvals.decided"


class OperationFailedError(Exception):
    """Stands for any failure after the audit entry was written in the same transaction."""


async def record_then_fail(engine: AsyncEngine, entry: AuditEntry) -> None:
    async with AsyncSession(engine) as session, session.begin():
        await record(session, entry)
        raise OperationFailedError


async def test_rolled_back_transaction_leaves_no_entry(engine: AsyncEngine) -> None:
    entry = make_entry()

    with pytest.raises(OperationFailedError):
        await record_then_fail(engine, entry)

    async with AsyncSession(engine) as session:
        count = await session.scalar(
            select(func.count()).select_from(AuditLogRow).where(AuditLogRow.id == entry.id)
        )
    assert count == 0

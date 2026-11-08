from sqlalchemy.ext.asyncio import AsyncSession

from deliverius.db.errors import translate_db_errors
from deliverius.db.tables import AuditLogRow
from deliverius.shared.audit import AuditEntry


async def record(session: AsyncSession, entry: AuditEntry) -> None:
    """Insert the entry in the caller's transaction: saved with its operation, or not at all.

    Never commits: the caller owns the unit of work.
    """
    session.add(
        AuditLogRow(
            id=entry.id,
            sandbox_id=entry.sandbox_id,
            occurred_at=entry.occurred_at,
            actor_type=entry.actor_type.value,
            actor_id=entry.actor_id,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            data=dict(entry.data),
        )
    )
    with translate_db_errors():
        await session.flush()

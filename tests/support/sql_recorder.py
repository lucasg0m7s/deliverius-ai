"""Records the SQL a session sends, to count statements and to EXPLAIN a repository's query."""

from typing import Any, Self

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession


class StatementRecorder:
    """Records every SQL statement the session sends, with its driver parameters."""

    def __init__(self, session: AsyncSession) -> None:
        self.statements: list[tuple[str, Any]] = []
        self._engine = session.bind.engine.sync_engine  # type: ignore[union-attr]

    def __enter__(self) -> Self:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        self.statements.append((statement, parameters))

"""Which attempt of each job gave the answer that was kept, so a repeated run starts there.

A failed attempt (provider error, deadline, invalid answer) leaves nothing in the response cache,
and the next attempt, with another key, then succeeds. Without a memory of it, a repeated run
would start over at attempt 1, call the real model again and could get another text: the output
would change. The log lives in the same SQLite file as the response cache. It holds only a hash
that names the job, an attempt number and the counts of what the earlier attempts discarded (so
the manifest of the repeated run adds up the same), never a prompt or a message.
"""

import hashlib
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from deliverius.datagen.discards import DiscardTally
from deliverius.llm.roles import ModelRole

FIRST_ATTEMPT = 1

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS datagen_attempts (
        job TEXT PRIMARY KEY,
        attempt INTEGER NOT NULL,
        discards TEXT NOT NULL
    )
"""


@dataclass(frozen=True, slots=True)
class Resume:
    """Where a job starts: the attempt that worked, and what the attempts before it discarded."""

    attempt: int
    discards: DiscardTally


class AttemptLog:
    """The winning attempt of each job, for one model.

    A job is named by its role, the model, its own key and the whole prompt it sends (system and
    human messages): a changed prompt, scenario or model is another job, and starts at attempt 1.
    """

    def __init__(self, path: Path, model: str) -> None:
        self._path = path
        self._model = model
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_CREATE_TABLE)

    def resume(self, role: ModelRole, key: str, messages: Sequence[tuple[str, str]]) -> Resume:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt, discards FROM datagen_attempts WHERE job = ?",
                (self._job(role, key, messages),),
            ).fetchone()
        if row is None:
            return Resume(FIRST_ATTEMPT, DiscardTally())
        return Resume(int(row[0]), DiscardTally.from_json(row[1]))

    def record(
        self,
        role: ModelRole,
        key: str,
        messages: Sequence[tuple[str, str]],
        attempt: int,
        earlier_discards: DiscardTally,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO datagen_attempts (job, attempt, discards) VALUES (?, ?, ?)",
                (self._job(role, key, messages), attempt, earlier_discards.to_json()),
            )

    def _job(self, role: ModelRole, key: str, messages: Sequence[tuple[str, str]]) -> str:
        parts = [role.value, self._model, key, *(part for message in messages for part in message)]
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

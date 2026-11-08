"""Response cache for evals and data generation. Never wired into the application.

Repeating an eval that already ran then costs no quota. The attempt number is part of the key
because pass^k needs k independent answers: without it the cache would return the same answer k
times. A cache object is for one attempt; a new attempt is a new object on the same file.

Only a hash of the request is stored, never the prompt, and the key is computed from `llm_string`,
which holds secrets as placeholders. The file must hold synthetic or masked content only.
"""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, override

import structlog
from langchain_core.caches import RETURN_VAL_TYPE, BaseCache
from langchain_core.load import dumps, loads
from langchain_core.load.serializable import Serializable
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, Generation
from pydantic import BaseModel

from deliverius.shared.errors import InvariantError

logger = structlog.get_logger(__name__)

# Set on a message that came from the cache, so the governor does not count it as a real call.
CACHE_HIT_MARKER = "llm_cache_hit"

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS llm_responses (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
"""
# Only these types may come back from the file: it is ours, but it is still read as data.
_ALLOWED_TYPES: list[type[Serializable]] = [
    Generation,
    ChatGeneration,
    ChatGenerationChunk,
    AIMessage,
    AIMessageChunk,
]


class SqliteResponseCache(BaseCache):
    """A `BaseCache` on a SQLite file. One short connection per operation, so threads are safe."""

    def __init__(self, path: Path, attempt: int) -> None:
        if attempt < 1:
            raise InvariantError("llm_cache.attempt_below_one")
        self._path = path
        self._attempt = attempt
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_CREATE_TABLE)

    @override
    def lookup(self, prompt: str, llm_string: str) -> RETURN_VAL_TYPE | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM llm_responses WHERE key = ?", (self._key(prompt, llm_string),)
            ).fetchone()
        if row is None:
            return None
        try:
            generations: list[Generation] = loads(row[0], allowed_objects=_ALLOWED_TYPES)
        except Exception as error:
            # A damaged or foreign entry is a miss, and the real answer then replaces it. It is
            # logged, by type only, so that a defect that makes every entry unreadable shows up.
            logger.warning("llm.cache_entry_unreadable", error_type=type(error).__name__)
            return None
        for generation in generations:
            if isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage):
                generation.message.response_metadata[CACHE_HIT_MARKER] = True
        return generations

    @override
    def update(self, prompt: str, llm_string: str, return_val: RETURN_VAL_TYPE) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO llm_responses (key, value) VALUES (?, ?)",
                (
                    self._key(prompt, llm_string),
                    dumps([_serializable(generation) for generation in return_val]),
                ),
            )

    @override
    def clear(self, **kwargs: Any) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM llm_responses")

    def _key(self, prompt: str, llm_string: str) -> str:
        material = "\0".join((str(self._attempt), llm_string, prompt))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _serializable(generation: Generation) -> Generation:
    """A copy that `dumps` can write; the generation that goes back to the caller is untouched.

    With structured output, `ChatOpenAI` puts the parsed Pydantic object in
    `additional_kwargs["parsed"]`, which `dumps` cannot write and `loads` then refuses. The OpenAI
    parser takes a `dict` there and validates it against the schema, so the object is stored as
    JSON and a cache hit yields the same validated object, with no schema needed here.
    """
    if not isinstance(generation, ChatGeneration):
        return generation
    parsed = generation.message.additional_kwargs.get("parsed")
    if not isinstance(parsed, BaseModel):
        return generation
    additional_kwargs = {
        **generation.message.additional_kwargs,
        "parsed": parsed.model_dump(mode="json"),
    }
    message = generation.message.model_copy(update={"additional_kwargs": additional_kwargs})
    return generation.model_copy(update={"message": message})

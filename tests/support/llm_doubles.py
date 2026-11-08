"""Test doubles for the LLM usage store and the provider pause."""

from datetime import date

from deliverius.db.errors import DatabaseUnavailableError
from deliverius.llm.errors import ProviderPausedError
from deliverius.llm.in_memory import InMemoryLlmUsageStore
from deliverius.llm.ports import BudgetLimit, BudgetUsage, LlmCallRecord
from deliverius.shared.model_ref import LlmProvider


class StoreDown(InMemoryLlmUsageStore):
    """The database is unreachable: every operation fails the way `translate_db_errors` reports."""

    async def reserve(self, provider: LlmProvider, day: date, limit: BudgetLimit) -> int | None:
        raise DatabaseUnavailableError

    async def add_tokens(self, provider: LlmProvider, day: date, tokens: int) -> int:
        raise DatabaseUnavailableError

    async def record_call(self, record: LlmCallRecord) -> None:
        raise DatabaseUnavailableError

    async def read_usage(self, provider: LlmProvider, day: date) -> BudgetUsage:
        raise DatabaseUnavailableError


class RecordingFails(InMemoryLlmUsageStore):
    """Budget works, but writing the usage row fails with a message that carries `leaked_text`."""

    def __init__(self, leaked_text: str = "driver detail") -> None:
        super().__init__()
        self._leaked_text = leaked_text

    async def record_call(self, record: LlmCallRecord) -> None:
        raise RuntimeError(self._leaked_text)


class TokenSumFails(InMemoryLlmUsageStore):
    async def add_tokens(self, provider: LlmProvider, day: date, tokens: int) -> int:
        raise RuntimeError


class FakePause:
    """A provider pause that can be set to refuse, and remembers the errors it was told about."""

    def __init__(self, seconds_left: float | None = None) -> None:
        self.seconds_left = seconds_left
        self.errors: list[BaseException] = []

    async def wait_if_paused(self) -> None:
        if self.seconds_left is not None:
            raise ProviderPausedError(LlmProvider.GEMINI, self.seconds_left)

    def note_error(self, error: BaseException) -> None:
        self.errors.append(error)

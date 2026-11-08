from datetime import date

from deliverius.llm.ports import BudgetLimit, BudgetUsage, LlmCallRecord
from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider


class InMemoryLlmUsageStore:
    """`LlmUsageStore` kept in memory, with the same contract as Postgres.

    For tests of code that spends budget; the shared contract suite keeps both interchangeable.
    No `await` sits between reading and writing a counter, so it is atomic on one event loop.
    """

    def __init__(self) -> None:
        self._usage: dict[tuple[LlmProvider, date], BudgetUsage] = {}
        self.calls: list[LlmCallRecord] = []

    async def reserve(self, provider: LlmProvider, day: date, limit: BudgetLimit) -> int | None:
        usage = self._usage.get((provider, day), BudgetUsage(0, 0))
        if limit.reached_by(usage):
            return None
        self._usage[(provider, day)] = BudgetUsage(usage.requests_used + 1, usage.tokens_used)
        return usage.requests_used + 1

    async def add_tokens(self, provider: LlmProvider, day: date, tokens: int) -> int:
        if tokens < 0:
            raise InvariantError("llm_usage.negative_tokens")
        usage = self._usage.get((provider, day), BudgetUsage(0, 0))
        total = usage.tokens_used + tokens
        self._usage[(provider, day)] = BudgetUsage(usage.requests_used, total)
        return total

    async def record_call(self, record: LlmCallRecord) -> None:
        self.calls.append(record)

    async def read_usage(self, provider: LlmProvider, day: date) -> BudgetUsage:
        return self._usage.get((provider, day), BudgetUsage(0, 0))

    def usage(self, provider: LlmProvider, day: date) -> BudgetUsage | None:
        return self._usage.get((provider, day))

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from deliverius.llm.pricing import TokenUsage
from deliverius.llm.roles import ModelRole
from deliverius.shared.clock import require_aware
from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider


class CallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    requests_used: int
    tokens_used: int


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    """Daily ceilings for one provider. `None` is unlimited; zero blocks every call."""

    daily_requests: int | None
    daily_tokens: int | None

    def __post_init__(self) -> None:
        for limit in (self.daily_requests, self.daily_tokens):
            if limit is not None and limit < 0:
                raise InvariantError("budget_limit.negative")

    def reached_by(self, usage: BudgetUsage) -> bool:
        """Whether a request or a token ceiling is already met, so the next call is refused."""
        requests, tokens = self.daily_requests, self.daily_tokens
        requests_reached = requests is not None and usage.requests_used >= requests
        tokens_reached = tokens is not None and usage.tokens_used >= tokens
        return requests_reached or tokens_reached


UNLIMITED = BudgetLimit(daily_requests=None, daily_tokens=None)


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """A provider's day: what was used, against which ceilings."""

    provider: LlmProvider
    day: date
    usage: BudgetUsage
    limit: BudgetLimit

    @property
    def exhausted(self) -> bool:
        return self.limit.reached_by(self.usage)


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One real attempt against a provider. Holds no message text and no provider error text."""

    id: UUID
    created_at: datetime
    role: ModelRole
    provider: LlmProvider
    model: str
    status: CallStatus
    latency_ms: int
    usage: TokenUsage
    conversation_id: UUID | None = None
    sandbox_id: UUID | None = None
    prompt_name: str | None = None
    prompt_version: int | None = None
    prompt_hash: str | None = None
    error_type: str | None = None
    real_cost_usd_micros: int | None = None
    equivalent_cost_usd_micros: int | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")
        if self.latency_ms < 0:
            raise InvariantError("llm_call.latency_negative")
        for cost in (self.real_cost_usd_micros, self.equivalent_cost_usd_micros):
            if cost is not None and cost < 0:
                raise InvariantError("llm_call.cost_negative")


class LlmUsageStore(Protocol):
    """Budget counters and the usage log. Every method is its own short transaction.

    The caller must not hold an open transaction around a model call (`robustez.md`, section 4):
    a reservation has to be committed before the provider is reached.
    """

    async def reserve(self, provider: LlmProvider, day: date, limit: BudgetLimit) -> int | None:
        """Count one request for the day, atomically, unless a limit is already reached.

        Returns the day's request count including this one, or `None` if the call is refused. A
        reservation is never handed back. Raises `DatabaseUnavailableError` if the store is down.
        """
        ...

    async def add_tokens(self, provider: LlmProvider, day: date, tokens: int) -> int:
        """Add the tokens a response used and return the day total; concurrent sums are not lost."""
        ...

    async def record_call(self, record: LlmCallRecord) -> None: ...

    async def read_usage(self, provider: LlmProvider, day: date) -> BudgetUsage:
        """The day's counters, zero where nothing was counted yet; changes nothing.

        Raises `DatabaseUnavailableError` if the store is down.
        """
        ...


class ProviderPause(Protocol):
    """What the governor needs from a provider that asked for a pause after a `429`."""

    async def wait_if_paused(self) -> None:
        """Return when the provider may be called; raise `ProviderPausedError` if it may not."""
        ...

    def note_error(self, error: BaseException) -> None:
        """Called with every failed call, so the pause can open on a `429`."""
        ...

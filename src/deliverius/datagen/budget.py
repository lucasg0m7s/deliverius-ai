"""The budget check the generator makes itself.

A model with a response cache only counts the budget, after the call, because a cache hit is known
only once the call is over. So a run that wants to stop at the daily ceiling asks before each call:
the usage of the day, read from the store, against the ceilings of the role's provider. A cache hit
is not spent, but it is not served either once the day is spent: the run stops, and the next day's
run replays the cache for free.
"""

from collections.abc import Awaitable, Callable

import structlog

from deliverius.db.errors import DatabaseUnavailableError
from deliverius.llm.call import DegradedReason
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime

logger = structlog.get_logger(__name__)

# `None` means the job may start; a reason means the run must stop.
type BudgetGuard = Callable[[], Awaitable[DegradedReason | None]]


def budget_guard(runtime: LlmRuntime, role: ModelRole) -> BudgetGuard:
    """Checks the role's provider before each job: spent ceiling or unreachable store stops."""

    async def check() -> DegradedReason | None:
        try:
            status = await runtime.budget_status(role)
        except DatabaseUnavailableError:
            return DegradedReason.BUDGET_UNAVAILABLE
        return DegradedReason.BUDGET_EXHAUSTED if status.exhausted else None

    return check

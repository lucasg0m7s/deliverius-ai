"""Running many LLM jobs under the quota: limited concurrency, new attempts, a clean stop.

A job is one call (a scenario, a policy section). The result list keeps the order of the jobs, not
the order they finished in, so the output does not depend on timing. If the budget runs out, the
jobs in flight finish, the pending ones never start, and the run ends with an error that says how
many are missing. The cache holds what was paid for, and the attempt log remembers which attempt
of each job gave the kept answer, so a repeated run asks the same thing and gets the same text.

A short pause of the provider (a per-minute `429`) is waited out and the same job is asked again,
without using up an attempt. A long one, or too many in a row, stops the run like an exhausted
budget.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog
from pydantic import BaseModel

from deliverius.datagen.attempts import FIRST_ATTEMPT, AttemptLog
from deliverius.datagen.budget import BudgetGuard
from deliverius.datagen.discards import DiscardTally
from deliverius.datagen.errors import BudgetExhaustedError
from deliverius.llm.cache import SqliteResponseCache
from deliverius.llm.call import DegradedMode, DegradedReason, call_structured
from deliverius.llm.gate import Sleep
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime, RoleModels
from deliverius.shared import InvariantError

logger = structlog.get_logger(__name__)

PROMPTS_DIRECTORY: Final = Path(__file__).parent / "prompts"

# Gives the models of one attempt, built on that attempt's cache: the attempt is part of the key.
type ModelsForAttempt = Callable[[int], RoleModels]

_STOPPING_REASONS: Final = frozenset(
    {
        DegradedReason.BUDGET_EXHAUSTED,
        DegradedReason.BUDGET_UNAVAILABLE,
        DegradedReason.PROVIDER_PAUSED,
    }
)
_NO_VALID_OUTPUT: Final = "no_valid_output"
# Beyond these, a pause is not a hiccup but a quota that needs the next minute or day.
MAX_PAUSE_WAIT_SECONDS: Final = 60.0
MAX_PAUSES_IN_A_ROW: Final = 10


def cached_models(runtime: LlmRuntime, role: ModelRole, cache_path: Path) -> ModelsForAttempt:
    """The models of `role` on the response cache of each attempt: one file, one object per attempt.

    The cache is what makes a run repeatable, and a repeated run free: the quota is spent once.
    """

    def models(attempt: int) -> RoleModels:
        return runtime.models_for(role, cache=SqliteResponseCache(cache_path, attempt))

    return models


@dataclass(frozen=True, slots=True)
class PauseWatch:
    """How long the provider still asks to be left alone, and how to wait for it."""

    seconds_left: Callable[[], float]
    sleep: Sleep = asyncio.sleep


def pause_watch(runtime: LlmRuntime, role: ModelRole, sleep: Sleep = asyncio.sleep) -> PauseWatch:
    return PauseWatch(lambda: runtime.pause_seconds_left(role), sleep)


@dataclass(frozen=True, slots=True)
class Job[Payload]:
    """`key` names the job in the logs (a scenario ID), never carries text from the model."""

    key: str
    payload: Payload
    messages: list[tuple[str, str]]


async def run_jobs[Payload, Schema: BaseModel, Result](
    jobs: Sequence[Job[Payload]],
    *,
    schema: type[Schema],
    role: ModelRole,
    models_for_attempt: ModelsForAttempt,
    accept: Callable[[Payload, Schema, DiscardTally], Result | None],
    discards: DiscardTally,
    concurrency: int,
    max_attempts: int,
    budget_guard: BudgetGuard | None = None,
    attempt_log: AttemptLog | None = None,
    pause: PauseWatch | None = None,
) -> list[Result | None]:
    """The accepted result of each job, in the order of `jobs`; `None` where every attempt failed.

    `accept` returns `None` when the answer has nothing usable, which asks for another attempt, and
    counts what it discards in the tally it is given. Every attempt's tally is added to `discards`,
    the failed ones too; a job resumed from the attempt log also adds back what its earlier
    attempts discarded, so a repeated run counts the same as the first.
    `budget_guard` is asked before every call; a reason from it stops the run like a degraded call.
    `attempt_log` starts each job at the attempt that worked in an earlier run and records the one
    that works now. `pause` makes a short pause of the provider something to wait for; without it
    any pause stops the run. Raises `BudgetExhaustedError` when a budget or pause stopped the run.
    """
    run = _Run(
        jobs=jobs,
        schema=schema,
        role=role,
        models_for_attempt=models_for_attempt,
        accept=accept,
        discards=discards,
        max_attempts=max_attempts,
        semaphore=asyncio.Semaphore(concurrency),
        budget_guard=budget_guard,
        attempt_log=attempt_log,
        pause=pause,
    )
    try:
        async with asyncio.TaskGroup() as group:
            for index in range(len(jobs)):
                group.create_task(run.work(index))
    except ExceptionGroup as failure:
        # The first failure is the one that matters; the group only wraps it.
        raise failure.exceptions[0] from failure
    if run.stop_reason is not None:
        remaining = run.finished.count(False)
        logger.warning("datagen.budget_exhausted", remaining=remaining, reason=run.stop_reason)
        raise BudgetExhaustedError(remaining, run.stop_reason)
    return run.results


class _Run[Payload, Schema: BaseModel, Result]:
    def __init__(
        self,
        *,
        jobs: Sequence[Job[Payload]],
        schema: type[Schema],
        role: ModelRole,
        models_for_attempt: ModelsForAttempt,
        accept: Callable[[Payload, Schema, DiscardTally], Result | None],
        discards: DiscardTally,
        max_attempts: int,
        semaphore: asyncio.Semaphore,
        budget_guard: BudgetGuard | None,
        attempt_log: AttemptLog | None,
        pause: PauseWatch | None,
    ) -> None:
        self._jobs = jobs
        self._schema = schema
        self._role = role
        self._models_for_attempt = models_for_attempt
        self._accept = accept
        self._discards = discards
        self._max_attempts = max_attempts
        self._semaphore = semaphore
        self._budget_guard = budget_guard
        self._attempt_log = attempt_log
        self._pause = pause
        self._pauses_in_a_row = 0
        self._models: dict[int, RoleModels] = {}
        self.results: list[Result | None] = [None] * len(jobs)
        self.finished = [False] * len(jobs)
        self.stop_reason: DegradedReason | None = None

    async def work(self, index: int) -> None:
        job = self._jobs[index]
        last_reason = _NO_VALID_OUTPUT
        first_attempt, earlier_discards = self._resume(job)
        for attempt in range(first_attempt, self._max_attempts + 1):
            answer = await self._ask_through_pauses(job, attempt)
            if answer is None:
                return
            if isinstance(answer, DegradedMode):
                if answer.reason in _STOPPING_REASONS:
                    self.stop_reason = self.stop_reason or answer.reason
                    return
                last_reason = answer.reason.value
                continue
            self._pauses_in_a_row = 0
            attempt_discards = DiscardTally()
            accepted = self._accept(job.payload, answer, attempt_discards)
            self._discards.merge(attempt_discards)
            if accepted is not None:
                self._record(job, attempt, earlier_discards)
                self.results[index] = accepted
                self.finished[index] = True
                return
            earlier_discards.merge(attempt_discards)
            last_reason = _NO_VALID_OUTPUT
        logger.warning(
            "datagen.scenario_failed",
            scenario_id=job.key,
            attempts=self._max_attempts,
            reason=last_reason,
        )
        self.finished[index] = True

    def _resume(self, job: Job[Payload]) -> tuple[int, DiscardTally]:
        """The attempt to start at and what the attempts before it discarded (already counted)."""
        if self._attempt_log is None:
            return FIRST_ATTEMPT, DiscardTally()
        resume = self._attempt_log.resume(self._role, job.key, job.messages)
        if resume.attempt > self._max_attempts:
            return FIRST_ATTEMPT, DiscardTally()
        self._discards.merge(resume.discards)
        return resume.attempt, resume.discards

    def _record(self, job: Job[Payload], attempt: int, earlier_discards: DiscardTally) -> None:
        if self._attempt_log is not None:
            self._attempt_log.record(self._role, job.key, job.messages, attempt, earlier_discards)

    async def _ask_through_pauses(
        self, job: Job[Payload], attempt: int
    ) -> Schema | DegradedMode | None:
        """Like `_ask`, but a pause worth waiting for is waited out and the same call made again."""
        while True:
            answer = await self._ask(job, attempt)
            paused = (
                isinstance(answer, DegradedMode) and answer.reason is DegradedReason.PROVIDER_PAUSED
            )
            if not paused or not await self._waited_out_pause(job):
                return answer

    async def _waited_out_pause(self, job: Job[Payload]) -> bool:
        """Whether the pause was waited out; `False` when it is too long or keeps coming back."""
        if self._pause is None:
            return False
        self._pauses_in_a_row += 1
        seconds = self._pause.seconds_left()
        if seconds > MAX_PAUSE_WAIT_SECONDS or self._pauses_in_a_row > MAX_PAUSES_IN_A_ROW:
            return False
        logger.info("datagen.provider_pause_waited", scenario_id=job.key, seconds=seconds)
        if seconds > 0:
            await self._pause.sleep(seconds)
        return True

    async def _ask(self, job: Job[Payload], attempt: int) -> Schema | DegradedMode | None:
        """`None` when the run was already stopped: the job never starts."""
        async with self._semaphore:
            if self.stop_reason is None and self._budget_guard is not None:
                reason = await self._budget_guard()
                # Another job may have stopped the run while this one waited for the store.
                self.stop_reason = self.stop_reason or reason
            if self.stop_reason is not None:
                return None
            if attempt not in self._models:
                self._models[attempt] = self._models_for_attempt(attempt)
            models = self._models[attempt]
            if models.role is not self._role:
                raise InvariantError("generation.role_mismatch")
            return await call_structured(models, self._schema, job.messages, self._role)

"""A provider that answered `429` is left alone for as long as it asked.

One gate per provider, shared by every role that uses it, in memory and for one instance (like
the rate limiter). Reading and writing `not_before` have no `await` between them, so one event
loop cannot interleave them. The wait asked for is parsed with plain number parsing, never a
regex over text from outside, and capped: a provider cannot park the application for a day.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import structlog
from langchain_core.exceptions import ModelRateLimitError

from deliverius.llm.errors import ProviderPausedError
from deliverius.shared.clock import Clock
from deliverius.shared.model_ref import LlmProvider

DEFAULT_PAUSE_SECONDS = 5.0
MAX_PAUSE_SECONDS = 3_600.0
# Up to this much is cheap to wait for (a per-minute `429` often asks for little); more is refused.
MAX_WAIT_SECONDS = 2.0

type Sleep = Callable[[float], Awaitable[None]]

logger = structlog.get_logger(__name__)


class ProviderGate:
    """Implements `ProviderPause` for one provider."""

    def __init__(self, provider: LlmProvider, clock: Clock, sleep: Sleep = asyncio.sleep) -> None:
        self._provider = provider
        self._clock = clock
        self._sleep = sleep
        self._not_before: datetime | None = None

    async def wait_if_paused(self) -> None:
        seconds_left = self.seconds_left()
        if seconds_left <= 0:
            return
        if seconds_left > MAX_WAIT_SECONDS:
            raise ProviderPausedError(self._provider, seconds_left)
        await self._sleep(seconds_left)

    def note_error(self, error: BaseException) -> None:
        if isinstance(error, ModelRateLimitError):
            self.pause_for(pause_seconds_from(error))

    def pause_for(self, seconds: float) -> None:
        """Never shortens a pause that is already running."""
        until = self._clock.now() + timedelta(seconds=seconds)
        if self._not_before is None or until > self._not_before:
            self._not_before = until
            logger.warning(
                "llm.provider_paused", provider=self._provider, state="opened", seconds=seconds
            )

    def seconds_left(self) -> float:
        """How long the pause still runs; `0.0` when there is none."""
        if self._not_before is None:
            return 0.0
        return max((self._not_before - self._clock.now()).total_seconds(), 0.0)


def pause_seconds_from(error: BaseException) -> float:
    """The wait the provider asked for, or the default when it did not say something usable."""
    for link in _causal_chain(error):
        asked = _retry_after_header(link)
        if asked is None:
            asked = _gemini_retry_delay(link)
        if asked is not None:
            return min(asked, MAX_PAUSE_SECONDS)
    return DEFAULT_PAUSE_SECONDS


def _causal_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain and len(chain) < 5:
        chain.append(current)
        current = current.__cause__
    return chain


def _retry_after_header(error: BaseException) -> float | None:
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        return _seconds(headers.get("retry-after"))
    except (AttributeError, TypeError):
        return None


def _gemini_retry_delay(error: BaseException) -> float | None:
    """The `retryDelay` of Google's `RetryInfo` detail (`"37s"`), inside the SDK error's JSON."""
    return _find_retry_delay(getattr(error, "details", None), depth=0)


def _find_retry_delay(node: Any, depth: int) -> float | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        for key in ("retryDelay", "retry_delay"):
            if key in node:
                return _seconds(node[key])
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            found = _find_retry_delay(child, depth + 1)
            if found is not None:
                return found
    return None


def _seconds(value: object) -> float | None:
    """A non-negative finite number of seconds from `"60"`, `"37s"` or `1.5`; else `None`.

    An HTTP date, a negative or a non-finite value is unreadable, and the caller uses the default.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip().removesuffix("s")
        try:
            seconds = float(text)
        except ValueError:
            return None
    else:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None

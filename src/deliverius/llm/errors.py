from enum import StrEnum

from deliverius.shared.errors import DeliveriusError
from deliverius.shared.model_ref import LlmProvider


class PromptError(DeliveriusError):
    """A prompt file is missing, malformed or asked for by an invalid name or version.

    `rule` is a stable code such as `prompt.no_header`; `subject` names the file or the bad input.
    """

    def __init__(self, rule: str, subject: str, detail: str | None = None) -> None:
        message = f"{rule}: {subject}" if detail is None else f"{rule}: {subject} ({detail})"
        super().__init__(message)
        self.rule = rule
        self.subject = subject
        self.detail = detail


class PromptNotFoundError(PromptError):
    """No file exists for the requested prompt name and version."""


class BudgetRefusal(StrEnum):
    EXHAUSTED = "exhausted"
    DATABASE_UNAVAILABLE = "database_unavailable"


class LlmBudgetExceededError(DeliveriusError):
    """The call was refused before leaving for the provider: no budget, or no way to check it."""

    def __init__(self, provider: LlmProvider, reason: BudgetRefusal) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason


class ProviderPausedError(DeliveriusError):
    """The provider asked to be left alone (a `429`) and the wait is longer than a call may take."""

    def __init__(self, provider: LlmProvider, seconds_left: float) -> None:
        super().__init__(f"{provider}: paused for {seconds_left:.1f}s")
        self.provider = provider
        self.seconds_left = seconds_left


class LlmConfigurationError(DeliveriusError):
    """The LLM layer cannot be assembled: a provider key is missing for a configured role.

    Names the environment variables, never their values.
    """

    def __init__(self, missing_keys: dict[str, list[str]]) -> None:
        detail = "; ".join(
            f"{key} (used by {', '.join(users)})" for key, users in sorted(missing_keys.items())
        )
        super().__init__(f"missing API key: {detail}")
        self.missing_keys = missing_keys

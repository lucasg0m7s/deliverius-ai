from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from deliverius.shared.errors import InvariantError


class LlmProvider(StrEnum):
    GEMINI = "gemini"
    GROQ = "groq"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A model as written in configuration: `provider:model`, e.g. `groq:openai/gpt-oss-120b`."""

    provider: LlmProvider
    model: str

    def __post_init__(self) -> None:
        if not self.model or any(character.isspace() for character in self.model):
            raise InvariantError("model_ref.model_invalid")

    @classmethod
    def parse(cls, value: object) -> Self:
        """Split at the first colon only, because model names may hold their own (`llama3.2:3b`)."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise InvariantError("model_ref.not_text")
        provider, separator, model = value.partition(":")
        if not separator:
            raise InvariantError("model_ref.format")
        try:
            known = LlmProvider(provider)
        except ValueError as error:
            raise InvariantError("model_ref.unknown_provider") from error
        return cls(known, model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"

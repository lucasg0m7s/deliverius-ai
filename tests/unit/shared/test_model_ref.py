import pytest

from deliverius.shared.errors import InvariantError
from deliverius.shared.model_ref import LlmProvider, ModelRef


@pytest.mark.parametrize(
    ("text", "provider", "model"),
    [
        ("gemini:gemini-3.5-flash-lite", LlmProvider.GEMINI, "gemini-3.5-flash-lite"),
        ("groq:openai/gpt-oss-120b", LlmProvider.GROQ, "openai/gpt-oss-120b"),
        ("ollama:llama3.2:3b", LlmProvider.OLLAMA, "llama3.2:3b"),
        ("anthropic:claude-x", LlmProvider.ANTHROPIC, "claude-x"),
    ],
)
def test_parse_splits_at_the_first_colon(text: str, provider: LlmProvider, model: str) -> None:
    reference = ModelRef.parse(text)

    assert (reference.provider, reference.model) == (provider, model)
    assert str(reference) == text


def test_parse_returns_an_existing_reference_unchanged() -> None:
    reference = ModelRef(LlmProvider.GROQ, "m")

    assert ModelRef.parse(reference) is reference


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("gemini", "model_ref.format"),
        ("", "model_ref.format"),
        ("mistral:large", "model_ref.unknown_provider"),
        ("Gemini:flash", "model_ref.unknown_provider"),
        ("gemini:", "model_ref.model_invalid"),
        ("gemini:a b", "model_ref.model_invalid"),
        ("gemini:\tmodel", "model_ref.model_invalid"),
        (42, "model_ref.not_text"),
        (None, "model_ref.not_text"),
    ],
)
def test_parse_rejects_a_malformed_reference_with_a_stable_rule(value: object, rule: str) -> None:
    with pytest.raises(InvariantError) as error:
        ModelRef.parse(value)

    assert error.value.rule == rule

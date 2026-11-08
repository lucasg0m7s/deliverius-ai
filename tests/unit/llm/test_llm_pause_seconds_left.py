"""`LlmRuntime.pause_seconds_left`: how long a role's provider still asks to be left alone."""

from datetime import timedelta

from support.llm_runtime import ScriptedProvider, scripted_runtime, settings_with

from deliverius.llm.roles import ModelRole
from deliverius.shared.model_ref import LlmProvider

PROVIDERS = {LlmProvider.GEMINI: ScriptedProvider([]), LlmProvider.GROQ: ScriptedProvider([])}


def test_there_is_no_wait_without_a_pause() -> None:
    runtime, _, _ = scripted_runtime(settings_with(), PROVIDERS)

    assert runtime.pause_seconds_left(ModelRole.DATAGEN) == 0.0


def test_the_wait_is_the_rest_of_the_pause_of_the_provider_of_the_role() -> None:
    runtime, _, clock = scripted_runtime(settings_with(), PROVIDERS)
    runtime.gate_for(LlmProvider.GEMINI).pause_for(7)
    clock.advance(timedelta(seconds=3))

    assert runtime.pause_seconds_left(ModelRole.DATAGEN) == 4.0
    assert runtime.pause_seconds_left(ModelRole.DATAGEN_HOLDOUT) == 0.0


def test_the_wait_is_zero_once_the_pause_is_over() -> None:
    runtime, _, clock = scripted_runtime(settings_with(), PROVIDERS)
    runtime.gate_for(LlmProvider.GEMINI).pause_for(7)
    clock.advance(timedelta(seconds=8))

    assert runtime.pause_seconds_left(ModelRole.DATAGEN) == 0.0

"""The production wiring, assembled without reaching the database."""

import pytest
from support.llm_runtime import settings_with

from deliverius.cli.wiring import database_runtime
from deliverius.config import Settings
from deliverius.llm.errors import LlmConfigurationError
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime


async def test_the_database_runtime_builds_the_runtime_and_closes_the_engine() -> None:
    async with database_runtime(settings_with(), [ModelRole.DATAGEN]) as runtime:
        assert isinstance(runtime, LlmRuntime)


async def test_a_role_without_its_provider_key_fails_with_the_variable_name() -> None:
    settings = Settings(_env_file=None, gemini_api_key="k")  # type: ignore[call-arg]

    with pytest.raises(LlmConfigurationError) as error:
        async with database_runtime(settings, [ModelRole.DATAGEN_HOLDOUT]):
            pass

    assert set(error.value.missing_keys) == {"GROQ_API_KEY"}

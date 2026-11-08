import pytest

from deliverius.llm.roles import ROLE_CONFIGS, ModelRole


def test_every_role_has_a_configuration() -> None:
    assert set(ROLE_CONFIGS) == set(ModelRole)


@pytest.mark.parametrize("role", list(ModelRole))
def test_every_role_configuration_is_complete_and_coherent(role: ModelRole) -> None:
    config = ROLE_CONFIGS[role]

    assert config.attempt_timeout_seconds > 0
    assert config.max_output_tokens > 0
    assert 0 <= config.temperature <= 2
    assert config.total_deadline_seconds > config.attempt_timeout_seconds


def test_agent_and_writer_values_follow_the_design() -> None:
    agent, writer = ROLE_CONFIGS[ModelRole.AGENT], ROLE_CONFIGS[ModelRole.WRITER]

    assert (agent.attempt_timeout_seconds, agent.total_deadline_seconds) == (20, 45)
    assert (writer.attempt_timeout_seconds, writer.total_deadline_seconds) == (10, 25)
    assert (agent.max_output_tokens, writer.max_output_tokens) == (1_024, 512)
    assert (agent.temperature, writer.temperature) == (0.0, 0.3)


@pytest.mark.parametrize(
    "role",
    [ModelRole.CUSTOMER_SIM, ModelRole.JUDGE, ModelRole.DATAGEN, ModelRole.DATAGEN_HOLDOUT],
)
def test_evaluation_and_generation_roles_share_the_long_limits(role: ModelRole) -> None:
    config = ROLE_CONFIGS[role]

    assert (config.attempt_timeout_seconds, config.total_deadline_seconds) == (30, 90)


@pytest.mark.parametrize("role", [ModelRole.CUSTOMER_SIM, ModelRole.JUDGE])
def test_evaluation_roles_cap_the_output_at_1024_tokens(role: ModelRole) -> None:
    assert ROLE_CONFIGS[role].max_output_tokens == 1_024


@pytest.mark.parametrize("role", [ModelRole.DATAGEN, ModelRole.DATAGEN_HOLDOUT])
def test_generation_roles_have_room_for_ten_messages_and_the_reasoning(role: ModelRole) -> None:
    assert ROLE_CONFIGS[role].max_output_tokens == 2_048


def test_the_holdout_role_is_configured_like_the_main_generation_role() -> None:
    assert ROLE_CONFIGS[ModelRole.DATAGEN_HOLDOUT] == ROLE_CONFIGS[ModelRole.DATAGEN]


def test_judge_is_deterministic_and_the_simulated_customer_is_varied() -> None:
    assert ROLE_CONFIGS[ModelRole.JUDGE].temperature == 0.0
    assert ROLE_CONFIGS[ModelRole.CUSTOMER_SIM].temperature == 0.9


def test_the_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        ROLE_CONFIGS[ModelRole.AGENT] = ROLE_CONFIGS[ModelRole.WRITER]  # type: ignore[index]

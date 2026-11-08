"""The steps of the generator: which roles they need, how many calls, what the manifest merges."""

import pytest

from deliverius.datagen.config import DatagenConfig
from deliverius.datagen.pipeline import (
    ALL_STEPS,
    CommitInfo,
    Step,
    estimate_calls,
    roles_needed,
    run_pipeline,
)
from deliverius.llm.roles import ModelRole
from deliverius.shared import InvariantError


def test_the_estimate_is_a_call_per_scenario_and_per_policy_section_by_provider_role() -> None:
    estimate = estimate_calls(DatagenConfig(), ALL_STEPS)

    assert estimate == {ModelRole.DATAGEN: 297 + 48, ModelRole.DATAGEN_HOLDOUT: 55}


def test_the_steps_without_an_llm_estimate_nothing() -> None:
    assert estimate_calls(DatagenConfig(), [Step.WORLD, Step.SCENARIOS]) == {}


def test_the_roles_needed_are_listed_once_each_in_the_order_of_use() -> None:
    assert roles_needed(ALL_STEPS) == (ModelRole.DATAGEN, ModelRole.DATAGEN_HOLDOUT)
    assert roles_needed([Step.HOLDOUT, Step.MESSAGES]) == (
        ModelRole.DATAGEN_HOLDOUT,
        ModelRole.DATAGEN,
    )
    assert roles_needed([Step.WORLD]) == ()


async def test_a_step_that_needs_an_llm_cannot_run_without_access() -> None:
    with pytest.raises(InvariantError) as error:
        await run_pipeline("messages", [Step.MESSAGES], DatagenConfig(), CommitInfo())

    assert error.value.rule == "pipeline.llm_access_missing"

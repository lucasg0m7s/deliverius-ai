"""What each role needs from a model, apart from which model it is (that is configuration)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class ModelRole(StrEnum):
    AGENT = "agent"
    WRITER = "writer"
    CUSTOMER_SIM = "customer_sim"
    JUDGE = "judge"
    DATAGEN = "datagen"
    DATAGEN_HOLDOUT = "datagen_holdout"
    INTENT_BASELINE = "intent_baseline"


@dataclass(frozen=True, slots=True)
class RoleConfig:
    """Fixed in code, not in the environment: it changes together with the prompts and the evals."""

    attempt_timeout_seconds: float
    # Covers every attempt on every provider, so it must exceed one attempt.
    total_deadline_seconds: float
    max_output_tokens: int
    temperature: float


ROLE_CONFIGS: Mapping[ModelRole, RoleConfig] = MappingProxyType(
    {
        ModelRole.AGENT: RoleConfig(
            attempt_timeout_seconds=20,
            total_deadline_seconds=45,
            max_output_tokens=1_024,
            temperature=0.0,
        ),
        ModelRole.WRITER: RoleConfig(
            attempt_timeout_seconds=10,
            total_deadline_seconds=25,
            max_output_tokens=512,
            temperature=0.3,
        ),
        ModelRole.CUSTOMER_SIM: RoleConfig(
            attempt_timeout_seconds=30,
            total_deadline_seconds=90,
            max_output_tokens=1_024,
            temperature=0.9,
        ),
        ModelRole.JUDGE: RoleConfig(
            attempt_timeout_seconds=30,
            total_deadline_seconds=90,
            max_output_tokens=1_024,
            temperature=0.0,
        ),
        # 10 messages of up to 600 characters per call, with room for the model's reasoning.
        ModelRole.DATAGEN: RoleConfig(
            attempt_timeout_seconds=30,
            total_deadline_seconds=90,
            max_output_tokens=2_048,
            temperature=0.9,
        ),
        ModelRole.DATAGEN_HOLDOUT: RoleConfig(
            attempt_timeout_seconds=30,
            total_deadline_seconds=90,
            max_output_tokens=2_048,
            temperature=0.9,
        ),
        # Research only: the "just ask the LLM" baseline of the intent study. One intent and a
        # confidence per call, deterministic so a cached answer is the answer.
        ModelRole.INTENT_BASELINE: RoleConfig(
            attempt_timeout_seconds=10,
            total_deadline_seconds=25,
            max_output_tokens=128,
            temperature=0.0,
        ),
    }
)

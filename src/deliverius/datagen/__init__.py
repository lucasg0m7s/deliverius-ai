"""Synthetic dataset generator: a development tool, never imported by the application."""

from deliverius.datagen.claims import ClaimsDataset, generate_claims
from deliverius.datagen.config import DATAGEN_VERSION, DatagenConfig
from deliverius.datagen.e2e import build_claim_scenario, generate_e2e_scenarios
from deliverius.datagen.errors import BudgetExhaustedError
from deliverius.datagen.generation import ModelsForAttempt, cached_models
from deliverius.datagen.messages import (
    DiscardReason,
    MessageGeneration,
    WrittenMessage,
    generate_holdout_messages,
    generate_messages,
)
from deliverius.datagen.output import (
    DatasetError,
    read_manifest,
    read_records,
    verify_dataset,
    write_dataset,
)
from deliverius.datagen.rag_pairs import PolicyQaGeneration, generate_rag_pairs
from deliverius.datagen.scenarios import (
    MessageScenario,
    MessageSet,
    generate_holdout_scenarios,
    generate_message_scenarios,
)
from deliverius.datagen.splits import (
    ClaimSplit,
    holdout_message_records,
    split_claims_by_time,
    split_message_records,
    split_scenarios,
)
from deliverius.datagen.world import World, generate_world

__all__ = [
    "DATAGEN_VERSION",
    "BudgetExhaustedError",
    "ClaimSplit",
    "ClaimsDataset",
    "DatagenConfig",
    "DatasetError",
    "DiscardReason",
    "MessageGeneration",
    "MessageScenario",
    "MessageSet",
    "ModelsForAttempt",
    "PolicyQaGeneration",
    "World",
    "WrittenMessage",
    "build_claim_scenario",
    "cached_models",
    "generate_claims",
    "generate_e2e_scenarios",
    "generate_holdout_messages",
    "generate_holdout_scenarios",
    "generate_message_scenarios",
    "generate_messages",
    "generate_rag_pairs",
    "generate_world",
    "holdout_message_records",
    "read_manifest",
    "read_records",
    "split_claims_by_time",
    "split_message_records",
    "split_scenarios",
    "verify_dataset",
    "write_dataset",
]

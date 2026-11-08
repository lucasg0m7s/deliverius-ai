"""Which step writes which files, and the manifest that vouches for all of them.

A step is one unit of the generator (the world, the end-to-end scenarios, one set of messages, the
policy pairs). `all` runs every step and writes one dataset; a single step writes a partial one in
the same layout, so the files of a step are named the same wherever they land.
"""

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

import structlog
from pydantic import BaseModel

from deliverius.config import Settings
from deliverius.datagen.attempts import AttemptLog
from deliverius.datagen.budget import BudgetGuard, budget_guard
from deliverius.datagen.claims import generate_claims
from deliverius.datagen.config import DATAGEN_VERSION, DatagenConfig
from deliverius.datagen.e2e import generate_e2e_scenarios
from deliverius.datagen.generation import ModelsForAttempt, PauseWatch, cached_models, pause_watch
from deliverius.datagen.messages import (
    MessageGeneration,
    generate_holdout_messages,
    generate_messages,
    load_message_prompt,
)
from deliverius.datagen.rag_pairs import (
    PolicyQaGeneration,
    generate_rag_pairs,
    load_rag_prompt,
    policy_sections,
    questions_per_section,
)
from deliverius.datagen.records import Intent, Manifest, MessageRecord, Split
from deliverius.datagen.scenarios import MessageSet, generate_message_scenarios
from deliverius.datagen.splits import (
    holdout_message_records,
    split_claims_by_time,
    split_message_records,
)
from deliverius.datagen.world import generate_world
from deliverius.llm.gate import Sleep
from deliverius.llm.prompts import Prompt
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime
from deliverius.shared import InvariantError

logger = structlog.get_logger(__name__)

UNCOMMITTED: Final = "uncommitted"


class Step(StrEnum):
    WORLD = "world"
    SCENARIOS = "scenarios"
    MESSAGES = "messages"
    HOLDOUT = "holdout"
    RAG_PAIRS = "rag-pairs"


# The order of `all`: what needs no LLM first, then the main provider, then the other family.
ALL_STEPS: Final = (Step.WORLD, Step.SCENARIOS, Step.MESSAGES, Step.RAG_PAIRS, Step.HOLDOUT)
STEP_ROLES: Final = {
    Step.MESSAGES: ModelRole.DATAGEN,
    Step.RAG_PAIRS: ModelRole.DATAGEN,
    Step.HOLDOUT: ModelRole.DATAGEN_HOLDOUT,
}


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """Which code wrote the files: `UNCOMMITTED` when there is no commit to name."""

    commit: str = UNCOMMITTED
    dirty: bool = False


@dataclass(frozen=True, slots=True)
class LlmAccess:
    """What the steps that write with an LLM need, in one place so a test can replace it."""

    models_for: Callable[[ModelRole], ModelsForAttempt]
    guard_for: Callable[[ModelRole], BudgetGuard]
    attempts_for: Callable[[ModelRole], AttemptLog]
    pause_for: Callable[[ModelRole], PauseWatch]
    model_names: Mapping[ModelRole, str]


def llm_access(
    runtime: LlmRuntime, settings: Settings, cache: Path, sleep: Sleep = asyncio.sleep
) -> LlmAccess:
    """The production access: models on the response cache, the budget checked before a job.

    The attempt log shares the cache file, and a short pause of the provider is slept through.
    """
    model_names = {
        ModelRole.DATAGEN: str(settings.llm_datagen_model),
        ModelRole.DATAGEN_HOLDOUT: str(settings.llm_datagen_holdout_model),
    }
    return LlmAccess(
        models_for=lambda role: cached_models(runtime, role, cache),
        guard_for=lambda role: budget_guard(runtime, role),
        attempts_for=lambda role: AttemptLog(cache, model_names[role]),
        pause_for=lambda role: pause_watch(runtime, role, sleep),
        model_names=model_names,
    )


@dataclass(frozen=True, slots=True)
class StepOutput:
    files: dict[str, Sequence[BaseModel]]
    counts: dict[str, int] = field(default_factory=dict)
    intent_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    discards: dict[str, int] = field(default_factory=dict)
    prompts: dict[str, Prompt] = field(default_factory=dict)
    roles: tuple[ModelRole, ...] = ()
    fraud_test_cutoff: datetime | None = None
    label_noise_fraction: float | None = None
    fraud_rate: float | None = None


@dataclass(frozen=True, slots=True)
class GeneratedDataset:
    files: dict[str, Sequence[BaseModel]]
    manifest: Manifest

    @property
    def discard_total(self) -> int:
        return sum(self.manifest.discards.values())


def roles_needed(steps: Sequence[Step]) -> tuple[ModelRole, ...]:
    """The roles that call an LLM, once each, in the order the steps first use them."""
    return tuple(dict.fromkeys(STEP_ROLES[step] for step in steps if step in STEP_ROLES))


def estimate_calls(config: DatagenConfig, steps: Sequence[Step]) -> dict[ModelRole, int]:
    """The fewest calls each role makes: a scenario or section is one call, a retry is one more."""
    settings = config.messages
    estimate: Counter[ModelRole] = Counter()
    for step in steps:
        if step is Step.MESSAGES:
            estimate[ModelRole.DATAGEN] += settings.scenarios_per_intent * len(Intent)
        elif step is Step.HOLDOUT:
            estimate[ModelRole.DATAGEN_HOLDOUT] += settings.holdout_scenarios_per_intent * len(
                Intent
            )
        elif step is Step.RAG_PAIRS:
            per_section = questions_per_section(len(policy_sections()), settings.rag_pair_target)
            estimate[ModelRole.DATAGEN] += sum(1 for count in per_section if count > 0)
    return dict(estimate)


async def run_pipeline(
    command: str,
    steps: Sequence[Step],
    config: DatagenConfig,
    commit: CommitInfo,
    access: LlmAccess | None = None,
) -> GeneratedDataset:
    """Runs the steps in order and returns the files with their manifest, ready to be written.

    Raises `BudgetExhaustedError` if the quota ends, with nothing returned.
    """
    if access is None and roles_needed(steps):
        raise InvariantError("pipeline.llm_access_missing")
    logger.info(
        "datagen.run_started",
        command=command,
        seed=config.seed,
        version=DATAGEN_VERSION,
        estimate={role.value: calls for role, calls in estimate_calls(config, steps).items()},
    )
    if access is None:
        outputs = [_OFFLINE_STEPS[step](config) for step in steps]
    else:
        outputs = [await _run_step(step, config, access) for step in steps]
    dataset = _assemble(config, commit, outputs, access)
    logger.info(
        "datagen.run_finished",
        command=command,
        counts=dataset.manifest.counts,
        discarded=dataset.discard_total,
    )
    return dataset


async def _run_step(step: Step, config: DatagenConfig, access: LlmAccess) -> StepOutput:
    offline = _OFFLINE_STEPS.get(step)
    if offline is not None:
        return offline(config)
    return await _llm_step(step, config, access)


async def _llm_step(step: Step, config: DatagenConfig, access: LlmAccess) -> StepOutput:
    role = STEP_ROLES[step]
    models, guard = access.models_for(role), access.guard_for(role)
    attempts, pause = access.attempts_for(role), access.pause_for(role)
    if step is Step.RAG_PAIRS:
        pairs = await generate_rag_pairs(
            models, config, budget_guard=guard, attempt_log=attempts, pause=pause
        )
        return _rag_output(pairs)
    main = generate_message_scenarios(config)
    if step is Step.MESSAGES:
        written = await generate_messages(
            main, models, config, budget_guard=guard, attempt_log=attempts, pause=pause
        )
        return _messages_output(written, config)
    held_out = await generate_holdout_messages(
        config, models, main, budget_guard=guard, attempt_log=attempts, pause=pause
    )
    return _holdout_output(held_out)


def _world_step(config: DatagenConfig) -> StepOutput:
    world = generate_world(config)
    claims = generate_claims(world, config)
    split = split_claims_by_time(claims.claims, config.fraud_test_cutoff)
    return StepOutput(
        files={
            "world/customers.jsonl": world.customer_records(),
            "world/restaurants.jsonl": world.restaurant_records(),
            "world/orders.jsonl": world.order_records(),
            "fraud_history/train.jsonl": split.train,
            "fraud_history/test.jsonl": split.test,
            "fraud_ground_truth/all.jsonl": claims.ground_truth,
        },
        counts={
            "customers": len(world.customers),
            "restaurants": len(world.restaurants),
            "orders": len(world.orders),
            "fraud_history.train": len(split.train),
            "fraud_history.test": len(split.test),
            "fraud_ground_truth": len(claims.ground_truth),
        },
        fraud_test_cutoff=config.fraud_test_cutoff,
        label_noise_fraction=claims.noise_fraction,
        fraud_rate=claims.fraud_rate,
    )


def _e2e_step(config: DatagenConfig) -> StepOutput:
    scenarios = generate_e2e_scenarios(config)
    counts = {"e2e_scenarios": len(scenarios)}
    for flow, total in sorted(Counter(scenario.flow.value for scenario in scenarios).items()):
        counts[f"e2e_scenarios.{flow}"] = total
    return StepOutput(files={"e2e_scenarios/test.jsonl": scenarios}, counts=counts)


_OFFLINE_STEPS: Final[dict[Step, Callable[[DatagenConfig], StepOutput]]] = {
    Step.WORLD: _world_step,
    Step.SCENARIOS: _e2e_step,
}


def _messages_output(generation: MessageGeneration, config: DatagenConfig) -> StepOutput:
    records = split_message_records(generation, config.seed)
    by_split = {split: [r for r in records if r.split is split] for split in Split}
    counts = {
        "messages": len(records),
        "scenarios.messages": len(generation.scenarios),
        "failed_scenarios.messages": len(generation.failed_scenarios),
    }
    intent_counts = {"messages": _by_intent(records)}
    for split, rows in by_split.items():
        counts[f"messages.{split.value}"] = len(rows)
        intent_counts[f"messages.{split.value}"] = _by_intent(rows)
    return StepOutput(
        files={f"messages/{split.value}.jsonl": rows for split, rows in by_split.items()},
        counts=counts,
        intent_counts=intent_counts,
        discards=_message_discards("messages", generation),
        prompts={"messages": load_message_prompt(MessageSet.MAIN)},
        roles=(ModelRole.DATAGEN,),
    )


def _holdout_output(generation: MessageGeneration) -> StepOutput:
    records = holdout_message_records(generation)
    return StepOutput(
        files={"messages_holdout/test.jsonl": records},
        counts={
            "messages_holdout": len(records),
            "scenarios.messages_holdout": len(generation.scenarios),
            "failed_scenarios.messages_holdout": len(generation.failed_scenarios),
        },
        intent_counts={"messages_holdout": _by_intent(records)},
        discards=_message_discards("messages_holdout", generation),
        prompts={"holdout": load_message_prompt(MessageSet.HOLDOUT)},
        roles=(ModelRole.DATAGEN_HOLDOUT,),
    )


def _rag_output(generation: PolicyQaGeneration) -> StepOutput:
    return StepOutput(
        files={"policy_qa/test.jsonl": generation.pairs},
        counts={
            "policy_qa": len(generation.pairs),
            "failed_sections.policy_qa": len(generation.failed_sections),
        },
        discards={f"policy_qa.{reason.value}": n for reason, n in generation.discards.items()},
        prompts={"rag_pairs": load_rag_prompt()},
        roles=(ModelRole.DATAGEN,),
    )


def _by_intent(records: Sequence[MessageRecord]) -> dict[str, int]:
    counted = Counter(record.intent for record in records)
    return {intent.value: counted.get(intent, 0) for intent in Intent}


def _message_discards(prefix: str, generation: MessageGeneration) -> dict[str, int]:
    return {
        f"{prefix}.{reason.value}.{intent.value}": count
        for (reason, intent), count in sorted(generation.discards.items())
    }


def _assemble(
    config: DatagenConfig,
    commit: CommitInfo,
    outputs: Sequence[StepOutput],
    access: LlmAccess | None,
) -> GeneratedDataset:
    files: dict[str, Sequence[BaseModel]] = {}
    manifest = Manifest(
        datagen_version=DATAGEN_VERSION,
        seed=config.seed,
        commit=commit.commit,
        dirty=commit.dirty,
        reference_instant=config.reference_instant,
    )
    for output in outputs:
        files.update(output.files)
        manifest = _merged(manifest, output, access)
    return GeneratedDataset(files, manifest)


def _merged(manifest: Manifest, output: StepOutput, access: LlmAccess | None) -> Manifest:
    models = {role.value: access.model_names[role] for role in output.roles if access}
    return manifest.model_copy(
        update={
            "prompt_versions": {
                **manifest.prompt_versions,
                **{name: f"v{prompt.version}" for name, prompt in output.prompts.items()},
            },
            "prompt_hashes": {
                **manifest.prompt_hashes,
                **{name: prompt.content_hash for name, prompt in output.prompts.items()},
            },
            "models": {**manifest.models, **models},
            "counts": {**manifest.counts, **output.counts},
            "intent_counts": {**manifest.intent_counts, **output.intent_counts},
            "discards": {**manifest.discards, **output.discards},
            "fraud_test_cutoff": output.fraud_test_cutoff or manifest.fraud_test_cutoff,
            "label_noise_fraction": _first_set(
                output.label_noise_fraction, manifest.label_noise_fraction
            ),
            "fraud_rate": _first_set(output.fraud_rate, manifest.fraud_rate),
        }
    )


def _first_set(new: float | None, old: float | None) -> float | None:
    return old if new is None else new

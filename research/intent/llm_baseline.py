"""Approach 4: "just ask the LLM", with no training, on a fixed sample (design D5).

Each masked message goes only in the user turn, inside a `<mensagem>` block with `<`, `>` and `&`
escaped; the system prompt is the versioned `intent_baseline` prompt. The answer is accepted only
through the structured schema: an invalid answer, a refusal or the degraded mode is an empty
prediction (an error), never dropped. Nothing is decided by the answer: it is a measurement.

Every answer is kept in the layer's SQLite response cache, so a run is repeatable and resumable
without spending quota. Latency is measured only on a call the cache did not answer and is kept
per message id, so a resumed run keeps it. When the day's budget is spent the run stops, says how
many messages are left and exits with 2; the same command goes on the next day.

`python -m research.intent.llm_baseline` runs on the validation sample (to check the prompt,
without touching the test sets) and freezes the LLM candidate with its threshold.
"""

import argparse
import asyncio
import html
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import mlflow
import numpy as np
import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import BaseModel, ConfigDict, Field

from deliverius.cli.commit import read_commit_info
from deliverius.cli.console import use_utf8_output
from deliverius.cli.wiring import database_runtime
from deliverius.config import Settings
from deliverius.datagen.budget import budget_guard
from deliverius.datagen.output import read_records
from deliverius.datagen.records import Intent, MessageRecord
from deliverius.llm.cache import CACHE_HIT_MARKER, SqliteResponseCache
from deliverius.llm.call import DegradedMode, DegradedReason, call_structured
from deliverius.llm.pricing import TokenUsage, equivalent_cost_usd_micros
from deliverius.llm.prompts import Prompt, load_prompt
from deliverius.llm.roles import ModelRole
from deliverius.llm.runtime import LlmRuntime, RoleModels
from research.dataset import fetch_dataset
from research.intent.calibration import summarize
from research.intent.candidate import (
    CANDIDATES_DIR,
    Approach,
    CandidateDocument,
    candidate_dir,
    canonical_json,
    write_candidate,
)
from research.intent.config import IntentConfig, load_config
from research.intent.data import INTENTS, LABELS, Corpus, Example, MessageSet, load_synthetic
from research.intent.metrics import (
    EMPTY,
    Predictions,
    coverage_at,
    finite_or_none,
    router_threshold,
)
from research.intent.sample import keyed_rng, stratified_ids
from research.intent.tracking import numeric_metrics, use_experiment

logger = structlog.get_logger(__name__)

PROMPTS_DIR: Final = Path(__file__).with_name("prompts")
PROMPT_NAME: Final = "intent_baseline"
PROMPT_FILE: Final = "prompt.json"
ROLE: Final = ModelRole.INTENT_BASELINE
EXIT_BUDGET_SPENT: Final = 2


class IntentAnswer(BaseModel):
    """The only answer accepted: one of the 11 intents and a confidence between 0 and 1."""

    model_config = ConfigDict(extra="forbid")

    intent: Intent = Field(description="A intenção da mensagem, uma das 11 da lista.")
    confidence: float = Field(ge=0, le=1, description="Confiança na intenção, de 0 a 1.")


def user_turn(text: str) -> str:
    """The customer's message as data: escaped, so it cannot close the block or open a tag."""
    escaped = html.escape(text, quote=False)
    return f"Classifique a mensagem do cliente.\n<mensagem>\n{escaped}\n</mensagem>"


def messages_for(prompt: Prompt, text: str) -> list[tuple[str, str]]:
    return [("system", prompt.text), ("human", user_turn(text))]


class CallObserver(BaseCallbackHandler):
    """Reads, from the model's own result, whether the cache answered and the tokens it used.

    A plain class, not a dataclass: LangChain keeps handlers in a set, so they must be hashable.
    """

    def __init__(self) -> None:
        self.cache_hit = False
        self.usage = TokenUsage(None, None)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                if isinstance(generation, ChatGeneration) and isinstance(
                    generation.message, AIMessage
                ):
                    message = generation.message
                    self.cache_hit = bool(message.response_metadata.get(CACHE_HIT_MARKER))
                    metadata = message.usage_metadata
                    if metadata is not None:
                        self.usage = TokenUsage(metadata["input_tokens"], metadata["output_tokens"])


@dataclass(frozen=True, slots=True)
class Outcome:
    id: str
    label: int
    confidence: float
    failure: DegradedReason | None
    cost_usd_micros: int | None


@dataclass(frozen=True, slots=True)
class BaselineRun:
    outcomes: tuple[Outcome, ...]
    latency_ms: dict[str, float]
    remaining: int

    def predictions(self) -> Predictions:
        return Predictions(
            labels=np.array([o.label for o in self.outcomes], dtype=np.int64),
            confidence=np.array([o.confidence for o in self.outcomes], dtype=np.float64),
        )

    @property
    def failures(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.failure is not None)


class LatencyLog:
    """Latency per message id, in a JSON file rewritten whole after each new measurement."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.values: dict[str, float] = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )

    def record(self, message_id: str, milliseconds: float) -> None:
        self.values[message_id] = milliseconds
        self._path.parent.mkdir(parents=True, exist_ok=True)
        staging = self._path.with_suffix(".tmp")
        staging.write_text(json.dumps(self.values, sort_keys=True), encoding="utf-8")
        staging.replace(self._path)


type BudgetCheck = Callable[[], Awaitable[DegradedReason | None]]


@dataclass(frozen=True, slots=True)
class Pacing:
    """The wait after each call the cache did not answer, outside the timed part.

    The layer's per-minute limiter lives inside the model, so a call would also time its wait in
    the queue. The study builds the model without it and keeps the same rhythm here instead.
    """

    seconds: float
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def paced_settings(settings: Settings) -> tuple[Settings, Pacing]:
    """The settings without the per-minute limiter of the role's provider, and its rhythm."""
    provider = settings.llm_intent_baseline_model.provider
    rpm = settings.limits_for(provider).requests_per_minute
    unthrottled = settings.model_copy(update={f"llm_{provider.value}_rpm": None})
    return unthrottled, Pacing(60 / rpm if rpm else 0.0)


async def classify(
    models: RoleModels, prompt: Prompt, example: Example, latency: LatencyLog
) -> tuple[Outcome, bool]:
    """The outcome, and whether the provider was really called (not the cache)."""
    observer = CallObserver()
    started = time.perf_counter()
    answer = await call_structured(
        models,
        IntentAnswer,
        messages_for(prompt, example.text),
        ROLE,
        {"callbacks": [observer]},
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not observer.cache_hit:
        latency.record(example.id, elapsed_ms)
    cost = equivalent_cost_usd_micros(models.primary_ref, observer.usage)
    if isinstance(answer, DegradedMode):
        # The id and the reason only: the message never goes to a log.
        logger.warning(
            "intent_research.llm_call_failed", message_id=example.id, reason=answer.reason
        )
        return Outcome(example.id, EMPTY, 0.0, answer.reason, cost), not observer.cache_hit
    outcome = Outcome(example.id, INTENTS.index(answer.intent), answer.confidence, None, cost)
    return outcome, not observer.cache_hit


async def run_baseline(
    examples: Sequence[Example],
    models: RoleModels,
    prompt: Prompt,
    latency: LatencyLog,
    check_budget: BudgetCheck,
    *,
    pacing: Pacing | None = None,
) -> BaselineRun:
    """One message at a time, in order. Stops before a call once the budget is spent."""
    outcomes: list[Outcome] = []
    for index, example in enumerate(examples):
        if await check_budget() is not None:
            remaining = len(examples) - index
            logger.warning("intent_research.budget_exhausted", remaining=remaining)
            return BaselineRun(tuple(outcomes), dict(latency.values), remaining)
        outcome, called = await classify(models, prompt, example, latency)
        outcomes.append(outcome)
        if called and pacing is not None and pacing.seconds > 0:
            await pacing.sleep(pacing.seconds)
    return BaselineRun(tuple(outcomes), dict(latency.values), 0)


def cached_models(runtime: LlmRuntime, cache_path: Path) -> RoleModels:
    return runtime.models_for(ROLE, cache=SqliteResponseCache(cache_path, attempt=1))


async def answer_messages(
    config: IntentConfig, settings: Settings, examples: Sequence[Example]
) -> tuple[BaselineRun, str]:
    """The run on Postgres's budget, paced outside the timing; returns it and the model."""
    unthrottled, pacing = paced_settings(settings)
    prompt = load_baseline_prompt(config.llm_baseline.prompt_version)
    async with database_runtime(unthrottled, [ROLE]) as runtime:
        models = cached_models(runtime, config.llm_baseline.cache_path)
        run = await run_baseline(
            examples,
            models,
            prompt,
            LatencyLog(config.llm_baseline.latency_path),
            budget_guard(runtime, ROLE),
            pacing=pacing,
        )
    return run, str(models.primary_ref)


def load_baseline_prompt(version: int) -> Prompt:
    return load_prompt(PROMPTS_DIR, PROMPT_NAME, version)


def prompt_examples(
    records: Sequence[MessageRecord], per_intent: int, seed: int
) -> dict[Intent, tuple[MessageRecord, ...]]:
    """Few-shot examples from training: no injection and no personal data, drawn by the seed."""
    clean = [r for r in records if not r.has_injection and not r.pii_spans]
    chosen: dict[Intent, tuple[MessageRecord, ...]] = {}
    for intent in Intent:
        pool = sorted((r for r in clean if r.intent is intent), key=lambda r: str(r.id))
        rows = keyed_rng(seed, "prompt", intent).choice(len(pool), size=per_intent, replace=False)
        chosen[intent] = tuple(pool[row] for row in sorted(rows))
    return chosen


# --- The validation check: `python -m research.intent.llm_baseline` ---------------------------


def run_summary(run: BaselineRun, messages: MessageSet, config: IntentConfig) -> dict[str, Any]:
    y = messages.subset(o.id for o in run.outcomes).labels
    predictions = run.predictions()
    threshold = router_threshold(
        predictions.confidence, predictions.correct(y), config.router.max_error
    )
    covered = coverage_at(predictions.confidence, predictions.correct(y), threshold)
    costs = [o.cost_usd_micros for o in run.outcomes if o.cost_usd_micros is not None]
    return summarize(predictions, y, config) | {
        "threshold": threshold,
        "coverage_at_threshold": covered.coverage,
        "error_at_threshold": finite_or_none(covered.error),
        "failures": run.failures,
        "cost_usd_per_thousand": sum(costs) / len(costs) / 1000 if costs else None,
    }


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    run: BaselineRun
    sample: MessageSet
    prompt: Prompt
    model: str


async def check_on_validation(config: IntentConfig, settings: Settings) -> ValidationCheck:
    sets = load_synthetic(fetch_dataset(config.dataset))
    validation = sets[Corpus.VALIDATION]
    sample = validation.subset(
        stratified_ids(validation, config.llm_baseline.sample_per_intent, config.seed)
    )
    run, model = await answer_messages(config, settings, sample.examples)
    prompt = load_baseline_prompt(config.llm_baseline.prompt_version)
    return ValidationCheck(run, sample, prompt, model)


def freeze_llm(
    check: ValidationCheck, config: IntentConfig, run_id: str | None, root: Path
) -> CandidateDocument:
    """The LLM candidate: the prompt is its predictor; the threshold comes from validation."""
    summary = run_summary(check.run, check.sample, config)
    commit = read_commit_info()
    return write_candidate(
        candidate_dir(Approach.LLM, root),
        CandidateDocument(
            approach=Approach.LLM,
            labels=LABELS,
            threshold=summary["threshold"],
            params={
                "model": check.model,
                "prompt_name": PROMPT_NAME,
                "prompt_version": check.prompt.version,
                "prompt_hash": check.prompt.content_hash,
                "validation_sample_ids": list(check.sample.ids),
            },
            seed=config.seed,
            code_commit=commit.commit,
            code_dirty=commit.dirty,
            dataset_version=config.dataset.version,
            dataset_revision=config.dataset.revision,
            mlflow_run_id=run_id,
            validation=summary,
        ),
        {PROMPT_FILE: canonical_json({"name": PROMPT_NAME, "text": check.prompt.text})},
    )


def examples_block(chosen: dict[Intent, tuple[MessageRecord, ...]]) -> str:
    """The examples part of a prompt version, ready to paste into a new `v<N>.md`."""
    lines = ["Exemplos (mensagem → intenção):", ""]
    for intent, records in chosen.items():
        lines += [f"- {json.dumps(r.text, ensure_ascii=False)} → {intent.value}" for r in records]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.intent.llm_baseline")
    parser.add_argument("--out", type=Path, default=CANDIDATES_DIR, help="pasta dos candidatos")
    parser.add_argument(
        "--print-examples",
        action="store_true",
        help="só imprime os exemplos do prompt sorteados do treino, sem chamar o LLM",
    )
    args = parser.parse_args(argv)
    use_utf8_output()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    config = load_config()
    if args.print_examples:
        records = read_records(fetch_dataset(config.dataset), "messages/train.jsonl", MessageRecord)
        chosen = prompt_examples(records, config.llm_baseline.examples_per_intent, config.seed)
        sys.stdout.write(examples_block(chosen) + "\n")
        return 0
    check = asyncio.run(check_on_validation(config, Settings()))
    if check.run.remaining:
        sys.stdout.write(
            f"Orçamento do dia esgotado: faltam {check.run.remaining} mensagens. "
            "Rode o mesmo comando amanhã; as respostas já obtidas vêm do cache.\n"
        )
        return EXIT_BUDGET_SPENT
    use_experiment()
    with mlflow.start_run(run_name="validate-llm") as mlflow_run:
        document = freeze_llm(check, config, mlflow_run.info.run_id, args.out)
        mlflow.log_params(
            {"approach": "llm", "model": check.model, "prompt_hash": check.prompt.content_hash}
        )
        mlflow.log_metrics(numeric_metrics("validation", document.validation))
    summary = document.validation
    sys.stdout.write(
        f"LLM na amostra da validação ({len(check.sample)} mensagens): F1 macro "
        f"{summary['macro_f1']:.3f}, falhas {summary['failures']}, limiar {summary['threshold']}.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

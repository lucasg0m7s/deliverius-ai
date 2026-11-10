"""The final evaluation, run once with the four candidates frozen (design D3, D6 and D11).

`python -m research.intent.evaluate` reads the three test sets for the first time: the synthetic
test, the holdout of another generator and the test written by people (validated, hashed and
checked against training and validation). Approaches 1 to 3 score every test message one at a
time, in CPU, with one intra-op thread, after 20 warm-up messages; the LLM answers its fixed
sample (resumable through the cache; a spent budget exits with 2). Every approach is also measured
on that sample, side by side. Then the choice rule picks the winner, and everything goes to one
parent MLflow run, with a child run per approach, and to `evaluation.json`.
"""

import argparse
import asyncio
import logging
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

import mlflow
import numpy as np
import structlog
from matplotlib.figure import Figure

from deliverius.cli.commit import read_commit_info
from deliverius.cli.console import use_utf8_output
from deliverius.config import Settings
from deliverius.retrieval import E5Embedder
from deliverius.retrieval.model_files import MODEL_FILES as E5_FILES
from research.dataset import fetch_dataset
from research.intent import e5, tfidf
from research.intent.candidate import (
    CANDIDATES_DIR,
    Approach,
    CandidateDocument,
    candidate_dir,
    canonical_json,
    read_candidate,
)
from research.intent.choice import Choice, Contender, choose
from research.intent.config import IntentConfig, load_config
from research.intent.data import (
    LABELS,
    SENSITIVE,
    TEST_SETS,
    Corpus,
    MessageSet,
    load_synthetic,
)
from research.intent.export import load_bertimbau
from research.intent.human_test import load_human_test, require_no_contamination
from research.intent.llm_baseline import (
    EXIT_BUDGET_SPENT,
    BaselineRun,
    answer_messages,
)
from research.intent.metrics import (
    Floats,
    Predictions,
    confusion_matrix,
    coverage_at,
    coverage_curve,
    expected_calibration_error,
    f1_interval,
    finite_or_none,
    latency_percentiles,
    recall_by_class,
    recall_of,
    softmax,
)
from research.intent.sample import llm_sample
from research.intent.tracking import numeric_metrics, use_experiment

logger = structlog.get_logger(__name__)

EVALUATION_FILE: Final = "evaluation.json"
SAMPLE: Final = "sample"
ERRORS_KEPT: Final = 40


class Scorer(Protocol):
    def logits(self, texts: Sequence[str]) -> Floats: ...


@dataclass(frozen=True, slots=True)
class Measured:
    """What one approach answered: predictions by message id, latencies, cost and size."""

    approach: str
    threshold: float | None
    by_id: dict[str, tuple[int, float]]
    latency_ms: Floats
    cost_per_thousand_usd: float
    size_bytes: int
    failures: int = 0

    def predictions(self, messages: MessageSet) -> Predictions:
        pairs = [self.by_id[i] for i in messages.ids]
        return Predictions(
            labels=np.array([label for label, _ in pairs], dtype=np.int64),
            confidence=np.array([conf for _, conf in pairs], dtype=np.float64),
        )


def measure_scorer(
    approach: str,
    scorer: Scorer,
    temperature: float,
    threshold: float | None,
    tests: Sequence[MessageSet],
    *,
    warmup: int,
    size_bytes: int,
    clock: Callable[[], float] = time.perf_counter,
) -> Measured:
    """Each test message alone, from masked text to probabilities; the first `warmup` messages
    are scored once more before, out of the count."""
    examples = [example for messages in tests for example in messages.examples]
    for example in examples[:warmup]:
        scorer.logits([example.text])
    by_id: dict[str, tuple[int, float]] = {}
    latency: list[float] = []
    for example in examples:
        started = clock()
        probabilities = softmax(scorer.logits([example.text]), temperature)[0]
        latency.append((clock() - started) * 1000)
        by_id[example.id] = (int(probabilities.argmax()), float(probabilities.max()))
    return Measured(approach, threshold, by_id, np.array(latency), 0.0, size_bytes)


def measure_llm(run: BaselineRun, threshold: float | None) -> Measured:
    costs = [o.cost_usd_micros for o in run.outcomes if o.cost_usd_micros is not None]
    measured_latency = [run.latency_ms[o.id] for o in run.outcomes if o.id in run.latency_ms]
    return Measured(
        approach=Approach.LLM.value,
        threshold=threshold,
        by_id={o.id: (o.label, o.confidence) for o in run.outcomes},
        latency_ms=np.array(measured_latency, dtype=np.float64),
        # Micro-dollars per message, times a thousand messages, in dollars.
        cost_per_thousand_usd=float(np.mean(costs)) / 1000 if costs else 0.0,
        size_bytes=0,
        failures=run.failures,
    )


def set_metrics(
    measured: Measured, messages: MessageSet, config: IntentConfig, *, seed: int
) -> dict[str, Any]:
    predictions = measured.predictions(messages)
    y = messages.labels
    correct = predictions.correct(y)
    covered = coverage_at(predictions.confidence, correct, measured.threshold)
    rule = config.rule
    return {
        "messages": len(messages),
        "macro_f1": f1_interval(
            y,
            predictions.labels,
            len(LABELS),
            resamples=rule.bootstrap_resamples,
            seed=seed,
            level=rule.confidence_level,
        ).as_dict(),
        "sensitive_recall": finite_or_none(recall_of(y, predictions.labels, SENSITIVE)),
        "recall_by_class": dict(
            zip(
                LABELS,
                map(finite_or_none, recall_by_class(y, predictions.labels, len(LABELS))),
                strict=True,
            )
        ),
        "ece": finite_or_none(
            expected_calibration_error(predictions.confidence, correct, config.calibration.ece_bins)
        ),
        "coverage_at_threshold": covered.coverage,
        "error_at_threshold": finite_or_none(covered.error),
        "confusion_matrix": confusion_matrix(y, predictions.labels, len(LABELS)).tolist(),
        "coverage_curve": [
            [p.threshold, p.coverage, p.error]
            for p in coverage_curve(predictions.confidence, correct)
        ],
    }


def combined_sensitive_recall(measured: Measured, tests: Sequence[MessageSet]) -> float:
    """Over every test message the approach answered, the three sets together (rule D2)."""
    answered = [
        (example.intent, measured.by_id[example.id][0])
        for messages in tests
        for example in messages.examples
        if example.id in measured.by_id
    ]
    labels = np.array([LABELS.index(intent.value) for intent, _ in answered], dtype=np.int64)
    predicted = np.array([label for _, label in answered], dtype=np.int64)
    return recall_of(labels, predicted, SENSITIVE)


def answered_part(measured: Measured, messages: MessageSet) -> MessageSet:
    return messages.subset(i for i in messages.ids if i in measured.by_id)


@dataclass(frozen=True, slots=True)
class Evaluation:
    approaches: dict[str, dict[str, Any]]
    choice: Choice
    human_errors: list[dict[str, Any]] = field(default_factory=list)


def evaluate(
    measured: Sequence[Measured],
    tests: Mapping[Corpus, MessageSet],
    sample_ids: Mapping[str, Sequence[str]],
    config: IntentConfig,
) -> Evaluation:
    sets = [tests[corpus] for corpus in TEST_SETS]
    human = tests[Corpus.HUMAN]
    sample = MessageSet(
        Corpus.TEST,
        tuple(
            e
            for messages in sets
            for e in messages.subset(sample_ids[str(messages.corpus)]).examples
        ),
    )
    approaches: dict[str, dict[str, Any]] = {}
    contenders: list[Contender] = []
    for m in measured:
        p50, p95 = latency_percentiles(m.latency_ms)
        per_set = {
            str(messages.corpus): set_metrics(
                m, answered_part(m, messages), config, seed=config.seed
            )
            for messages in sets
            if answered_part(m, messages).examples
        }
        per_set[SAMPLE] = set_metrics(m, sample, config, seed=config.seed)
        recall = combined_sensitive_recall(m, sets)
        approaches[m.approach] = {
            "sets": per_set,
            "sensitive_recall_combined": finite_or_none(recall),
            "latency_ms": {"p50": p50, "p95": p95, "messages": len(m.latency_ms)},
            "cost_per_thousand_usd": m.cost_per_thousand_usd,
            "size_bytes": m.size_bytes,
            "threshold": m.threshold,
            "failures": m.failures,
        }
        contenders.append(
            Contender(m.approach, recall, p95, m.size_bytes, m.predictions(human).labels)
        )
    choice = choose(contenders, human.labels, len(LABELS), config.rule, config.seed)
    errors = _human_errors(measured, choice, human)
    return Evaluation(approaches, choice, errors)


def _human_errors(
    measured: Sequence[Measured], choice: Choice, human: MessageSet
) -> list[dict[str, Any]]:
    """The winner's (or the best F1's) mistakes on the human test, for the report's analysis."""
    name = choice.winner or max(choice.human_f1, key=lambda n: choice.human_f1[n])
    chosen = next(m for m in measured if m.approach == name)
    errors: list[dict[str, Any]] = []
    for example in human.examples:
        label, confidence = chosen.by_id[example.id]
        if label != LABELS.index(example.intent.value):
            errors.append(
                {
                    "id": example.id,
                    "text": example.text,
                    "true": example.intent.value,
                    "predicted": LABELS[label] if label >= 0 else None,
                    "confidence": confidence,
                }
            )
    return sorted(errors, key=lambda e: -e["confidence"])[:ERRORS_KEPT]


def evaluation_document(evaluation: Evaluation, lineage: dict[str, Any]) -> dict[str, Any]:
    choice = evaluation.choice
    return {
        "lineage": lineage,
        "approaches": evaluation.approaches,
        "choice": {
            "winner": choice.winner,
            "best": choice.best,
            "eligible": list(choice.eligible),
            "refused": {
                name: [r.value for r in reasons] for name, reasons in choice.refused.items()
            },
            "human_macro_f1": choice.human_f1,
            "differences_best_minus_other": {
                name: interval.as_dict() for name, interval in choice.differences.items()
            },
            "tied_with_best": list(choice.tied_with_best),
        },
        "human_errors": evaluation.human_errors,
    }


# --- Figures -----------------------------------------------------------------------------------


def plot_confusion(matrix: list[list[int]], title: str, path: Path) -> None:
    values = np.array(matrix)
    figure = Figure(figsize=(8, 7))
    axes = figure.subplots()
    axes.imshow(values, cmap="Blues")
    axes.set_xticks(range(len(LABELS) + 1), [*LABELS, "(vazia)"], rotation=60, ha="right")
    axes.set_yticks(range(len(LABELS)), LABELS)
    for (row, column), count in np.ndenumerate(values):
        if count:
            axes.text(column, row, str(count), ha="center", va="center", fontsize=7)
    axes.set_xlabel("previsto")
    axes.set_ylabel("verdadeiro")
    axes.set_title(title)
    figure.tight_layout()
    figure.savefig(path, dpi=120)


def plot_coverage(approach: dict[str, Any], name: str, path: Path) -> None:
    figure = Figure(figsize=(6, 4))
    axes = figure.subplots()
    for set_name, metrics in approach["sets"].items():
        curve = np.array(metrics["coverage_curve"]) if metrics["coverage_curve"] else None
        if curve is not None:
            axes.plot(curve[:, 1], curve[:, 2], label=set_name)
    axes.set_xlabel("cobertura")
    axes.set_ylabel("erro entre as cobertas")
    axes.set_title(f"{name}: cobertura e erro")
    axes.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=120)


def figures(evaluation: Evaluation, folder: Path) -> list[Path]:
    paths = []
    for name, approach in evaluation.approaches.items():
        if str(Corpus.HUMAN) in approach["sets"]:
            path = folder / f"confusion_{name}_human.png"
            plot_confusion(approach["sets"][str(Corpus.HUMAN)]["confusion_matrix"], name, path)
            paths.append(path)
        path = folder / f"coverage_{name}.png"
        plot_coverage(approach, name, path)
        paths.append(path)
    return paths


# --- The single run: `python -m research.intent.evaluate` ---------------------------------------


def candidate_size(document: CandidateDocument, folder: Path) -> int:
    """The files that would be published; for e5, the pinned embedder too."""
    size = sum((folder / name).stat().st_size for name in document.files)
    if document.approach is Approach.E5:
        size += sum(file.size_bytes for file in E5_FILES)
    return size


def load_scorer(
    document: CandidateDocument, folder: Path, settings: Settings, threads: int
) -> Scorer:
    match document.approach:
        case Approach.TFIDF:
            return tfidf.from_files(folder)
        case Approach.E5:
            return e5.from_files(folder, E5Embedder.load(settings.retrieval_model_dir, threads))
        case Approach.BERTIMBAU:
            return load_bertimbau(folder, int(document.params["max_length"]), threads)
        case Approach.LLM:
            raise ValueError(document.approach)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.intent.evaluate")
    parser.add_argument("--candidates", type=Path, default=CANDIDATES_DIR)
    args = parser.parse_args(argv)
    use_utf8_output()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    config = load_config()
    settings = Settings()
    documents = {a: read_candidate(candidate_dir(a, args.candidates)) for a in Approach}
    synthetic = load_synthetic(fetch_dataset(config.dataset))
    human = load_human_test(config.human_test.path, config.human_test.min_per_intent)
    require_no_contamination(
        human.messages, [synthetic[Corpus.TRAIN], synthetic[Corpus.VALIDATION]]
    )
    tests = {Corpus.TEST: synthetic[Corpus.TEST], Corpus.HOLDOUT: synthetic[Corpus.HOLDOUT]}
    tests[Corpus.HUMAN] = human.messages
    sample_ids = llm_sample(
        [tests[Corpus.TEST], tests[Corpus.HOLDOUT]],
        human.messages,
        config.llm_baseline.sample_per_intent,
        config.seed,
    )
    sample_examples = [
        e for corpus in TEST_SETS for e in tests[corpus].subset(sample_ids[str(corpus)]).examples
    ]
    run, _ = asyncio.run(answer_messages(config, settings, sample_examples))
    if run.remaining:
        sys.stdout.write(
            f"Orçamento do dia esgotado: faltam {run.remaining} mensagens da amostra do LLM. "
            "Rode o mesmo comando amanhã; o que já foi respondido vem do cache.\n"
        )
        return EXIT_BUDGET_SPENT
    threads = config.latency.intra_op_threads
    test_sets = [tests[corpus] for corpus in TEST_SETS]
    measured = [
        measure_scorer(
            approach.value,
            load_scorer(
                documents[approach], candidate_dir(approach, args.candidates), settings, threads
            ),
            documents[approach].temperature or 1.0,
            documents[approach].threshold,
            test_sets,
            warmup=config.latency.warmup_messages,
            size_bytes=candidate_size(
                documents[approach], candidate_dir(approach, args.candidates)
            ),
        )
        for approach in (Approach.TFIDF, Approach.E5, Approach.BERTIMBAU)
    ]
    measured.append(measure_llm(run, documents[Approach.LLM].threshold))
    evaluation = evaluate(measured, tests, sample_ids, config)
    commit = read_commit_info()
    lineage = {
        "code_commit": commit.commit,
        "code_dirty": commit.dirty,
        "dataset_version": config.dataset.version,
        "dataset_revision": config.dataset.revision,
        "human_test_sha256": human.sha256,
        "seed": config.seed,
        "candidates": {a.value: d.mlflow_run_id for a, d in documents.items()},
    }
    document = evaluation_document(evaluation, lineage)
    run_id = record(evaluation, document, args.candidates)
    document["lineage"]["mlflow_run_id"] = run_id
    (args.candidates / EVALUATION_FILE).write_bytes(canonical_json(document))
    logger.info("intent_research.evaluation_finished", winner=evaluation.choice.winner)
    for line in table_lines(evaluation):
        sys.stdout.write(f"{line}\n")
    return 0


def record(evaluation: Evaluation, document: dict[str, Any], folder: Path) -> str:
    use_experiment()
    with mlflow.start_run(run_name="evaluate") as parent:
        mlflow.log_params(
            {
                **{k: str(v) for k, v in document["lineage"].items() if k != "candidates"},
                "winner": str(evaluation.choice.winner),
            }
        )
        for name, approach in evaluation.approaches.items():
            with mlflow.start_run(run_name=f"evaluate-{name}", nested=True):
                mlflow.log_param("approach", name)
                for set_name, metrics in approach["sets"].items():
                    mlflow.log_metrics(
                        numeric_metrics(set_name, metrics)
                        | {f"{set_name}_macro_f1": metrics["macro_f1"]["point"]}
                    )
                mlflow.log_metrics(
                    {
                        "p50_ms": approach["latency_ms"]["p50"],
                        "p95_ms": approach["latency_ms"]["p95"],
                        "size_bytes": float(approach["size_bytes"]),
                        "cost_per_thousand_usd": approach["cost_per_thousand_usd"],
                    }
                )
        with tempfile.TemporaryDirectory() as scratch:
            for path in figures(evaluation, Path(scratch)):
                mlflow.log_artifact(str(path), artifact_path="figures")
            (Path(scratch) / EVALUATION_FILE).write_bytes(canonical_json(document))
            mlflow.log_artifact(str(Path(scratch) / EVALUATION_FILE))
        run_id: str = parent.info.run_id
    return run_id


def table_lines(evaluation: Evaluation) -> list[str]:
    lines = ["Abordagem   | F1 humano [IC 95%]     | recall sensitive | p95 ms   | elegível"]
    for name, approach in evaluation.approaches.items():
        human = approach["sets"].get(str(Corpus.HUMAN), {}).get("macro_f1")
        f1 = f"{human['point']:.3f} [{human['low']:.3f}, {human['high']:.3f}]" if human else "-"
        recall = approach["sensitive_recall_combined"]
        recall_text = "-" if recall is None else f"{recall:.3f}"
        eligible = "sim" if name in evaluation.choice.eligible else "não"
        lines.append(
            f"{name:<11} | {f1:<22} | {recall_text:<16} | "
            f"{approach['latency_ms']['p95']:8.1f} | {eligible}"
        )
    winner = evaluation.choice.winner
    lines.append(f"Vencedor pela regra: {winner}" if winner else "Nenhuma abordagem elegível.")
    return lines


if __name__ == "__main__":
    sys.exit(main())

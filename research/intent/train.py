"""Trains approaches 1 and 2 locally, in CPU, and freezes them as candidates.

`python -m research.intent.train` reads training and validation only (design D3): it fits each
approach, chooses `C`, the temperature and the router threshold on validation, writes the
candidate to `models/intent-candidate/<approach>/` and records an MLflow run in the
`deliverius-intent` experiment. The test sets are not read here.
"""

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mlflow
import structlog

from deliverius.cli.commit import read_commit_info
from deliverius.cli.console import use_utf8_output
from deliverius.config import Settings
from deliverius.datagen.pipeline import CommitInfo
from deliverius.retrieval import E5Embedder
from deliverius.retrieval.model_files import MODEL_REPOSITORY, MODEL_REVISION
from research.dataset import fetch_dataset
from research.intent import e5, tfidf
from research.intent.calibration import Calibrated, calibrate
from research.intent.candidate import (
    CANDIDATE_FILE,
    CANDIDATES_DIR,
    Approach,
    CandidateDocument,
    candidate_dir,
    write_candidate,
)
from research.intent.config import IntentConfig, load_config
from research.intent.data import LABELS, Corpus, MessageSet, load_synthetic
from research.intent.logistic import GridPoint
from research.intent.metrics import Floats
from research.intent.tracking import numeric_metrics, use_experiment


@dataclass(frozen=True, slots=True)
class Trained:
    """What one approach hands to the candidate: logits on validation, files and parameters."""

    validation_logits: Floats
    files: dict[str, bytes]
    params: dict[str, Any]


def train_tfidf_approach(
    train: MessageSet, validation: MessageSet, config: IntentConfig
) -> Trained:
    model, grid = tfidf.train_tfidf(
        train.texts,
        train.labels,
        validation.texts,
        validation.labels,
        section=config.tfidf,
        seed=config.seed,
    )
    params = config.tfidf.model_dump(mode="json") | {
        "C": model.logistic.C,
        "grid": _grid(grid),
        "word_features": len(model.word.vocabulary_),
        "char_features": len(model.char.vocabulary_),
    }
    return Trained(model.logits(validation.texts), tfidf.to_files(model), params)


def e5_trainer(
    embedder: e5.QueryEmbedder,
) -> Callable[[MessageSet, MessageSet, IntentConfig], Trained]:
    def train_e5_approach(
        train: MessageSet, validation: MessageSet, config: IntentConfig
    ) -> Trained:
        model, grid = e5.train_e5(
            embedder,
            train.texts,
            train.labels,
            validation.texts,
            validation.labels,
            section=config.e5,
            seed=config.seed,
        )
        params = config.e5.model_dump(mode="json") | {
            "C": model.logistic.C,
            "grid": _grid(grid),
            "embedder_repository": MODEL_REPOSITORY,
            "embedder_revision": MODEL_REVISION,
        }
        return Trained(model.logits(validation.texts), e5.to_files(model), params)

    return train_e5_approach


def _grid(grid: Sequence[GridPoint]) -> list[dict[str, float]]:
    return [{"C": p.C, "validation_macro_f1": p.validation_macro_f1} for p in grid]


def freeze(
    approach: Approach,
    trained: Trained,
    validation: MessageSet,
    config: IntentConfig,
    *,
    commit: CommitInfo,
    run_id: str | None,
    root: Path,
) -> CandidateDocument:
    calibrated: Calibrated = calibrate(trained.validation_logits, validation.labels, config)
    document = CandidateDocument(
        approach=approach,
        labels=LABELS,
        temperature=calibrated.temperature,
        threshold=calibrated.threshold,
        params=trained.params,
        seed=config.seed,
        code_commit=commit.commit,
        code_dirty=commit.dirty,
        dataset_version=config.dataset.version,
        dataset_revision=config.dataset.revision,
        mlflow_run_id=run_id,
        validation=calibrated.summary,
    )
    return write_candidate(candidate_dir(approach, root), document, trained.files)


def record(document: CandidateDocument, folder: Path) -> None:
    mlflow.log_params(
        {
            "approach": document.approach.value,
            "seed": document.seed,
            "code_commit": document.code_commit,
            "code_dirty": document.code_dirty,
            "dataset_version": document.dataset_version,
            "dataset_revision": document.dataset_revision,
            "C": document.params.get("C"),
        }
    )
    mlflow.log_metrics(numeric_metrics("validation", document.validation))
    mlflow.log_artifact(str(folder / CANDIDATE_FILE), artifact_path="candidate")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.intent.train")
    parser.add_argument("--out", type=Path, default=CANDIDATES_DIR, help="pasta dos candidatos")
    parser.add_argument(
        "--approach",
        choices=[Approach.TFIDF.value, Approach.E5.value],
        nargs="+",
        default=[Approach.TFIDF.value, Approach.E5.value],
    )
    args = parser.parse_args(argv)
    use_utf8_output()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    config = load_config()
    commit = read_commit_info()
    sets = load_synthetic(fetch_dataset(config.dataset))
    train, validation = sets[Corpus.TRAIN], sets[Corpus.VALIDATION]
    use_experiment()
    for name in args.approach:
        approach = Approach(name)
        trainer = (
            train_tfidf_approach
            if approach is Approach.TFIDF
            else e5_trainer(E5Embedder.load(Settings().retrieval_model_dir, 1))
        )
        with mlflow.start_run(run_name=f"train-{approach.value}") as run:
            document = freeze(
                approach,
                trainer(train, validation, config),
                validation,
                config,
                commit=commit,
                run_id=run.info.run_id,
                root=args.out,
            )
            record(document, candidate_dir(approach, args.out))
        summary = document.validation
        sys.stdout.write(
            f"{approach.value}: F1 macro na validação {summary['macro_f1']:.3f}, "
            f"temperatura {document.temperature:.3f}, limiar {document.threshold}. "
            f"Execução {run.info.run_id}.\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

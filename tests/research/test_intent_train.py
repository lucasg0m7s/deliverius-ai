"""Training freezes each approach with what validation chose, and reads no test set."""

from pathlib import Path

import numpy as np
from support.intent_data import learnable_set

from deliverius.datagen.pipeline import CommitInfo
from research.intent import tfidf
from research.intent.candidate import Approach, read_candidate
from research.intent.config import load_config
from research.intent.data import LABELS, Corpus
from research.intent.metrics import softmax
from research.intent.tracking import numeric_metrics
from research.intent.train import freeze, train_tfidf_approach

CONFIG = load_config()
TRAIN = learnable_set(Corpus.TRAIN, 12)
VALIDATION = learnable_set(Corpus.VALIDATION, 4)
COMMIT = CommitInfo(commit="a" * 40, dirty=False)


def test_the_frozen_tfidf_candidate_predicts_what_was_trained(tmp_path: Path) -> None:
    trained = train_tfidf_approach(TRAIN, VALIDATION, CONFIG)

    document = freeze(
        Approach.TFIDF, trained, VALIDATION, CONFIG, commit=COMMIT, run_id="run-1", root=tmp_path
    )

    folder = tmp_path / "tfidf"
    assert read_candidate(folder) == document
    assert document.labels == LABELS
    assert document.mlflow_run_id == "run-1"
    assert document.dataset_revision == CONFIG.dataset.revision
    assert document.validation["macro_f1"] == 1.0
    assert document.temperature is not None
    assert document.params["C"] in CONFIG.tfidf.C_grid
    rebuilt = tfidf.from_files(folder)
    np.testing.assert_allclose(
        softmax(rebuilt.logits(VALIDATION.texts), document.temperature),
        softmax(trained.validation_logits, document.temperature),
        atol=1e-12,
    )


def test_only_numbers_become_mlflow_metrics() -> None:
    assert numeric_metrics("validation", {"f1": 0.9, "threshold": None, "n": 3, "ok": True}) == {
        "validation_f1": 0.9,
        "validation_n": 3.0,
    }

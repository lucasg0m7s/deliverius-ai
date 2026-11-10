"""Approaches 1 and 2: deterministic training, `C` on validation, JSON that predicts the same."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from support.intent_data import KEYWORDS, learnable_set

from research.intent import e5, tfidf
from research.intent.config import load_config
from research.intent.data import Corpus
from research.intent.metrics import Predictions, macro_f1, softmax

CONFIG = load_config()
TRAIN = learnable_set(Corpus.TRAIN, 12)
VALIDATION = learnable_set(Corpus.VALIDATION, 4)


def train_tfidf() -> tfidf.TfidfModel:
    model, _ = tfidf.train_tfidf(
        TRAIN.texts,
        TRAIN.labels,
        VALIDATION.texts,
        VALIDATION.labels,
        section=CONFIG.tfidf,
        seed=CONFIG.seed,
    )
    return model


def test_tfidf_learns_the_keywords_and_reports_the_grid() -> None:
    model, grid = tfidf.train_tfidf(
        TRAIN.texts,
        TRAIN.labels,
        VALIDATION.texts,
        VALIDATION.labels,
        section=CONFIG.tfidf,
        seed=CONFIG.seed,
    )

    predicted = Predictions.from_probabilities(softmax(model.logits(VALIDATION.texts)))
    assert macro_f1(VALIDATION.labels, predicted.labels, 11) == 1.0
    assert [point.C for point in grid] == sorted(CONFIG.tfidf.C_grid)
    assert model.logistic.C == max(grid, key=lambda p: p.validation_macro_f1).C


def test_tfidf_training_is_deterministic() -> None:
    first, second = train_tfidf(), train_tfidf()

    assert np.array_equal(first.logits(VALIDATION.texts), second.logits(VALIDATION.texts))


def test_tfidf_rebuilt_from_json_gives_the_same_logits(tmp_path: Path) -> None:
    model = train_tfidf()
    for name, data in tfidf.to_files(model).items():
        (tmp_path / name).write_bytes(data)

    rebuilt = tfidf.from_files(tmp_path)

    texts = [*VALIDATION.texts, "texto novo com palavra nunca vista [CPF_1]"]
    np.testing.assert_allclose(rebuilt.logits(texts), model.logits(texts), rtol=0, atol=1e-12)


class KeywordEmbedder:
    """One dimension per keyword: a stand-in for e5 with an obvious signal."""

    def __init__(self, keywords: Sequence[str]) -> None:
        self.keywords = keywords
        self.calls = 0

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        self.calls += 1
        return np.array(
            [[float(k in text) for k in self.keywords] for text in texts], dtype=np.float32
        )


def test_e5_trains_on_the_embedder_and_rebuilds_from_json(tmp_path: Path) -> None:
    embedder = KeywordEmbedder(list(KEYWORDS.values()))
    model, grid = e5.train_e5(
        embedder,
        TRAIN.texts,
        TRAIN.labels,
        VALIDATION.texts,
        VALIDATION.labels,
        section=CONFIG.e5,
        seed=CONFIG.seed,
    )
    for name, data in e5.to_files(model).items():
        (tmp_path / name).write_bytes(data)

    rebuilt = e5.from_files(tmp_path, embedder)

    predicted = Predictions.from_probabilities(softmax(model.logits(VALIDATION.texts)))
    assert macro_f1(VALIDATION.labels, predicted.labels, 11) == 1.0
    assert len(grid) == len(CONFIG.e5.C_grid)
    assert embedder.calls >= 2
    np.testing.assert_allclose(
        rebuilt.logits(VALIDATION.texts), model.logits(VALIDATION.texts), rtol=0, atol=1e-12
    )

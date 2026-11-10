"""Approach 1: TF-IDF of words (1-2) and of characters inside words (3-5) + logistic regression.

The two vectorizers and the regression are saved as JSON numbers: vocabulary, IDF and analyzer
parameters in `tfidf.json`, coefficients in `logistic.json`. Rebuilding from them gives the same
logits, so the frozen candidate predicts without any pickle.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
from scipy.sparse import hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from research.intent.candidate import canonical_json
from research.intent.config import TfidfSection
from research.intent.data import LABELS
from research.intent.logistic import (
    GridPoint,
    choose_c,
    fit_logistic,
    logistic_document,
    logistic_from_document,
    logits,
)
from research.intent.metrics import Floats, Ints

TFIDF_FILE: Final = "tfidf.json"
LOGISTIC_FILE: Final = "logistic.json"
# Saved with the vocabulary: what `transform` needs besides it.
_ANALYZER_PARAMS: Final = (
    "analyzer",
    "ngram_range",
    "lowercase",
    "token_pattern",
    "sublinear_tf",
    "smooth_idf",
    "norm",
)


@dataclass(frozen=True, slots=True)
class TfidfModel:
    word: TfidfVectorizer
    char: TfidfVectorizer
    logistic: LogisticRegression

    def features(self, texts: Sequence[str]) -> Any:
        return hstack([self.word.transform(texts), self.char.transform(texts)]).tocsr()

    def logits(self, texts: Sequence[str]) -> Floats:
        return logits(self.logistic, self.features(texts))


def vectorizers(section: TfidfSection) -> tuple[TfidfVectorizer, TfidfVectorizer]:
    word = TfidfVectorizer(
        analyzer="word", ngram_range=section.word_ngrams, min_df=section.min_df, sublinear_tf=True
    )
    char = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=section.char_ngrams,
        min_df=section.min_df,
        sublinear_tf=True,
    )
    return word, char


def train_tfidf(
    train_texts: Sequence[str],
    train_y: Ints,
    validation_texts: Sequence[str],
    validation_y: Ints,
    *,
    section: TfidfSection,
    seed: int,
) -> tuple[TfidfModel, tuple[GridPoint, ...]]:
    """Vectorizers fitted on training only; `C` chosen by validation macro F1."""
    word, char = vectorizers(section)
    x_train = hstack([word.fit_transform(train_texts), char.fit_transform(train_texts)]).tocsr()
    x_validation = hstack([word.transform(validation_texts), char.transform(validation_texts)])
    logistic, grid = choose_c(
        lambda c: fit_logistic(x_train, train_y, c, section.max_iter, seed),
        x_validation.tocsr(),
        validation_y,
        section.C_grid,
        len(LABELS),
    )
    return TfidfModel(word, char, logistic), grid


def _vectorizer_document(vectorizer: TfidfVectorizer) -> dict[str, Any]:
    params = vectorizer.get_params()
    return {
        "params": {name: params[name] for name in _ANALYZER_PARAMS},
        "vocabulary": {term: int(index) for term, index in vectorizer.vocabulary_.items()},
        "idf": vectorizer.idf_.tolist(),
    }


def _vectorizer_from_document(document: dict[str, Any]) -> TfidfVectorizer:
    params = dict(document["params"])
    params["ngram_range"] = tuple(params["ngram_range"])
    vectorizer = TfidfVectorizer(vocabulary=document["vocabulary"], **params)
    vectorizer.idf_ = np.array(document["idf"], dtype=np.float64)
    return vectorizer


def to_files(model: TfidfModel) -> dict[str, bytes]:
    return {
        TFIDF_FILE: canonical_json(
            {"word": _vectorizer_document(model.word), "char": _vectorizer_document(model.char)}
        ),
        LOGISTIC_FILE: canonical_json(logistic_document(model.logistic)),
    }


def from_files(directory: Path) -> TfidfModel:
    tfidf = json.loads((directory / TFIDF_FILE).read_bytes())
    logistic = json.loads((directory / LOGISTIC_FILE).read_bytes())
    return TfidfModel(
        word=_vectorizer_from_document(tfidf["word"]),
        char=_vectorizer_from_document(tfidf["char"]),
        logistic=logistic_from_document(logistic),
    )

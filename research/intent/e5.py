"""Approach 2: the application's e5-small embeddings (`E5Embedder.embed_queries`) + logistic.

The embedder is the pinned ONNX the policy search already uses, so the only new numbers are the
regression's coefficients (`logistic.json`); the manifest points at the e5 revision.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import numpy as np
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression

from research.intent.candidate import canonical_json
from research.intent.config import E5Section
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

LOGISTIC_FILE: Final = "logistic.json"


class QueryEmbedder(Protocol):
    def embed_queries(self, texts: Sequence[str]) -> NDArray[np.float32]: ...


@dataclass(frozen=True, slots=True)
class E5Model:
    embedder: QueryEmbedder
    logistic: LogisticRegression

    def logits(self, texts: Sequence[str]) -> Floats:
        return logits(self.logistic, embed(self.embedder, texts))


def embed(embedder: QueryEmbedder, texts: Sequence[str]) -> Floats:
    """In float64: a float32 fit would keep float32 coefficients, and the JSON copy would then
    compute in another precision than the model it came from."""
    return embedder.embed_queries(texts).astype(np.float64)


def train_e5(
    embedder: QueryEmbedder,
    train_texts: Sequence[str],
    train_y: Ints,
    validation_texts: Sequence[str],
    validation_y: Ints,
    *,
    section: E5Section,
    seed: int,
) -> tuple[E5Model, tuple[GridPoint, ...]]:
    x_train = embed(embedder, train_texts)
    x_validation = embed(embedder, validation_texts)
    logistic, grid = choose_c(
        lambda c: fit_logistic(x_train, train_y, c, section.max_iter, seed),
        x_validation,
        validation_y,
        section.C_grid,
        len(LABELS),
    )
    return E5Model(embedder, logistic), grid


def to_files(model: E5Model) -> dict[str, bytes]:
    return {LOGISTIC_FILE: canonical_json(logistic_document(model.logistic))}


def from_files(directory: Path, embedder: QueryEmbedder) -> E5Model:
    document = json.loads((directory / LOGISTIC_FILE).read_bytes())
    return E5Model(embedder, logistic_from_document(document))

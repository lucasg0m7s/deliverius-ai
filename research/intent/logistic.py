"""Multinomial logistic regression for approaches 1 and 2: fit, `C` chosen on validation, JSON.

The model is kept as numbers in JSON (coefficients, intercepts, classes), never a pickle, and is
rebuilt from them with the same `decision_function`: the logits the temperature scales.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression

from research.intent.metrics import Floats, Ints, macro_f1

type Features = Any  # a dense array or a scipy sparse matrix


@dataclass(frozen=True, slots=True)
class GridPoint:
    C: float  # scikit-learn's name for the inverse regularization strength
    validation_macro_f1: float


def fit_logistic(x: Features, y: Ints, c: float, max_iter: int, seed: int) -> LogisticRegression:
    return LogisticRegression(C=c, max_iter=max_iter, random_state=seed).fit(x, y)


def choose_c(
    fit: Callable[[float], LogisticRegression],
    x_validation: Features,
    y_validation: Ints,
    grid: Sequence[float],
    n_classes: int,
) -> tuple[LogisticRegression, tuple[GridPoint, ...]]:
    """The grid point with the best validation macro F1; a tie keeps the smaller `C` (first)."""
    scored: list[tuple[GridPoint, LogisticRegression]] = []
    for c in sorted(grid):
        model = fit(c)
        predicted = model.predict(x_validation).astype(np.int64)
        scored.append((GridPoint(c, macro_f1(y_validation, predicted, n_classes)), model))
    best = max(scored, key=lambda pair: pair[0].validation_macro_f1)
    return best[1], tuple(point for point, _ in scored)


def logits(model: LogisticRegression, x: Features) -> Floats:
    scores: Floats = np.asarray(model.decision_function(x), dtype=np.float64)
    return scores


def logistic_document(model: LogisticRegression) -> dict[str, Any]:
    return {
        "classes": [int(label) for label in model.classes_],
        "coefficients": model.coef_.tolist(),
        "intercepts": model.intercept_.tolist(),
        "params": {"C": float(model.C), "max_iter": int(model.max_iter)},
    }


def logistic_from_document(document: dict[str, Any]) -> LogisticRegression:
    """The same `decision_function` as the fitted model, from the numbers alone."""
    model = LogisticRegression(C=document["params"]["C"], max_iter=document["params"]["max_iter"])
    model.classes_ = np.array(document["classes"], dtype=np.int64)
    model.coef_ = np.array(document["coefficients"], dtype=np.float64)
    model.intercept_ = np.array(document["intercepts"], dtype=np.float64)
    return model

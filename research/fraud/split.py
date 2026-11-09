"""The split by time: train on the first months, validate on the next one, test on the last.

Inside training, hyperparameters are chosen with expanding-window folds (fit up to month k,
validate on month k + 1), so the validation month is never touched before the model choice.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray

from research.fraud.config import SplitSection

type Mask = NDArray[np.bool_]


@dataclass(frozen=True)
class TimeSplit:
    train: Mask
    validation: Mask
    test: Mask
    # (fit, validate) masks, all inside `train`.
    folds: tuple[tuple[Mask, Mask], ...]


def split_by_month(claimed_at: Sequence[datetime], config: SplitSection) -> TimeSplit:
    months = month_indices(claimed_at, config.year)
    train_months = sorted(config.train_months)
    folds = tuple(
        (np.isin(months, train_months[:k]), months == train_months[k])
        for k in range(1, len(train_months))
    )
    return TimeSplit(
        train=np.isin(months, train_months),
        validation=months == config.validation_month,
        test=months == config.test_month,
        folds=folds,
    )


def month_indices(claimed_at: Sequence[datetime], year: int) -> NDArray[np.int64]:
    """The UTC month of each instant; an instant of another year gets 0 and falls in no set."""
    return np.array(
        [at.astimezone(UTC).month if at.astimezone(UTC).year == year else 0 for at in claimed_at],
        dtype=np.int64,
    )

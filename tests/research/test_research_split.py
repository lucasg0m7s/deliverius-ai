from datetime import UTC, datetime

import numpy as np

from research.fraud.config import load_config
from research.fraud.split import month_indices, split_by_month

CONFIG = load_config().split
INSTANTS = [
    datetime(2026, month, day, hour, tzinfo=UTC)
    for month in range(4, 12)
    for day in (1, 15, 28)
    for hour in (0, 23)
]


def test_the_test_set_starts_on_october_first() -> None:
    split = split_by_month(INSTANTS, CONFIG)

    test = [at for at, inside in zip(INSTANTS, split.test, strict=True) if inside]
    assert test
    assert min(test) >= datetime(2026, 10, 1, tzinfo=UTC)
    assert max(test) < datetime(2026, 11, 1, tzinfo=UTC)


def test_the_sets_do_not_overlap_and_follow_each_other_in_time() -> None:
    split = split_by_month(INSTANTS, CONFIG)
    instants = np.array(INSTANTS)

    assert not (split.train & split.validation).any()
    assert not (split.validation & split.test).any()
    assert not (split.train & split.test).any()
    assert instants[split.train].max() < instants[split.validation].min()
    assert instants[split.validation].max() < instants[split.test].min()


def test_months_outside_the_plan_fall_in_no_set() -> None:
    split = split_by_month(INSTANTS, CONFIG)
    used = split.train | split.validation | split.test

    unused = {at.month for at, inside in zip(INSTANTS, used, strict=True) if not inside}
    assert unused == {4, 11}


def test_three_expanding_folds_each_validate_the_month_after_its_training() -> None:
    split = split_by_month(INSTANTS, CONFIG)
    months = month_indices(INSTANTS, CONFIG.year)

    assert len(split.folds) == 3
    for k, (fit, valid) in enumerate(split.folds, start=1):
        assert set(months[fit]) == set(CONFIG.train_months[:k])
        assert set(months[valid]) == {CONFIG.train_months[k]}
        assert months[valid].min() > months[fit].max()
        assert not (fit & ~split.train).any()
        assert not (valid & ~split.train).any()


def test_the_month_is_taken_in_utc_and_another_year_is_out() -> None:
    # 01:00 UTC on October 1st is still September 30th in Brazil: the split is by UTC month.
    late_september_in_brazil = datetime(2026, 10, 1, 1, tzinfo=UTC)

    assert month_indices([late_september_in_brazil], 2026).tolist() == [10]
    assert month_indices([datetime(2025, 10, 5, tzinfo=UTC)], 2026).tolist() == [0]

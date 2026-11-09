"""The training table: checked dataset, denied claims out, history as the application stores it."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from support.research_data import claim, order_of, write_small_dataset

from deliverius.datagen.output import DatasetError
from deliverius.datagen.records import DecisionKind
from deliverius.fraud.features import FEATURE_NAMES
from deliverius.shared.ids import CustomerId, new_id
from research.fraud import dataset
from research.fraud.dataset import load_claim_table

AT = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def feature(table: dataset.ClaimTable, row: int, name: str) -> float:
    return float(table.features[row, FEATURE_NAMES.index(name)])


def test_a_tampered_dataset_is_refused_before_any_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    customer = CustomerId(new_id())
    order = order_of(customer, AT - timedelta(hours=2))
    folder = write_small_dataset(tmp_path / "data", [claim(order, AT)], [order])
    path = folder / "fraud_history" / "train.jsonl"
    path.write_bytes(path.read_bytes().replace(b"2000", b"9000"))

    def no_features(*_: object) -> None:
        pytest.fail("features computed on an unchecked dataset")

    monkeypatch.setattr(dataset, "compute_features", no_features)

    with pytest.raises(DatasetError) as refused:
        load_claim_table(folder)

    assert (refused.value.rule, refused.value.file) == (
        "dataset.hash_mismatch",
        "fraud_history/train.jsonl",
    )


def test_denied_claims_are_neither_rows_nor_history(tmp_path: Path) -> None:
    customer = CustomerId(new_id())
    order = order_of(customer, AT - timedelta(days=1))
    denied = claim(order, AT - timedelta(hours=5), DecisionKind.DENIED)
    reviewed = claim(order, AT - timedelta(hours=4), DecisionKind.NEEDS_APPROVAL)
    target = claim(order, AT)
    folder = write_small_dataset(tmp_path / "data", [denied, reviewed, target], [order])

    table = load_claim_table(folder)

    assert table.claim_ids == (reviewed.id, target.id)
    assert feature(table, 1, "claims_30d") == 1.0
    assert feature(table, 1, "reviews_90d") == 1.0


def test_a_claim_at_the_same_instant_is_not_in_the_history(tmp_path: Path) -> None:
    customer = CustomerId(new_id())
    first, second = (
        order_of(customer, AT - timedelta(days=2)),
        order_of(customer, AT - timedelta(days=1)),
    )
    twins = [claim(first, AT), claim(second, AT)]
    folder = write_small_dataset(tmp_path / "data", twins, [first, second])

    table = load_claim_table(folder)

    assert len(table) == 2
    assert [feature(table, row, "claims_90d") for row in range(2)] == [0.0, 0.0]
    assert [feature(table, row, "orders_90d") for row in range(2)] == [2.0, 2.0]


def test_the_ground_truth_is_never_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    customer = CustomerId(new_id())
    order = order_of(customer, AT - timedelta(hours=1))
    folder = write_small_dataset(tmp_path / "data", [claim(order, AT, fraud=True)], [order])
    read: list[str] = []
    original = dataset.read_records

    def recording(directory: Path, name: str, model: type) -> list[object]:
        read.append(name)
        return original(directory, name, model)

    monkeypatch.setattr(dataset, "read_records", recording)

    table = load_claim_table(folder)

    assert table.is_fraud.tolist() == [1]
    assert "fraud_ground_truth/all.jsonl" not in read
    assert sorted(read) == sorted(
        [*dataset.CLAIM_FILES, dataset.CUSTOMERS_FILE, dataset.ORDERS_FILE]
    )


def test_the_rows_keep_the_claim_order_and_their_contexts(tmp_path: Path) -> None:
    customer = CustomerId(new_id())
    order = order_of(customer, AT - timedelta(days=3))
    later, earlier = claim(order, AT, cents=500), claim(order, AT - timedelta(days=1), cents=700)
    folder = write_small_dataset(tmp_path / "data", [later, earlier], [order])

    table = load_claim_table(folder)

    assert table.claim_ids == (earlier.id, later.id)
    assert table.requested_cents.tolist() == [700, 500]
    assert [context.claimed_at for context in table.contexts] == list(table.claimed_at)
    subset = table.subset(table.requested_cents > 600)
    assert subset.claim_ids == (earlier.id,)
    assert subset.features.shape == (1, len(FEATURE_NAMES))

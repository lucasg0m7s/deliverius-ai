"""The golden hash: the bytes of a small, fixed world must not change without a new version.

If this fails on the hash, the same seed now writes different files. That is a change of the
dataset: raise `DATAGEN_VERSION`, set `GOLDEN_VERSION` and `GOLDEN_HASH` here to the new values,
and say why in the change. Never edit only the hash.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from deliverius.datagen import (
    DATAGEN_VERSION,
    DatagenConfig,
    generate_claims,
    generate_world,
    split_claims_by_time,
)
from deliverius.datagen.output import render_jsonl

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_VERSION = "1.0.0"
GOLDEN_HASH = "13122105f7b350d8d42f0afff9b4643cbf14ded57ea55386bb405792025567e3"
GOLDEN_CONFIG = DatagenConfig(seed=42, customer_count=50, restaurant_count=8, order_count=300)


def generated_files(config: DatagenConfig) -> dict[str, bytes]:
    world = generate_world(config)
    claims = generate_claims(world, config)
    split = split_claims_by_time(claims.claims, config.fraud_test_cutoff)
    return {
        "customers.jsonl": render_jsonl(world.customer_records()),
        "restaurants.jsonl": render_jsonl(world.restaurant_records()),
        "orders.jsonl": render_jsonl(world.order_records()),
        "fraud_history/train.jsonl": render_jsonl(split.train),
        "fraud_history/test.jsonl": render_jsonl(split.test),
        "ground_truth.jsonl": render_jsonl(claims.ground_truth),
    }


def digest(files: dict[str, bytes]) -> str:
    combined = hashlib.sha256()
    for name in sorted(files):
        combined.update(f"{name}\n{hashlib.sha256(files[name]).hexdigest()}\n".encode())
    return combined.hexdigest()


def test_the_small_world_still_hashes_to_the_golden_value() -> None:
    actual = digest(generated_files(GOLDEN_CONFIG))

    assert GOLDEN_VERSION == DATAGEN_VERSION, (
        f"DATAGEN_VERSION is {DATAGEN_VERSION} but the golden hash was fixed for {GOLDEN_VERSION}: "
        f"update GOLDEN_VERSION and GOLDEN_HASH together (new hash: {actual})"
    )
    assert actual == GOLDEN_HASH, (
        "The generator wrote different bytes for the same seed. If this is intended, raise "
        f"DATAGEN_VERSION in deliverius/datagen/config.py, then set GOLDEN_VERSION and "
        f"GOLDEN_HASH in this test (new hash: {actual})"
    )


def test_two_runs_with_the_same_seed_write_the_same_bytes() -> None:
    assert generated_files(GOLDEN_CONFIG) == generated_files(GOLDEN_CONFIG)


def test_seeds_42_and_43_write_different_datasets() -> None:
    other = GOLDEN_CONFIG.model_copy(update={"seed": 43})

    first, second = generated_files(GOLDEN_CONFIG), generated_files(other)

    assert digest(first) != digest(second)
    for name in ("customers.jsonl", "orders.jsonl", "ground_truth.jsonl"):
        assert first[name] != second[name], name


def test_the_golden_world_exercises_every_file() -> None:
    files = generated_files(GOLDEN_CONFIG)

    assert all(files.values()), [name for name, content in files.items() if not content]


def test_another_process_with_another_hash_seed_writes_the_same_bytes() -> None:
    code = (
        "from tests.unit.datagen.test_datagen_golden import *;"
        "print(digest(generated_files(GOLDEN_CONFIG)))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
        cwd=REPOSITORY_ROOT,
    )

    assert result.stdout.strip().splitlines()[-1] == GOLDEN_HASH

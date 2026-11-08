import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from deliverius.datagen import (
    DatasetError,
    output,
    read_manifest,
    read_records,
    verify_dataset,
    write_dataset,
)
from deliverius.datagen.output import MANIFEST_FILE, render_jsonl, sha256_hex
from deliverius.datagen.records import Manifest, RestaurantRecord

MANIFEST = Manifest(
    datagen_version="1.0.0",
    seed=42,
    commit="abc1234",
    reference_instant=datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
)
ROWS = (
    RestaurantRecord(id=UUID(int=1), name="Pizzaria Açaí", category="Pizzaria"),
    RestaurantRecord(id=UUID(int=2), name="Lanchonete Silva", category="Lanchonete"),
)


def written(folder: Path) -> Path:
    destination = folder / "v1"
    write_dataset(destination, {"restaurants.jsonl": ROWS, "nested/more.jsonl": ROWS[:1]}, MANIFEST)
    return destination


def failing(error: Exception) -> Callable[[Path, Manifest], None]:
    def crash(directory: Path, manifest: Manifest) -> None:
        raise error

    return crash


def test_a_row_is_one_line_of_sorted_compact_json_with_real_accents() -> None:
    content = render_jsonl(ROWS[:1])

    assert content == (
        b'{"category":"Pizzaria","id":"00000000-0000-0000-0000-000000000001",'
        b'"name":"Pizzaria A\xc3\xa7a\xc3\xad"}\n'
    )


def test_no_rows_make_an_empty_file() -> None:
    assert render_jsonl([]) == b""


def test_the_same_rows_always_give_the_same_bytes() -> None:
    assert render_jsonl(ROWS) == render_jsonl(list(ROWS))


def test_a_written_dataset_reads_back_with_its_manifest(tmp_path: Path) -> None:
    destination = written(tmp_path)

    assert read_records(destination, "restaurants.jsonl", RestaurantRecord) == list(ROWS)
    assert read_records(destination, "nested/more.jsonl", RestaurantRecord) == list(ROWS[:1])
    manifest = read_manifest(destination)
    assert manifest.files["restaurants.jsonl"] == sha256_hex(render_jsonl(ROWS))
    assert verify_dataset(destination) == manifest


def test_the_manifest_bytes_do_not_depend_on_the_run(tmp_path: Path) -> None:
    first = written(tmp_path / "a")
    second = written(tmp_path / "b")

    assert (first / MANIFEST_FILE).read_bytes() == (second / MANIFEST_FILE).read_bytes()
    assert json.loads((first / MANIFEST_FILE).read_text(encoding="utf-8"))["seed"] == 42


def test_the_manifest_is_written_after_every_other_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    present_at_manifest_time: list[set[str]] = []
    real_write_manifest = output._write_manifest

    def spy(directory: Path, manifest: Manifest) -> None:
        present_at_manifest_time.append(
            {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
        )
        real_write_manifest(directory, manifest)

    monkeypatch.setattr(output, "_write_manifest", spy)
    written(tmp_path)

    assert present_at_manifest_time == [{"restaurants.jsonl", "nested/more.jsonl"}]


def test_a_changed_file_is_refused_on_read_naming_the_file(tmp_path: Path) -> None:
    destination = written(tmp_path)
    (destination / "restaurants.jsonl").write_bytes(b'{"changed":true}\n')

    with pytest.raises(DatasetError, match=r"restaurants\.jsonl") as raised:
        read_records(destination, "restaurants.jsonl", RestaurantRecord)
    assert raised.value.rule == "dataset.hash_mismatch"
    assert raised.value.file == "restaurants.jsonl"
    with pytest.raises(DatasetError, match=r"restaurants\.jsonl"):
        verify_dataset(destination)


def test_an_untouched_file_still_reads_when_another_one_was_changed(tmp_path: Path) -> None:
    destination = written(tmp_path)
    (destination / "restaurants.jsonl").write_bytes(b"x\n")

    assert read_records(destination, "nested/more.jsonl", RestaurantRecord) == list(ROWS[:1])


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    destination = written(tmp_path)
    (destination / "restaurants.jsonl").unlink()

    with pytest.raises(DatasetError, match=r"dataset\.file_missing: restaurants\.jsonl"):
        verify_dataset(destination)


def test_a_file_the_manifest_does_not_list_is_refused(tmp_path: Path) -> None:
    destination = written(tmp_path)
    (destination / "extra.jsonl").write_bytes(b"")

    with pytest.raises(DatasetError, match=r"dataset\.file_not_in_manifest: extra\.jsonl"):
        read_records(destination, "extra.jsonl", RestaurantRecord)


def test_a_folder_without_manifest_is_not_a_dataset(tmp_path: Path) -> None:
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / "restaurants.jsonl").write_bytes(render_jsonl(ROWS))

    with pytest.raises(DatasetError, match=r"dataset\.manifest_missing"):
        read_records(tmp_path / "half", "restaurants.jsonl", RestaurantRecord)


def test_a_broken_manifest_is_refused(tmp_path: Path) -> None:
    destination = written(tmp_path)
    (destination / MANIFEST_FILE).write_text("{not json", encoding="utf-8")

    with pytest.raises(DatasetError, match=r"dataset\.manifest_invalid"):
        read_manifest(destination)


def test_an_existing_folder_is_refused_without_overwrite_and_left_alone(tmp_path: Path) -> None:
    destination = written(tmp_path)
    before = (destination / MANIFEST_FILE).read_bytes()

    with pytest.raises(DatasetError, match=r"dataset\.exists"):
        write_dataset(destination, {"other.jsonl": ROWS}, MANIFEST)

    assert (destination / MANIFEST_FILE).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v1"]


def test_overwrite_replaces_the_folder_whole(tmp_path: Path) -> None:
    destination = written(tmp_path)

    write_dataset(destination, {"other.jsonl": ROWS}, MANIFEST, overwrite=True)

    assert sorted(read_manifest(destination).files) == ["other.jsonl"]
    assert not (destination / "restaurants.jsonl").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v1"]


def test_a_folder_that_appears_during_the_write_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "v1"
    real_write_manifest = output._write_manifest

    def rival_appears(directory: Path, manifest: Manifest) -> None:
        real_write_manifest(directory, manifest)
        destination.mkdir()

    monkeypatch.setattr(output, "_write_manifest", rival_appears)

    with pytest.raises(DatasetError, match=r"dataset\.exists"):
        write_dataset(destination, {"restaurants.jsonl": ROWS}, MANIFEST)

    assert list(destination.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v1"]


def test_a_crash_before_the_manifest_leaves_no_dataset_and_no_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output, "_write_manifest", failing(OSError("disk went away")))

    with pytest.raises(OSError, match="disk went away"):
        written(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_a_crash_while_overwriting_keeps_the_previous_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = written(tmp_path)
    before = verify_dataset(destination)
    monkeypatch.setattr(output, "_write_manifest", failing(OSError("boom")))

    with pytest.raises(OSError, match="boom"):
        write_dataset(destination, {"other.jsonl": ROWS}, MANIFEST, overwrite=True)

    assert verify_dataset(destination) == before


def test_a_failed_swap_puts_the_previous_dataset_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = written(tmp_path)
    before = verify_dataset(destination)
    real_rename = Path.rename
    swap_failed = OSError("swap failed")

    def rename(self: Path, target: Path) -> Path:
        if self.name.endswith(".tmp"):
            raise swap_failed
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)

    with pytest.raises(OSError, match="swap failed"):
        write_dataset(destination, {"other.jsonl": ROWS}, MANIFEST, overwrite=True)

    assert verify_dataset(destination) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v1"]


@pytest.mark.parametrize(
    "name", ["", "/etc/passwd", "../outside.jsonl", "a/../../b.jsonl", MANIFEST_FILE]
)
def test_file_names_that_leave_the_folder_or_clash_with_the_manifest_are_refused(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(DatasetError, match=r"dataset\.invalid_file_name"):
        write_dataset(tmp_path / "v1", {name: ROWS}, MANIFEST)

    assert list(tmp_path.iterdir()) == []

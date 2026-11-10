"""A candidate folder is all or nothing, and a changed file is refused."""

from pathlib import Path

import pytest

from research.intent.candidate import (
    CANDIDATE_FILE,
    Approach,
    CandidateDocument,
    CandidateError,
    candidate_dir,
    read_candidate,
    write_candidate,
)

DOCUMENT = CandidateDocument(
    approach=Approach.TFIDF,
    labels=("a", "b"),
    temperature=1.3,
    threshold=0.8,
    params={"C": 3.0},
    seed=1,
    code_commit="c" * 40,
    code_dirty=False,
    dataset_version="1.0.0",
    dataset_revision="d" * 40,
    validation={"macro_f1": 0.9},
)
FILES = {"logistic.json": b'{"x": 1}\n', "tfidf.json": b'{"y": 2}\n'}


def test_the_folder_holds_the_files_and_their_hashes(tmp_path: Path) -> None:
    folder = candidate_dir(Approach.TFIDF, tmp_path)

    written = write_candidate(folder, DOCUMENT, FILES)

    assert folder == tmp_path / "tfidf"
    assert sorted(p.name for p in folder.iterdir()) == [CANDIDATE_FILE, *sorted(FILES)]
    assert read_candidate(folder) == written
    assert set(written.files) == set(FILES)


def test_writing_again_replaces_the_candidate(tmp_path: Path) -> None:
    folder = tmp_path / "tfidf"
    write_candidate(folder, DOCUMENT, {**FILES, "old.json": b"{}"})

    write_candidate(folder, DOCUMENT, FILES)

    assert not (folder / "old.json").exists()


def test_a_crash_before_the_rename_leaves_no_partial_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(self: Path, target: Path) -> Path:
        raise OSError(28, "disk gone")

    monkeypatch.setattr(Path, "rename", crash)

    with pytest.raises(OSError, match="disk gone"):
        write_candidate(tmp_path / "tfidf", DOCUMENT, FILES)

    assert list(tmp_path.iterdir()) == []


def test_a_changed_file_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "tfidf"
    write_candidate(folder, DOCUMENT, FILES)
    (folder / "logistic.json").write_bytes(b'{"x": 2}\n')

    with pytest.raises(CandidateError) as refused:
        read_candidate(folder)

    assert refused.value.rule == "candidate.hash_mismatch"


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "tfidf"
    write_candidate(folder, DOCUMENT, FILES)
    (folder / "tfidf.json").unlink()

    with pytest.raises(CandidateError) as refused:
        read_candidate(folder)

    assert refused.value.rule == "candidate.file_missing"

"""The commit in the manifest: the real one, `uncommitted` if git cannot say, and the dirty mark."""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from deliverius.cli import commit as commit_module
from deliverius.cli.commit import read_commit_info, run_git
from deliverius.datagen.pipeline import UNCOMMITTED, CommitInfo

GIT_EXECUTABLE = shutil.which("git") or "git"


def fake_git(answers: dict[str, str | None]):  # type: ignore[no-untyped-def]
    def git(arguments: Sequence[str]) -> str | None:
        return answers.get(arguments[0])

    return git


def test_a_clean_tree_at_a_commit_is_that_commit_without_the_dirty_mark() -> None:
    info = read_commit_info(fake_git({"rev-parse": "abc123\n", "status": ""}))

    assert info == CommitInfo("abc123", dirty=False)


def test_changes_in_the_tree_mark_the_manifest_dirty() -> None:
    info = read_commit_info(fake_git({"rev-parse": "abc123\n", "status": " M file.py\n"}))

    assert info == CommitInfo("abc123", dirty=True)


def test_a_repository_without_a_commit_is_uncommitted_and_still_dirty_if_it_has_files() -> None:
    info = read_commit_info(fake_git({"rev-parse": None, "status": "?? new.py\n"}))

    assert info == CommitInfo(UNCOMMITTED, dirty=True)


def test_when_git_fails_altogether_the_commit_is_uncommitted_and_not_dirty() -> None:
    assert read_commit_info(fake_git({})) == CommitInfo(UNCOMMITTED, dirty=False)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_real_git_answers_for_a_repository_with_a_commit_and_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commit_module, "_PACKAGE_DIRECTORY", tmp_path)
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]

    def git(*arguments: str) -> None:
        subprocess.run(  # noqa: S603 - fixed arguments in a throwaway repository
            [GIT_EXECUTABLE, *identity, *arguments], cwd=tmp_path, check=True, capture_output=True
        )

    git("init", "-q")
    assert run_git(["rev-parse", "HEAD"]) is None
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first")

    clean = read_commit_info()
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    dirty = read_commit_info()

    assert len(clean.commit) == 40
    assert not clean.dirty
    assert dirty == CommitInfo(clean.commit, dirty=True)


def test_git_missing_from_the_machine_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commit_module.shutil, "which", lambda name: None)

    assert run_git(["status"]) is None


def test_git_taking_too_long_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])  # type: ignore[arg-type]

    monkeypatch.setattr(commit_module.subprocess, "run", slow)

    assert run_git(["status"]) is None
    assert read_commit_info() == CommitInfo(UNCOMMITTED, dirty=False)


def test_the_call_to_git_has_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="x")

    monkeypatch.setattr(commit_module.subprocess, "run", spy)

    assert run_git(["status"]) == "x"
    assert seen["timeout"] == commit_module.GIT_TIMEOUT_SECONDS

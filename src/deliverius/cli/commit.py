"""Which commit the code is at, for the manifest: asked to git, never assumed."""

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from deliverius.datagen.pipeline import UNCOMMITTED, CommitInfo

GIT_TIMEOUT_SECONDS: Final = 5.0
# The code that writes the files is the code of this package, wherever the command is run from.
_PACKAGE_DIRECTORY: Final = Path(__file__).resolve().parent

# Runs `git <arguments>` and gives its output, or `None` if git is missing, fails or is too slow.
type GitRunner = Callable[[Sequence[str]], str | None]


def run_git(arguments: Sequence[str]) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed arguments, the executable comes from PATH
            [git, *arguments],
            cwd=_PACKAGE_DIRECTORY,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def read_commit_info(git: GitRunner = run_git) -> CommitInfo:
    """The commit of `HEAD` (`UNCOMMITTED` if there is none) and whether the tree has changes."""
    head = (git(["rev-parse", "HEAD"]) or "").strip()
    status = (git(["status", "--porcelain"]) or "").strip()
    return CommitInfo(commit=head or UNCOMMITTED, dirty=bool(status))

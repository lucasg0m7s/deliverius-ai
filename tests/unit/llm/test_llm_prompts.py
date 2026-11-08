import hashlib
import re
from pathlib import Path

import pytest

from deliverius.llm.errors import PromptError, PromptNotFoundError
from deliverius.llm.prompts import load_prompt
from deliverius.llm.roles import ModelRole

VALID = """---
version: 1
role: writer
changed: first version
---
Você é o Deli, o atendente do Deliverius.
"""

PROMPT_TEXT = "Você é o Deli, o atendente do Deliverius."
HEADER_ERROR = "prompt.invalid_header_line"


def write_prompt(directory: Path, name: str, version: int, content: str) -> Path:
    path = directory / name / f"v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return path


def test_loading_returns_text_version_role_and_hash(tmp_path: Path) -> None:
    path = write_prompt(tmp_path, "compose_reply", 1, VALID)

    prompt = load_prompt(tmp_path, "compose_reply", 1)

    assert prompt.name == "compose_reply"
    assert prompt.version == 1
    assert prompt.role is ModelRole.WRITER
    assert prompt.changed == "first version"
    assert prompt.text == PROMPT_TEXT
    assert prompt.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_header_never_reaches_the_returned_text(tmp_path: Path) -> None:
    write_prompt(tmp_path, "compose_reply", 1, VALID)

    text = load_prompt(tmp_path, "compose_reply", 1).text

    for fragment in ("---", "version:", "role:", "changed:", "first version"):
        assert fragment not in text


def test_the_text_may_hold_its_own_dividers(tmp_path: Path) -> None:
    content = VALID + "\n---\nSecond block with a colon: kept.\n"
    write_prompt(tmp_path, "compose_reply", 1, content)

    text = load_prompt(tmp_path, "compose_reply", 1).text

    assert text.endswith("---\nSecond block with a colon: kept.")


def test_windows_line_endings_load_the_same_way(tmp_path: Path) -> None:
    write_prompt(tmp_path, "compose_reply", 1, VALID.replace("\n", "\r\n"))

    prompt = load_prompt(tmp_path, "compose_reply", 1)

    assert prompt.text == PROMPT_TEXT
    assert prompt.role is ModelRole.WRITER


def test_the_changed_note_may_contain_colons(tmp_path: Path) -> None:
    write_prompt(tmp_path, "p", 1, VALID.replace("first version", "fix: tone, again"))

    assert load_prompt(tmp_path, "p", 1).changed == "fix: tone, again"


def test_any_edit_changes_the_hash(tmp_path: Path) -> None:
    write_prompt(tmp_path, "a", 1, VALID)
    write_prompt(tmp_path, "b", 1, VALID.replace("Deli", "Deli!"))
    write_prompt(tmp_path, "c", 1, VALID.replace("changed: first version", "changed: other"))

    hashes = {load_prompt(tmp_path, name, 1).content_hash for name in "abc"}

    assert len(hashes) == 3


def test_a_requested_version_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    write_prompt(tmp_path, "compose_reply", 1, VALID)

    with pytest.raises(PromptNotFoundError, match=r"prompt.not_found.*v2.md"):
        load_prompt(tmp_path, "compose_reply", 2)


def test_an_unknown_prompt_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PromptNotFoundError, match=r"prompt.not_found.*missing_prompt"):
        load_prompt(tmp_path, "missing_prompt", 1)


@pytest.mark.parametrize(
    "name", ["..", "../outside", "a/b", "a\\b", "", ".hidden", "Upper", "with space", "a.b"]
)
def test_a_name_that_could_leave_the_directory_is_refused(tmp_path: Path, name: str) -> None:
    prompts = tmp_path / "prompts"
    write_prompt(tmp_path, "outside", 1, VALID)
    prompts.mkdir()

    with pytest.raises(PromptError, match=r"prompt.invalid_name"):
        load_prompt(prompts, name, 1)


@pytest.mark.parametrize("version", [0, -1, True])
def test_a_version_that_is_not_a_positive_integer_is_refused(tmp_path: Path, version: int) -> None:
    with pytest.raises(PromptError, match=r"prompt.invalid_version"):
        load_prompt(tmp_path, "compose_reply", version)


BODY = "Text\n"
HEADER_VARIANTS = [
    ("no-header", "Just text, no header.\n", "prompt.no_header"),
    ("empty-file", "", "prompt.no_header"),
    ("unclosed", "---\nversion: 1\nrole: writer\nchanged: x\nText.\n", "prompt.header_not_closed"),
    ("no-version", "---\nrole: writer\nchanged: x\n---\n" + BODY, "prompt.header_missing_key"),
    ("no-role", "---\nversion: 1\nchanged: x\n---\n" + BODY, "prompt.header_missing_key"),
    ("no-changed", "---\nversion: 1\nrole: writer\n---\n" + BODY, "prompt.header_missing_key"),
    (
        "version-mismatch",
        "---\nversion: 2\nrole: writer\nchanged: x\n---\n" + BODY,
        "prompt.version_mismatch",
    ),
    (
        "version-text",
        "---\nversion: one\nrole: writer\nchanged: x\n---\n" + BODY,
        "prompt.version_mismatch",
    ),
    (
        "unknown-role",
        "---\nversion: 1\nrole: poet\nchanged: x\n---\n" + BODY,
        "prompt.unknown_role",
    ),
    (
        "unknown-key",
        "---\nversion: 1\nrole: writer\nchanged: x\nauthor: me\n---\n" + BODY,
        HEADER_ERROR,
    ),
    (
        "duplicate-key",
        "---\nversion: 1\nversion: 1\nrole: writer\nchanged: x\n---\n" + BODY,
        HEADER_ERROR,
    ),
    ("no-colon", "---\nversion: 1\nrole writer\nchanged: x\n---\n" + BODY, HEADER_ERROR),
    ("empty-value", "---\nversion: 1\nrole: writer\nchanged:\n---\n" + BODY, HEADER_ERROR),
    ("empty-body", "---\nversion: 1\nrole: writer\nchanged: x\n---\n   \n", "prompt.empty_text"),
]


@pytest.mark.parametrize(
    ("content", "reason"),
    [(content, reason) for _, content, reason in HEADER_VARIANTS],
    ids=[name for name, _, _ in HEADER_VARIANTS],
)
def test_an_invalid_file_is_refused_citing_the_file(
    tmp_path: Path, content: str, reason: str
) -> None:
    path = write_prompt(tmp_path, "compose_reply", 1, content)

    with pytest.raises(PromptError, match=re.escape(reason)) as error:
        load_prompt(tmp_path, "compose_reply", 1)

    assert str(path) in str(error.value)


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "compose_reply" / "v1.md"
    path.parent.mkdir()
    path.write_bytes(b"---\nversion: 1\n\xff\xfe\n---\nText\n")

    with pytest.raises(PromptError, match=r"prompt.not_utf8"):
        load_prompt(tmp_path, "compose_reply", 1)


def test_blank_lines_inside_the_header_are_ignored(tmp_path: Path) -> None:
    content = VALID.replace("version: 1\n", "version: 1\n\n")
    write_prompt(tmp_path, "compose_reply", 1, content)

    assert load_prompt(tmp_path, "compose_reply", 1).text == PROMPT_TEXT

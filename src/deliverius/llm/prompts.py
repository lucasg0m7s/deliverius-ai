"""Versioned prompt files: `<directory>/<name>/v<N>.md`, with a header the model never sees.

```
---
version: 2
role: writer
changed: what changed since the previous version
---
The prompt text, in Portuguese.
```

A new prompt is a new file, never an edit of an old one, so a stored result always points at the
exact text that produced it.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from deliverius.llm.errors import PromptError, PromptNotFoundError
from deliverius.llm.roles import ModelRole

HEADER_FENCE = "---"
HEADER_KEYS = frozenset({"version", "role", "changed"})
NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: int
    role: ModelRole
    changed: str
    text: str
    # SHA-256 of the whole file, header included, so any edit changes it.
    content_hash: str


def load_prompt(directory: Path, name: str, version: int) -> Prompt:
    """Raises `PromptNotFoundError` for a missing version and `PromptError` for anything invalid."""
    _require_valid_request(name, version)
    path = directory / name / f"v{version}.md"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise PromptNotFoundError("prompt.not_found", str(path)) from error
    return _parse(raw, path, name, version)


def _require_valid_request(name: str, version: int) -> None:
    # The name becomes a path segment, so only a plain identifier may pass: no `..` or separators.
    if NAME_PATTERN.fullmatch(name) is None:
        raise PromptError("prompt.invalid_name", repr(name))
    if type(version) is not int or version < 1:
        raise PromptError("prompt.invalid_version", repr(version))


def _parse(raw: bytes, path: Path, name: str, version: int) -> Prompt:
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PromptError("prompt.not_utf8", str(path)) from error
    lines = content.splitlines()
    if not lines or lines[0].strip() != HEADER_FENCE:
        raise PromptError("prompt.no_header", str(path))
    try:
        closing = next(
            i for i, line in enumerate(lines[1:], start=1) if line.strip() == HEADER_FENCE
        )
    except StopIteration:
        raise PromptError("prompt.header_not_closed", str(path)) from None
    header = _parse_header(lines[1:closing], path)
    text = "\n".join(lines[closing + 1 :]).strip()
    if not text:
        raise PromptError("prompt.empty_text", str(path))
    if header["version"] != str(version):
        raise PromptError(
            "prompt.version_mismatch", str(path), f"header says {header['version']!r}"
        )
    try:
        role = ModelRole(header["role"])
    except ValueError as error:
        raise PromptError("prompt.unknown_role", str(path), repr(header["role"])) from error
    return Prompt(
        name=name,
        version=version,
        role=role,
        changed=header["changed"],
        text=text,
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def _parse_header(lines: list[str], path: Path) -> dict[str, str]:
    header: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator or not value or key not in HEADER_KEYS or key in header:
            raise PromptError("prompt.invalid_header_line", str(path), repr(line))
        header[key] = value
    missing = HEADER_KEYS - header.keys()
    if missing:
        raise PromptError("prompt.header_missing_key", str(path), ", ".join(sorted(missing)))
    return header

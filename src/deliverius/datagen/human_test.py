"""The test set written by people: its format, and the checks a file must pass before it is used.

The file is data from outside, so it is read as bytes with a size cap and checked row by row. A
report names the line and the rule that failed, never the content of the line: a row that holds
personal data must not be echoed to a terminal or a log.
"""

import csv
import io
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import structlog

from deliverius.datagen.records import Intent
from deliverius.privacy import find_personal_data, normalize_text
from deliverius.shared import DeliveriusError

logger = structlog.get_logger(__name__)

MAX_FILE_BYTES: Final = 1_000_000
MAX_ROWS: Final = 2_000
MAX_TEXT_CHARS: Final = 600
REQUIRED_COLUMNS: Final = ("id", "text", "intent")
OPTIONAL_COLUMNS: Final = ("notes",)
# Rows of `exemplo.csv` carry this prefix, so an example that slips into the real file is caught.
EXAMPLE_ID_PREFIX: Final = "exemplo-"


class HumanTestFileError(DeliveriusError):
    """The file cannot be read as a test set at all: nothing row by row can be said about it."""

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


class RowProblem(StrEnum):
    WRONG_COLUMN_COUNT = "wrong_column_count"
    EMPTY_ID = "empty_id"
    DUPLICATE_ID = "duplicate_id"
    EXAMPLE_ROW = "example_row"
    EMPTY_TEXT = "empty_text"
    TEXT_TOO_LONG = "text_too_long"
    UNKNOWN_INTENT = "unknown_intent"
    PERSONAL_DATA = "personal_data"


@dataclass(frozen=True, slots=True)
class RowIssue:
    """`line` is the line of the file where the row starts; `detail` never holds file content."""

    line: int
    problem: RowProblem
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HumanTestReport:
    rows: int
    issues: tuple[RowIssue, ...]
    intent_counts: dict[str, int]

    @property
    def is_valid(self) -> bool:
        return not self.issues

    @property
    def invalid_rows(self) -> int:
        return len({issue.line for issue in self.issues})


def validate_human_test(path: Path) -> HumanTestReport:
    """Checks the file at `path`. Raises `HumanTestFileError` if it cannot be read as a table."""
    rows = _read_rows(path)
    columns = _column_positions(rows[0][1])
    body = [(line, row) for line, row in rows[1:] if row]
    if len(body) > MAX_ROWS:
        raise HumanTestFileError("human_test.too_many_rows")
    issues: list[RowIssue] = []
    seen_ids: set[str] = set()
    intents: Counter[str] = Counter()
    for line, row in body:
        row_issues = _check_row(line, row, columns, seen_ids)
        issues += row_issues
        if not row_issues:
            intents[row[columns["intent"]].strip()] += 1
    report = HumanTestReport(len(body), tuple(issues), dict(intents))
    logger.info("datagen.human_test_validated", rows=report.rows, invalid_rows=report.invalid_rows)
    return report


def _read_rows(path: Path) -> list[tuple[int, list[str]]]:
    """Every record with the line it starts on; the header comes first."""
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise HumanTestFileError("human_test.unreadable") from error
    if len(content) > MAX_FILE_BYTES:
        raise HumanTestFileError("human_test.file_too_large")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HumanTestFileError("human_test.not_utf8") from error
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[tuple[int, list[str]]] = []
    line = 1
    try:
        for row in reader:
            rows.append((line, row))
            line = reader.line_num + 1
    except csv.Error as error:
        raise HumanTestFileError("human_test.malformed_csv") from error
    if not rows:
        raise HumanTestFileError("human_test.empty_file")
    return rows


def _column_positions(header: list[str]) -> dict[str, int]:
    names = [name.strip() for name in header]
    allowed = {*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS}
    if len(set(names)) != len(names) or not set(REQUIRED_COLUMNS) <= set(names) <= allowed:
        raise HumanTestFileError("human_test.invalid_header")
    return {name: names.index(name) for name in names}


def _check_row(
    line: int, row: list[str], columns: dict[str, int], seen_ids: set[str]
) -> list[RowIssue]:
    if len(row) != len(columns):
        return [RowIssue(line, RowProblem.WRONG_COLUMN_COUNT)]
    issues: list[RowIssue] = []
    identifier = row[columns["id"]].strip()
    text = row[columns["text"]].strip()
    intent = row[columns["intent"]].strip()
    notes = row[columns["notes"]] if "notes" in columns else ""
    issues += _id_problems(line, identifier, seen_ids)
    issues += _text_problems(line, text)
    if intent not in {member.value for member in Intent}:
        issues.append(RowIssue(line, RowProblem.UNKNOWN_INTENT))
    kinds = _personal_data_kinds(text, notes)
    if kinds:
        issues.append(RowIssue(line, RowProblem.PERSONAL_DATA, ", ".join(kinds)))
    return issues


def _id_problems(line: int, identifier: str, seen_ids: set[str]) -> list[RowIssue]:
    if not identifier:
        return [RowIssue(line, RowProblem.EMPTY_ID)]
    problems: list[RowIssue] = []
    if identifier.casefold().startswith(EXAMPLE_ID_PREFIX):
        problems.append(RowIssue(line, RowProblem.EXAMPLE_ROW))
    if identifier in seen_ids:
        problems.append(RowIssue(line, RowProblem.DUPLICATE_ID))
    seen_ids.add(identifier)
    return problems


def _text_problems(line: int, text: str) -> list[RowIssue]:
    if not text:
        return [RowIssue(line, RowProblem.EMPTY_TEXT)]
    if len(text) > MAX_TEXT_CHARS:
        return [RowIssue(line, RowProblem.TEXT_TOO_LONG)]
    return []


def _personal_data_kinds(*fields: str) -> list[str]:
    """The kinds of personal data found, sorted, once each: names of kinds, never values."""
    found = {
        match.kind.value for field in fields for match in find_personal_data(normalize_text(field))
    }
    return sorted(found)

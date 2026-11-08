"""The twelve policy documents, shipped as package data, and the filling of their placeholders.

The documents never write a number: `{{auto_approval_limit}}` is replaced by the value the engine
uses, so what the customer reads and what the code decides cannot drift apart.
"""

import re
from dataclasses import dataclass, fields
from datetime import timedelta
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import assert_never

from deliverius.policy.limits import PolicyLimits
from deliverius.policy.models import PolicyDocumentId
from deliverius.shared.errors import DeliveriusError
from deliverius.shared.money import Money

# Linear pattern, run only over files from the repository.
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_TITLE = re.compile(r"# (POL-\d{2}) · (.+)")
_SECTION = re.compile(r"^## \S", re.MULTILINE)
_HOUR = 3_600
_DAY = 86_400
_LARGEST_DURATION_IN_HOURS = 3 * _DAY

type LimitValue = Money | int | timedelta


class PolicyDocumentError(DeliveriusError):
    """A policy document is malformed or names a placeholder that is not a limit: a repo bug."""

    def __init__(self, rule: str, document: str, placeholder: str | None = None) -> None:
        detail = (
            f"{rule}: {document}" if placeholder is None else f"{rule}: {document} {placeholder}"
        )
        super().__init__(detail)
        self.rule = rule
        self.document = document
        self.placeholder = placeholder


@dataclass(frozen=True, slots=True)
class PolicyDocument:
    id: PolicyDocumentId
    title: str
    # Raw text, placeholders included: show it only through `render_policy_document`.
    markdown: str


def load_policy_documents(directory: Traversable | None = None) -> tuple[PolicyDocument, ...]:
    """Exactly `POL-01` to `POL-12`, each with a title that matches its file name and a section.

    `directory` defaults to the documents shipped in the package.
    """
    directory = directory or files("deliverius.policy") / "docs"
    documents = sorted(
        (_parse(entry) for entry in directory.iterdir() if entry.name.endswith(".md")),
        key=lambda document: document.id,
    )
    ids = [document.id for document in documents]
    if ids != list(PolicyDocumentId):
        raise PolicyDocumentError("policy_document.set_mismatch", ", ".join(ids))
    return tuple(documents)


def render_policy_document(document: PolicyDocument, limits: PolicyLimits) -> str:
    values = _limit_values(limits)

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise PolicyDocumentError("policy_document.unknown_placeholder", document.id, name)
        return format_limit(values[name])

    return _PLACEHOLDER.sub(fill, document.markdown)


def format_limit(value: LimitValue) -> str:
    """As the customer reads it: `R$ 50,00`, `50%`, `24 horas`."""
    match value:
        case Money():
            return value.format_brl()
        case timedelta():
            return _format_duration(value)
        case int():
            return f"{value}%"
        case unreachable:  # pragma: no cover - mypy proves every type is handled
            assert_never(unreachable)


def _format_duration(duration: timedelta) -> str:
    # Claim windows read better in hours ("48 horas"), long ones in days ("90 dias").
    seconds = int(duration.total_seconds())
    if seconds % _HOUR:
        return _count(seconds // 60, "minuto")
    if seconds < _LARGEST_DURATION_IN_HOURS or seconds % _DAY:
        return _count(seconds // _HOUR, "hora")
    return _count(seconds // _DAY, "dia")


def _count(amount: int, unit: str) -> str:
    return f"{amount} {unit}" if amount == 1 else f"{amount} {unit}s"


def _limit_values(limits: PolicyLimits) -> dict[str, LimitValue]:
    return {limit.name: getattr(limits, limit.name) for limit in fields(limits)}


def _parse(entry: Traversable) -> PolicyDocument:
    markdown = entry.read_text(encoding="utf-8")
    title = _TITLE.fullmatch(markdown.split("\n", 1)[0])
    if title is None or title.group(1) not in PolicyDocumentId:
        raise PolicyDocumentError("policy_document.bad_title", entry.name)
    document_id = PolicyDocumentId(title.group(1))
    if not entry.name.startswith(f"{document_id}-"):
        raise PolicyDocumentError("policy_document.file_name_mismatch", entry.name)
    if _SECTION.search(markdown) is None:
        raise PolicyDocumentError("policy_document.no_section", entry.name)
    return PolicyDocument(id=document_id, title=title.group(2), markdown=markdown)

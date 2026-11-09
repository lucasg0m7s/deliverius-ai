"""Policy documents split into one chunk per `##` section, ready to be indexed.

A chunk starts with the title of its document and the title of its section, so both help the search
and the embedding find it. Its ID is citable: `POL-03#prazo`. The hash is of the *rendered*
document plus the revision of the embedding model: changing a number in `limits.py` re-indexes
only the documents that show it, and changing the model re-indexes everything.
"""

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from deliverius.policy import (
    POLICY_LIMITS,
    PolicyDocument,
    PolicyLimits,
    load_policy_documents,
    render_policy_document,
)
from deliverius.retrieval.model_files import MODEL_REVISION
from deliverius.shared.errors import DeliveriusError

_SECTION_MARK: Final = "## "
_NOT_ALPHANUMERIC: Final = re.compile(r"[^a-z0-9]+")


class ChunkingError(DeliveriusError):
    """A document has two sections with the same slug, or a section title with no letters."""

    def __init__(self, rule: str, document: str, section: str) -> None:
        super().__init__(f"{rule}: {document} {section}")
        self.rule = rule
        self.document = document
        self.section = section


@dataclass(frozen=True, slots=True)
class PolicyChunk:
    chunk_id: str
    document_id: str
    section: str
    # Order of the section in the document, from 1.
    position: int
    content: str


@dataclass(frozen=True, slots=True)
class ChunkedDocument:
    document_id: str
    document_hash: str
    chunks: tuple[PolicyChunk, ...]


def chunk_policy_documents(
    documents: Iterable[PolicyDocument] | None = None, limits: PolicyLimits = POLICY_LIMITS
) -> tuple[ChunkedDocument, ...]:
    """Every document in order; `documents` defaults to the ones shipped in the package."""
    return tuple(
        chunk_document(document, limits)
        for document in (documents if documents is not None else load_policy_documents())
    )


def chunk_document(document: PolicyDocument, limits: PolicyLimits) -> ChunkedDocument:
    document_id = str(document.id)
    rendered = render_policy_document(document, limits)
    chunks: list[PolicyChunk] = []
    seen: set[str] = set()
    for position, (section, body) in enumerate(_sections_of(rendered), start=1):
        slug = section_slug(section)
        if not slug:
            raise ChunkingError("chunking.empty_slug", document_id, section)
        if slug in seen:
            raise ChunkingError("chunking.duplicate_section", document_id, section)
        seen.add(slug)
        chunks.append(
            PolicyChunk(
                chunk_id=f"{document_id}#{slug}",
                document_id=document_id,
                section=section,
                position=position,
                content=f"{document.title} · {section}\n\n{body}",
            )
        )
    document_hash = _hash(rendered)
    return ChunkedDocument(document_id, document_hash, tuple(chunks))


def _hash(rendered: str) -> str:
    return hashlib.sha256("\n".join((MODEL_REVISION, rendered)).encode("utf-8")).hexdigest()


def section_slug(title: str) -> str:
    """Lowercase, no accents, words joined by hyphens: `Quando se aplica` -> `quando-se-aplica`."""
    decomposed = unicodedata.normalize("NFKD", title.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NOT_ALPHANUMERIC.sub("-", unaccented).strip("-")


def _sections_of(rendered: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    for line in rendered.splitlines():
        if line.startswith(_SECTION_MARK):
            sections.append((line.removeprefix(_SECTION_MARK).strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return [(title, "\n".join(body).strip()) for title, body in sections]

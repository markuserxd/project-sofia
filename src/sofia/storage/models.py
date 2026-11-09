"""Typed, bounded inputs and results for the trusted memory controller."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Protocol

MAX_TEXT = 16_384
MAX_RESULTS = 100
MAX_RECORDS = 512
MAX_BYTES = 4 * 1024 * 1024
MAX_LINKS = 8
MAX_EXPORT = 25
RETENTION_OPTIONS = (None, 30, 365)


class MemoryError(Exception):
    """Fixed diagnostic codes without raw input, database errors or paths."""


class Kind(StrEnum):
    DIARY = "diary"
    FACT = "fact"
    TASK = "task"


class Status(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class Fact:
    subject: str
    predicate: str
    object: str
    exclusive: bool = False


@dataclass(frozen=True, slots=True)
class Draft:
    kind: Kind
    title: str
    text: str = ""
    source: str = "User entry"
    fact: Fact | None = None
    related_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    draft: Draft
    created_at: str
    updated_at: str
    content_version: int
    content_hash: str
    status: Status
    expires_at: str | None
    superseded_by: str | None


@dataclass(frozen=True, slots=True)
class Search:
    text: str = ""
    kind: Kind | None = None
    status: Status | None = Status.CURRENT
    since: date | None = None
    through: date | None = None
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    limit: int = 25
    offset: int = 0


@dataclass(frozen=True, slots=True)
class MemoryHit:
    memory_id: str
    kind: Kind
    title: str
    source: str
    status: Status
    content_version: int
    snippet: str
    available: bool


@dataclass(frozen=True, slots=True)
class MemorySettings:
    enabled: bool = False
    retention_days: int | None = None
    records: int = 0
    bytes_used: int = 0


@dataclass(frozen=True, slots=True)
class Recovery:
    unavailable: int = 0
    pending: int = 0


class MemoryRetriever(Protocol):
    def search(self, query: Search = Search()) -> tuple[MemoryHit, ...]: ...


class MemoryStore(MemoryRetriever, Protocol):
    def save(self, draft: Draft) -> MemoryRecord: ...
    def get(self, memory_id: str) -> MemoryRecord: ...
    def update(
        self,
        memory_id: str,
        draft: Draft,
        *,
        version: int,
        reviewed_hash: str | None = None,
    ) -> MemoryRecord: ...
    def forget(self, memory_id: str, *, version: int) -> None: ...
    def export(self, memory_ids: tuple[str, ...], destination: Path) -> None: ...


_ID = re.compile(r"[a-f0-9]{32}")
_CREDENTIAL = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"\b(?:sk-(?:ant-|proj-)?|gh[pousr]_|github_pat_|AKIA)[\w-]{12,}|"
    r"\bBearer\s+[\w.+/=-]{8,}|"
    r"\b(?:pass(?:word|phrase)|api[ _-]?key|(?:access[ _-]?)?token|"
    r"client[ _-]?secret|authorization)\s*[=:]\s*\S+|"
    r"[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@",
    re.IGNORECASE,
)


def require_id(value: str) -> None:
    if type(value) is not str or not _ID.fullmatch(value):
        raise MemoryError("invalid_id")


def clean(
    value: str, bound: int, *, multiline: bool = False, empty: bool = False
) -> str:
    if type(value) is not str or len(value) > bound:
        raise MemoryError("invalid_text")
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n"))
    if not multiline:
        value = value.strip()
    if (not value.strip() and not empty) or any(
        unicodedata.category(char) in {"Cc", "Cs"}
        and not (multiline and char in "\n\t")
        for char in value
    ):
        raise MemoryError("invalid_text")
    if len(value.encode("utf-8")) > bound:
        raise MemoryError("invalid_text")
    return value


def searchable(value: str) -> str:
    return unicodedata.normalize("NFC", value.casefold())


def check_sensitive(*values: str) -> None:
    if _CREDENTIAL.search("\n".join(values)):
        raise MemoryError("sensitive_content")


def validate(draft: Draft) -> Draft:
    if not isinstance(draft, Draft) or not isinstance(draft.kind, Kind):
        raise MemoryError("invalid_draft")
    title = clean(draft.title, 240)
    source = clean(draft.source, 512)
    if type(draft.related_ids) is not tuple or len(draft.related_ids) > MAX_LINKS:
        raise MemoryError("invalid_links")
    for memory_id in draft.related_ids:
        require_id(memory_id)
    related = tuple(sorted(set(draft.related_ids)))
    if draft.kind is Kind.FACT:
        fact = draft.fact
        if (
            not isinstance(fact, Fact)
            or type(fact.exclusive) is not bool
            or draft.text != ""
        ):
            raise MemoryError("invalid_fact")
        fact = Fact(
            clean(fact.subject, 256),
            clean(fact.predicate, 128),
            clean(fact.object, 512),
            fact.exclusive,
        )
        check_sensitive(
            title, source, f"{fact.subject}\n{fact.predicate}: {fact.object}"
        )
        return Draft(draft.kind, title, source=source, fact=fact, related_ids=related)
    if draft.fact is not None:
        raise MemoryError("invalid_draft")
    body = clean(draft.text, MAX_TEXT, multiline=True)
    check_sensitive(title, source, body, f"{title}: {body}")
    return Draft(draft.kind, title, body, source, related_ids=related)

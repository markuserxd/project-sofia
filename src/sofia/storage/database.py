"""Transactional SQLite schema and connection setup."""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from sofia.storage.models import MemoryError

SCHEMA_VERSION = 2
MAX_DB_BYTES = 128 * 1024 * 1024

_MIGRATIONS = (
    (
        """CREATE TABLE settings (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            retention_days INTEGER CHECK (retention_days IN (30, 365))
        )""",
        "INSERT INTO settings VALUES (1, 0, NULL)",
        """CREATE TABLE records (
            row_id INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK (kind IN ('diary', 'task', 'fact')),
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            body TEXT,
            subject TEXT,
            predicate TEXT,
            object TEXT,
            exclusive INTEGER NOT NULL CHECK (exclusive IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT,
            version INTEGER NOT NULL CHECK (version >= 1),
            content_hash TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size > 0),
            status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current', 'superseded')),
            superseded_by TEXT REFERENCES records(memory_id) ON DELETE SET NULL,
            CHECK ((kind = 'task') = (body IS NOT NULL)),
            CHECK ((kind = 'fact') = (subject IS NOT NULL)),
            CHECK ((kind = 'fact') = (predicate IS NOT NULL)),
            CHECK ((kind = 'fact') = (object IS NOT NULL))
        )""",
        """CREATE UNIQUE INDEX current_fact ON records(subject, predicate, object)
            WHERE kind = 'fact' AND status = 'current'""",
        """CREATE TABLE links (
            memory_id TEXT NOT NULL REFERENCES records(memory_id) ON DELETE CASCADE,
            related_id TEXT NOT NULL REFERENCES records(memory_id) ON DELETE CASCADE,
            PRIMARY KEY (memory_id, related_id), CHECK (memory_id <> related_id)
        )""",
        """CREATE TABLE cleanup (
            memory_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (memory_id, version)
        )""",
    ),
    ("CREATE VIRTUAL TABLE memory_fts USING fts5(title, body, tokenize='trigram')",),
)


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def configure(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("PRAGMA synchronous = FULL")
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    max_pages = MAX_DB_BYTES // page_size
    if (
        connection.execute(f"PRAGMA max_page_count = {max_pages}").fetchone()[0]
        > max_pages
    ):
        raise MemoryError("quota_exceeded")
    if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise MemoryError("unsupported_journal")


def migrate(connection: sqlite3.Connection) -> bool:
    """Run inside the same transaction as the first index rebuild."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= SCHEMA_VERSION:
        raise MemoryError("unsupported_schema")
    if (
        version == 0
        and connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    ):
        raise MemoryError("unsupported_schema")
    try:
        for step in _MIGRATIONS[version:]:
            for statement in step:
                connection.execute(statement)
        connection.execute("SELECT rowid FROM memory_fts LIMIT 0")
    except sqlite3.OperationalError as error:
        if "no such module: fts5" in str(error) or "no such tokenizer" in str(error):
            raise MemoryError("fts_unavailable") from None
        raise
    if version != SCHEMA_VERSION:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return version != SCHEMA_VERSION

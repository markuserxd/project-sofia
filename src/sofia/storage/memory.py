"""Trusted application entry point for explicit, local memory operations."""

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sofia.storage import diary
from sofia.storage.database import configure, migrate, transaction
from sofia.storage.models import (
    MAX_BYTES,
    MAX_EXPORT,
    MAX_LINKS,
    MAX_RECORDS,
    MAX_RESULTS,
    RETENTION_OPTIONS,
    Draft,
    Fact,
    Kind,
    MemoryError,
    MemoryHit,
    MemoryRecord,
    MemorySettings,
    Recovery,
    Search,
    Status,
    clean,
    require_id,
    searchable,
    validate,
)
from sofia.tools.workspace_paths import Directory, WorkspaceError, relative_parts

_FILE_ERRORS = (OSError, WorkspaceError, MemoryError)
_MAX_DIARY_FILES = MAX_RECORDS * 4
_FIELDS = (
    "title, source, body, subject, predicate, object, exclusive, updated_at, "
    "expires_at, version, content_hash, byte_size"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _expiry(created: str, days: int | None) -> str | None:
    return (
        _stamp(datetime.fromisoformat(created) + timedelta(days=days)) if days else None
    )


def _body(draft: Draft) -> str:
    fact = draft.fact
    return f"{fact.subject}\n{fact.predicate}\n{fact.object}" if fact else draft.text


class MemoryManager:
    """Only the trusted UI/application owns this capability. Construction is inert.

    All public calls are synchronous and use a fresh, short-lived connection.
    SQLite's writer lock serializes cooperating processes and diary operations.
    """

    def __init__(
        self, root: Path | None = None, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self.root = root if root is not None else diary.default_root()
        self._clock = clock

    @contextmanager
    def _session(
        self, *, create: bool = False
    ) -> Iterator[tuple[sqlite3.Connection, Directory | None] | None]:
        try:
            with ExitStack() as stack:
                try:
                    root = stack.enter_context(
                        diary.open_directory(self.root, create=create)
                    )
                except FileNotFoundError:
                    if create:
                        raise
                    yield None
                    return
                if not diary.exists(root, diary.DATABASE):
                    if not create:
                        yield None
                        return
                    try:
                        os.close(root.create(diary.DATABASE))
                    except FileExistsError:
                        diary.exists(root, diary.DATABASE)
                for suffix in ("-journal", "-wal", "-shm"):
                    diary.exists(root, diary.DATABASE + suffix)
                files = None
                try:
                    files = stack.enter_context(diary.open_diary(root, create=create))
                except FileNotFoundError:
                    if create:
                        raise
                db = stack.enter_context(
                    closing(
                        sqlite3.connect(
                            (self.root / diary.DATABASE).as_uri() + "?mode=rw",
                            uri=True,
                            timeout=0.75,
                            isolation_level=None,
                        )
                    )
                )
                configure(db)
                with transaction(db):
                    if migrate(db):
                        self._rebuild(db, files, _stamp(self._clock()))
                yield db, files
        except WorkspaceError:
            raise MemoryError("unsafe_path") from None
        except (OSError, sqlite3.Error):
            raise MemoryError("storage_unavailable") from None

    @staticmethod
    def _required(
        session: tuple[sqlite3.Connection, Directory | None] | None,
    ) -> tuple[sqlite3.Connection, Directory | None]:
        if session is None:
            raise MemoryError("persistence_disabled")
        return session

    @staticmethod
    def _settings(db: sqlite3.Connection) -> MemorySettings:
        enabled, days = db.execute(
            "SELECT enabled, retention_days FROM settings"
        ).fetchone()
        count, size = db.execute(
            "SELECT count(*), coalesce(sum(byte_size), 0) FROM records"
        ).fetchone()
        return MemorySettings(bool(enabled), days, count, size)

    def settings(self) -> MemorySettings:
        with self._session() as session:
            return self._settings(session[0]) if session else MemorySettings()

    def set_policy(
        self, *, enabled: bool, retention_days: int | None = None
    ) -> Recovery:
        """Commit the user's policy choice, then retry physical diary cleanup."""
        if (
            type(enabled) is not bool
            or (retention_days is not None and type(retention_days) is not int)
            or retention_days not in RETENTION_OPTIONS
        ):
            raise MemoryError("invalid_policy")
        now = _stamp(self._clock())
        with self._session(create=enabled or retention_days is not None) as session:
            if session is None:
                return Recovery()
            db, files = session
            with transaction(db):
                self._expire(db, now)
                db.execute(
                    "UPDATE settings SET enabled=?, retention_days=?",
                    (enabled, retention_days),
                )
                for row in self._rows(db):
                    db.execute(
                        "UPDATE records SET expires_at=? WHERE row_id=?",
                        (
                            _expiry(row["created_at"], retention_days),
                            row["row_id"],
                        ),
                    )
                self._expire(db, now)
            return self._recover(db, files, now, rebuild=False)

    @staticmethod
    def _rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
        rows = db.execute(
            "SELECT * FROM records LIMIT ?", (MAX_RECORDS + 1,)
        ).fetchall()
        if len(rows) > MAX_RECORDS:
            raise MemoryError("quota_exceeded")
        return rows

    @staticmethod
    def _find(db: sqlite3.Connection, memory_id: str, now: str) -> sqlite3.Row:
        require_id(memory_id)
        row = db.execute(
            "SELECT * FROM records WHERE memory_id=? AND (expires_at IS NULL OR expires_at>?)",
            (memory_id, now),
        ).fetchone()
        if row is None:
            raise MemoryError("not_found")
        return row

    @staticmethod
    def _version(row: sqlite3.Row, version: int, *, current: bool = True) -> None:
        if (
            type(version) is not int
            or version != row["version"]
            or (current and row["status"] != Status.CURRENT)
        ):
            raise MemoryError("version_conflict")

    def _record(
        self,
        db: sqlite3.Connection,
        files: Directory | None,
        row: sqlite3.Row,
        *,
        review: bool = False,
    ) -> MemoryRecord:
        kind = Kind(row["kind"])
        ids = db.execute(
            "SELECT related_id FROM links WHERE memory_id=? ORDER BY related_id LIMIT ?",
            (row["memory_id"], MAX_LINKS),
        )
        fact = (
            Fact(
                row["subject"], row["predicate"], row["object"], bool(row["exclusive"])
            )
            if kind is Kind.FACT
            else None
        )
        draft = Draft(
            kind,
            row["title"],
            row["body"] or "",
            row["source"],
            fact,
            tuple(item[0] for item in ids),
        )
        content_hash = row["content_hash"]
        if kind is Kind.DIARY:
            if files is None:
                raise MemoryError("diary_missing")
            raw = diary.read(files, diary.name_for(row["memory_id"], row["version"]))
            actual_hash = diary.digest(raw)
            if not review and actual_hash != content_hash:
                raise MemoryError("diary_changed")
            prefix = diary.header(
                row["memory_id"],
                draft,
                row["created_at"],
                row["updated_at"],
                row["version"],
            )
            raw = raw.replace(b"\r\n", b"\n")
            if not raw.startswith(prefix):
                raise MemoryError("diary_invalid")
            try:
                text = raw[len(prefix) :].decode("utf-8").removesuffix("\n")
                draft = validate(replace(draft, text=text))
            except UnicodeError:
                raise MemoryError("diary_invalid") from None
            content_hash = actual_hash
        return MemoryRecord(
            row["memory_id"],
            draft,
            row["created_at"],
            row["updated_at"],
            row["version"],
            content_hash,
            Status(row["status"]),
            row["expires_at"],
            row["superseded_by"],
        )

    def get(self, memory_id: str) -> MemoryRecord:
        return self._inspect(memory_id, review=False)

    def review_diary(self, memory_id: str) -> MemoryRecord:
        """Review an external body edit before explicitly adopting its hash."""
        return self._inspect(memory_id, review=True)

    def _inspect(self, memory_id: str, *, review: bool) -> MemoryRecord:
        require_id(memory_id)
        with self._session() as session:
            db, files = self._required(session)
            with transaction(db):
                row = self._find(db, memory_id, _stamp(self._clock()))
                if row["kind"] == Kind.DIARY and not review and files is not None:
                    diary.publish(files, memory_id, row["version"], row["content_hash"])
                return self._record(db, files, row, review=review)

    @staticmethod
    def _index(db: sqlite3.Connection, row_id: int, draft: Draft) -> None:
        db.execute(
            "INSERT INTO memory_fts(rowid,title,body) VALUES (?,?,?)",
            (
                row_id,
                searchable(draft.title),
                searchable(_body(draft)),
            ),
        )

    def _rebuild(
        self, db: sqlite3.Connection, files: Directory | None, now: str
    ) -> int:
        unavailable = 0
        db.execute("DELETE FROM memory_fts")
        for row in self._rows(db):
            if row["expires_at"] is not None and row["expires_at"] <= now:
                continue
            try:
                record = self._record(db, files, row)
            except _FILE_ERRORS:
                unavailable += 1
                continue
            self._index(db, row["row_id"], record.draft)
        return unavailable

    def search(self, query: Search = Search()) -> tuple[MemoryHit, ...]:
        if not isinstance(query, Search):
            raise MemoryError("invalid_filter")
        needle = searchable(clean(query.text, 256, empty=True))
        if (
            type(query.limit) is not int
            or not 1 <= query.limit <= MAX_RESULTS
            or (type(query.offset) is not int or not 0 <= query.offset < MAX_RECORDS)
        ):
            raise MemoryError("invalid_limit")
        if (
            (query.kind is not None and not isinstance(query.kind, Kind))
            or (query.status is not None and not isinstance(query.status, Status))
            or any(
                value is not None and type(value) is not date
                for value in (query.since, query.through)
            )
        ):
            raise MemoryError("invalid_filter")
        if query.since and query.through and query.since > query.through:
            raise MemoryError("invalid_filter")
        if needle and len(needle) < 3:
            raise MemoryError("search_too_short")
        with self._session() as session:
            if session is None:
                return ()
            db, files = session
            with transaction(db):
                return self._search(db, files, query, needle)

    def _search(
        self,
        db: sqlite3.Connection,
        files: Directory | None,
        query: Search,
        needle: str,
    ) -> tuple[MemoryHit, ...]:
        sql = "SELECT records.* FROM records"
        conditions = ["(expires_at IS NULL OR expires_at>?)"]
        parameters: list[object] = [_stamp(self._clock())]
        if needle:
            sql += " JOIN memory_fts ON memory_fts.rowid=records.row_id"
            conditions.append("memory_fts MATCH ?")
            parameters.append('"' + needle.replace('"', '""') + '"')
        for column, operator, value in (
            ("kind", "=", query.kind),
            ("status", "=", query.status),
            (
                "substr(created_at,1,10)",
                ">=",
                query.since.isoformat() if query.since else None,
            ),
            (
                "substr(created_at,1,10)",
                "<=",
                query.through.isoformat() if query.through else None,
            ),
            (
                "subject",
                "=",
                clean(query.subject, 256) if query.subject is not None else None,
            ),
            (
                "predicate",
                "=",
                clean(query.predicate, 128) if query.predicate is not None else None,
            ),
            (
                "object",
                "=",
                clean(query.object, 512) if query.object is not None else None,
            ),
        ):
            if value is not None:
                conditions.append(f"{column}{operator}?")
                parameters.append(value)
        sql += " WHERE " + " AND ".join(conditions)
        order = "rank, " if needle else ""
        sql += f" ORDER BY {order}updated_at DESC, records.row_id DESC LIMIT ?"
        parameters.append(MAX_RECORDS)
        hits = []
        skipped = 0
        for row in db.execute(sql, parameters):
            try:
                record = self._record(db, files, row)
                body = _body(record.draft)
                if needle and not any(
                    needle in searchable(part) for part in (row["title"], body)
                ):
                    continue  # A stale index is never proof of a match.
                snippet = body[:240]
                available = True
            except _FILE_ERRORS:
                if needle:
                    continue
                snippet = ""
                available = False
            if skipped < query.offset:
                skipped += 1
                continue
            hits.append(
                MemoryHit(
                    row["memory_id"],
                    Kind(row["kind"]),
                    row["title"],
                    row["source"],
                    Status(row["status"]),
                    row["version"],
                    snippet,
                    available,
                )
            )
            if len(hits) == query.limit:
                break
        return tuple(hits)

    @staticmethod
    def _enabled(db: sqlite3.Connection) -> None:
        if not db.execute("SELECT enabled FROM settings").fetchone()[0]:
            raise MemoryError("persistence_disabled")

    def _write(
        self,
        db: sqlite3.Connection,
        files: Directory | None,
        draft: Draft,
        now: str,
        *,
        previous: sqlite3.Row | None = None,
    ) -> MemoryRecord:
        memory_id = previous["memory_id"] if previous else uuid4().hex
        created = previous["created_at"] if previous else now
        version = previous["version"] + 1 if previous else 1
        name = diary.name_for(memory_id, version)
        for related in draft.related_ids:
            if related == memory_id:
                raise MemoryError("invalid_links")
            self._find(db, related, now)
        fact = draft.fact
        if fact:
            candidates = db.execute(
                """SELECT object, exclusive FROM records WHERE kind='fact' AND status='current'
                   AND subject=? AND predicate=? AND memory_id<>? LIMIT ?""",
                (fact.subject, fact.predicate, memory_id, MAX_RECORDS),
            )
            for candidate in candidates:
                if fact.object == candidate["object"]:
                    raise MemoryError("duplicate_fact")
                if fact.exclusive or candidate["exclusive"]:
                    raise MemoryError("fact_conflict")
        policy = self._settings(db)
        expires = _expiry(created, policy.retention_days)
        if expires is not None and expires <= now:
            raise MemoryError("not_found")
        # Links can be removed by foreign-key cascade. The content hash covers
        # the note or triple and its own metadata, independently of link state.
        canonical = json.dumps(
            asdict(replace(draft, related_ids=())), ensure_ascii=False
        ).encode()
        content = canonical
        if draft.kind is Kind.DIARY:
            content = (
                diary.header(memory_id, draft, created, now, version)
                + (draft.text + "\n").encode()
            )
        size = len(content) + 32 * len(draft.related_ids)
        queued = db.execute("SELECT count(*) FROM cleanup").fetchone()[0]
        if (
            (not previous and policy.records >= MAX_RECORDS)
            or queued >= MAX_RECORDS
            or (
                policy.bytes_used + size - (previous["byte_size"] if previous else 0)
                > MAX_BYTES
            )
        ):
            raise MemoryError("quota_exceeded")
        if draft.kind is Kind.DIARY:
            if files is None:
                raise MemoryError("diary_missing")
            if diary.exists(files, name):
                raise MemoryError("diary_changed")
            with files.entries() as entries:
                for count, _ in enumerate(entries, start=1):
                    if count >= _MAX_DIARY_FILES:
                        raise MemoryError("quota_exceeded")
            diary.stage(files, memory_id, content)
        content_hash = diary.digest(content)
        values = (
            draft.title,
            draft.source,
            draft.text if draft.kind is Kind.TASK else None,
            fact.subject if fact else None,
            fact.predicate if fact else None,
            fact.object if fact else None,
            int(bool(fact and fact.exclusive)),
            now,
            expires,
            version,
            content_hash,
            size,
        )
        if previous:
            row_id = previous["row_id"]
            assignments = ", ".join(f"{field}=?" for field in _FIELDS.split(", "))
            db.execute(
                f"UPDATE records SET {assignments} WHERE row_id=?", (*values, row_id)
            )
            db.execute("DELETE FROM links WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memory_fts WHERE rowid=?", (row_id,))
            if draft.kind is Kind.DIARY:
                db.execute(
                    "INSERT INTO cleanup VALUES (?,?)", (memory_id, previous["version"])
                )
        else:
            columns = "memory_id, kind, created_at, " + _FIELDS
            placeholders = ",".join("?" for _ in range(3 + len(values)))
            row_id = db.execute(
                f"INSERT INTO records ({columns}) VALUES ({placeholders})",
                (
                    memory_id,
                    draft.kind,
                    created,
                    *values,
                ),
            ).lastrowid
            assert row_id is not None
        db.executemany(
            "INSERT INTO links VALUES (?,?)",
            ((memory_id, item) for item in draft.related_ids),
        )
        self._index(db, row_id, draft)
        return MemoryRecord(
            memory_id,
            draft,
            created,
            now,
            version,
            content_hash,
            Status.CURRENT,
            expires,
            None,
        )

    def save(self, draft: Draft) -> MemoryRecord:
        draft = validate(draft)
        with self._session() as session:
            db, files = self._required(session)
            with transaction(db):
                self._enabled(db)
                now = _stamp(self._clock())
                self._expire(db, now)
                record = self._write(db, files, draft, now)
            self._finish(db, files, record)
            return record

    def update(
        self,
        memory_id: str,
        draft: Draft,
        *,
        version: int,
        reviewed_hash: str | None = None,
    ) -> MemoryRecord:
        require_id(memory_id)
        draft = validate(draft)
        with self._session() as session:
            db, files = self._required(session)
            with transaction(db):
                self._enabled(db)
                now = _stamp(self._clock())
                self._expire(db, now)
                row = self._find(db, memory_id, now)
                self._version(row, version)
                if draft.kind != row["kind"]:
                    raise MemoryError("invalid_draft")
                if (
                    draft.kind is Kind.DIARY
                    and files is not None
                    and reviewed_hash is None
                ):
                    diary.publish(files, memory_id, version, row["content_hash"])
                previous = self._record(
                    db, files, row, review=reviewed_hash is not None
                )
                if reviewed_hash is not None:
                    if (
                        draft.kind is not Kind.DIARY
                        or previous.content_hash != reviewed_hash
                    ):
                        raise MemoryError("version_conflict")
                    assert files is not None
                    diary.remove(files, diary.pending_for(memory_id))
                if (
                    draft.fact
                    and previous.draft.fact
                    and (
                        (draft.fact.subject, draft.fact.predicate, draft.fact.object)
                        != (
                            previous.draft.fact.subject,
                            previous.draft.fact.predicate,
                            previous.draft.fact.object,
                        )
                    )
                ):
                    raise MemoryError("supersession_required")
                if self._cleanup(
                    db, files, memory_id=memory_id, reviewed=reviewed_hash is not None
                ):
                    raise MemoryError("cleanup_pending")
                record = self._write(db, files, draft, now, previous=row)
            self._finish(db, files, record)
            return record

    def supersede(self, memory_id: str, draft: Draft, *, version: int) -> MemoryRecord:
        """Explicitly replace the value of one fact, preserving the old record."""
        return self._replace_records(
            ((memory_id, version),), validate(draft), Kind.FACT
        )

    def consolidate(
        self, versions: tuple[tuple[str, int], ...], draft: Draft
    ) -> MemoryRecord:
        """Record an explicitly reviewed merge of two to five notes of one kind."""
        draft = validate(draft)
        if not 2 <= len(versions) <= 5 or draft.kind is Kind.FACT:
            raise MemoryError("invalid_merge")
        return self._replace_records(versions, draft, draft.kind)

    def _replace_records(
        self,
        versions: tuple[tuple[str, int], ...],
        draft: Draft,
        kind: Kind,
    ) -> MemoryRecord:
        for memory_id, _ in versions:
            require_id(memory_id)
        if len({item[0] for item in versions}) != len(versions) or draft.kind != kind:
            raise MemoryError("invalid_replacement")
        with self._session() as session:
            db, files = self._required(session)
            with transaction(db):
                self._enabled(db)
                now = _stamp(self._clock())
                self._expire(db, now)
                for memory_id, version in versions:
                    row = self._find(db, memory_id, now)
                    self._version(row, version)
                    if row["kind"] != kind:
                        raise MemoryError("invalid_replacement")
                    if draft.fact and (
                        (draft.fact.subject, draft.fact.predicate)
                        != (row["subject"], row["predicate"])
                        or draft.fact.object == row["object"]
                    ):
                        raise MemoryError("invalid_replacement")
                    self._record(db, files, row)
                    db.execute(
                        "UPDATE records SET status='superseded' WHERE memory_id=?",
                        (memory_id,),
                    )
                record = self._write(db, files, draft, now)
                db.executemany(
                    "UPDATE records SET superseded_by=? WHERE memory_id=?",
                    ((record.memory_id, item[0]) for item in versions),
                )
            self._finish(db, files, record)
            return record

    @staticmethod
    def _delete(db: sqlite3.Connection, row: sqlite3.Row) -> None:
        if row["kind"] == Kind.DIARY:
            db.execute(
                "INSERT OR IGNORE INTO cleanup VALUES (?,?)",
                (row["memory_id"], row["version"]),
            )
        db.execute("DELETE FROM memory_fts WHERE rowid=?", (row["row_id"],))
        db.execute("DELETE FROM records WHERE row_id=?", (row["row_id"],))

    def _expire(self, db: sqlite3.Connection, now: str) -> None:
        rows = db.execute(
            "SELECT * FROM records WHERE expires_at<=? LIMIT ?", (now, MAX_RECORDS)
        )
        for row in rows.fetchall():
            self._delete(db, row)

    def forget(self, memory_id: str, *, version: int) -> None:
        require_id(memory_id)
        with self._session() as session:
            db, files = self._required(session)
            with transaction(db):
                row = self._find(db, memory_id, _stamp(self._clock()))
                self._version(row, version, current=False)
                self._delete(db, row)
            with transaction(db):
                pending = self._cleanup(db, files, memory_id=memory_id)
            if pending:
                raise MemoryError("deletion_pending")

    def _finish(
        self, db: sqlite3.Connection, files: Directory | None, record: MemoryRecord
    ) -> None:
        if record.draft.kind is not Kind.DIARY:
            return
        assert files is not None
        try:
            with transaction(db):
                row = db.execute(
                    "SELECT * FROM records WHERE memory_id=?", (record.memory_id,)
                ).fetchone()
                if row is not None:
                    diary.publish(
                        files, record.memory_id, row["version"], row["content_hash"]
                    )
                pending = self._cleanup(db, files, memory_id=record.memory_id)
        except (*_FILE_ERRORS, sqlite3.Error):
            raise MemoryError("publication_pending") from None
        if pending:
            raise MemoryError("cleanup_after_save")

    @staticmethod
    def _cleanup(
        db: sqlite3.Connection,
        files: Directory | None,
        *,
        memory_id: str | None = None,
        reviewed: bool = False,
    ) -> int:
        sql = """SELECT cleanup.*, records.version AS live_version, records.content_hash AS live_hash
                 FROM cleanup LEFT JOIN records USING (memory_id)"""
        parameters: tuple[object, ...] = ()
        if memory_id is not None:
            sql += " WHERE memory_id=?"
            parameters = (memory_id,)
        pending = 0
        for row in db.execute(
            sql + " ORDER BY cleanup.version LIMIT ?", (*parameters, 2 * MAX_RECORDS)
        ):
            target, version, live = (
                row["memory_id"],
                row["version"],
                row["live_version"],
            )
            try:
                if files is not None:
                    if live is None:
                        diary.remove(files, diary.pending_for(target))
                    elif not reviewed:
                        if version >= live:
                            raise MemoryError("invalid_cleanup")
                        diary.publish(files, target, live, row["live_hash"])
                    diary.remove(files, diary.name_for(target, version))
            except _FILE_ERRORS:
                pending += 1
                continue
            db.execute(
                "DELETE FROM cleanup WHERE memory_id=? AND version=?", (target, version)
            )
        return pending

    def recover(self) -> Recovery:
        """Finish committed file work, expire records and rebuild derived search."""
        with self._session() as session:
            if session is None:
                return Recovery()
            return self._recover(*session, _stamp(self._clock()), rebuild=True)

    def _recover(
        self,
        db: sqlite3.Connection,
        files: Directory | None,
        now: str,
        *,
        rebuild: bool,
    ) -> Recovery:
        with transaction(db):
            self._expire(db, now)
        with transaction(db):
            rows = self._rows(db)
            diaries = {
                row["memory_id"]: row for row in rows if row["kind"] == Kind.DIARY
            }
            unavailable = 0
            pending = self._cleanup(db, files)
            for row in diaries.values():
                try:
                    if files is None:
                        raise MemoryError("diary_missing")
                    diary.publish(
                        files, row["memory_id"], row["version"], row["content_hash"]
                    )
                except _FILE_ERRORS:
                    unavailable += 1
            if files is not None:
                with files.entries() as entries:
                    for count, entry in enumerate(entries):
                        if count >= _MAX_DIARY_FILES:
                            pending += 1
                            break
                        if not entry.name.endswith(".pending"):
                            continue
                        candidate = entry.name.removesuffix(".pending")
                        if candidate not in diaries:
                            try:
                                require_id(candidate)
                                diary.remove(files, entry.name)
                            except _FILE_ERRORS:
                                pending += 1
            if rebuild:
                unavailable = self._rebuild(db, files, now)
            return Recovery(unavailable, pending)

    def export(self, memory_ids: tuple[str, ...], destination: Path) -> None:
        """Write only reviewed selections to a new JSON file at a trusted path."""
        if not 1 <= len(memory_ids) <= MAX_EXPORT or len(set(memory_ids)) != len(
            memory_ids
        ):
            raise MemoryError("invalid_selection")
        if (
            not destination.is_absolute()
            or ".." in destination.parts
            or (
                destination.is_relative_to(self.root)
                or destination.suffix.lower() != ".json"
            )
        ):
            raise MemoryError("unsafe_export_path")
        with self._session() as session:
            db, files = self._required(session)
            with transaction(db):
                now = _stamp(self._clock())
                selected = []
                for memory_id in memory_ids:
                    row = self._find(db, memory_id, now)
                    record = self._record(db, files, row)
                    validate(record.draft)
                    selected.append(asdict(record))
        payload = json.dumps(
            {"format": "sofia-memory-1", "records": selected},
            ensure_ascii=False,
            indent=2,
        )
        try:
            relative_parts(destination.name)
            with diary.open_directory(destination.parent, create=False) as parent:
                temporary = f".sofia-{uuid4().hex}.tmp"
                fd = parent.create(temporary)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write((payload + "\n").encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                    parent.publish(temporary, destination.name, replace=False)
                finally:
                    try:
                        parent.unlink(temporary)
                    except FileNotFoundError:
                        pass
                diary.sync(parent)
        except FileExistsError:
            raise MemoryError("export_exists") from None
        except _FILE_ERRORS:
            raise MemoryError("export_failed") from None

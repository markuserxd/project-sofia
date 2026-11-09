import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest

from sofia.storage import (
    Draft,
    Fact,
    Kind,
    MemoryError,
    MemoryManager,
    Search,
    Status,
    database,
    diary,
    memory,
)


@pytest.fixture
def store(tmp_path):
    store = MemoryManager(tmp_path / "memory")
    store.set_policy(enabled=True)
    return store


def markdown(store, record):
    return (
        store.root / "diary" / diary.name_for(record.memory_id, record.content_version)
    )


def test_disabled_default_and_read_only_after_disable(tmp_path, caplog):
    root = tmp_path / "private" / "memory"
    store = MemoryManager(root)
    assert not store.settings().enabled
    assert store.search() == ()
    assert not store.recover().pending
    with pytest.raises(MemoryError, match="persistence_disabled"):
        store.save(Draft(Kind.DIARY, "Private title", "A private synthetic note"))
    assert not root.parent.exists()
    store.set_policy(enabled=True)
    record = store.save(Draft(Kind.TASK, "Selected task", "Synthetic outcome"))
    store.set_policy(enabled=False)
    reopened = MemoryManager(root)
    assert not reopened.settings().enabled
    assert reopened.get(record.memory_id) == record
    with pytest.raises(MemoryError, match="persistence_disabled"):
        reopened.update(record.memory_id, record.draft, version=1)
    reopened.forget(record.memory_id, version=1)
    assert reopened.search() == ()
    assert "Synthetic outcome" not in caplog.text


@pytest.mark.parametrize(
    "draft",
    [
        Draft(Kind.DIARY, "Diary", "password=synthetic-value"),
        Draft(
            Kind.TASK,
            "Task",
            "Completed",
            source="Authorization: Bearer synthetic-token",
        ),
        Draft(
            Kind.FACT, "Credential", fact=Fact("fixture", "api_key", "synthetic-value")
        ),
        Draft(Kind.DIARY, "Diary", "-----BEGIN PRIVATE KEY-----"),
    ],
)
def test_recognizable_credentials_never_stored(store, draft, caplog):
    with pytest.raises(MemoryError, match="^sensitive_content$"):
        store.save(draft)
    assert store.settings().records == 0
    assert list((store.root / "diary").iterdir()) == []
    assert "synthetic-value" not in caplog.text


@pytest.mark.parametrize("text", ["", "   ", "a\x00b", "é" * 8193, "x" * 16385])
def test_reject_invalid_diary_text(store, text):
    with pytest.raises(MemoryError, match="invalid_text"):
        store.save(Draft(Kind.DIARY, "Note", text))
    assert store.settings().records == 0


def test_diary_lifecycle_reopen_and_search(store):
    original = "    วันนี้ทดสอบระบบใหม่\n\nOffline Python prototype.\n"
    saved = store.save(
        Draft(Kind.DIARY, "บันทึก SOFIA", original, "Reviewed synthetic conversation")
    )
    path = markdown(store, saved)
    assert original in path.read_text(encoding="utf-8")
    assert saved.memory_id in path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(store.root / diary.DATABASE)) as db:
        assert db.execute("SELECT body FROM records").fetchone() == (None,)
    store = MemoryManager(store.root)
    assert store.get(saved.memory_id) == saved
    for query in ("ทดสอบ", "python", "บันทึก", "sofia"):
        assert store.search(Search(query))[0].memory_id == saved.memory_id
    updated = store.update(
        saved.memory_id, Draft(Kind.DIARY, "Updated", "Final design"), version=1
    )
    assert updated.content_version == 2 and updated.content_hash != saved.content_hash
    assert not path.exists()
    assert store.search(Search("prototype")) == ()
    with pytest.raises(MemoryError, match="version_conflict"):
        store.update(saved.memory_id, saved.draft, version=1)
    store.forget(saved.memory_id, version=2)
    assert not markdown(store, updated).exists()
    assert MemoryManager(store.root).search(Search("Final")) == ()


def test_filters_safe_match_and_pagination(store):
    first = store.save(Draft(Kind.TASK, 'Example "AND"', "Café task"))
    store.save(Draft(Kind.DIARY, "Example diary", "Café diary"))
    assert store.search(Search('"AND"'))[0].memory_id == first.memory_id
    assert len(store.search(Search("Cafe\u0301", limit=1))) == 1
    assert store.search(Search("Café", kind=Kind.TASK))[0].memory_id == first.memory_id
    today = date.fromisoformat(first.created_at[:10])
    assert store.search(Search(since=today, through=today, kind=Kind.TASK))
    assert not store.search(Search(since=today + timedelta(days=1)))
    assert (
        store.search(Search("Café", limit=1, offset=1))[0]
        != store.search(Search("Café", limit=1))[0]
    )
    assert store.search(Search('" OR *); DROP TABLE records;--')) == ()
    assert store.search(Search("title:example")) == ()
    for query, error in [
        (Search(limit=101), "invalid_limit"),
        (Search("xy"), "search_too_short"),
        (Search("\x00bad"), "invalid_text"),
        (Search(since=today, through=date.min), "invalid_filter"),
    ]:
        with pytest.raises(MemoryError, match=error):
            store.search(query)


def test_facts_conflicts_history_and_foreign_keys(store):
    source = store.save(Draft(Kind.DIARY, "Design review", "Synthetic design note"))
    old_draft = Draft(
        Kind.FACT,
        "Language",
        fact=Fact("Fixture", "uses", "Python", True),
        related_ids=(source.memory_id,),
    )
    old = store.save(old_draft)
    with pytest.raises(MemoryError, match="duplicate_fact"):
        store.save(old_draft)
    replacement = Draft(
        Kind.FACT,
        "Language",
        fact=Fact("Fixture", "uses", "Rust"),
        related_ids=(source.memory_id,),
    )
    with pytest.raises(MemoryError, match="fact_conflict"):
        store.save(replacement)
    with pytest.raises(MemoryError, match="supersession_required"):
        store.update(old.memory_id, replacement, version=1)
    new = store.supersede(old.memory_id, replacement, version=1)
    assert store.get(old.memory_id).superseded_by == new.memory_id
    assert (
        store.search(Search(subject="Fixture", predicate="uses", object="Rust"))[
            0
        ].memory_id
        == new.memory_id
    )
    assert store.search(Search("Python")) == ()
    assert (
        store.search(Search("Python", status=Status.SUPERSEDED))[0].memory_id
        == old.memory_id
    )
    store.forget(source.memory_id, version=1)
    assert store.get(new.memory_id).draft.related_ids == ()
    store.forget(new.memory_id, version=1)
    assert store.get(old.memory_id).superseded_by is None
    with store._session() as (db, _):
        with pytest.raises(sqlite3.IntegrityError), database.transaction(db):
            db.execute("INSERT INTO links VALUES (?,?)", (old.memory_id, "f" * 32))
        with pytest.raises(sqlite3.IntegrityError), database.transaction(db):
            db.execute("UPDATE records SET superseded_by='missing'")
        with pytest.raises(sqlite3.IntegrityError), database.transaction(db):
            columns = "memory_id,kind,title,source,subject,predicate,object,exclusive,created_at,updated_at,version,content_hash,byte_size"
            db.execute(f"INSERT INTO records ({columns}) SELECT {columns} FROM records")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bounded_explicit_consolidation(store):
    one = store.save(Draft(Kind.TASK, "First", "Step one"))
    two = store.save(Draft(Kind.TASK, "Second", "Step two"))
    merged = Draft(Kind.TASK, "Reviewed summary", "Steps one and two completed")
    with pytest.raises(MemoryError, match="version_conflict"):
        store.consolidate(((one.memory_id, 1), (two.memory_id, 9)), merged)
    assert store.get(one.memory_id).status is Status.CURRENT
    result = store.consolidate(((one.memory_id, 1), (two.memory_id, 1)), merged)
    assert [hit.memory_id for hit in store.search()] == [result.memory_id]
    assert store.get(one.memory_id).superseded_by == result.memory_id
    assert len(store.search(Search(status=None))) == 3


def test_retention_across_restart_and_no_resurrection(tmp_path):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    root = tmp_path / "memory"
    store = MemoryManager(root, clock=lambda: start)
    store.set_policy(enabled=True, retention_days=30)
    record = store.save(Draft(Kind.DIARY, "Temporary", "Approved brief note"))
    assert record.expires_at.startswith("2026-12-01")
    reopened = MemoryManager(root, clock=lambda: start + timedelta(days=31))
    assert reopened.settings().retention_days == 30
    assert reopened.search() == ()
    assert not reopened.set_policy(enabled=True, retention_days=None).pending
    assert not markdown(store, record).exists()
    assert reopened.search(Search(status=None)) == ()


def test_quota_rejects_without_discarding_data(store, monkeypatch):
    monkeypatch.setattr(memory, "MAX_RECORDS", 1)
    record = store.save(Draft(Kind.TASK, "First", "Keep me"))
    with pytest.raises(MemoryError, match="quota_exceeded"):
        store.save(Draft(Kind.TASK, "Second", "Reject me"))
    monkeypatch.setattr(memory, "MAX_BYTES", 1)
    with pytest.raises(MemoryError, match="quota_exceeded"):
        store.update(
            record.memory_id, Draft(Kind.TASK, "Update", "Change me"), version=1
        )
    assert store.get(record.memory_id) == record


def test_diary_file_bound_preserves_unrecognized_files(store, monkeypatch):
    monkeypatch.setattr(memory, "_MAX_DIARY_FILES", 1)
    unrelated = store.root / "diary" / "notes-from-user.txt"
    unrelated.write_text("Leave this file untouched", encoding="utf-8")
    with pytest.raises(MemoryError, match="quota_exceeded"):
        store.save(Draft(Kind.DIARY, "New note", "At the file limit"))
    assert unrelated.read_text(encoding="utf-8") == "Leave this file untouched"
    assert store.search() == ()


def test_selected_export_is_readable_and_no_clobber(store, tmp_path):
    record = store.save(Draft(Kind.DIARY, "Export", "บันทึกสำหรับส่งออก"))
    store.save(Draft(Kind.TASK, "Unselected", "Unrelated content"))
    output = tmp_path / "selected.json"
    store.export((record.memory_id,), output)
    content = json.loads(output.read_text(encoding="utf-8"))
    assert content["format"] == "sofia-memory-1"
    assert len(content["records"]) == 1
    assert content["records"][0]["memory_id"] == record.memory_id
    assert content["records"][0]["draft"]["text"] == record.draft.text
    with pytest.raises(MemoryError, match="export_exists"):
        store.export((record.memory_id,), output)
    with pytest.raises(MemoryError, match="unsafe_export_path"):
        store.export((record.memory_id,), store.root / "export.json")
    assert not list(tmp_path.glob(".sofia-*.tmp"))
    store.forget(record.memory_id, version=1)
    assert output.is_file()


def test_manual_edit_delete_and_index_rebuild(store):
    record = store.save(Draft(Kind.DIARY, "Manually editable", "Original text"))
    path = markdown(store, record)
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"Original text", b"External change"))
    assert not store.search(Search("Original"))
    assert not store.search()[0].available
    with pytest.raises(MemoryError, match="diary_changed"):
        store.get(record.memory_id)
    assert store.recover().unavailable == 1
    review = store.review_diary(record.memory_id)
    assert review.draft.text == "External change"
    path.write_bytes(raw.replace(b"Original text", b"External new change"))
    with pytest.raises(MemoryError, match="version_conflict"):
        store.update(
            record.memory_id, review.draft, version=1, reviewed_hash=review.content_hash
        )
    review = store.review_diary(record.memory_id)
    saved = store.update(
        record.memory_id, review.draft, version=1, reviewed_hash=review.content_hash
    )
    assert store.search(Search("External"))
    with store._session() as (db, _):
        db.execute("UPDATE memory_fts SET body='stale results'")
    assert store.search(Search("stale")) == ()
    store.recover()
    assert store.search(Search("External"))
    markdown(store, saved).unlink()
    assert store.search(Search("External")) == ()
    assert store.recover().unavailable == 1
    store.forget(record.memory_id, version=2)


def test_invalid_header_and_unsafe_paths(store, tmp_path):
    record = store.save(Draft(Kind.DIARY, "Original", "Private synthetic text"))
    path = markdown(store, record)
    path.write_bytes(path.read_bytes().replace(record.memory_id.encode(), b"f" * 32))
    with pytest.raises(MemoryError, match="diary_invalid"):
        store.review_diary(record.memory_id)
    with pytest.raises(MemoryError, match="invalid_id"):
        store.forget("../outside", version=1)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "untouched").write_text("external", encoding="utf-8")
    link = tmp_path / "linked"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemoryError, match="unsafe_path"):
        MemoryManager(link / "memory").set_policy(enabled=True)
    assert list(outside.iterdir()) == [outside / "untouched"]


def test_failed_stage_and_interrupted_publication(store, monkeypatch):
    record = store.save(Draft(Kind.DIARY, "Diary", "Old content"))
    new = Draft(Kind.DIARY, "Updated", "New content")
    stage = diary.stage

    def broken_stage(*args):
        stage(*args)
        raise OSError("synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(diary, "stage", broken_stage)
        with pytest.raises(MemoryError, match="storage_unavailable"):
            store.update(record.memory_id, new, version=1)
    assert store.get(record.memory_id) == record
    publish = diary.publish

    def interrupted(files, memory_id, version, expected):
        if version == 2:
            raise OSError("synthetic interruption")
        publish(files, memory_id, version, expected)

    with monkeypatch.context() as patch:
        patch.setattr(diary, "publish", interrupted)
        with pytest.raises(MemoryError, match="publication_pending"):
            store.update(record.memory_id, new, version=1)
        assert store.search(Search("New content")) == ()
        assert store.search(Search("Old content")) == ()
    restarted = MemoryManager(store.root)
    assert restarted.recover().unavailable == 0
    assert restarted.get(record.memory_id).draft == new
    assert not markdown(store, record).exists()


def test_commit_failure_leaves_old_record_and_recovers_orphan(store, monkeypatch):
    class FailCommit(sqlite3.Connection):
        def commit(self):
            if self.execute(
                "SELECT 1 FROM records WHERE title='Interrupted'"
            ).fetchone():
                raise sqlite3.OperationalError("synthetic commit error")
            super().commit()

    connect = sqlite3.connect
    old = store.save(Draft(Kind.DIARY, "Previous", "Keep the previous version"))
    new = Draft(Kind.DIARY, "Interrupted", "Uncommitted value")
    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: connect(*args, **kwargs, factory=FailCommit),
        )
        with pytest.raises(MemoryError, match="storage_unavailable"):
            store.update(old.memory_id, new, version=1)
        with pytest.raises(MemoryError, match="storage_unavailable"):
            store.save(new)
    assert len(list((store.root / "diary").glob("*.pending"))) == 2
    assert MemoryManager(store.root).recover().pending == 0
    assert store.get(old.memory_id) == old
    assert list((store.root / "diary").iterdir()) == [markdown(store, old)]
    assert store.search(Search("Uncommitted")) == ()


def test_interrupted_delete_and_concurrent_versions(store, monkeypatch):
    saved = store.save(Draft(Kind.DIARY, "Versioned", "Initial text"))
    gate = Barrier(2)

    def change(text):
        gate.wait(timeout=5)
        try:
            return MemoryManager(store.root).update(
                saved.memory_id, Draft(Kind.DIARY, "Versioned", text), version=1
            )
        except MemoryError as error:
            assert str(error) == "version_conflict"
            return None

    with ThreadPoolExecutor(2) as pool:
        (updated,) = [
            record
            for record in pool.map(change, ("Option one", "Option two"))
            if record
        ]
    assert updated.content_version == 2
    remove = diary.remove

    def blocked(directory, name):
        if name.endswith(".md"):
            raise PermissionError("synthetic sharing violation")
        remove(directory, name)

    with monkeypatch.context() as patch:
        patch.setattr(diary, "remove", blocked)
        with pytest.raises(MemoryError, match="deletion_pending"):
            store.forget(saved.memory_id, version=2)
        assert markdown(store, updated).is_file()
        assert store.search(Search(status=None)) == ()
        assert store.recover().pending == 1
    assert MemoryManager(store.root).recover().pending == 0
    assert not markdown(store, updated).exists()
    assert store.search(Search("Option")) == ()


def test_migration_rebuild_and_reject_newer_or_corrupt_database(store, tmp_path):
    saved = store.save(Draft(Kind.TASK, "Migration", "Backfilled search"))
    with closing(sqlite3.connect(store.root / diary.DATABASE)) as db:
        db.execute("DROP TABLE memory_fts")
        db.execute("PRAGMA user_version = 1")
        db.commit()
    assert (
        MemoryManager(store.root).search(Search("Backfilled"))[0].memory_id
        == saved.memory_id
    )
    with closing(sqlite3.connect(store.root / diary.DATABASE)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        db.execute("PRAGMA user_version = 9")
    with pytest.raises(MemoryError, match="unsupported_schema"):
        store.settings()
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / diary.DATABASE).write_bytes(b"Not a database" * 50)
    with pytest.raises(MemoryError, match="storage_unavailable"):
        MemoryManager(corrupt).settings()


def test_missing_fts_rolls_back_initial_migration(tmp_path, monkeypatch):
    class NoFts(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if "CREATE VIRTUAL TABLE" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(sql, parameters)

    connect = sqlite3.connect
    store = MemoryManager(tmp_path / "memory")
    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: connect(*args, **kwargs, factory=NoFts),
        )
        with pytest.raises(MemoryError, match="fts_unavailable"):
            store.set_policy(enabled=True)
    with closing(connect(store.root / diary.DATABASE)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert db.execute("SELECT * FROM sqlite_schema").fetchall() == []
    assert not store.settings().enabled

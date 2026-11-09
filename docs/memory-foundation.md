# Local memory foundation

```text
SOFIA Memory Manager dialog
       |
       v
Trusted application worker -> MemoryManager -> SQLite records, links and policy
                                   |                   |
                                   v                   v
                              Markdown diary      Derived FTS5 index
                                   |                   |
                                   +-> verified retrieval <-+
```

Markdown owns saved diary narrative. SQLite owns identity, provenance, versions,
retention, lifecycle, links, task summaries, and fact triples. FTS5 indexes saved
titles and text. It is rebuildable from the authoritative data. SOFIA chat, the
agent tool registry, providers, and speech controllers have no memory capability.

## Open and use Memory

1. Launch `python -m sofia.ui` and open **Memory**. A new installation shows
   **Disabled for saves**. Chat and the recent-task panel keep session history.
2. Check **Allow explicit saves** and choose **Apply policy**. To save a specific
   excerpt, select and copy the visible chat text, or copy a sanitized task
   outcome, then paste it into **New memory**. Review the draft and choose
   **Save**. Cancel discards the editor contents. Save a short summary where
   possible. Settings for display, persona, and speech remain session-scoped.
3. Choose **Diary / conversation note** for episodic events and decisions,
   **Task summary** for selected outcomes, or **Fact / relationship** for a
   subject, predicate, and object. Fill in **Source** for provenance and add
   optional related memory IDs. Use notes and outcomes free of raw approvals,
   tool arguments, recordings, credentials, and private output.
4. Use **Search**, the type filter, and **Include history** to browse records.
   An empty query lists metadata and flags unavailable diaries. Select a row to
   inspect or edit its full content. An edit checks the version you reviewed.
5. Select a record and confirm **Delete** to forget it. Select up to 25 records
   with Ctrl-click or Shift-click and choose **Export** to write a new UTF-8
   JSON file. The export includes only selected records, with IDs, provenance,
   timestamps, and IDs of related records. Choose a local destination outside
   the memory folder. Existing files are protected against overwrite.
6. Clear **Allow explicit saves** and apply to stop new saves and revisions.
   Existing records remain accessible from the dialog for inspection, export,
   and deletion. Closing the dialog cancels drafts and unconfirmed actions.
   Shutdown waits for a submitted local operation to settle. Emergency Stop
   stays accessible for agent runs and voice operations.

## Facts and retrieval

For example, the triple `Example project → uses_language → Python` expresses an
explicit statement. Exact duplicate current triples are rejected. When a fact
uses the single-value option, another current value for the same exact subject
and predicate is a conflict. The user can inspect that value and edit its object
to create a replacement. The prior record stays in history and links to its
successor. Deleting a source or successor removes the associated link. For
multi-valued predicates, different objects can coexist. Fact comparisons are
case-sensitive after Unicode NFC normalization. `Search` supports exact subject,
predicate, and object filters and inclusive UTC creation-date filters.

Titles, diaries, summaries, and fact triples support literal, case-insensitive
substring search. Queries are normalized to NFC, case folded, and quoted for
FTS5. Results are verified against the authoritative record, and current-only
search excludes superseded, expired, unavailable, and deleted content. Quotes
and punctuation in queries are literal. The trigram tokenizer supports Thai
substrings of at least three Unicode code points, including within text with no
spaces. It has no Thai word segmentation, transliteration, fuzzy matching, or
semantic ranking. Combining marks count as code points. One- and two-code-point
queries return a validation message. [SQLite FTS5 tokenizer reference](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)

`MemoryStore` and `MemoryRetriever` expose typed records and bounded search.
`MemoryManager.consolidate` accepts two to five reviewed diary or task records
of the same type, their expected versions, and a user-written replacement. It
preserves the originals as history. A future retriever can index by ID, version,
hash, source, status, and timestamp. A future context builder can consume bounded
results as untrusted context after a trusted authorization step.

## Files, limits, and consent

```text
%LOCALAPPDATA%/SOFIA/memory/       # Windows, local NTFS
$XDG_DATA_HOME/sofia/memory/       # Linux, defaults to ~/.local/share/sofia/memory/
├── sofia_memory.db
└── diary/
    ├── <id>.<version>.md
    └── <id>.pending               # temporary while a change settles
```

The dialog is the first application entry point that opens this store. A fresh
store stays absent until a policy is applied. The default is **Keep until
deleted**. A committed 30- or 365-day rule applies to all kinds, including
historical records, from each record's creation time. Shortening retention
requires confirmation. Expired records are excluded immediately. Maintenance
and authorized writes remove expired metadata and queue diary file cleanup.
Opening Memory or choosing **Refresh / recover** processes that queue. Relaxing
retention first processes records already expired under the previous policy.

The store admits up to 512 records and 4 MiB of logical saved bytes, including
history. A narrative can contain up to 16 KiB of UTF-8 text. Titles, provenance,
fact fields, and links have separate bounds. Searches return at most 100 hits
and the dialog uses 25 per page. An export can contain at most 25 records.
SQLite caps the database at 128 MiB and uses rollback journaling. The transient
journal and staged files consume additional space. Directory scans stop at 2,048
entries. At a limit, saves fail and the user chooses what to delete.

The application uses the OS user's application data directory and checks local
paths for symlinks, junctions, reparse points, and hard-linked files. Record IDs
and versions generate filenames. Use an application-owned directory that other
accounts cannot write. The store coordinates cooperating SOFIA processes and
assumes exclusive control of its files during operations. It does not protect
against an administrator or hostile processes running as the same OS user.
Files are readable by the OS user. Use OS disk encryption for protection at rest
if needed. Credentials with recognizable patterns are blocked in all draft
fields, but detection cannot identify every secret. Explicit review is the
primary privacy control. Markdown and result content are plain text in SOFIA.

## Consistency, backup, and recovery

- **Write.** The store verifies the current version and file hash, stages and
  flushes new Markdown, then commits its metadata and search entry in one
  SQLite transaction. Only after commit does it atomically publish the versioned
  file. The earlier file stays available for cleanup until the replacement
  verifies. SQLite serializes writes using short transactions and per-operation
  connections. A failed commit leaves the previous version intact.
- **Recover.** On opening Memory or choosing **Refresh / recover**, SOFIA
  publishes matching committed stages, discards abandoned stages, retries
  queued cleanup, and rebuilds FTS5 from verified records. A missing, invalid,
  or changed diary remains unavailable until resolved. It cannot match a stale
  search entry. Edit only the body below the title when working externally.
  Choose **Review diary edit** to inspect and adopt the exact external version.
  SOFIA checks that hash again at save time. For a damaged header, restore the
  original file from backup or paste reviewed content into a new draft. JSON
  exports can also be inspected and copied into an explicit draft.
- **Forget.** SQLite removes content, metadata, search entries, and related
  links before physical diary cleanup. The cleanup queue holds IDs and versions
  only. An interrupted delete cannot reappear in search. Refresh to retry files
  held open by another program. Existing exports and backups are the user's
  copies. Deletion is logical removal and file unlinking. SSD wear leveling,
  rollback journals, index pages, backups, and filesystem snapshots can retain
  recoverable bytes.
- **Back up and restore.** Close every SOFIA instance before copying the whole
  memory directory. Keep the SQLite file and diary together. If a rollback
  journal exists, keep it with the matching database. Restore the directory as
  a set with SOFIA closed, then open Memory to reconcile. A restored snapshot can
  contain content deleted after the backup. Review retention and old copies.
- **Migrate.** `PRAGMA user_version` records the schema. Version 1 creates the
  settings, records, links, and cleanup tables. Version 2 creates and backfills
  FTS5. Migrations and index backfills commit together. Unknown versions,
  corruption, and missing FTS5 trigram capability fail closed. Recover from a
  known backup if SQLite cannot read the store. SQLite and Markdown do not share
  an atomic commit. A power loss that loses both a stage and its final file
  leaves the record unavailable for explicit recovery.

## Trusted API example

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from sofia.storage import Draft, Kind, MemoryManager, Search

with TemporaryDirectory() as sandbox:
    memory = MemoryManager(Path(sandbox) / "memory")
    memory.set_policy(enabled=True)  # The trusted caller has explicit user consent.
    saved = memory.save(Draft(Kind.DIARY, "Fixture decision", "Use synthetic samples."))
    assert memory.search(Search("synthetic"))[0].memory_id == saved.memory_id
    reviewed = memory.get(saved.memory_id)
    memory.forget(reviewed.memory_id, version=reviewed.content_version)
```

## Verify

Install the repository test extras and run `python -m pytest tests/memory -v`
and `python -m pytest tests/ui -v`. Set `$env:QT_QPA_PLATFORM = 'offscreen'` in
PowerShell before automated Qt tests. For a GUI smoke test, use a clean test
profile and open `python -m sofia.ui`. Send a synthetic Thai or English mock
message with memory disabled. Confirm no memory directory was created. Enable
explicit saves, add a Thai diary, an English task summary, and a fact. Reopen
the app, search, inspect, change the fact value, and check its history. Edit and
export a selected record, cancel one delete, then confirm it and reopen to check
it stays gone. Review retention settings and test Cancel before committing them.
Confirm that Persona, editable STT drafts, manual TTS, approvals, and Emergency
Stop continue to work using the existing offline fixtures.

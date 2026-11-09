"""Explicit, modeless memory management with one bounded worker at a time."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sofia.storage import (
    Draft,
    Fact,
    Kind,
    MemoryError,
    MemoryHit,
    MemoryManager,
    MemoryRecord,
    MemorySettings,
    Recovery,
    Search,
    Status,
)
from sofia.storage.models import MAX_BYTES, MAX_EXPORT, MAX_RECORDS, MAX_TEXT
from sofia.ui.views import label

PAGE_SIZE = 25
ERRORS = {
    "persistence_disabled": "Enable persistence and apply the policy before saving.",
    "sensitive_content": "Possible credentials detected. Remove sensitive details before saving.",
    "invalid_text": "Check for empty required fields, invalid characters, or oversized text.",
    "invalid_links": "Enter at most eight valid memory IDs as related records.",
    "invalid_id": "Enter a valid memory ID.",
    "search_too_short": "Enter at least three characters to search.",
    "version_conflict": "This record changed. Refresh and review the latest version.",
    "fact_conflict": "A current fact with the same subject and predicate conflicts. Find and edit the current fact to replace its value.",
    "duplicate_fact": "This fact is already current.",
    "invalid_replacement": "A replacement must keep the subject and predicate and change the object.",
    "diary_changed": "This diary changed outside SOFIA. Select it and choose Review diary edit.",
    "diary_missing": "The diary file is missing. Use Refresh / recover after restoring it.",
    "diary_invalid": "The diary header or body is invalid. Review the file locally.",
    "publication_pending": "The change committed. Refresh / recover to finish publishing the diary.",
    "cleanup_pending": "A diary file still needs cleanup. Use Refresh / recover.",
    "cleanup_after_save": "The change committed. A previous diary file still needs cleanup.",
    "deletion_pending": "The record is gone from memory. Refresh / recover to retry diary file cleanup.",
    "quota_exceeded": "Memory is at its storage limit. Review saved records and pending cleanup.",
    "export_exists": "An export already exists at that location. Choose a new filename.",
    "export_failed": "The export could not be written. Choose a local folder you can write to.",
    "unsafe_export_path": "Choose a new JSON file outside SOFIA's memory folder.",
    "unsafe_path": "A memory path failed the local filesystem safety checks.",
    "fts_unavailable": "This SQLite runtime needs FTS5 with trigram support.",
    "unsupported_schema": "This memory database uses an unsupported schema.",
    "not_found": "This memory is missing or expired. Refresh the list.",
}
_COMMITTED = {"publication_pending", "cleanup_after_save", "deletion_pending"}


class _Job(QThread):
    def __init__(self, operation: Callable[[], object], parent: QWidget) -> None:
        super().__init__(parent)
        self.operation = operation
        self.result: object = None
        self.error: str | None = None
        self.code: str | None = None

    def run(self) -> None:
        try:
            self.result = self.operation()
        except MemoryError as error:
            self.code = str(error)
            self.error = ERRORS.get(self.code, "Memory operation could not complete.")
        except Exception:
            # Errors may contain private input or local paths.
            self.error = "Memory operation could not complete."


class MemoryEditor(QDialog):
    submitted = Signal(object)

    def __init__(self, parent: QWidget, record: MemoryRecord | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review and save memory")
        self.resize(520, 490)
        form = QFormLayout(self)
        self.kind = QComboBox()
        for kind, title in (
            (Kind.DIARY, "Diary / conversation note"),
            (Kind.TASK, "Task summary"),
            (Kind.FACT, "Fact / relationship"),
        ):
            self.kind.addItem(title, kind)
        form.addRow("Type", self.kind)
        self.title = self._line(form, "Title", 240)
        self.source = self._line(form, "Source", 512)
        self.source.setText("User entry")
        self.related = self._line(form, "Related IDs (comma separated)", 272)
        self.body = QPlainTextEdit()
        self.body.setAccessibleName("Note to save")
        self.body.setPlaceholderText("Paste or write the specific note to retain")
        self.body.textChanged.connect(self._bound)
        form.addRow("Note", self.body)
        self.subject = self._line(form, "Subject", 256)
        self.predicate = self._line(form, "Predicate", 128)
        self.object = self._line(form, "Object", 512)
        self.exclusive = QCheckBox(
            "Only one current value for this subject and predicate"
        )
        form.addRow(self.exclusive)
        form.addRow(
            label(
                "Save only details you intend to keep. Leave out credentials and private information."
            )
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.kind.currentIndexChanged.connect(self._fields)
        if record:
            draft = record.draft
            self.kind.setCurrentIndex(self.kind.findData(draft.kind))
            self.kind.setEnabled(False)
            self.title.setText(draft.title)
            self.source.setText(draft.source)
            self.body.setPlainText(draft.text)
            self.related.setText(", ".join(draft.related_ids))
            if draft.fact:
                self.subject.setText(draft.fact.subject)
                self.predicate.setText(draft.fact.predicate)
                self.object.setText(draft.fact.object)
                self.exclusive.setChecked(draft.fact.exclusive)
                self.subject.setReadOnly(True)
                self.predicate.setReadOnly(True)
                form.insertRow(
                    form.rowCount() - 1,
                    label(
                        "Changing the object creates a replacement and keeps the old fact in history."
                    ),
                )
        self._fields()

    @staticmethod
    def _line(form: QFormLayout, name: str, bound: int) -> QLineEdit:
        field = QLineEdit()
        field.setAccessibleName(name)
        field.setMaxLength(bound)
        form.addRow(name, field)
        return field

    def _bound(self) -> None:
        text = self.body.toPlainText()
        if len(text) > MAX_TEXT:
            self.body.setPlainText(text[:MAX_TEXT])

    def _fields(self) -> None:
        is_fact = self.kind.currentData() == Kind.FACT
        form = self.layout()
        assert isinstance(form, QFormLayout)
        for field in (self.subject, self.predicate, self.object, self.exclusive):
            form.setRowVisible(field, is_fact)
        form.setRowVisible(self.body, not is_fact)

    def _submit(self) -> None:
        kind = Kind(self.kind.currentData())
        fact = (
            Fact(
                self.subject.text(),
                self.predicate.text(),
                self.object.text(),
                self.exclusive.isChecked(),
            )
            if kind is Kind.FACT
            else None
        )
        draft = Draft(
            kind,
            self.title.text(),
            "" if fact else self.body.toPlainText(),
            self.source.text(),
            fact,
            tuple(
                item.strip() for item in self.related.text().split(",") if item.strip()
            ),
        )
        self.submitted.emit(draft)


class MemoryDialog(QDialog):
    idle = Signal()

    def __init__(self, manager: MemoryManager, parent: QWidget) -> None:
        super().__init__(parent)
        self.manager = manager
        self._worker: _Job | None = None
        self._success: Callable[[object], None] | None = None
        self._child: QDialog | None = None
        self._closing = False
        self._policy = MemorySettings()
        self._query = Search(limit=PAGE_SIZE)
        self.setWindowTitle("SOFIA Memory Manager")
        self.resize(850, 680)
        layout = QVBoxLayout(self)
        self.panel = QWidget()
        layout.addWidget(self.panel, 1)
        controls = QVBoxLayout(self.panel)
        policy = QHBoxLayout()
        self.enabled = QCheckBox("Allow explicit saves")
        policy.addWidget(self.enabled)
        self.retention = QComboBox()
        self.retention.setAccessibleName("Memory retention")
        self.retention.addItem("Keep until deleted", None)
        self.retention.addItem("30 days from creation", 30)
        self.retention.addItem("365 days from creation", 365)
        policy.addWidget(self.retention)
        self.apply = self._button(policy, "Apply policy", self._apply_policy)
        policy.addStretch()
        self._button(policy, "Refresh / recover", self.refresh)
        controls.addLayout(policy)
        self.policy_text = label("Loading policy…", "muted")
        controls.addWidget(self.policy_text)
        filters = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setMaxLength(256)
        self.query.setPlaceholderText("Search titles and saved text")
        self.query.returnPressed.connect(self._search)
        filters.addWidget(self.query, 1)
        self.kind = QComboBox()
        self.kind.setAccessibleName("Filter by type")
        self.kind.addItem("All types", None)
        for kind in Kind:
            self.kind.addItem(kind.value.title(), kind)
        filters.addWidget(self.kind)
        self.history = QCheckBox("Include history")
        filters.addWidget(self.history)
        self._button(filters, "Search", self._search)
        controls.addLayout(filters)
        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.results.setAccessibleName("Saved memories")
        self.results.itemSelectionChanged.connect(self._buttons)
        self.results.itemDoubleClicked.connect(lambda _: self._open_record("inspect"))
        controls.addWidget(self.results, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Full memory content")
        controls.addWidget(self.details, 1)
        pages = QHBoxLayout()
        self.previous = self._button(pages, "Previous", lambda: self._page(-PAGE_SIZE))
        self.next = self._button(pages, "Next", lambda: self._page(PAGE_SIZE))
        pages.addStretch()
        self.new = self._button(pages, "New memory", self._editor)
        self.inspect = self._button(
            pages, "Inspect", lambda: self._open_record("inspect")
        )
        self.edit = self._button(pages, "Edit", lambda: self._open_record("edit"))
        self.review = self._button(
            pages, "Review diary edit", lambda: self._open_record("review")
        )
        self.delete = self._button(pages, "Delete", self._delete)
        self.export = self._button(pages, "Export", self._export)
        controls.addLayout(pages)
        self.notice = label("", "muted")
        layout.addWidget(self.notice)
        self.refresh()

    @staticmethod
    def _button(
        row: QHBoxLayout, title: str, clicked: Callable[[], None]
    ) -> QPushButton:
        button = QPushButton(title)
        button.clicked.connect(lambda: clicked())
        row.addWidget(button)
        return button

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def _run(
        self, operation: Callable[[], object], success: Callable[[object], None]
    ) -> None:
        if self.busy or self._closing:
            return
        self.panel.setEnabled(False)
        self.notice.setText("Working locally…")
        self._success = success
        worker = _Job(operation, self)
        self._worker = worker
        worker.finished.connect(self._finished)
        worker.start()

    @Slot()
    def _finished(self) -> None:
        worker, success = self._worker, self._success
        assert worker is not None and success is not None
        self._worker = None
        self._success = None
        worker.deleteLater()
        if self._closing:
            self.idle.emit()
            self.reject()
            return
        self.panel.setEnabled(self._child is None)
        if worker.error and worker.code in _COMMITTED:
            if self._child is not None:
                self._child.reject()
            self._load(recover=True, message=worker.error)
        elif worker.error:
            self.notice.setText(worker.error)
            if self._child is not None:
                self._child.setEnabled(True)
                self._child.setWindowTitle(worker.error)
        else:
            self.notice.clear()
            success(worker.result)
        self._buttons()

    def _load(
        self, *, recover: bool = False, message: str = "", query: Search | None = None
    ) -> None:
        query = query if query is not None else self._query

        def load() -> object:
            report = self.manager.recover() if recover else Recovery()
            return self.manager.settings(), self.manager.search(query), report

        def loaded(result: object) -> None:
            assert isinstance(result, tuple)
            settings, hits, report = result
            self._query = query
            self._policy = settings
            self.enabled.setChecked(settings.enabled)
            self.retention.setCurrentIndex(
                self.retention.findData(settings.retention_days)
            )
            state = (
                "Enabled for explicit saves"
                if settings.enabled
                else "Disabled for saves"
            )
            self.policy_text.setText(
                f"{state} · {settings.records}/{MAX_RECORDS} records · {settings.bytes_used:,}/{MAX_BYTES:,} saved bytes"
            )
            self.results.clear()
            for hit in hits:
                state = hit.status.value if hit.available else "unavailable"
                item = QListWidgetItem(
                    f"{hit.kind.value.title()} · {hit.title} [{state}]"
                )
                item.setData(Qt.ItemDataRole.UserRole, hit)
                self.results.addItem(item)
            self.details.clear()
            if report.unavailable or report.pending:
                message_text = f"Recovery found {report.unavailable} unavailable records and {report.pending} pending file operations."
                self.notice.setText(f"{message} {message_text}".strip())
            else:
                self.notice.setText(
                    message or ("No matching memories." if not hits else "")
                )

        self._run(load, loaded)

    def refresh(self) -> None:
        self._load(recover=True)

    def _search(self) -> None:
        kind = self.kind.currentData()
        query = Search(
            self.query.text(),
            Kind(kind) if kind else None,
            None if self.history.isChecked() else Status.CURRENT,
            limit=PAGE_SIZE,
        )
        self._load(query=query)

    def _page(self, delta: int) -> None:
        self._load(query=replace(self._query, offset=self._query.offset + delta))

    def _selected(self) -> list[MemoryHit]:
        return [
            item.data(Qt.ItemDataRole.UserRole) for item in self.results.selectedItems()
        ]

    def _buttons(self) -> None:
        selected = self._selected()
        one = selected[0] if len(selected) == 1 else None
        editable = (
            self._policy.enabled and one is not None and one.status is Status.CURRENT
        )
        self.new.setEnabled(self._policy.enabled)
        self.inspect.setEnabled(one is not None and one.available)
        self.edit.setEnabled(editable and one is not None and one.available)
        self.review.setEnabled(
            editable
            and one is not None
            and one.kind is Kind.DIARY
            and not one.available
        )
        self.delete.setEnabled(one is not None)
        self.export.setEnabled(
            0 < len(selected) <= MAX_EXPORT and all(hit.available for hit in selected)
        )
        self.previous.setEnabled(self._query.offset > 0)
        self.next.setEnabled(
            self.results.count() == PAGE_SIZE
            and self._query.offset + PAGE_SIZE < MAX_RECORDS
        )

    def _show_child(self, child: QDialog) -> None:
        self._child = child
        self.panel.setEnabled(False)
        child.setWindowModality(Qt.WindowModality.NonModal)
        child.finished.connect(self._child_finished)
        child.show()

    @Slot()
    def _child_finished(self) -> None:
        child, self._child = self._child, None
        if child is not None:
            child.deleteLater()
        self.panel.setEnabled(not self.busy and not self._closing)

    def _confirm(self, title: str, text: str, accepted: Callable[[], None]) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            self,
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.finished.connect(
            lambda result: (
                accepted() if result == QMessageBox.StandardButton.Yes else None
            )
        )
        self._show_child(box)

    def _apply_policy(self) -> None:
        days, enabled = self.retention.currentData(), self.enabled.isChecked()

        def commit() -> None:
            self._run(
                lambda: self.manager.set_policy(enabled=enabled, retention_days=days),
                lambda _: self._load(recover=True, message="Policy saved."),
            )

        if days != self._policy.retention_days and days is not None:
            self._confirm(
                "Apply retention",
                "Apply this age limit to saved memories? Records past the new limit will be deleted.",
                commit,
            )
        else:
            commit()

    def _open_record(self, action: str) -> None:
        selected = self._selected()
        if len(selected) != 1:
            return
        hit = selected[0]
        read = self.manager.review_diary if action == "review" else self.manager.get

        def opened(result: object) -> None:
            assert isinstance(result, MemoryRecord)
            if action == "inspect":
                draft = result.draft
                fact = draft.fact
                value = (
                    f"{fact.subject} → {fact.predicate} → {fact.object}\nSingle value: {fact.exclusive}"
                    if fact
                    else draft.text
                )
                self.details.setPlainText(
                    f"{draft.title}\n{draft.kind.value} · {result.status.value} · version {result.content_version}\n"
                    f"ID: {result.memory_id}\nSource: {draft.source}\nCreated: {result.created_at}\n"
                    f"Updated: {result.updated_at}\nExpires: {result.expires_at or 'Until deleted'}\n"
                    f"Replacement: {result.superseded_by or 'None'}\nRelated IDs: {', '.join(draft.related_ids) or 'None'}\n\n{value}"
                )
            elif result.status is Status.CURRENT:
                self._editor(result, review=action == "review")

        self._run(lambda: read(hit.memory_id), opened)

    def _editor(
        self, record: MemoryRecord | None = None, *, review: bool = False
    ) -> None:
        editor = MemoryEditor(self, record)
        if review:
            editor.setWindowTitle("Review external diary body before adopting it")

        def submit(draft: Draft) -> None:
            def save() -> MemoryRecord:
                if record is None:
                    return self.manager.save(draft)
                original = record.draft.fact
                if (
                    original
                    and draft.fact
                    and original.object != draft.fact.object.strip()
                ):
                    return self.manager.supersede(
                        record.memory_id, draft, version=record.content_version
                    )
                return self.manager.update(
                    record.memory_id,
                    draft,
                    version=record.content_version,
                    reviewed_hash=record.content_hash if review else None,
                )

            def saved(_: object) -> None:
                editor.accept()
                self._load(message="Memory saved.")

            editor.setEnabled(False)
            self._run(save, saved)

        editor.submitted.connect(submit)
        self._show_child(editor)

    def _delete(self) -> None:
        selected = self._selected()
        if len(selected) != 1:
            return
        hit = selected[0]
        self._confirm(
            "Delete memory",
            "Delete the selected saved memory? Exported copies remain wherever you saved them.",
            lambda: self._run(
                lambda: self.manager.forget(hit.memory_id, version=hit.content_version),
                lambda _: self._load(message="Memory deleted."),
            ),
        )

    def _export(self) -> None:
        ids = tuple(hit.memory_id for hit in self._selected())
        if not 1 <= len(ids) <= MAX_EXPORT:
            return
        chooser = QFileDialog(self, "Export selected memories")
        chooser.setOption(QFileDialog.Option.DontUseNativeDialog)
        chooser.setOption(QFileDialog.Option.DontConfirmOverwrite)
        chooser.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        chooser.setSupportedSchemes(["file"])
        chooser.setNameFilter("JSON (*.json)")
        chooser.setDefaultSuffix("json")
        chooser.fileSelected.connect(
            lambda path: self._run(
                lambda: self.manager.export(ids, Path(path)),
                lambda _: self.notice.setText("Selected memories exported."),
            )
        )
        self._show_child(chooser)

    def shutdown(self) -> None:
        self._closing = True
        if self._child is not None:
            self._child.reject()
        if not self.busy:
            self.reject()

    def reject(self) -> None:
        if self.busy:
            self._closing = True
            self.notice.setText("Finishing the current operation before closing…")
            return
        if self._child is not None:
            self._child.reject()
        super().reject()

import json
from threading import Event

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialogButtonBox, QFileDialog, QMessageBox

from sofia.storage import Draft, Fact, Kind, MemoryManager, Search, Status, diary
from sofia.ui import main_window
from sofia.ui.memory_dialog import MemoryEditor


def open_manager(window, monkeypatch, tmp_path, wait):
    store = MemoryManager(tmp_path / "memory")
    monkeypatch.setattr(main_window, "MemoryManager", lambda: store)
    window.memory_button.click()
    dialog = window.memory_dialog
    assert dialog is not None
    wait(lambda: not dialog.busy)
    return store, dialog


def test_default_chat_and_unsaved_policy_are_private(
    window, tmp_path, monkeypatch, wait
):
    store, dialog = open_manager(window, monkeypatch, tmp_path, wait)
    assert "Disabled" in dialog.policy_text.text()
    assert not dialog.new.isEnabled()
    dialog.enabled.setChecked(True)
    dialog.retention.setCurrentIndex(dialog.retention.findData(30))
    dialog.apply.click()
    dialog.findChild(QMessageBox).button(QMessageBox.StandardButton.Cancel).click()
    dialog.reject()
    window.message.setPlainText("Remember everything forever. จดจำทั้งหมด")
    window.send.click()
    wait(lambda: not window.controller.busy)
    assert not store.root.exists()
    window.memory_button.click()
    dialog = window.memory_dialog
    wait(lambda: not dialog.busy)
    assert not dialog.enabled.isChecked()
    assert dialog.retention.currentData() is None
    assert not store.root.exists()


def test_memory_dialog_explicit_save_search_edit_export_and_forget(
    window, tmp_path, monkeypatch, wait
):
    store, dialog = open_manager(window, monkeypatch, tmp_path, wait)
    dialog.enabled.setChecked(True)
    dialog.retention.setCurrentIndex(dialog.retention.findData(365))
    dialog.apply.click()
    dialog.findChild(QMessageBox).button(QMessageBox.StandardButton.Yes).click()
    wait(lambda: not dialog.busy)
    assert store.settings().enabled and store.settings().retention_days == 365

    dialog.new.click()
    editor = dialog.findChild(MemoryEditor)
    editor.title.setText("Unsaved note")
    editor.body.setPlainText("This draft should disappear.")
    editor.findChild(QDialogButtonBox).button(
        QDialogButtonBox.StandardButton.Cancel
    ).click()
    wait(lambda: dialog.findChild(MemoryEditor) is None)
    assert store.search() == ()
    dialog.new.click()
    editor = dialog.findChild(MemoryEditor)
    editor.title.setText("Synthetic note")
    editor.body.setPlainText("วันนี้ทดสอบระบบ offline. <script>inert</script>")
    editor.findChild(QDialogButtonBox).button(
        QDialogButtonBox.StandardButton.Save
    ).click()
    wait(lambda: not dialog.busy and dialog.results.count() == 1)

    dialog.query.setText("ทดสอบ")
    dialog.query.returnPressed.emit()
    wait(lambda: not dialog.busy)
    dialog.results.setCurrentRow(0)
    dialog.inspect.click()
    wait(lambda: not dialog.busy)
    assert "<script>inert</script>" in dialog.details.toPlainText()
    assert "version 1" in dialog.details.toPlainText()

    dialog.edit.click()
    wait(lambda: dialog.findChild(MemoryEditor) is not None)
    editor = dialog.findChild(MemoryEditor)
    editor.body.setPlainText("ทดสอบ Updated synthetic text")
    editor.findChild(QDialogButtonBox).button(
        QDialogButtonBox.StandardButton.Save
    ).click()
    wait(lambda: not dialog.busy and dialog.results.count() == 1)
    assert store.search(Search("Updated"))[0].content_version == 2

    dialog.results.setCurrentRow(0)
    dialog.export.click()
    chooser = dialog.findChild(QFileDialog)
    output = tmp_path / "selected.json"
    chooser.selectFile(str(output))
    chooser.accept()
    wait(lambda: not dialog.busy)
    assert (
        json.loads(output.read_text(encoding="utf-8"))["records"][0]["content_version"]
        == 2
    )

    dialog.delete.click()
    dialog.findChild(QMessageBox).button(QMessageBox.StandardButton.Cancel).click()
    wait(lambda: dialog.findChild(QMessageBox) is None)
    assert store.search()
    dialog.delete.click()
    dialog.findChild(QMessageBox).button(QMessageBox.StandardButton.Yes).click()
    wait(lambda: not dialog.busy)
    assert not store.search()
    dialog.enabled.setChecked(False)
    dialog.apply.click()
    wait(lambda: not dialog.busy)
    assert not store.settings().enabled
    assert not dialog.new.isEnabled()


def test_fact_edit_records_history(window, tmp_path, monkeypatch, wait):
    store = MemoryManager(tmp_path / "memory")
    store.set_policy(enabled=True)
    saved = store.save(
        Draft(
            Kind.FACT, "Fixture language", fact=Fact("Fixture", "uses", "Python", True)
        )
    )
    monkeypatch.setattr(main_window, "MemoryManager", lambda: store)
    window.memory_button.click()
    dialog = window.memory_dialog
    wait(lambda: not dialog.busy)
    dialog.results.setCurrentRow(0)
    dialog.edit.click()
    wait(lambda: dialog.findChild(MemoryEditor) is not None)
    editor = dialog.findChild(MemoryEditor)
    editor.object.setText("Rust")
    editor.findChild(QDialogButtonBox).button(
        QDialogButtonBox.StandardButton.Save
    ).click()
    wait(lambda: not dialog.busy)
    assert store.search(Search("Rust"))
    assert store.get(saved.memory_id).status is Status.SUPERSEDED


def test_committed_pending_save_recovers_without_offering_retry(
    window, tmp_path, monkeypatch, wait
):
    store, dialog = open_manager(window, monkeypatch, tmp_path, wait)
    dialog.enabled.setChecked(True)
    dialog.apply.click()
    wait(lambda: not dialog.busy and dialog.new.isEnabled())
    publish = diary.publish
    interrupted = False

    def first_publish(*args):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("synthetic publish interruption")
        publish(*args)

    monkeypatch.setattr(diary, "publish", first_publish)
    dialog.new.click()
    editor = dialog.findChild(MemoryEditor)
    editor.title.setText("Reviewed note")
    editor.body.setPlainText("Exactly one approved diary.")
    editor.findChild(QDialogButtonBox).button(
        QDialogButtonBox.StandardButton.Save
    ).click()
    wait(lambda: not dialog.busy and dialog.findChild(MemoryEditor) is None)
    assert "committed" in dialog.notice.text()
    assert len(store.search(Search("approved"))) == 1


def test_memory_worker_keeps_stop_and_shutdown_responsive(
    window, tmp_path, monkeypatch, wait
):
    store, dialog = open_manager(window, monkeypatch, tmp_path, wait)
    entered, release = Event(), Event()
    recover = store.recover

    def blocked():
        entered.set()
        release.wait(5)
        return recover()

    monkeypatch.setattr(store, "recover", blocked)
    dialog.refresh()
    try:
        wait(entered.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        wait(lambda: bool(ticks))
        assert dialog.busy
        window.slow.click()
        wait(lambda: window.controller.busy)
        window.stop.click()
        wait(lambda: not window.controller.busy)
        window.close()
        assert dialog.busy
    finally:
        release.set()
    wait(lambda: not window.isVisible() and window.memory_dialog is None)
    assert not store.root.exists()

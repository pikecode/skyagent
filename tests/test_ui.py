from __future__ import annotations

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLineEdit, QMessageBox

from skyagent_manager.db import StoreDatabase
from skyagent_manager.imports import ImportReport, ImportRowResult
from skyagent_manager.ledger_dialogs import (
    BenefitDialog,
    ImportReportDialog,
    MemberDialog,
)
from skyagent_manager.main_window import MainWindow
from skyagent_manager.sync import ItemResult, SyncWorker


@pytest.fixture(scope="module")
def app():
    import os

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    application = QApplication.instance() or QApplication([])
    yield application


def test_offscreen_window_backup_and_sync_result(app, tmp_path):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"q" * 32)
    sid = database.add_store("测试门店")
    database.add_member(sid, "会员", "13812345678")
    window = MainWindow(database.path, database=database)
    window.show()
    try:
        app.processEvents()
        assert window.tabs.count() == 8
        assert window.tabs.indexOf(window.backend_query_page) >= 0
        assert window.members_table.rowCount() == 1
        assert "13812345678" not in window.members_table.item(0, 1).text()
        assert list(window.backups.directory.glob("auto-*.skybackup"))
        item = database.sync_items(sid, "members")[0]
        window._sync_batch(sid, "members", [ItemResult(item["id"], True, "已同步")])
        assert window.sync_table.rowCount() == 1
        assert window.sync_table.item(0, 2).text() == "成功"
    finally:
        window.close()
        app.processEvents()


def test_worker_batches_and_cancel_without_sharing_database(app):
    class Client:
        batch_size = 200
        calls = 0
        closed = False

        def submit(self, items):
            self.calls += 1
            worker.requestInterruption()
            return [ItemResult(item["id"], True, "已同步") for item in items]

        def close(self):
            self.closed = True

    client = Client()
    worker = SyncWorker(
        "store", "members", [{"id": str(i)} for i in range(201)], client
    )
    batches = []
    worker.batch_ready.connect(lambda store, entity, results: batches.append(results))
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(loop.quit)
    watchdog.start(5000)
    worker.start()
    loop.exec()
    assert worker.wait(1000)
    app.processEvents()
    assert client.calls == 1 and client.closed
    assert len(batches[0]) == 200 and batches[0][0].ok
    assert len(batches[1]) == 1 and not batches[1][0].ok


def test_filters_preserve_selection_by_id_then_clear_when_hidden(app, tmp_path):
    database = StoreDatabase(tmp_path / "ledger.sqlite3", key=b"u" * 32)
    store = database.add_store("A")
    alpha = database.add_member(store, "Alpha", "13812345678")
    database.add_member(store, "Beta", "13912345678")
    benefit = database.add_benefit(store, "gift", "Gift", member_phone="13812345678")
    window = MainWindow(database.path, database=database)
    try:
        index = next(
            i
            for i in range(window.members_table.rowCount())
            if window.members_table.item(i, 0).data(Qt.UserRole) == alpha
        )
        window.members_table.setCurrentCell(index, 0)
        window._refresh_members()
        assert window._selected_id(window.members_table) == alpha
        window.member_search.setText("Beta")
        assert window.members_table.rowCount() == 1
        assert window.members_table.currentRow() == -1
        with pytest.raises(ValueError, match="选择"):
            window._selected_id(window.members_table)
        window.benefit_kind_filter.setCurrentIndex(
            window.benefit_kind_filter.findData("gift")
        )
        assert window.benefits_table.rowCount() == 1
        database.set_benefit_state(benefit, "已使用", store_id=store)
        window.benefit_state_filter.setCurrentText("可用")
        assert window.benefits_table.rowCount() == 0
    finally:
        window.close()
        app.processEvents()


def test_edit_dialogs_keep_sensitive_fields_masked_and_values_intact(app):
    member = MemberDialog(
        record={"display_name": "Name", "phone": "13812345678", "note": "line1\nline2"}
    )
    benefit = BenefitDialog(
        record={
            "name": "Gift",
            "kind": "gift",
            "code": "private-code",
            "member_phone": "13812345678",
            "note": "",
        }
    )
    try:
        assert member.phone.echoMode() == QLineEdit.Password
        assert member.phone.text() == "13812345678"
        assert member.note.text() == "line1\nline2"
        assert benefit.code.echoMode() == QLineEdit.Password
        assert benefit.code.text() == "private-code"
    finally:
        member.close()
        benefit.close()


def test_member_editor_and_confirmed_delete_keep_benefit(app, tmp_path, monkeypatch):
    database = StoreDatabase(tmp_path / "ledger.sqlite3", key=b"u" * 32)
    store = database.add_store("A")
    member = database.add_member(store, "Alpha", "13812345678")
    benefit = database.add_benefit(store, "gift", "Gift", member_phone="13812345678")
    window = MainWindow(database.path, database=database)

    def editor(parent, *, record):
        dialog = MemberDialog(parent, record=record)
        dialog.name.setText("Updated")
        dialog.phone.setText("13912345678")
        dialog.authorized.setChecked(True)
        monkeypatch.setattr(dialog, "exec", lambda: QDialog.Accepted)
        return dialog

    monkeypatch.setattr("skyagent_manager.main_window.MemberDialog", editor)
    monkeypatch.setattr(window, "_error", lambda message: pytest.fail(message))
    try:
        window.members_table.setCurrentCell(0, 0)
        window._edit_member()
        assert database.get_member(store, member)["display_name"] == "Updated"
        assert window._selected_id(window.members_table) == member
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
        window._delete_member()
        assert database.get_member(store, member)
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
        window._delete_member()
        assert window.members_table.rowCount() == 0
        assert database.get_benefit(store, benefit)["member_id"] is None
        assert window.benefits_table.rowCount() == 1
    finally:
        window.close()
        app.processEvents()


def test_benefit_editor_and_delete_refresh_the_visible_list(app, tmp_path, monkeypatch):
    database = StoreDatabase(tmp_path / "ledger.sqlite3", key=b"u" * 32)
    store = database.add_store("A")
    benefit = database.add_benefit(store, "gift", "Gift", "private-code")
    window = MainWindow(database.path, database=database)

    def editor(parent, *, record):
        dialog = BenefitDialog(parent, record=record)
        dialog.name.setText("Updated")
        dialog.quantity.setValue(3)
        dialog.state.setCurrentText("已预留")
        monkeypatch.setattr(dialog, "exec", lambda: QDialog.Accepted)
        return dialog

    monkeypatch.setattr("skyagent_manager.main_window.BenefitDialog", editor)
    monkeypatch.setattr(window, "_error", lambda message: pytest.fail(message))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    try:
        window.benefits_table.setCurrentCell(0, 0)
        window._edit_benefit()
        assert database.get_benefit(store, benefit)["state"] == "已预留"
        assert database.get_benefit(store, benefit)["quantity"] == 3
        window._delete_benefit()
        assert window.benefits_table.rowCount() == 0
    finally:
        window.close()
        app.processEvents()


def test_export_current_filter_does_not_export_other_rows(app, tmp_path, monkeypatch):
    database = StoreDatabase(tmp_path / "ledger.sqlite3", key=b"u" * 32)
    store = database.add_store("A")
    database.add_member(store, "Alpha", "13812345678", "=formula")
    database.add_member(store, "Beta", "13912345678")
    window = MainWindow(database.path, database=database)
    path = tmp_path / "export.csv"
    monkeypatch.setattr(
        "skyagent_manager.main_window.ExportPreviewDialog.exec",
        lambda self: QDialog.Accepted,
    )
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    try:
        window.member_search.setText("Alpha")
        window._export_members()
        content = path.read_text(encoding="utf-8-sig")
        assert "Alpha" in content and "Beta" not in content
        assert "13812345678" not in content and "'=formula" in content
    finally:
        window.close()
        app.processEvents()


def test_import_report_shows_skips_and_exports_all(app, tmp_path, monkeypatch):
    report = ImportReport(
        [
            ImportRowResult(2, True, "已导入"),
            ImportRowResult(3, False, "手机号格式不正确。"),
        ]
    )
    dialog = ImportReportDialog(report)
    path = tmp_path / "report.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    try:
        assert dialog.table.rowCount() == 1
        assert dialog.table.item(0, 0).text() == "3"
        dialog.failed_only.setChecked(False)
        assert dialog.table.rowCount() == 2
        dialog._export()
        content = path.read_text(encoding="utf-8-sig")
        assert "2,已导入" in content and "3,跳过" in content
    finally:
        dialog.close()

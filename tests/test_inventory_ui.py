import csv
import os

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.main_window import MainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "inventory.db", key=b"v" * 32)
    database.add_store("A")
    database.add_store("B")
    view = MainWindow(database.path, database=database)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    view.errors = []
    monkeypatch.setattr(view, "_error", view.errors.append)
    yield view
    view.close()
    app.processEvents()


@pytest.mark.parametrize(
    "outcome,state",
    [("succeeded", "used"), ("failed", "available"), ("canceled", "available")],
)
def test_inventory_page_task_lifecycle(window, outcome, state):
    page = window.inventory_page
    page.import_text("private-stock-code\nprivate-stock-code\n")
    assert page.table.rowCount() == 1
    assert "private-stock-code" not in page.table.item(0, 2).text()
    page.check_visible()
    page.start()
    assert page.table.item(0, 3).text() == "已预占"
    assert page.task_table.rowCount() == 1
    page.task_table.setCurrentCell(0, 0)
    page.finish(outcome)
    assert Inventory(window.db).list(window.current_store)[0]["state"] == state
    assert not window.errors


def test_task_report_masking_progress_and_store_isolation(window, tmp_path):
    page = window.inventory_page
    sid = window.current_store
    page.repository.import_values(
        sid, "gift", ["private-first-resource", "private-second-resource"]
    )
    ids = [row["id"] for row in page.repository.list(sid)]
    task = page.repository.start(sid, ids)
    page.repository.complete_item(sid, task, ids[0], succeeded=True)
    before = window.db.path.read_bytes()
    path = tmp_path / "report.csv"
    page.export_task_to(path, task)
    content = path.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(content.splitlines()))
    assert len(rows) == 2
    assert {row["item_state"] for row in rows} == {"used", "reserved"}
    assert all(
        row["scope"] == "local-simulation-only"
        and row["completed"] == "1"
        and row["total"] == "2"
        for row in rows
    )
    assert (
        "private-first-resource" not in content
        and "private-second-resource" not in content
    )
    assert "value" not in rows[0] and "token" not in rows[0] and "note" not in rows[0]
    assert window.db.path.read_bytes() == before
    window.store_combo.setCurrentIndex(1)
    with pytest.raises(ValueError, match="不存在"):
        page.export_task_to(path, task)
    assert path.read_text(encoding="utf-8-sig") == content


def test_task_report_atomic_failure_preserves_existing_file(
    window, tmp_path, monkeypatch
):
    import skyagent_manager.security as security

    page = window.inventory_page
    sid = window.current_store
    page.repository.import_values(sid, "gift", ["private-export-resource"])
    task = page.repository.start(sid, [page.repository.list(sid)[0]["id"]])
    path = tmp_path / "existing.csv"
    path.write_bytes(b"existing report")
    before = window.db.path.read_bytes()

    def fail(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(security.os, "replace", fail)
    with pytest.raises(OSError):
        page.export_task_to(path, task)
    assert path.read_bytes() == b"existing report"
    assert not list(tmp_path.glob(".existing.csv.*.tmp"))
    assert window.db.path.read_bytes() == before


def test_task_report_database_symlink_rejected(window, tmp_path):
    page = window.inventory_page
    sid = window.current_store
    page.repository.import_values(sid, "gift", ["private-resource"])
    task = page.repository.start(sid, [page.repository.list(sid)[0]["id"]])
    path = tmp_path / "database-alias.csv"
    try:
        path.symlink_to(window.db.path)
    except OSError:
        pytest.skip("symbolic links unavailable")
    before = window.db.path.read_bytes()
    with pytest.raises(ValueError, match="不能覆盖数据库"):
        page.export_task_to(path, task)
    assert window.db.path.read_bytes() == before


def test_task_report_dialog_cancel_and_store_change(window, tmp_path, monkeypatch):
    page = window.inventory_page
    sid = window.current_store
    page.repository.import_values(sid, "gift", ["private-resource"])
    task = page.repository.start(sid, [page.repository.list(sid)[0]["id"]])
    page.refresh()
    page.task_table.setCurrentCell(0, 0)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    page.export_task()
    assert not window.errors
    path = tmp_path / "report.csv"

    def switch(*args):
        window.store_combo.setCurrentIndex(1)
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", switch)
    page.export_task()
    assert not path.exists()
    assert "门店已变化" in window.errors[-1]
    window.store_combo.setCurrentIndex(0)
    with pytest.raises(ValueError):
        page.export_task_to(window.db.path, task)
    with pytest.raises(ValueError):
        page.export_task_to(tmp_path / "report.txt", task)
    window.sync_worker = object()
    try:
        with pytest.raises(ValueError, match="不可导出"):
            page.export_task_to(path, task)
    finally:
        window.sync_worker = None


def test_invite_selection_edit_delete_and_isolation(window):
    page = window.inventory_page
    page.kind.setCurrentIndex(page.kind.findData("invite"))
    page.import_text("private-invitation-value")
    page.table.setCurrentCell(0, 0)
    rid = page.selected_id()
    page.select_invite(False)
    assert "未选择" not in page.selected_invite.text()
    assert "private-invitation-value" not in page.selected_invite.text()
    sid = window.current_store
    page.repository.edit_invite(sid, rid, "edited-private-invitation")
    page.refresh()
    page.check_visible()
    assert len(page.checked_ids()) == 1
    window.store_combo.setCurrentIndex(1)
    assert page.table.rowCount() == 0
    assert "未选择" in page.selected_invite.text()
    window.store_combo.setCurrentIndex(0)
    page.table.setCurrentCell(0, 0)
    page.delete()
    assert not window.db.get_setting(f"selected_invite/{sid}")
    assert "未选择" in page.selected_invite.text()


def test_stock_filters_switching_and_sync_guard(window):
    page = window.inventory_page
    page.import_text("first-resource\nsecond-resource")
    page.check_visible()
    page.search.setText("second")
    assert len(page.checked_ids()) == 1
    window.store_combo.setCurrentIndex(1)
    assert page.table.rowCount() == 0 and page.checked_ids() == []
    window.store_combo.setCurrentIndex(0)
    assert page.checked_ids() == [] and page.table.rowCount() == 2
    window.sync_worker = object()
    try:
        page.import_text("blocked-resource")
        assert page.table.rowCount() == 2
    finally:
        window.sync_worker = None
    window.db.archive_store(window.current_store)
    window.current_archived = True
    page.import_text("blocked-resource")
    assert page.table.rowCount() == 2


def test_import_decline_and_invalid_input(window, monkeypatch):
    page = window.inventory_page
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.import_text("declined")
    assert page.table.rowCount() == 0
    with pytest.raises(ValueError):
        page.import_text("\n")
    with pytest.raises(ValueError):
        page.import_text("x" * (20 * 1024 * 1024 + 1))


def test_restore_refreshes_invite_and_interrupts_task(window, tmp_path):
    page = window.inventory_page
    page.import_text("resource-for-restore")
    page.check_visible()
    page.start()
    sid = window.current_store
    backup = window.backups.create(tmp_path / "restore.skybackup")
    page.task_table.setCurrentCell(0, 0)
    page.finish("succeeded")
    window.backups.restore(backup)
    window._refresh_all()
    assert page.task_table.item(0, 1).text() == "已中断"
    assert Inventory(window.db).list(sid)[0]["state"] == "available"

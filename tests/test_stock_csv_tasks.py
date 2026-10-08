import os

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from skyagent_manager.db import StoreDatabase
from skyagent_manager.imports import CsvRow, read_csv
from skyagent_manager.inventory import Inventory
from skyagent_manager.ledger_dialogs import ImportPreviewDialog
from skyagent_manager.main_window import MainWindow


@pytest.fixture
def context(tmp_path):
    db = StoreDatabase(tmp_path / "stock.db", key=b"k" * 32)
    sid = db.add_store("A")
    yield db, sid
    db.close()


def test_csv_rows_status_note_duplicates_and_physical_lines(context, tmp_path):
    db, sid = context
    path = tmp_path / "stock.csv"
    path.write_text(
        'kind,value,state,note\ninvite,private-code,available,"first\nsecond"\ngift,used-gift,used,\ngift,used-gift,available,\ngift,blocked,reserved,\n',
        encoding="utf-8",
        newline="",
    )
    stock = Inventory(db)
    report = stock.import_rows(sid, read_csv(path, "stock"))
    assert report.inserted == 2 and report.skipped == 2
    assert [r.row_number for r in report.rows] == [2, 4, 5, 6]
    invite = next(row for row in stock.list(sid) if row["kind"] == "invite")
    assert stock.invite_note(sid, invite["id"]) == "first\nsecond"
    assert "private-code" not in repr(report) and "used-gift" not in repr(report)
    assert b"private-code" not in db.path.read_bytes()


def test_invalid_row_report_and_persist_rollback(context, monkeypatch):
    db, sid = context
    stock = Inventory(db)
    report = stock.import_rows(
        sid,
        [
            CsvRow({"kind": "unknown", "value": "private"}, 8),
            {"kind": "gift", "value": "code", "note": "not-supported"},
        ],
    )
    assert report.skipped == 2 and not stock.list(sid)

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        stock.import_rows(sid, [{"kind": "gift", "value": "new"}])
    assert not stock.list(sid)


def test_partial_success_then_cancel_keeps_consumed_resource(context):
    db, sid = context
    stock = Inventory(db)
    stock.import_values(sid, "gift", ["one", "two"])
    ids = [r["id"] for r in stock.list(sid)]
    task = stock.start(sid, ids)
    stock.complete_item(sid, task, ids[0], succeeded=True)
    assert stock.tasks(sid)[0]["completed"] == 1
    stock.finish(sid, task, "canceled")
    assert [r["state"] for r in stock.list(sid)] == ["used", "available"]
    with pytest.raises(ValueError):
        stock.complete_item(sid, task, ids[1], succeeded=True)


def test_mixed_results_complete_and_duplicate_callbacks_rejected(context):
    db, sid = context
    stock = Inventory(db)
    stock.import_values(sid, "breakfast", ["one", "two"])
    ids = [r["id"] for r in stock.list(sid)]
    task = stock.start(sid, ids)
    stock.complete_item(sid, task, ids[0], succeeded=False)
    with pytest.raises(ValueError):
        stock.complete_item(sid, task, ids[0], succeeded=True)
    with pytest.raises(ValueError):
        stock.finish(sid, task, "succeeded")
    stock.complete_item(sid, task, ids[1], succeeded=True)
    assert stock.tasks(sid)[0]["state"] == "failed"
    assert stock.tasks(sid)[0]["completed"] == 2


def test_restart_releases_only_pending_items(context):
    db, sid = context
    stock = Inventory(db)
    stock.import_values(sid, "gift", ["one", "two"])
    ids = [r["id"] for r in stock.list(sid)]
    task = stock.start(sid, ids)
    stock.complete_item(sid, task, ids[0], succeeded=True)
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        assert [r["state"] for r in Inventory(reopened).list(sid)] == [
            "used",
            "available",
        ]
        assert Inventory(reopened).tasks(sid)[0]["state"] == "interrupted"
    finally:
        reopened.close()


def test_item_result_persistence_failure_rolls_back(context, monkeypatch):
    db, sid = context
    stock = Inventory(db)
    stock.import_values(sid, "gift", ["one"])
    rid = stock.list(sid)[0]["id"]
    task = stock.start(sid, [rid])

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        stock.complete_item(sid, task, rid, succeeded=True)
    assert stock.list(sid)[0]["state"] == "reserved"
    assert stock.task_items(sid, task)[0]["state"] == "reserved"


def test_gui_csv_import_and_item_completion(tmp_path, monkeypatch):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "gui.db", key=b"g" * 32)
    sid = db.add_store("A")
    path = tmp_path / "input.csv"
    path.write_text(
        "kind,value,state,note\ngift,resource,available,\ninvite,invitation,available,note\n"
    )
    window = MainWindow(db.path, database=db)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(ImportPreviewDialog, "exec", lambda self: QDialog.Accepted)
    try:
        page = window.inventory_page
        page.import_file()
        assert page.reports[sid].inserted == 2
        page.kind.setCurrentIndex(page.kind.findData("gift"))
        page.check_visible()
        page.start()
        page.task_table.setCurrentCell(0, 0)
        assert page.item_table.rowCount() == 1
        page.item_table.setCurrentCell(0, 0)
        page.complete_item(True)
        assert page.task_table.item(0, 1).text() == "模拟成功"
        assert page.task_table.item(0, 4).text() == "1 / 1"
        assert not errors
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("switch_store", [False, True])
def test_preview_masking_cancel_and_store_change(tmp_path, monkeypatch, switch_store):
    from PySide6.QtWidgets import QTableWidget

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "preview.db", key=b"p" * 32)
    sid = db.add_store("A")
    db.add_store("B")
    window = MainWindow(db.path, database=db)
    rows = [
        CsvRow(
            {
                "kind": "invite",
                "value": "private-resource-value",
                "state": "used",
                "note": "private-sensitive-note",
            },
            4,
        )
    ]

    def preview(self):
        table = self.findChild(QTableWidget)
        text = " ".join(table.item(0, col).text() for col in range(table.columnCount()))
        assert (
            "private-resource-value" not in text
            and "private-sensitive-note" not in text
        )
        assert "已使用" in text and table.item(0, 0).text() == "4"
        if switch_store:
            window.store_combo.setCurrentIndex(1)
            return QDialog.Accepted
        return QDialog.Rejected

    monkeypatch.setattr(ImportPreviewDialog, "exec", preview)
    try:
        if switch_store:
            with pytest.raises(ValueError, match="门店已变化"):
                window.inventory_page.preview_csv(rows)
        else:
            assert not window.inventory_page.preview_csv(rows)
        assert not Inventory(db).list(sid)
    finally:
        window.close()
        app.processEvents()

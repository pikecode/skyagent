import csv
import os

import pytest
from PySide6.QtWidgets import QApplication

from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.main_window import MainWindow


@pytest.fixture
def db(tmp_path):
    database = StoreDatabase(tmp_path / "manager.db", key=b"e" * 32)
    database.add_store("A")
    yield database
    database.close()


def test_notes_multiselect_delete_and_portable_restore(db, tmp_path):
    sid = db.list_stores()[0]["id"]
    stock = Inventory(db)
    stock.import_values(sid, "invite", ["first-invite", "second-invite"])
    ids = [row["id"] for row in stock.list(sid)]
    stock.edit_invite(sid, ids[0], "first-invite", "private-note")
    stock.select_invites(sid, ids)
    assert stock.selected_invites(sid) == ids
    assert b"private-note" not in db.path.read_bytes()
    backup = BackupService(db).create_portable(
        tmp_path / "invites.skyportable", "test-long-backup-password"
    )
    destination = StoreDatabase(tmp_path / "destination.db", key=b"z" * 32)
    try:
        BackupService(destination).restore(backup, "test-long-backup-password")
        assert Inventory(destination).selected_invites(sid) == ids
        assert Inventory(destination).invite_note(sid, ids[0]) == "private-note"
    finally:
        destination.close()
    stock.delete(sid, ids[0])
    assert stock.selected_invites(sid) == [ids[1]]
    assert not stock.invite_note(sid, ids[0])
    assert db.get_setting(f"selected_invite/{sid}") == ids[1]


def test_multiselect_validation_and_legacy_single_selection(db):
    sid = db.list_stores()[0]["id"]
    other = db.add_store("B")
    stock = Inventory(db)
    stock.import_values(sid, "invite", ["first-invite"])
    rid = stock.list(sid)[0]["id"]
    with db.connection:
        db.connection.execute(
            "INSERT INTO settings VALUES(?,?)", (f"selected_invite/{sid}", rid)
        )
    assert stock.selected_invites(sid) == [rid]
    for target, ids in [(other, [rid]), (sid, [rid, rid]), (sid, ["missing"])]:
        with pytest.raises(ValueError):
            stock.select_invites(target, ids)
    assert stock.selected_invites(sid) == [rid]
    stock.select_invites(sid, [])
    assert not stock.selected_invites(sid)


def test_note_validation_and_transaction_rollback(db, monkeypatch):
    sid = db.list_stores()[0]["id"]
    stock = Inventory(db)
    stock.import_values(sid, "invite", ["first-invite"])
    rid = stock.list(sid)[0]["id"]
    with pytest.raises(ValueError):
        stock.edit_invite(sid, rid, "changed", "x" * 2001)

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        stock.edit_invite(sid, rid, "changed", "note")
    assert stock.list(sid)[0]["value"] == "first-invite"
    assert not stock.invite_note(sid, rid)
    with pytest.raises(OSError):
        stock.select_invites(sid, [rid])
    assert not stock.selected_invites(sid)


def test_ui_export_mask_full_formula_and_filtered_multiselect(db, tmp_path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    sid = db.list_stores()[0]["id"]
    stock = Inventory(db)
    stock.import_values(sid, "invite", ["private-invite-code", "=SUM(1,2)"])
    ids = [row["id"] for row in stock.list(sid)]
    stock.edit_invite(sid, ids[0], "private-invite-code", "@formula-note")
    window = MainWindow(db.path, database=db)
    try:
        page = window.inventory_page
        page.kind.setCurrentIndex(page.kind.findData("invite"))
        page.check_visible()
        page.select_invite(False)
        assert len(stock.selected_invites(sid)) == 2
        assert "已选 2 条" in page.selected_invite.text()
        masked = tmp_path / "masked.csv"
        page.export_to(masked)
        assert "private-invite-code" not in masked.read_text(encoding="utf-8-sig")
        full = tmp_path / "full.csv"
        page.export_to(full, full=True)
        with full.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows[0]["value"] == "private-invite-code"
        assert rows[0]["note"] == "'@formula-note"
        assert rows[1]["value"] == "'=SUM(1,2)"
        page.search.setText("formula-note")
        assert page.table.rowCount() == 1
        page.export_to(full, full=True)
        with full.open(encoding="utf-8-sig", newline="") as stream:
            assert len(list(csv.DictReader(stream))) == 1
        with pytest.raises(ValueError):
            page.export_to(db.path, full=True)
    finally:
        window.close()
        app.processEvents()

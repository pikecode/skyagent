import sqlite3

import pytest
from PySide6.QtCore import QLockFile, Qt
from PySide6.QtWidgets import QDialog
from test_legacy_migration import db as db
from test_ui import app as app

from skyagent_manager.main import open_window
from skyagent_manager.main_window import MainWindow
from skyagent_manager.store_selector import StoreSelectorDialog, create_startup_window
from skyagent_manager.sync import validate_config


def test_selector_readonly_last_store_and_target(app, db, monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("selector must not use credentials/network")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    db.configure_store(
        b, "B", validate_config("https://test.example.invalid", "", "", False)
    )
    db.remember_store(b)
    before = db.path.read_bytes()
    dialog = StoreSelectorDialog(db)
    assert dialog.current_store()["id"] == b
    assert "https://test.example.invalid" in dialog.details.text()
    assert "/api/open/pool/accounts/bulk-import" in dialog.details.text()
    dialog.store_list.setCurrentRow(
        next(
            i
            for i in range(dialog.store_list.count())
            if dialog.store_list.item(i).data(Qt.UserRole) == a
        )
    )
    assert "尚未配置" in dialog.details.text()
    dialog.reject()
    assert dialog.selected_store_id is None and db.get_setting("last_store_id") == b
    assert db.path.read_bytes() == before


def test_archived_store_not_enterable_with_active_stores(app, db):
    archived = db.add_store("Z archived")
    db.archive_store(archived)
    dialog = StoreSelectorDialog(db)
    assert dialog.store_list.count() == 1
    dialog.show_archived.setChecked(True)
    assert dialog.store_list.count() == 2
    dialog.store_list.setCurrentRow(1)
    assert not dialog.enter_button.isEnabled()
    dialog.enter()
    assert dialog.selected_store_id is None and dialog.result() != QDialog.Accepted


@pytest.mark.parametrize("empty", [True, False])
def test_no_active_store_management_restore_entry(app, db, empty):
    for store in db.list_stores():
        db.archive_store(store["id"])
    dialog = StoreSelectorDialog(db)
    if not empty:
        dialog.show_archived.setChecked(True)
    assert dialog.enter_button.isEnabled() and "备份恢复" in dialog.enter_button.text()
    dialog.enter()
    assert dialog.selected_store_id == "" and dialog.result() == QDialog.Accepted


def test_revalidate_archived_after_selector_display(app, db):
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    db.remember_store(a)
    dialog = StoreSelectorDialog(db)
    assert dialog.current_store()["id"] == a
    db.archive_store(a)
    dialog.enter()
    assert dialog.selected_store_id is None and not dialog.enter_button.isEnabled()
    assert b in [store["id"] for store in db.list_stores()]


def test_main_window_initial_store_is_not_first_or_last(app, db):
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    db.remember_store(a)
    db.add_member(b, "B member", "13812345678")
    window = MainWindow(db.path, database=db, initial_store_id=b)
    try:
        assert window.current_store == b
        assert db.get_setting("last_store_id") == b
        assert window.members_table.rowCount() == 1
    finally:
        window.close()


@pytest.mark.parametrize("mode", ["cancel", "accept", "invalid", "archived"])
def test_startup_factory_closes_or_transfers_database(app, db, monkeypatch, mode):
    import skyagent_manager.db as database_module

    selected = db.add_store("B")
    if mode == "archived":
        db.archive_store(selected)
    monkeypatch.setattr(database_module, "StoreDatabase", lambda path: db)

    def choose(self):
        if mode == "cancel":
            return QDialog.Rejected
        self.selected_store_id = "missing" if mode == "invalid" else selected
        return QDialog.Accepted

    monkeypatch.setattr(StoreSelectorDialog, "exec", choose)
    if mode in {"invalid", "archived"}:
        with pytest.raises(ValueError):
            create_startup_window(db.path)
        with pytest.raises(sqlite3.ProgrammingError):
            db.connection.execute("SELECT 1")
    elif mode == "cancel":
        assert create_startup_window(db.path) is None
        with pytest.raises(sqlite3.ProgrammingError):
            db.connection.execute("SELECT 1")
        assert not (db.path.parent / "backups").exists()
    else:
        window = create_startup_window(db.path)
        assert window.current_store == selected
        assert db.connection.execute("SELECT 1").fetchone()[0] == 1
        window.close()


def test_startup_cancel_holds_then_releases_instance_lock(app, db, monkeypatch):
    import skyagent_manager.db as database_module

    monkeypatch.setattr(database_module, "StoreDatabase", lambda path: db)

    def decline(self):
        second = QLockFile(str(db.path.parent / "manager.lock"))
        assert not second.tryLock(0)
        return QDialog.Rejected

    monkeypatch.setattr(StoreSelectorDialog, "exec", decline)
    assert open_window(db.path) is None
    second = QLockFile(str(db.path.parent / "manager.lock"))
    assert second.tryLock(0)
    second.unlock()


def test_plain_text_store_name_not_html(app, db):
    sid = db.add_store("<b>literal</b>")
    db.remember_store(sid)
    dialog = StoreSelectorDialog(db)
    assert dialog.details.textFormat() == Qt.PlainText
    assert "<b>literal</b>" in dialog.details.text()

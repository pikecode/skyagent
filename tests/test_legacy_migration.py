import json
import os

import pytest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
)

from skyagent_manager.accounts import Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.legacy_migration import apply_plan, preview_workspace
from skyagent_manager.main_window import MainWindow
from skyagent_manager.migration_dialog import MigrationDialog

TOKEN = "synthetic-legacy-account-token"


def write_json(directory, filename, data):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def db(tmp_path):
    database = StoreDatabase(tmp_path / "target" / "manager.db", key=b"j" * 32)
    database.add_store("Target")
    yield database
    database.close()


@pytest.fixture
def workspace(tmp_path):
    directory = tmp_path / "old-store"
    write_json(
        directory,
        "params_register.json",
        {
            "inventories": {
                "silver": [{"value": "private-stock-code", "status": "成功√"}],
                "breakfast": ["available-breakfast"],
                "upgrade": [],
                "delay": [],
                "gift": [],
            },
            "invite_code_library": [
                {"code": "private-invite-code", "note": "unmigrated-note"}
            ],
            "secret": "must-not-migrate-secret",
            "auto_import": True,
        },
    )
    write_json(
        directory,
        "results_register.json",
        [["COM1", "13812345678", TOKEN, "成功√", "", "", "", "", "", "已入库√", ""]],
    )
    return directory


def test_preview_is_read_only_and_masked(db, workspace):
    original = {p.name: p.read_bytes() for p in workspace.iterdir()}
    before = db.path.read_bytes()
    plan = preview_workspace(workspace)
    assert len(plan.rows) == 4
    assert TOKEN not in repr(plan) and TOKEN not in repr(plan.rows)
    assert all(
        "private-" not in row.masked() and "13812345678" not in row.masked()
        for row in plan.rows
    )
    assert db.path.read_bytes() == before
    assert original == {p.name: p.read_bytes() for p in workspace.iterdir()}


def test_migrate_atomic_dedup_used_state_and_backup(db, workspace):
    sid = db.list_stores()[0]["id"]
    source = {p.name: p.read_bytes() for p in workspace.iterdir()}
    plan = preview_workspace(workspace)
    report, safety = apply_plan(db, sid, plan, authorized=True)
    assert report.inserted == 4 and report.skipped == 0
    invite = next(row for row in Inventory(db).list(sid) if row["kind"] == "invite")
    assert Inventory(db).invite_note(sid, invite["id"]) == "unmigrated-note"
    assert BackupService(db).inspect(safety)[1]["accounts"] == 0
    account = Accounts(db).list(sid)[0]
    assert account["token"] == TOKEN and account["is_new_user"] is None
    assert account["import_state"] is None
    assert (
        next(r for r in Inventory(db).list(sid) if r["kind"] == "silver")["state"]
        == "used"
    )
    assert b"must-not-migrate-secret" not in db.connection.serialize()
    assert TOKEN.encode() not in db.path.read_bytes()
    again, _ = apply_plan(db, sid, plan, authorized=True)
    assert again.inserted == 0 and again.skipped == 4
    assert source == {p.name: p.read_bytes() for p in workspace.iterdir()}
    assert "private-" not in str(report) and TOKEN not in str(report)


def test_changed_source_and_authorization_guard(db, workspace):
    sid = db.list_stores()[0]["id"]
    plan = preview_workspace(workspace)
    with pytest.raises(ValueError, match="授权"):
        apply_plan(db, sid, plan)
    write_json(workspace, "results_register.json", [])
    with pytest.raises(ValueError, match="变化"):
        apply_plan(db, sid, plan, authorized=True)
    assert not Inventory(db).list(sid)
    assert not (db.path.parent / "backups").exists()


def test_conflict_rolls_back_all_rows(db, workspace):
    sid = db.list_stores()[0]["id"]
    Inventory(db).import_values(sid, "silver", ["private-stock-code"])
    with pytest.raises(ValueError, match="状态冲突"):
        apply_plan(db, sid, preview_workspace(workspace), authorized=True)
    assert len(Inventory(db).list(sid)) == 1
    assert not Accounts(db).list(sid)
    assert list((db.path.parent / "backups").glob("before-json-migration-*.skybackup"))


def test_account_conflict_after_stock_inserts_rolls_back(db, workspace):
    from skyagent_manager.accounts import AccountInput

    sid = db.list_stores()[0]["id"]
    Accounts(db).add(sid, AccountInput("13812345678", "different-synthetic-token"))
    with pytest.raises(ValueError, match="对应关系冲突"):
        apply_plan(db, sid, preview_workspace(workspace), authorized=True)
    assert not Inventory(db).list(sid)
    assert Accounts(db).list(sid)[0]["token"] == "different-synthetic-token"


@pytest.mark.parametrize(
    "data", ['{"inventories":{},"inventories":{}}', "{", "NaN", '"text"']
)
def test_reject_invalid_json(tmp_path, data):
    directory = tmp_path / "legacy"
    directory.mkdir()
    (directory / "params_register.json").write_text(data)
    with pytest.raises(ValueError):
        preview_workspace(directory)


def test_legacy_pools_and_unknown_status(tmp_path):
    write_json(
        tmp_path,
        "params_register.json",
        {
            "silver_card_pool": ["silver"],
            "share_codes": "gift-one,gift-two",
            "invite_code": "invite",
        },
    )
    assert len(preview_workspace(tmp_path).rows) == 4
    write_json(
        tmp_path,
        "params_register.json",
        {"inventories": {"gift": [{"value": "code", "status": "处理中"}]}},
    )
    plan = preview_workspace(tmp_path)
    assert not plan.rows and len(plan.skipped) == 1


def test_reject_links_and_multiple_store_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(ValueError, match="具体旧门店"):
        preview_workspace(root)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (root / "params_register.json").symlink_to(outside)
    with pytest.raises(ValueError):
        preview_workspace(root)


def test_persistence_failure_rolls_back(db, workspace, monkeypatch):
    sid = db.list_stores()[0]["id"]

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        apply_plan(db, sid, preview_workspace(workspace), authorized=True)
    assert not Inventory(db).list(sid) and not Accounts(db).list(sid)


@pytest.mark.parametrize("mode", ["replace", "link", "ancestor-link"])
def test_single_directory_identity_rechecked(db, workspace, mode):
    import shutil

    plan = preview_workspace(workspace)
    target = workspace.parent if mode == "ancestor-link" else workspace
    moved = target.with_name(target.name + "-original")
    target.rename(moved)
    if mode == "replace":
        shutil.copytree(moved, target)
    else:
        target.symlink_to(moved, target_is_directory=True)
    before = db.path.read_bytes()
    with pytest.raises(ValueError, match="目录"):
        apply_plan(db, db.list_stores()[0]["id"], plan, authorized=True)
    assert db.path.read_bytes() == before
    assert not (db.path.parent / "backups").exists()


def test_migration_dialog_and_main_window_flow(tmp_path, workspace, monkeypatch):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "ui.db", key=b"m" * 32)
    sid = database.add_store("A")
    database.add_store("B")
    window = MainWindow(database.path, database=database)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    try:
        dialog = MigrationDialog(
            preview_workspace(workspace), database.list_stores(), sid
        )
        save = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Save)
        assert not save.isEnabled()
        dialog.authorized.setChecked(True)
        assert save.isEnabled() and dialog.target.currentData() == sid
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda *args: str(workspace)
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *args: QMessageBox.Ok)

        def confirm(self):
            if isinstance(self, MigrationDialog):
                self.authorized.setChecked(True)
                self.target.setCurrentIndex(1)
            return QDialog.Accepted

        monkeypatch.setattr(QDialog, "exec", confirm)
        window._migrate_json()
        assert not errors
        assert window.current_store != sid
        assert len(Accounts(database).list(window.current_store)) == 1
        assert not Accounts(database).list(sid)
        assert window.account_page.table.rowCount() == 1
    finally:
        window.close()
        app.processEvents()


def test_size_and_record_limits(workspace, monkeypatch):
    import skyagent_manager.legacy_migration as migration

    monkeypatch.setattr(migration, "MAX_ROWS", 2)
    with pytest.raises(ValueError, match="20000"):
        preview_workspace(workspace)
    monkeypatch.setattr(migration, "MAX_BYTES", 4)
    with pytest.raises(ValueError, match="20 MiB"):
        preview_workspace(workspace)


def test_invalid_account_row_is_skipped_without_exposure(db, tmp_path):
    write_json(
        tmp_path,
        "results_register.json",
        [
            ["COM1", "13812345678", "private-invalid-token!"],
            ["COM2", "13912345678", TOKEN],
        ],
    )
    plan = preview_workspace(tmp_path)
    assert len(plan.rows) == 1 and len(plan.skipped) == 1
    sid = db.list_stores()[0]["id"]
    report, _ = apply_plan(db, sid, plan, authorized=True)
    assert report.inserted == 1 and report.skipped == 1
    assert "13812345678" not in str(report) and "private-invalid" not in str(report)


def test_archived_destination_is_rejected(db, workspace):
    sid = db.list_stores()[0]["id"]
    db.archive_store(sid)
    with pytest.raises(ValueError):
        apply_plan(db, sid, preview_workspace(workspace), authorized=True)
    assert not Inventory(db).list(sid)


def test_new_source_file_after_preview_is_rejected(db, tmp_path):
    write_json(tmp_path, "params_register.json", {"invite_code": "invite"})
    plan = preview_workspace(tmp_path)
    write_json(tmp_path, "results_register.json", [])
    with pytest.raises(ValueError, match="变化"):
        apply_plan(db, db.list_stores()[0]["id"], plan, authorized=True)

import pytest
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox
from test_backend_query_ui import context as context
from test_legacy_migration import db as db
from test_legacy_migration import write_json

from skyagent_manager.accounts import Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.inventory import Inventory
from skyagent_manager.ledger_dialogs import ImportReportDialog
from skyagent_manager.legacy_migration import apply_workspace_batch, preview_workspaces
from skyagent_manager.migration_dialog import WorkspaceBatchDialog


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "old-stores"
    for name in ("a", "b"):
        write_json(
            root / name,
            "params_register.json",
            {
                "inventories": {"gift": [f"synthetic-stock-{name}"]},
                "secret": "discard-secret",
            },
        )
    return root


def test_batch_preview_and_atomic_success(db, root):
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    before = db.path.read_bytes()
    originals = {path: path.read_bytes() for path in root.glob("*/*.json")}
    batch = preview_workspaces(root)
    assert len(batch.plans) == 2 and "synthetic-stock" not in repr(batch)
    assert db.path.read_bytes() == before
    config = [db.get_store_sync(sid) for sid in (a, b)]
    report, safety = apply_workspace_batch(db, batch, [(0, a), (1, b)], authorized=True)
    assert report.inserted == 2 and report.skipped == 0
    assert Inventory(db).list(a)[0]["value"] == "synthetic-stock-a"
    assert Inventory(db).list(b)[0]["value"] == "synthetic-stock-b"
    assert [db.get_store_sync(sid) for sid in (a, b)] == config
    assert BackupService(db).inspect(safety)[1]["stock"] == 0
    assert len(list((db.path.parent / "backups").glob("before-json-migration-*"))) == 1
    assert "synthetic-stock" not in str(
        report
    ) and "discard-secret" not in db.connection.serialize().decode("latin1")
    assert originals == {path: path.read_bytes() for path in root.glob("*/*.json")}
    again, _ = apply_workspace_batch(db, batch, [(0, a), (1, b)], authorized=True)
    assert again.inserted == 0 and again.skipped == 2


def test_batch_second_store_conflict_rolls_back_first(db, root):
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    Inventory(db).import_values(b, "gift", ["synthetic-stock-b"])
    write_json(
        root / "b",
        "params_register.json",
        {"inventories": {"gift": [{"value": "synthetic-stock-b", "status": "成功√"}]}},
    )
    before = db.path.read_bytes()
    with pytest.raises(ValueError, match="状态冲突"):
        apply_workspace_batch(
            db, preview_workspaces(root), [(0, a), (1, b)], authorized=True
        )
    assert not Inventory(db).list(a)
    assert Inventory(db).list(b)[0]["state"] == "available"
    assert db.path.read_bytes() == before
    assert len(list((db.path.parent / "backups").glob("before-json-migration-*"))) == 1


@pytest.mark.parametrize("mode", ["child-replaced", "root-replaced", "ancestor-link"])
def test_directory_replacement_rejected_even_with_identical_files(db, root, mode):
    import shutil

    batch = preview_workspaces(root)
    target = root / "a" if mode == "child-replaced" else root
    moved = target.with_name(target.name + "-original")
    target.rename(moved)
    if mode == "ancestor-link":
        target.symlink_to(moved, target_is_directory=True)
    else:
        shutil.copytree(moved, target)
    before = db.path.read_bytes()
    with pytest.raises(ValueError, match="目录"):
        apply_workspace_batch(
            db, batch, [(0, db.list_stores()[0]["id"])], authorized=True
        )
    assert db.path.read_bytes() == before
    assert not (db.path.parent / "backups").exists()


def test_batch_persistence_failure_rolls_back_all_stores(db, root, monkeypatch):
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    batch = preview_workspaces(root)
    before = db.path.read_bytes()
    memory = db.connection.serialize()

    def fail():
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        apply_workspace_batch(db, batch, [(0, a), (1, b)], authorized=True)
    assert db.connection.serialize() == memory
    assert db.path.read_bytes() == before
    assert not Inventory(db).list(a) and not Inventory(db).list(b)
    safety = list((db.path.parent / "backups").glob("before-json-migration-*"))
    assert len(safety) == 1
    assert BackupService(db).inspect(safety[0])[1]["stock"] == 0


@pytest.mark.parametrize(
    "mode",
    ["auth", "duplicate", "archived", "changed", "new-directory", "missing-file-added"],
)
def test_batch_guards_before_backup(db, root, mode):
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    batch = preview_workspaces(root)
    mappings = [(0, a), (1, b)]
    if mode == "duplicate":
        mappings = [(0, a), (1, a)]
    elif mode == "archived":
        db.archive_store(b)
    elif mode == "changed":
        write_json(
            root / "b", "params_register.json", {"inventories": {"gift": ["changed"]}}
        )
    elif mode == "new-directory":
        write_json(root / "c", "params_register.json", {})
    elif mode == "missing-file-added":
        write_json(root / "a", "results_register.json", [])
    before = db.path.read_bytes()
    with pytest.raises(ValueError):
        apply_workspace_batch(db, batch, mappings, authorized=mode != "auth")
    assert db.path.read_bytes() == before
    assert not (db.path.parent / "backups").exists()


def test_batch_discovery_is_shallow_and_ignores_link_directories(root):
    write_json(root / "ignored" / "nested", "params_register.json", {})
    (root / "linked").symlink_to(root / "a", target_is_directory=True)
    assert [plan.directory.name for plan in preview_workspaces(root).plans] == [
        "a",
        "b",
    ]
    (root / "a" / "params_register.json").unlink()
    (root / "a" / "params_register.json").symlink_to(
        root / "b" / "params_register.json"
    )
    with pytest.raises(ValueError):
        preview_workspaces(root)


def test_batch_total_record_limit(root, monkeypatch):
    import skyagent_manager.legacy_migration as migration

    monkeypatch.setattr(migration, "MAX_ROWS", 1)
    with pytest.raises(ValueError, match="20000"):
        preview_workspaces(root)


def test_batch_total_byte_limit(root, monkeypatch):
    import skyagent_manager.legacy_migration as migration

    write_json(
        root / "c",
        "params_register.json",
        {"inventories": {"gift": ["synthetic-stock-c"]}, "secret": "discard-secret"},
    )
    largest = max(path.stat().st_size for path in root.glob("*/*.json"))
    monkeypatch.setattr(migration, "MAX_BYTES", largest + 1)
    with pytest.raises(ValueError, match="合计最多40"):
        preview_workspaces(root)


def test_batch_accounts_keep_unknown_eligibility_and_no_import_status(db, root):
    a = db.list_stores()[0]["id"]
    b = db.add_store("B")
    for name, phone in (("a", "13812345678"), ("b", "13912345678")):
        write_json(
            root / name,
            "results_register.json",
            [["COM1", phone, f"synthetic-batch-token-{name}", "成功√", "已入库√"]],
        )
    report, _ = apply_workspace_batch(
        db, preview_workspaces(root), [(0, a), (1, b)], authorized=True
    )
    assert report.inserted == 4
    for sid in (a, b):
        account = Accounts(db).list(sid)[0]
        assert account["is_new_user"] is None and account["import_state"] is None
        assert account["token"].encode() not in db.path.read_bytes()


def test_batch_dialog_explicit_mapping_and_confirmation(context, root):
    _app, window, _client, _errors = context
    dialog = WorkspaceBatchDialog(preview_workspaces(root), window.db.list_stores())
    assert not dialog.mappings() and not dialog.save_button.isEnabled()
    dialog.authorized.setChecked(True)
    assert not dialog.save_button.isEnabled()
    dialog.targets[0].setCurrentIndex(1)
    assert dialog.save_button.isEnabled()
    dialog.targets[1].setCurrentIndex(1)
    assert not dialog.save_button.isEnabled()
    dialog.targets[1].setCurrentIndex(2)
    assert dialog.save_button.isEnabled()
    dialog.close()


@pytest.mark.parametrize("accepted", [False, True])
def test_batch_ui_no_automatic_mapping_or_upload(context, root, monkeypatch, accepted):
    _app, window, client, errors = context
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(root))

    def preview(dialog):
        assert not dialog.mappings()
        if accepted:
            dialog.targets[0].setCurrentIndex(1)
            dialog.targets[1].setCurrentIndex(2)
            dialog.authorized.setChecked(True)
        return QDialog.Accepted if accepted else QDialog.Rejected

    monkeypatch.setattr(WorkspaceBatchDialog, "exec", preview)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(ImportReportDialog, "exec", lambda *args: QDialog.Rejected)
    before = window.db.path.read_bytes()
    window._migrate_multi_json()
    assert not errors and not client.pages and window.sync_worker is None
    if accepted:
        assert "新增 2" in window.status.text()
        for store in window.db.list_stores():
            assert len(Inventory(window.db).list(store["id"])) == 1
    else:
        assert window.db.path.read_bytes() == before

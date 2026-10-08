from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QLineEdit,
    QMessageBox,
    QTableWidget,
)

from skyagent_manager.account_page import AccountDialog
from skyagent_manager.accounts import AccountInput, Accounts, parse_account_text
from skyagent_manager.backup import BackupService
from skyagent_manager.contract_check import local_backend
from skyagent_manager.db import StoreDatabase
from skyagent_manager.imports import read_csv
from skyagent_manager.main_window import MainWindow
from skyagent_manager.security import encrypt
from skyagent_manager.sync import (
    ACCOUNT_PATH,
    AccountImportClient,
    ItemResult,
    SyncWorker,
    validate_config,
)

TOKEN = "synthetic-authorized-token-value"


@pytest.fixture
def db(tmp_path):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"c" * 32)
    yield database
    database.close()


def test_account_encryption_and_restart(db):
    sid = db.add_store("A")
    account = AccountInput("13812345678", TOKEN, "COM1", is_new_user=False)
    rid = Accounts(db).add(sid, account)
    assert TOKEN not in repr(account) and "13812345678" not in repr(account)
    assert TOKEN.encode() not in db.path.read_bytes()
    reopened = StoreDatabase(db.path, key=b"c" * 32)
    try:
        assert Accounts(reopened).get(sid, rid)["token"] == TOKEN
        assert reopened.connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        reopened.close()


def test_account_duplicates_store_isolation_and_unknown_flag(db):
    a, b = db.add_store("A"), db.add_store("B")
    repository = Accounts(db)
    rid = repository.add(a, AccountInput("13812345678", TOKEN))
    repository.add(b, AccountInput("13812345678", TOKEN, is_new_user=False))
    with pytest.raises(ValueError, match="已存在"):
        repository.add(a, AccountInput("138-1234-5678", "different-token-value"))
    with pytest.raises(ValueError):
        repository.get(b, rid)
    with pytest.raises(ValueError, match="未知"):
        repository.sync_items(a)
    assert repository.sync_items(a, []) == []
    db.archive_store(a)
    with pytest.raises(ValueError):
        repository.update(a, rid, AccountInput("13812345678", TOKEN))


def test_update_invalidates_result_and_delete_cascades(db):
    sid = db.add_store("A")
    repository = Accounts(db)
    rid = repository.add(sid, AccountInput("13812345678", TOKEN, is_new_user=False))
    created = repository.get(sid, rid)["created_at"]
    repository.save_results(sid, [ItemResult(rid, True, "已同步")])
    repository.update(
        sid,
        rid,
        AccountInput("13812345678", "replacement-token-value", is_new_user=True),
    )
    assert repository.list(sid)[0]["import_state"] is None
    assert repository.get(sid, rid)["created_at"] == created
    repository.save_results(sid, [ItemResult(rid, False, "后端拒绝")])
    repository.delete(sid, [rid])
    assert repository.list(sid) == []
    assert not db.connection.execute("SELECT * FROM account_import_results").fetchall()
    assert TOKEN not in str([dict(r) for r in db.list_activity(sid)])


@pytest.mark.parametrize("operation", ["add", "update", "delete", "import", "results"])
def test_account_disk_failure_atomic(db, monkeypatch, operation):
    sid = db.add_store("A")
    repository = Accounts(db)
    rid = repository.add(sid, AccountInput("13812345678", TOKEN, is_new_user=False))
    original = db.path.read_bytes()
    rows = [dict(r) for r in repository.list(sid)]
    audit = [dict(r) for r in db.list_activity(sid)]

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr("skyagent_manager.db.atomic_write", fail)
    with pytest.raises(OSError):
        if operation == "add":
            repository.add(sid, AccountInput("13912345678", "another-token-value"))
        elif operation == "update":
            repository.update(
                sid, rid, AccountInput("13812345678", "another-token-value")
            )
        elif operation == "delete":
            repository.delete(sid, [rid])
        elif operation == "import":
            repository.import_rows(
                sid, [{"phone": "13912345678", "token": "another-token-value"}]
            )
        else:
            repository.save_results(sid, [ItemResult(rid, True, "已同步")])
    assert db.path.read_bytes() == original
    assert [dict(r) for r in repository.list(sid)] == rows
    assert [dict(r) for r in db.list_activity(sid)] == audit


def test_text_csv_import_reports_and_privacy(db, tmp_path):
    sid = db.add_store("A")
    rows = parse_account_text(
        f"\nCOM1 13812345678 {TOKEN}\ninvalid {TOKEN}\n13812345678 {TOKEN}"
    )
    report = Accounts(db).import_rows(sid, rows)
    assert (report.inserted, report.skipped) == (1, 2)
    assert [r.row_number for r in report.rows] == [2, 3, 4]
    assert TOKEN not in repr(report) and "13812345678" not in repr(report)
    path = tmp_path / "accounts.csv"
    path.write_text(
        "phone,token,is_new_user\n13912345678,another-token-value,false\n",
        encoding="utf-8",
    )
    report = Accounts(db).import_rows(sid, read_csv(path, "accounts"))
    assert report.inserted == 1
    added = next(row for row in Accounts(db).list(sid) if row["phone"] == "13912345678")
    assert len(Accounts(db).sync_items(sid, [added["id"]])) == 1


def test_v3_upgrade_and_restore_preserve_ledger_and_add_accounts(db, tmp_path):
    sid = db.add_store("A")
    db.add_member(sid, "会员", "13812345678")
    old = sqlite3.connect(":memory:")
    old.deserialize(db.connection.serialize())
    old.executescript(
        "DROP TABLE account_import_results; DROP TABLE accounts; PRAGMA user_version=3;"
    )
    path = tmp_path / "v3.skybackup"
    path.write_bytes(encrypt(old.serialize(), db.key))
    old.close()
    restored = StoreDatabase(tmp_path / "restored.sqlite3", key=db.key)
    try:
        BackupService(restored).restore(path)
        assert restored.list_members(sid)[0]["display_name"] == "会员"
        assert Accounts(restored).list(sid) == []
        assert restored.connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        restored.close()


def test_portable_accounts_restore_with_different_key(db, tmp_path):
    sid = db.add_store("A")
    Accounts(db).add(sid, AccountInput("13812345678", TOKEN))
    portable = BackupService(db).create_portable(
        tmp_path / "accounts.skyportable", "long-backup-passphrase"
    )
    destination = StoreDatabase(tmp_path / "destination.sqlite3", key=b"z" * 32)
    try:
        BackupService(destination).restore(portable, "long-backup-passphrase")
        assert Accounts(destination).list(sid)[0]["token"] == TOKEN
        assert TOKEN.encode() not in destination.path.read_bytes()
        assert (
            BackupService(destination).inspect(portable, "long-backup-passphrase")[1][
                "accounts"
            ]
            == 1
        )
    finally:
        destination.close()


def test_real_account_http_exact_payload_and_result_mapping(db):
    sid = db.add_store("A")
    repository = Accounts(db)
    a = repository.add(sid, AccountInput("00000000000", TOKEN, is_new_user=False))
    b = repository.add(
        sid, AccountInput("00000000001", "another-token-value", is_new_user=True)
    )
    with local_backend() as (url, state):
        client = AccountImportClient(
            validate_config(url, "", "", True), "synthetic-secret"
        )
        try:
            results = client.submit(repository.sync_items(sid))
            assert {r.record_id: r.ok for r in results} == {a: True, b: False}
            repository.save_results(sid, results)
            assert repository.sync_items(sid, failed_only=True)[0]["id"] == b
            path, body = state["requests"][0]
            assert path == ACCOUNT_PATH
            assert set(body["items"][0]) == {
                "phone",
                "token",
                "is_online",
                "is_new_user",
                "is_enabled",
                "remark",
            }
        finally:
            client.close()


def test_account_worker_401_split_and_no_local_ids():
    items = [
        {
            "id": str(i),
            "phone": f"00000{i:06d}",
            "token": TOKEN,
            "is_online": True,
            "is_new_user": False,
            "is_enabled": True,
            "remark": "软件导入",
        }
        for i in range(401)
    ]
    with local_backend() as (url, state):
        client = AccountImportClient(
            validate_config(url, "", "", True), "synthetic-secret"
        )
        SyncWorker("test-store", "accounts", items, client).run()
        assert [len(body["items"]) for _, body in state["requests"]] == [200, 200, 1]
        assert all(
            "id" not in item for _, body in state["requests"] for item in body["items"]
        )


def test_account_ui_masking_authorization_selection_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"u" * 32)
    sid = database.add_store("A")
    rid = Accounts(database).add(
        sid, AccountInput("13812345678", TOKEN, is_new_user=False)
    )
    window = MainWindow(database.path, database=database)
    app.processEvents()
    page = window.account_page
    try:
        assert TOKEN not in page.table.item(0, 2).text()
        assert "13812345678" not in page.table.item(0, 1).text()
        page.table.item(0, 0).setCheckState(Qt.Checked)
        page.refresh()
        assert page.checked_ids() == [rid]
        dialog = AccountDialog(page, Accounts(database).get(sid, rid))
        assert dialog.token.echoMode() == QLineEdit.Password
        with pytest.raises(ValueError, match="授权"):
            dialog.value()
        dialog.authorized.setChecked(True)
        assert dialog.value().token == TOKEN
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
        page.delete()
        assert page.table.rowCount() == 0
    finally:
        window.close()


def test_import_hold_survives_edit_delete_reimport_and_restart(db):
    sid = db.add_store("A")
    repo = Accounts(db)
    value = AccountInput("13812345678", TOKEN, is_new_user=False)
    rid = repo.add(sid, value)
    repo.reserve_upload(sid, [rid])
    with pytest.raises(ValueError, match="禁止重复"):
        repo.sync_items(sid, [rid])
    repo.update(
        sid,
        rid,
        AccountInput(value.phone, "replacement-token-value", is_new_user=False),
    )
    assert repo.import_hold(sid, repo.get(sid, rid)) == "pending"
    repo.delete(sid, [rid])
    new_id = repo.add(sid, value)
    with pytest.raises(ValueError):
        repo.sync_items(sid, [new_id])
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        assert (
            Accounts(reopened).import_hold(sid, Accounts(reopened).get(sid, new_id))
            == "pending"
        )
    finally:
        reopened.close()


def test_only_explicit_backend_rejection_can_retry(db):
    sid = db.add_store("A")
    repo = Accounts(db)
    rid = repo.add(sid, AccountInput("13812345678", TOKEN, is_new_user=False))
    repo.reserve_upload(sid, [rid])
    repo.save_results(sid, [ItemResult(rid, False, "网络或响应异常")])
    assert repo.sync_items(sid, failed_only=True) == []
    repo.save_results(sid, [ItemResult(rid, False, "后端拒绝该记录")])
    assert len(repo.sync_items(sid, failed_only=True)) == 1
    repo.reserve_upload(sid, [rid])
    repo.save_results(sid, [ItemResult(rid, True, "已同步")])
    with pytest.raises(ValueError):
        repo.sync_items(sid, [rid])
    assert repo.sync_items(sid, failed_only=True) == []


def test_upload_hold_persist_failure_rolls_back(db, monkeypatch):
    sid = db.add_store("A")
    repo = Accounts(db)
    rid = repo.add(sid, AccountInput("13812345678", TOKEN, is_new_user=False))
    before = db.path.read_bytes()

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        repo.reserve_upload(sid, [rid])
    assert repo.import_hold(sid, repo.get(sid, rid)) == ""
    assert db.path.read_bytes() == before


def test_config_change_invalidates_account_import_result(db):
    sid = db.add_store("A")
    repository = Accounts(db)
    rid = repository.add(sid, AccountInput("13812345678", TOKEN))
    repository.save_results(sid, [ItemResult(rid, True, "已同步")])
    db.configure_store(sid, "A", validate_config("", "", "", False))
    assert repository.list(sid)[0]["import_state"] is None
    assert repository.import_hold(sid, repository.get(sid, rid)) == "success"


def test_open_v3_creates_upgrade_snapshot(db, tmp_path):
    sid = db.add_store("A")
    db.add_member(sid, "保留会员", "13812345678")
    old = sqlite3.connect(":memory:")
    old.deserialize(db.connection.serialize())
    old.executescript(
        "DROP TABLE account_import_results; DROP TABLE accounts; PRAGMA user_version=3;"
    )
    path = tmp_path / "upgrade" / "manager.sqlite3"
    path.parent.mkdir()
    path.write_bytes(encrypt(old.serialize(), db.key))
    old.close()
    upgraded = StoreDatabase(path, key=db.key)
    try:
        assert upgraded.list_members(sid)[0]["display_name"] == "保留会员"
        snapshots = list(
            (path.parent / "backups").glob("before-upgrade-v6-*.skybackup")
        )
        assert len(snapshots) == 1
        assert BackupService(upgraded).inspect(snapshots[0])[1]["accounts"] == 0
    finally:
        upgraded.close()


def test_account_import_preview_masks_and_requires_consent(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"u" * 32)
    sid = database.add_store("A")
    window = MainWindow(database.path, database=database)
    app.processEvents()
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)
    try:
        rows = parse_account_text(f"13812345678 {TOKEN}")
        window.account_page.import_rows(rows)
        assert not Accounts(database).list(sid) and errors

        def accept(dialog):
            for table in dialog.findChildren(QTableWidget):
                for row in range(table.rowCount()):
                    for col in range(table.columnCount()):
                        item = table.item(row, col)
                        if item is not None:
                            assert (
                                TOKEN not in item.text()
                                and "13812345678" not in item.text()
                            )
            for check in dialog.findChildren(QCheckBox):
                if "授权" in check.text():
                    check.setChecked(True)
            return QDialog.Accepted

        monkeypatch.setattr(QDialog, "exec", accept)
        window.account_page.import_rows(rows)
        assert Accounts(database).list(sid)[0]["token"] == TOKEN
        assert window.account_page.table.rowCount() == 1
    finally:
        window.close()


def test_success_hold_cannot_be_downgraded_and_released(db):
    sid = db.add_store("A")
    repo = Accounts(db)
    rid = repo.add(sid, AccountInput("13812345678", TOKEN, is_new_user=False))
    repo.save_results(sid, [ItemResult(rid, True, "已同步")])
    repo.save_results(sid, [ItemResult(rid, False, "网络异常")])
    repo.save_results(sid, [ItemResult(rid, False, "后端拒绝该记录")])
    assert repo.import_hold(sid, repo.get(sid, rid)) == "success"
    assert repo.sync_items(sid, failed_only=True) == []


@pytest.mark.parametrize(
    "mode,save_failure", [("normal", False), ("invalid-json", False), ("normal", True)]
)
def test_account_ui_upload_with_real_worker_and_loopback(
    tmp_path, monkeypatch, mode, save_failure
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"u" * 32)
    sid = database.add_store("A")
    rid = Accounts(database).add(
        sid, AccountInput("00000000000", TOKEN, is_new_user=False)
    )
    with local_backend() as (url, state):
        state["mode"] = mode
        database.configure_store(sid, "A", validate_config(url, "", "", True))

        class Vault:
            def get(self, account):
                return "synthetic-secret"

        database.vault = Vault()
        window = MainWindow(database.path, database=database)
        app.processEvents()
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
        errors = []
        monkeypatch.setattr(window, "_error", errors.append)
        if save_failure:

            def fail(*args):
                raise OSError("synthetic save failure")

            monkeypatch.setattr(Accounts, "save_results", fail)
        try:
            window.account_page.check_all()
            window._start_sync("accounts")
            assert not window.store_combo.isEnabled()
            loop = QEventLoop()
            poll = QTimer()
            poll.timeout.connect(
                lambda: loop.quit() if window.sync_worker is None else None
            )
            poll.start(10)
            QTimer.singleShot(5000, loop.quit)
            loop.exec()
            poll.stop()
            assert window.sync_worker is None
            assert window.store_combo.isEnabled()
            expected = (
                None if save_failure else "success" if mode == "normal" else "failed"
            )
            assert Accounts(database).list(sid)[0]["import_state"] == expected
            assert state["requests"][0][0] == ACCOUNT_PATH
            assert Accounts(database).get(sid, rid)["token"] == TOKEN
            hold = "success" if mode == "normal" and not save_failure else "pending"
            assert (
                Accounts(database).import_hold(sid, Accounts(database).get(sid, rid))
                == hold
            )
            request_count = len(state["requests"])
            window.account_page.check_all()
            window._start_sync("accounts")
            assert window.sync_worker is None
            assert len(state["requests"]) == request_count
            assert "禁止重复上传" in errors[-1]
            assert (
                "待后台核对" if hold == "pending" else "已提交"
            ) in window.account_page.table.item(0, 5).text()
        finally:
            if window.sync_worker is not None:
                window.sync_worker.requestInterruption()
                window.sync_worker.wait(45000)
                app.processEvents()
            window.close()

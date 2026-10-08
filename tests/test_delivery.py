from __future__ import annotations

import json
import sys

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.main_window import MainWindow
from skyagent_manager.self_check import run


def test_remember_store_and_backend_display(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "manager.sqlite3"
    database = StoreDatabase(path, key=b"d" * 32)
    database.add_store("第一门店")
    second = database.add_store("第二门店")
    window = MainWindow(path, database=database)
    window._reload_stores(second)
    assert database.get_setting("last_store_id") == second
    assert "尚未配置" in window.backend_target.text()
    window.close()
    app.processEvents()
    database = StoreDatabase(path, key=b"d" * 32)
    window = MainWindow(path, database=database)
    assert window.current_store == second
    window.close()
    app.processEvents()


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="native platform")
def test_offline_self_check(tmp_path, monkeypatch):
    from skyagent_manager.security import KeyVault

    def forbidden(*args, **kwargs):
        raise AssertionError("must not access native credential storage")

    monkeypatch.setattr(KeyVault, "__init__", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    report = tmp_path / "report.json"
    assert run(report) == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["ok"]
    assert "qt-window" in result["checks"]
    assert {"benefit-filter-masked-report", "offline-link-file-import"}.issubset(
        result["checks"]
    )


def test_close_waits_for_sync_then_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"c" * 32)
    database.add_store("门店")
    window = MainWindow(database.path, database=database)
    window.show()
    app.processEvents()

    class Worker:
        interrupted = False
        deleted = False

        def requestInterruption(self):
            self.interrupted = True

        def deleteLater(self):
            self.deleted = True

    worker = Worker()
    window.sync_worker = worker
    window.sync_error = False
    window.close()
    assert worker.interrupted and window.close_pending and window.isVisible()
    window._sync_finished()
    app.processEvents()
    assert worker.deleted and not window.isVisible()


def test_window_restores_portable_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    source = StoreDatabase(tmp_path / "source.sqlite3", key=b"s" * 32)
    sid = source.add_store("跨机门店")
    portable = BackupService(source).create_portable(
        tmp_path / "portable.skyportable", "ui-test-long-passphrase"
    )
    source.close()
    destination = StoreDatabase(tmp_path / "target.sqlite3", key=b"t" * 32)
    window = MainWindow(destination.path, database=destination)
    app.processEvents()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(portable), ""))
    monkeypatch.setattr(
        QInputDialog, "getText", lambda *a: ("ui-test-long-passphrase", True)
    )
    prompts = []

    def confirm(*args):
        prompts.append(args[2])
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    window._restore_backup()
    assert window.current_store == sid
    assert "恢复完成" in window.status.text()
    assert "真实分享已阻断" in window.status.text()
    assert any("持续阻断真实分享" in prompt for prompt in prompts)
    window.close()


def test_cancel_portable_password_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"t" * 32)
    sid = database.add_store("原门店")
    portable = BackupService(database).create_portable(
        tmp_path / "portable.skyportable", "ui-test-long-passphrase"
    )
    window = MainWindow(database.path, database=database)
    app.processEvents()
    original = database.path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(portable), ""))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a: ("", False))
    window._restore_backup()
    assert database.path.read_bytes() == original and window.current_store == sid
    window.close()


def test_restore_uses_backup_store_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"t" * 32)
    first = database.add_store("第一门店")
    second = database.add_store("第二门店")
    database.remember_store(second)
    backup = BackupService(database).create(tmp_path / "saved.skybackup")
    window = MainWindow(database.path, database=database)
    app.processEvents()
    window._reload_stores(first)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(backup), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
    window._restore_backup()
    assert window.current_store == second
    window.close()


def test_archived_store_settings_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"t" * 32)
    sid = database.add_store("归档门店")
    database.archive_store(sid)
    window = MainWindow(database.path, database=database)
    app.processEvents()
    window.show_archived.setChecked(True)
    window._reload_stores(sid)
    assert not window.store_settings_button.isEnabled()
    window.close()

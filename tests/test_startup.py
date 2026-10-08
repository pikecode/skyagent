from __future__ import annotations

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QFileDialog, QMessageBox

from skyagent_manager.main import data_path, open_window
from skyagent_manager.security import MissingDatabaseKeyError


def test_explicit_data_directory_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYAGENT_MANAGER_DATA_DIR", str(tmp_path / "environment"))
    assert data_path(tmp_path / "explicit") == tmp_path / "explicit" / "manager.sqlite3"
    assert data_path() == tmp_path / "environment" / "manager.sqlite3"


def test_single_instance_rejects_before_opening_database(tmp_path, monkeypatch):
    held = QLockFile(str(tmp_path / "manager.lock"))
    assert held.tryLock(0)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: messages.append(a))

    def forbidden(path):
        raise AssertionError("must not open a locked database")

    try:
        assert (
            open_window(tmp_path / "manager.sqlite3", window_factory=forbidden) is None
        )
        assert messages
    finally:
        held.unlock()


def test_open_failure_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: None)

    def fail(path):
        raise OSError("cannot open database")

    assert open_window(tmp_path / "manager.sqlite3", window_factory=fail) is None
    lock = QLockFile(str(tmp_path / "manager.lock"))
    assert lock.tryLock(0)
    lock.unlock()


def test_missing_key_recovery_preserves_original_and_locks_new_directory(
    tmp_path, monkeypatch
):
    original = tmp_path / "old" / "manager.sqlite3"
    original.parent.mkdir()
    original.write_bytes(b"original-encrypted-file")
    directory = tmp_path / "recovery"
    directory.mkdir()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(directory))
    window = object()
    calls = []

    def factory(path):
        calls.append(path)
        if path == original:
            raise MissingDatabaseKeyError("missing")
        return window

    actual, lock = open_window(original, window_factory=factory)
    try:
        assert actual is window
        assert calls == [original, directory / "manager.sqlite3"]
        assert original.read_bytes() == b"original-encrypted-file"
        old_lock = QLockFile(str(original.parent / "manager.lock"))
        assert old_lock.tryLock(0)
        old_lock.unlock()
        second = QLockFile(str(directory / "manager.lock"))
        assert not second.tryLock(0)
    finally:
        lock.unlock()


def test_nonempty_recovery_directory_rejected(tmp_path, monkeypatch):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    existing = occupied / "personal.txt"
    existing.write_text("preserve")
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(occupied))
    messages = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: messages.append(a))

    def missing(path):
        raise MissingDatabaseKeyError("missing")

    assert open_window(tmp_path / "manager.sqlite3", window_factory=missing) is None
    assert existing.read_text() == "preserve" and messages


def test_cancel_missing_key_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.No)

    def missing(path):
        raise MissingDatabaseKeyError("missing")

    assert open_window(tmp_path / "manager.sqlite3", window_factory=missing) is None
    lock = QLockFile(str(tmp_path / "manager.lock"))
    assert lock.tryLock(0)
    lock.unlock()

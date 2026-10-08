from __future__ import annotations

import pytest

from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.security import (
    PORTABLE_MAGIC,
    SecurityError,
    decrypt_portable,
    encrypt_portable,
)

PASSWORD = "independent-long-backup-passphrase"


def test_portable_restore_reencrypts_with_destination_key(tmp_path):
    source = StoreDatabase(tmp_path / "source.sqlite3", key=b"s" * 32)
    destination = StoreDatabase(tmp_path / "target.sqlite3", key=b"t" * 32)
    try:
        sid = source.add_store("迁移门店")
        source.add_member(sid, "会员", "13812345678")
        destination.add_store("恢复前门店")
        portable = BackupService(source).create_portable(
            tmp_path / "transfer.skyportable", PASSWORD
        )
        assert portable.read_bytes().startswith(PORTABLE_MAGIC)
        assert b"13812345678" not in portable.read_bytes()
        service = BackupService(destination)
        safety = service.restore(portable, PASSWORD)
        assert service.inspect(safety)[1]["stores"] == 1
        assert service.inspect(safety)[1]["members"] == 0
        assert destination.list_members(sid)[0]["phone"] == "13812345678"
        reopened = StoreDatabase(destination.path, key=b"t" * 32)
        assert reopened.list_members(sid)
        reopened.close()
        with pytest.raises(SecurityError):
            StoreDatabase(destination.path, key=b"s" * 32)
    finally:
        source.close()
        destination.close()


def test_wrong_password_and_tamper_preserve_destination(tmp_path):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"d" * 32)
    try:
        database.add_store("原门店")
        service = BackupService(database)
        portable = service.create_portable(tmp_path / "backup.skyportable", PASSWORD)
        original = database.path.read_bytes()
        with pytest.raises(SecurityError):
            service.restore(portable, "another-incorrect-passphrase")
        with pytest.raises(ValueError, match="口令"):
            service.inspect(portable)
        data = portable.read_bytes()
        tampered = data[:-1] + bytes([data[-1] ^ 1])
        with pytest.raises(SecurityError):
            decrypt_portable(tampered, PASSWORD)
        assert database.path.read_bytes() == original
        assert not service.directory.exists()
    finally:
        database.close()


@pytest.mark.parametrize("password", ["", "short", "x" * 1025])
def test_portable_password_bounds(password):
    with pytest.raises(SecurityError):
        encrypt_portable(b"snapshot", password)


@pytest.mark.parametrize("data", [b"", PORTABLE_MAGIC, b"unknown-version"])
def test_reject_truncated_or_unknown_portable_format(data):
    with pytest.raises(SecurityError):
        decrypt_portable(data, PASSWORD)


def test_portable_random_salt_and_nonce():
    first = encrypt_portable(b"snapshot", PASSWORD)
    second = encrypt_portable(b"snapshot", PASSWORD)
    assert first != second
    assert decrypt_portable(first, PASSWORD) == b"snapshot"


def test_portable_write_failure_preserves_existing_file(tmp_path, monkeypatch):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"d" * 32)
    try:
        existing = tmp_path / "backup.skyportable"
        existing.write_bytes(b"original")

        def fail(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr("skyagent_manager.backup.atomic_write", fail)
        with pytest.raises(OSError):
            BackupService(database).create_portable(existing, PASSWORD)
        assert existing.read_bytes() == b"original"
        with pytest.raises(ValueError):
            BackupService(database).create_portable(database.path, PASSWORD)
    finally:
        database.close()


def test_portable_restore_disk_failure_rolls_back(tmp_path, monkeypatch):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"d" * 32)
    try:
        service = BackupService(database)
        saved = service.create_portable(tmp_path / "backup.skyportable", PASSWORD)
        database.add_store("保留门店")
        original = database.path.read_bytes()

        def fail(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr("skyagent_manager.db.atomic_write", fail)
        with pytest.raises(OSError):
            service.restore(saved, PASSWORD)
        assert database.path.read_bytes() == original
        assert database.list_stores()[0]["name"] == "保留门店"
        assert list(service.directory.glob("before-restore-*.skybackup"))
    finally:
        database.close()

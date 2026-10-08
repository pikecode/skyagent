from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.security import MAGIC, SecurityError, decrypt, encrypt

KEY = b"k" * 32


@pytest.fixture
def database(tmp_path):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=KEY)
    yield database
    database.close()


def test_encrypted_persistence_and_restart(database):
    sid = database.add_store("一号门店")
    database.add_member(sid, "会员", "13812345678")
    database.add_benefit(sid, "breakfast", "早餐", "sensitive-coupon-value")
    payload = database.path.read_bytes()
    assert payload.startswith(MAGIC)
    assert b"13812345678" not in payload
    assert b"sensitive-coupon-value" not in payload
    assert not list(database.path.parent.glob("*-wal"))
    reopened = StoreDatabase(database.path, key=KEY)
    try:
        assert (
            reopened.connection.execute("SELECT phone FROM members").fetchone()[0]
            == "13812345678"
        )
    finally:
        reopened.close()


def test_wrong_key_and_tamper_do_not_overwrite(database):
    database.add_store("门店")
    original = database.path.read_bytes()
    with pytest.raises(SecurityError):
        StoreDatabase(database.path, key=b"x" * 32)
    assert database.path.read_bytes() == original
    corrupted = original[:-1] + bytes([original[-1] ^ 1])
    with pytest.raises(SecurityError):
        decrypt(corrupted, KEY)


def test_save_failure_rolls_back_memory_and_preserves_disk(database, monkeypatch):
    original = database.path.read_bytes()

    def failing(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr("skyagent_manager.db.atomic_write", failing)
    with pytest.raises(OSError):
        database.add_store("不会保存")
    assert database.list_stores() == []
    assert database.path.read_bytes() == original


def test_backup_restore_safety_copy_and_wrong_key(database, tmp_path):
    sid = database.add_store("门店")
    backup = BackupService(database)
    saved = backup.create(tmp_path / "saved.skybackup")
    database.add_member(sid, "会员", "13812345678")
    safety = backup.restore(saved)
    assert not database.list_members(sid)
    assert backup.inspect(safety)[1]["members"] == 1
    assert b"13812345678" not in safety.read_bytes()
    bad = tmp_path / "bad.skybackup"
    bad.write_bytes(encrypt(database.connection.serialize(), b"z" * 32))
    with pytest.raises(SecurityError):
        backup.restore(bad)
    with pytest.raises(ValueError):
        backup.create(database.path)


def test_backup_rejects_future_schema(database, tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.deserialize(database.connection.serialize())
    connection.execute("PRAGMA user_version=99")
    path = tmp_path / "future.skybackup"
    path.write_bytes(encrypt(connection.serialize(), KEY))
    connection.close()
    original = database.path.read_bytes()
    with pytest.raises(ValueError):
        BackupService(database).restore(path)
    assert database.path.read_bytes() == original


def test_automatic_backup_retention_and_disabled(database, monkeypatch):
    database.set_backup_settings(True, 2)
    service = BackupService(database)
    service.directory.mkdir()
    for date in ("20200101", "20200102", "20200103"):
        service.create(service.directory / f"auto-{date}.skybackup")
    manual = service.create(service.directory / "manual.skybackup")

    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 30, tzinfo=UTC)

    monkeypatch.setattr("skyagent_manager.backup.datetime", Clock)
    assert service.automatic().name == "auto-20260930.skybackup"
    assert len(list(service.directory.glob("auto-*.skybackup"))) == 2
    assert manual.exists()
    assert service.automatic() is None
    database.set_backup_settings(True, 1)
    assert service.automatic() is None
    assert len(list(service.directory.glob("auto-*.skybackup"))) == 1
    database.set_backup_settings(False, 2)
    assert service.automatic() is None


def test_migrate_legacy_with_wal_mode(database, tmp_path):
    sid = database.add_store("旧门店")
    database.add_member(sid, "旧会员", "138-1234-5678")
    legacy_path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(legacy_path)
    database.connection.backup(legacy)
    legacy.executescript(
        "DROP TABLE store_sync; DROP TABLE sync_results; DROP TABLE settings; "
        "DROP INDEX members_store_phone_norm; ALTER TABLE members DROP COLUMN phone_norm; PRAGMA user_version=2;"
    )
    legacy.execute(
        "INSERT INTO members(id, store_id, display_name, phone, note, authorized_at, created_at) VALUES (?, ?, ?, ?, '', '', '')",
        ("legacy-duplicate", sid, "格式重复旧会员", "13812345678"),
    )
    legacy.commit()
    legacy.execute("PRAGMA journal_mode=WAL")
    legacy.close()
    restored = StoreDatabase(legacy_path, key=KEY)
    try:
        assert restored.list_stores()[0]["name"] == "旧门店"
        assert len(restored.list_members(sid)) == 2
        assert {row["phone_norm"] for row in restored.list_members(sid)} == {
            "13812345678"
        }
        assert restored.connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert legacy_path.read_bytes().startswith(MAGIC)
        assert (tmp_path / "backups" / "before-encryption.skybackup").exists()
        service = BackupService(restored)
        service.restore(tmp_path / "backups" / "before-encryption.skybackup")
        assert restored.list_stores()[0]["name"] == "旧门店"
        assert restored.connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        restored.close()


def test_phone_dedup_and_cross_store_constraint(database):
    a, b = database.add_store("A"), database.add_store("B")
    database.add_member(a, "会员", "138-1234-5678")
    with pytest.raises(ValueError):
        database.add_member(a, "重复", "13812345678")
    database.add_benefit(a, "breakfast", "早餐", member_phone="138 1234 5678")
    member = database.list_members(a)[0]
    with pytest.raises(sqlite3.IntegrityError):
        with database.connection:
            database.connection.execute(
                "UPDATE benefits SET store_id=? WHERE member_id=?", (b, member["id"])
            )
    assert not database.list_benefits(b)


def test_settings_and_sync_results_are_scoped(database):
    a, b = database.add_store("A"), database.add_store("B")
    database.add_member(a, "会员", "13812345678")
    item = database.sync_items(a, "members")[0]
    database.save_sync_result(a, "members", item["id"], False, "失败")
    assert database.sync_items(a, "members", failed_only=True)[0]["id"] == item["id"]
    assert database.sync_items(b, "members") == []
    database.archive_store(a)
    with pytest.raises(ValueError):
        database.sync_items(a, "members")


def test_missing_native_key_does_not_create_replacement(tmp_path, monkeypatch):
    from skyagent_manager.security import KeyVault

    vault = object.__new__(KeyVault)
    monkeypatch.setattr(vault, "get", lambda name: None)
    monkeypatch.setattr(
        vault, "set", lambda *args: pytest.fail("must not create a new key")
    )
    with pytest.raises(SecurityError, match="密钥"):
        vault.database_key(encrypted_exists=True)


def test_explicit_backend_has_no_plaintext_fallback(tmp_path, monkeypatch):
    import sys

    from skyagent_manager.security import KeyVault

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SecurityError):
        KeyVault(tmp_path)


def test_unknown_schema_keeps_original_file(database, tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.deserialize(database.connection.serialize())
    connection.execute("PRAGMA user_version=100")
    future = tmp_path / "future.sqlite3"
    payload = encrypt(connection.serialize(), KEY)
    future.write_bytes(payload)
    connection.close()
    with pytest.raises(ValueError):
        StoreDatabase(future, key=KEY)
    assert future.read_bytes() == payload


def test_invalid_file_does_not_access_credentials(tmp_path, monkeypatch):
    path = tmp_path / "invalid.sqlite3"
    path.write_bytes(b"invalid")
    monkeypatch.setattr(
        "skyagent_manager.db.KeyVault",
        lambda *args: pytest.fail("must not access credentials"),
    )
    with pytest.raises(ValueError):
        StoreDatabase(path)
    assert path.read_bytes() == b"invalid"


def test_restore_write_failure_preserves_active_database(
    database, tmp_path, monkeypatch
):
    sid = database.add_store("门店")
    service = BackupService(database)
    saved = service.create(tmp_path / "saved.skybackup")
    database.add_member(sid, "会员", "13812345678")
    original = database.path.read_bytes()

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr("skyagent_manager.db.atomic_write", fail)
    with pytest.raises(OSError):
        service.restore(saved)
    assert len(database.list_members(sid)) == 1
    assert database.path.read_bytes() == original

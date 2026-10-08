from __future__ import annotations

import pytest

from skyagent_manager.db import StoreDatabase
from skyagent_manager.sync import validate_config


@pytest.fixture
def database(tmp_path):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"a" * 32)
    yield database
    database.close()


def test_store_operations_have_single_atomic_audit(database):
    sid = database.add_store("门店")
    database.configure_store(sid, "新门店", validate_config("", "", "", False))
    database.archive_store(sid)
    database.restore_store(sid)
    assert [r["action"] for r in database.list_activity(sid)] == [
        "恢复门店",
        "归档门店",
        "门店设置",
        "添加门店",
    ]


@pytest.mark.parametrize("operation", ["archive", "restore", "configure"])
def test_failed_store_write_rolls_back_record_and_audit(
    database, monkeypatch, operation
):
    sid = database.add_store("门店")
    if operation == "restore":
        database.archive_store(sid)
    original = database.path.read_bytes()
    store = dict(database.list_stores(True)[0])
    activity = [dict(row) for row in database.list_activity(sid)]

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr("skyagent_manager.db.atomic_write", fail)
    with pytest.raises(OSError):
        if operation == "archive":
            database.archive_store(sid)
        elif operation == "restore":
            database.restore_store(sid)
        else:
            database.configure_store(sid, "新名称", validate_config("", "", "", False))
    assert database.path.read_bytes() == original
    assert dict(database.list_stores(True)[0]) == store
    assert [dict(row) for row in database.list_activity(sid)] == activity


def test_archived_store_configuration_and_unknown_store_rejected(database):
    sid = database.add_store("门店")
    database.archive_store(sid)
    config = validate_config("", "", "", False)
    with pytest.raises(ValueError):
        database.configure_store(sid, "改变", config)
    for method in (database.archive_store, database.restore_store):
        with pytest.raises(ValueError):
            method("missing-store")
    with pytest.raises(ValueError):
        database.configure_store("missing-store", "改变", config)

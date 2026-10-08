import sqlite3

import pytest

from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.security import encrypt


@pytest.fixture
def db(tmp_path):
    database = StoreDatabase(tmp_path / "stock.db", key=b"s" * 32)
    yield database
    database.close()


def test_stock_dedup_encryption_and_store_isolation(db):
    a, b = db.add_store("A"), db.add_store("B")
    stock = Inventory(db)
    assert (
        stock.import_values(
            a, "silver", ["sensitive-stock-code", "sensitive-stock-code"]
        )
        == 1
    )
    assert b"sensitive-stock-code" not in db.path.read_bytes()
    rid = stock.list(a)[0]["id"]
    with pytest.raises(ValueError):
        stock.start(b, [rid])
    assert not db.connection.execute("SELECT * FROM local_tasks").fetchall()
    assert stock.list(a)[0]["state"] == "available"
    db.archive_store(a)
    with pytest.raises(ValueError):
        stock.import_values(a, "gift", ["code"])


@pytest.mark.parametrize(
    "outcome,state",
    [("succeeded", "used"), ("failed", "available"), ("canceled", "available")],
)
def test_task_reservation_and_completion(db, outcome, state):
    sid = db.add_store("A")
    stock = Inventory(db)
    stock.import_values(sid, "breakfast", ["code"])
    rid = stock.list(sid)[0]["id"]
    task = stock.start(sid, [rid])
    with pytest.raises(ValueError):
        stock.start(sid, [rid])
    with pytest.raises(ValueError):
        stock.delete(sid, rid)
    stock.finish(sid, task, outcome)
    assert stock.list(sid)[0]["state"] == state
    with pytest.raises(ValueError):
        stock.finish(sid, task, outcome)
    with pytest.raises(ValueError):
        stock.delete(sid, rid)


def test_recovery_and_backup(db, tmp_path):
    sid = db.add_store("A")
    stock = Inventory(db)
    stock.import_values(sid, "invite", ["invitation"])
    with pytest.raises(ValueError):
        stock.start(sid, [stock.list(sid)[0]["id"]])
    stock.import_values(sid, "gift", ["resource"])
    rid = next(r["id"] for r in stock.list(sid) if r["kind"] == "gift")
    task = stock.start(sid, [rid])
    backup = BackupService(db)
    path = backup.create(tmp_path / "backup.skybackup")
    reopened = StoreDatabase(db.path, key=b"s" * 32)
    try:
        assert (
            reopened.connection.execute(
                "SELECT state FROM local_tasks WHERE id=?", (task,)
            ).fetchone()[0]
            == "interrupted"
        )
        assert (
            next(r for r in Inventory(reopened).list(sid) if r["id"] == rid)["state"]
            == "available"
        )
    finally:
        reopened.close()
    assert backup.inspect(path)[1]["stock"] == 2
    backup.restore(path)
    assert (
        db.connection.execute(
            "SELECT state FROM local_tasks WHERE id=?", (task,)
        ).fetchone()[0]
        == "interrupted"
    )


def test_atomic_reservation_rollback(db):
    sid = db.add_store("A")
    stock = Inventory(db)
    stock.import_values(sid, "room_upgrade", ["resource"])
    rid = stock.list(sid)[0]["id"]
    with pytest.raises(ValueError):
        stock.start(sid, [rid, "missing"])
    assert stock.list(sid)[0]["state"] == "available"
    assert not db.connection.execute("SELECT * FROM local_task_items").fetchall()


def test_v4_upgrade_preserves_data_and_snapshot(db, tmp_path):
    sid = db.add_store("A")
    old = sqlite3.connect(":memory:")
    old.deserialize(db.connection.serialize())
    old.executescript(
        "DROP TABLE local_task_items; DROP TABLE local_tasks; DROP TABLE stock; PRAGMA user_version=4;"
    )
    path = tmp_path / "v4" / "manager.db"
    path.parent.mkdir()
    path.write_bytes(encrypt(old.serialize(), db.key))
    old.close()
    upgraded = StoreDatabase(path, key=db.key)
    try:
        assert upgraded.list_stores()[0]["id"] == sid
        assert Inventory(upgraded).list(sid) == []
        assert (
            len(list((path.parent / "backups").glob("before-upgrade-v6-*.skybackup")))
            == 1
        )
    finally:
        upgraded.close()


def test_persist_failure_rolls_back_task_and_resource(db, monkeypatch):
    sid = db.add_store("A")
    stock = Inventory(db)
    stock.import_values(sid, "gift", ["resource"])
    rid = stock.list(sid)[0]["id"]

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        stock.start(sid, [rid])
    assert stock.list(sid)[0]["state"] == "available"
    assert not db.connection.execute("SELECT * FROM local_tasks").fetchall()


def test_invite_edit_and_selection_validation(db):
    a, b = db.add_store("A"), db.add_store("B")
    stock = Inventory(db)
    stock.import_values(a, "invite", ["first-invite", "second-invite"])
    rid = stock.list(a)[0]["id"]
    with pytest.raises(ValueError):
        stock.select_invite(b, rid)
    with pytest.raises(ValueError):
        stock.edit_invite(b, rid, "replacement")
    with pytest.raises(ValueError):
        stock.edit_invite(a, rid, "second-invite")
    stock.select_invite(a, rid)
    stock.edit_invite(a, rid, "updated-invite")
    assert db.get_setting(f"selected_invite/{a}") == rid
    stock.select_invite(a)
    assert not db.get_setting(f"selected_invite/{a}")
    assert b"updated-invite" not in db.path.read_bytes()


def test_invite_selection_in_portable_backup(db, tmp_path):
    sid = db.add_store("A")
    stock = Inventory(db)
    stock.import_values(sid, "invite", ["portable-invite"])
    rid = stock.list(sid)[0]["id"]
    stock.select_invite(sid, rid)
    path = BackupService(db).create_portable(
        tmp_path / "invite.skyportable", "safe-long-backup-password"
    )
    destination = StoreDatabase(tmp_path / "destination.db", key=b"n" * 32)
    try:
        BackupService(destination).restore(path, "safe-long-backup-password")
        assert destination.get_setting(f"selected_invite/{sid}") == rid
        assert Inventory(destination).list(sid)[0]["value"] == "portable-invite"
    finally:
        destination.close()

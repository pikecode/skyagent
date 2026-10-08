import sqlite3
from dataclasses import replace

import pytest
from test_coupon_sharing import URL
from test_coupon_sharing import context as context
from test_prop_sharing import prop

from skyagent_manager.backup import BackupService, validate_snapshot
from skyagent_manager.coupon_sharing import CouponShareOutputs
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.security import decrypt, encrypt
from skyagent_manager.share_journal import ShareJournal
from skyagent_manager.share_safety import share_writes_blocked


def saved(context):
    db, sid, aid, owner, item = context
    item = prop(item)
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    outputs = CouponShareOutputs(db)
    outputs.save(sid, aid, owner, item, operation, URL)
    return db, sid, aid, owner, item, operation, outputs


def v5_image(db):
    old = sqlite3.connect(":memory:")
    try:
        old.deserialize(db.connection.serialize())
        old.executescript(
            "PRAGMA foreign_keys=OFF; BEGIN;"
            "CREATE TABLE old_stock(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),"
            "kind TEXT NOT NULL CHECK(kind IN ('silver','breakfast','room_upgrade','delayed_checkout','gift','invite')),"
            "value TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','reserved','used')),"
            "UNIQUE(store_id,kind,value),UNIQUE(store_id,id));"
            "INSERT INTO old_stock(rowid,id,store_id,kind,value,state) SELECT rowid,id,store_id,kind,value,state FROM stock;"
            "DROP TABLE stock; ALTER TABLE old_stock RENAME TO stock;"
            "PRAGMA user_version=5; COMMIT;"
        )
        return old.serialize()
    finally:
        old.close()


def test_single_link_not_query_count_and_restart_duplicate(context):
    db, sid, aid, owner, item, operation, outputs = saved(context)
    item = replace(item, count=500)
    stock_id = outputs.import_stock(sid, aid, owner, item)
    assert len(Inventory(db).list(sid)) == 1
    assert Inventory(db).list(sid)[0]["kind"] == "prop"
    assert URL.encode() not in db.path.read_bytes()
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        row = CouponShareOutputs(reopened).load_operation(sid, aid, operation)
        assert row.stock_id == stock_id
        Inventory(reopened).delete(sid, stock_id)
        with pytest.raises(ValueError, match="已处理"):
            CouponShareOutputs(reopened).import_operation(sid, aid, owner, operation)
    finally:
        reopened.close()


@pytest.mark.parametrize("expiry", ["", "2000-01-01"])
def test_history_date_invalid_no_stock(context, expiry):
    db, sid, aid, owner, item, operation, outputs = saved(context)
    payload = outputs._payload(operation)
    payload["expiry"] = expiry
    import json

    with db.connection:
        db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?",
            (json.dumps(payload), outputs.PREFIX + operation),
        )
    before = db.path.read_bytes()
    with pytest.raises(ValueError, match="有效期"):
        outputs.import_operation(sid, aid, owner, operation)
    assert not Inventory(db).list(sid) and db.path.read_bytes() == before


def test_current_expiry_cannot_replace_saved_expiry(context):
    db, sid, aid, owner, item, operation, outputs = saved(context)
    with pytest.raises(ValueError, match="有效期不一致"):
        outputs.import_stock(sid, aid, owner, replace(item, expiry="2099-01-01"))
    assert not Inventory(db).list(sid)
    assert not outputs.load_operation(sid, aid, operation).stock_id


def test_stock_output_transaction_rolls_back(context, monkeypatch):
    db, sid, aid, owner, item, operation, outputs = saved(context)
    before = db.path.read_bytes()

    def fail():
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        outputs.import_operation(sid, aid, owner, operation)
    assert not Inventory(db).list(sid)
    assert not outputs.load_operation(sid, aid, operation).stock_id
    assert db.path.read_bytes() == before


def test_same_link_other_kind_or_store_not_reclassified(context):
    db, sid, aid, owner, item, operation, outputs = saved(context)
    other = db.add_store("B")
    Inventory(db).import_values(other, "silver", [URL])
    with pytest.raises(ValueError, match="已存在"):
        outputs.import_operation(sid, aid, owner, operation)
    assert not Inventory(db).list(sid)
    assert Inventory(db).list(other)[0]["kind"] == "silver"


def test_prop_manual_rows_isolation_and_local_simulation(context):
    db, sid, *_ = context
    stock = Inventory(db)
    report = stock.import_rows(
        sid, [{"kind": "prop", "value": "synthetic-local-prop"}] * 2
    )
    assert report.inserted == 1 and report.skipped == 1
    stock_id = stock.list(sid)[0]["id"]
    other = db.add_store("B")
    with pytest.raises(ValueError):
        stock.start(other, [stock_id])
    task = stock.start(sid, [stock_id])
    assert stock.list(sid)[0]["state"] == "reserved"
    stock.finish(sid, task, "succeeded")
    assert stock.list(sid)[0]["state"] == "used"
    with pytest.raises(ValueError):
        stock.delete(sid, stock_id)
    assert validate_snapshot(db.connection.serialize())["stock"] == 1


def test_prop_link_import_offline_preserves_state(context):
    from skyagent_manager.resource_links import parse_link_text

    db, sid, *_ = context
    report = Inventory(db).import_rows(sid, parse_link_text(URL + "\tused", "prop"))
    assert report.inserted == 1
    assert Inventory(db).list(sid)[0]["kind"] == "prop"
    assert Inventory(db).list(sid)[0]["state"] == "used"


def test_v5_upgrade_preserves_ids_rows_tasks_settings_and_backup(context, tmp_path):
    db, sid, aid, owner, *_ = context
    stock = Inventory(db)
    for kind in ("breakfast", "room_upgrade", "invite"):
        stock.import_values(sid, kind, ["synthetic-" + kind])
    rows = stock.list(sid)
    ids = {row["kind"]: row["id"] for row in rows}
    done = stock.start(sid, [ids["breakfast"]])
    stock.finish(sid, done, "succeeded")
    running = stock.start(sid, [ids["room_upgrade"]])
    stock.select_invite(sid, ids["invite"])
    snapshot = v5_image(db)
    path = tmp_path / "old" / "manager.db"
    path.parent.mkdir()
    path.write_bytes(encrypt(snapshot, db.key))
    upgraded = StoreDatabase(path, key=db.key)
    try:
        assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert upgraded.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not upgraded.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert [row["id"] for row in Inventory(upgraded).list(sid)] == [
            row["id"] for row in rows
        ]
        assert Inventory(upgraded).selected_invites(sid) == [ids["invite"]]
        assert Inventory(upgraded).task_items(sid, done)[0]["state"] == "used"
        assert Inventory(upgraded).task_items(sid, running)[0]["state"] == "released"
        assert Inventory(upgraded).tasks(sid)[0]["state"] == "interrupted"
        safety = list((path.parent / "backups").glob("before-upgrade-v6-*.skybackup"))
        assert len(safety) == 1 and decrypt(safety[0].read_bytes(), db.key) == snapshot
        assert BackupService(upgraded).inspect(safety[0])[1]["stock"] == 3
        Inventory(upgraded).import_values(sid, "prop", ["synthetic-prop-v6"])
        assert validate_snapshot(upgraded.connection.serialize())["stock"] == 4
    finally:
        upgraded.close()


def test_v5_upgrade_failure_preserves_original_file(context, tmp_path):
    db, sid, *_ = context
    Inventory(db).import_values(sid, "breakfast", ["synthetic-resource"])
    old = sqlite3.connect(":memory:")
    old.deserialize(v5_image(db))
    old.execute("CREATE TABLE stock_upgrade_v6(unrelated TEXT)")
    old.commit()
    path = tmp_path / "failed.db"
    original = encrypt(old.serialize(), db.key)
    old.close()
    path.write_bytes(original)
    with pytest.raises(sqlite3.OperationalError):
        StoreDatabase(path, key=db.key)
    assert path.read_bytes() == original
    assert len(list((tmp_path / "backups").glob("before-upgrade-v6-*.skybackup"))) == 1


def test_restore_v5_upgrades_and_preserves_share_block(context, tmp_path):
    db, sid, aid, owner, item, operation, outputs = saved(context)
    path = tmp_path / "v5.skybackup"
    path.write_bytes(encrypt(v5_image(db), db.key))
    BackupService(db).restore(path)
    assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 6
    assert share_writes_blocked(db)
    assert outputs.load_operation(sid, aid, operation).kind == "prop"
    with pytest.raises(ValueError):
        outputs.import_operation(sid, aid, owner, operation)
    assert not Inventory(db).list(sid)


def test_v6_prop_backup_roundtrip(context, tmp_path):
    db, sid, aid, owner, item, operation, outputs = saved(context)
    stock_id = outputs.import_operation(sid, aid, owner, operation)
    backup = BackupService(db)
    path = backup.create(tmp_path / "prop.skybackup")
    assert backup.inspect(path)[1]["stock"] == 1
    backup.restore(path)
    assert Inventory(db).list(sid)[0]["id"] == stock_id
    assert Inventory(db).list(sid)[0]["kind"] == "prop"
    assert outputs.load_operation(sid, aid, operation).stock_id == stock_id


def test_v5_cannot_claim_v6_prop_kind(context):
    db, sid, *_ = context
    Inventory(db).import_values(sid, "prop", ["synthetic-value"])
    old = sqlite3.connect(":memory:")
    try:
        old.deserialize(db.connection.serialize())
        old.execute("PRAGMA user_version=5")
        with pytest.raises(ValueError, match="类型"):
            validate_snapshot(old.serialize())
    finally:
        old.close()

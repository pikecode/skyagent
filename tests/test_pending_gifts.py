import json

import pytest
from test_coupon_sharing import context as context
from test_gift_sharing import CODE, successful

from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.gift_sharing import (
    PENDING_GIFT_PREFIX,
    GiftShareOutputs,
    PendingGiftInventory,
)
from skyagent_manager.inventory import Inventory


def registered(context):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    saved = outputs.load_operation(sid, aid, operation)
    pending_id = PendingGiftInventory(db).register(
        sid, aid, owner, operation, expected=saved
    )
    return db, sid, aid, owner, operation, outputs, pending_id


def test_register_reference_only_encrypted_and_stock_task_isolation(context):
    db, sid, aid, owner, operation, outputs, pending_id = registered(context)
    entry = PendingGiftInventory(db).list(sid)[0]
    assert entry.pending_id == pending_id and entry.operation_id == operation
    assert outputs.load_operation(sid, aid, operation).pending_id == pending_id
    assert CODE not in db.get_setting(PENDING_GIFT_PREFIX + pending_id)
    assert CODE.encode() not in db.path.read_bytes() and CODE not in repr(entry)
    assert not Inventory(db).list(sid)
    with pytest.raises(ValueError):
        Inventory(db).start(sid, [pending_id])
    assert not Inventory(db).tasks(sid)
    with pytest.raises(ValueError):
        Inventory(db).delete(sid, pending_id)


def test_restart_read_only_and_duplicate_block(context):
    db, sid, aid, owner, operation, outputs, pending_id = registered(context)
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        before = reopened.path.read_bytes()
        assert PendingGiftInventory(reopened).list(sid)[0].pending_id == pending_id
        assert (
            GiftShareOutputs(reopened).load_operation(sid, aid, operation).pending_id
            == pending_id
        )
        with pytest.raises(ValueError, match="已登记"):
            PendingGiftInventory(reopened).register(sid, aid, owner, operation)
        assert reopened.path.read_bytes() == before
    finally:
        reopened.close()


def test_store_isolation_archive_rejection(context):
    db, sid, aid, owner, operation, outputs, pending_id = registered(context)
    other = db.add_store("B")
    assert not PendingGiftInventory(db).list(other)
    with pytest.raises(ValueError):
        PendingGiftInventory(db).register(other, aid, owner, operation)
    db.archive_store(sid)
    with pytest.raises(ValueError):
        PendingGiftInventory(db).list(sid)


def test_write_failure_rolls_back_both_source_and_registration(context, monkeypatch):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    before = db.path.read_bytes()

    def fail():
        raise OSError("synthetic-disk-failure")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        PendingGiftInventory(db).register(sid, aid, owner, operation)
    assert not PendingGiftInventory(db).list(sid)
    assert not outputs.load_operation(sid, aid, operation).pending_id
    assert db.path.read_bytes() == before


def test_restore_keeps_pending_read_only_blocks_new_registration(context, tmp_path):
    db, sid, aid, owner, operation, outputs, pending_id = registered(context)
    backup = BackupService(db)
    backup.restore(backup.create(tmp_path / "pending.skybackup"))
    assert PendingGiftInventory(db).list(sid)[0].pending_id == pending_id
    with pytest.raises(ValueError, match="恢复"):
        PendingGiftInventory(db).register(sid, aid, owner, operation)
    assert not Inventory(db).list(sid)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "available"),
        ("state", "used"),
        ("operation_id", "f" * 32),
        ("store_id", "e" * 32),
        ("account_id", "d" * 32),
        ("registered_at", "tomorrow"),
    ],
)
def test_corrupt_pending_source_link_rejects_partial_list(context, field, value):
    db, sid, aid, owner, operation, outputs, pending_id = registered(context)
    payload = json.loads(db.get_setting(PENDING_GIFT_PREFIX + pending_id))
    payload[field] = value
    with db.connection:
        db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?",
            (json.dumps(payload), PENDING_GIFT_PREFIX + pending_id),
        )
    with pytest.raises(ValueError):
        outputs.load_operation(sid, aid, operation)
    with pytest.raises(ValueError):
        PendingGiftInventory(db).list(sid)


def test_missing_pending_association_no_recreation(context):
    db, sid, aid, owner, operation, outputs, pending_id = registered(context)
    with db.connection:
        db.connection.execute(
            "DELETE FROM settings WHERE name=?", (PENDING_GIFT_PREFIX + pending_id,)
        )
    with pytest.raises(ValueError):
        outputs.load_operation(sid, aid, operation)
    with pytest.raises(ValueError):
        PendingGiftInventory(db).register(sid, aid, owner, operation)


def test_removed_source_marker_does_not_release_duplicate_registration(context):
    db, sid, aid, owner, operation, outputs, pending_id = registered(context)
    payload = json.loads(db.get_setting(outputs.PREFIX + operation))
    payload.pop("pending_id")
    with db.connection:
        db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?",
            (json.dumps(payload), outputs.PREFIX + operation),
        )
    with pytest.raises(ValueError):
        PendingGiftInventory(db).register(sid, aid, owner, operation)
    with pytest.raises(ValueError):
        PendingGiftInventory(db).list(sid)


def test_existing_stock_not_reclassified(context):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    Inventory(db).import_values(sid, "gift", [CODE])
    with pytest.raises(ValueError, match="库存记录"):
        PendingGiftInventory(db).register(sid, aid, owner, operation)
    assert not PendingGiftInventory(db).list(sid)
    assert Inventory(db).list(sid)[0]["state"] == "available"


def test_changed_owner_or_expected_no_registration(context):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    with pytest.raises(ValueError):
        PendingGiftInventory(db).register(sid, aid, (sid, aid, "changed"), operation)
    with pytest.raises(ValueError):
        PendingGiftInventory(db).register(
            sid, aid, owner, operation, expected="changed"
        )
    assert not PendingGiftInventory(db).list(sid)


def test_global_cap_fails_without_source_mutation(context):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    with db.connection:
        db.connection.executemany(
            "INSERT INTO settings VALUES(?,?)",
            [(PENDING_GIFT_PREFIX + f"{i:032x}", "{}") for i in range(2000)],
        )
    before = db.path.read_bytes()
    with pytest.raises(ValueError, match="2000"):
        PendingGiftInventory(db).register(sid, aid, owner, operation)
    assert not outputs.load_operation(sid, aid, operation).pending_id
    assert db.path.read_bytes() == before

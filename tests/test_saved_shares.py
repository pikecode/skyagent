import csv
import json
from dataclasses import replace

import pytest
from test_coupon_sharing import TOKEN, URL, save
from test_coupon_sharing import context as context

from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.coupon_sharing import CouponShareOutputs
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.share_journal import ShareJournal


def target(tmp_path):
    return tmp_path.parent / (tmp_path.name + "-share-export.csv")


def change_payload(db, operation, **changes):
    key = CouponShareOutputs.PREFIX + operation
    payload = json.loads(db.get_setting(key))
    payload.update(changes)
    with db.connection:
        db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?", (json.dumps(payload), key)
        )


def test_offline_list_restart_import_without_original_query(context, monkeypatch):
    db, sid, aid, owner, item = context
    operation = save(context)

    def forbidden(*args, **kwargs):
        raise AssertionError("history must not network")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        outputs = CouponShareOutputs(reopened)
        before = reopened.path.read_bytes()
        rows = outputs.list_saved(sid, aid)
        assert len(rows) == 1 and rows[0].expiry == item.expiry
        assert rows[0].resource_keys == ShareJournal.resource_key(item)
        assert rows[0].created_at and URL not in repr(rows)
        assert reopened.path.read_bytes() == before
        stock = outputs.import_operation(sid, aid, owner, operation, expected=rows[0])
        assert Inventory(reopened).list(sid)[0]["value"] == URL
        assert outputs.list_saved(sid, aid)[0].stock_id == stock
        with pytest.raises(ValueError):
            outputs.import_operation(sid, aid, owner, operation)
    finally:
        reopened.close()


def test_legacy_payload_visible_but_unknown_expiry_not_assumed(context):
    db, sid, aid, owner, item = context
    operation = save(context)
    key = CouponShareOutputs.PREFIX + operation
    payload = json.loads(db.get_setting(key))
    for field in ("expiry", "created_at", "resource_keys"):
        payload.pop(field)
    with db.connection:
        db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?", (json.dumps(payload), key)
        )
    outputs = CouponShareOutputs(db)
    row = outputs.list_saved(sid, aid)[0]
    assert row.expiry == "" and row.created_at
    before = db.path.read_bytes()
    with pytest.raises(ValueError, match="有效期未知"):
        outputs.import_operation(sid, aid, owner, operation)
    assert db.path.read_bytes() == before and not Inventory(db).list(sid)


def test_store_account_isolation_and_unknown_not_success(context):
    db, sid, aid, owner, item = context
    operation = save(context)
    other = db.add_store("B")
    second = Accounts(db).add(sid, AccountInput("13912345678", "synthetic-other-token"))
    outputs = CouponShareOutputs(db)
    assert not outputs.list_saved(other, aid) and not outputs.list_saved(sid, second)
    with pytest.raises(ValueError):
        outputs.load_operation(other, aid, operation)
    other_item = replace(
        item, identifier="synthetic-other-id", code="synthetic-other-code"
    )
    ShareJournal(db).finish(
        other_item, ShareJournal(db).reserve(sid, aid, owner, other_item)
    )
    assert len(outputs.list_saved(sid, aid)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"resource_keys": []},
        {"resource_keys": ["broken"]},
        {"kind": "gift"},
        {"created_at": "2026-10-07"},
        {"expiry": "tomorrow"},
        {"stock_id": "bad"},
        {"url": "https://evil.invalid/private"},
    ],
)
def test_corrupt_records_fail_closed_without_partial_listing(context, changes):
    db, sid, aid, owner, item = context
    operation = save(context)
    change_payload(db, operation, **changes)
    with pytest.raises(ValueError) as error:
        CouponShareOutputs(db).list_saved(sid, aid)
    assert "private" not in str(error.value) and URL not in str(error.value)


def test_missing_journal_does_not_authorize_saved_output(context):
    db, sid, aid, owner, item = context
    save(context)
    with db.connection:
        db.connection.execute(
            "DELETE FROM settings WHERE name=?", (ShareJournal.resource_key(item)[0],)
        )
    with pytest.raises(ValueError):
        CouponShareOutputs(db).list_saved(sid, aid)


def test_list_over_bound_rejects_not_truncates(context):
    db, sid, aid, owner, item = context
    with db.connection:
        db.connection.executemany(
            "INSERT INTO settings VALUES (?, ?)",
            [(CouponShareOutputs.PREFIX + f"{i:032x}", "{}") for i in range(2001)],
        )
    with pytest.raises(ValueError, match="2000"):
        CouponShareOutputs(db).list_saved(sid, aid)


@pytest.mark.parametrize("full", [False, True])
def test_export_privacy_and_no_database_mutation(context, tmp_path, full):
    db, sid, aid, owner, item = context
    save(context)
    outputs = CouponShareOutputs(db)
    rows = outputs.list_saved(sid, aid)
    before = db.path.read_bytes()
    path = target(tmp_path)
    outputs.export_to(path, sid, aid, owner, rows, full=full)
    raw = path.read_text(encoding="utf-8-sig")
    assert (URL in raw) is full and TOKEN not in raw and item.code not in raw
    assert "13812345678" not in raw and "非领取凭证" in raw
    assert len(list(csv.DictReader(raw.splitlines()))) == 1
    assert db.path.read_bytes() == before and not Inventory(db).list(sid)


def test_changed_owner_record_and_unsafe_targets_never_overwrite(context, tmp_path):
    db, sid, aid, owner, item = context
    operation = save(context)
    outputs = CouponShareOutputs(db)
    rows = outputs.list_saved(sid, aid)
    path = target(tmp_path)
    path.write_bytes(b"existing-export")
    with pytest.raises(ValueError):
        outputs.export_to(path, sid, aid, owner[:-1] + ("changed",), rows, full=True)
    assert path.read_bytes() == b"existing-export"
    with pytest.raises(ValueError):
        outputs.export_to(db.path, sid, aid, owner, rows)
    with pytest.raises(ValueError):
        outputs.export_to(db.path.parent / "backup.csv", sid, aid, owner, rows)
    change_payload(db, operation, expiry="2099-01-01")
    with pytest.raises(ValueError):
        outputs.export_to(path, sid, aid, owner, rows)
    assert path.read_bytes() == b"existing-export"


def test_export_write_failure_preserves_existing_file(context, tmp_path, monkeypatch):
    db, sid, aid, owner, item = context
    save(context)
    outputs = CouponShareOutputs(db)
    path = target(tmp_path)
    path.write_bytes(b"existing-export")

    def fail(*args):
        raise OSError("private-path")

    monkeypatch.setattr("skyagent_manager.coupon_sharing.atomic_write", fail)
    with pytest.raises(ValueError) as error:
        outputs.export_to(
            path, sid, aid, owner, outputs.list_saved(sid, aid), full=True
        )
    assert path.read_bytes() == b"existing-export" and "private-path" not in str(
        error.value
    )


def test_history_import_atomic_failure_and_expired(context, monkeypatch):
    db, sid, aid, owner, item = context
    operation = save(context)
    outputs = CouponShareOutputs(db)
    before = db.path.read_bytes()

    def fail(*args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("skyagent_manager.db.atomic_write", fail)
        with pytest.raises(OSError):
            outputs.import_operation(sid, aid, owner, operation)
    assert (
        db.path.read_bytes() == before
        and not outputs.load_operation(sid, aid, operation).stock_id
    )
    change_payload(db, operation, expiry="2000-01-01")
    with pytest.raises(ValueError):
        outputs.import_operation(sid, aid, owner, operation)


def test_restore_allows_masked_view_but_blocks_import_full_export(context, tmp_path):
    db, sid, aid, owner, item = context
    operation = save(context)
    service = BackupService(db)
    service.restore(service.create(tmp_path / "saved.skybackup"))
    outputs = CouponShareOutputs(db)
    rows = outputs.list_saved(sid, aid)
    outputs.export_to(target(tmp_path), sid, aid, owner, rows)
    assert URL not in target(tmp_path).read_text()
    with pytest.raises(ValueError):
        outputs.import_operation(sid, aid, owner, operation)
    with pytest.raises(ValueError):
        outputs.export_to(target(tmp_path), sid, aid, owner, rows, full=True)


def test_empty_history_is_read_only(context):
    db, sid, aid, owner, item = context
    before = db.path.read_bytes()
    assert CouponShareOutputs(db).list_saved(sid, aid) == ()
    assert db.path.read_bytes() == before

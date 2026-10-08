import pytest

from skyagent_manager.account_benefits import (
    AccountBenefitSession,
    SimulatedBenefitAdapter,
)
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.share_journal import ShareJournal
from skyagent_manager.share_safety import (
    RESTORE_BLOCK_KEY,
    require_share_writes_allowed,
)


@pytest.fixture
def context(tmp_path):
    db = StoreDatabase(tmp_path / "share.db", key=b"r" * 32)
    sid = db.add_store("A")
    aid = Accounts(db).add(sid, AccountInput("13812345678", "synthetic-secret-token"))
    session = AccountBenefitSession(db, SimulatedBenefitAdapter())
    item = session.query(sid, aid)[0]
    yield db, sid, aid, session.owner, item
    db.close()


@pytest.mark.parametrize("portable", [False, True])
def test_old_backup_without_hold_blocks_new_writes_after_restart(
    context, tmp_path, portable
):
    db, sid, aid, owner, item = context
    service = BackupService(db)
    password = "synthetic-backup-passphrase" if portable else None
    saved = (
        service.create_portable(tmp_path / "old.skyportable", password)
        if portable
        else service.create(tmp_path / "old.skybackup")
    )
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    ShareJournal(db).finish(item, operation, confirmed=True)
    safety = service.restore(saved, password)
    assert ShareJournal(db).state(item) is None  # The backup really predates the hold.
    with pytest.raises(ValueError, match="恢复备份"):
        ShareJournal(db).reserve(sid, aid, owner, item)
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        with pytest.raises(ValueError, match="恢复备份"):
            ShareJournal(reopened).reserve(sid, aid, owner, item)
        # Restoring the pre-restore safety copy is not an unblock mechanism.
        BackupService(reopened).restore(safety)
        with pytest.raises(ValueError):
            require_share_writes_allowed(reopened)
        assert ShareJournal(reopened).state(item)["state"] == "confirmed"
    finally:
        reopened.close()


def test_fresh_database_and_backup_creation_do_not_block(context, tmp_path):
    db, sid, aid, owner, item = context
    service = BackupService(db)
    service.create(tmp_path / "saved.skybackup")
    service.automatic()
    require_share_writes_allowed(db)
    assert ShareJournal(db).reserve(sid, aid, owner, item)


def test_direct_snapshot_replace_cannot_bypass_restore_block(context):
    db, sid, aid, owner, item = context
    db.replace_snapshot(db.connection.serialize())
    with pytest.raises(ValueError):
        ShareJournal(db).reserve(sid, aid, owner, item)
    # Queries and simulations remain independent of real-write protection.
    session = AccountBenefitSession(db, SimulatedBenefitAdapter())
    benefit = session.query(sid, aid)[0]
    assert session.share(sid, aid, (benefit.source, benefit.identifier)).startswith(
        "https://example.invalid/"
    )


@pytest.mark.parametrize(
    "value", ["", "0", "false", "broken", "restored-history-unverified"]
)
def test_blank_or_malformed_marker_never_unblocks(context, value):
    db, sid, aid, owner, item = context
    with db.connection:
        db.connection.execute(
            "INSERT INTO settings VALUES (?, ?)", (RESTORE_BLOCK_KEY, value)
        )
    before = db.path.read_bytes()
    with pytest.raises(ValueError):
        ShareJournal(db).reserve(sid, aid, owner, item)
    assert db.path.read_bytes() == before


def test_recovery_commits_only_once_with_block_already_present(context, monkeypatch):
    db, sid, aid, owner, item = context
    inventory = Inventory(db)
    inventory.import_values(sid, "breakfast", ["synthetic-stock-code"])
    task = inventory.start(sid, [inventory.list(sid)[0]["id"]])
    snapshot = db.connection.serialize()
    calls = []
    original_persist = db._persist

    def observe():
        assert db.get_setting(RESTORE_BLOCK_KEY)
        assert (
            db.connection.execute(
                "SELECT state FROM local_tasks WHERE id=?", (task,)
            ).fetchone()[0]
            == "interrupted"
        )
        calls.append(True)
        original_persist()

    monkeypatch.setattr(db, "_persist", observe)
    db.replace_snapshot(snapshot)
    assert len(calls) == 1
    assert db.connection.persist is not None


def test_failed_restore_rolls_back_marker_and_restores_persistence(
    context, tmp_path, monkeypatch
):
    db, sid, aid, owner, item = context
    saved = BackupService(db).create(tmp_path / "saved.skybackup")
    db.add_member(sid, "保留会员", "13912345678")
    before = db.path.read_bytes()
    callback = db.connection.persist

    def fail(*args):
        raise OSError("synthetic disk failure")

    with monkeypatch.context() as patch:
        patch.setattr("skyagent_manager.db.atomic_write", fail)
        with pytest.raises(OSError):
            BackupService(db).restore(saved)
    assert db.path.read_bytes() == before
    assert len(db.list_members(sid)) == 1
    assert db.connection.persist == callback
    require_share_writes_allowed(db)
    assert ShareJournal(db).reserve(sid, aid, owner, item)


def test_invalid_backup_leaves_no_marker_or_safety_file(context, tmp_path):
    db, sid, aid, owner, item = context
    saved = BackupService(db).create_portable(
        tmp_path / "saved.skyportable", "synthetic-backup-passphrase"
    )
    before = db.path.read_bytes()
    with pytest.raises(ValueError):
        BackupService(db).restore(saved)
    assert db.path.read_bytes() == before
    assert not BackupService(db).directory.exists()
    require_share_writes_allowed(db)


def test_recovery_failure_preserves_original_and_callback(context, monkeypatch):
    db, sid, aid, owner, item = context
    before = db.path.read_bytes()
    callback = db.connection.persist

    def fail(*args):
        raise RuntimeError("synthetic recovery failure")

    with monkeypatch.context() as patch:
        patch.setattr(Inventory, "recover", fail)
        with pytest.raises(RuntimeError):
            db.replace_snapshot(db.connection.serialize())
    assert db.path.read_bytes() == before
    assert db.connection.persist == callback
    require_share_writes_allowed(db)
    assert ShareJournal(db).reserve(sid, aid, owner, item)

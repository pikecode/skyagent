from dataclasses import replace

import pytest

from skyagent_manager.account_benefits import (
    AccountBenefitSession,
    SimulatedBenefitAdapter,
)
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.db import StoreDatabase
from skyagent_manager.share_journal import ShareJournal


@pytest.fixture
def context(tmp_path):
    db = StoreDatabase(tmp_path / "share.db", key=b"j" * 32)
    sid = db.add_store("A")
    aid = Accounts(db).add(sid, AccountInput("13812345678", "synthetic-secret-token"))
    session = AccountBenefitSession(db, SimulatedBenefitAdapter())
    item = session.query(sid, aid)[0]
    yield db, sid, aid, session.owner, item
    db.close()


@pytest.mark.parametrize("result", [None, False, True])
def test_restart_blocks_every_outcome(context, result):
    db, sid, aid, owner, item = context
    journal = ShareJournal(db)
    operation = journal.reserve(sid, aid, owner, item)
    if result is not None:
        journal.finish(item, operation, confirmed=result)
    reopened = StoreDatabase(db.path, key=b"j" * 32)
    try:
        assert ShareJournal(reopened).state(item)["state"] == (
            "pending" if result is None else "confirmed" if result else "unknown"
        )
        with pytest.raises(ValueError, match="禁止重复"):
            ShareJournal(reopened).reserve(sid, aid, owner, item)
        assert b"synthetic-secret-token" not in db.path.read_bytes()
        raw = db.get_setting(journal.resource_key(item)[0])
        assert item.identifier not in raw and item.code not in raw
    finally:
        reopened.close()


def test_alias_and_account_copy_cannot_bypass(context):
    db, sid, aid, owner, item = context
    journal = ShareJournal(db)
    journal.reserve(sid, aid, owner, item)
    other = db.add_store("B")
    other_account = Accounts(db).add(
        other, AccountInput("13812345678", "changed-token-long-enough")
    )
    other_owner = AccountBenefitSession(db, None)._identity(other, other_account)[1]
    for changed in (
        item,
        replace(item, identifier="new-id"),
        replace(item, code="new-code"),
    ):
        with pytest.raises(ValueError):
            journal.reserve(other, other_account, other_owner, changed)


def test_direct_identifier_equal_code_supported(context):
    db, sid, aid, owner, item = context
    item = replace(item, identifier=item.code)
    journal = ShareJournal(db)
    operation = journal.reserve(sid, aid, owner, item)
    journal.finish(item, operation, confirmed=True)
    assert journal.state(item)["state"] == "confirmed"


def test_result_save_failure_keeps_pending(context, monkeypatch):
    db, sid, aid, owner, item = context
    journal = ShareJournal(db)
    operation = journal.reserve(sid, aid, owner, item)
    before = db.path.read_bytes()

    def fail(*args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("skyagent_manager.db.atomic_write", fail)
        with pytest.raises(OSError):
            journal.finish(item, operation, confirmed=True)
    assert db.path.read_bytes() == before
    assert journal.state(item)["state"] == "pending"
    with pytest.raises(ValueError):
        journal.reserve(sid, aid, owner, item)


def test_disk_failure_returns_no_reservation(context, monkeypatch):
    db, sid, aid, owner, item = context
    before = db.path.read_bytes()

    def fail(*args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("skyagent_manager.db.atomic_write", fail)
        with pytest.raises(OSError):
            ShareJournal(db).reserve(sid, aid, owner, item)
    assert db.path.read_bytes() == before
    assert ShareJournal(db).state(item) is None


def test_identity_and_unshareable_reject_without_write(context):
    db, sid, aid, owner, item = context
    before = db.path.read_bytes()
    for identity, benefit in (
        (owner[:-1] + ("changed",), item),
        (owner, replace(item, shareable=False)),
    ):
        with pytest.raises(ValueError):
            ShareJournal(db).reserve(sid, aid, identity, benefit)
    assert db.path.read_bytes() == before


def test_wrong_operation_terminal_and_corrupt_records_block(context):
    db, sid, aid, owner, item = context
    journal = ShareJournal(db)
    operation = journal.reserve(sid, aid, owner, item)
    with pytest.raises(ValueError):
        journal.finish(item, "wrong")
    journal.finish(item, operation)
    with pytest.raises(ValueError):
        journal.finish(item, operation, confirmed=True)
    with db.connection:
        db.connection.execute(
            "UPDATE settings SET value='broken' WHERE name=?",
            (journal.resource_key(item)[0],),
        )
    with pytest.raises(ValueError):
        journal.reserve(sid, aid, owner, item)

import json
import sqlite3
from threading import Event

import pytest
from PySide6.QtWidgets import QMessageBox
from test_backend_query_ui import context as context
from test_backend_query_ui import wait
from test_coupon_sharing import URL, Session
from test_direct_benefits import setup

from skyagent_manager.coupon_sharing import CouponShareOutputs, DirectCouponShareAdapter
from skyagent_manager.inventory import Inventory
from skyagent_manager.security import decrypt
from skyagent_manager.share_journal import ShareJournal


def prepare(context):
    window, page, query_session, errors = setup(context)
    page.query()
    wait(window)
    page.table.setCurrentCell(0, 0)
    item = page.session.items[0]
    share_session = Session()
    page.share_adapter_factory = lambda: DirectCouponShareAdapter(
        session_factory=lambda: share_session
    )
    return window, page, item, share_session, errors


def test_confirmed_single_share_then_separate_stock_and_no_resend(context):
    window, page, item, remote, errors = prepare(context)
    sid, aid = page.session.owner[:2]

    def assert_pre_network_hold():
        # Independently read persisted ciphertext, not GUI-thread SQLite.
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(decrypt(window.db.path.read_bytes(), window.db.key))
            raw = connection.execute(
                "SELECT value FROM settings WHERE name=?",
                (ShareJournal.resource_key(item)[0],),
            ).fetchone()[0]
            assert json.loads(raw)["state"] == "pending"
        finally:
            connection.close()

    remote.callback = assert_pre_network_hold
    page.share()
    assert window.sync_worker is not None and not page.mode.isEnabled()
    wait(window)
    assert len(remote.calls) == 1 and not errors and remote.closed
    assert "加密保存" in page.result.text()
    assert ShareJournal(window.db).state(item)["state"] == "confirmed"
    assert CouponShareOutputs(window.db).load(sid, aid, item).url == URL
    assert not Inventory(window.db).list(sid)
    assert (
        URL not in page.result.text()
        and URL.encode() not in window.db.path.read_bytes()
    )
    page.table.setCurrentCell(0, 0)
    page.share()
    assert len(remote.calls) == 1 and errors
    errors.clear()
    page.import_saved_share()
    assert not errors and Inventory(window.db).list(sid)[0]["value"] == URL
    page.import_saved_share()
    assert len(Inventory(window.db).list(sid)) == 1 and errors
    assert page.mode.isEnabled()


def test_decline_share_has_no_hold_or_request(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)
    before = window.db.path.read_bytes()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.share()
    assert not remote.calls and not errors and window.sync_worker is None
    assert (
        ShareJournal(window.db).state(item) is None
        and window.db.path.read_bytes() == before
    )


def test_selection_changes_during_confirmation_rejected(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)

    def change(*args):
        page.table.setCurrentCell(1, 0)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change)
    page.share()
    assert errors and not remote.calls and ShareJournal(window.db).state(item) is None


@pytest.mark.parametrize("cancel", [False, True])
def test_cancel_or_identity_change_preserves_unknown(context, cancel):
    window, page, item, remote, errors = prepare(context)
    started, release = Event(), Event()

    def slow():
        started.set()
        assert release.wait(3)

    remote.callback = slow
    page.share()
    assert started.wait(3)
    if cancel:
        window._cancel_sync()
    else:
        page.mode.setCurrentIndex(0)  # Programmatic change must also cancel.
    release.set()
    wait(window)
    assert len(remote.calls) == 1
    assert ShareJournal(window.db).state(item)["state"] == "unknown"
    assert not Inventory(window.db).list(window.current_store)


def test_malformed_result_unknown_not_stock(context):
    window, page, item, remote, errors = prepare(context)
    remote.response.payload = (
        b'{"retcode":0,"result":{"shareUrl":"https://evil.invalid/private"}}'
    )
    page.share()
    wait(window)
    assert ShareJournal(window.db).state(item)["state"] == "unknown"
    assert not Inventory(window.db).list(window.current_store)
    assert "private" not in page.result.text()


def test_restore_block_has_no_request(context):
    window, page, item, remote, errors = prepare(context)
    window.db.replace_snapshot(window.db.connection.serialize())
    page.share()
    assert errors and "恢复备份" in errors[-1] and not remote.calls


def test_success_save_disk_failure_keeps_hold(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)
    started, release = Event(), Event()
    remote.callback = lambda: (started.set(), release.wait(3))
    page.share()
    assert started.wait(3)

    def fail(*args):
        raise OSError("synthetic disk error")

    with monkeypatch.context() as patch:
        patch.setattr("skyagent_manager.db.atomic_write", fail)
        release.set()
        wait(window)
    assert ShareJournal(window.db).state(item)["state"] == "pending"
    assert not Inventory(window.db).list(window.current_store)
    assert "禁止重发" in page.result.text()


def test_stock_confirmation_change_and_decline_no_write(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)
    page.share()
    wait(window)
    page.table.setCurrentCell(0, 0)
    before = window.db.path.read_bytes()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.import_saved_share()
    assert window.db.path.read_bytes() == before

    def change(*args):
        page.table.setCurrentCell(1, 0)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change)
    page.import_saved_share()
    assert errors and not Inventory(window.db).list(window.current_store)


def test_prop_and_simulation_do_not_create_real_share(context):
    window, page, item, remote, errors = prepare(context)
    page.table.setCurrentCell(3, 0)
    page.share()
    assert errors and not remote.calls
    page.mode.setCurrentIndex(0)
    page.import_saved_share()
    assert "模拟结果不能" in errors[-1]


def test_reservation_disk_failure_no_network(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)
    before = window.db.path.read_bytes()

    def fail(*args):
        raise OSError("synthetic disk failure")

    with monkeypatch.context() as patch:
        patch.setattr("skyagent_manager.db.atomic_write", fail)
        page.share()
    assert errors and not remote.calls and window.sync_worker is None
    assert (
        ShareJournal(window.db).state(item) is None
        and window.db.path.read_bytes() == before
    )

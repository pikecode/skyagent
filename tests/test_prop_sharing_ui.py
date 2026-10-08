from dataclasses import replace

from PySide6.QtWidgets import QMessageBox
from test_backend_query_ui import context as context
from test_backend_query_ui import wait
from test_coupon_sharing import URL
from test_direct_benefits import setup
from test_prop_sharing import Session

from skyagent_manager.coupon_sharing import CouponShareOutputs, DirectPropShareAdapter
from skyagent_manager.inventory import Inventory
from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog
from skyagent_manager.share_journal import ShareJournal


def prepare(context):
    window, page, query, errors = setup(context)
    coupon = query.payloads[3]["group"][0]["memberCouponResponse"]
    coupon.update(disType=7, couponsType=3, recommend=URL)
    page.query()
    wait(window)
    page.table.setCurrentCell(3, 0)
    item = page.session.items[3]
    remote = Session()
    page.prop_share_adapter_factory = lambda: DirectPropShareAdapter(
        session_factory=lambda: remote
    )
    return window, page, item, remote, errors


def test_prop_share_saved_and_explicit_independent_stock(context):
    window, page, item, remote, errors = prepare(context)
    page.share()
    wait(window)
    assert not errors and len(remote.calls) == 1 and remote.calls[0][0][0] == "GET"
    assert ShareJournal(window.db).state(item)["state"] == "confirmed"
    rows = CouponShareOutputs(window.db).list_saved(
        window.current_store, page.account.currentData()
    )
    assert rows[0].kind == "prop" and rows[0].url == URL
    assert not Inventory(window.db).list(window.current_store)
    page.table.setCurrentCell(3, 0)
    page.import_saved_share()
    assert not errors
    stock = Inventory(window.db).list(window.current_store)
    assert len(stock) == 1 and stock[0]["kind"] == "prop"
    page.mode.setCurrentIndex(0)
    dialog = SavedCouponSharesDialog(page)
    dialog.table.setCurrentCell(0, 0)
    assert dialog.table.item(0, 1).text() == "法宝"
    dialog.import_selected()
    assert "已处理" in dialog.status.text()
    dialog.close()


def test_decline_and_context_duplicate_no_more_requests(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)
    with monkeypatch.context() as patch:
        patch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
        page.share()
    assert not remote.calls and ShareJournal(window.db).state(item) is None
    page.share()
    wait(window)
    changed = replace(item, identifier="new-prop-id", code="new-prop-code")
    page.session.items = (*page.session.items[:3], changed)
    page.populate()
    page.table.setCurrentCell(3, 0)
    page.share()
    assert errors and len(remote.calls) == 1


def test_unknown_response_prop_context_also_blocked(context):
    window, page, item, remote, errors = prepare(context)
    remote.response.payload = (
        b'{"retcode":0,"result":{"recommend":"https://evil.invalid/private"}}'
    )
    page.share()
    wait(window)
    changed = replace(item, identifier="new-prop-id", code="new-prop-code")
    assert (
        ShareJournal(window.db).state_for_owner(
            window.current_store, page.account.currentData(), changed
        )["state"]
        == "unknown"
    )
    assert not Inventory(window.db).list(window.current_store)


def test_prop_context_changes_during_confirmation_no_request(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)

    def change(*args):
        page.session.items = (*page.session.items[:3], replace(item, dis_type="8"))
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change)
    page.share()
    assert errors and not remote.calls and ShareJournal(window.db).state(item) is None


def test_prop_restore_guard_no_request(context):
    window, page, item, remote, errors = prepare(context)
    window.db.replace_snapshot(window.db.connection.serialize())
    page.share()
    assert errors and "恢复备份" in errors[-1] and not remote.calls

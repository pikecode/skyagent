from PySide6.QtWidgets import QFileDialog, QMessageBox
from test_backend_query_ui import context as context
from test_backend_query_ui import wait
from test_gift_sharing import CODE, ORDER, Session

from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.direct_gifts import (
    DirectGiftOrderQueryAdapter,
    DirectGiftShareAdapter,
)
from skyagent_manager.gift_sharing import GiftShareOutputs
from skyagent_manager.inventory import Inventory
from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog
from skyagent_manager.share_journal import ShareJournal

TOKEN = "synthetic-gift-ui-authorized-token"


def prepare(context, *, direct=True):
    _app, window, _client, errors = context
    aid = Accounts(window.db).add(
        window.current_store, AccountInput("13812345678", TOKEN)
    )
    page = window.account_benefit_page
    page.refresh()
    page.account.setCurrentIndex(page.account.findData(aid))
    if direct:
        page.mode.setCurrentIndex(1)
    query, share = Session([ORDER]), Session({"shareCode": CODE})
    page.gift_query_adapter_factory = lambda: DirectGiftOrderQueryAdapter(
        session_factory=lambda: query
    )
    page.gift_share_adapter_factory = lambda: DirectGiftShareAdapter(
        session_factory=lambda: share
    )
    return window, page, query, share, errors


def queried(context):
    window, page, query, share, errors = prepare(context)
    page.query_orders()
    wait(window)
    page.table.setCurrentCell(0, 0)
    return window, page, query, share, errors


def completed(context):
    window, page, query, share, errors = queried(context)
    item = page.session.items[0]
    page.share()
    wait(window)
    return window, page, item, query, share, errors


def test_default_simulation_does_not_query_orders(context):
    window, page, query, share, errors = prepare(context, direct=False)
    page.query_orders()
    assert errors and not query.calls and not share.calls
    assert window.sync_worker is None


def test_read_orders_is_read_only_and_unknown_eligibility(context):
    window, page, query, share, errors = prepare(context)
    before = window.db.path.read_bytes()
    page.query_orders()
    wait(window)
    assert not errors and len(query.calls) == 1 and not share.calls
    assert window.db.path.read_bytes() == before
    item = page.session.items[0]
    assert not item.can_share() and item.can_generate_gift()
    assert "资格未验证" in page.table.item(0, 5).text()
    assert ORDER["folioId"] not in page.table.item(0, 2).text()
    assert ShareJournal(window.db).state(item) is None


def test_decline_order_query_no_requests(context, monkeypatch):
    window, page, query, share, errors = prepare(context)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.query_orders()
    assert not query.calls and not share.calls and not page.session.items


def test_cancel_read_discards_order_snapshot(context):
    window, page, query, share, errors = prepare(context)
    query.callback = lambda: page.query_worker.requestInterruption()
    page.query_orders()
    wait(window)
    assert len(query.calls) == 1 and not page.session.items and not share.calls


def test_generate_after_durable_reservation_save_masked_history_no_stock(context):
    window, page, query, share, errors = queried(context)
    before = window.db.path.read_bytes()
    share.callback = lambda: assert_persisted(window, before)
    page.share()
    wait(window)
    item = page.session.items[0]
    assert not errors and len(share.calls) == 1
    assert ShareJournal(window.db).state(item)["state"] == "confirmed"
    assert CODE not in page.result.text() and not Inventory(window.db).list(
        window.current_store
    )
    page.table.setCurrentCell(0, 0)
    page.import_saved_share()
    assert "有效期" in errors[-1] and not Inventory(window.db).list(
        window.current_store
    )
    page.mode.setCurrentIndex(0)
    dialog = SavedCouponSharesDialog(page, gift=True)
    try:
        assert dialog.table.rowCount() == 1 and len(dialog.action_buttons) == 4
        assert CODE not in " ".join(
            dialog.table.item(0, column).text() for column in range(6)
        )
        dialog.table.setCurrentCell(0, 0)
        dialog.import_selected()
        assert "未知" in dialog.status.text() and not Inventory(window.db).list(
            window.current_store
        )
    finally:
        dialog.close()


def assert_persisted(window, before):
    assert window.db.path.read_bytes() != before
    assert TOKEN.encode() not in window.db.path.read_bytes()


def test_decline_generate_no_hold_or_followup(context, monkeypatch):
    window, page, query, share, errors = queried(context)
    item = page.session.items[0]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.share()
    assert not share.calls and ShareJournal(window.db).state(item) is None


def test_generation_mode_change_during_confirmation_no_request(context, monkeypatch):
    window, page, query, share, errors = queried(context)

    def change(*args):
        page.mode.setCurrentIndex(0)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change)
    page.share()
    assert errors and not share.calls


def test_failed_response_unknown_blocks_second_request(context):
    window, page, query, share, errors = queried(context)
    item = page.session.items[0]
    share.response.payload = (
        b'{"retcode":0,"result":{"shareCode":"https://evil.invalid/secret"}}'
    )
    page.share()
    wait(window)
    assert ShareJournal(window.db).state(item)["state"] == "unknown"
    page.table.setCurrentCell(0, 0)
    page.share()
    assert errors and len(share.calls) == 1
    assert not GiftShareOutputs(window.db).list_saved(
        window.current_store, page.account.currentData()
    )


def test_cancel_generation_keeps_unknown_and_no_result(context):
    window, page, query, share, errors = queried(context)
    item = page.session.items[0]
    share.callback = lambda: page.share_worker.requestInterruption()
    page.share()
    wait(window)
    assert ShareJournal(window.db).state(item)["state"] == "unknown"
    assert len(share.calls) == 1 and not GiftShareOutputs(window.db).list_saved(
        window.current_store, page.account.currentData()
    )


def test_offline_full_code_export_never_generates(context, monkeypatch, tmp_path):
    window, page, item, query, share, errors = completed(context)
    page.mode.setCurrentIndex(0)
    dialog = SavedCouponSharesDialog(page, gift=True)
    before = window.db.path.read_bytes()
    path = tmp_path.parent / (tmp_path.name + "-gift-export.csv")
    try:
        dialog.table.setCurrentCell(0, 0)
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *args: (str(path), "")
        )
        dialog.export_masked()
        assert CODE not in path.read_text()
        dialog.export_full()
        assert CODE in path.read_text()
        assert window.db.path.read_bytes() == before
        assert len(query.calls) == 1 and len(share.calls) == 1
    finally:
        dialog.close()


def test_export_identity_change_after_file_selection_no_file(
    context, monkeypatch, tmp_path
):
    window, page, item, query, share, errors = completed(context)
    dialog = SavedCouponSharesDialog(page, gift=True)
    path = tmp_path.parent / (tmp_path.name + "-changed.csv")

    def change(*args):
        page.mode.setCurrentIndex(0)
        return str(path), ""

    try:
        dialog.table.setCurrentCell(0, 0)
        monkeypatch.setattr(QFileDialog, "getSaveFileName", change)
        dialog.export_full()
        assert not path.exists() and "变化" in dialog.status.text()
    finally:
        dialog.close()

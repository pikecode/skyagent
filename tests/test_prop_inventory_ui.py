from PySide6.QtWidgets import QMessageBox
from test_backend_query_ui import context as context
from test_backend_query_ui import wait
from test_coupon_sharing import URL
from test_prop_sharing_ui import prepare

from skyagent_manager.coupon_sharing import CouponShareOutputs
from skyagent_manager.inventory import Inventory
from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog


def history(context):
    window, page, item, remote, errors = prepare(context)
    page.share()
    wait(window)
    page.mode.setCurrentIndex(0)
    dialog = SavedCouponSharesDialog(page)
    dialog.table.setCurrentCell(0, 0)
    return window, page, item, remote, errors, dialog


def test_offline_prop_history_import_without_live_query_and_masked_inventory(context):
    window, page, item, remote, errors, dialog = history(context)
    try:
        assert not page.session.items
        dialog.import_selected()
        assert not errors and "已入本地" in dialog.status.text()
        stock = Inventory(window.db).list(window.current_store)
        assert len(stock) == 1 and stock[0]["kind"] == "prop"
        assert stock[0]["value"] == URL and len(remote.calls) == 1
        inventory = window.inventory_page
        inventory.kind.setCurrentIndex(inventory.kind.findData("prop"))
        inventory.refresh()
        assert inventory.table.rowCount() == 1
        assert inventory.table.item(0, 1).text() == "法宝分享资源"
        assert URL not in inventory.table.item(0, 2).text()
        dialog.table.setCurrentCell(0, 0)
        dialog.import_selected()
        assert "已处理" in dialog.status.text()
        assert len(Inventory(window.db).list(window.current_store)) == 1
    finally:
        dialog.close()


def test_decline_history_stock_has_no_write_or_network(context, monkeypatch):
    window, page, item, remote, errors, dialog = history(context)
    before = window.db.path.read_bytes()
    try:
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
        dialog.import_selected()
        assert not Inventory(window.db).list(window.current_store)
        assert window.db.path.read_bytes() == before and len(remote.calls) == 1
    finally:
        dialog.close()


def test_history_store_change_during_confirmation_no_stock(context, monkeypatch):
    window, page, item, remote, errors, dialog = history(context)
    other = window.db.add_store("other")

    def change(*args):
        window.current_store = other
        return QMessageBox.Yes

    try:
        monkeypatch.setattr(QMessageBox, "question", change)
        dialog.import_selected()
        assert "当前门店下找不到该账号" in dialog.status.text()
        assert not Inventory(window.db).list(dialog.owner[0])
        assert not Inventory(window.db).list(other) and len(remote.calls) == 1
    finally:
        dialog.close()


def test_current_prop_stock_confirmation_identity_change_no_stock(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)
    page.share()
    wait(window)
    page.table.setCurrentCell(3, 0)

    def change(*args):
        page.mode.setCurrentIndex(0)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change)
    page.import_saved_share()
    assert errors and not Inventory(window.db).list(window.current_store)
    assert len(remote.calls) == 1


def test_prop_history_restore_block_no_stock(context):
    window, page, item, remote, errors, dialog = history(context)
    from skyagent_manager.share_safety import RESTORE_BLOCK_KEY

    try:
        with window.db.connection:
            window.db.connection.execute(
                "INSERT INTO settings VALUES(?,?)",
                (RESTORE_BLOCK_KEY, "synthetic-restore"),
            )
        dialog.import_selected()
        assert "恢复" in dialog.status.text()
        assert not Inventory(window.db).list(window.current_store)
        assert len(remote.calls) == 1
        assert (
            not CouponShareOutputs(window.db)
            .list_saved(dialog.owner[0], dialog.owner[1])[0]
            .stock_id
        )
    finally:
        dialog.close()

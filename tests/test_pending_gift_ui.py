from PySide6.QtWidgets import QMessageBox
from test_backend_query_ui import context as context
from test_gift_sharing import CODE
from test_gift_sharing_ui import completed

from skyagent_manager.gift_sharing import PendingGiftInventory
from skyagent_manager.inventory import Inventory
from skyagent_manager.pending_gift_dialog import PendingGiftDialog
from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog


def history(context):
    window, page, item, query, share, errors = completed(context)
    page.mode.setCurrentIndex(0)
    dialog = SavedCouponSharesDialog(page, gift=True)
    dialog.table.setCurrentCell(0, 0)
    return window, page, query, share, dialog


def test_confirm_pending_view_is_masked_no_network_or_consumable_stock(context):
    window, page, query, share, dialog = history(context)
    try:
        dialog.import_selected()
        assert "已登记待核验" in dialog.status.text()
        assert not Inventory(window.db).list(window.current_store)
        before = window.db.path.read_bytes()
        pending = PendingGiftDialog(window)
        try:
            assert pending.table.rowCount() == 1
            assert CODE not in " ".join(
                pending.table.item(0, column).text() for column in range(5)
            )
            pending.refresh()
            assert window.db.path.read_bytes() == before
            assert len(query.calls) == len(share.calls) == 1
        finally:
            pending.close()
        dialog.table.setCurrentCell(0, 0)
        dialog.import_selected()
        assert "已登记" in dialog.status.text()
        assert len(PendingGiftInventory(window.db).list(window.current_store)) == 1
    finally:
        dialog.close()


def test_decline_pending_register_no_write(context, monkeypatch):
    window, page, query, share, dialog = history(context)
    before = window.db.path.read_bytes()
    try:
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
        dialog.import_selected()
        assert not PendingGiftInventory(window.db).list(window.current_store)
        assert window.db.path.read_bytes() == before
    finally:
        dialog.close()


def test_identity_change_during_confirmation_no_register(context, monkeypatch):
    window, page, query, share, dialog = history(context)
    store = window.current_store

    def change(*args):
        page.mode.setCurrentIndex(1)
        return QMessageBox.Yes

    try:
        monkeypatch.setattr(QMessageBox, "question", change)
        dialog.import_selected()
        assert "变化" in dialog.status.text()
        assert not PendingGiftInventory(window.db).list(store)
    finally:
        dialog.close()


def test_pending_view_store_change_clears_rows(context):
    window, page, query, share, dialog = history(context)
    try:
        dialog.import_selected()
        pending = PendingGiftDialog(window)
        try:
            assert pending.table.rowCount() == 1
            other = next(
                row["id"]
                for row in window.db.list_stores()
                if row["id"] != window.current_store
            )
            window.store_combo.setCurrentIndex(window.store_combo.findData(other))
            assert pending.table.rowCount() == 0
            assert not pending.refresh_button.isEnabled()
            assert "变化" in pending.status.text()
        finally:
            pending.close()
    finally:
        dialog.close()

import json

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox
from test_backend_query_ui import context as context
from test_backend_query_ui import wait
from test_coupon_sharing import URL
from test_coupon_sharing_ui import prepare

from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.coupon_sharing import CouponShareOutputs
from skyagent_manager.inventory import Inventory
from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog


def local_history(context, monkeypatch):
    window, page, item, remote, errors = prepare(context)
    page.share()
    wait(window)
    page.mode.setCurrentIndex(0)
    assert not page.session.items  # No live coupon/query snapshot remains.

    def forbidden(*args, **kwargs):
        raise AssertionError("saved history must not network")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    dialog = SavedCouponSharesDialog(page)
    dialog.table.setCurrentCell(0, 0)
    return window, page, item, remote, dialog


def target(tmp_path):
    return tmp_path.parent / (tmp_path.name + "-history.csv")


def test_masked_history_import_independent_of_original_coupon(context, monkeypatch):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    before = window.db.path.read_bytes()
    assert dialog.table.rowCount() == 1 and not page.session.items
    texts = " ".join(dialog.table.item(0, i).text() for i in range(6))
    assert URL not in texts and item.code not in texts
    assert window.db.path.read_bytes() == before
    dialog.import_selected()
    assert Inventory(window.db).list(window.current_store)[0]["value"] == URL
    assert "已入本地资源库存" in dialog.status.text()
    assert len(remote.calls) == 1
    dialog.close()


def test_entry_available_in_default_mode_without_query(context, monkeypatch):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    dialog.close()
    seen = []

    def inspect(self):
        seen.append(self.table.rowCount())
        return 0

    monkeypatch.setattr(SavedCouponSharesDialog, "exec", inspect)
    page.open_saved_shares()
    assert seen == [1] and page.saved_dialog is None and len(remote.calls) == 1


def test_offline_history_clears_immediately_on_empty_store_without_query_binding(
    context,
    monkeypatch,
):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    dialog.close()
    assert page.binding is None and not page.session.items
    inspected = []

    def inspect(self):
        assert page.saved_dialog is self and self.table.rowCount() == 1
        store, account = self.owner[:2]
        before = CouponShareOutputs(window.db).list_saved(store, account)
        window.store_combo.setCurrentIndex(1)
        assert page.account.count() == 0
        assert self.table.rowCount() == 0 and not self.rows
        assert all(not button.isEnabled() for button in self.action_buttons)
        assert "上下文已变化" in self.status.text()
        # Store switching legitimately saves last_store_id; business history
        # and inventory must remain unchanged, not necessarily ciphertext.
        assert CouponShareOutputs(window.db).list_saved(store, account) == before
        assert not Inventory(window.db).list(store)
        inspected.append(True)
        return 0

    monkeypatch.setattr(SavedCouponSharesDialog, "exec", inspect)
    page.open_saved_shares()
    assert inspected == [True] and page.saved_dialog is None
    assert len(remote.calls) == 1


@pytest.mark.parametrize("full", [False, True])
def test_export_masked_or_explicit_full_with_no_state_change(
    context, monkeypatch, tmp_path, full
):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    path = target(tmp_path)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    before = window.db.path.read_bytes()
    dialog.export_full() if full else dialog.export_masked()
    content = path.read_text(encoding="utf-8-sig")
    assert (URL in content) is full
    assert "13812345678" not in content and item.code not in content
    assert window.db.path.read_bytes() == before and len(remote.calls) == 1
    dialog.close()


def test_decline_full_export_and_cancel_file_no_write(context, monkeypatch, tmp_path):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    path = target(tmp_path)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    dialog.export_full()
    assert not path.exists()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    dialog.export_full()
    assert not path.exists()
    dialog.close()


def test_store_change_during_confirmation_no_import(context, monkeypatch):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    old_store = window.current_store

    def change(*args):
        window.store_combo.setCurrentIndex(1)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change)
    dialog.import_selected()
    assert not Inventory(window.db).list(old_store)
    assert "变化" in dialog.status.text()
    dialog.close()


def test_file_dialog_context_change_preserves_existing_export(
    context, monkeypatch, tmp_path
):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    path = target(tmp_path)
    path.write_bytes(b"original")

    def change(*args):
        page.mode.setCurrentIndex(1)
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", change)
    dialog.export_full()
    assert path.read_bytes() == b"original" and "变化" in dialog.status.text()
    dialog.close()


def test_record_changes_during_confirmation_no_import(context, monkeypatch):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    key = CouponShareOutputs.PREFIX + dialog.rows[0].operation_id

    def change(*args):
        payload = json.loads(window.db.get_setting(key))
        payload["expiry"] = "2098-01-01"
        with window.db.connection:
            window.db.connection.execute(
                "UPDATE settings SET value=? WHERE name=?", (json.dumps(payload), key)
            )
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change)
    dialog.import_selected()
    assert (
        not Inventory(window.db).list(window.current_store)
        and "变化" in dialog.status.text()
    )
    dialog.close()


def test_restore_history_readable_but_import_export_blocked(
    context, monkeypatch, tmp_path
):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    dialog.close()
    window.db.replace_snapshot(window.db.connection.serialize())
    dialog = SavedCouponSharesDialog(page)
    dialog.table.setCurrentCell(0, 0)
    assert dialog.table.rowCount() == 1
    dialog.import_selected()
    assert "恢复备份" in dialog.status.text()
    path = target(tmp_path)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    dialog.export_full()
    assert not path.exists()
    dialog.export_masked()
    assert URL not in path.read_text(encoding="utf-8-sig")
    dialog.close()


def test_legacy_missing_expiry_cannot_import(context, monkeypatch):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    key = CouponShareOutputs.PREFIX + dialog.rows[0].operation_id
    payload = json.loads(window.db.get_setting(key))
    for field in ("expiry", "resource_keys", "created_at"):
        payload.pop(field)
    with window.db.connection:
        window.db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?", (json.dumps(payload), key)
        )
    dialog.refresh()
    dialog.table.setCurrentCell(0, 0)
    assert "未知" in dialog.table.item(0, 3).text()
    dialog.import_selected()
    assert "旧版" in dialog.status.text() and not Inventory(window.db).list(
        window.current_store
    )
    dialog.close()


def test_empty_and_corrupt_history_no_partial_rows(context, monkeypatch):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    key = CouponShareOutputs.PREFIX + dialog.rows[0].operation_id
    with window.db.connection:
        window.db.connection.execute(
            "UPDATE settings SET value='broken' WHERE name=?", (key,)
        )
    dialog.refresh()
    assert (
        dialog.table.rowCount() == 0
        and not dialog.rows
        and "异常" in dialog.status.text()
    )
    dialog.export_full()
    assert "选择" in dialog.status.text()
    dialog.close()


def test_changed_token_during_file_selection_no_export(context, monkeypatch, tmp_path):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    path = target(tmp_path)
    sid, aid = dialog.owner[:2]

    def change(*args):
        Accounts(window.db).update(
            sid, aid, AccountInput("13812345678", "synthetic-changed-token")
        )
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", change)
    dialog.export_full()
    assert not path.exists() and "变化" in dialog.status.text()
    dialog.close()


def test_active_dialog_invalidated_on_mode_change(context, monkeypatch):
    window, page, item, remote, dialog = local_history(context, monkeypatch)
    page.saved_dialog = dialog
    page.mode.setCurrentIndex(1)
    assert dialog.table.rowCount() == 0 and not dialog.rows
    assert all(not button.isEnabled() for button in dialog.action_buttons)
    page.saved_dialog = None
    dialog.close()

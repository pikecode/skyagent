"""Programmatic native Qt smoke check: temporary synthetic data, no network/keychain."""

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offscreen", action="store_true")
    options = parser.parse_args()
    output = options.output.resolve()
    if output.exists():
        parser.error("Use a new output directory; existing evidence is preserved.")
    output.mkdir(parents=True)
    if options.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    import requests
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

    from skyagent_manager import __version__
    from skyagent_manager.accounts import AccountInput, Accounts
    from skyagent_manager.db import StoreDatabase
    from skyagent_manager.inventory import Inventory
    from skyagent_manager.main_window import MainWindow

    def forbidden(*args, **kwargs):
        raise AssertionError("Desktop acceptance must not make network requests")

    requests.Session.request = forbidden
    app = QApplication([])
    report = {
        "ok": False,
        "version": __version__,
        "scope": "programmatic-local-synthetic",
        "qt_platform": app.platformName(),
        "network": "blocked",
        "native_credentials": "not-accessed",
        "human_mouse_keyboard_acceptance": False,
    }
    window = None
    db = None
    try:
        if not options.offscreen and app.platformName() not in {"cocoa", "windows"}:
            raise RuntimeError("Native macOS or Windows Qt platform required")
        with TemporaryDirectory(prefix="skyagent-desktop-acceptance-") as temporary:
            db = StoreDatabase(Path(temporary) / "manager.sqlite3", key=b"d" * 32)
            store_a = db.add_store("验收A（虚构）")
            store_b = db.add_store("验收B（虚构）")
            db.add_member(store_a, "虚构会员", "13800000000")
            Accounts(db).add(
                store_a,
                AccountInput(
                    "13900000000", "synthetic-desktop-token", is_new_user=False
                ),
            )
            Inventory(db).import_values(store_a, "gift", ["synthetic-desktop-gift"])
            from skyagent_manager.store_selector import StoreSelectorDialog

            selector_before = db.path.read_bytes()
            selector = StoreSelectorDialog(db)
            selector.show()
            app.processEvents()
            if not selector.grab().save(str(output / "startup-store-selector.png")):
                raise RuntimeError("Store selector screenshot failed")
            if selector.current_store()["id"] != store_a:
                raise RuntimeError("Store selector initial store mismatch")
            selector.reject()
            app.processEvents()
            if db.path.read_bytes() != selector_before:
                raise RuntimeError("Read-only selector changed database")
            report["startup_selector_checked"] = True
            window = MainWindow(db.path, database=db, initial_store_id=store_a)
            window.resize(1120, 720)
            window.show()
            app.processEvents()
            report["window"] = [window.width(), window.height()]
            report["minimum"] = [
                window.minimumSizeHint().width(),
                window.minimumSizeHint().height(),
            ]
            if window.height() != 720 or window.width() != 1120:
                raise RuntimeError("Window cannot fit the intended 1120x720 size")
            checked = []
            for index in range(window.tabs.count()):
                window.tabs.setCurrentIndex(index)
                app.processEvents()
                if window.height() != 720:
                    raise RuntimeError("A tab forces excessive window height")
                checked.append(window.tabs.tabText(index))
                if not window.grab().save(str(output / f"tab-{index}.png")):
                    raise RuntimeError("Screenshot save failed")
            report["tabs_checked"] = checked
            window.store_combo.setCurrentIndex(window.store_combo.findData(store_b))
            app.processEvents()
            if window.account_page.table.rowCount() or window.members_table.rowCount():
                raise RuntimeError("Store B exposes store A data")
            window.store_combo.setCurrentIndex(window.store_combo.findData(store_a))
            app.processEvents()
            if (
                window.account_page.table.rowCount() != 1
                or window.members_table.rowCount() != 1
            ):
                raise RuntimeError("Store A bindings were not restored")
            report["store_isolation"] = True
            from skyagent_manager.account_benefits import (
                AccountBenefitSession,
                SimulatedBenefitAdapter,
            )
            from skyagent_manager.coupon_sharing import CouponShareOutputs
            from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog
            from skyagent_manager.share_journal import ShareJournal

            benefit_page = window.account_benefit_page
            account_id = benefit_page.account.currentData()
            synthetic = AccountBenefitSession(db, SimulatedBenefitAdapter())
            coupon = synthetic.query(store_a, account_id)[0]
            operation = ShareJournal(db).reserve(
                store_a, account_id, synthetic.owner, coupon
            )
            CouponShareOutputs(db).save(
                store_a,
                account_id,
                synthetic.owner,
                coupon,
                operation,
                "https://mobile.yaduo.com/share?code=synthetic-desktop-output",
            )
            history_before = db.path.read_bytes()
            history = SavedCouponSharesDialog(benefit_page)
            history.show()
            app.processEvents()
            if history.table.rowCount() != 1 or not all(
                button.isVisible() for button in history.action_buttons
            ):
                raise RuntimeError("Saved share history controls unavailable")
            if not history.grab().save(str(output / "saved-share-history.png")):
                raise RuntimeError("Saved share history screenshot failed")
            history.close()
            if db.path.read_bytes() != history_before:
                raise RuntimeError("Saved share history modified database")
            report["saved_share_history_checked"] = True
            prop = replace(
                coupon,
                source="prop",
                kind="prop",
                identifier="synthetic-desktop-prop",
                code="synthetic-desktop-prop-code",
                dis_type="7",
                coupons_type="3",
            )
            prop_operation = ShareJournal(db).reserve(
                store_a, account_id, synthetic.owner, prop
            )
            CouponShareOutputs(db).save(
                store_a,
                account_id,
                synthetic.owner,
                prop,
                prop_operation,
                "https://mobile.yaduo.com/share?code=synthetic-desktop-prop-output",
            )
            prop_history = SavedCouponSharesDialog(benefit_page)
            prop_history.show()
            app.processEvents()
            for index, saved in enumerate(prop_history.rows):
                if saved.operation_id == prop_operation:
                    prop_history.table.setCurrentCell(index, 0)
                    break
            from unittest.mock import patch

            with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                prop_history.import_selected()
            prop_stock = [
                row for row in Inventory(db).list(store_a) if row["kind"] == "prop"
            ]
            if len(prop_stock) != 1 or "已入本地" not in prop_history.status.text():
                raise RuntimeError("Prop history inventory import failed")
            prop_history.close()
            inventory_page = window.inventory_page
            window.tabs.setCurrentWidget(inventory_page)
            inventory_page.kind.setCurrentIndex(inventory_page.kind.findData("prop"))
            inventory_page.refresh()
            app.processEvents()
            if (
                inventory_page.table.rowCount() != 1
                or inventory_page.table.item(0, 1).text() != "法宝分享资源"
            ):
                raise RuntimeError("Prop inventory filter failed")
            if not window.grab().save(str(output / "prop-inventory.png")):
                raise RuntimeError("Prop inventory screenshot failed")
            report["prop_inventory_checked"] = True
            from skyagent_manager.gift_sharing import GiftShareOutputs

            gift = replace(
                coupon,
                source="order",
                kind="gift",
                identifier="desktop-folio:desktop-chain",
                code="desktop-folio",
                expiry="",
                status="unknown",
                folio_id="desktop-folio",
                chain_id="desktop-chain",
            )
            gift_operation = ShareJournal(db).reserve(
                store_a, account_id, synthetic.owner, gift
            )
            GiftShareOutputs(db).save(
                store_a,
                account_id,
                synthetic.owner,
                gift,
                gift_operation,
                "synthetic-desktop-gift-code",
            )
            gift_before = db.path.read_bytes()
            gift_history = SavedCouponSharesDialog(benefit_page, gift=True)
            gift_history.show()
            app.processEvents()
            if (
                gift_history.table.rowCount() != 1
                or len(gift_history.action_buttons) != 4
                or not all(button.isVisible() for button in gift_history.action_buttons)
            ):
                raise RuntimeError("Gift saved history unavailable")
            if not gift_history.grab().save(str(output / "gift-share-history.png")):
                raise RuntimeError("Gift history screenshot failed")
            gift_history.close()
            if db.path.read_bytes() != gift_before:
                raise RuntimeError("Gift history modified database")
            report["gift_history_checked"] = True
            from skyagent_manager.gift_sharing import PendingGiftInventory
            from skyagent_manager.pending_gift_dialog import PendingGiftDialog

            PendingGiftInventory(db).register(
                store_a, account_id, synthetic.owner, gift_operation
            )
            pending_before = db.path.read_bytes()
            pending_dialog = PendingGiftDialog(window)
            pending_dialog.show()
            app.processEvents()
            if pending_dialog.table.rowCount() != 1:
                raise RuntimeError("Pending gift view failed")
            if not pending_dialog.grab().save(
                str(output / "pending-gift-inventory.png")
            ):
                raise RuntimeError("Pending gift screenshot failed")
            pending_dialog.close()
            if db.path.read_bytes() != pending_before:
                raise RuntimeError("Pending gift view modified database")
            report["pending_gift_inventory_checked"] = True
            page = window.backend_query_page
            window.tabs.setCurrentWidget(page)
            task_button = next(
                button
                for button in page.findChildren(QPushButton)
                if button.text() == "确认查询已有兑换任务"
            )
            for name, target in (
                ("backend-top", page.username),
                ("backend-bottom", task_button),
            ):
                page.scroll_area.ensureWidgetVisible(target)
                app.processEvents()
                position = target.mapTo(page.scroll_area.viewport(), QPoint(0, 0))
                if (
                    not page.scroll_area.viewport().rect().contains(position)
                    or position.y() + target.height()
                    > page.scroll_area.viewport().height()
                ):
                    raise RuntimeError("Backend control cannot be reached")
                if not window.grab().save(str(output / f"{name}.png")):
                    raise RuntimeError("Screenshot save failed")
            report["backend_controls_reachable"] = True
            if window.sync_worker is not None:
                raise RuntimeError("Unexpected background operation")
            window.close()
            window = None
            db = None
            app.processEvents()
            report["ok"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
    finally:
        if window is not None:
            window.close()
            app.processEvents()
        elif db is not None:
            db.close()
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

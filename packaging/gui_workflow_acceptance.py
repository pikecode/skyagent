"""Programmatic Qt coupon/gift workflows with synthetic temporary data."""

import argparse
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workflow_acceptance import (  # noqa: E402
    GIFT_CODE,
    TOKEN,
    URL,
    SyntheticGiftTransport,
    SyntheticTransport,
    require,
)


def run(output, *, offscreen=False, kind="coupon"):
    if kind not in {"coupon", "gift"}:
        raise ValueError("Unsupported acceptance workflow")
    gift = kind == "gift"
    query_count = 1 if gift else 4
    secret_output = GIFT_CODE if gift else URL
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton

    from skyagent_manager import __version__
    from skyagent_manager.accounts import Accounts, parse_account_text
    from skyagent_manager.coupon_sharing import (
        CouponShareOutputs,
        DirectCouponShareAdapter,
    )
    from skyagent_manager.db import StoreDatabase
    from skyagent_manager.direct_benefits import DirectBenefitQueryAdapter
    from skyagent_manager.direct_gifts import (
        DirectGiftOrderQueryAdapter,
        DirectGiftShareAdapter,
    )
    from skyagent_manager.gift_sharing import GiftShareOutputs, PendingGiftInventory
    from skyagent_manager.inventory import Inventory
    from skyagent_manager.main_window import MainWindow
    from skyagent_manager.pending_gift_dialog import PendingGiftDialog
    from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog
    from skyagent_manager.security import decrypt
    from skyagent_manager.share_journal import ShareJournal

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    report = {
        "ok": False,
        "version": __version__,
        "qt_platform": app.platformName(),
        "scope": f"programmatic-qt-synthetic-{kind}-workflow",
        "resource_kind": kind,
        "network": "blocked",
        "production_verified": False,
        "human_mouse_keyboard_acceptance": False,
        "native_credentials": "not-accessed",
        "user_database_accessed": False,
        "confirmations": [],
        "steps": [],
    }
    calls, sessions, errors = [], [], []
    window = dialog = pending = db = None
    decision = QMessageBox.Yes

    def confirmation(_parent, title, *_args):
        report["confirmations"].append(
            {"title": title, "accepted": decision == QMessageBox.Yes}
        )
        return decision

    def factory(*, before_share=None):
        transport_type = SyntheticGiftTransport if gift else SyntheticTransport
        transport = transport_type(calls, before_share=before_share)
        sessions.append(transport)
        return transport

    def wait_worker():
        loop, timer = QEventLoop(), QTimer()
        timer.timeout.connect(
            lambda: loop.quit() if window.sync_worker is None else None
        )
        timer.start(5)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        timer.stop()
        require(window.sync_worker is None)
        require(not errors)

    def screenshot(widget, filename):
        app.processEvents()
        require(widget.grab().save(str(output / filename)))

    def attach(database, store):
        result = MainWindow(database.path, database=database, initial_store_id=store)
        result._error = errors.append
        result.resize(1120, 720)
        result.show()
        result.tabs.setCurrentWidget(result.account_benefit_page)
        app.processEvents()
        return result

    try:
        require(offscreen or app.platformName() in {"cocoa", "windows"})
        with (
            patch(
                "requests.sessions.Session.request",
                side_effect=RuntimeError("Network forbidden"),
            ),
            patch.object(QMessageBox, "question", confirmation),
            patch.object(
                QFileDialog,
                "getSaveFileName",
                return_value=(str(output / "masked-history.csv"), ""),
            ),
            TemporaryDirectory(prefix="skyagent-gui-workflow-") as temporary,
        ):
            key = secrets.token_bytes(32)
            path = Path(temporary) / "data" / "manager.sqlite3"
            db = StoreDatabase(path, key=key)
            store = db.add_store("虚构界面验收门店")
            other = db.add_store("虚构隔离门店")
            imported = Accounts(db).import_rows(
                store, parse_account_text(f"13900000000----{TOKEN}")
            )
            require(imported.inserted == 1)
            # Account setup is via the repository, not an account-import UI claim.
            window = attach(db, store)
            page = window.account_benefit_page
            account = page.account.currentData()
            page.mode.setCurrentIndex(page.mode.findData("direct"))
            if gift:
                page.gift_query_adapter_factory = lambda: DirectGiftOrderQueryAdapter(
                    session_factory=factory
                )
            else:
                page.session.adapter = DirectBenefitQueryAdapter(
                    session_factory=factory
                )
            before = path.read_bytes()
            if gift:
                query_button = next(
                    button
                    for button in page.findChildren(QPushButton)
                    if button.text() == "确认直连查询订单（不生成礼包）"
                )
                query_button.click()
            else:
                page.query_button.click()
            require(
                window.sync_worker is not None and not window.store_combo.isEnabled()
            )
            wait_worker()
            require(
                page.table.rowCount() == (1 if gift else 3)
                and len(calls) == query_count
            )
            require(path.read_bytes() == before and window.store_combo.isEnabled())
            page.table.setCurrentCell(0, 0)
            item = page.session.items[0]
            if gift:
                require(item.status == "unknown" and not item.expiry)
                require("资格未验证" in page.table.item(0, 5).text())
            require(item.code not in page.table.item(0, 2).text())
            screenshot(window, "01-query.png")
            report["steps"].append("query-button-worker-lock-and-masked-snapshot")

            def durable_pending():
                connection = sqlite3.connect(":memory:")
                try:
                    connection.deserialize(decrypt(path.read_bytes(), key))
                    raw = connection.execute(
                        "SELECT value FROM settings WHERE name=?",
                        (ShareJournal.resource_key(item)[0],),
                    ).fetchone()[0]
                    require(json.loads(raw)["state"] == "pending")
                finally:
                    connection.close()

            adapter_type = DirectGiftShareAdapter if gift else DirectCouponShareAdapter

            def share_factory():
                return adapter_type(
                    session_factory=lambda: factory(before_share=durable_pending)
                )

            if gift:
                page.gift_share_adapter_factory = share_factory
            else:
                page.share_adapter_factory = share_factory
            decision = QMessageBox.No
            page.share_button.click()
            require(len(calls) == query_count and ShareJournal(db).state(item) is None)
            require(path.read_bytes() == before)
            report["steps"].append("declined-share-no-request-no-hold")
            decision = QMessageBox.Yes
            page.share_button.click()
            require(window.sync_worker is not None and not page.mode.isEnabled())
            wait_worker()
            require(
                len(calls) == query_count + 1
                and ShareJournal(db).state(item)["state"] == "confirmed"
            )
            require(
                "加密保存" in page.result.text()
                and secret_output not in page.result.text()
            )
            require(secret_output.encode() not in path.read_bytes())
            require(not Inventory(db).list(store))
            before_replay = path.read_bytes()
            page.table.setCurrentCell(0, 0)
            page.share_button.click()
            require(window.sync_worker is None and len(errors) == 1)
            require(
                len(calls) == query_count + 1 and path.read_bytes() == before_replay
            )
            errors.clear()
            report["duplicate_generation_blocked"] = True
            screenshot(window, "02-share-saved.png")
            report["steps"].append("confirmed-share-button-durable-save-no-auto-stock")
            window.close()
            app.processEvents()
            window = db = None

            db = StoreDatabase(path, key=key)
            window = attach(db, store)
            page = window.account_benefit_page
            require(page.mode.currentData() == "simulation" and not page.session.items)
            before = path.read_bytes()
            dialog = SavedCouponSharesDialog(page, gift=gift)
            # Keep the same owner registration as _open_saved_results; the
            # acceptance driver uses show() instead of blocking modal exec().
            page.saved_dialog = dialog
            dialog.show()
            app.processEvents()
            require(dialog.table.rowCount() == 1 and path.read_bytes() == before)
            dialog.table.setCurrentCell(0, 0)
            require(
                secret_output
                not in " ".join(
                    dialog.table.item(0, column).text() for column in range(6)
                )
            )
            screenshot(dialog, "03-restarted-history.png")
            report["steps"].append("restart-default-mode-offline-history")

            decision = QMessageBox.No
            dialog.action_buttons[1].click()
            require(not Inventory(db).list(store) and path.read_bytes() == before)
            if gift:
                require(not PendingGiftInventory(db).list(store))
            decision = QMessageBox.Yes
            dialog.action_buttons[1].click()
            require(
                len(Inventory(db).list(store)) == (0 if gift else 1)
                and len(calls) == query_count + 1
            )
            if gift:
                require(GiftShareOutputs(db).list_saved(store, account)[0].pending_id)
                require(len(PendingGiftInventory(db).list(store)) == 1)
                pending = PendingGiftDialog(window)
                pending.show()
                app.processEvents()
                require(pending.table.rowCount() == 1)
                require(
                    GIFT_CODE
                    not in " ".join(
                        pending.table.item(0, column).text() for column in range(5)
                    )
                )
                before_pending = path.read_bytes()
                pending.refresh_button.click()
                require(path.read_bytes() == before_pending)
                screenshot(pending, "05-pending-gifts.png")
            else:
                require(CouponShareOutputs(db).list_saved(store, account)[0].stock_id)
            dialog.table.setCurrentCell(0, 0)
            dialog.action_buttons[1].click()
            require(
                len(Inventory(db).list(store)) == (0 if gift else 1)
                and ("已登记" if gift else "已处理") in dialog.status.text()
            )
            report["steps"].append(
                "separate-quarantine-confirmation-no-consumable-stock-no-duplicate"
                if gift
                else "separate-stock-confirmation-decline-accept-and-no-duplicate"
            )

            before = path.read_bytes()
            dialog.action_buttons[2].click()
            require(path.read_bytes() == before)
            content = (output / "masked-history.csv").read_text(encoding="utf-8-sig")
            require(
                all(
                    secret not in content
                    for secret in (secret_output, TOKEN, "13900000000", item.code)
                )
            )
            screenshot(dialog, "04-stock-and-export.png")
            report["steps"].append("masked-export-button-no-network-no-db-write")

            window.store_combo.setCurrentIndex(window.store_combo.findData(other))
            app.processEvents()
            require(dialog.table.rowCount() == 0 and page.account.count() == 0)
            require(not Inventory(db).list(other) and len(calls) == query_count + 1)
            if gift:
                require(
                    pending.table.rowCount() == 0
                    and not pending.refresh_button.isEnabled()
                )
                require(not PendingGiftInventory(db).list(other))
                require(len(PendingGiftInventory(db).list(store)) == 1)
                report["gift_qualification_verified"] = False
                report["gift_consumable_stock"] = False
            require(all(session.closed for session in sessions) and not errors)
            report["steps"].append("store-change-invalidates-old-history")
            report["protocol_requests"] = {"query": query_count, "share": 1}
            report["ok"] = True
            dialog.close()
            page.saved_dialog = None
            dialog = None
            if pending is not None:
                pending.close()
                pending = None
            window.close()
            app.processEvents()
            window = db = None
    except Exception:
        report["error"] = (
            "GUI workflow acceptance failed; no sensitive diagnostic retained."
        )
    finally:
        if pending is not None:
            pending.close()
        if dialog is not None:
            dialog.close()
        if window is not None:
            if window.sync_worker is not None:
                window.sync_worker.requestInterruption()
                window.sync_worker.wait(5000)
                app.processEvents()
            window.close()
            app.processEvents()
        elif db is not None:
            db.close()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--kind", choices=("coupon", "gift"), default="coupon")
    options = parser.parse_args()
    if options.output.exists():
        parser.error("Use a new output directory; existing evidence is preserved.")
    report = run(options.output, offscreen=options.offscreen, kind=options.kind)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

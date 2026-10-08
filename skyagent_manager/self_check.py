"""Offline startup checks; never access real credentials or production data."""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from skyagent_manager import __version__
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.security import MAGIC
from skyagent_manager.share_journal import ShareJournal
from skyagent_manager.share_safety import require_share_writes_allowed


def run(report_path: Path | None = None) -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication

    from skyagent_manager.main_window import MainWindow

    report = {
        "ok": False,
        "platform": platform.system(),
        "version": __version__,
        "checks": [],
    }
    database = None
    window = None
    try:
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory(prefix="skyagent-self-check-") as temporary:
            path = Path(temporary) / "manager.sqlite3"
            key = AESGCM.generate_key(bit_length=256)
            database = StoreDatabase(path, key=key)
            store = database.add_store("自检门店")
            database.add_member(store, "自检会员", "13800000000")
            Accounts(database).add(
                store,
                AccountInput(
                    "13900000000", "synthetic-self-check-token", is_new_user=False
                ),
            )
            if not path.read_bytes().startswith(MAGIC):
                raise RuntimeError("encrypted persistence failed")
            if b"synthetic-self-check-token" in path.read_bytes():
                raise RuntimeError("account token exposed")
            backup = BackupService(database)
            from skyagent_manager.account_benefits import (
                AccountBenefitSession,
                SimulatedBenefitAdapter,
            )

            share_session = AccountBenefitSession(database, SimulatedBenefitAdapter())
            share_account = Accounts(database).list(store)[0]["id"]
            share_item = share_session.query(store, share_account)[0]
            operation = ShareJournal(database).reserve(
                store, share_account, share_session.owner, share_item
            )
            ShareJournal(database).finish(share_item, operation)
            from skyagent_manager.inventory import Inventory

            inventory = Inventory(database)
            inventory.import_values(store, "gift", ["synthetic-resource-code"])
            inventory.import_values(store, "invite", ["synthetic-invite-code"])
            resource = next(r for r in inventory.list(store) if r["kind"] == "gift")
            task = inventory.start(store, [resource["id"]])
            inventory.finish(store, task, "failed")
            from skyagent_manager.legacy_migration import apply_plan, preview_workspace

            legacy = Path(temporary) / "legacy-workspace"
            legacy.mkdir()
            (legacy / "params_register.json").write_text(
                json.dumps({"inventories": {"gift": ["synthetic-resource-code"]}}),
                encoding="utf-8",
            )
            migration_report, _ = apply_plan(
                database, store, preview_workspace(legacy), authorized=True
            )
            if migration_report.inserted != 0 or migration_report.skipped != 1:
                raise RuntimeError("legacy migration dedup failed")
            if b"synthetic-resource-code" in path.read_bytes():
                raise RuntimeError("stock value exposed")
            snapshot = backup.create(Path(temporary) / "check.skybackup")
            if backup.inspect(snapshot)[1]["members"] != 1:
                raise RuntimeError("backup validation failed")
            portable = backup.create_portable(
                Path(temporary) / "check.skyportable", "self-check-only-passphrase"
            )
            destination = StoreDatabase(
                Path(temporary) / "destination.sqlite3",
                key=AESGCM.generate_key(bit_length=256),
            )
            try:
                BackupService(destination).restore(
                    portable, "self-check-only-passphrase"
                )
                if len(destination.list_members(store)) != 1:
                    raise RuntimeError("portable restore failed")
                try:
                    require_share_writes_allowed(destination)
                except ValueError:
                    pass
                else:
                    raise RuntimeError("restore share write block missing")
                if len(Accounts(destination).list(store)) != 1:
                    raise RuntimeError("portable account restore failed")
                if len(Inventory(destination).list(store)) != 2:
                    raise RuntimeError("portable inventory restore failed")
                from skyagent_manager.legacy_migration import (
                    apply_workspace_batch,
                    preview_workspaces,
                )

                batch_root = Path(temporary) / "multi-workspace"
                for name in ("a", "b"):
                    workspace = batch_root / name
                    workspace.mkdir(parents=True)
                    (workspace / "params_register.json").write_text(
                        json.dumps(
                            {"inventories": {"gift": [f"synthetic-batch-{name}"]}}
                        ),
                        encoding="utf-8",
                    )
                second_store = destination.add_store("批量迁移自检目标")
                batch_report, _ = apply_workspace_batch(
                    destination,
                    preview_workspaces(batch_root),
                    [(0, store), (1, second_store)],
                    authorized=True,
                )
                if (
                    batch_report.inserted != 2
                    or len(Inventory(destination).list(second_store)) != 1
                ):
                    raise RuntimeError("multi-workspace migration failed")
                remote_accounts = Accounts(destination)
                remote_id = remote_accounts.save_backend_account(
                    second_store,
                    "13700000000",
                    "synthetic-backend-save-token",
                    authorized=True,
                )
                if (
                    remote_accounts.import_hold(
                        second_store, remote_accounts.get(second_store, remote_id)
                    )
                    != "backend-existing"
                ):
                    raise RuntimeError("backend existing account hold failed")
                try:
                    remote_accounts.sync_items(second_store, [remote_id])
                except ValueError:
                    pass
                else:
                    raise RuntimeError("backend existing account reupload allowed")
                from PySide6.QtCore import Qt

                from skyagent_manager.store_selector import StoreSelectorDialog

                selector_before = destination.path.read_bytes()
                selector = StoreSelectorDialog(destination)
                for index in range(selector.store_list.count()):
                    if (
                        selector.store_list.item(index).data(Qt.UserRole)
                        == second_store
                    ):
                        selector.store_list.setCurrentRow(index)
                        break
                selector.enter()
                if (
                    selector.selected_store_id != second_store
                    or destination.path.read_bytes() != selector_before
                ):
                    raise RuntimeError("startup store selection failed")
            finally:
                destination.close()
            database.close()
            database = StoreDatabase(path, key=key)
            if ShareJournal(database).state(share_item)["state"] != "unknown":
                raise RuntimeError("share journal restart protection failed")
            window = MainWindow(path, database=database)
            window.show()
            app.processEvents()
            if window.members_table.rowCount() != 1:
                raise RuntimeError("window data binding failed")
            if window.account_page.table.rowCount() != 1:
                raise RuntimeError("account window data binding failed")
            window.account_benefit_page.query()
            deadline = time.monotonic() + 5
            while window.sync_worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.001)
            if window.sync_worker is not None:
                window.sync_worker.requestInterruption()
                window.sync_worker.wait()
                app.processEvents()
                raise RuntimeError("simulated benefit worker timed out")
            if window.account_benefit_page.table.rowCount() != 5:
                raise RuntimeError("simulated account benefits failed")
            benefit = window.account_benefit_page.session.items[0]
            window.account_benefit_page.session.share(
                store,
                window.account_benefit_page.account.currentData(),
                (benefit.source, benefit.identifier),
            )
            window.inventory_page.kind.setCurrentIndex(
                window.inventory_page.kind.findData("gift")
            )
            if (
                window.inventory_page.table.rowCount() != 1
                or window.inventory_page.task_table.rowCount() != 1
            ):
                raise RuntimeError("inventory window data binding failed")
            page = window.account_benefit_page
            page.keyword.setText("早餐")
            if page.table.rowCount() != 1:
                raise RuntimeError("benefit keyword filtering failed")
            _, headers, rows = page.report_data(page.filtered_items())
            if len(rows) != 1 or "模拟" not in rows[0][0]:
                raise RuntimeError("masked benefit report failed")
            serialized = json.dumps([headers, rows], ensure_ascii=False)
            if any(
                value in serialized
                for value in (
                    "synthetic-self-check-token",
                    benefit.code,
                    benefit.identifier,
                )
            ):
                raise RuntimeError("benefit report exposed sensitive fields")
            page.clear_filters()
            if page.table.rowCount() != 5:
                raise RuntimeError("benefit filter reset failed")
            from skyagent_manager.resource_links import read_link_file

            links_path = Path(temporary) / "synthetic-links.txt"
            links_path.write_text(
                "说明 https://example.invalid/synthetic-link-a\tused\n"
                "https://example.invalid/synthetic-link-b\n"
                "https://example.invalid/synthetic-link-b\n"
                "https://example.invalid/synthetic-link-c\tUNKNOWN\n",
                encoding="utf-8-sig",
            )
            link_report = Inventory(database).import_rows(
                store, read_link_file(links_path, "breakfast")
            )
            if link_report.inserted != 2 or link_report.skipped != 2:
                raise RuntimeError("offline link import report failed")
            stock = [
                r for r in Inventory(database).list(store) if r["kind"] == "breakfast"
            ]
            if {r["state"] for r in stock} != {"used", "available"}:
                raise RuntimeError("offline link status preservation failed")
            if b"synthetic-link-a" in path.read_bytes():
                raise RuntimeError("offline link persistence exposed")
            from skyagent_manager.coupon_sharing import CouponShareOutputs

            coupon = next(
                item for item in page.session.items if item.kind == "delayed_checkout"
            )
            share_owner = page.session.owner
            operation = ShareJournal(database).reserve(
                store, share_owner[1], share_owner, coupon
            )
            output_url = (
                "https://mobile.yaduo.com/share?code=synthetic-self-check-output"
            )
            outputs = CouponShareOutputs(database)
            outputs.save(
                store, share_owner[1], share_owner, coupon, operation, output_url
            )
            stock_id = outputs.import_stock(store, share_owner[1], share_owner, coupon)
            if outputs.load(store, share_owner[1], coupon).stock_id != stock_id:
                raise RuntimeError("coupon share stock state failed")
            if output_url.encode() in path.read_bytes():
                raise RuntimeError("coupon share output exposed")
            from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog

            history_before = path.read_bytes()
            saved_rows = outputs.list_saved(store, share_owner[1])
            if len(saved_rows) != 1 or saved_rows[0].stock_id != stock_id:
                raise RuntimeError("saved share history failed")
            dialog = SavedCouponSharesDialog(page)
            if dialog.table.rowCount() != 1:
                raise RuntimeError("saved share history dialog failed")
            dialog.close()
            with TemporaryDirectory(
                prefix="skyagent-share-export-check-"
            ) as export_directory:
                masked = Path(export_directory) / "masked.csv"
                full = Path(export_directory) / "full.csv"
                outputs.export_to(
                    masked, store, share_owner[1], share_owner, saved_rows
                )
                outputs.export_to(
                    full, store, share_owner[1], share_owner, saved_rows, full=True
                )
                if output_url in masked.read_text(
                    encoding="utf-8-sig"
                ) or output_url not in full.read_text(encoding="utf-8-sig"):
                    raise RuntimeError("saved share export privacy failed")
            if path.read_bytes() != history_before:
                raise RuntimeError("saved share reads or exports changed database")
            from dataclasses import replace

            prop = replace(
                coupon,
                identifier="synthetic-self-check-prop",
                code="synthetic-self-check-prop-code",
                source="prop",
                kind="prop",
                dis_type="7",
                coupons_type="3",
            )
            journal = ShareJournal(database)
            prop_operation = journal.reserve(store, share_owner[1], share_owner, prop)
            prop_output_url = (
                "https://mobile.yaduo.com/share?code=synthetic-self-check-prop-output"
            )
            outputs.save(
                store,
                share_owner[1],
                share_owner,
                prop,
                prop_operation,
                prop_output_url,
            )
            changed_prop = replace(prop, identifier="changed-prop", code="changed-code")
            if (
                journal.state_for_owner(store, share_owner[1], changed_prop)["state"]
                != "confirmed"
            ):
                raise RuntimeError("prop context guard failed")
            prop_saved = outputs.load_operation(store, share_owner[1], prop_operation)
            if prop_saved.kind != "prop" or len(prop_saved.resource_keys) != 4:
                raise RuntimeError("prop output association failed")
            prop_stock_id = outputs.import_operation(
                store, share_owner[1], share_owner, prop_operation
            )
            prop_saved = outputs.load_operation(store, share_owner[1], prop_operation)
            prop_stock = [
                row
                for row in Inventory(database).list(store)
                if row["id"] == prop_stock_id
            ]
            if len(prop_stock) != 1 or prop_stock[0]["kind"] != "prop":
                raise RuntimeError("prop output inventory kind failed")
            with TemporaryDirectory(
                prefix="skyagent-prop-export-check-"
            ) as prop_directory:
                outputs.export_to(
                    Path(prop_directory) / "prop.csv",
                    store,
                    share_owner[1],
                    share_owner,
                    [prop_saved],
                    full=True,
                )
            if prop_output_url.encode() in path.read_bytes():
                raise RuntimeError("prop output exposed")
            from skyagent_manager.gift_sharing import GiftShareOutputs

            gift = replace(
                coupon,
                source="order",
                kind="gift",
                identifier="synthetic-folio:synthetic-chain",
                code="synthetic-folio",
                expiry="",
                status="unknown",
                folio_id="synthetic-folio",
                chain_id="synthetic-chain",
            )
            gift_operation = journal.reserve(store, share_owner[1], share_owner, gift)
            gift_outputs = GiftShareOutputs(database)
            gift_code = "synthetic-frozen-gift-code"
            gift_outputs.save(
                store, share_owner[1], share_owner, gift, gift_operation, gift_code
            )
            gift_rows = gift_outputs.list_saved(store, share_owner[1])
            if (
                len(gift_rows) != 1
                or gift_rows[0].share_code != gift_code
                or gift_code in repr(gift_rows)
            ):
                raise RuntimeError("gift output association or privacy failed")
            history = SavedCouponSharesDialog(page, gift=True)
            if history.table.rowCount() != 1 or len(history.action_buttons) != 4:
                raise RuntimeError("gift saved history window failed")
            history.close()
            from skyagent_manager.gift_sharing import PendingGiftInventory
            from skyagent_manager.pending_gift_dialog import PendingGiftDialog

            pending_id = PendingGiftInventory(database).register(
                store,
                share_owner[1],
                share_owner,
                gift_operation,
                expected=gift_rows[0],
            )
            if (
                GiftShareOutputs(database)
                .load_operation(store, share_owner[1], gift_operation)
                .pending_id
                != pending_id
                or len(PendingGiftInventory(database).list(store)) != 1
            ):
                raise RuntimeError("pending gift association failed")
            if database.connection.execute(
                "SELECT 1 FROM stock WHERE id=?", (pending_id,)
            ).fetchone():
                raise RuntimeError("pending gift incorrectly entered stock")
            pending_dialog = PendingGiftDialog(window)
            if pending_dialog.table.rowCount() != 1:
                raise RuntimeError("pending gift dialog failed")
            pending_dialog.close()
            gift_rows = gift_outputs.list_saved(store, share_owner[1])
            gift_before = path.read_bytes()
            with TemporaryDirectory(
                prefix="skyagent-gift-export-check-"
            ) as gift_directory:
                masked = Path(gift_directory) / "masked.csv"
                full = Path(gift_directory) / "full.csv"
                gift_outputs.export_to(
                    masked, store, share_owner[1], share_owner, gift_rows
                )
                gift_outputs.export_to(
                    full, store, share_owner[1], share_owner, gift_rows, full=True
                )
                if gift_code in masked.read_text(
                    encoding="utf-8-sig"
                ) or gift_code not in full.read_text(encoding="utf-8-sig"):
                    raise RuntimeError("gift export privacy failed")
            if (
                path.read_bytes() != gift_before
                or gift_code.encode() in path.read_bytes()
            ):
                raise RuntimeError("gift export changed database or exposed code")
            window.close()
            window = None
            database = None
            app.processEvents()
            report["checks"] = [
                "encryption",
                "restart",
                "backup",
                "portable-restore",
                "share-journal-durable",
                "restore-share-write-block",
                "synthetic-coupon-output-stock",
                "saved-share-history-offline",
                "saved-share-export-privacy",
                "synthetic-prop-output-context-guard",
                "synthetic-prop-output-stock",
                "synthetic-order-gift-output",
                "gift-history-export-privacy",
                "pending-gift-register-stock-isolation",
                "qt-window",
                "accounts-v4",
                "inventory-v6",
                "legacy-json-migration",
                "multi-workspace-json-migration",
                "backend-existing-account-save-hold",
                "startup-store-selection",
                "simulated-account-benefits",
                "benefit-filter-masked-report",
                "offline-link-file-import",
            ]
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
        elif sys.platform.startswith("win"):
            from keyring.backends.Windows import WinVaultKeyring as Keyring
        else:
            raise RuntimeError("unsupported native credentials platform")
        Keyring()  # Import/construct only; no keychain reads or writes.
        report["checks"].append("native-credentials-import")
        report["ok"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        if window is not None:
            window.close()
        elif database is not None:
            database.close()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path is not None:
        report_path.write_text(output + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(output)
    return 0 if report["ok"] else 1

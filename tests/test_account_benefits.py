import csv
import os
import time
from dataclasses import replace
from datetime import date
from threading import Event, get_ident

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from skyagent_manager.account_benefits import (
    AccountBenefitSession,
    BenefitPage,
    SimulatedBenefitAdapter,
)
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.main_window import MainWindow


def wait_for_query(app, window):
    deadline = time.monotonic() + 3
    while window.sync_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert window.sync_worker is None


@pytest.fixture
def context(tmp_path):
    db = StoreDatabase(tmp_path / "benefits.db", key=b"b" * 32)
    sid = db.add_store("A")
    aid = Accounts(db).add(sid, AccountInput("13812345678", "synthetic-benefit-token"))
    yield db, sid, aid
    db.close()


def test_query_mapping_share_rules_and_no_stock(context):
    db, sid, aid = context
    session = AccountBenefitSession(db, SimulatedBenefitAdapter())
    items = session.query(sid, aid)
    assert len(items) == 5
    assert "synthetic-benefit-token" not in repr(items)
    assert {item.kind for item in items} == {
        "breakfast",
        "room_upgrade",
        "delayed_checkout",
        "prop",
        "gift",
    }
    item = items[0]
    key = (item.source, item.identifier)
    assert session.share(sid, aid, key) == session.share(sid, aid, key)
    assert not Inventory(db).list(sid) and not db.list_benefits(sid)
    assert len([r for r in db.list_activity(sid) if r["action"] == "模拟权益分享"]) == 1
    assert not replace(item, expiry="unknown").can_share()
    assert not replace(item, expiry="2000-01-01").can_share()
    assert not replace(item, count=0).can_share()
    blocked = next(item for item in items if not item.shareable)
    with pytest.raises(ValueError):
        session.share(sid, aid, (blocked.source, blocked.identifier))


def test_token_change_and_store_isolation(context):
    db, sid, aid = context
    session = AccountBenefitSession(db, SimulatedBenefitAdapter())
    item = session.query(sid, aid)[0]
    other = db.add_store("B")
    with pytest.raises(ValueError):
        session.share(other, aid, (item.source, item.identifier))
    Accounts(db).update(
        sid, aid, AccountInput("13812345678", "changed-synthetic-token")
    )
    with pytest.raises(ValueError, match="变化"):
        session.share(sid, aid, (item.source, item.identifier))
    assert not session.items


@pytest.mark.parametrize("mode", ["cycle", "conflict", "exception", "invalid"])
def test_invalid_response_and_partial_failure_clear_results(context, mode):
    db, sid, aid = context
    original = SimulatedBenefitAdapter().fetch("unused", None).items[0]

    class Adapter:
        def fetch(self, token, cursor):
            if cursor is None:
                return BenefitPage((original,), "next")
            if mode == "exception":
                raise RuntimeError(token)
            if mode == "cycle":
                return BenefitPage((original,), "next")
            if mode == "conflict":
                return BenefitPage((replace(original, count=2),))
            return BenefitPage((replace(original, count=True),))

    session = AccountBenefitSession(db, Adapter())
    with pytest.raises(ValueError) as error:
        session.query(sid, aid)
    assert "synthetic-benefit-token" not in str(error.value)
    assert not session.items and session.owner is None


def test_dedup_and_at_filter(context):
    db, sid, aid = context
    item = SimulatedBenefitAdapter().fetch("unused", None).items[0]

    class Adapter:
        def fetch(self, token, cursor):
            return BenefitPage(
                (item, item, replace(item, identifier="excluded", name="A.T. coupon"))
            )

    assert len(AccountBenefitSession(db, Adapter()).query(sid, aid)) == 1


def test_share_error_is_redacted(context):
    db, sid, aid = context

    class Adapter(SimulatedBenefitAdapter):
        def share(self, token, benefit, request_id):
            raise RuntimeError(token)

    session = AccountBenefitSession(db, Adapter())
    item = session.query(sid, aid)[0]
    with pytest.raises(ValueError) as error:
        session.share(sid, aid, (item.source, item.identifier))
    assert "synthetic-benefit-token" not in str(error.value)
    assert not session.shares


@pytest.mark.parametrize("status", ["used", "expired", "unknown"])
def test_non_available_status_blocks_share(status):
    item = SimulatedBenefitAdapter().fetch("unused", None).items[0]
    assert not replace(item, status=status).can_share()
    with pytest.raises(ValueError):
        replace(item, status="invalid").validated()


@pytest.mark.parametrize(
    "status,expiry,expected",
    [
        ("available", "2026-10-06", "available"),
        ("available", "2026-10-05", "expired"),
        ("available", "unknown", "unknown"),
        ("used", "2099-01-01", "used"),
        ("expired", "2099-01-01", "expired"),
        ("unknown", "2099-01-01", "unknown"),
    ],
)
def test_effective_status_conservative(status, expiry, expected):
    item = SimulatedBenefitAdapter().fetch("unused", None).items[0]
    item = replace(item, status=status, expiry=expiry)
    assert item.effective_status(date(2026, 10, 6)) == expected
    assert item.can_share(date(2026, 10, 6)) == (expected == "available")


def test_status_validation_rejects_non_string():
    item = SimulatedBenefitAdapter().fetch("unused", None).items[0]
    with pytest.raises(ValueError):
        replace(item, status=[]).validated()


def test_query_only_adapter_never_shares_or_writes(context):
    db, sid, aid = context

    class ReadOnly:
        def fetch(self, token, cursor):
            return SimulatedBenefitAdapter().fetch(token, cursor)

    session = AccountBenefitSession(db, ReadOnly())
    item = session.query(sid, aid)[0]
    before = db.list_activity(sid)
    with pytest.raises(ValueError, match="只读"):
        session.share(sid, aid, (item.source, item.identifier))
    assert db.list_activity(sid) == before
    assert not session.shares and not Inventory(db).list(sid)


@pytest.mark.parametrize("after_fetch", [False, True])
def test_query_cancel_discards_results(context, after_fetch):
    db, sid, aid = context
    calls = []

    class Adapter(SimulatedBenefitAdapter):
        def fetch(self, token, cursor):
            calls.append(cursor)
            return super().fetch(token, cursor)

    session = AccountBenefitSession(db, Adapter())
    with pytest.raises(ValueError):
        session.query(sid, aid, cancelled=lambda: bool(calls) or not after_fetch)
    assert len(calls) == int(after_fetch)
    assert session.owner is None and not session.items


def test_account_changed_during_query_discards_results(context):
    db, sid, aid = context

    class Adapter(SimulatedBenefitAdapter):
        def fetch(self, token, cursor):
            Accounts(db).update(
                sid, aid, AccountInput("13812345678", "replacement-synthetic-token")
            )
            return super().fetch(token, cursor)

    session = AccountBenefitSession(db, Adapter())
    with pytest.raises(ValueError):
        session.query(sid, aid)
    assert not session.items and session.owner is None


def test_ui_query_filter_share_and_account_invalidation(tmp_path, monkeypatch):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "ui.db", key=b"u" * 32)
    sid = db.add_store("A")
    aid = Accounts(db).add(
        sid, AccountInput("13812345678", "synthetic-ui-benefit-token")
    )
    db.add_store("B")
    window = MainWindow(db.path, database=db)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    try:
        page = window.account_benefit_page
        page.query()
        wait_for_query(app, window)
        assert page.table.rowCount() == 5
        page.shareable.setChecked(True)
        assert page.table.rowCount() == 4
        page.table.setCurrentCell(0, 0)
        page.share()
        assert "已生成模拟分享" in page.result.text()
        assert not errors and not Inventory(db).list(sid)
        Accounts(db).update(
            sid, aid, AccountInput("13812345678", "updated-ui-benefit-token")
        )
        window._refresh_all()
        assert page.table.rowCount() == 0
        page.query()
        wait_for_query(app, window)
        window.store_combo.setCurrentIndex(1)
        assert page.table.rowCount() == 0 and not page.session.shares
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("action", ["cancel", "change", "close", "error"])
def test_background_query_lifecycle(tmp_path, monkeypatch, action):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "worker.db", key=b"w" * 32)
    sid = db.add_store("A")
    aid = Accounts(db).add(sid, AccountInput("13812345678", "synthetic-worker-token"))
    window = MainWindow(db.path, database=db)
    monkeypatch.setattr(window, "_error", lambda message: pytest.fail(message))
    entered, release = Event(), Event()
    threads = []

    class Adapter(SimulatedBenefitAdapter):
        def fetch(self, token, cursor):
            threads.append(get_ident())
            entered.set()
            assert release.wait(3)
            if action == "error":
                raise RuntimeError(token)
            return super().fetch(token, cursor)

    page = window.account_benefit_page
    page.session.adapter = Adapter()
    try:
        page.query()
        worker = page.query_worker
        assert entered.wait(2)
        assert threads[0] != get_ident()
        assert not window.store_combo.isEnabled()
        assert window.cancel_sync.isEnabled()
        if action == "cancel":
            window._cancel_sync()
        elif action == "change":
            Accounts(db).update(
                sid, aid, AccountInput("13812345678", "changed-synthetic-token")
            )
        elif action == "close":
            window.close()
            assert window.close_pending and window.sync_worker is worker
        release.set()
        wait_for_query(app, window)
        assert worker.token is None
        assert not page.session.items and page.table.rowCount() == 0
        assert "synthetic-worker-token" not in page.result.text()
        if action != "close":
            assert window.store_combo.isEnabled()
            assert not window.cancel_sync.isEnabled()
    finally:
        release.set()
        wait_for_query(app, window)
        window.close()
        app.processEvents()


def test_ui_keyword_state_filters_keep_identity_and_masking(tmp_path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "filters.db", key=b"f" * 32)
    sid = db.add_store("A")
    Accounts(db).add(sid, AccountInput("13812345678", "synthetic-filter-token"))
    window = MainWindow(db.path, database=db)
    page = window.account_benefit_page

    class Adapter:
        def fetch(self, token, cursor):
            original = SimulatedBenefitAdapter().fetch(token, None).items[0]
            return BenefitPage(
                tuple(
                    replace(
                        original,
                        identifier=f"item-{i}",
                        name=f"Name {i}",
                        status=status,
                    )
                    for i, status in enumerate(("available", "used", "unknown"))
                )
            )

    page.session.adapter = Adapter()
    try:
        page.query()
        wait_for_query(app, window)
        snapshot = page.session.items
        assert page.table.rowCount() == 3
        page.keyword.setText("  NAME 1  ")
        assert page.table.rowCount() == 1
        assert page.table.item(0, 0).data(Qt.UserRole) == ("coupon", "item-1")
        assert page.table.item(0, 7).text() == "已用"
        assert page.table.item(0, 5).text() == "否"
        page.shareable.setChecked(True)
        assert page.table.rowCount() == 0
        page.clear_filters()
        page.state.setCurrentIndex(page.state.findData("unknown"))
        assert page.table.rowCount() == 1
        page.clear_filters()
        page.keyword.setText("COUPON")
        assert page.table.rowCount() == 3
        page.keyword.setText(snapshot[0].code)
        assert page.table.rowCount() == 0
        page.clear_filters()
        assert page.table.rowCount() == 3
        assert page.summary.text() == "显示 3 / 3 条（模拟结果）"
        assert page.session.items == snapshot
        assert not Inventory(db).list(sid) and not page.session.shares
        assert snapshot[0].code != page.table.item(0, 2).text()
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("mode", ["filtered", "empty", "changed", "busy"])
def test_masked_report_has_no_external_side_effects(tmp_path, monkeypatch, mode):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "report.db", key=b"r" * 32)
    sid = db.add_store("A")
    aid = Accounts(db).add(sid, AccountInput("13812345678", "synthetic-report-token"))
    window = MainWindow(db.path, database=db)
    exports, errors = [], []
    monkeypatch.setattr(window, "_export_csv", lambda *args: exports.append(args))
    monkeypatch.setattr(window, "_error", errors.append)
    page = window.account_benefit_page
    try:
        page.query()
        wait_for_query(app, window)
        snapshot = page.session.items
        before = db.list_activity(sid)
        if mode == "filtered":
            page.keyword.setText("早餐")
        elif mode == "empty":
            page.keyword.setText("不存在的权益")
        elif mode == "changed":
            Accounts(db).update(
                sid, aid, AccountInput("13812345678", "changed-report-token")
            )
            before = db.list_activity(sid)
        else:
            window.sync_worker = object()
        page.export_report()
        window.sync_worker = None
        if mode == "filtered":
            assert len(exports) == 1
            title, headers, rows = exports[0]
            assert len(rows) == 1 and rows[0][1] == "早餐券"
            assert "无分享链接" in title
            assert "模拟" in rows[0][0]
            assert "券码（遮蔽）" in headers
            serialized = repr(exports)
            for value in (
                "synthetic-report-token",
                "13812345678",
                snapshot[0].code,
                snapshot[0].identifier,
            ):
                assert value not in serialized
            assert not errors
        else:
            assert not exports
            assert bool(errors) == (mode != "busy")
        assert db.list_activity(sid) == before
        assert not Inventory(db).list(sid) and not page.session.shares
    finally:
        window.sync_worker = None
        window.close()
        app.processEvents()


def test_report_csv_is_masked_formula_safe_and_audited(tmp_path, monkeypatch):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "csv.db", key=b"c" * 32)
    sid = db.add_store("A")
    Accounts(db).add(sid, AccountInput("13812345678", "synthetic-csv-token"))
    window = MainWindow(db.path, database=db)
    page = window.account_benefit_page
    path = tmp_path / "report.csv"
    monkeypatch.setattr(
        "skyagent_manager.main_window.ExportPreviewDialog.exec",
        lambda self: QDialog.Accepted,
    )
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    class ReadOnly:
        def fetch(self, token, cursor):
            item = SimulatedBenefitAdapter().fetch(token, None).items[0]
            return BenefitPage((replace(item, name="=1+1"),))

    page.session.adapter = ReadOnly()
    try:
        page.query()
        wait_for_query(app, window)
        page.export_report()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        assert len(rows) == 2 and rows[1][2] == "'=1+1"
        assert "模拟" in rows[1][0]
        assert page.session.items[0].code not in path.read_text(encoding="utf-8-sig")
        assert not Inventory(db).list(sid) and not page.session.shares
        assert any("导出模拟权益" in row["action"] for row in db.list_activity(sid))
    finally:
        window.close()
        app.processEvents()

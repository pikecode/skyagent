import pytest

from skyagent_manager.account_task_plan import (
    OperationProgress,
    available_stock_counts,
    batch_report_data,
    plan_account_batch,
    plan_account_task,
)


def test_all_tasks_token_branch_and_default_targets():
    plan = plan_account_task(has_token=True)
    assert plan.route == "claim" and not plan.executable
    assert len(plan.operations) == 7
    assert plan.operations[0].remaining == 0
    assert "跳过注册" in plan.operations[0].state
    assert set(vars(plan)) == {"route", "operations", "executable"}


def test_no_token_registration_branch():
    plan = plan_account_task(has_token=False)
    assert plan.route == "register" and not plan.executable
    assert all(item.remaining == 1 for item in plan.operations)


def test_quantity_delta_completed_and_unknown():
    plan = plan_account_task(
        has_token=True,
        breakfast_count=2,
        delay_count=2,
        progress={
            "breakfast": OperationProgress(1),
            "delay": OperationProgress(2),
            "gift": OperationProgress(1, True),
        },
    )
    rows = {item.operation: item for item in plan.operations}
    assert rows["breakfast"].remaining == 1
    assert rows["delay"].remaining == 0 and "跳过" in rows["delay"].state
    assert "禁止重放" in rows["gift"].state


@pytest.mark.parametrize(
    "task", ["reward", "silver", "breakfast", "upgrade", "delay", "gift"]
)
def test_missing_token_blocks_claim(task):
    plan = plan_account_task(has_token=False, task=task)
    assert len(plan.operations) == 1 and "阻断" in plan.operations[0].state


@pytest.mark.parametrize(
    "arguments",
    [
        {"has_token": 1},
        {"has_token": True, "task": "invalid"},
        {"has_token": True, "breakfast_count": True},
        {"has_token": True, "delay_count": 3},
        {"has_token": True, "progress": {"gift": OperationProgress(-1)}},
        {"has_token": True, "progress": {"gift": OperationProgress(0, 1)}},
        {"has_token": True, "progress": {"invalid": OperationProgress()}},
    ],
)
def test_invalid_plan_inputs(arguments):
    with pytest.raises(ValueError):
        plan_account_task(**arguments)


@pytest.mark.parametrize("history", [OperationProgress(0, True), OperationProgress(1)])
def test_registration_unresolved_blocks_dependent_operations(history):
    plan = plan_account_task(has_token=False, progress={"register": history})
    assert all("阻断" in item.state for item in plan.operations[1:])
    assert not plan.executable


def test_resource_shortage_only_counts_remaining():
    plan = plan_account_task(
        has_token=True,
        breakfast_count=2,
        stock={"breakfast": 1},
        progress={"breakfast": OperationProgress(1)},
    )
    rows = {item.operation: item for item in plan.operations}
    assert rows["breakfast"].shortage == 0 and rows["breakfast"].remaining == 1
    assert rows["gift"].shortage == 1 and "不足" in rows["gift"].state
    assert rows["reward"].available is None
    assert not plan.executable


@pytest.mark.parametrize(
    "stock", [{"breakfast": True}, {"gift": -1}, {"invite": 1}, [], {"gift": "1"}]
)
def test_invalid_stock_counts(stock):
    with pytest.raises(ValueError):
        plan_account_task(has_token=True, stock=stock)


def test_available_counts_exclude_other_store_used_and_reserved(tmp_path):
    from skyagent_manager.db import StoreDatabase
    from skyagent_manager.inventory import Inventory

    db = StoreDatabase(tmp_path / "stock.db", key=b"s" * 32)
    try:
        sid, other = db.add_store("A"), db.add_store("B")
        inventory = Inventory(db)
        inventory.import_rows(
            sid,
            [
                {"kind": "breakfast", "value": "first"},
                {"kind": "breakfast", "value": "second"},
                {"kind": "breakfast", "value": "used", "state": "used"},
                {"kind": "invite", "value": "invite"},
            ],
        )
        inventory.import_values(other, "breakfast", ["other-store"])
        rid = next(row["id"] for row in inventory.list(sid) if row["value"] == "second")
        inventory.start(sid, [rid])
        before = db.list_activity(sid)
        assert available_stock_counts(db, sid) == {"breakfast": 1}
        assert available_stock_counts(db, other) == {"breakfast": 1}
        assert db.list_activity(sid) == before
    finally:
        db.close()


def test_batch_demand_does_not_reuse_same_stock_for_every_account():
    batch = plan_account_batch(
        token_flags=[True, True, True],
        stock={"breakfast": 2},
        task="breakfast",
        breakfast_count=2,
    )
    assert dict(batch.demand)["breakfast"] == 6
    assert dict(batch.shortages)["breakfast"] == 4
    assert len(batch.accounts) == 3 and not batch.executable


def test_batch_excludes_blocked_claims_and_includes_registration_dependencies():
    blocked = plan_account_batch(token_flags=[True, False], stock={}, task="gift")
    assert dict(blocked.demand)["gift"] == 1
    registration = plan_account_batch(token_flags=[True, False], stock={}, task="all")
    assert dict(registration.demand)["gift"] == 2


@pytest.mark.parametrize("flags", [[], [True] * 201, [1], "true"])
def test_batch_invalid_account_flags(flags):
    with pytest.raises(ValueError):
        plan_account_batch(token_flags=flags, stock={})


def test_batch_requires_stock_and_keeps_input_unchanged():
    with pytest.raises(ValueError):
        plan_account_batch(token_flags=[True], stock=None)
    stock = {"gift": 10}
    batch = plan_account_batch(token_flags=[True] * 2, stock=stock, task="gift")
    assert dict(batch.shortages)["gift"] == 0
    assert stock == {"gift": 10}


def test_checked_accounts_batch_preview_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog, QLabel

    from skyagent_manager.accounts import AccountInput, Accounts
    from skyagent_manager.db import StoreDatabase
    from skyagent_manager.inventory import Inventory
    from skyagent_manager.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "batch.db", key=b"b" * 32)
    sid = db.add_store("A")
    for phone in ("13812345678", "13912345678"):
        Accounts(db).add(sid, AccountInput(phone, "synthetic-batch-token-" + phone))
    Inventory(db).import_values(sid, "breakfast", ["synthetic-stock"])
    window = MainWindow(db.path, database=db)

    def inspect(dialog):
        labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "2 个账号" in labels and "早餐需求 2/可用 1/缺 1" in labels
        assert "首个账号" in labels
        assert "synthetic-batch-token" not in labels and "synthetic-stock" not in labels
        return QDialog.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect)
    try:
        before = db.list_activity(sid)
        for index in range(2):
            window.account_page.table.item(index, 0).setCheckState(Qt.Checked)
        window.account_page.preview_task()
        assert db.list_activity(sid) == before
        assert Inventory(db).list(sid)[0]["state"] == "available"
        assert window.sync_worker is None
    finally:
        window.close()
        app.processEvents()


def test_batch_report_anonymous_rows_and_resource_summary():
    stock = {"breakfast": 1}
    batch = plan_account_batch(
        token_flags=[True, True], stock=stock, task="breakfast", breakfast_count=2
    )
    title, headers, rows = batch_report_data(batch, stock)
    assert "未执行" in title and len(headers) == 10
    assert len(rows) == 7 and all(len(row) == len(headers) for row in rows)
    assert rows[0][1] == "账号序号 1" and rows[1][1] == "账号序号 2"
    summary = next(row for row in rows if row[1] == "整批资源快照" and row[3] == "早餐")
    assert summary[4] == "4" and summary[8:] == ["1", "3"]
    assert all("未执行" in row[0] for row in rows)


@pytest.mark.parametrize("mode", ["save", "cancel", "store_change"])
def test_plan_report_export_and_cancel_boundaries(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import csv

    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFileDialog,
        QMessageBox,
        QPushButton,
    )

    from skyagent_manager.accounts import AccountInput, Accounts
    from skyagent_manager.db import StoreDatabase
    from skyagent_manager.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "export.db", key=b"e" * 32)
    sid = db.add_store("A")
    other = db.add_store("B")
    Accounts(db).add(sid, AccountInput("13812345678", "synthetic-plan-report-token"))
    window = MainWindow(db.path, database=db)
    path = tmp_path / "plan.csv"
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args: ("" if mode == "cancel" else str(path), ""),
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    def inspect(dialog):
        if dialog.windowTitle() != "账号任务计划（不执行）":
            return QDialog.Accepted
        if mode == "store_change":
            window.store_combo.setCurrentIndex(window.store_combo.findData(other))
        button = next(
            button
            for button in dialog.findChildren(QPushButton)
            if "导出匿名" in button.text()
        )
        button.click()
        return QDialog.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect)
    try:
        window.account_page.table.setCurrentCell(0, 0)
        before = db.list_activity(sid)
        window.account_page.preview_task()
        if mode == "save":
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            assert len(rows) == 13 and all("未执行" in row[0] for row in rows[1:])
            text = path.read_text(encoding="utf-8-sig")
            assert (
                "13812345678" not in text and "synthetic-plan-report-token" not in text
            )
        else:
            assert not path.exists() and db.list_activity(sid) == before
        assert bool(errors) == (mode == "store_change")
        assert not db.connection.execute("SELECT * FROM local_tasks").fetchall()
        assert window.sync_worker is None
    finally:
        window.close()
        app.processEvents()


def test_account_preview_dialog_no_writes_or_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QTableWidget

    from skyagent_manager.accounts import AccountInput, Accounts
    from skyagent_manager.db import StoreDatabase
    from skyagent_manager.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "plan.db", key=b"p" * 32)
    sid = db.add_store("A")
    Accounts(db).add(sid, AccountInput("13812345678", "synthetic-preview-token"))
    window = MainWindow(db.path, database=db)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)

    def inspect(dialog):
        table = dialog.findChild(QTableWidget)
        assert table.rowCount() == 7
        assert "跳过注册" in table.item(0, 3).text()
        combos = dialog.findChildren(QComboBox)
        combos[0].setCurrentIndex(combos[0].findData("breakfast"))
        combos[1].setCurrentIndex(1)
        assert table.rowCount() == 1 and table.item(0, 1).text() == "2"
        assert table.item(0, 4).text() == "0" and table.item(0, 5).text() == "2"
        assert "不足" in table.item(0, 3).text()
        visible = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "历史结果尚未接入" in visible
        assert "synthetic-preview-token" not in visible and "13812345678" not in visible
        return QDialog.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect)
    try:
        before = db.list_activity(sid)
        window.account_page.table.setCurrentCell(0, 0)
        window.account_page.preview_task()
        assert not errors
        assert db.list_activity(sid) == before
        assert window.sync_worker is None
        assert not db.connection.execute("SELECT * FROM local_tasks").fetchall()
    finally:
        window.close()
        app.processEvents()

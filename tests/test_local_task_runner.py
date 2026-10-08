import os

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.local_task_runner import LocalTaskRunner
from skyagent_manager.main_window import MainWindow


@pytest.fixture
def context(tmp_path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "runner.db", key=b"r" * 32)
    sid = db.add_store("A")
    stock = Inventory(db)
    stock.import_values(sid, "gift", ["one", "two", "three"])
    task = stock.start(sid, [row["id"] for row in stock.list(sid)])
    yield app, db, sid, task
    db.close()


@pytest.mark.parametrize(
    "mode,state",
    [("success", "succeeded"), ("failure", "failed"), ("alternating", "failed")],
)
def test_runner_automatic_completion(context, mode, state):
    app, db, sid, task = context
    runner = LocalTaskRunner(db, sid, task, mode=mode)
    loop = QEventLoop()
    runner.finished.connect(loop.quit)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(loop.quit)
    watchdog.start(3000)
    runner.timer.setInterval(1)
    runner.start()
    loop.exec()
    assert runner.ended
    assert Inventory(db).tasks(sid)[0]["state"] == state
    assert not any(row["state"] == "reserved" for row in Inventory(db).list(sid))
    with pytest.raises(ValueError):
        runner.start()


def test_runner_cancel_keeps_success(context):
    app, db, sid, task = context
    runner = LocalTaskRunner(db, sid, task)
    runner.step()
    runner.requestInterruption()
    runner.step()
    assert runner.ended
    assert [row["state"] for row in Inventory(db).list(sid)] == [
        "used",
        "available",
        "available",
    ]
    assert Inventory(db).tasks(sid)[0]["state"] == "canceled"


def test_runner_persist_failure_stops_without_false_release(context, monkeypatch):
    app, db, sid, task = context
    runner = LocalTaskRunner(db, sid, task)
    errors = []
    runner.error.connect(errors.append)

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(db.connection, "persist", fail)
    runner.step()
    assert runner.ended and errors
    assert all(row["state"] == "reserved" for row in Inventory(db).list(sid))


def test_window_locks_mutations_and_close_cancels_then_closes(context, monkeypatch):
    app, db, sid, task = context
    window = MainWindow(db.path, database=db)
    monkeypatch.setattr(window, "_error", lambda *args: None)
    window._start_local_task(task, "success")
    runner = window.sync_worker
    assert not window.store_combo.isEnabled()
    assert all(not button.isEnabled() for button in window.action_buttons)
    assert not window.inventory_page.allowed()
    runner.step()
    window.show()
    window.close()
    assert window.close_pending and runner.interrupted
    runner.step()
    assert window.sync_worker is None
    app.processEvents()
    reopened = StoreDatabase(db.path, key=b"r" * 32)
    try:
        assert Inventory(reopened).tasks(sid)[0]["state"] == "canceled"
        assert [row["state"] for row in Inventory(reopened).list(sid)] == [
            "used",
            "available",
            "available",
        ]
    finally:
        reopened.close()

import pytest
from PySide6.QtWidgets import QMessageBox
from test_backend_query import Response, Session
from test_backend_query_ui import context as context
from test_backend_query_ui import login, wait

from skyagent_manager.backend_query import (
    BackendQueryClient,
    BackendQueryError,
    RemoteExchangeTask,
)


@pytest.mark.parametrize("mode", ["valid", "identity", "state", "progress", "missing"])
def test_exchange_task_contract(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    item = dict(
        id="task-secret-id",
        state="completed",
        progress=100,
        error="discard-secret",
        result={"phone": "discard-phone", "token": "discard-token"},
    )
    if mode == "identity":
        item["id"] = "other-task"
    elif mode == "state":
        item["state"] = []
    elif mode == "progress":
        item["progress"] = True
    elif mode == "missing":
        del item["progress"]
    session.response = Response({"item": item})
    if mode == "valid":
        result = client.exchange_task("task-secret-id", authorized=True)
        assert result.progress == 100 and result.state == "completed"
        assert all(
            value not in repr(result)
            for value in (
                "task-secret-id",
                "discard-secret",
                "discard-phone",
                "discard-token",
            )
        )
    else:
        with pytest.raises(BackendQueryError):
            client.exchange_task("task-secret-id", authorized=True)
    method, url, _options = session.calls[-1]
    assert method == "GET" and url.endswith(
        "/api/pool/coupon-exchange-tasks/task-secret-id"
    )


def test_exchange_task_authorization_and_id():
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    for identifier, authorized in [
        ("task1", False),
        ("../task1", True),
        (True, True),
        ("", True),
    ]:
        with pytest.raises(BackendQueryError):
            client.exchange_task(identifier, authorized=authorized)
    assert len(session.calls) == 1


def test_task_ui_confirmation_read_only(context, monkeypatch):
    _app, window, client, errors = context
    page = window.backend_query_page
    calls = []
    client.exchange_task = lambda identifier, **kwargs: (
        calls.append((identifier, kwargs))
        or RemoteExchangeTask(identifier, "completed", 100)
    )
    before = window.db.path.read_bytes()
    login(page)
    wait(window)
    page.exchange_task_id.setText("task1")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.start(False, exchange_task_id="task1")
    assert not calls
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    page.start(False, exchange_task_id="task1")
    assert not page.exchange_task_id.isEnabled()
    wait(window)
    assert calls == [("task1", {"authorized": True})]
    assert (
        "completed" in page.exchange_task_note.text()
        and "不证明" in page.exchange_task_note.text()
    )
    assert (
        page.exchange_task_id.isEnabled()
        and window.db.path.read_bytes() == before
        and not errors
    )
    page.exchange_task_id.setText("task2")
    assert "completed" not in page.exchange_task_note.text()
    page.clear()
    assert (
        not page.exchange_task_id.text() and "未查询" in page.exchange_task_note.text()
    )


@pytest.mark.parametrize("mode", ["target", "id", "cancel"])
def test_task_context_change_or_cancel_discards(context, monkeypatch, mode):
    _app, window, client, errors = context
    page = window.backend_query_page
    calls = []
    login(page)
    wait(window)
    page.exchange_task_id.setText("task1")

    def query(identifier, **kwargs):
        calls.append(identifier)
        if mode == "cancel":
            window.sync_worker.requestInterruption()
        return RemoteExchangeTask(identifier, "completed", 100)

    client.exchange_task = query

    def confirm(*args):
        if mode == "target":
            window.store_combo.setCurrentIndex(1)
        elif mode == "id":
            page.exchange_task_id.setText("task2")
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    page.start(False, exchange_task_id="task1")
    if mode == "cancel":
        wait(window)
        assert client.closed and page.client is None
    else:
        assert not calls and errors
    assert "completed" not in page.exchange_task_note.text()

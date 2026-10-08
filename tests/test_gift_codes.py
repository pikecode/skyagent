import pytest
from PySide6.QtWidgets import QMessageBox
from test_backend_query import Response, Session
from test_backend_query_ui import context as context
from test_backend_query_ui import login, wait

from skyagent_manager.backend_query import (
    BackendQueryClient,
    BackendQueryError,
    RemoteGiftCode,
    RemoteGiftPage,
)


@pytest.mark.parametrize(
    "mode", ["valid", "duplicate", "page", "count", "status", "source", "code"]
)
def test_gift_contract(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    row = dict(
        id="g1",
        shareCode="synthetic-secret-code",
        source="SCAN",
        status="ACTIVE",
        receivedCount=2,
        maxReceive=20,
        token="discard-token",
        accountPhone="discard-phone",
    )
    data = dict(items=[row], page=1, pageSize=20, total=30)
    if mode == "duplicate":
        data["items"].append(row)
    elif mode == "page":
        data["page"] = True
    elif mode == "count":
        row["receivedCount"] = False
    elif mode == "status":
        row["status"] = "UNKNOWN"
    elif mode == "source":
        row["source"] = []
    elif mode == "code":
        row["shareCode"] = "bad\ncode"
    session.response = Response(data)
    if mode != "valid":
        with pytest.raises(BackendQueryError):
            client.gift_codes(authorized=True)
    else:
        result = client.gift_codes(authorized=True)
        assert result.has_more and result.items[0].received == 2
        assert result.items[0].masked_code != row["shareCode"]
        assert all(
            value not in repr(result)
            for value in (row["shareCode"], "discard-token", "discard-phone")
        )
    method, url, options = session.calls[-1]
    assert method == "GET" and url.endswith("/api/gift-codes")
    assert options["params"] == {"page": 1, "pageSize": 20}


def test_gift_authorization_and_page_block_requests():
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    for page, authorized in [(1, False), (True, True), (0, True), (10001, True)]:
        with pytest.raises(BackendQueryError):
            client.gift_codes(page, authorized=authorized)
    assert len(session.calls) == 1


def test_gift_ui_global_scope_confirmation_no_persistence(context, monkeypatch):
    _app, window, client, errors = context
    page = window.backend_query_page
    calls = []
    client.gift_codes = lambda number, **kwargs: (
        calls.append((number, kwargs))
        or RemoteGiftPage(
            (RemoteGiftCode("syn••••code", "SCAN", "ACTIVE", 2, 20),), number, 1, False
        )
    )
    before = window.db.path.read_bytes()
    login(page)
    wait(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.start(False, gift_page=1)
    assert not calls and window.sync_worker is None
    prompts = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: prompts.append(args[2]) or QMessageBox.Yes,
    )
    page.start(False, gift_page=1)
    wait(window)
    assert calls == [(1, {"authorized": True})]
    assert "不按本地门店隔离" in prompts[0]
    assert page.gift_table.rowCount() == 1
    assert "不保证实时可领取" in page.gift_note.text()
    assert window.db.path.read_bytes() == before and not errors
    page.clear()
    assert page.gift_table.rowCount() == 0


def test_gift_cancel_discards(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    login(page)
    wait(window)

    def cancelled(*args, **kwargs):
        window.sync_worker.requestInterruption()
        return RemoteGiftPage((), 1, 0, False)

    client.gift_codes = cancelled
    page.start(False, gift_page=1)
    wait(window)
    assert page.client is None and client.closed
    assert page.gift_table.rowCount() == 0 and not errors


def test_gift_confirmation_rejects_changed_target(context, monkeypatch):
    _app, window, client, errors = context
    page = window.backend_query_page
    calls = []
    client.gift_codes = lambda *args, **kwargs: calls.append(args)
    login(page)
    wait(window)

    def change_target(*args):
        window.store_combo.setCurrentIndex(1)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", change_target)
    page.start(False, gift_page=1)
    assert not calls and window.sync_worker is None
    assert client.closed and errors

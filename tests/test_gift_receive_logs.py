import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from test_backend_query import Response, Session
from test_backend_query_ui import context as context
from test_backend_query_ui import login, wait

from skyagent_manager.backend_query import (
    BackendQueryClient,
    BackendQueryError,
    RemoteGiftCode,
    RemoteGiftPage,
    RemoteGiftReceiveLog,
    RemoteGiftReceiveLogs,
)


def payload():
    return {
        "items": [
            {
                "id": "log1",
                "codeId": "gift1",
                "accountPhone": "13812345678",
                "createdAt": "2026-10-07T00:00:00.000Z",
                "shareCode": "discard-gift-code",
                "content": "discard-content-token",
                "accountId": "discard-account-id",
            }
        ]
    }


@pytest.mark.parametrize("mode", ["valid", "empty", "unknown-fields"])
def test_gift_log_projection(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    data = payload()
    if mode == "empty":
        data["items"] = []
    elif mode == "unknown-fields":
        data["items"][0]["accountPhone"] = None
        data["items"][0]["createdAt"] = None
    session.response = Response(data)
    result = client.gift_receive_logs("gift1", authorized=True)
    assert len(result.items) == len(data["items"])
    if result.items:
        assert result.items[0].masked_phone != "13812345678"
    for secret in (
        "13812345678",
        "discard-gift-code",
        "discard-content-token",
        "discard-account-id",
    ):
        assert secret not in repr(result)
    method, url, options = session.calls[-1]
    assert method == "GET" and url.endswith("/api/gift-codes/gift1/logs")
    assert (
        options["verify"] and not options["allow_redirects"] and "params" not in options
    )


@pytest.mark.parametrize(
    "mode",
    [
        "wrong-code",
        "duplicate",
        "invalid-id",
        "bad-phone",
        "bad-time",
        "naive-time",
        "control-time",
        "oversized",
        "missing-list",
        "not-row",
    ],
)
def test_gift_log_invalid_whole_result_rejected(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    data = payload()
    row = data["items"][0]
    if mode == "wrong-code":
        row["codeId"] = "other"
    elif mode == "duplicate":
        data["items"].append(row)
    elif mode == "invalid-id":
        row["id"] = "../other"
    elif mode == "bad-phone":
        row["accountPhone"] = "secret-token"
    elif mode == "bad-time":
        row["createdAt"] = "secret-time-token"
    elif mode == "naive-time":
        row["createdAt"] = "2026-10-07T00:00:00"
    elif mode == "control-time":
        row["createdAt"] = "2026-10-07\n00:00:00Z"
    elif mode == "oversized":
        data["items"] *= 501
    elif mode == "missing-list":
        data = {}
    elif mode == "not-row":
        data["items"] = [True]
    session.response = Response(data)
    with pytest.raises(BackendQueryError) as error:
        client.gift_receive_logs("gift1", authorized=True)
    assert "secret" not in str(error.value) and len(session.calls) == 2


@pytest.mark.parametrize("mode", ["decline", "invalid-id", "no-session", "403"])
def test_gift_log_guards_and_permission_no_fallback(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    if mode == "no-session":
        client._token = ""
    session.response = Response({"message": "discard-content-token"}, 403)
    with pytest.raises(BackendQueryError) as error:
        client.gift_receive_logs(
            "../gift" if mode == "invalid-id" else "gift1", authorized=mode != "decline"
        )
    assert "discard-content-token" not in str(error.value)
    assert len(session.calls) == (2 if mode == "403" else 1)


def setup_page(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    client.gift_codes = lambda number, **kwargs: RemoteGiftPage(
        (RemoteGiftCode("syn••••code", "IMPORT", "ACTIVE", 1, 20, "gift1"),),
        number,
        1,
        False,
    )
    calls = []
    client.gift_receive_logs = lambda identifier, **kwargs: (
        calls.append((identifier, kwargs))
        or RemoteGiftReceiveLogs(
            identifier, (RemoteGiftReceiveLog("138••••5678", "2026-10-07T00:00:00Z"),)
        )
    )
    login(page)
    wait(window)
    page.start(False, gift_page=1)
    wait(window)
    page.gift_table.selectRow(0)
    return window, client, errors, page, calls


def test_gui_log_selection_and_read_only(context):
    window, client, errors, page, calls = setup_page(context)
    before = window.db.path.read_bytes()
    assert page.gift_table.item(0, 0).data(Qt.UserRole) == "gift1"
    page.gift_logs()
    assert not page.gift_table.isEnabled()
    wait(window)
    assert calls == [("gift1", {"authorized": True})]
    assert page.gift_table.rowCount() == 1 and page.gift_log_table.rowCount() == 1
    assert page.gift_log_table.item(0, 0).text() == "138••••5678"
    assert "不是原任务历史" in page.gift_log_note.text()
    assert window.db.path.read_bytes() == before and not errors
    page.gift_table.clearSelection()
    assert page.gift_log_table.rowCount() == 0


@pytest.mark.parametrize("mode", ["decline", "selection", "store", "session"])
def test_gui_confirmation_changes_block_requests(context, monkeypatch, mode):
    window, client, errors, page, calls = setup_page(context)

    def confirm(*args):
        if mode == "selection":
            page.gift_table.clearSelection()
        elif mode == "store":
            window.store_combo.setCurrentIndex(1)
        elif mode == "session":
            page.clear()
        return QMessageBox.No if mode == "decline" else QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    page.gift_logs()
    assert not calls and window.sync_worker is None


@pytest.mark.parametrize("mode", ["cancel", "wrong-gift", "error"])
def test_gui_cancel_mismatch_and_error_no_results(context, mode):
    window, client, errors, page, calls = setup_page(context)
    before = window.db.path.read_bytes()

    def read(identifier, **kwargs):
        if mode == "cancel":
            window.sync_worker.requestInterruption()
        if mode == "error":
            raise ValueError("discard-sensitive-content")
        return RemoteGiftReceiveLogs(
            "other" if mode == "wrong-gift" else identifier,
            (RemoteGiftReceiveLog("138••••5678", "2026-10-07T00:00:00Z"),),
        )

    client.gift_receive_logs = read
    page.gift_logs()
    wait(window)
    assert page.gift_log_table.rowCount() == 0 and window.db.path.read_bytes() == before
    assert "discard-sensitive-content" not in str(errors)


def test_gui_clearing_session_clears_logs(context):
    window, client, errors, page, calls = setup_page(context)
    page.gift_logs()
    wait(window)
    page.clear()
    assert page.gift_log_table.rowCount() == 0 and client.closed


def test_gui_result_binding_change_discards(context):
    from types import SimpleNamespace

    from skyagent_manager.sync import validate_config

    window, client, errors, page, calls = setup_page(context)
    worker = SimpleNamespace(
        cancelled=False,
        isInterruptionRequested=lambda: False,
        gift_log_id="gift1",
        binding=page.binding,
    )
    window.sync_worker = worker
    try:
        window.db.configure_store(
            window.current_store,
            "changed",
            validate_config(
                "https://changed.example.invalid",
                "",
                "",
                False,
            ),
        )
        page._ready(
            (
                RemoteGiftReceiveLogs(
                    "gift1",
                    (RemoteGiftReceiveLog("138••••5678", "2026-10-07T00:00:00Z"),),
                ),
                client,
            )
        )
        assert (
            page.gift_log_table.rowCount() == 0
            and client.closed
            and page.client is None
        )
    finally:
        window.sync_worker = None

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


def data():
    return {
        "items": [
            {
                "id": "gift1",
                "shareCode": "synthetic-gift-code",
                "source": "IMPORT",
                "status": "ACTIVE",
                "receivedCount": 1,
                "maxReceive": 20,
            }
        ],
        "page": 1,
        "pageSize": 20,
        "total": 30,
    }


def test_filter_payload_and_safe_projection():
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    session.response = Response(data())
    result = client.gift_codes(
        authorized=True, keyword=" synthetic-filter ", status="ACTIVE", source="IMPORT"
    )
    assert result.items[0].identifier == "gift1" and result.has_more
    assert session.calls[-1][2]["params"] == {
        "page": 1,
        "pageSize": 20,
        "keyword": "synthetic-filter",
        "status": "ACTIVE",
        "source": "IMPORT",
    }
    assert "synthetic-gift-code" not in repr(result)


@pytest.mark.parametrize(
    "options",
    [
        {"keyword": None},
        {"keyword": "x" * 129},
        {"keyword": "secret\nvalue"},
        {"status": "UNKNOWN"},
        {"status": []},
        {"source": "UNKNOWN"},
        {"source": True},
    ],
)
def test_invalid_filters_no_request(options):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    with pytest.raises(BackendQueryError):
        client.gift_codes(authorized=True, **options)
    assert len(session.calls) == 1


@pytest.mark.parametrize("options", [{"status": "EXHAUSTED"}, {"source": "SCAN"}])
def test_filter_mismatch_rejects_whole_result(options):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    session.response = Response(data())
    with pytest.raises(BackendQueryError, match="筛选不一致"):
        client.gift_codes(authorized=True, **options)


def setup(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    calls = []
    client.gift_codes = lambda number, **kwargs: (
        calls.append((number, kwargs))
        or RemoteGiftPage(
            (RemoteGiftCode("syn••••code", "IMPORT", "ACTIVE", 1, 20, "gift1"),),
            number,
            30,
            number == 1,
        )
    )
    login(page)
    wait(window)
    return window, page, calls, errors


def test_gui_filters_and_manual_paging_no_writes(context):
    window, page, calls, errors = setup(context)
    page.gift_keyword.setText("synthetic-filter")
    page.gift_status.setCurrentIndex(page.gift_status.findData("ACTIVE"))
    page.gift_source.setCurrentIndex(page.gift_source.findData("IMPORT"))
    before = window.db.path.read_bytes()
    assert not calls
    page.start(False, gift_page=1)
    assert not page.gift_keyword.isEnabled() and not page.gift_page.isEnabled()
    wait(window)
    assert calls == [
        (
            1,
            {
                "authorized": True,
                "keyword": "synthetic-filter",
                "status": "ACTIVE",
                "source": "IMPORT",
            },
        )
    ]
    page.navigate_gifts(1)
    wait(window)
    assert [call[0] for call in calls] == [1, 2]
    page.navigate_gifts(-1)
    wait(window)
    assert [call[0] for call in calls] == [1, 2, 1]
    page.gift_keyword.setText("changed-filter")
    assert page.last_gift_page is None and page.gift_table.rowCount() == 0
    assert page.gift_page.value() == 1 and page.gift_log_table.rowCount() == 0
    assert window.db.path.read_bytes() == before and not errors


def test_gui_clear_filters_does_not_request(context):
    window, page, calls, errors = setup(context)
    page.gift_keyword.setText("test")
    page.gift_source.setCurrentIndex(1)
    page.clear_gift_filters()
    assert page.gift_filters() == ("", "", "") and not calls


def test_gui_filter_change_during_confirmation_blocked(context, monkeypatch):
    window, page, calls, errors = setup(context)

    def confirm(*args):
        page.gift_keyword.setText("changed-filter")
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    page.start(False, gift_page=1)
    assert not calls and window.sync_worker is None and errors


@pytest.mark.parametrize("mode", ["no-query", "first", "last", "decline"])
def test_navigation_limits_and_decline(context, monkeypatch, mode):
    window, page, calls, errors = setup(context)
    if mode != "no-query":
        page.start(False, gift_page=1)
        wait(window)
    if mode == "last":
        page.navigate_gifts(1)
        wait(window)
    calls.clear()
    if mode == "decline":
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.navigate_gifts(-1 if mode == "first" else 1)
    assert not calls and window.sync_worker is None


def test_result_after_filter_change_discarded(context):
    from types import SimpleNamespace

    window, page, calls, errors = setup(context)
    client = page.client
    window.sync_worker = SimpleNamespace(
        cancelled=False,
        isInterruptionRequested=lambda: False,
        binding=page.binding,
        gift_filters=("", "", ""),
    )
    try:
        page.gift_keyword.setText("changed-filter")
        page._ready((RemoteGiftPage((), 1, 0, False), client))
        assert page.gift_table.rowCount() == 0 and page.client is None and client.closed
        assert page.last_gift_page is None
    finally:
        window.sync_worker = None

import pytest
from test_backend_query import Response, Session
from test_backend_query_ui import context as context
from test_backend_query_ui import login, wait

from skyagent_manager.backend_query import (
    BackendQueryClient,
    BackendQueryError,
    RemoteOrder,
    RemoteOrderDetail,
    RemoteOrderPage,
)


@pytest.mark.parametrize(
    "mode", ["valid", "duplicate", "identity", "page", "count", "unknown"]
)
def test_real_order_contract_projection(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    row = {
        "folioId": 123456789123,
        "chainId": 123,
        "hotelName": "测试酒店",
        "start": "2026-10-10",
        "end": "2026-10-11",
        "orderStateName": "待入住",
        "customerName": "discard-person",
        "token": "discard-token",
    }
    data = {"page": {"pageNo": 1, "pageSize": 10, "totalCount": 20}, "result": [row]}
    if mode == "duplicate":
        data["result"].append(row)
    elif mode == "identity":
        row["folioId"] = True
    elif mode == "page":
        data["page"]["pageNo"] = 2
    elif mode == "count":
        data["page"]["totalCount"] = False
    elif mode == "unknown":
        del row["orderStateName"]
    session.response = Response(data)
    if mode in {"duplicate", "identity", "page", "count"}:
        with pytest.raises(BackendQueryError):
            client.account_orders("a1", authorized=True)
    else:
        result = client.account_orders("a1", authorized=True)
        assert result.has_more and result.items[0].hotel == "测试酒店"
        assert "discard-person" not in repr(result) and "discard-token" not in repr(
            result
        )
        assert "123456789123" not in repr(result)
    method, path, options = session.calls[-1]
    assert method == "GET" and path.endswith("/api/pool/accounts/a1/orders")
    assert options["params"] == {
        "pageNo": 1,
        "pageSize": 10,
        "state": "0",
        "queryState": "1",
    }


def test_no_authorization_or_invalid_page_never_requests():
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    for page, auth in [(1, False), (True, True), (0, True)]:
        with pytest.raises(BackendQueryError):
            client.account_orders("a1", page, authorized=auth)
    assert len(session.calls) == 1


def test_order_ui_confirmed_query_masked_no_persistence(context):
    _app, window, client, errors = context
    calls = []
    client.account_orders = lambda identifier, page, **kwargs: (
        calls.append((identifier, page, kwargs))
        or RemoteOrderPage(
            identifier,
            (
                RemoteOrder(
                    "123456789123",
                    "123",
                    "测试酒店",
                    "2026-10-10",
                    "2026-10-11",
                    "待入住",
                ),
            ),
            page,
            1,
            False,
        )
    )
    backend = window.backend_query_page
    before = window.db.path.read_bytes()
    login(backend)
    wait(window)
    backend.table.setCurrentCell(0, 0)
    backend.orders()
    wait(window)
    assert calls == [("backend-a", 1, {"authorized": True})]
    assert backend.order_table.rowCount() == 1
    assert backend.order_table.item(0, 0).text() != "123456789123"
    assert window.db.path.read_bytes() == before and not errors
    backend.search.setText("changed")
    assert backend.order_table.rowCount() == 0


def test_order_ui_confirmation_cancel_never_requests(context, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _app, window, client, errors = context
    backend = window.backend_query_page
    login(backend)
    wait(window)
    calls = []
    client.account_orders = lambda *args, **kwargs: calls.append(args)
    backend.table.setCurrentCell(0, 0)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    backend.orders()
    assert not calls and window.sync_worker is None and not errors


def test_order_worker_cancel_discards_and_clears_session(context):
    _app, window, client, errors = context
    backend = window.backend_query_page
    login(backend)
    wait(window)

    def orders(identifier, page, **kwargs):
        window.sync_worker.requestInterruption()
        return RemoteOrderPage(identifier, (), page, 0, False)

    client.account_orders = orders
    backend.table.setCurrentCell(0, 0)
    backend.orders()
    wait(window)
    assert backend.order_table.rowCount() == 0 and client.closed
    assert backend.client is None and not errors


@pytest.mark.parametrize("mode", ["valid", "identity", "state", "missing"])
def test_order_detail_identity_status_and_privacy(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    result = {
        "folioId": 123456789,
        "chainId": 123,
        "payState": 1,
        "orderState": 0,
        "orderStateName": "待入住",
        "customerName": "private-customer",
        "token": "private-token",
    }
    if mode == "identity":
        result["folioId"] = 999
    elif mode == "state":
        result["payState"] = True
    elif mode == "missing":
        del result["payState"]
    session.response = Response({"result": result})
    if mode in {"identity", "state"}:
        with pytest.raises(BackendQueryError):
            client.account_order_detail("a1", "123456789", "123", authorized=True)
    else:
        detail = client.account_order_detail("a1", "123456789", "123", authorized=True)
        assert detail.payment_state == (None if mode == "missing" else 1)
        assert "private-customer" not in repr(detail) and "private-token" not in repr(
            detail
        )
        assert "123456789" not in repr(detail)
    assert session.calls[-1][0] == "GET"
    assert session.calls[-1][1].endswith(
        "/api/pool/accounts/a1/orders/123456789/detail"
    )
    assert session.calls[-1][2]["params"] == {"chainId": "123"}


def test_detail_ui_binds_order_and_never_claims_gift_eligibility(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    calls = []
    client.account_orders = lambda identifier, number, **kwargs: RemoteOrderPage(
        identifier,
        (RemoteOrder("123456789", "123", "酒店", "", "", "待入住"),),
        number,
        1,
        False,
    )
    client.account_order_detail = lambda *args, **kwargs: (
        calls.append((args, kwargs))
        or RemoteOrderDetail(args[0], args[1], args[2], 1, 0, "待入住")
    )
    before = window.db.path.read_bytes()
    login(page)
    wait(window)
    page.table.setCurrentCell(0, 0)
    page.orders()
    wait(window)
    page.order_table.setCurrentCell(0, 0)
    page.order_detail()
    wait(window)
    assert calls == [(("backend-a", "123456789", "123"), {"authorized": True})]
    assert "资格仍未知" in page.order_detail_note.text()
    assert "支付状态码：1" in page.order_detail_note.text()
    assert window.db.path.read_bytes() == before and not errors
    page.clear()
    assert "未查询" in page.order_detail_note.text()

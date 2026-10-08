import pytest
import requests
from test_backend_query import Response
from test_backend_query_ui import context as context
from test_backend_query_ui import wait

from skyagent_manager.account_benefits import AccountBenefitSession
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.direct_benefits import DirectBenefitQueryAdapter


class Session:
    def __init__(self):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = []
        self.closed = False
        self.payloads = [
            {
                "couponList": [
                    {
                        "code": f"synthetic-code-{i}",
                        "expiryStr": "2099-12-31",
                        "share": 1,
                    }
                ]
            }
            for i in range(3)
        ] + [
            {
                "group": [
                    {
                        "availableCount": 2,
                        "propCardName": "synthetic-prop",
                        "memberCouponResponse": {
                            "code": "synthetic-prop-code",
                            "expiryStr": "2099/12/31",
                            "share": 1,
                        },
                    }
                ]
            }
        ]
        self.status = 200

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.payloads[len(self.calls) - 1]
        return Response(
            result if isinstance(result, bytes) else {"retcode": 0, "result": result},
            self.status,
        )

    def close(self):
        self.closed = True


TOKEN = "synthetic-direct-authorized-token"


def test_recovered_query_protocol_and_safe_snapshot():
    session = Session()
    adapter = DirectBenefitQueryAdapter(session_factory=lambda: session)
    items = AccountBenefitSession.fetch_items(adapter, TOKEN)
    assert len(items) == 4 and items[3].count == 2 and not items[3].shareable
    assert [item.kind for item in items] == [
        "breakfast",
        "room_upgrade",
        "delayed_checkout",
        "prop",
    ]
    assert TOKEN not in repr(items) and "synthetic-code-0" not in repr(items)
    assert all(
        call[1].startswith("https://miniapp.yaduo.com/atourlife/")
        for call in session.calls
    )
    assert [call[0] for call in session.calls] == ["POST", "POST", "POST", "GET"]
    assert session.calls[0][2]["json"]["stateCodes"] == ["AVAILABLE"]
    assert all(
        call[2]["verify"] and not call[2]["allow_redirects"] for call in session.calls
    )
    assert not session.trust_env and session.closed
    assert not callable(getattr(adapter, "share", None))


@pytest.mark.parametrize(
    "mode",
    [
        "http",
        "json",
        "duplicate",
        "oversize",
        "missing",
        "row",
        "prop-count",
        "bad-expiry",
    ],
)
def test_invalid_remote_response_redacted_and_closed(mode):
    session = Session()
    if mode == "http":
        session.status = 403
    elif mode == "json":
        session.payloads[0] = b"not-json-sensitive"
    elif mode == "duplicate":
        session.payloads[0] = b'{"retcode":0,"retcode":1,"result":{}}'
    elif mode == "oversize":
        session.payloads[0] = b"x" * (2 * 1024 * 1024 + 1)
    elif mode == "missing":
        session.payloads[0] = {}
    elif mode == "row":
        session.payloads[0]["couponList"] = [True]
    elif mode == "prop-count":
        session.payloads[3]["group"][0]["availableCount"] = True
    elif mode == "bad-expiry":
        session.payloads[0]["couponList"][0]["expiryStr"] = "unknown"
    adapter = DirectBenefitQueryAdapter(session_factory=lambda: session)
    if mode == "bad-expiry":
        items = AccountBenefitSession.fetch_items(adapter, TOKEN)
        assert not items[0].can_share() and items[0].effective_status() == "unknown"
    else:
        with pytest.raises(ValueError) as error:
            AccountBenefitSession.fetch_items(adapter, TOKEN)
        assert "sensitive" not in str(error.value) and TOKEN not in str(error.value)
    assert session.closed and len(session.calls) <= 4


@pytest.mark.parametrize("at", [0, 1, 2, 3, 4])
def test_cancel_between_requests_no_followups(at):
    session = Session()
    adapter = DirectBenefitQueryAdapter(session_factory=lambda: session)
    with pytest.raises(ValueError):
        AccountBenefitSession.fetch_items(
            adapter, TOKEN, cancelled=lambda: len(session.calls) >= at
        )
    assert len(session.calls) == at
    if at:
        assert session.closed


def setup(context):
    _app, window, client, errors = context
    Accounts(window.db).add(window.current_store, AccountInput("13812345678", TOKEN))
    window._refresh_all()
    page = window.account_benefit_page
    page.mode.setCurrentIndex(1)
    session = Session()
    page.session.adapter = DirectBenefitQueryAdapter(session_factory=lambda: session)
    return window, page, session, errors


def test_gui_direct_query_no_persistence_and_no_share(context):
    window, page, session, errors = setup(context)
    before = window.db.path.read_bytes()
    page.query()
    wait(window)
    assert page.table.rowCount() == 4 and "第三方权益快照" in page.result.text()
    assert window.db.path.read_bytes() == before and not errors
    assert "直连查询快照" in page.report_data(page.filtered_items())[2][0][0]
    page.share()
    assert errors and len(session.calls) == 4


def test_gui_confirmation_decline_no_network(context, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window, page, session, errors = setup(context)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    page.query()
    assert not session.calls and window.sync_worker is None


def test_requests_to_loopback_query_protocol(monkeypatch):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    import skyagent_manager.direct_benefits as direct

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, result):
            raw = json.dumps({"retcode": 0, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            calls.append(self.path.split("?")[0])
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["token"] == TOKEN and body["stateCodes"] == ["AVAILABLE"]
            self.reply(
                {
                    "couponList": [
                        {
                            "code": body["titleCode"],
                            "share": 1,
                            "expiryStr": "2099-12-31",
                        }
                    ]
                }
            )

        def do_GET(self):
            calls.append(self.path.split("?")[0])
            self.reply({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    monkeypatch.setattr(direct, "ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        items = AccountBenefitSession.fetch_items(DirectBenefitQueryAdapter(), TOKEN)
        assert len(items) == 3 and len(calls) == 4
        assert calls[-1] == "/propCard/classification/list"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

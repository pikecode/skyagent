import json
from dataclasses import replace

import pytest
import requests
from test_backend_query import Response
from test_coupon_sharing import TOKEN, URL
from test_coupon_sharing import context as context
from test_direct_benefits import Session as QuerySession

from skyagent_manager.account_benefits import AccountBenefitSession
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.coupon_sharing import CouponShareOutputs, DirectPropShareAdapter
from skyagent_manager.db import StoreDatabase
from skyagent_manager.direct_benefits import DirectBenefitQueryAdapter
from skyagent_manager.inventory import Inventory
from skyagent_manager.share_journal import ShareJournal


def prop(item):
    return replace(
        item,
        identifier="synthetic-prop-id",
        source="prop",
        kind="prop",
        code="synthetic-prop-code",
        count=2,
        shareable=True,
        dis_type="7",
        coupons_type="3",
    )


class Session:
    def __init__(self):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls, self.closed = [], False
        self.response = Response({"retcode": 0, "result": {"recommend": URL}})

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response

    def close(self):
        self.closed = True


def test_recovered_get_once_body_context_and_tls(context):
    *_, item = context
    session = Session()
    assert (
        DirectPropShareAdapter(session_factory=lambda: session).share(TOKEN, prop(item))
        == URL
    )
    assert len(session.calls) == 1 and session.closed and not session.trust_env
    args, kwargs = session.calls[0]
    assert args == (
        "GET",
        "https://miniapp.yaduo.com/atourlife/coupon/share/queryShareCode",
    )
    assert kwargs["params"]["disType"] == "7" and kwargs["params"]["couponsType"] == "3"
    assert (
        kwargs["params"]["platType"] == "6"
        and kwargs["params"]["At-Platform-Type"] == "5"
    )
    assert kwargs["params"]["appVer"] == "4.9.0" and kwargs["data"] == {"token": TOKEN}
    assert "code" not in kwargs["params"]
    assert kwargs["verify"] and not kwargs["allow_redirects"]
    assert kwargs["timeout"] == (10, 30)


@pytest.mark.parametrize(
    "change",
    [
        {"dis_type": ""},
        {"coupons_type": ""},
        {"dis_type": True},
        {"coupons_type": "0"},
        {"dis_type": "bad\n"},
        {"shareable": False},
        {"count": 0},
        {"status": "used"},
    ],
)
def test_bad_context_or_unavailable_has_no_network(context, change):
    *_, item = context
    session = Session()
    with pytest.raises(ValueError):
        DirectPropShareAdapter(session_factory=lambda: session).share(
            TOKEN, replace(prop(item), **change)
        )
    assert not session.calls


@pytest.mark.parametrize(
    "mode", ["http", "missing", "conflict", "evil", "duplicate", "oversize"]
)
def test_response_failure_closed_and_not_retried(context, mode):
    *_, item = context
    session = Session()
    if mode == "http":
        session.response.status_code = 403
    elif mode == "missing":
        session.response = Response({"retcode": 0, "result": {}})
    elif mode == "conflict":
        session.response = Response(
            {"retcode": 0, "result": {"recommend": URL, "shareUrl": URL + "x"}}
        )
    elif mode == "evil":
        session.response = Response(
            {"retcode": 0, "result": {"recommend": "https://evil.invalid/private"}}
        )
    elif mode == "duplicate":
        session.response = Response(b'{"retcode":0,"retcode":0,"result":{}}')
    else:
        session.response = Response(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError) as error:
        DirectPropShareAdapter(session_factory=lambda: session).share(TOKEN, prop(item))
    assert (
        len(session.calls) == 1
        and session.closed
        and TOKEN not in str(error.value)
        and "private" not in str(error.value)
    )


@pytest.mark.parametrize("at", [0, 1])
def test_cancel_never_reissues_get(context, at):
    *_, item = context
    session = Session()
    with pytest.raises(ValueError):
        DirectPropShareAdapter(session_factory=lambda: session).share(
            TOKEN, prop(item), cancelled=lambda: len(session.calls) >= at
        )
    assert len(session.calls) == at


@pytest.mark.parametrize("coupons_type", [None, 3, "3"])
def test_query_context_projection_and_not_reusing_recommend(coupons_type):
    session = QuerySession()
    coupon = session.payloads[3]["group"][0]["memberCouponResponse"]
    coupon.update(disType=7, couponsType=coupons_type, recommend=URL)
    item = AccountBenefitSession.fetch_items(
        DirectBenefitQueryAdapter(session_factory=lambda: session), TOKEN
    )[3]
    assert item.can_share() and item.dis_type == "7" and item.coupons_type == "3"
    assert URL not in repr(item) and item.dis_type not in repr(
        item
    )  # Context is hidden.


@pytest.mark.parametrize(
    "change",
    [
        {"disType": True},
        {"couponsType": False},
        {"recommend": "https://evil.invalid/a"},
        {"share": True},
        {"couponState": "USED"},
    ],
)
def test_query_unknown_context_or_flag_is_read_only(change):
    session = QuerySession()
    coupon = session.payloads[3]["group"][0]["memberCouponResponse"]
    coupon.update(disType=7, couponsType=3, recommend=URL)
    coupon.update(change)
    item = AccountBenefitSession.fetch_items(
        DirectBenefitQueryAdapter(session_factory=lambda: session), TOKEN
    )[3]
    assert not item.can_share()


@pytest.mark.parametrize("confirmed", [False, True])
def test_context_hold_restart_code_changes_and_cross_store_copy(context, confirmed):
    db, sid, aid, owner, item = context
    item = prop(item)
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    outputs = CouponShareOutputs(db)
    if confirmed:
        outputs.save(sid, aid, owner, item, operation, URL)
    else:
        ShareJournal(db).finish(item, operation)
    changed = replace(item, identifier="new-id", code="new-code")
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        assert ShareJournal(reopened).state_for_owner(sid, aid, changed)["state"] == (
            "confirmed" if confirmed else "unknown"
        )
        with pytest.raises(ValueError):
            ShareJournal(reopened).reserve(sid, aid, owner, changed)
        other = reopened.add_store("B")
        second = Accounts(reopened).add(
            other, AccountInput("13812345678", "synthetic-changed-token")
        )
        second_owner = AccountBenefitSession(reopened, None)._identity(other, second)[1]
        with pytest.raises(ValueError):
            ShareJournal(reopened).reserve(other, second, second_owner, changed)
    finally:
        reopened.close()


def test_phone_change_same_local_account_keeps_context_hold(context):
    db, sid, aid, owner, item = context
    item = prop(item)
    ShareJournal(db).reserve(sid, aid, owner, item)
    Accounts(db).update(sid, aid, AccountInput("13912345678", TOKEN))
    with pytest.raises(ValueError):
        ShareJournal(db).reserve(
            sid, aid, owner, replace(item, identifier="new-id", code="new-code")
        )


def test_prop_outputs_export_but_never_wrong_stock(context, tmp_path):
    db, sid, aid, owner, item = context
    item = prop(item)
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    outputs = CouponShareOutputs(db)
    outputs.save(sid, aid, owner, item, operation, URL)
    rows = outputs.list_saved(sid, aid)
    assert rows[0].kind == "prop" and len(rows[0].resource_keys) == 4
    path = tmp_path.parent / (tmp_path.name + "-prop.csv")
    outputs.export_to(path, sid, aid, owner, rows, full=True)
    assert URL in path.read_text() and "prop" in path.read_text()
    stock_id = outputs.import_operation(sid, aid, owner, operation)
    stock = Inventory(db).list(sid)
    assert len(stock) == 1 and stock[0]["kind"] == "prop" and stock[0]["id"] == stock_id
    with pytest.raises(ValueError, match="已处理"):
        outputs.import_operation(sid, aid, owner, operation)


def test_requests_to_loopback_prop_get(monkeypatch, context):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit

    import skyagent_manager.direct_benefits as direct

    *_, item = context
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            calls.append(
                (
                    parts.path,
                    parse_qs(parts.query),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            raw = json.dumps({"retcode": 0, "result": {"recommend": URL}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    monkeypatch.setattr(direct, "ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        assert DirectPropShareAdapter().share(TOKEN, prop(item)) == URL
        assert len(calls) == 1 and calls[0][0] == "/coupon/share/queryShareCode"
        assert (
            calls[0][1]["disType"] == ["7"] and calls[0][2].decode() == "token=" + TOKEN
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

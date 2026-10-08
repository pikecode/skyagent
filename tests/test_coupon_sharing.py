from dataclasses import replace

import pytest
import requests
from test_backend_query import Response

from skyagent_manager.account_benefits import (
    AccountBenefitSession,
    SimulatedBenefitAdapter,
)
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.coupon_sharing import (
    CouponShareOutputs,
    DirectCouponShareAdapter,
    validated_share_url,
)
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.share_journal import ShareJournal

URL = "https://mobile.yaduo.com/share?code=synthetic-output-code"
TOKEN = "synthetic-coupon-sharing-token"


class Session:
    def __init__(self, result=None, status=200):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls, self.closed = [], False
        self.response = Response(
            {"retcode": 0, "result": {"shareUrl": URL}} if result is None else result,
            status,
        )
        self.callback = lambda: None

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.callback()
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def context(tmp_path):
    db = StoreDatabase(tmp_path / "shares.db", key=b"c" * 32)
    sid = db.add_store("A")
    aid = Accounts(db).add(sid, AccountInput("13812345678", TOKEN))
    session = AccountBenefitSession(db, SimulatedBenefitAdapter())
    item = session.query(sid, aid)[0]
    yield db, sid, aid, session.owner, item
    db.close()


def test_single_post_protocol_and_tls(context):
    *_, item = context
    session = Session()
    assert (
        DirectCouponShareAdapter(session_factory=lambda: session).share(TOKEN, item)
        == URL
    )
    assert len(session.calls) == 1 and session.closed and not session.trust_env
    args, kwargs = session.calls[0]
    assert args == (
        "POST",
        "https://miniapp.yaduo.com/atourlife/user/share/generateShareInfo",
    )
    assert kwargs["json"] == {
        "shareBizType": "COUPON",
        "shareBizKey": item.code,
        "token": TOKEN,
    }
    assert kwargs["params"]["platType"] == "2"
    assert kwargs["params"]["At-Platform-Type"] == "2"
    assert kwargs["verify"] and not kwargs["allow_redirects"] and kwargs["stream"]
    assert kwargs["timeout"] == (10, 30)
    assert "authorized-coupon-share" in kwargs["headers"]["User-Agent"]


@pytest.mark.parametrize(
    "url",
    [
        "http://mobile.yaduo.com/share",
        "https://yaduo.com.evil.invalid/share",
        "https://evil.invalid/share",
        "https://u@mobile.yaduo.com/share",
        "https://mobile.yaduo.com:444/share",
        "https://mobile.yaduo.com./share",
        "https://mobile.yaduo.com",
        "https://mobile.yaduo.com/\\evil",
        URL + "\n",
        "https://mobile.yaduo.com/share?Token=secret",
        URL + "&access_token=secret",
        "https://mobile.yaduo.com/#/share?token%3Dsecret",
        URL + "&key=" + TOKEN,
        "https://mobile.yaduo.com/share?bad=%0a",
        "https://mobile.yaduo.com/share?bad=%ZZ",
        "https://-bad.yaduo.com/share",
        "https://mobile.yaduo.com/share?bad=%2525252574oken%253Dsecret",
        None,
        "",
        "x" * 4097,
    ],
)
def test_reject_unsafe_or_secret_urls(url):
    with pytest.raises(ValueError) as error:
        validated_share_url(url, TOKEN)
    assert TOKEN not in str(error.value) and "secret" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "https://mobile.yaduo.com/#/coupon?shareCode=synthetic",
        "https://yaduo.com:443/share?id=synthetic",
    ],
)
def test_supported_url_policy_without_dns(url):
    assert validated_share_url(url) == url


@pytest.mark.parametrize(
    "mode",
    ["http", "json", "duplicate", "oversize", "retcode", "missing", "evil", "timeout"],
)
def test_error_no_retry_and_redaction(context, mode):
    *_, item = context
    session = Session()
    if mode == "http":
        session.response.status_code = 302
    elif mode == "json":
        session.response = Response(b"synthetic-secret-invalid-json")
    elif mode == "duplicate":
        session.response = Response(b'{"retcode":0,"retcode":0,"result":{}}')
    elif mode == "oversize":
        session.response = Response(b"x" * (2 * 1024 * 1024 + 1))
    elif mode == "retcode":
        session.response = Response({"retcode": True, "result": {"shareUrl": URL}})
    elif mode == "missing":
        session.response = Response({"retcode": 0, "result": {}})
    elif mode == "evil":
        session.response = Response(
            {"retcode": 0, "result": {"shareUrl": "https://evil.invalid/secret"}}
        )
    else:

        def fail():
            raise requests.Timeout(TOKEN)

        session.callback = fail
    with pytest.raises(ValueError) as error:
        DirectCouponShareAdapter(session_factory=lambda: session).share(TOKEN, item)
    assert session.closed and len(session.calls) == 1
    assert TOKEN not in str(error.value) and "secret" not in str(error.value)


@pytest.mark.parametrize("at", [0, 1])
def test_cancel_before_or_after_single_request(context, at):
    *_, item = context
    session = Session()
    with pytest.raises(ValueError):
        DirectCouponShareAdapter(session_factory=lambda: session).share(
            TOKEN, item, cancelled=lambda: len(session.calls) >= at
        )
    assert len(session.calls) == at
    if at:
        assert session.closed


def save(context):
    db, sid, aid, owner, item = context
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    CouponShareOutputs(db).save(sid, aid, owner, item, operation, URL)
    return operation


def test_save_restart_import_and_no_replay(context):
    db, sid, aid, owner, item = context
    save(context)
    assert not Inventory(db).list(sid)
    assert URL not in repr(CouponShareOutputs(db).load(sid, aid, item))
    assert URL.encode() not in db.path.read_bytes()
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        outputs = CouponShareOutputs(reopened)
        assert outputs.load(sid, aid, item).url == URL
        stock = outputs.import_stock(sid, aid, owner, item)
        assert outputs.load(sid, aid, item).stock_id == stock
        assert Inventory(reopened).list(sid)[0]["value"] == URL
        with pytest.raises(ValueError):
            outputs.import_stock(sid, aid, owner, item)
        with pytest.raises(ValueError):
            ShareJournal(reopened).reserve(sid, aid, owner, item)
    finally:
        reopened.close()


@pytest.mark.parametrize("stage", ["save", "stock"])
def test_atomic_persistence_failure(context, monkeypatch, stage):
    db, sid, aid, owner, item = context
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    outputs = CouponShareOutputs(db)
    if stage == "stock":
        outputs.save(sid, aid, owner, item, operation, URL)
    before = db.path.read_bytes()

    def fail(*args):
        raise OSError("synthetic disk error")

    with monkeypatch.context() as patch:
        patch.setattr("skyagent_manager.db.atomic_write", fail)
        with pytest.raises(OSError):
            if stage == "save":
                outputs.save(sid, aid, owner, item, operation, URL)
            else:
                outputs.import_stock(sid, aid, owner, item)
    assert db.path.read_bytes() == before and not Inventory(db).list(sid)
    assert ShareJournal(db).state(item)["state"] == (
        "pending" if stage == "save" else "confirmed"
    )
    if stage == "stock":
        assert not outputs.load(sid, aid, item).stock_id


def test_wrong_owner_result_and_restore_block(context, tmp_path):
    db, sid, aid, owner, item = context
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    outputs = CouponShareOutputs(db)
    with pytest.raises(ValueError):
        outputs.save(sid, aid, owner[:-1] + ("changed",), item, operation, URL)
    with pytest.raises(ValueError):
        outputs.save(sid, aid, owner, item, "wrong", URL)
    outputs.save(sid, aid, owner, item, operation, URL)
    with pytest.raises(ValueError):
        outputs.load("wrong-store", aid, item)
    with pytest.raises(ValueError):
        outputs.load(sid, "wrong-account", item)
    service = BackupService(db)
    service.restore(service.create(tmp_path / "saved.skybackup"))
    with pytest.raises(ValueError):
        outputs.import_stock(sid, aid, owner, item)


def test_expired_output_not_usable_stock(context):
    db, sid, aid, owner, item = context
    save(context)
    with pytest.raises(ValueError):
        CouponShareOutputs(db).import_stock(
            sid, aid, owner, replace(item, expiry="2000-01-01")
        )
    assert not Inventory(db).list(sid)


def test_blank_hold_is_corrupt_not_absent(context):
    db, sid, aid, owner, item = context
    with db.connection:
        db.connection.execute(
            "INSERT INTO settings VALUES (?, '')", (ShareJournal.resource_key(item)[0],)
        )
    with pytest.raises(ValueError):
        ShareJournal(db).reserve(sid, aid, owner, item)


@pytest.mark.parametrize(
    "change",
    [{"shareable": False}, {"expiry": "unknown"}, {"source": "prop", "kind": "prop"}],
)
def test_unshareable_items_send_nothing(context, change):
    *_, item = context
    session = Session()
    with pytest.raises(ValueError):
        DirectCouponShareAdapter(session_factory=lambda: session).share(
            TOKEN, replace(item, **change)
        )
    assert not session.calls


def test_requests_to_loopback_single_share(context, monkeypatch):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit

    import skyagent_manager.direct_benefits as direct

    *_, item = context
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            parts = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((parts.path, parse_qs(parts.query), body))
            raw = json.dumps({"retcode": 0, "result": {"shareUrl": URL}}).encode()
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
        assert DirectCouponShareAdapter().share(TOKEN, item) == URL
        assert len(calls) == 1
        path, params, body = calls[0]
        assert path == "/user/share/generateShareInfo"
        assert params["token"] == [TOKEN] and params["platType"] == ["2"]
        assert body == {
            "shareBizType": "COUPON",
            "shareBizKey": item.code,
            "token": TOKEN,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

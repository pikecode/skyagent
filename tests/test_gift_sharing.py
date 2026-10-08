import json
from dataclasses import replace

import pytest
import requests
from test_backend_query import Response
from test_coupon_sharing import TOKEN
from test_coupon_sharing import context as context

from skyagent_manager.account_benefits import AccountBenefit, AccountBenefitSession
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.db import StoreDatabase
from skyagent_manager.direct_gifts import (
    DirectGiftOrderQueryAdapter,
    DirectGiftShareAdapter,
)
from skyagent_manager.gift_sharing import GiftShareOutputs
from skyagent_manager.inventory import Inventory
from skyagent_manager.share_journal import ShareJournal

CODE = "synthetic-private-gift-share-code"
ORDER = {
    "folioId": "synthetic-folio-123",
    "chainId": "synthetic-chain-456",
    "chainName": "虚构酒店",
    "orderStateName": "未知快照状态",
}


def gift():
    return AccountBenefit(
        "synthetic-folio-123:synthetic-chain-456",
        "order",
        "gift",
        "虚构酒店",
        "synthetic-folio-123",
        1,
        "",
        True,
        "unknown",
        folio_id="synthetic-folio-123",
        chain_id="synthetic-chain-456",
    )


class Session:
    def __init__(self, result):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls, self.closed = [], False
        self.response = Response({"retcode": 0, "result": result})
        self.callback = lambda: None

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.callback()
        return self.response

    def close(self):
        self.closed = True


def successful(context):
    db, sid, aid, owner, _ = context
    item = gift()
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    outputs = GiftShareOutputs(db)
    outputs.save(sid, aid, owner, item, operation, CODE)
    return db, sid, aid, owner, item, operation, outputs


def test_order_query_only_first_page_and_no_generate():
    session = Session([ORDER])
    items = AccountBenefitSession.fetch_items(
        DirectGiftOrderQueryAdapter(session_factory=lambda: session), TOKEN
    )
    assert len(items) == 1 and items[0].can_generate_gift() and not items[0].can_share()
    assert items[0].status == "unknown" and items[0].expiry == ""
    assert ORDER["folioId"] not in repr(items) and ORDER["chainId"] not in repr(items)
    args, kwargs = session.calls[0]
    assert args == ("POST", "https://api2.yaduo.com/atourlife/order/getOrderList")
    assert kwargs["data"]["pageNo"] == "1" and kwargs["data"]["pageSize"] == "20"
    assert (
        kwargs["data"]["channelId"] == "3000001"
        and kwargs["data"]["appVer"] == "3.31.0"
    )
    assert kwargs["data"]["state"] == "0" and kwargs["data"]["queryState"] == "1"
    assert kwargs["data"]["token"] == TOKEN and "json" not in kwargs
    assert len(session.calls) == 1 and session.closed and not session.trust_env
    assert kwargs["verify"] and kwargs["stream"] and not kwargs["allow_redirects"]


def test_generate_one_form_and_no_claim_payment_or_stock():
    session = Session({"shareCode": CODE})
    assert (
        DirectGiftShareAdapter(session_factory=lambda: session).share(TOKEN, gift())
        == CODE
    )
    assert session.closed and not session.trust_env and len(session.calls) == 1
    args, kwargs = session.calls[0]
    assert args == (
        "POST",
        "https://api2.yaduo.com/atourlife/fission/lucky/createShare",
    )
    assert kwargs["data"] == {
        "At-App-Version": "4.8.1",
        "At-Channel-Id": "20001",
        "At-Platform-Type": "2",
        "appVer": "4.8.1",
        "chainId": ORDER["chainId"],
        "channelId": "20001",
        "folioId": ORDER["folioId"],
        "platType": "2",
        "token": TOKEN,
    }
    assert kwargs["headers"]["At-Platform-Type"] == "2"
    assert (
        kwargs["verify"]
        and not kwargs["allow_redirects"]
        and kwargs["timeout"] == (10, 30)
    )


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {**ORDER, "folioId": True},
        {**ORDER, "chainId": "0"},
        {**ORDER, "folioId": "a/b"},
        {**ORDER, "chainName": "bad\n"},
    ],
)
def test_query_bad_row_rejects_entire_result(row):
    session = Session([ORDER, row])
    with pytest.raises(ValueError):
        AccountBenefitSession.fetch_items(
            DirectGiftOrderQueryAdapter(session_factory=lambda: session), TOKEN
        )
    assert len(session.calls) == 1 and session.closed


@pytest.mark.parametrize("result", [None, {}, [ORDER] * 21])
def test_order_missing_or_oversized(result):
    session = Session(result)
    with pytest.raises(ValueError):
        DirectGiftOrderQueryAdapter(session_factory=lambda: session).fetch(TOKEN, None)
    assert session.closed and len(session.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"folio_id": ""},
        {"chain_id": "bad/"},
        {"code": "wrong"},
        {"identifier": "wrong"},
        {"shareable": False},
        {"count": 0},
        {"source": "coupon"},
    ],
)
def test_invalid_context_no_network(change):
    session = Session({"shareCode": CODE})
    with pytest.raises(ValueError):
        DirectGiftShareAdapter(session_factory=lambda: session).share(
            TOKEN, replace(gift(), **change)
        )
    assert not session.calls


@pytest.mark.parametrize(
    "mode",
    ["http", "boolean", "duplicate", "oversize", "missing", "url", "token", "control"],
)
def test_bad_response_generic_closed_no_retry(mode):
    session = Session({"shareCode": CODE})
    if mode == "http":
        session.response.status_code = 302
    elif mode == "boolean":
        session.response = Response({"retcode": False, "result": {"shareCode": CODE}})
    elif mode == "duplicate":
        session.response = Response(b'{"retcode":0,"retcode":0,"result":{}}')
    elif mode == "oversize":
        session.response = Response(b"x" * (2 * 1024 * 1024 + 1))
    else:
        value = {
            "missing": None,
            "url": "https://mobile.yaduo.com/share",
            "token": TOKEN,
            "control": CODE + "\n",
        }[mode]
        session.response = Response({"retcode": 0, "result": {"shareCode": value}})
    with pytest.raises(ValueError) as caught:
        DirectGiftShareAdapter(session_factory=lambda: session).share(TOKEN, gift())
    assert TOKEN not in str(caught.value) and CODE not in str(caught.value)
    assert session.closed and len(session.calls) == 1


@pytest.mark.parametrize("at", [0, 1])
def test_cancel_before_or_after_no_retry(at):
    session = Session({"shareCode": CODE})
    with pytest.raises(ValueError):
        DirectGiftShareAdapter(session_factory=lambda: session).share(
            TOKEN, gift(), cancelled=lambda: len(session.calls) >= at
        )
    assert len(session.calls) == at


def test_encrypted_success_restart_and_offline_exports(context, tmp_path):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    assert CODE.encode() not in db.path.read_bytes()
    assert not Inventory(db).list(sid)
    reopened = StoreDatabase(db.path, key=db.key)
    try:
        outputs = GiftShareOutputs(reopened)
        rows = outputs.list_saved(sid, aid)
        assert len(rows) == 1 and rows[0].share_code == CODE and CODE not in repr(rows)
        before = reopened.path.read_bytes()
        masked = tmp_path.parent / (tmp_path.name + "-masked.csv")
        full = tmp_path.parent / (tmp_path.name + "-full.csv")
        outputs.export_to(masked, sid, aid, owner, rows)
        outputs.export_to(full, sid, aid, owner, rows, full=True)
        assert CODE not in masked.read_text(encoding="utf-8-sig")
        assert CODE in full.read_text(encoding="utf-8-sig")
        assert reopened.path.read_bytes() == before
    finally:
        reopened.close()


@pytest.mark.parametrize("state", ["pending", "unknown", "confirmed"])
def test_order_guard_survives_token_copy_chain_change(context, state):
    db, sid, aid, owner, _ = context
    item = gift()
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    if state == "confirmed":
        GiftShareOutputs(db).save(sid, aid, owner, item, operation, CODE)
    elif state == "unknown":
        ShareJournal(db).finish(item, operation)
    other = db.add_store("copy")
    copied = Accounts(db).add(other, AccountInput("13812345678", TOKEN + "-changed"))
    copied_owner = AccountBenefitSession(db, None)._identity(other, copied)[1]
    with pytest.raises(ValueError):
        ShareJournal(db).reserve(other, copied, copied_owner, item)
    changed = replace(
        item, chain_id="new-chain", identifier=item.folio_id + ":new-chain"
    )
    with pytest.raises(ValueError):
        ShareJournal(db).reserve(sid, aid, owner, changed)


def test_save_failure_retains_durable_pending(context, monkeypatch):
    db, sid, aid, owner, _ = context
    item = gift()
    operation = ShareJournal(db).reserve(sid, aid, owner, item)
    before = db.path.read_bytes()

    def fail():
        raise OSError("synthetic-disk-failure")

    monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises(OSError):
        GiftShareOutputs(db).save(sid, aid, owner, item, operation, CODE)
    assert ShareJournal(db).state(item)["state"] == "pending"
    assert (
        not GiftShareOutputs(db).list_saved(sid, aid) and db.path.read_bytes() == before
    )


def test_same_code_other_order_not_confirmed(context):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    other = replace(
        item,
        folio_id="other-order",
        code="other-order",
        identifier="other-order:" + item.chain_id,
    )
    other_operation = ShareJournal(db).reserve(sid, aid, owner, other)
    with pytest.raises(ValueError):
        outputs.save(sid, aid, owner, other, other_operation, CODE)
    assert ShareJournal(db).state(other)["state"] == "pending"


def test_restore_allows_masked_only_and_prevents_regeneration(context, tmp_path):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    backup = BackupService(db)
    backup.restore(backup.create(tmp_path / "saved.skybackup"))
    rows = outputs.list_saved(sid, aid)
    path = tmp_path.parent / (tmp_path.name + "-restore.csv")
    outputs.export_to(path, sid, aid, owner, rows)
    with pytest.raises(ValueError):
        outputs.export_to(path, sid, aid, owner, rows, full=True)
    with pytest.raises(ValueError):
        ShareJournal(db).reserve(sid, aid, owner, item)
    assert CODE not in path.read_text(encoding="utf-8-sig")


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "wrong"),
        ("share_code", "changed-code"),
        ("resource_keys", []),
        ("created_at", "2099-01-01T00:00:00+00:00"),
    ],
)
def test_tampered_output_fail_closed(context, field, value):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    name = outputs.PREFIX + operation
    payload = json.loads(db.get_setting(name))
    payload[field] = value
    with db.connection:
        db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?", (json.dumps(payload), name)
        )
    with pytest.raises(ValueError):
        outputs.load_operation(sid, aid, operation)


def test_export_forbidden_app_directory_and_changed_owner(context, tmp_path):
    db, sid, aid, owner, item, operation, outputs = successful(context)
    rows = outputs.list_saved(sid, aid)
    with pytest.raises(ValueError):
        outputs.export_to(tmp_path / "bad.csv", sid, aid, owner, rows, full=True)
    with pytest.raises(ValueError):
        outputs.export_to(
            tmp_path.parent / "bad-owner.csv", sid, aid, (sid, aid, "changed"), rows
        )


@pytest.mark.parametrize("generate", [False, True])
def test_requests_loopback_form_contract(monkeypatch, generate):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs

    from skyagent_manager import direct_gifts

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(
                (self.path, parse_qs(body.decode()), self.headers["Content-Type"])
            )
            result = {"shareCode": CODE} if generate else [ORDER]
            payload = json.dumps({"retcode": 0, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    monkeypatch.setattr(
        direct_gifts, "ORIGIN", f"http://127.0.0.1:{server.server_port}"
    )
    try:
        if generate:
            assert DirectGiftShareAdapter().share(TOKEN, gift()) == CODE
            assert received[0][0] == "/fission/lucky/createShare"
            assert received[0][1]["folioId"] == [ORDER["folioId"]]
        else:
            assert len(DirectGiftOrderQueryAdapter().fetch(TOKEN, None).items) == 1
            assert received[0][0] == "/order/getOrderList"
            assert received[0][1]["pageSize"] == ["20"]
        assert received[0][1]["token"] == [TOKEN]
        assert (
            received[0][2] == "application/x-www-form-urlencoded" and len(received) == 1
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

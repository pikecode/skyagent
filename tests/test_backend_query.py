import json

import pytest

from skyagent_manager.backend_query import (
    BackendCaptchaRequired,
    BackendQueryClient,
    BackendQueryError,
    validate_captcha_svg,
)


class Response:
    def __init__(self, payload, status=200):
        self.payload = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.payload


class Session:
    def __init__(self):
        self.response = Response({"token": "synthetic-bearer-token"})
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    def close(self):
        self.closed = True


def test_login_query_projection_and_close():
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    with pytest.raises(BackendQueryError):
        client.accounts_page()
    assert not session.calls
    client.login("test", "synthetic-password")
    session.response = Response(
        {
            "items": [
                {"id": "a1", "phone": "00000000000", "token": "private-account-token"}
            ],
            "meta": {"page": 1, "total": 1, "hasMore": False},
        }
    )
    result = client.accounts_page()
    assert result.items[0].phone == "00000000000"
    assert "private-account-token" not in repr(result) and "00000000000" not in repr(
        result
    )
    method, url, options = session.calls[-1]
    assert method == "GET" and url.endswith("/api/pool/accounts")
    assert options["headers"] == {"Authorization": "Bearer synthetic-bearer-token"}
    assert (
        options["verify"] and not options["allow_redirects"] and not session.trust_env
    )
    client.close()
    assert session.closed
    with pytest.raises(BackendQueryError):
        client.accounts_page()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"token":"a","token":"b"}',
        b'{"token":NaN}',
        b"[" * 1000 + b"0" + b"]" * 1000,
        b"x" * (2 * 1024 * 1024 + 1),
    ],
    ids=["duplicate-fields", "nonfinite-number", "deep-nesting", "oversized-body"],
)
def test_invalid_response_redacted_no_retry(payload):
    session = Session()
    session.response = Response(payload)
    client = BackendQueryClient("https://test.example.invalid", session=session)
    with pytest.raises(BackendQueryError):
        client.login("test", "private-password")
    assert len(session.calls) == 1
    assert client._token == ""


@pytest.mark.parametrize("status", [401, 403, 307, 500])
def test_status_no_retry_or_redirect(status):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "password")
    session.response = Response({"message": "private-token"}, status)
    with pytest.raises(BackendQueryError) as error:
        client.accounts_page()
    assert "private-token" not in str(error.value)
    assert len(session.calls) == 2
    if status in {401, 403}:
        assert client._token == ""


def test_duplicate_identity_and_changed_page_rejected():
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "password")
    row = {"id": "a1", "phone": "00000000000"}
    session.response = Response(
        {"items": [row, row], "meta": {"page": 1, "total": 2, "hasMore": False}}
    )
    with pytest.raises(BackendQueryError):
        client.accounts_page()
    session.response = Response(
        {"items": [row], "meta": {"page": 1, "total": 1, "hasMore": False}}
    )
    with pytest.raises(BackendQueryError):
        client.accounts_page(2)


def test_http_requires_explicit_permission():
    with pytest.raises(ValueError):
        BackendQueryClient("http://127.0.0.1")


def test_search_is_encoded_as_parameter_and_not_echoed():
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "password")
    session.response = Response(
        {"items": [], "meta": {"page": 1, "total": 0, "hasMore": False}}
    )
    result = client.accounts_page(search="  synthetic &keyword  ")
    assert session.calls[-1][2]["params"]["search"] == "synthetic &keyword"
    assert "synthetic &keyword" not in repr(result)


@pytest.mark.parametrize("search", ["x" * 257, "x\n", None, False])
def test_invalid_search_makes_no_request(search):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "password")
    count = len(session.calls)
    with pytest.raises(BackendQueryError):
        client.accounts_page(search=search)
    assert len(session.calls) == count


def test_manual_captcha_same_origin_image_and_login_fields():
    identifier = "12345678-1234-1234-1234-123456789abc"
    session = Session()
    session.response = Response(
        {
            "captchaRequired": True,
            "captcha": {"id": identifier, "imageUrl": "https://evil.example/image"},
        },
        403,
    )
    client = BackendQueryClient("https://test.example.invalid", session=session)
    with pytest.raises(BackendCaptchaRequired) as error:
        client.login("test", "password")
    assert error.value.identifier == identifier and len(session.calls) == 1
    session.response = Response(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="42"><rect width="3" height="3" fill="#fff"/></svg>'
    )
    image = client.captcha_image(identifier)
    assert b"svg" in image
    assert (
        session.calls[-1][1]
        == f"https://test.example.invalid/api/auth/login-captcha/{identifier}/image"
    )
    session.response = Response({"token": "synthetic-token"})
    client.login("test", "password", identifier, "ABC12")
    assert session.calls[-1][2]["json"]["captchaAnswer"] == "ABC12"
    assert session.calls[-1][2]["json"]["captchaId"] == identifier


@pytest.mark.parametrize(
    "content",
    [
        "<script>alert(1)</script>",
        '<image href="https://evil.example"/>',
        '<rect onclick="evil()"/>',
        '<rect fill="url(https://evil.example)"/>',
        "<foreignObject/>",
        '<use href="#other"/>',
    ],
)
def test_captcha_rejects_active_or_external_svg(content):
    with pytest.raises(BackendQueryError):
        validate_captcha_svg(
            f'<svg xmlns="http://www.w3.org/2000/svg">{content}</svg>'.encode()
        )


def test_captcha_identifier_and_size_bounds():
    client = BackendQueryClient("https://test.example.invalid", session=Session())
    with pytest.raises(BackendQueryError):
        client.captcha_image("../external")
    with pytest.raises(BackendQueryError):
        validate_captcha_svg(b"x" * (128 * 1024 + 1))
    with pytest.raises(BackendQueryError):
        validate_captcha_svg(b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>')


def test_captcha_rejects_deep_nesting():
    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        + b"<g>" * 40
        + b"</g>" * 40
        + b"</svg>"
    )
    with pytest.raises(BackendQueryError):
        validate_captcha_svg(payload)


@pytest.mark.parametrize("bad", ["id", "bool", "count", "missing"])
def test_detail_rejects_invalid_identity_and_fields(bad):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "password")
    data = {
        "id": "a1",
        "phone": "00000000000",
        "is_enabled": True,
        "is_online": False,
        "is_new_user": True,
        "breakfast_coupons": 2,
        "room_upgrade_coupons": 1,
        "late_checkout_coupons": 0,
    }
    if bad == "id":
        data["id"] = "other"
    elif bad == "bool":
        data["is_online"] = "false"
    elif bad == "count":
        data["breakfast_coupons"] = True
    else:
        del data["late_checkout_coupons"]
    session.response = Response(data)
    with pytest.raises(BackendQueryError):
        client.account_detail("a1")


@pytest.mark.parametrize(
    "mode", ["valid", "absent", "empty", "duplicate", "count", "type", "limit"]
)
def test_real_detail_discount_asset_projection(mode):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "password")
    asset = {
        "code": "private-discount-code",
        "couponDesc": "测试折扣券",
        "valueDesc": "30元",
        "expiryStr": "2026-12-31",
        "expiryTip": "待后台核对",
        "couponState": "AVAILABLE",
        "token": "do-not-retain",
        "discountRule": "discard-rule",
    }
    data = {
        "id": "a1",
        "phone": "00000000000",
        "is_enabled": True,
        "is_online": True,
        "is_new_user": False,
        "breakfast_coupons": 0,
        "room_upgrade_coupons": 0,
        "late_checkout_coupons": 0,
        "discount_coupon_assets": [asset],
        "discount_coupon_assets_count": 1,
    }
    if mode == "absent":
        del data["discount_coupon_assets"]
    elif mode == "empty":
        data.update(discount_coupon_assets=[], discount_coupon_assets_count=0)
    elif mode == "duplicate":
        data.update(
            discount_coupon_assets=[asset, asset], discount_coupon_assets_count=2
        )
    elif mode == "count":
        data["discount_coupon_assets_count"] = True
    elif mode == "type":
        asset["couponDesc"] = []
    elif mode == "limit":
        data["discount_coupon_assets"] = [asset] * 501
    session.response = Response(data)
    if mode in {"duplicate", "count", "type", "limit"}:
        with pytest.raises(BackendQueryError) as error:
            client.account_detail("a1")
        assert "private-discount-code" not in str(error.value)
    else:
        result = client.account_detail("a1")
        if mode == "valid":
            assert result.discount_assets[0].description == "测试折扣券"
            assert result.discount_assets[0].masked_code != asset["code"]
            assert "private-discount-code" not in repr(result)
            assert "do-not-retain" not in repr(result) and "discard-rule" not in repr(
                result
            )
        else:
            assert result.discount_assets == (None if mode == "absent" else ())


def test_real_loopback_login_captcha_list_detail_contract():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from socketserver import TCPServer
    from threading import Thread

    challenge = "12345678-1234-1234-1234-123456789abc"
    calls = []
    detail = {
        "id": "a1",
        "phone": "00000000000",
        "is_enabled": True,
        "is_online": False,
        "is_new_user": False,
        "breakfast_coupons": 2,
        "room_upgrade_coupons": 1,
        "late_checkout_coupons": 3,
        "token": "must-not-be-retained",
        "remark": "private-note",
        "discount_coupon_assets": [
            {
                "code": "loopback-discount-code",
                "couponDesc": "本机合约券",
                "valueDesc": "30元",
                "expiryStr": "2026-12-31",
                "couponState": "AVAILABLE",
            }
        ],
        "discount_coupon_assets_count": 1,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, status=200):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            calls.append(("POST", self.path))
            assert self.path == "/api/auth/login"
            fields = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if (
                fields.get("captchaId") != challenge
                or fields.get("captchaAnswer") != "ABC12"
            ):
                self.reply({"captchaRequired": True, "captcha": {"id": challenge}}, 403)
            else:
                self.reply({"token": "synthetic-session"})

        def do_GET(self):
            calls.append(("GET", self.path))
            if self.path == f"/api/auth/login-captcha/{challenge}/image":
                self.reply(
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="42"><rect width="10" height="10"/></svg>'
                )
            elif self.headers.get("Authorization") != "Bearer synthetic-session":
                self.reply({}, 401)
            elif self.path == "/api/pool/accounts/a1":
                self.reply(detail)
            elif self.path == "/api/pool/accounts/a1/token":
                self.reply({"token": "loopback-authorized-account-token"})
            elif self.path == "/api/gift-codes/g1/logs":
                self.reply(
                    {
                        "items": [
                            {
                                "id": "log1",
                                "codeId": "g1",
                                "accountPhone": "13812345678",
                                "createdAt": "2026-10-07T00:00:00Z",
                                "content": "discard-log-content",
                                "shareCode": "discard-log-share-code",
                            }
                        ]
                    }
                )
            elif self.path.startswith("/api/pool/accounts/a1/orders?"):
                self.reply(
                    {
                        "page": {"pageNo": 1, "pageSize": 10, "totalCount": 1},
                        "result": [
                            {
                                "folioId": 123456789,
                                "chainId": 123,
                                "hotelName": "本机合约酒店",
                                "orderStateName": "待入住",
                            }
                        ],
                    }
                )
            elif self.path.startswith("/api/pool/accounts/a1/orders/123456789/detail?"):
                self.reply(
                    {
                        "result": {
                            "folioId": 123456789,
                            "chainId": 123,
                            "payState": 1,
                            "orderState": 0,
                            "orderStateName": "待入住",
                        }
                    }
                )
            elif self.path.startswith("/api/gift-codes?"):
                from urllib.parse import parse_qs, urlsplit

                query = parse_qs(urlsplit(self.path).query)
                assert query["page"] == ["1"] and query["pageSize"] == ["20"]
                if "keyword" in query:
                    assert query["keyword"] == ["synthetic loopback"]
                    assert query["status"] == ["ACTIVE"] and query["source"] == ["SCAN"]
                self.reply(
                    {
                        "items": [
                            {
                                "id": "g1",
                                "shareCode": "loopback-gift-code",
                                "source": "SCAN",
                                "status": "ACTIVE",
                                "receivedCount": 0,
                                "maxReceive": 20,
                            }
                        ],
                        "page": 1,
                        "pageSize": 20,
                        "total": 1,
                    }
                )
            elif self.path == "/api/pool/coupon-exchange-tasks/task1":
                self.reply(
                    {
                        "item": {
                            "id": "task1",
                            "state": "completed",
                            "progress": 100,
                            "result": {"token": "discard-task-token"},
                        }
                    }
                )
            elif self.path.startswith("/api/pool/accounts?"):
                self.reply(
                    {
                        "items": [detail],
                        "meta": {"page": 1, "total": 1, "hasMore": False},
                    }
                )
            else:
                self.reply({}, 404)

    class Server(ThreadingHTTPServer):
        def server_bind(self):
            TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = self.server_address[1]

    server = Server(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    client = BackendQueryClient(
        f"http://127.0.0.1:{server.server_port}", allow_http=True
    )
    try:
        with pytest.raises(BackendCaptchaRequired):
            client.login("synthetic", "synthetic-password")
        client.captcha_image(challenge)
        client.login("synthetic", "synthetic-password", challenge, "ABC12")
        assert client.accounts_page().items[0].identifier == "a1"
        result = client.account_detail("a1")
        assert result.breakfast == 2 and result.late_checkout == 3
        assert result.discount_assets[0].description == "本机合约券"
        assert result.discount_assets[0].masked_code != "loopback-discount-code"
        assert "must-not-be-retained" not in repr(
            result
        ) and "private-note" not in repr(result)
        orders = client.account_orders("a1", authorized=True)
        assert orders.items[0].hotel == "本机合约酒店" and not orders.has_more
        order_detail = client.account_order_detail(
            "a1", "123456789", "123", authorized=True
        )
        assert order_detail.payment_state == 1
        gifts = client.gift_codes(authorized=True)
        assert gifts.items[0].status == "ACTIVE" and not gifts.has_more
        assert gifts.items[0].masked_code != "loopback-gift-code"
        task = client.exchange_task("task1", authorized=True)
        assert task.state == "completed" and task.progress == 100
        assert "discard-task-token" not in repr(task)
        credential = client.account_credential("a1", authorized=True)
        assert credential.token == "loopback-authorized-account-token"
        assert credential.phone == "00000000000"
        assert "loopback-authorized-account-token" not in repr(credential)
        logs = client.gift_receive_logs(gifts.items[0].identifier, authorized=True)
        assert logs.items[0].masked_phone != "13812345678"
        assert "discard-log-content" not in repr(logs)
        assert "discard-log-share-code" not in repr(logs)
        filtered = client.gift_codes(
            authorized=True,
            keyword="synthetic loopback",
            status="ACTIVE",
            source="SCAN",
        )
        assert filtered.items[0].source == "SCAN"
        assert len(calls) == 14
        assert all(
            path.startswith("/api/auth/")
            or path.startswith("/api/pool/accounts")
            or path.startswith("/api/gift-codes")
            or path.startswith("/api/pool/coupon-exchange-tasks/")
            for _, path in calls
        )
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

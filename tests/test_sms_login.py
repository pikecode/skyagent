import pytest
from test_backend_query import Response, Session

from skyagent_manager.backend_query import BackendQueryClient, BackendQueryError


def client():
    session = Session()
    value = BackendQueryClient("https://test.example.invalid", session=session)
    value.login("test", "synthetic-password")
    return value, session


def test_sms_real_contract_authorization_cooldown_and_private_token():
    value, session = client()
    with pytest.raises(BackendQueryError):
        value.send_account_login_code("13812345678")
    assert len(session.calls) == 1
    session.response = Response(
        {"ok": True, "cooldownMs": 60000, "message": "private-response"}
    )
    assert value.send_account_login_code("13812345678", authorized=True) == 60000
    method, path, options = session.calls[-1]
    assert method == "POST" and path.endswith("/api/pool/accounts/login/send-code")
    assert options["json"] == {"phone": "13812345678"}
    with pytest.raises(BackendQueryError):
        value.send_account_login_code("13812345678", authorized=True)
    assert len(session.calls) == 2
    session.response = Response(
        {
            "ok": True,
            "token": "synthetic-sms-login-token",
            "isPlatinum": False,
            "isNewUser": True,
            "vipGrade": "discard",
        }
    )
    result = value.account_login_by_code("13812345678", "123456", authorized=True)
    assert result.token == "synthetic-sms-login-token" and result.new_user_hint
    assert result.token not in repr(result) and "discard" not in repr(result)
    assert session.calls[-1][1].endswith("/api/pool/accounts/login/token")
    assert session.calls[-1][2]["json"] == {
        "phone": "13812345678",
        "verifyCode": "123456",
    }
    assert value._token == "synthetic-bearer-token"
    value.close()
    assert not value._sms_cooldowns


@pytest.mark.parametrize("mode", ["timeout", "invalid", "server"])
def test_unknown_send_result_blocks_immediate_resend_and_redacts(mode):
    value, session = client()
    session.response = Response(
        {"ok": False, "message": "private-sensitive-body"},
        500 if mode == "server" else 200,
    )
    if mode == "timeout":

        def fail(*args, **kwargs):
            session.calls.append((args, kwargs))
            raise RuntimeError("private-sensitive-body")

        session.request = fail
    with pytest.raises(BackendQueryError) as error:
        value.send_account_login_code("13812345678", authorized=True)
    assert "private-sensitive-body" not in str(error.value)
    count = len(session.calls)
    with pytest.raises(BackendQueryError):
        value.send_account_login_code("13812345678", authorized=True)
    assert len(session.calls) == count


@pytest.mark.parametrize(
    "patch", [{"token": "short"}, {"isNewUser": 1}, {"ok": False}, {"isPlatinum": None}]
)
def test_invalid_login_result_not_retained(patch):
    value, session = client()
    data = {
        "ok": True,
        "token": "synthetic-sms-token",
        "isPlatinum": False,
        "isNewUser": False,
    }
    data.update(patch)
    session.response = Response(data)
    with pytest.raises(BackendQueryError) as error:
        value.account_login_by_code("13812345678", "123456", authorized=True)
    assert "synthetic-sms-token" not in str(error.value)
    assert value._token == "synthetic-bearer-token"


def test_invalid_inputs_and_missing_backend_login_never_request():
    value, session = client()
    for phone, code, authorization in [
        ("invalid", "123456", True),
        ("13812345678", "", True),
        ("13812345678", "123456", False),
    ]:
        with pytest.raises(BackendQueryError):
            value.account_login_by_code(phone, code, authorized=authorization)
    assert len(session.calls) == 1


def test_loopback_sms_login_contract_no_external_service():
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body, self.headers.get("Authorization")))
            if self.path == "/api/auth/login":
                response = {"token": "synthetic-loopback-bearer"}
            elif self.path.endswith("/send-code"):
                response = {"ok": True, "cooldownMs": 60000}
            elif self.path.endswith("/token"):
                response = {
                    "ok": True,
                    "token": "synthetic-loopback-account-token",
                    "isPlatinum": False,
                    "isNewUser": True,
                }
            else:
                self.send_error(404)
                return
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    value = BackendQueryClient(
        f"http://127.0.0.1:{server.server_port}", allow_http=True
    )
    try:
        value.login("test", "synthetic-password")
        value.send_account_login_code("13800000000", authorized=True)
        result = value.account_login_by_code("13800000000", "123456", authorized=True)
        assert result.token == "synthetic-loopback-account-token"
        assert len(calls) == 3
        assert calls[1][2] == calls[2][2] == "Bearer synthetic-loopback-bearer"
        assert calls[2][1] == {"phone": "13800000000", "verifyCode": "123456"}
    finally:
        value.close()
        server.shutdown()
        server.server_close()
        thread.join()
    value.close()
    with pytest.raises(BackendQueryError):
        value.send_account_login_code("13812345678", authorized=True)
    assert len(calls) == 3

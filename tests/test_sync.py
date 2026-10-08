from __future__ import annotations

import json

import pytest
import requests

from skyagent_manager.sync import (
    ACCOUNT_PATH,
    AccountImportClient,
    SyncClient,
    parse_results,
    validate_config,
)


def config():
    return validate_config(
        "https://backend.example.com", "/members", "/benefits", False
    )


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        yield json.dumps(self.data).encode()


class Session:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        pass


@pytest.mark.parametrize("account", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        b'{"ok":false,"ok":true,"results":[{"index":0,"ok":true}]}',
        b'{"ok":true,"results":[{"index":0,"ok":false,"ok":true}]}',
        b'{"ok":true,"results":[{"index":0,"ok":true}],"other":NaN}',
        b"[" * 1500 + b"0" + b"]" * 1500,
    ],
)
def test_ambiguous_or_deep_json_is_unknown_not_success(account, payload):
    class RawResponse(Response):
        def iter_content(self, chunk_size):
            yield payload

    session = Session(RawResponse())
    client = (
        AccountImportClient(config(), "test-secret", session=session)
        if account
        else SyncClient(config(), "members", "test-secret", session=session)
    )
    items = [
        {
            "id": "a",
            "phone": "13812345678",
            "token": "synthetic-private-token",
            "is_online": True,
            "is_new_user": False,
            "is_enabled": True,
            "remark": "软件导入",
        }
    ]
    results = client.submit(items)
    assert len(results) == 1 and not results[0].ok
    assert "可能未知" in results[0].summary
    assert "13812345678" not in repr(results) and "synthetic-private-token" not in repr(
        results
    )
    assert len(session.calls) == 1


def test_http_is_explicit_and_account_address_is_normalized():
    url = "http://47.94.173.184:63200" + ACCOUNT_PATH
    with pytest.raises(ValueError):
        validate_config(url, "", "", False)
    assert validate_config(url, "", "", True)["api_url"] == "http://47.94.173.184:63200"
    with pytest.raises(ValueError):
        validate_config("https://example.com", ACCOUNT_PATH, "", False)


@pytest.mark.parametrize(
    "url,path",
    [
        ("https://user:secret@example.com", "/members"),
        ("https://example.com?q=a", "/members"),
        ("https://example.com", "//other.example.com"),
        ("https://example.com", "/members?token=a"),
        ("https://example.com:bad", "/members"),
    ],
)
def test_rejects_ambiguous_urls(url, path):
    with pytest.raises(ValueError):
        validate_config(url, path, "", False)


def test_batch_results_missing_duplicates_and_sensitive_errors():
    items = [{"id": "a"}, {"id": "b"}]
    results = parse_results(items, {"ok": True, "results": [{"index": 0, "ok": True}]})
    assert results[0].ok and not results[1].ok
    results = parse_results(
        items,
        {
            "ok": True,
            "results": [
                {"index": 0, "ok": True},
                {"index": 0, "ok": True},
                {"index": 1, "ok": False, "error": "secret 13812345678"},
            ],
        },
    )
    assert all(not result.ok for result in results)
    assert "13812345678" not in repr(results)


def test_transport_uses_bounded_timeouts_tls_and_no_redirects():
    session = Session(Response({"ok": True, "results": [{"index": 0, "ok": True}]}))
    client = SyncClient(config(), "members", "dummy-secret", session=session)
    assert client.submit([{"id": "a", "phone": "13812345678"}])[0].ok
    url, args = session.calls[0]
    assert url == "https://backend.example.com/members"
    assert args["verify"] is True and args["allow_redirects"] is False
    assert args["timeout"] == (10, 30)
    assert args["stream"] is True
    assert args["headers"]["x-skyhotel-import-secret"] == "dummy-secret"


@pytest.mark.parametrize("status", [301, 401, 500])
def test_http_errors_do_not_claim_success(status):
    client = SyncClient(
        config(), "members", "dummy-secret", session=Session(Response(status=status))
    )
    assert not client.submit([{"id": "a"}])[0].ok


def test_timeout_is_unknown_and_not_automatically_retried():
    session = Session(error=requests.Timeout("potentially-sensitive"))
    client = SyncClient(config(), "members", "dummy-secret", session=session)
    result = client.submit([{"id": "a"}])[0]
    assert not result.ok and "未知" in result.summary
    assert len(session.calls) == 1 and "potentially-sensitive" not in result.summary


def test_batch_size_enforced_before_network():
    session = Session()
    client = SyncClient(config(), "members", "dummy-secret", session=session)
    assert client.submit([]) == []
    with pytest.raises(ValueError, match="200"):
        client.submit([{"id": str(i)} for i in range(201)])
    assert not session.calls


def test_oversized_response_is_bounded(monkeypatch):
    monkeypatch.setattr("skyagent_manager.sync.MAX_RESPONSE_SIZE", 8)
    client = SyncClient(
        config(),
        "members",
        "dummy-secret",
        session=Session(Response({"ok": True, "results": []})),
    )
    result = client.submit([{"id": "a"}])[0]
    assert not result.ok and "安全限制" in result.summary

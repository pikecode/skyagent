import pytest

from skyagent_manager.silver_protocol import (
    EXCHANGE_PATH,
    PROTECTED_FIELDS,
    SMS_PATH,
    build_exchange_draft,
    build_sms_draft,
    classify_redemption_response,
    parse_silver_link_draft,
)

PHONE = "synthetic_encrypted_phone_only"


def test_offline_forms_and_sensitive_repr():
    sms = build_sms_draft(PHONE)
    exchange = build_exchange_draft(PHONE, "123456", {"code": "FB_fake"})
    assert sms.path == SMS_PATH
    assert exchange.path == EXCHANGE_PATH
    assert sms.form()["type"] == exchange.form()["type"] == "18"
    assert exchange.form()["taskCode"] == "magic_invite"
    assert exchange.form()["verifyCode"] == "123456"
    assert not sms.executable and not exchange.executable
    assert all(secret not in repr(exchange) for secret in (PHONE, "123456", "FB_fake"))
    copied = exchange.form()
    copied["type"] = "17"
    assert exchange.form()["type"] == "18"


@pytest.mark.parametrize("key", sorted(PROTECTED_FIELDS))
def test_protected_fields_never_overridden(key):
    with pytest.raises(ValueError):
        build_exchange_draft(PHONE, "123456", {"code": "FB_fake", key: "override"})


@pytest.mark.parametrize(
    "phone", ["13800138000", "1" * 32, "", None, "bad\nphone", "x" * 4097]
)
def test_reject_plaintext_and_invalid_phone(phone):
    with pytest.raises(ValueError):
        build_sms_draft(phone)


@pytest.mark.parametrize(
    "code", ["", "123", "123456789", "１２３４５６", None, "1234\n"]
)
def test_verification_is_bounded_ascii(code):
    with pytest.raises(ValueError):
        build_exchange_draft(PHONE, code, {"code": "FB_fake"})


@pytest.mark.parametrize(
    "params",
    [{}, {"code": "FB_fake", "extra": "x"}, {"code": "x&token=y"}, {"code": 1}, None],
)
def test_unknown_business_contract_fails_closed(params):
    with pytest.raises(ValueError):
        build_exchange_draft(PHONE, "123456", params)


@pytest.mark.parametrize(
    "payload", [{"retcode": 0, "result": {}}, {"retcode": 1}, None, {"success": True}]
)
def test_no_guessed_success_or_safe_release(payload):
    assert classify_redemption_response(payload) == "unknown"


@pytest.mark.parametrize(
    "suffix",
    [
        "/memberExchange?code=FB_fake",
        "/#/memberExchange?code=FB_fake&name=%E9%93%B6%E5%8D%A1",
        "/memberExchange?title=Silver&code=FB_fake&name=Silver",
    ],
)
def test_link_to_exchange_draft_without_network(suffix, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline parser attempted network access")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    parsed = parse_silver_link_draft("https://wechat.yaduo.com" + suffix)
    assert not parsed.executable
    assert "FB_fake" not in repr(parsed)
    form = build_exchange_draft(PHONE, "123456", parsed.business_params()).form()
    assert form["code"] == "FB_fake"
    assert "name" not in form and "title" not in form


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "http://wechat.yaduo.com/?code=FB_fake",
        "https://evil.invalid/?code=FB_fake",
        "https://wechat.yaduo.com.evil.invalid/?code=FB_fake",
        "https://wechat.yaduo.com@evil.invalid/?code=FB_fake",
        "https://user@wechat.yaduo.com/?code=FB_fake",
        "https://wechat.yaduo.com:443/?code=FB_fake",
        "https://wechat.yaduo.com./?code=FB_fake",
        "https://wechat.yaduo.com/?code=FB_fake&code=FB_other",
        "https://wechat.yaduo.com/?code=FB_fake&%63ode=FB_other",
        "https://wechat.yaduo.com/?code=FB_fake&token=secret",
        "https://wechat.yaduo.com/?code=FB_fake&unknown=x",
        "https://wechat.yaduo.com/?code=FB_fake#/route?code=FB_other",
        "https://wechat.yaduo.com/#code=FB_fake",
        "https://wechat.yaduo.com/?code=",
        "https://wechat.yaduo.com/?code=FB_fake&name=",
        "https://wechat.yaduo.com/?code=FB_fake&name=%FF",
        "https://wechat.yaduo.com/?code=FB_fake&name=%ZZ",
        "https://wechat.yaduo.com/?code=FB_fake&name=%0A",
        "https://wechat.yaduo.com/?code=FB_fake&title=%7F",
        "https://wechat.yaduo.com/?code=FB_fake&name=x&title=x&other=x",
        "https://wechat.yaduo.com/?code=FB_fake%26token%3Dx",
        "https://wechat.yaduo.com/?code=FB_fake\n",
        "https://wechat.yaduo.com/\\evil?code=FB_fake",
        "https://wechat.yaduo.com/?code=" + "x" * 4096,
    ],
)
def test_link_rejection_is_generic_and_secret_free(value):
    with pytest.raises(ValueError) as caught:
        parse_silver_link_draft(value)
    assert "FB_fake" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("key", sorted(PROTECTED_FIELDS))
def test_link_protected_fields_rejected_before_form(key):
    with pytest.raises(ValueError):
        parse_silver_link_draft(f"https://wechat.yaduo.com/?code=FB_fake&{key}=x")

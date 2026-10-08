"""Offline silver-card form drafts; deliberately no transport or encryption helper.

These drafts are not production-ready: the encrypted-phone algorithm, complete
business parameter contract and authoritative redemption receipt remain unknown.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

ORIGIN = "https://api2.yaduo.com/atourlife"
SMS_PATH = "/miniapp/verifyCode/getVerifyCodeByTypeForMarket"
EXCHANGE_PATH = "/user/magicCodeExchange"
PROTECTED_FIELDS = frozenset(
    {
        "activeId",
        "r",
        "token",
        "appVer",
        "channelId",
        "type",
        "phoneNum",
        "taskCode",
        "verifyCode",
        "activitySource",
        "platType",
    }
)


@dataclass(frozen=True)
class SilverLinkDraft:
    code: str = field(repr=False)
    executable: bool = field(default=False, init=False)

    def business_params(self):
        return {"code": self.code}


def parse_silver_link_draft(value):
    """Parse an offline subset, not provider qualification or URL resolution.

    The fixed host and accepted parameter subset are conservative local policy,
    not a recovered exhaustive whitelist of production silver-card links.
    """
    try:
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in value)
        ):
            raise ValueError()
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.netloc != "wechat.yaduo.com"
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError()
        fragment_query = ""
        if parts.fragment:
            route, separator, fragment_query = parts.fragment.partition("?")
            if not separator or not route.startswith("/") or "#" in route:
                raise ValueError()
        # The original query-or-fragment fallback silently discards one source.
        if parts.query and parts.fragment:
            raise ValueError()
        query = parts.query or fragment_query
        if not query or re.search(r"%(?![0-9a-fA-F]{2})", query) or "#" in query:
            raise ValueError()
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=3,
        )
        params = {}
        for key, item in pairs:
            if key in params or key not in {"code", "name", "title"}:
                raise ValueError()
            if (
                not item
                or len(item) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in item)
            ):
                raise ValueError()
            params[key] = item
        code = params.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", code):
            raise ValueError()
        # Display labels are never forwarded into an exchange form.
        return SilverLinkDraft(code)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(
            "银卡链接不符合当前离线参数规则；未访问链接、未验证权益有效性。"
        ) from None


@dataclass(frozen=True)
class SilverRequestDraft:
    path: str
    fields: tuple[tuple[str, str], ...] = field(repr=False)
    executable: bool = field(default=False, init=False)

    def form(self):
        """Return a fresh, sensitive in-memory form, never a log/export payload."""
        return dict(self.fields)


def _base(encrypted_phone):
    if (
        not isinstance(encrypted_phone, str)
        or not re.fullmatch(r"[A-Za-z0-9+/=_-]{16,4096}", encrypted_phone)
        or encrypted_phone.isdecimal()
    ):
        raise ValueError(
            "需要可信来源的手机号加密值；不支持明文手机号或外部HTTP加密服务。"
        )
    return {
        "channelId": "3000001",
        "activitySource": "",
        "activeId": "",
        "platType": "5",
        "r": "0.4644509997106073",
        "token": "",
        "phoneNum": encrypted_phone,
        "type": "18",
        "appVer": "4.1.3",
    }


def build_sms_draft(encrypted_phone):
    return SilverRequestDraft(SMS_PATH, tuple(_base(encrypted_phone).items()))


def build_exchange_draft(encrypted_phone, verify_code, business_params):
    data = _base(encrypted_phone)
    if not isinstance(verify_code, str) or not re.fullmatch(r"[0-9]{4,8}", verify_code):
        raise ValueError("验证码格式不符合当前离线校验规则。")
    # `code` is evidenced by the recovered link parser. It is only a minimal
    # offline subset, not a claim that other production parameters are optional.
    if (
        not isinstance(business_params, dict)
        or any(key in PROTECTED_FIELDS for key in business_params)
        or set(business_params) != {"code"}
        or not isinstance(business_params["code"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", business_params["code"])
    ):
        raise ValueError("链接参数不符合已恢复的最小契约；禁止覆盖系统字段。")
    data.update(
        r="0.3727106604045911",
        verifyCode=verify_code,
        taskCode="magic_invite",
        code=business_params["code"],
    )
    return SilverRequestDraft(EXCHANGE_PATH, tuple(data.items()))


def classify_redemption_response(payload):
    """No recovered response proves consumption or membership activation.

    Even an explicit remote error may follow a committed write. Until a trusted
    receipt/reconciliation contract exists, every submitted attempt stays held.
    """
    return "unknown"

"""Existing atourhotel queries and explicitly authorized manual SMS login."""

import json
import re
import time
from dataclasses import dataclass, field
from hashlib import sha256
from xml.etree import ElementTree

import requests

from skyagent_manager.sync import (
    MAX_RESPONSE_SIZE,
    _reject_response_constant,
    _unique_response_object,
    _validate_response_depth,
    validate_config,
)


class BackendQueryError(ValueError):
    pass


class BackendCaptchaRequired(BackendQueryError):
    def __init__(self, identifier):
        super().__init__("需要人工验证码，请输入后再次确认登录。")
        self.identifier = identifier


def validate_captcha_svg(payload):
    """Accept only the primitive SVG elements emitted by the existing backend."""
    if len(payload) > 128 * 1024 or b"<!" in payload:
        raise BackendQueryError("验证码图片格式无效。")
    try:
        root = ElementTree.fromstring(payload)
        nodes = list(root.iter())
        pending = [(root, 0)]
        while pending:
            node, depth = pending.pop()
            if depth > 32:
                raise ValueError()
            pending.extend((child, depth + 1) for child in node)
        if root.tag != "{http://www.w3.org/2000/svg}svg" or len(nodes) > 2000:
            raise ValueError()
        allowed = {"svg", "g", "rect", "line", "circle"}
        attributes = {
            "width",
            "height",
            "viewBox",
            "x",
            "y",
            "x1",
            "x2",
            "y1",
            "y2",
            "cx",
            "cy",
            "r",
            "rx",
            "fill",
            "stroke",
            "stroke-width",
            "opacity",
            "transform",
            "key",
        }
        for node in nodes:
            if node.tag not in {
                f"{{http://www.w3.org/2000/svg}}{name}" for name in allowed
            }:
                raise ValueError()
            for key, value in node.attrib.items():
                if (
                    key not in attributes
                    or len(value) > 128
                    or "url" in value.lower()
                    or ":" in value
                    or "&" in value
                ):
                    raise ValueError()
        return ElementTree.tostring(root)
    except (ElementTree.ParseError, ValueError):
        raise BackendQueryError("验证码图片格式无效，不加载外部资源。") from None


@dataclass(frozen=True)
class RemoteAccount:
    identifier: str
    phone: str = field(repr=False)


@dataclass(frozen=True)
class RemotePage:
    items: tuple[RemoteAccount, ...]
    page: int
    total: int
    has_more: bool


@dataclass(frozen=True)
class RemoteAccountDetail(RemoteAccount):
    enabled: bool
    online: bool
    new_user: bool
    breakfast: int
    room_upgrade: int
    late_checkout: int
    discount_assets: tuple["RemoteDiscountAsset", ...] | None = None


@dataclass(frozen=True)
class RemoteDiscountAsset:
    masked_code: str = field(repr=False)
    description: str
    value: str
    expiry: str
    expiry_tip: str
    state: str


@dataclass(frozen=True)
class SmsLoginResult:
    token: str = field(repr=False)
    platinum_hint: bool
    new_user_hint: bool


@dataclass(frozen=True)
class RemoteAccountCredential:
    identifier: str
    phone: str = field(repr=False)
    token: str = field(repr=False)


@dataclass(frozen=True)
class RemoteOrder:
    folio_id: str = field(repr=False)
    chain_id: str = field(repr=False)
    hotel: str
    start: str
    end: str
    state: str


@dataclass(frozen=True)
class RemoteOrderPage:
    account_id: str
    items: tuple[RemoteOrder, ...]
    page: int
    total: int
    has_more: bool


@dataclass(frozen=True)
class RemoteOrderDetail:
    account_id: str
    folio_id: str = field(repr=False)
    chain_id: str = field(repr=False)
    payment_state: int | None
    order_state: int | None
    state_text: str


@dataclass(frozen=True)
class RemoteGiftCode:
    masked_code: str = field(repr=False)
    source: str
    status: str
    received: int
    maximum: int
    identifier: str = field(default="", repr=False)


@dataclass(frozen=True)
class RemoteGiftPage:
    items: tuple[RemoteGiftCode, ...]
    page: int
    total: int
    has_more: bool


@dataclass(frozen=True)
class RemoteGiftReceiveLog:
    masked_phone: str = field(repr=False)
    created_at: str


@dataclass(frozen=True)
class RemoteGiftReceiveLogs:
    identifier: str = field(repr=False)
    items: tuple[RemoteGiftReceiveLog, ...]


@dataclass(frozen=True)
class RemoteExchangeTask:
    identifier: str = field(repr=False)
    state: str
    progress: int


def project_discount_assets(data):
    """Project stored snapshots only, never infer shareability or keep raw codes."""
    from skyagent_manager.db import mask_code

    if "discount_coupon_assets" not in data:
        return None
    assets = data["discount_coupon_assets"]
    if not isinstance(assets, list) or len(assets) > 500:
        raise BackendQueryError("折扣券资产列表无效或超过 500 条。")
    declared = data.get("discount_coupon_assets_count", len(assets))
    if type(declared) is not int or declared != len(assets):
        raise BackendQueryError("折扣券资产数量不一致。")
    result, codes = [], set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise BackendQueryError("折扣券资产结构无效。")
        code = asset.get("code")
        values = [
            asset.get(key, "")
            for key in (
                "couponDesc",
                "valueDesc",
                "expiryStr",
                "expiryTip",
                "couponState",
            )
        ]
        if (
            not isinstance(code, str)
            or not code.strip()
            or len(code) > 4096
            or any(ord(char) < 32 for char in code)
            or code in codes
            or any(
                not isinstance(value, str)
                or len(value) > 512
                or any(ord(char) < 32 for char in value)
                for value in values
            )
        ):
            raise BackendQueryError("折扣券资产字段或身份无效。")
        codes.add(code)
        result.append(RemoteDiscountAsset(mask_code(code), *values))
    return tuple(result)


class BackendQueryClient:
    def __init__(self, base_url, *, allow_http=False, session=None):
        self.base_url = validate_config(base_url, "", "", allow_http)["api_url"]
        if not self.base_url:
            raise BackendQueryError("请配置后端基础地址。")
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._token = ""
        self._sms_cooldowns = {}

    def _sms_phone(self, phone, authorized):
        if authorized is not True:
            raise BackendQueryError("请先明确确认已获授权操作此号码。")
        if not self._token:
            raise BackendQueryError("请先登录后台。")
        if not isinstance(phone, str) or not re.fullmatch(r"1[0-9]{10}", phone):
            raise BackendQueryError("短信登录手机号格式无效。")
        return sha256(phone.encode()).hexdigest()

    def send_account_login_code(self, phone, *, authorized=False):
        key = self._sms_phone(phone, authorized)
        now = time.monotonic()
        if self._sms_cooldowns.get(key, 0) > now:
            raise BackendQueryError("此号码发码仍在冷却中；未知结果也不自动重发。")
        # Reserve before sending: timeout does not prove no SMS was sent.
        self._sms_cooldowns[key] = now + 60
        try:
            data = self._request(
                "POST", "/api/pool/accounts/login/send-code", json={"phone": phone}
            )
        except Exception:
            raise BackendQueryError(
                "发码请求未确认成功；可能已发送，不自动重试。"
            ) from None
        cooldown = data.get("cooldownMs")
        if (
            data.get("ok") is not True
            or type(cooldown) is not int
            or not 1000 <= cooldown <= 600000
        ):
            raise BackendQueryError("发码结果无效；可能已发送，请等待且勿自动重试。")
        self._sms_cooldowns[key] = time.monotonic() + max(60, cooldown / 1000)
        return cooldown

    def account_login_by_code(self, phone, verify_code, *, authorized=False):
        self._sms_phone(phone, authorized)
        if not isinstance(verify_code, str) or not re.fullmatch(
            r"[A-Za-z0-9]{1,32}", verify_code
        ):
            raise BackendQueryError("请输入人工验证码，不允许空白或超过 32 字符。")
        try:
            data = self._request(
                "POST",
                "/api/pool/accounts/login/token",
                json={"phone": phone, "verifyCode": verify_code},
            )
        except Exception:
            raise BackendQueryError(
                "短信登录请求失败或结果未知；不自动重试。"
            ) from None
        token = data.get("token")
        flags = (data.get("isPlatinum"), data.get("isNewUser"))
        if (
            data.get("ok") is not True
            or not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9._+/=\-]{16,4096}", token)
            or any(type(flag) is not bool for flag in flags)
        ):
            raise BackendQueryError("短信登录结果无效，未保存 Token；不自动重试。")
        # Hints are not the backend's first-night/new-user eligibility evidence.
        return SmsLoginResult(token, *flags)

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            with self.session.request(
                method,
                self.base_url + path,
                headers=headers,
                timeout=(10, 30),
                verify=True,
                allow_redirects=False,
                stream=True,
                **kwargs,
            ) as response:
                if response.status_code not in {200, 401, 403}:
                    raise BackendQueryError(
                        "后端请求未成功；不会跟随重定向或自动重试。"
                    )
                if response.status_code in {401, 403}:
                    self._token = ""
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    if len(payload) + len(chunk) > MAX_RESPONSE_SIZE:
                        raise ValueError()
                    payload.extend(chunk)
                data = json.loads(
                    payload,
                    object_pairs_hook=_unique_response_object,
                    parse_constant=_reject_response_constant,
                )
                _validate_response_depth(data)
                if not isinstance(data, dict):
                    raise ValueError()
                if response.status_code != 200:
                    challenge = data.get("captcha")
                    identifier = (
                        challenge.get("id") if isinstance(challenge, dict) else None
                    )
                    if (
                        path == "/api/auth/login"
                        and data.get("captchaRequired") is True
                        and isinstance(identifier, str)
                        and re.fullmatch(r"[a-fA-F0-9-]{36}", identifier)
                    ):
                        raise BackendCaptchaRequired(identifier)
                    if response.status_code in {401, 403}:
                        self._token = ""
                        raise BackendQueryError(
                            "鉴权失败或需要人工验证码，请在后台确认登录权限；不会自动重试。"
                        )
                    raise BackendQueryError(
                        "后端请求未成功；不会跟随重定向或自动重试。"
                    )
                return data
        except BackendQueryError:
            raise
        except (requests.RequestException, ValueError, RecursionError):
            raise BackendQueryError(
                "网络或响应格式异常；未保存敏感响应，不自动重试。"
            ) from None

    def captcha_image(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[a-fA-F0-9-]{36}", identifier
        ):
            raise BackendQueryError("验证码标识无效。")
        try:
            with self.session.request(
                "GET",
                self.base_url + f"/api/auth/login-captcha/{identifier}/image",
                timeout=(10, 30),
                verify=True,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise BackendQueryError("验证码已过期或加载失败，请重新手动登录。")
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    if len(payload) + len(chunk) > 128 * 1024:
                        raise BackendQueryError("验证码图片超过限制。")
                    payload.extend(chunk)
                return validate_captcha_svg(bytes(payload))
        except requests.RequestException:
            raise BackendQueryError("验证码加载失败，不自动重试。") from None

    def login(self, username, password, captcha_id="", captcha_answer=""):
        self._token = ""
        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username) > 256
            or not isinstance(password, str)
            or not password
            or len(password) > 4096
        ):
            raise BackendQueryError("登录字段无效。")
        fields = {"username": username, "password": password}
        if captcha_id:
            if (
                not isinstance(captcha_id, str)
                or not re.fullmatch(r"[a-fA-F0-9-]{36}", captcha_id)
                or not isinstance(captcha_answer, str)
                or not re.fullmatch(r"[A-Za-z0-9]{5}", captcha_answer.strip())
            ):
                raise BackendQueryError("请手动输入五位验证码。")
            fields.update(captchaId=captcha_id, captchaAnswer=captcha_answer.strip())
        data = self._request("POST", "/api/auth/login", json=fields)
        token = data.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,4096}", token):
            raise BackendQueryError("登录响应无有效会话凭据。")
        self._token = token

    def accounts_page(self, page=1, page_size=20, *, search=""):
        if not self._token:
            raise BackendQueryError("请先登录后台；导入 Secret 不能代替 Bearer 会话。")
        if (
            type(page) is not int
            or not 1 <= page <= 10000
            or type(page_size) is not int
            or not 1 <= page_size <= 200
        ):
            raise BackendQueryError("分页参数无效。")
        if (
            not isinstance(search, str)
            or len(search) > 256
            or any(ord(char) < 32 for char in search)
        ):
            raise BackendQueryError("搜索条件无效。")
        params = {"page": page, "pageSize": page_size}
        if search.strip():
            params["search"] = search.strip()
        data = self._request("GET", "/api/pool/accounts", params=params)
        rows, meta = data.get("items"), data.get("meta")
        if (
            not isinstance(rows, list)
            or len(rows) > page_size
            or not isinstance(meta, dict)
        ):
            raise BackendQueryError("账号分页响应无效。")
        total = meta.get("total")
        if (
            type(meta.get("page")) is not int
            or meta["page"] != page
            or type(total) is not int
            or total < len(rows)
            or type(meta.get("hasMore")) is not bool
        ):
            raise BackendQueryError("账号分页元数据无效或页码已变化，请重新查询。")
        items, identifiers = [], set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["id"])
                or row["id"] in identifiers
                or not isinstance(row.get("phone"), str)
                or not re.fullmatch(r"[0-9+ ()-]{1,32}", row["phone"])
            ):
                raise BackendQueryError("账号字段或身份无效。")
            identifiers.add(row["id"])
            items.append(RemoteAccount(row["id"], row["phone"]))
        if meta["hasMore"] and not items:
            raise BackendQueryError("空分页不能继续。")
        return RemotePage(tuple(items), page, total, meta["hasMore"])

    def account_detail(self, identifier):
        if not self._token:
            raise BackendQueryError("请先登录后台。")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identifier
        ):
            raise BackendQueryError("后端账号 ID 无效。")
        data = self._request("GET", "/api/pool/accounts/" + identifier)
        flags = [data.get(key) for key in ("is_enabled", "is_online", "is_new_user")]
        counts = [
            data.get(key)
            for key in (
                "breakfast_coupons",
                "room_upgrade_coupons",
                "late_checkout_coupons",
            )
        ]
        phone = data.get("phone")
        if (
            data.get("id") != identifier
            or not isinstance(phone, str)
            or not re.fullmatch(r"[0-9+ ()-]{1,32}", phone)
            or any(type(value) is not bool for value in flags)
            or any(
                type(value) is not int or not 0 <= value <= 1000000 for value in counts
            )
        ):
            raise BackendQueryError("账号详情身份或汇总字段无效。")
        return RemoteAccountDetail(
            identifier, phone, *flags, *counts, project_discount_assets(data)
        )

    def account_credential(self, identifier, *, authorized=False, cancelled=None):
        """Explicit ADMIN credential retrieval; never infer eligibility or validity."""
        from skyagent_manager.accounts import AccountInput

        if authorized is not True or not self._token:
            raise BackendQueryError("请先登录并确认管理员账号Token读取。")

        def check_cancelled():
            if cancelled is not None and cancelled():
                raise BackendQueryError("账号Token读取已取消，未保存结果。")

        check_cancelled()
        before = self.account_detail(identifier)
        check_cancelled()
        data = self._request("GET", f"/api/pool/accounts/{identifier}/token")
        check_cancelled()
        token = data.get("token")
        if not isinstance(token, str):
            raise BackendQueryError("后台账号Token字段无效。")
        try:
            phone, normalized, token, *_ = AccountInput(before.phone, token).validated()
        except ValueError:
            raise BackendQueryError("后台账号凭据格式无效。") from None
        after = self.account_detail(identifier)
        check_cancelled()
        from skyagent_manager.db import normalize_phone

        if normalize_phone(after.phone) != normalized:
            raise BackendQueryError("后台账号手机号已变化，请重新查询；结果丢弃。")
        return RemoteAccountCredential(identifier, phone, token)

    def close(self):
        self._token = ""
        self._sms_cooldowns.clear()
        self.session.close()

    def account_order_detail(self, identifier, folio_id, chain_id, *, authorized=False):
        if authorized is not True or not self._token:
            raise BackendQueryError("请先登录并确认订单详情第三方查询。")
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
            for value in (identifier, folio_id, chain_id)
        ):
            raise BackendQueryError("订单详情上下文无效。")
        data = self._request(
            "GET",
            f"/api/pool/accounts/{identifier}/orders/{folio_id}/detail",
            params={"chainId": chain_id},
        )
        result = data.get("result")
        if not isinstance(result, dict):
            raise BackendQueryError("订单详情结构无效。")
        ids = (result.get("folioId"), result.get("chainId"))
        if any(type(value) not in {int, str} for value in ids) or tuple(
            map(str, ids)
        ) != (folio_id, chain_id):
            raise BackendQueryError("订单详情身份不匹配，结果已拒绝。")
        flags = (result.get("payState"), result.get("orderState"))
        text = result.get("orderStateName", "")
        if (
            any(
                value is not None and (type(value) is not int or not 0 <= value <= 100)
                for value in flags
            )
            or not isinstance(text, str)
            or len(text) > 256
            or any(ord(char) < 32 for char in text)
        ):
            raise BackendQueryError("订单详情状态字段无效。")
        return RemoteOrderDetail(identifier, folio_id, chain_id, *flags, text)

    def exchange_task(self, identifier, *, authorized=False):
        """Read an existing scoped exchange task, not generic unscoped history."""
        if authorized is not True or not self._token:
            raise BackendQueryError("请先登录并确认查询已有签到券兑换任务。")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identifier
        ):
            raise BackendQueryError("兑换任务 ID 无效。")
        data = self._request("GET", f"/api/pool/coupon-exchange-tasks/{identifier}")
        item = data.get("item")
        if not isinstance(item, dict) or item.get("id") != identifier:
            raise BackendQueryError("兑换任务身份不匹配，结果已拒绝。")
        state, progress = item.get("state"), item.get("progress")
        if (
            not isinstance(state, str)
            or state not in {"waiting", "active", "completed", "failed"}
            or type(progress) is not int
            or not 0 <= progress <= 100
        ):
            raise BackendQueryError("兑换任务状态或进度无效。")
        return RemoteExchangeTask(identifier, state, progress)

    def gift_codes(self, page=1, *, authorized=False, keyword="", status="", source=""):
        """Read ADMIN global stock; never retain usable share codes or infer eligibility."""
        from skyagent_manager.db import mask_code

        if authorized is not True or not self._token:
            raise BackendQueryError("请先登录并确认查询管理员全局礼包库。")
        if type(page) is not int or not 1 <= page <= 10000:
            raise BackendQueryError("礼包库页码无效。")
        if (
            not isinstance(keyword, str)
            or len(keyword) > 128
            or any(ord(char) < 32 for char in keyword)
            or not isinstance(status, str)
            or status not in {"", "ACTIVE", "EXHAUSTED", "INVALID"}
            or not isinstance(source, str)
            or source not in {"", "SCAN", "IMPORT"}
        ):
            raise BackendQueryError("礼包筛选条件无效。")
        params = {"page": page, "pageSize": 20}
        for key, value in (
            ("keyword", keyword.strip()),
            ("status", status),
            ("source", source),
        ):
            if value:
                params[key] = value
        data = self._request("GET", "/api/gift-codes", params=params)
        rows, total = data.get("items"), data.get("total")
        if (
            not isinstance(rows, list)
            or len(rows) > 20
            or type(data.get("page")) is not int
            or data["page"] != page
            or type(data.get("pageSize")) is not int
            or data["pageSize"] != 20
            or type(total) is not int
            or not len(rows) <= total <= 1000000
        ):
            raise BackendQueryError("礼包库分页结构无效。")
        items, identities = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise BackendQueryError("礼包库记录无效。")
            identifier, code = row.get("id"), row.get("shareCode")
            received, maximum = row.get("receivedCount"), row.get("maxReceive")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
                or identifier in identities
                or not isinstance(code, str)
                or not 1 <= len(code) <= 2048
                or any(ord(char) < 33 or ord(char) == 127 for char in code)
                or not isinstance(row.get("source"), str)
                or row["source"] not in {"SCAN", "IMPORT"}
                or not isinstance(row.get("status"), str)
                or row["status"] not in {"ACTIVE", "EXHAUSTED", "INVALID"}
                or type(received) is not int
                or type(maximum) is not int
                or not 0 <= received <= 1000000
                or not 1 <= maximum <= 1000000
            ):
                raise BackendQueryError("礼包库身份、状态或计数字段无效。")
            identities.add(identifier)
            if (status and row["status"] != status) or (
                source and row["source"] != source
            ):
                raise BackendQueryError("礼包返回状态或来源与筛选不一致。")
            items.append(
                RemoteGiftCode(
                    mask_code(code),
                    row["source"],
                    row["status"],
                    received,
                    maximum,
                    identifier,
                )
            )
        return RemoteGiftPage(tuple(items), page, total, page * 20 < total)

    def gift_receive_logs(self, identifier, *, authorized=False):
        """ADMIN database records only; content and full recipient identity discarded."""
        from datetime import datetime

        from skyagent_manager.db import mask_phone, normalize_phone

        if authorized is not True or not self._token:
            raise BackendQueryError("请先登录并确认管理员礼包记录查询。")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identifier
        ):
            raise BackendQueryError("礼包记录身份无效。")
        data = self._request("GET", f"/api/gift-codes/{identifier}/logs")
        rows = data.get("items")
        if not isinstance(rows, list) or len(rows) > 500:
            raise BackendQueryError(
                "礼包记录结构无效或超过500条；接口不支持分页，未展示截断结果。"
            )
        items, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise BackendQueryError("礼包记录结构无效。")
            log_id = row.get("id")
            if (
                not isinstance(log_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", log_id)
                or log_id in seen
                or row.get("codeId") != identifier
            ):
                raise BackendQueryError("礼包记录身份重复或不匹配，结果已拒绝。")
            phone, created = row.get("accountPhone"), row.get("createdAt")
            try:
                if phone is not None:
                    if not isinstance(phone, str) or len(phone) > 64:
                        raise ValueError()
                    normalize_phone(phone)
                if created is not None:
                    if (
                        not isinstance(created, str)
                        or len(created) > 64
                        or any(ord(char) < 32 for char in created)
                    ):
                        raise ValueError()
                    parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError()
            except (ValueError, TypeError):
                raise BackendQueryError("礼包记录手机号或时间字段无效。") from None
            seen.add(log_id)
            items.append(
                RemoteGiftReceiveLog(
                    mask_phone(phone) if phone else "未知", created or "未知"
                )
            )
        return RemoteGiftReceiveLogs(identifier, tuple(items))

    def account_orders(self, identifier, page=1, *, authorized=False):
        if authorized is not True or not self._token:
            raise BackendQueryError("请先登录并确认订单查询可能触发第三方请求。")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identifier
        ):
            raise BackendQueryError("后端账号 ID 无效。")
        if type(page) is not int or not 1 <= page <= 10000:
            raise BackendQueryError("订单页码无效。")
        data = self._request(
            "GET",
            f"/api/pool/accounts/{identifier}/orders",
            params={"pageNo": page, "pageSize": 10, "state": "0", "queryState": "1"},
        )
        meta, rows = data.get("page"), data.get("result")
        if not isinstance(meta, dict) or not isinstance(rows, list) or len(rows) > 10:
            raise BackendQueryError("订单分页结构无效。")
        total = meta.get("totalCount")
        if (
            type(meta.get("pageNo")) is not int
            or meta["pageNo"] != page
            or type(meta.get("pageSize")) is not int
            or meta["pageSize"] != 10
            or type(total) is not int
            or not len(rows) <= total <= 1000000
        ):
            raise BackendQueryError("订单分页字段无效。")
        items, identities = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise BackendQueryError("订单记录结构无效。")
            ids = (row.get("folioId", row.get("atourOrderId")), row.get("chainId"))
            if any(
                type(value) not in {int, str}
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(value))
                for value in ids
            ):
                raise BackendQueryError("订单身份字段无效。")
            identity = tuple(str(value) for value in ids)
            values = (
                row.get("hotelName") or row.get("chainName", ""),
                row.get("start", ""),
                row.get("end", ""),
                row.get("orderStateName", ""),
            )
            if identity in identities or any(
                not isinstance(value, str)
                or len(value) > 256
                or any(ord(char) < 32 for char in value)
                for value in values
            ):
                raise BackendQueryError("订单重复或展示字段无效。")
            identities.add(identity)
            items.append(RemoteOrder(*identity, *values))
        return RemoteOrderPage(identifier, tuple(items), page, total, page * 10 < total)

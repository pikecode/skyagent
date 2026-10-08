"""Account-owned benefit contracts and an offline synthetic adapter only."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import sha256
from typing import Protocol

from skyagent_manager.accounts import Accounts

KINDS = {
    "breakfast": "早餐券",
    "room_upgrade": "升房券",
    "delayed_checkout": "延迟券",
    "prop": "法宝",
    "gift": "订房礼包",
}

STATUSES = {
    "available": "可用",
    "used": "已用",
    "expired": "已过期",
    "unknown": "未知",
}


@dataclass(frozen=True)
class AccountBenefit:
    identifier: str = field(repr=False)
    source: str
    kind: str
    name: str
    code: str = field(repr=False)
    count: int
    expiry: str
    shareable: bool
    status: str = "available"
    dis_type: str = field(default="", repr=False)
    coupons_type: str = field(default="", repr=False)
    folio_id: str = field(default="", repr=False)
    chain_id: str = field(default="", repr=False)

    def validated(self):
        if self.source not in {"coupon", "prop", "order"} or self.kind not in KINDS:
            raise ValueError("权益类型或来源无效。")
        expected = (
            "prop"
            if self.kind == "prop"
            else "order"
            if self.kind == "gift"
            else "coupon"
        )
        if self.source != expected:
            raise ValueError("权益类型与来源不匹配。")
        for value in (self.identifier, self.name, self.code):
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                raise ValueError("权益字段格式无效。")
        if (
            type(self.count) is not int
            or not 0 <= self.count <= 1000000
            or type(self.shareable) is not bool
        ):
            raise ValueError("权益数量或分享标记无效。")
        if not isinstance(self.expiry, str):
            raise ValueError("权益有效期格式无效。")
        if not isinstance(self.status, str) or self.status not in STATUSES:
            raise ValueError("权益状态无效。")
        if not isinstance(self.dis_type, str) or not isinstance(self.coupons_type, str):
            raise ValueError("法宝分享上下文无效。")
        if self.dis_type or self.coupons_type:
            if self.source != "prop" or not self.prop_context_valid():
                raise ValueError("法宝分享上下文无效。")
        if not isinstance(self.folio_id, str) or not isinstance(self.chain_id, str):
            raise ValueError("订单分享上下文无效。")
        if self.folio_id or self.chain_id:
            if self.source != "order" or not self.gift_context_valid():
                raise ValueError("订单分享上下文无效。")
            if (
                self.identifier != f"{self.folio_id}:{self.chain_id}"
                or self.code != self.folio_id
            ):
                raise ValueError("订单分享身份不一致。")
        return self

    def gift_context_valid(self):
        return all(
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
            and value != "0"
            for value in (self.folio_id, self.chain_id)
        )

    def can_generate_gift(self):
        # This permits an explicit server-side qualification attempt, not claiming
        # eligibility, payment, availability or a made-up expiration date.
        return (
            self.source == "order"
            and self.kind == "gift"
            and self.gift_context_valid()
            and self.shareable
            and self.count == 1
        )

    def prop_context_valid(self):
        return bool(
            isinstance(self.dis_type, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.dis_type)
            and self.dis_type != "0"
            and isinstance(self.coupons_type, str)
            and re.fullmatch(r"[1-9][0-9]{0,5}", self.coupons_type)
        )

    def effective_status(self, today=None):
        """An explicit blocked state wins; never infer availability from a date."""
        if self.status != "available":
            return self.status
        try:
            expiry = date.fromisoformat(self.expiry)
        except (ValueError, TypeError):
            return "unknown"
        return "expired" if expiry < (today or date.today()) else "available"

    def can_share(self, today=None):
        try:
            expiry = date.fromisoformat(self.expiry)
        except ValueError:
            return False  # Unknown expiry must not silently authorize a write.
        return (
            self.effective_status(today) == "available"
            and self.shareable
            and self.count > 0
            and expiry >= (today or date.today())
        )


@dataclass(frozen=True)
class BenefitPage:
    items: tuple[AccountBenefit, ...]
    next_cursor: str | None = None


class BenefitQueryAdapter(Protocol):
    def fetch(self, token: str, cursor: str | None) -> BenefitPage: ...


class BenefitAdapter(BenefitQueryAdapter, Protocol):
    def share(self, token: str, benefit: AccountBenefit, request_id: str) -> str: ...


class SimulatedBenefitAdapter:
    """No sockets, HTTP or account inspection: data is always fabricated."""

    def fetch(self, token, cursor):
        if cursor not in {None, "second"}:
            raise ValueError("模拟分页游标无效。")
        expiry = (date.today() + timedelta(days=30)).isoformat()
        items = tuple(
            AccountBenefit(
                f"demo-{kind}",
                "prop" if kind == "prop" else "order" if kind == "gift" else "coupon",
                kind,
                f"模拟{label}",
                f"synthetic-{kind}-code",
                1,
                expiry,
                kind != "room_upgrade",
            )
            for kind, label in KINDS.items()
        )
        return (
            BenefitPage(items[:3], "second")
            if cursor is None
            else BenefitPage(items[3:])
        )

    def share(self, token, benefit, request_id):
        return f"https://example.invalid/simulated-share/{request_id}"


class AccountBenefitSession:
    """Session-only results bound to one store/account and token fingerprint.

    Synthetic outputs are never saved into real stock or benefit ledgers.
    """

    def __init__(self, database, adapter: BenefitQueryAdapter):
        self.db = database
        self.adapter = adapter
        self.clear()

    def clear(self):
        self.owner = None
        self.items = ()
        self.shares = {}

    def _identity(self, store_id, account_id):
        account = Accounts(self.db).get(store_id, account_id)
        return account["token"], (
            store_id,
            account_id,
            sha256(account["token"].encode()).hexdigest(),
        )

    def query(
        self, store_id, account_id, *, cancelled: Callable[[], bool] | None = None
    ):
        self.clear()
        token, owner = self._identity(store_id, account_id)
        items = self.fetch_items(self.adapter, token, cancelled=cancelled)
        self.publish(store_id, account_id, owner, items)
        return self.items

    @staticmethod
    def fetch_items(adapter, token, *, cancelled=None):
        """Fetch without database access; safe to run in a background worker."""
        items, cursors = {}, set()
        cursor = None
        try:
            for _ in range(100):
                if cancelled is not None and cancelled():
                    raise ValueError()
                fetch_cancelled = getattr(adapter, "fetch_with_cancel", None)
                page = (
                    fetch_cancelled(token, cursor, cancelled)
                    if callable(fetch_cancelled)
                    else adapter.fetch(token, cursor)
                )
                if cancelled is not None and cancelled():
                    raise ValueError()
                if not isinstance(page, BenefitPage) or len(page.items) > 2000:
                    raise ValueError()
                for item in page.items:
                    if not isinstance(item, AccountBenefit):
                        raise ValueError()
                    item.validated()
                    if "a.t." in item.name.casefold():
                        continue
                    key = (item.source, item.identifier)
                    if key in items and items[key] != item:
                        raise ValueError()
                    items[key] = item
                    if len(items) > 20000:
                        raise ValueError()
                cursor = page.next_cursor
                if cursor is None:
                    break
                if (
                    not isinstance(cursor, str)
                    or not cursor
                    or len(cursor) > 4096
                    or cursor in cursors
                ):
                    raise ValueError()
                cursors.add(cursor)
            else:
                raise ValueError()
        except Exception:
            raise ValueError(
                "权益查询失败或分页/数据无效；未保留部分结果，请重新查询。"
            ) from None
        return tuple(items.values())

    def publish(self, store_id, account_id, owner, items):
        """Publish on the database's owning thread after identity verification."""
        self.clear()
        if self._identity(store_id, account_id)[1] != owner:
            raise ValueError("账号、门店或 Token 已变化，查询结果已丢弃。")
        self.owner, self.items = owner, items

    def share(self, store_id, account_id, key):
        token, owner = self._identity(store_id, account_id)
        if owner != self.owner:
            self.clear()
            raise ValueError("账号、门店或 Token 已变化，请重新查询。")
        item = next(
            (item for item in self.items if (item.source, item.identifier) == key), None
        )
        if item is None or not item.can_share():
            raise ValueError("该权益不可分享、已过期、数量不足或有效期未知。")
        if not callable(getattr(self.adapter, "share", None)):
            raise ValueError("当前适配器仅支持只读查询，不能生成分享。")
        if key in self.shares:
            return self.shares[key]
        request_id = sha256(repr((owner, key)).encode()).hexdigest()
        try:
            result = self.adapter.share(token, item, request_id)
            # This implementation only permits synthetic, non-routable output.
            if (
                not isinstance(result, str)
                or not result.startswith("https://example.invalid/simulated-share/")
                or len(result) > 4096
            ):
                raise ValueError()
        except Exception:
            raise ValueError("模拟分享失败，结果未保存；真实接口尚未接入。") from None
        with self.db.connection:
            self.db._activity_in_transaction(
                store_id, "模拟权益分享", "仅生成虚构结果，未向第三方提交或写入库存"
            )
        self.shares[key] = result
        return result

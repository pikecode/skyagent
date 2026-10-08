"""Statically recovered order reads and explicit one-order gift generation."""

import json
import random
import re

import requests

from skyagent_manager.account_benefits import AccountBenefit, BenefitPage
from skyagent_manager.direct_benefits import text
from skyagent_manager.sync import (
    MAX_RESPONSE_SIZE,
    _reject_response_constant,
    _unique_response_object,
    _validate_response_depth,
)

ORIGIN = "https://api2.yaduo.com/atourlife"


def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(
        r"[A-Za-z0-9._+/=\-]{16,4096}", token
    ):
        raise ValueError("账号Token无效。")


def order_id(value):
    if type(value) is int and value > 0:
        value = str(value)
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
        or value == "0"
    ):
        raise ValueError("订单上下文无效。")
    return value


def validated_gift_code(value, token=""):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9._+-]{4,512}", value)
        or (token and token.casefold() in value.casefold())
    ):
        raise ValueError("礼包结果不是可确认的码。")
    return value


def request(session, path, data, *, headers=None):
    with session.request(
        "POST",
        ORIGIN + path,
        headers={
            "Accept": "application/json",
            "User-Agent": "SkyAgentManager/authorized-gift",
            **(headers or {}),
        },
        data=data,
        timeout=(10, 30),
        verify=True,
        allow_redirects=False,
        stream=True,
    ) as response:
        if response.status_code != 200:
            raise ValueError()
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if len(raw) + len(chunk) > MAX_RESPONSE_SIZE:
                raise ValueError()
            raw.extend(chunk)
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_response_object,
            parse_constant=_reject_response_constant,
        )
        _validate_response_depth(payload)
        if (
            not isinstance(payload, dict)
            or type(payload.get("retcode")) is not int
            or payload["retcode"] != 0
        ):
            raise ValueError()
        return payload.get("result")


class DirectGiftOrderQueryAdapter:
    direct = True

    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or requests.Session

    def fetch_with_cancel(self, token, cursor, cancelled):
        return self.fetch(token, cursor, cancelled=cancelled)

    def fetch(self, token, cursor, *, cancelled=None):
        session = None
        try:
            validate_token(token)
            if cursor is not None or (cancelled and cancelled()):
                raise ValueError()
            session = self.session_factory()
            session.trust_env = False
            session.cookies.clear()
            rows = request(
                session,
                "/order/getOrderList",
                {
                    "channelId": "3000001",
                    "activitySource": "",
                    "activeId": "",
                    "platType": "5",
                    "r": str(random.random()),
                    "token": token,
                    "state": "0",
                    "queryState": "1",
                    "pageNo": "1",
                    "pageSize": "20",
                    "appVer": "3.31.0",
                },
                headers={
                    "Origin": "https://wechat.yaduo.com",
                    "Referer": "https://wechat.yaduo.com/",
                },
            )
            if (
                (cancelled and cancelled())
                or not isinstance(rows, list)
                or len(rows) > 20
            ):
                raise ValueError()
            items = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError()
                folio, chain = (
                    order_id(row.get("folioId")),
                    order_id(row.get("chainId")),
                )
                hotel = text(row.get("chainName") or "订房订单", required=True)
                state = text(row.get("orderStateName") or "状态未知")
                if any(
                    secret in hotel or secret in state
                    for secret in (token, folio, chain)
                    if len(secret) >= 8
                ):
                    raise ValueError()
                items.append(
                    AccountBenefit(
                        f"{folio}:{chain}",
                        "order",
                        "gift",
                        f"待生成礼包订单：{hotel}（{state}）",
                        folio,
                        1,
                        "",
                        True,
                        "unknown",
                        folio_id=folio,
                        chain_id=chain,
                    ).validated()
                )
            return BenefitPage(tuple(items))
        except Exception:
            raise ValueError(
                "订单查询失败或契约不兼容；未保留部分结果，不展示敏感响应、不重试。"
            ) from None
        finally:
            if session is not None:
                session.cookies.clear()
                session.close()


class DirectGiftShareAdapter:
    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or requests.Session

    def share(self, token, item, *, cancelled=None):
        session = None
        try:
            validate_token(token)
            item.validated()
            if not item.can_generate_gift() or (cancelled and cancelled()):
                raise ValueError()
            session = self.session_factory()
            session.trust_env = False
            session.cookies.clear()
            if cancelled and cancelled():
                raise ValueError()
            result = request(
                session,
                "/fission/lucky/createShare",
                {
                    "At-App-Version": "4.8.1",
                    "At-Channel-Id": "20001",
                    "At-Platform-Type": "2",
                    "appVer": "4.8.1",
                    "chainId": item.chain_id,
                    "channelId": "20001",
                    "folioId": item.folio_id,
                    "platType": "2",
                    "token": token,
                },
                headers={
                    "At-App-Version": "4.8.1",
                    "At-Channel-Id": "20001",
                    "At-Platform-Type": "2",
                },
            )
            if (cancelled and cancelled()) or not isinstance(result, dict):
                raise ValueError()
            return validated_gift_code(result.get("shareCode"), token)
        except Exception:
            raise ValueError(
                "礼包生成未取得可确认结果；禁止重发，不展示敏感响应。"
            ) from None
        finally:
            if session is not None:
                session.cookies.clear()
                session.close()

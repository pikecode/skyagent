"""Authorized direct query protocol recovered statically; never claims benefits."""

import json
import re
from datetime import date
from uuid import uuid4

import requests

from skyagent_manager.account_benefits import AccountBenefit, BenefitPage
from skyagent_manager.sync import (
    MAX_RESPONSE_SIZE,
    _reject_response_constant,
    _unique_response_object,
    _validate_response_depth,
)

ORIGIN = "https://miniapp.yaduo.com/atourlife"
TITLES = (
    ("BREAKFAST_COUPON", "早餐券", "breakfast"),
    ("UP_COUPON", "升房券", "room_upgrade"),
    ("DELAY_COUPON", "延迟券", "delayed_checkout"),
)


def text(value, *, required=False, maximum=512):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
        or (required and not value.strip())
    ):
        raise ValueError("第三方权益字段无效。")
    return value.strip()


def expiry(value):
    value = text(value)
    if re.fullmatch(r"\d{4}[-/.]\d{2}[-/.]\d{2}", value):
        try:
            return date.fromisoformat(
                value.replace("/", "-").replace(".", "-")
            ).isoformat()
        except ValueError:
            pass
    return ""  # Unknown format never grants sharing permission.


class DirectBenefitQueryAdapter:
    direct = True

    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or requests.Session

    def fetch_with_cancel(self, token, cursor, cancelled):
        return self.fetch(token, cursor, cancelled=cancelled)

    def fetch(self, token, cursor, *, cancelled=None):
        if (
            cursor is not None
            or not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9._+/=\-]{16,4096}", token)
        ):
            raise ValueError("账号Token或查询游标无效。")
        session = self.session_factory()
        session.trust_env = False
        session.cookies.clear()
        params = {
            "appVer": "4.13.1",
            "version": "4.13.1",
            "channelId": "300001",
            "deviceId": str(uuid4()),
            "token": token,
            "clientId": "6",
            "platType": "5",
            "At-Platform-Type": "5",
        }
        items = []

        def check_cancel():
            if cancelled is not None and cancelled():
                raise ValueError("查询已取消。")

        try:
            for title, label, kind in TITLES:
                check_cancel()
                result = self.request(
                    session,
                    "POST",
                    "/coupon/memberCouponListOfType",
                    params=params,
                    json={
                        "titleCode": title,
                        "sortScene": "",
                        "stateCodes": ["AVAILABLE"],
                        "token": token,
                    },
                )
                rows = result.get("couponList")
                if not isinstance(rows, list) or len(rows) > 500:
                    raise ValueError("券列表缺失或超量，未保留部分结果。")
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("券记录无效。")
                    code = text(row.get("code"), required=True, maximum=4096)
                    name = text(row.get("couponDesc", label), required=True)
                    share = row.get("share")
                    if type(share) is not int or share not in {0, 1}:
                        share = 0
                    state = row.get("stateCode", row.get("couponState", "AVAILABLE"))
                    # Availability query is evidence of snapshot state, not validity.
                    status = (
                        {
                            "AVAILABLE": "available",
                            "USED": "used",
                            "EXPIRED": "expired",
                        }.get(state, "unknown")
                        if isinstance(state, str)
                        else "unknown"
                    )
                    items.append(
                        AccountBenefit(
                            code,
                            "coupon",
                            kind,
                            name,
                            code,
                            1,
                            expiry(row.get("expiryStr", "")),
                            share == 1,
                            status,
                        )
                    )
            prop_params = dict(
                params, appVer="4.9.0", version="4.9.0", effective="true", source=""
            )
            check_cancel()
            groups = self.request(
                session, "GET", "/propCard/classification/list", params=prop_params
            )
            check_cancel()
            for group in groups.values():
                if not isinstance(group, list):
                    raise ValueError("道具分组结构不支持，未保留部分结果。")
                for row in group:
                    if not isinstance(row, dict) or not isinstance(
                        row.get("memberCouponResponse"), dict
                    ):
                        raise ValueError("道具结构无效。")
                    coupon = row["memberCouponResponse"]
                    code = text(coupon.get("code"), required=True, maximum=4096)
                    count = row.get("availableCount")
                    if type(count) is not int or not 0 <= count <= 1000000:
                        raise ValueError("道具数量未知或无效。")
                    dis_type = coupon.get("disType")
                    coupons_type = coupon.get("couponsType")
                    if coupons_type is None:
                        coupons_type = "3"  # Explicit recovered protocol default.
                    dis_type = str(dis_type) if type(dis_type) is int else dis_type
                    coupons_type = (
                        str(coupons_type) if type(coupons_type) is int else coupons_type
                    )
                    context_valid = (
                        isinstance(dis_type, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dis_type)
                        and dis_type != "0"
                        and isinstance(coupons_type, str)
                        and re.fullmatch(r"[1-9][0-9]{0,5}", coupons_type)
                    )
                    recommendation_valid = False
                    if isinstance(coupon.get("recommend"), str):
                        from skyagent_manager.coupon_sharing import validated_share_url

                        try:
                            validated_share_url(coupon["recommend"], token)
                            recommendation_valid = True
                        except ValueError:
                            pass
                    shareable = bool(
                        context_valid
                        and recommendation_valid
                        and type(coupon.get("share")) is int
                        and coupon["share"] == 1
                        and count > 0
                    )
                    state_code = coupon.get(
                        "stateCode", coupon.get("couponState", "AVAILABLE")
                    )
                    prop_status = (
                        {
                            "AVAILABLE": "available",
                            "USED": "used",
                            "EXPIRED": "expired",
                        }.get(state_code, "unknown")
                        if isinstance(state_code, str)
                        else "unknown"
                    )
                    items.append(
                        AccountBenefit(
                            code,
                            "prop",
                            "prop",
                            text(
                                row.get(
                                    "propCardName", coupon.get("disTypeName", "法宝")
                                ),
                                required=True,
                            ),
                            code,
                            count,
                            expiry(coupon.get("expiryStr", row.get("endDate", ""))),
                            shareable,
                            prop_status,
                            dis_type if context_valid else "",
                            coupons_type if context_valid else "",
                        )
                    )  # Never keep/follow the original recommendation link.
                    if len(items) > 2000:
                        raise ValueError("权益总数超过2000条。")
            return BenefitPage(tuple(items))
        except Exception:
            raise ValueError(
                "第三方权益查询失败或契约不兼容，结果全部丢弃；不展示响应、不自动重试。"
            ) from None
        finally:
            session.cookies.clear()
            session.close()

    @staticmethod
    def request(
        session,
        method,
        path,
        *,
        user_agent="SkyAgentManager/authorized-query",
        **kwargs,
    ):
        with session.request(
            method,
            ORIGIN + path,
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
            timeout=(10, 30),
            verify=True,
            allow_redirects=False,
            stream=True,
            **kwargs,
        ) as response:
            if response.status_code != 200:
                raise ValueError("第三方请求未成功。")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(raw) + len(chunk) > MAX_RESPONSE_SIZE:
                    raise ValueError("第三方响应超量。")
                raw.extend(chunk)
            data = json.loads(
                raw,
                object_pairs_hook=_unique_response_object,
                parse_constant=_reject_response_constant,
            )
            _validate_response_depth(data)
            if (
                not isinstance(data, dict)
                or type(data.get("retcode")) is not int
                or data["retcode"] != 0
                or not isinstance(data.get("result"), dict)
            ):
                raise ValueError("第三方响应结构无效。")
            return data["result"]

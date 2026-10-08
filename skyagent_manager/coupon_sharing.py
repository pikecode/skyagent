"""Single coupon sharing protocol and encrypted output; no automatic upload."""

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import requests

from skyagent_manager.account_benefits import AccountBenefitSession
from skyagent_manager.db import utc_now
from skyagent_manager.direct_benefits import DirectBenefitQueryAdapter
from skyagent_manager.security import atomic_write
from skyagent_manager.share_journal import ShareJournal
from skyagent_manager.share_safety import require_share_writes_allowed
from skyagent_manager.sync import _unique_response_object

COUPON_KINDS = {"breakfast", "room_upgrade", "delayed_checkout"}
SHARE_KINDS = COUPON_KINDS | {"prop"}


def validated_share_url(value, token=""):
    try:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 or char == "\\" for char in value)
        ):
            raise ValueError()
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or not (host == "yaduo.com" or host.endswith(".yaduo.com"))
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
            or any(
                not label or label.startswith("-") or label.endswith("-")
                for label in host.split(".")
            )
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
            or not (parts.path.strip("/") or parts.query or parts.fragment)
        ):
            raise ValueError()
        decoded = value
        for _ in range(16):
            if (token and token in decoded) or re.search(
                r"(?:[?&#=]|^)(?:token|access_token|authorization|secret|password)=",
                decoded,
                re.I,
            ):
                raise ValueError()
            if any(ord(char) < 32 or char == "\\" for char in decoded):
                raise ValueError()
            expanded = unquote(decoded)
            if expanded == decoded:
                break
            decoded = expanded
        else:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ValueError(
            "分享链接格式或域名不支持，结果需人工核实，禁止重发。"
        ) from None


class DirectCouponShareAdapter:
    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or requests.Session

    def share(self, token, item, *, cancelled=None):
        session = None
        try:
            item.validated()
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9._+/=\-]{16,4096}", token)
                or item.source != "coupon"
                or item.kind not in COUPON_KINDS
                or not item.can_share()
                or (cancelled and cancelled())
            ):
                raise ValueError()
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
                "platType": "2",
                "At-Platform-Type": "2",
                "inactiveId": "",
                "elementId": "0",
                "traceId": "0",
                "activeId": "0",
                "activityId": "0",
                "activitySource": "",
                "osversion": "iOS",
                "devbrand": "iPhone",
                "devmodel": "iPhone",
            }
            if cancelled and cancelled():
                raise ValueError()
            result = DirectBenefitQueryAdapter.request(
                session,
                "POST",
                "/user/share/generateShareInfo",
                user_agent="SkyAgentManager/authorized-coupon-share",
                params=params,
                json={
                    "shareBizType": "COUPON",
                    "shareBizKey": item.code,
                    "token": token,
                },
            )
            if cancelled and cancelled():
                raise ValueError()
            return validated_share_url(result.get("shareUrl"), token)
        except Exception:
            raise ValueError(
                "分享未取得可确认结果；已保留防重记录，不展示敏感响应、不自动重试。"
            ) from None
        finally:
            if session is not None:
                session.cookies.clear()
                session.close()


class DirectPropShareAdapter:
    """Recovered GET may create/share a code; never auto-retry it."""

    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or requests.Session

    def share(self, token, item, *, cancelled=None):
        session = None
        try:
            item.validated()
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9._+/=\-]{16,4096}", token)
                or item.source != "prop"
                or item.kind != "prop"
                or not item.prop_context_valid()
                or not item.can_share()
                or (cancelled and cancelled())
            ):
                raise ValueError()
            session = self.session_factory()
            session.trust_env = False
            session.cookies.clear()
            params = {
                "appVer": "4.9.0",
                "version": "4.9.0",
                "channelId": "300001",
                "deviceId": str(uuid4()),
                "token": token,
                "clientId": "6",
                "platType": "6",
                "At-Platform-Type": "5",
                "inactiveId": "",
                "elementId": "0",
                "traceId": "0",
                "activeId": "0",
                "activityId": "0",
                "activitySource": "",
                "disType": item.dis_type,
                "couponsType": item.coupons_type,
            }
            if cancelled and cancelled():
                raise ValueError()
            result = DirectBenefitQueryAdapter.request(
                session,
                "GET",
                "/coupon/share/queryShareCode",
                user_agent="SkyAgentManager/authorized-prop-share",
                params=params,
                data={"token": token},
            )
            if cancelled and cancelled():
                raise ValueError()
            recommend, share_url = result.get("recommend"), result.get("shareUrl")
            if recommend and share_url and recommend != share_url:
                raise ValueError()
            return validated_share_url(recommend or share_url, token)
        except Exception:
            raise ValueError(
                "法宝分享未取得可确认结果；禁止重发，不展示敏感响应。"
            ) from None
        finally:
            if session is not None:
                session.cookies.clear()
                session.close()


@dataclass(frozen=True)
class SavedCouponShare:
    operation_id: str
    store_id: str
    account_id: str
    kind: str
    url: str = field(repr=False)
    stock_id: str = ""
    expiry: str = ""
    created_at: str = ""
    resource_keys: tuple[str, ...] = field(default=(), repr=False)


class CouponShareOutputs:
    PREFIX = "third_party_share/output/v1/"

    def __init__(self, database):
        self.db = database

    def save(self, store_id, account_id, owner, item, operation_id, url):
        token, current = AccountBenefitSession(self.db, None)._identity(
            store_id, account_id
        )
        item.validated()
        if (
            current != owner
            or item.source not in {"coupon", "prop"}
            or item.kind not in SHARE_KINDS
        ):
            raise ValueError("分享归属已变化，禁止保存成功结果。")
        require_share_writes_allowed(self.db)
        url = validated_share_url(url, token)
        journal = ShareJournal(self.db)
        # State transition and output land in the same encrypted transaction.
        with self.db.connection:
            record = journal.state(item)
            if (
                record is None
                or record["operation_id"] != operation_id
                or record["state"] != "pending"
            ):
                raise ValueError("分享操作状态不匹配，禁止保存。")
            keys = journal.record_keys(item, record)
            record["state"] = "confirmed"
            record["updated_at"] = utc_now()
            for key in keys:
                self.db.connection.execute(
                    "UPDATE settings SET value=? WHERE name=?",
                    (json.dumps(record), key),
                )
            self.db.connection.execute(
                "INSERT INTO settings(name,value) VALUES (?,?)",
                (
                    self.PREFIX + operation_id,
                    json.dumps(
                        {
                            "store_id": store_id,
                            "account_id": account_id,
                            "kind": item.kind,
                            "source": item.source,
                            "url": url,
                            "stock_id": "",
                            "expiry": item.expiry,
                            "created_at": record["updated_at"],
                            "resource_keys": list(keys),
                        }
                    ),
                ),
            )
            self.db._activity_in_transaction(
                store_id, "分享结果保存", "已加密保存已确认链接；未入库存、未上传"
            )

    def load(self, store_id, account_id, item):
        self.db._require_active_store(store_id)
        try:
            record = ShareJournal(self.db).state(item)
            if record is None or record["state"] != "confirmed":
                raise ValueError()
            saved = self.load_operation(store_id, account_id, record["operation_id"])
            if saved.kind != item.kind or item.source not in {"coupon", "prop"}:
                raise ValueError()
            return saved
        except Exception:
            raise ValueError(
                "当前权益没有归属匹配的已保存分享结果；不得重新提交以补结果。"
            ) from None

    def _payload(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", operation_id
        ):
            raise ValueError()
        raw = self.db.get_setting(self.PREFIX + operation_id)
        if not raw or len(raw) > 16384:
            raise ValueError()
        output = json.loads(raw, object_pairs_hook=_unique_response_object)
        if not isinstance(output, dict):
            raise ValueError()
        return output

    def load_operation(self, store_id, account_id, operation_id):
        """Local read independent of live queries; verify durable journal links."""
        self.db._require_active_store(store_id)
        try:
            output = self._payload(operation_id)
            if (
                output.get("store_id") != store_id
                or output.get("account_id") != account_id
                or output.get("kind") not in SHARE_KINDS
                or output.get("source", "coupon")
                != ("prop" if output.get("kind") == "prop" else "coupon")
                or not isinstance(output.get("stock_id"), str)
                or (
                    output["stock_id"]
                    and not re.fullmatch(r"[0-9a-f]{32}", output["stock_id"])
                )
            ):
                raise ValueError()
            keys = output.get("resource_keys")
            if keys is None:
                # 0.2.22 had no reverse index. Find only this operation's hashes;
                # never guess raw coupon identity or create another share.
                candidates = self.db.connection.execute(
                    "SELECT name,value FROM settings WHERE name GLOB ? AND instr(value,?)>0 LIMIT 5",
                    (ShareJournal.PREFIX + "*", operation_id),
                ).fetchall()
                keys = [row["name"] for row in candidates]
            if (
                not isinstance(keys, list)
                or not 1 <= len(keys) <= 4
                or len(set(keys)) != len(keys)
                or any(
                    not isinstance(key, str)
                    or not re.fullmatch(
                        re.escape(ShareJournal.PREFIX) + r"[0-9a-f]{64}", key
                    )
                    for key in keys
                )
            ):
                raise ValueError()
            records = []
            for key in keys:
                raw = self.db.get_setting(key)
                if len(raw) > 16384:
                    raise ValueError()
                record = json.loads(raw, object_pairs_hook=_unique_response_object)
                if (
                    not isinstance(record, dict)
                    or record.get("operation_id") != operation_id
                    or record.get("state") != "confirmed"
                ):
                    raise ValueError()
                records.append(record)
            if any(record != records[0] for record in records):
                raise ValueError()
            if "guard_keys" in records[0] and records[0]["guard_keys"] != keys:
                raise ValueError()
            expiry = output.get("expiry", "")
            if not isinstance(expiry, str) or (
                expiry and date.fromisoformat(expiry).isoformat() != expiry
            ):
                raise ValueError()
            created_at = output.get("created_at", records[0].get("updated_at", ""))
            if (
                not isinstance(created_at, str)
                or len(created_at) > 64
                or datetime.fromisoformat(created_at).utcoffset() is None
            ):
                raise ValueError()
            return SavedCouponShare(
                operation_id,
                store_id,
                account_id,
                output["kind"],
                validated_share_url(output.get("url")),
                output["stock_id"],
                expiry,
                created_at,
                tuple(keys),
            )
        except Exception:
            raise ValueError(
                "已保存分享记录缺失、归属或防重关联异常；未使用该结果，不得重新分享。"
            ) from None

    def list_saved(self, store_id, account_id):
        self.db._require_active_store(store_id)
        try:
            rows = self.db.connection.execute(
                "SELECT name,value FROM settings WHERE name GLOB ? ORDER BY name LIMIT 2001",
                (self.PREFIX + "*",),
            ).fetchall()
            if len(rows) > 2000:
                raise ValueError()
            results = []
            for row in rows:
                operation_id = row["name"][len(self.PREFIX) :]
                output = self._payload(operation_id)
                if (
                    output.get("store_id") == store_id
                    and output.get("account_id") == account_id
                ):
                    results.append(
                        self.load_operation(store_id, account_id, operation_id)
                    )
            return tuple(
                sorted(
                    results,
                    key=lambda saved: (saved.created_at, saved.operation_id),
                    reverse=True,
                )
            )
        except Exception:
            raise ValueError(
                "本地已保存分享记录异常或超过2000条，未显示部分列表。"
            ) from None

    def _insert_stock(self, saved):
        if saved.kind not in SHARE_KINDS:
            raise ValueError("分享资源类型无效。")
        if saved.stock_id:
            raise ValueError("该分享结果已处理入库存，禁止再次导入。")
        existing = self.db.connection.execute(
            "SELECT id FROM stock WHERE value=?", (saved.url,)
        ).fetchone()
        if existing:
            raise ValueError("该链接已存在库存，未重复导入。")
        stock_id = uuid4().hex
        self.db.connection.execute(
            "INSERT INTO stock VALUES(?,?,?,?,?)",
            (stock_id, saved.store_id, saved.kind, saved.url, "available"),
        )
        payload = self._payload(saved.operation_id)
        payload["stock_id"] = stock_id
        self.db.connection.execute(
            "UPDATE settings SET value=? WHERE name=?",
            (json.dumps(payload), self.PREFIX + saved.operation_id),
        )
        self.db._activity_in_transaction(
            saved.store_id,
            "分享入资源库存",
            "确认导入一条已保存分享链接；未上传后台，不推断第三方数量",
        )
        return stock_id

    def import_operation(
        self, store_id, account_id, owner, operation_id, *, expected=None
    ):
        if (
            AccountBenefitSession(self.db, None)._identity(store_id, account_id)[1]
            != owner
        ):
            raise ValueError("账号、门店或 Token 已变化，禁止入库存。")
        require_share_writes_allowed(self.db)
        with self.db.connection:
            saved = self.load_operation(store_id, account_id, operation_id)
            if expected is not None and saved != expected:
                raise ValueError("已保存结果已变化，请重新查看并确认。")
            try:
                if date.fromisoformat(saved.expiry) < date.today():
                    raise ValueError()
            except ValueError:
                raise ValueError(
                    "保存的有效期未知或已过期，不加入可用库存；旧版记录需另行核实。"
                ) from None
            return self._insert_stock(saved)

    def export_to(self, path, store_id, account_id, owner, saved_rows, *, full=False):
        """Explicit local CSV export; atomic file write, no database mutation."""
        try:
            if type(full) is not bool or not saved_rows or len(saved_rows) > 2000:
                raise ValueError()
            if full and len(saved_rows) != 1:
                raise ValueError()
            if (
                AccountBenefitSession(self.db, None)._identity(store_id, account_id)[1]
                != owner
            ):
                raise ValueError()
            if full:
                require_share_writes_allowed(self.db)
            checked = tuple(
                self.load_operation(store_id, account_id, row.operation_id)
                for row in saved_rows
            )
            if checked != tuple(saved_rows) or len(
                {row.operation_id for row in checked}
            ) != len(checked):
                raise ValueError()
            path = Path(path)
            if path.suffix.lower() != ".csv" or path.resolve().is_relative_to(
                self.db.path.parent.resolve()
            ):
                raise ValueError()
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "模式",
                    "类型",
                    "保存时间",
                    "原券有效期",
                    "库存处理",
                    "分享链接" if full else "链接",
                ]
            )
            for row in checked:
                writer.writerow(
                    [
                        "本地已确认分享，非领取凭证",
                        row.kind,
                        row.created_at,
                        row.expiry or "未知（旧版未保存）",
                        "已处理入库存" if row.stock_id else "未入库存",
                        row.url if full else "已隐藏",
                    ]
                )
            atomic_write(path, stream.getvalue().encode("utf-8-sig"))
        except Exception:
            raise ValueError(
                "导出未完成：归属或记录已变化、恢复阻断、目标位于应用数据目录或文件无法保存；未修改提交状态。"
            ) from None

    def import_stock(self, store_id, account_id, owner, item):
        if (
            AccountBenefitSession(self.db, None)._identity(store_id, account_id)[1]
            != owner
        ):
            raise ValueError("账号、门店或 Token 已变化，禁止入库存。")
        # Recovery may expose stale links as well as stale submission history.
        require_share_writes_allowed(self.db)
        try:
            if date.fromisoformat(item.expiry) < date.today():
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("有效期未知或已过期，不加入可用资源库存。") from None
        with self.db.connection:
            saved = self.load(store_id, account_id, item)
            if saved.expiry != item.expiry:
                raise ValueError("查询与保存的有效期不一致，未入库存。")
            return self._insert_stock(saved)

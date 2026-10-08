"""Durable, fail-closed reservations for future third-party share writes.

This module performs no network operations and stores no token or raw resource ID.
Reservations deliberately survive account/token/store changes. There is no retry
or release API: even a pending operation may already have reached the provider.
"""

import json
import re
from hashlib import sha256
from uuid import uuid4

from skyagent_manager.account_benefits import AccountBenefitSession
from skyagent_manager.accounts import Accounts
from skyagent_manager.db import utc_now
from skyagent_manager.share_safety import require_share_writes_allowed


class ShareJournal:
    PREFIX = "third_party_share/v1/"
    STATES = {"pending", "unknown", "confirmed"}

    def __init__(self, database):
        self.db = database

    @classmethod
    def resource_key(cls, item):
        item.validated()
        if item.source == "order" and item.gift_context_valid():
            return tuple(
                cls.PREFIX + sha256(json.dumps(identity).encode()).hexdigest()
                for identity in (
                    ["order-share", "folio", item.folio_id],
                    ["order-share", "context", item.folio_id, item.chain_id],
                )
            )
        # Both identities are held independently, preventing changed aliases or
        # store/account copies from silently releasing a consumed resource.
        return tuple(
            dict.fromkeys(
                cls.PREFIX
                + sha256(json.dumps([item.source, value]).encode()).hexdigest()
                for value in (item.identifier, item.code)
            )
        )

    def state(self, item):
        records = []
        keys = self.resource_key(item)
        for key in keys:
            row = self.db.connection.execute(
                "SELECT value FROM settings WHERE name=?", (key,)
            ).fetchone()
            if row is None:
                continue
            raw = row["value"]
            try:
                record = json.loads(raw)
                if (
                    not isinstance(record, dict)
                    or record.get("state") not in self.STATES
                    or not isinstance(record.get("operation_id"), str)
                    or not record["operation_id"]
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("分享保护记录异常，禁止提交。") from None
            records.append(record)
        if not records:
            return None
        if len(records) != len(keys) or any(record != records[0] for record in records):
            raise ValueError("分享保护记录不一致，禁止提交。")
        return dict(records[0])

    def _prop_context_keys(self, store_id, account_id, item):
        if not item.prop_context_valid():
            raise ValueError("法宝分享上下文缺失或无效，禁止提交。")
        account = Accounts(self.db).get(store_id, account_id)
        return tuple(
            self.PREFIX
            + sha256(
                json.dumps(
                    [
                        "prop-share-context",
                        identity,
                        value,
                        item.dis_type,
                        item.coupons_type,
                    ]
                ).encode()
            ).hexdigest()
            for identity, value in (
                ("phone", account["phone_norm"]),
                ("account", account_id),
            )
        )

    def state_for_owner(self, store_id, account_id, item):
        record = self.state(item)
        if item.source != "prop":
            return record
        try:
            rows = [
                self.db.connection.execute(
                    "SELECT value FROM settings WHERE name=?", (key,)
                ).fetchone()
                for key in self._prop_context_keys(store_id, account_id, item)
            ]
            if all(row is None for row in rows) and record is None:
                return None
            if any(row is None for row in rows):
                raise ValueError()
            context_records = [json.loads(row["value"]) for row in rows]
            context_record = context_records[0]
            if (
                not isinstance(context_record, dict)
                or context_record.get("state") not in self.STATES
                or not isinstance(context_record.get("operation_id"), str)
                or context_record != context_records[1]
            ):
                raise ValueError()
            if record is not None and context_record != record:
                raise ValueError()
            return context_record
        except Exception:
            raise ValueError("法宝上下文防重记录异常，禁止提交。") from None

    def record_keys(self, item, record):
        keys = record.get("guard_keys", list(self.resource_key(item)))
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= 4
            or len(set(keys)) != len(keys)
            or any(
                not isinstance(key, str)
                or not re.fullmatch(re.escape(self.PREFIX) + r"[0-9a-f]{64}", key)
                for key in keys
            )
            or not set(self.resource_key(item)).issubset(keys)
        ):
            raise ValueError("分享关联保护记录异常。")
        for key in keys:
            if json.loads(self.db.get_setting(key)) != record:
                raise ValueError("分享关联保护记录不一致。")
        return tuple(keys)

    def reserve(self, store_id, account_id, owner, item):
        require_share_writes_allowed(self.db)
        item.validated()
        if (
            AccountBenefitSession(self.db, None)._identity(store_id, account_id)[1]
            != owner
        ):
            raise ValueError("账号、门店或 Token 已变化，禁止提交。")
        if not (item.can_share() or item.can_generate_gift()):
            raise ValueError("权益当前不可分享，禁止提交。")
        keys = self.resource_key(item)
        if item.source == "prop":
            keys = tuple(
                dict.fromkeys(
                    (*keys, *self._prop_context_keys(store_id, account_id, item))
                )
            )
        with self.db.connection:
            if self.state_for_owner(store_id, account_id, item) is not None:
                raise ValueError("该权益已有提交记录；禁止重复分享，请人工核实。")
            operation_id = uuid4().hex
            record = json.dumps(
                {
                    "operation_id": operation_id,
                    "state": "pending",
                    "updated_at": utc_now(),
                    "guard_keys": list(keys),
                }
            )
            for key in keys:
                self.db.connection.execute(
                    "INSERT INTO settings(name, value) VALUES (?, ?)", (key, record)
                )
        # Returning only after the encrypted file has persisted is essential.
        return operation_id

    def finish(self, item, operation_id, *, confirmed=False):
        if type(confirmed) is not bool:
            raise ValueError("分享结果状态无效。")
        with self.db.connection:
            record = self.state(item)
            if record is None or record["operation_id"] != operation_id:
                raise ValueError("分享提交身份不匹配。")
            if record["state"] != "pending":
                raise ValueError("分享记录已终结，禁止覆盖。")
            keys = self.record_keys(item, record)
            record["state"] = "confirmed" if confirmed else "unknown"
            record["updated_at"] = utc_now()
            for key in keys:
                self.db.connection.execute(
                    "UPDATE settings SET value=? WHERE name=?",
                    (json.dumps(record), key),
                )

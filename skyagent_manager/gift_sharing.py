"""Encrypted one-order gift codes; no inferred expiration or stock eligibility."""

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from skyagent_manager.account_benefits import AccountBenefitSession
from skyagent_manager.db import utc_now
from skyagent_manager.direct_gifts import validated_gift_code
from skyagent_manager.security import atomic_write
from skyagent_manager.share_journal import ShareJournal
from skyagent_manager.share_safety import require_share_writes_allowed
from skyagent_manager.sync import _unique_response_object

PENDING_GIFT_PREFIX = "pending_gift/v1/"


@dataclass(frozen=True)
class PendingGiftEntry:
    pending_id: str
    store_id: str
    account_id: str
    operation_id: str
    registered_at: str


def read_pending_gift(database, pending_id):
    if not isinstance(pending_id, str) or not re.fullmatch(r"[0-9a-f]{32}", pending_id):
        raise ValueError("待核验礼包关联无效。")
    raw = database.get_setting(PENDING_GIFT_PREFIX + pending_id)
    if not raw or len(raw) > 16384:
        raise ValueError("待核验礼包关联缺失。")
    payload = json.loads(raw, object_pairs_hook=_unique_response_object)
    if not isinstance(payload, dict) or payload.get("state") != "pending_verification":
        raise ValueError("待核验礼包状态无效。")
    for key in ("store_id", "account_id", "operation_id"):
        if not isinstance(payload.get(key), str) or not re.fullmatch(
            r"[0-9a-f]{32}", payload[key]
        ):
            raise ValueError("待核验礼包身份无效。")
    stamp = payload.get("registered_at")
    if (
        not isinstance(stamp, str)
        or len(stamp) > 64
        or datetime.fromisoformat(stamp).utcoffset() is None
    ):
        raise ValueError("待核验礼包登记时间无效。")
    return PendingGiftEntry(
        pending_id,
        payload["store_id"],
        payload["account_id"],
        payload["operation_id"],
        stamp,
    )


@dataclass(frozen=True)
class SavedGiftShare:
    operation_id: str
    store_id: str
    account_id: str
    share_code: str = field(repr=False)
    created_at: str
    resource_keys: tuple[str, ...] = field(repr=False)
    pending_id: str = ""


class GiftShareOutputs:
    PREFIX = "third_party_gift/output/v1/"
    CODE_PREFIX = "third_party_gift/code/v1/"

    def __init__(self, database):
        self.db = database

    @classmethod
    def code_key(cls, code):
        return cls.CODE_PREFIX + sha256(code.encode()).hexdigest()

    def save(self, store_id, account_id, owner, item, operation_id, code):
        token, current = AccountBenefitSession(self.db, None)._identity(
            store_id, account_id
        )
        item.validated()
        if current != owner or not item.can_generate_gift():
            raise ValueError("礼包分享归属或上下文已变化，未保存。")
        require_share_writes_allowed(self.db)
        code = validated_gift_code(code, token)
        journal = ShareJournal(self.db)
        with self.db.connection:
            record = journal.state(item)
            if (
                not record
                or record["operation_id"] != operation_id
                or record["state"] != "pending"
            ):
                raise ValueError("礼包提交状态不匹配。")
            keys = journal.record_keys(item, record)
            if self.db.get_setting(self.code_key(code)):
                raise ValueError("礼包码已有关联，未冒充新的生成成功。")
            record["state"] = "confirmed"
            record["updated_at"] = utc_now()
            for key in keys:
                self.db.connection.execute(
                    "UPDATE settings SET value=? WHERE name=?",
                    (json.dumps(record), key),
                )
            payload = {
                "store_id": store_id,
                "account_id": account_id,
                "share_code": code,
                "created_at": record["updated_at"],
                "resource_keys": list(keys),
            }
            self.db.connection.executemany(
                "INSERT INTO settings(name,value) VALUES (?,?)",
                [
                    (self.PREFIX + operation_id, json.dumps(payload)),
                    (self.code_key(code), operation_id),
                ],
            )
            self.db._activity_in_transaction(
                store_id,
                "礼包分享结果保存",
                "已加密保存已确认礼包码；有效期未知，未入库存、未领取、未上传",
            )

    def load_operation(self, store_id, account_id, operation_id):
        self.db._require_active_store(store_id)
        try:
            if not isinstance(operation_id, str) or not re.fullmatch(
                r"[0-9a-f]{32}", operation_id
            ):
                raise ValueError()
            raw = self.db.get_setting(self.PREFIX + operation_id)
            if not raw or len(raw) > 16384:
                raise ValueError()
            payload = json.loads(raw, object_pairs_hook=_unique_response_object)
            if (
                not isinstance(payload, dict)
                or payload.get("store_id") != store_id
                or payload.get("account_id") != account_id
            ):
                raise ValueError()
            code = validated_gift_code(payload.get("share_code"))
            if self.db.get_setting(self.code_key(code)) != operation_id:
                raise ValueError()
            keys = payload.get("resource_keys")
            if (
                not isinstance(keys, list)
                or len(keys) != 2
                or any(
                    not isinstance(key, str)
                    or not re.fullmatch(
                        re.escape(ShareJournal.PREFIX) + r"[0-9a-f]{64}", key
                    )
                    for key in keys
                )
                or keys[0] == keys[1]
            ):
                raise ValueError()
            records = []
            for key in keys:
                raw_record = self.db.get_setting(key)
                if len(raw_record) > 16384:
                    raise ValueError()
                record = json.loads(
                    raw_record, object_pairs_hook=_unique_response_object
                )
                if (
                    not isinstance(record, dict)
                    or record.get("state") != "confirmed"
                    or record.get("operation_id") != operation_id
                    or record.get("guard_keys") != keys
                ):
                    raise ValueError()
                records.append(record)
            created_at = payload.get("created_at")
            if (
                records[0] != records[1]
                or not isinstance(created_at, str)
                or len(created_at) > 64
                or datetime.fromisoformat(created_at).utcoffset() is None
                or created_at != records[0].get("updated_at")
            ):
                raise ValueError()
            pending_id = payload.get("pending_id", "")
            if not isinstance(pending_id, str):
                raise ValueError()
            if pending_id:
                pending = read_pending_gift(self.db, pending_id)
                if (pending.store_id, pending.account_id, pending.operation_id) != (
                    store_id,
                    account_id,
                    operation_id,
                ):
                    raise ValueError()
            return SavedGiftShare(
                operation_id,
                store_id,
                account_id,
                code,
                created_at,
                tuple(keys),
                pending_id,
            )
        except Exception:
            raise ValueError(
                "已保存礼包结果缺失、归属或防重关联异常；不得重新生成。"
            ) from None

    def list_saved(self, store_id, account_id):
        self.db._require_active_store(store_id)
        try:
            AccountBenefitSession(self.db, None)._identity(store_id, account_id)
            rows = self.db.connection.execute(
                "SELECT name,value FROM settings WHERE name GLOB ? LIMIT 2001",
                (self.PREFIX + "*",),
            ).fetchall()
            if len(rows) > 2000:
                raise ValueError()
            saved = []
            for row in rows:
                if len(row["value"]) > 16384:
                    raise ValueError()
                payload = json.loads(
                    row["value"], object_pairs_hook=_unique_response_object
                )
                if not isinstance(payload, dict):
                    raise ValueError()
                if (
                    payload.get("store_id") == store_id
                    and payload.get("account_id") == account_id
                ):
                    saved.append(
                        self.load_operation(
                            store_id, account_id, row["name"][len(self.PREFIX) :]
                        )
                    )
            return tuple(
                sorted(
                    saved,
                    key=lambda row: (row.created_at, row.operation_id),
                    reverse=True,
                )
            )
        except Exception:
            raise ValueError(
                "本地礼包成功记录异常或超过2000条；未显示部分列表。"
            ) from None

    def export_to(self, path, store_id, account_id, owner, rows, *, full=False):
        try:
            self.db._require_active_store(store_id)
            if (
                type(full) is not bool
                or not rows
                or len(rows) > 2000
                or (full and len(rows) != 1)
            ):
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
                for row in rows
            )
            if checked != tuple(rows) or len(
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
                    "操作摘要",
                    "保存时间",
                    "有效期",
                    "礼包码（明文）" if full else "礼包码（隐藏）",
                ]
            )
            for row in checked:
                code = row.share_code if full else "隐藏"
                if code.startswith(("=", "+", "-", "@")):
                    code = "'" + code
                writer.writerow([row.operation_id[:8], row.created_at, "未知", code])
            atomic_write(path, stream.getvalue().encode("utf-8-sig"))
        except Exception:
            raise ValueError(
                "礼包导出被阻断或写入失败；未修改分享状态，不展示敏感内容。"
            ) from None


class PendingGiftInventory:
    """A reference-only quarantine, deliberately outside consumable stock/tasks.

    There is no promotion, deletion, expiry override, release or claiming API.
    Gift codes stay in the original encrypted confirmed-result repository.
    """

    def __init__(self, database):
        self.db = database

    def register(self, store_id, account_id, owner, operation_id, *, expected=None):
        if (
            AccountBenefitSession(self.db, None)._identity(store_id, account_id)[1]
            != owner
        ):
            raise ValueError("账号、门店或Token已变化，未登记。")
        require_share_writes_allowed(self.db)
        outputs = GiftShareOutputs(self.db)
        with self.db.connection:
            saved = outputs.load_operation(store_id, account_id, operation_id)
            if expected is not None and saved != expected:
                raise ValueError("礼包成功结果已变化，请重新确认。")
            if saved.pending_id:
                raise ValueError("该礼包已登记待核验，不重复登记。")
            if self.db.connection.execute(
                "SELECT 1 FROM settings WHERE name GLOB ? AND instr(value,?)>0 LIMIT 1",
                (PENDING_GIFT_PREFIX + "*", operation_id),
            ).fetchone():
                raise ValueError("已有待核验引用但结果关联异常，禁止重复登记。")
            if (
                len(
                    self.db.connection.execute(
                        "SELECT name FROM settings WHERE name GLOB ? LIMIT 2000",
                        (PENDING_GIFT_PREFIX + "*",),
                    ).fetchall()
                )
                >= 2000
            ):
                raise ValueError("待核验库已达2000条上限，未登记。")
            if self.db.connection.execute(
                "SELECT 1 FROM stock WHERE value=? LIMIT 1", (saved.share_code,)
            ).fetchone():
                raise ValueError("该码已有库存记录，需人工核对，不重复登记或更改状态。")
            pending_id = uuid4().hex
            payload = {
                "store_id": store_id,
                "account_id": account_id,
                "operation_id": operation_id,
                "registered_at": utc_now(),
                "state": "pending_verification",
            }
            self.db.connection.execute(
                "INSERT INTO settings(name,value) VALUES(?,?)",
                (PENDING_GIFT_PREFIX + pending_id, json.dumps(payload)),
            )
            source = json.loads(
                self.db.get_setting(outputs.PREFIX + operation_id),
                object_pairs_hook=_unique_response_object,
            )
            source["pending_id"] = pending_id
            self.db.connection.execute(
                "UPDATE settings SET value=? WHERE name=?",
                (json.dumps(source), outputs.PREFIX + operation_id),
            )
            self.db._activity_in_transaction(
                store_id,
                "礼包待核验登记",
                "登记一条已保存成功结果；资格/日期未知，未加入可用库存、未领取、未上传",
            )
        return pending_id

    def list(self, store_id):
        self.db._require_active_store(store_id)
        try:
            rows = self.db.connection.execute(
                "SELECT name FROM settings WHERE name GLOB ? LIMIT 2001",
                (PENDING_GIFT_PREFIX + "*",),
            ).fetchall()
            if len(rows) > 2000:
                raise ValueError()
            entries = []
            outputs = GiftShareOutputs(self.db)
            for row in rows:
                entry = read_pending_gift(
                    self.db, row["name"][len(PENDING_GIFT_PREFIX) :]
                )
                source_raw = self.db.get_setting(outputs.PREFIX + entry.operation_id)
                if not source_raw or len(source_raw) > 16384:
                    raise ValueError()
                source = json.loads(
                    source_raw, object_pairs_hook=_unique_response_object
                )
                if not isinstance(source, dict) or (
                    source.get("store_id"),
                    source.get("account_id"),
                    source.get("pending_id"),
                ) != (entry.store_id, entry.account_id, entry.pending_id):
                    raise ValueError()
                if entry.store_id != store_id:
                    continue
                saved = outputs.load_operation(
                    store_id, entry.account_id, entry.operation_id
                )
                if saved.pending_id != entry.pending_id:
                    raise ValueError()
                entries.append(entry)
            return tuple(
                sorted(
                    entries,
                    key=lambda entry: (entry.registered_at, entry.pending_id),
                    reverse=True,
                )
            )
        except Exception:
            raise ValueError(
                "待核验礼包记录异常或超过2000条，未显示部分列表；不据此重新生成。"
            ) from None

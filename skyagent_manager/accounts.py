"""Authorized account records, independent of the member ledger."""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from hashlib import sha256

from skyagent_manager.db import StoreDatabase, normalize_phone, utc_now
from skyagent_manager.imports import MAX_ROWS, CsvRow, ImportReport, ImportRowResult
from skyagent_manager.validation import clean_text, row_text, search_pattern


@dataclass(frozen=True)
class AccountInput:
    phone: str = field(repr=False)
    token: str = field(repr=False)
    label: str = ""
    note: str = ""
    is_new_user: bool | None = None

    def validated(self):
        phone = clean_text(self.phone, "手机号", 64, required=True)
        normalized = normalize_phone(phone)
        token = self.token.strip()
        if not re.fullmatch(r"[A-Za-z0-9._+/=\-]{16,4096}", token):
            raise ValueError("Token 格式无效，需为 16–4096 字符且不含空白。")
        if self.is_new_user is not None and type(self.is_new_user) is not bool:
            raise ValueError("新用户标记必须为是、否或未知。")
        return (
            phone,
            normalized,
            token,
            clean_text(self.label, "来源标识", 128),
            clean_text(self.note, "备注", 2000),
            self.is_new_user,
        )


def parse_account_text(text: str) -> list[CsvRow]:
    if len(text.encode("utf-8")) > 20 * 1024 * 1024:
        raise ValueError("账号文本最多 20 MiB。")
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(rows) >= MAX_ROWS:
            raise ValueError("单次最多导入 20,000 行。")
        parts = re.split(r"(?:-{2,}|—+|[,\t ]+)", line.strip())
        phones, tokens, labels = [], [], []
        for part in parts:
            if re.fullmatch(r"COM\d+", part, re.IGNORECASE):
                labels.append(part.upper())
            elif re.fullmatch(r"\+?\d{7,18}", part):
                phones.append(part)
            elif re.fullmatch(r"[A-Za-z0-9._+/=\-]{16,4096}", part):
                tokens.append(part)
            else:
                labels.append("invalid")
        if (
            len(phones) != 1
            or len(tokens) != 1
            or len(labels) > 1
            or "invalid" in labels
        ):
            values = {
                "phone": "",
                "token": "",
                "parse_error": "账号行缺少手机号/Token 或字段有歧义",
            }
        else:
            values = {
                "phone": phones[0],
                "token": tokens[0],
                "label": labels[0] if labels else "",
            }
        rows.append(CsvRow(values, line_number))
    return rows


def account_from_row(row: dict) -> AccountInput:
    if row.get("parse_error") or None in row:
        raise ValueError("账号行缺少手机号/Token 或字段有歧义。")
    flag = row_text(row, "is_new_user").strip().lower()
    flags = {
        "": None,
        "unknown": None,
        "未知": None,
        "true": True,
        "1": True,
        "是": True,
        "false": False,
        "0": False,
        "否": False,
    }
    if flag not in flags:
        raise ValueError("新用户标记必须为 true/false、1/0、是/否或空值。")
    return AccountInput(
        row_text(row, "phone"),
        row_text(row, "token"),
        row_text(row, "label"),
        row_text(row, "note"),
        flags[flag],
    )


class Accounts:
    def __init__(self, database: StoreDatabase):
        self.db = database

    @staticmethod
    def _hold_key(store_id, phone_norm):
        digest = sha256(phone_norm.encode()).hexdigest()
        return f"account_import_hold/{store_id}/{digest}"

    def import_hold(self, store_id, row):
        return self.db.get_setting(self._hold_key(store_id, row["phone_norm"]))

    def reserve_upload(self, store_id, ids):
        """Persist conservative holds before network starts, including crash recovery."""
        items = self.sync_items(store_id, ids)
        if len(items) != len(set(ids)):
            raise ValueError("账号上传范围已变化，请重新选择。")
        with self.db.connection:
            for item in items:
                self.db.connection.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                    (self._hold_key(store_id, item["phone"]), "pending"),
                )

    def list(self, store_id: str, search: str = ""):
        return list(
            self.db.connection.execute(
                "SELECT a.*, r.state AS import_state, r.summary AS import_summary FROM accounts a "
                "LEFT JOIN account_import_results r ON r.store_id=a.store_id AND r.record_id=a.id "
                "WHERE a.store_id=? AND (a.phone LIKE ? ESCAPE '\\' OR a.label LIKE ? ESCAPE '\\' "
                "OR a.note LIKE ? ESCAPE '\\') ORDER BY a.created_at, a.id",
                (store_id, *(search_pattern(search.strip()) for _ in range(3))),
            )
        )

    def get(self, store_id: str, record_id: str):
        self.db._require_active_store(store_id)
        row = self.db.connection.execute(
            "SELECT * FROM accounts WHERE store_id=? AND id=?", (store_id, record_id)
        ).fetchone()
        if row is None:
            raise ValueError("当前门店下找不到该账号。")
        return row

    def _add(self, store_id: str, value: AccountInput) -> str:
        phone, normalized, token, label, note, new_user = value.validated()
        record_id, now = uuid.uuid4().hex, utc_now()
        try:
            self.db.connection.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    store_id,
                    label,
                    phone,
                    normalized,
                    token,
                    new_user,
                    note,
                    now,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(
                "此门店已存在相同手机号或 Token，请审阅后编辑，不会自动覆盖。"
            ) from None
        return record_id

    def add(self, store_id: str, value: AccountInput) -> str:
        self.db._require_active_store(store_id)
        with self.db.connection:
            record_id = self._add(store_id, value)
            self.db._activity_in_transaction(store_id, "添加账号", f"记录 {record_id}")
        return record_id

    def save_backend_account(self, store_id, phone, token, *, authorized=False):
        """Save an already-existing backend identity with a durable upload hold."""
        if authorized is not True:
            raise ValueError("需明确确认后台已有账号本地保存。")
        self.db._require_active_store(store_id)
        value = AccountInput(phone, token, is_new_user=None)
        normalized = value.validated()[1]
        with self.db.connection:
            record_id = self._add(store_id, value)
            self.db.connection.execute(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                (self._hold_key(store_id, normalized), "backend-existing"),
            )
            self.db._activity_in_transaction(
                store_id, "保存后台已有账号", "加密保存；资格未知，禁止重复上传"
            )
        return record_id

    def update(self, store_id: str, record_id: str, value: AccountInput) -> None:
        self.get(store_id, record_id)
        phone, normalized, token, label, note, new_user = value.validated()
        with self.db.connection:
            try:
                self.db.connection.execute(
                    "UPDATE accounts SET phone=?, phone_norm=?, token=?, label=?, note=?, "
                    "is_new_user=?, authorized_at=?, updated_at=? WHERE store_id=? AND id=?",
                    (
                        phone,
                        normalized,
                        token,
                        label,
                        note,
                        new_user,
                        utc_now(),
                        utc_now(),
                        store_id,
                        record_id,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    "此门店已存在相同手机号或 Token，不会自动覆盖。"
                ) from None
            self.db.connection.execute(
                "DELETE FROM account_import_results WHERE store_id=? AND record_id=?",
                (store_id, record_id),
            )
            self.db._activity_in_transaction(store_id, "编辑账号", f"记录 {record_id}")

    def delete(self, store_id: str, record_ids: list[str]) -> None:
        for record_id in record_ids:
            self.get(store_id, record_id)
        with self.db.connection:
            self.db.connection.executemany(
                "DELETE FROM accounts WHERE store_id=? AND id=?",
                [(store_id, record_id) for record_id in record_ids],
            )
            self.db._activity_in_transaction(
                store_id, "删除账号", f"本地删除 {len(record_ids)} 条；不删除后端"
            )

    def import_rows(self, store_id: str, rows: list[dict]) -> ImportReport:
        self.db._require_active_store(store_id)
        if len(rows) > MAX_ROWS:
            raise ValueError("单次最多导入 20,000 行。")
        results = []
        with self.db.connection:
            for index, row in enumerate(rows, 2):
                try:
                    self._add(store_id, account_from_row(row))
                except ValueError as exc:
                    results.append(
                        ImportRowResult(
                            getattr(row, "line_number", index), False, str(exc)
                        )
                    )
                else:
                    results.append(
                        ImportRowResult(
                            getattr(row, "line_number", index), True, "已导入"
                        )
                    )
            report = ImportReport(results)
            self.db._activity_in_transaction(
                store_id,
                "导入账号",
                f"导入 {report.inserted} 条，跳过 {report.skipped} 条",
            )
        return report

    def sync_items(
        self, store_id: str, ids: list[str] | None = None, *, failed_only=False
    ):
        self.db._require_active_store(store_id)
        rows = self.list(store_id)
        selected = set(ids) if ids is not None else None
        items = []
        for row in rows:
            if selected is not None and row["id"] not in selected:
                continue
            if failed_only and row["import_state"] != "failed":
                continue
            hold = self.import_hold(store_id, row)
            if hold or row["import_state"] == "success":
                if failed_only:
                    continue
                raise ValueError(
                    "选中账号已入库或提交结果待核对，后端目前不去重，禁止重复上传。"
                )
            if (
                row["import_state"] == "failed"
                and row["import_summary"] != "后端拒绝该记录"
            ):
                if failed_only:
                    continue
                raise ValueError(
                    "选中账号存在未知入库结果，请先核对后台，不能直接重试。"
                )
            if row["is_new_user"] is None:
                raise ValueError("选中账号含未知新用户标记，请先编辑确认是或否再上传。")
            items.append(
                {
                    "id": row["id"],
                    "phone": row["phone_norm"],
                    "token": row["token"],
                    "is_new_user": bool(row["is_new_user"]),
                    "is_online": True,
                    "is_enabled": True,
                    "remark": "软件导入",
                }
            )
        if selected is not None and selected - {row["id"] for row in rows}:
            raise ValueError("部分选择不属于当前门店。")
        return items

    def save_results(self, store_id: str, results: list) -> None:
        for result in results:
            self.get(store_id, result.record_id)
        with self.db.connection:
            for result in results:
                row = self.get(store_id, result.record_id)
                key = self._hold_key(store_id, row["phone_norm"])
                if not result.ok and result.summary == "后端拒绝该记录":
                    self.db.connection.execute(
                        "DELETE FROM settings WHERE name=? AND value='pending'", (key,)
                    )
                else:
                    self.db.connection.execute(
                        "INSERT INTO settings VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET "
                        "value=CASE WHEN settings.value='success' THEN 'success' ELSE excluded.value END",
                        (key, "success" if result.ok else "pending"),
                    )
            self.db.connection.executemany(
                "INSERT OR REPLACE INTO account_import_results VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        store_id,
                        result.record_id,
                        "success" if result.ok else "failed",
                        result.summary[:100],
                        utc_now(),
                    )
                    for result in results
                ],
            )

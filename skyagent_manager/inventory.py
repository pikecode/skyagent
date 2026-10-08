"""Encrypted, store-isolated stock and local-only simulation tasks.

No external requests or benefit claiming are performed by this module.
"""

import json
import sqlite3
import uuid

from skyagent_manager.db import utc_now
from skyagent_manager.imports import ImportReport, ImportRowResult

KINDS = {
    "silver": "银卡资源",
    "breakfast": "早餐资源",
    "room_upgrade": "升房资源",
    "delayed_checkout": "延迟退房资源",
    "gift": "礼包资源",
    "invite": "邀请码",
    "prop": "法宝分享资源",
}


class Inventory:
    def __init__(self, database):
        self.db = database
        self.connection = database.connection

    def list(self, store_id):
        return list(
            self.connection.execute(
                "SELECT * FROM stock WHERE store_id=? ORDER BY kind,rowid", (store_id,)
            )
        )

    def import_values(self, store_id, kind, values):
        self.db._require_active_store(store_id)
        values = list(values)
        if kind not in KINDS or len(values) > 20000:
            raise ValueError("资源类型无效或超过 20000 条。")
        if any(
            not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in values
        ):
            raise ValueError("资源不能为空，且每条不能超过 4096 字符。")
        added = 0
        with self.connection:
            for value in values:
                cursor = self.connection.execute(
                    "INSERT OR IGNORE INTO stock(id,store_id,kind,value) VALUES(?,?,?,?)",
                    (uuid.uuid4().hex, store_id, kind, value.strip()),
                )
                added += cursor.rowcount
            self.db._activity_in_transaction(
                store_id,
                "资源导入",
                f"新增 {added} 条，跳过重复 {len(values) - added} 条",
            )
        return added

    def import_rows(self, store_id, rows):
        self.db._require_active_store(store_id)
        rows = list(rows)
        if len(rows) > 20000:
            raise ValueError("单次最多导入 20000 条资源。")
        results = []
        with self.connection:
            for index, row in enumerate(rows, 1):
                number = getattr(row, "line_number", index)
                try:
                    if not isinstance(row, dict) or None in row:
                        raise ValueError("字段数量或行结构无效")
                    fields = [
                        row.get(key, "") for key in ("kind", "value", "state", "note")
                    ]
                    if any(not isinstance(value, str) for value in fields):
                        raise ValueError("缺少字段或字段类型无效")
                    kind, value, state, note = [value.strip() for value in fields]
                    state = state or "available"
                    if kind not in KINDS or not value or len(value) > 4096:
                        raise ValueError("资源类型或内容无效")
                    if state not in {"available", "used"}:
                        raise ValueError("只允许 available 或 used；预占需通过任务创建")
                    if len(note) > 2000 or (kind != "invite" and note):
                        raise ValueError("备注仅支持邀请码，且最多 2000 字符")
                    existing = self.connection.execute(
                        "SELECT state FROM stock WHERE store_id=? AND kind=? AND value=?",
                        (store_id, kind, value),
                    ).fetchone()
                    if existing:
                        raise ValueError(
                            "重复，未覆盖"
                            if existing["state"] == state
                            else "同值资源状态冲突，未覆盖"
                        )
                    rid = uuid.uuid4().hex
                    self.connection.execute(
                        "INSERT INTO stock VALUES(?,?,?,?,?)",
                        (rid, store_id, kind, value, state),
                    )
                    if note:
                        self.connection.execute(
                            "INSERT INTO settings VALUES(?,?)",
                            (f"invite_note/{store_id}/{rid}", note),
                        )
                except ValueError as exc:
                    results.append(ImportRowResult(number, False, str(exc)))
                else:
                    results.append(ImportRowResult(number, True, "已导入"))
            report = ImportReport(results)
            self.db._activity_in_transaction(
                store_id,
                "资源逐行导入",
                f"新增 {report.inserted} 条，跳过 {report.skipped} 条",
            )
        return report

    def delete(self, store_id, stock_id):
        self.db._require_active_store(store_id)
        with self.connection:
            try:
                cursor = self.connection.execute(
                    "DELETE FROM stock WHERE store_id=? AND id=? AND state='available'",
                    (store_id, stock_id),
                )
            except sqlite3.IntegrityError:
                raise ValueError("资源已关联任务，需保留历史记录。") from None
            if cursor.rowcount != 1:
                raise ValueError("资源不存在或不可删除。")
            self.db._activity_in_transaction(
                store_id, "资源删除", "已删除一条未使用资源"
            )
            self.connection.execute(
                "DELETE FROM settings WHERE name=? AND value=?",
                (f"selected_invite/{store_id}", stock_id),
            )
            selected = [
                rid for rid in self.selected_invites(store_id) if rid != stock_id
            ]
            self._save_invites(store_id, selected)
            self.connection.execute(
                "DELETE FROM settings WHERE name=?",
                (f"invite_note/{store_id}/{stock_id}",),
            )

    def edit_invite(self, store_id, stock_id, value, note=None):
        self.db._require_active_store(store_id)
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise ValueError("邀请码不能为空，且不能超过 4096 字符。")
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise ValueError("邀请码备注最多 2000 字符。")
        with self.connection:
            try:
                cursor = self.connection.execute(
                    "UPDATE stock SET value=? WHERE store_id=? AND id=? AND kind='invite' AND state='available'",
                    (value.strip(), store_id, stock_id),
                )
            except sqlite3.IntegrityError:
                raise ValueError("邀请码已存在。") from None
            if cursor.rowcount != 1:
                raise ValueError("邀请码不存在或不可编辑。")
            if note is not None:
                self.connection.execute(
                    "INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (f"invite_note/{store_id}/{stock_id}", note.strip()),
                )
            self.db._activity_in_transaction(store_id, "邀请码编辑", "已更新邀请码")

    def invite_note(self, store_id, stock_id):
        return self.db.get_setting(f"invite_note/{store_id}/{stock_id}")

    def selected_invites(self, store_id):
        raw = self.db.get_setting(f"selected_invites/{store_id}")
        if raw:
            try:
                ids = json.loads(raw)
            except (ValueError, RecursionError):
                return []
            if not isinstance(ids, list) or any(
                not isinstance(rid, str) for rid in ids
            ):
                return []
        else:
            legacy = self.db.get_setting(f"selected_invite/{store_id}")
            ids = [legacy] if legacy else []
        available = {
            row["id"]
            for row in self.list(store_id)
            if row["kind"] == "invite" and row["state"] == "available"
        }
        return list(dict.fromkeys(rid for rid in ids if rid in available))

    def _save_invites(self, store_id, ids):
        self.connection.executemany(
            "INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            [
                (f"selected_invites/{store_id}", json.dumps(ids)),
                (f"selected_invite/{store_id}", ids[0] if ids else ""),
            ],
        )

    def select_invites(self, store_id, stock_ids):
        self.db._require_active_store(store_id)
        ids = list(stock_ids)
        if (
            len(ids) > 20000
            or any(not isinstance(rid, str) for rid in ids)
            or len(ids) != len(set(ids))
        ):
            raise ValueError("邀请码选择无效或重复。")
        available = {
            row["id"]
            for row in self.list(store_id)
            if row["kind"] == "invite" and row["state"] == "available"
        }
        if any(rid not in available for rid in ids):
            raise ValueError("当前门店下找不到可用邀请码。")
        with self.connection:
            self._save_invites(store_id, ids)
            self.db._activity_in_transaction(
                store_id, "邀请码选择", f"已选用 {len(ids)} 条邀请码"
            )

    def select_invite(self, store_id, stock_id=""):
        self.select_invites(store_id, [stock_id] if stock_id else [])

    def tasks(self, store_id):
        return list(
            self.connection.execute(
                "SELECT t.*,count(i.stock_id) AS total,sum(i.state<>'reserved') AS completed FROM local_tasks t LEFT JOIN local_task_items i ON i.task_id=t.id AND i.store_id=t.store_id WHERE t.store_id=? GROUP BY t.id ORDER BY t.rowid DESC",
                (store_id,),
            )
        )

    def start(self, store_id, stock_ids):
        self.db._require_active_store(store_id)
        ids = list(stock_ids)
        if not ids or len(ids) > 20000 or len(ids) != len(set(ids)):
            raise ValueError("请选择不重复的资源，最多 20000 条。")
        task_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO local_tasks VALUES(?,?,?,?)",
                (task_id, store_id, "running", utc_now()),
            )
            for stock_id in ids:
                cursor = self.connection.execute(
                    "UPDATE stock SET state='reserved' WHERE store_id=? AND id=? AND state='available' AND kind<>'invite'",
                    (store_id, stock_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("资源不可用、属于其他门店或是邀请码。")
                self.connection.execute(
                    "INSERT INTO local_task_items VALUES(?,?,?,?)",
                    (store_id, task_id, stock_id, "reserved"),
                )
            self.db._activity_in_transaction(
                store_id, "本地模拟任务", f"预占 {len(ids)} 条资源；不会请求外部服务"
            )
        return task_id

    def finish(self, store_id, task_id, outcome):
        self.db._require_active_store(store_id)
        if outcome not in {"succeeded", "failed", "canceled"}:
            raise ValueError("任务结果无效。")
        with self.connection:
            if (
                outcome == "succeeded"
                and self.connection.execute(
                    "SELECT 1 FROM local_task_items WHERE store_id=? AND task_id=? AND state='released'",
                    (store_id, task_id),
                ).fetchone()
            ):
                raise ValueError("任务已有失败/释放项，不能整任务标记成功。")
            cursor = self.connection.execute(
                "UPDATE local_tasks SET state=? WHERE store_id=? AND id=? AND state='running'",
                (outcome, store_id, task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("任务不存在或已经结束。")
            self._release_or_use(store_id, task_id, outcome == "succeeded")
            self.db._activity_in_transaction(store_id, "本地模拟结果", outcome)

    def task_items(self, store_id, task_id):
        return list(
            self.connection.execute(
                "SELECT i.*,s.kind,s.value FROM local_task_items i JOIN stock s ON s.id=i.stock_id AND s.store_id=i.store_id WHERE i.store_id=? AND i.task_id=? ORDER BY i.rowid",
                (store_id, task_id),
            )
        )

    def complete_item(self, store_id, task_id, stock_id, *, succeeded):
        self.db._require_active_store(store_id)
        if type(succeeded) is not bool:
            raise ValueError("逐项模拟结果无效。")
        with self.connection:
            if not self.connection.execute(
                "SELECT 1 FROM local_tasks WHERE store_id=? AND id=? AND state='running'",
                (store_id, task_id),
            ).fetchone():
                raise ValueError("任务不存在或已结束。")
            cursor = self.connection.execute(
                "UPDATE local_task_items SET state=? WHERE store_id=? AND task_id=? AND stock_id=? AND state='reserved'",
                ("used" if succeeded else "released", store_id, task_id, stock_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("任务项不存在或已提交结果。")
            cursor = self.connection.execute(
                "UPDATE stock SET state=? WHERE store_id=? AND id=? AND state='reserved'",
                ("used" if succeeded else "available", store_id, stock_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("资源预占状态不一致，未保存结果。")
            remaining = self.connection.execute(
                "SELECT 1 FROM local_task_items WHERE store_id=? AND task_id=? AND state='reserved'",
                (store_id, task_id),
            ).fetchone()
            if not remaining:
                failed = self.connection.execute(
                    "SELECT 1 FROM local_task_items WHERE store_id=? AND task_id=? AND state='released'",
                    (store_id, task_id),
                ).fetchone()
                self.connection.execute(
                    "UPDATE local_tasks SET state=? WHERE store_id=? AND id=?",
                    ("failed" if failed else "succeeded", store_id, task_id),
                )
            self.db._activity_in_transaction(
                store_id,
                "逐项本地模拟",
                "成功消耗一条资源" if succeeded else "失败释放一条资源",
            )

    def _release_or_use(self, store_id, task_id, used):
        self.connection.execute(
            "UPDATE stock SET state=? WHERE store_id=? AND id IN (SELECT stock_id FROM local_task_items WHERE store_id=? AND task_id=? AND state='reserved')",
            ("used" if used else "available", store_id, store_id, task_id),
        )
        self.connection.execute(
            "UPDATE local_task_items SET state=? WHERE store_id=? AND task_id=? AND state='reserved'",
            ("used" if used else "released", store_id, task_id),
        )

    def recover(self):
        """Interrupt local simulations on open/restore; never auto-replay."""
        tasks = self.connection.execute(
            "SELECT * FROM local_tasks WHERE state='running'"
        ).fetchall()
        if not tasks:
            return
        with self.connection:
            for task in tasks:
                self._release_or_use(task["store_id"], task["id"], False)
                self.connection.execute(
                    "UPDATE local_tasks SET state='interrupted' WHERE id=?",
                    (task["id"],),
                )
                self.db._activity_in_transaction(
                    task["store_id"],
                    "模拟任务中断",
                    "重启或恢复后已释放本地预占；未自动重放",
                )

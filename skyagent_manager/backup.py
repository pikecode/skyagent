"""Encrypted snapshot backups, validation and bounded automatic retention."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from skyagent_manager.security import (
    PORTABLE_MAGIC,
    atomic_write,
    decrypt,
    decrypt_portable,
    encrypt,
    encrypt_portable,
    read_file,
)

if TYPE_CHECKING:
    from skyagent_manager.db import StoreDatabase


def validate_snapshot(snapshot: bytes) -> dict[str, int]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(snapshot)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 2, 3, 4, 5, 6}:
            raise ValueError("该备份不是受支持的台账数据库版本。")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性检查失败。")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份存在无效的数据关联。")
        mismatched = connection.execute(
            "SELECT 1 FROM benefits b JOIN members m ON m.id=b.member_id WHERE b.store_id<>m.store_id LIMIT 1"
        ).fetchone()
        if mismatched:
            raise ValueError("备份存在跨门店会员关联。")
        if version >= 3:
            for table in ("store_sync", "sync_results", "settings"):
                connection.execute(f"SELECT 1 FROM {table} LIMIT 1")
        if version >= 4:
            connection.execute(
                "SELECT id,store_id,label,phone,phone_norm,token,is_new_user,note,authorized_at,created_at,updated_at FROM accounts LIMIT 1"
            )
            connection.execute(
                "SELECT store_id,record_id,state,summary,updated_at FROM account_import_results LIMIT 1"
            )
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("stores", "members", "benefits")
        }
        counts["accounts"] = (
            connection.execute("SELECT count(*) FROM accounts").fetchone()[0]
            if version >= 4
            else 0
        )
        if version >= 5:
            allowed_kinds = {
                "silver",
                "breakfast",
                "room_upgrade",
                "delayed_checkout",
                "gift",
                "invite",
            }
            if version >= 6:
                allowed_kinds.add("prop")
            if any(
                row[0] not in allowed_kinds
                for row in connection.execute("SELECT DISTINCT kind FROM stock")
            ):
                raise ValueError("备份库存类型与数据库版本不一致。")
            inconsistent = connection.execute(
                "SELECT 1 FROM local_tasks t LEFT JOIN local_task_items i ON i.task_id=t.id AND i.store_id=t.store_id "
                "GROUP BY t.id HAVING count(i.stock_id)=0 "
                "OR (t.state='running' AND sum(i.state='reserved')=0) "
                "OR (t.state<>'running' AND sum(i.state='reserved')>0) "
                "OR (t.state='succeeded' AND sum(i.state<>'used')>0) LIMIT 1"
            ).fetchone()
            duplicate = connection.execute(
                "SELECT 1 FROM local_task_items WHERE state IN ('reserved','used') GROUP BY store_id,stock_id HAVING count(*)>1 LIMIT 1"
            ).fetchone()
            bad_item = connection.execute(
                "SELECT 1 FROM local_task_items i JOIN stock s ON s.id=i.stock_id AND s.store_id=i.store_id "
                "WHERE s.kind='invite' OR (i.state='used' AND s.state<>'used') LIMIT 1"
            ).fetchone()
            if inconsistent or duplicate or bad_item:
                raise ValueError("备份任务状态、资源归属或重复预占/消耗不一致。")
            invalid = connection.execute(
                "SELECT 1 FROM stock s WHERE (s.state='reserved') <> EXISTS ("
                "SELECT 1 FROM local_task_items i JOIN local_tasks t ON t.id=i.task_id "
                "WHERE i.stock_id=s.id AND i.store_id=s.store_id AND i.state='reserved' AND t.state='running') LIMIT 1"
            ).fetchone()
            if invalid:
                raise ValueError("备份资源预占状态与任务不一致。")
            for table, columns in (
                ("stock", "id,store_id,kind,value,state"),
                ("local_tasks", "id,store_id,state,created_at"),
                ("local_task_items", "store_id,task_id,stock_id,state"),
            ):
                connection.execute(f"SELECT {columns} FROM {table} LIMIT 1")
                counts[table] = connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
        return counts
    except sqlite3.DatabaseError:
        raise ValueError("备份数据库结构无效。") from None
    finally:
        connection.close()


class BackupService:
    def __init__(self, database: StoreDatabase):
        self.database = database
        self.directory = database.path.parent / "backups"

    def create(self, path: Path) -> Path:
        path = Path(path)
        if path.resolve() == self.database.path.resolve():
            raise ValueError("备份不能覆盖正在使用的数据库。")
        snapshot = self.database.connection.serialize()
        validate_snapshot(snapshot)
        atomic_write(path, encrypt(snapshot, self.database.key))
        return path

    def create_portable(self, path: Path, password: str) -> Path:
        path = Path(path)
        if path.resolve() == self.database.path.resolve():
            raise ValueError("备份不能覆盖正在使用的数据库。")
        snapshot = self.database.connection.serialize()
        validate_snapshot(snapshot)
        atomic_write(path, encrypt_portable(snapshot, password))
        return path

    def inspect(
        self, path: Path, password: str | None = None
    ) -> tuple[bytes, dict[str, int]]:
        data = read_file(Path(path))
        if data.startswith(PORTABLE_MAGIC):
            if password is None:
                raise ValueError("跨机备份需要备份口令，不是应用启动密码。")
            snapshot = decrypt_portable(data, password)
        else:
            snapshot = decrypt(data, self.database.key)
        return snapshot, validate_snapshot(snapshot)

    def restore(self, path: Path, password: str | None = None) -> Path:
        snapshot, _ = self.inspect(path, password)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        safety = self.create(self.directory / f"before-restore-{stamp}.skybackup")
        self.database.replace_snapshot(snapshot)
        return safety

    def automatic(self) -> Path | None:
        if self.database.get_setting("backup_enabled", "1") != "1":
            return None
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = self.directory / f"auto-{day}.skybackup"
        created = None
        if path.exists():
            self.inspect(path)
        else:
            created = self.create(path)
        keep = max(1, min(90, int(self.database.get_setting("backup_keep", "7"))))
        # Only our exact date-based names qualify for automatic pruning.
        files = sorted(
            p
            for p in self.directory.glob("auto-????????.skybackup")
            if p.stem[5:].isdigit() and p.is_file()
        )
        for old in files[:-keep]:
            old.unlink()
        return created

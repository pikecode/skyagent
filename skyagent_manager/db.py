"""SQLite storage for store-isolated member and benefit records."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from skyagent_manager.imports import ImportReport, ImportRowResult
from skyagent_manager.security import (
    MAGIC,
    KeyVault,
    atomic_write,
    decrypt,
    encrypt,
    read_file,
)
from skyagent_manager.validation import (
    clean_date,
    clean_quantity,
    clean_text,
    row_text,
    search_pattern,
)


class EncryptedConnection(sqlite3.Connection):
    """Keep disk and memory consistent when a transaction cannot be persisted."""

    persist = None

    def __enter__(self):
        self.before = self.serialize()
        return super().__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        if exc_type is None and self.persist:
            try:
                self.persist()
            except Exception:
                self.deserialize(self.before)
                self.execute("PRAGMA foreign_keys = ON")
                raise
        return result


BENEFIT_KINDS = {
    "早餐券": "breakfast",
    "升房券": "room_upgrade",
    "延迟退房": "late_checkout",
    "礼包": "gift",
    "其他": "other",
}
BENEFIT_KIND_LABELS = {value: key for key, value in BENEFIT_KINDS.items()}
BENEFIT_STATES = {"可用", "已预留", "已使用", "已过期"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def mask_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 7:
        return "••••"
    return f"{digits[:3]}{'•' * max(4, len(digits) - 7)}{digits[-4:]}"


def mask_code(value: str) -> str:
    value = value.strip()
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}{'•' * min(12, len(value) - 7)}{value[-4:]}"


def normalize_phone(value: str) -> str:
    """Return a formatting-insensitive phone key while preserving display input."""
    clean = value.strip()
    if any(ch not in "+0123456789 ()-" for ch in clean):
        raise ValueError("手机号格式不正确。")
    digits = "".join(ch for ch in clean if ch.isdigit())
    if (
        not 7 <= len(digits) <= 18
        or clean.count("+") > 1
        or ("+" in clean and not clean.startswith("+"))
    ):
        raise ValueError("手机号格式不正确。")
    return ("+" if clean.startswith("+") else "") + digits


class StoreDatabase:
    """Own the local database and expose small, transaction-safe operations."""

    def __init__(self, path: Path, *, key: bytes | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        original = read_file(self.path) if self.path.exists() else None
        encrypted_exists = original is not None and original.startswith(MAGIC)
        legacy_exists = original is not None and original.startswith(
            b"SQLite format 3\x00"
        )
        if original is not None and not (encrypted_exists or legacy_exists):
            raise ValueError("数据库文件格式无效，未修改原文件或系统密钥。")
        self.vault = KeyVault(self.path.parent) if key is None else None
        self.key = (
            key
            if key is not None
            else self.vault.database_key(encrypted_exists=encrypted_exists)
        )
        self.connection = sqlite3.connect(":memory:", factory=EncryptedConnection)
        self.connection.row_factory = sqlite3.Row
        if original:
            if encrypted_exists:
                self.connection.deserialize(decrypt(original, self.key))
            elif original.startswith(b"SQLite format 3\x00"):
                if any(
                    Path(str(self.path) + suffix).exists()
                    for suffix in ("-wal", "-shm")
                ):
                    self.connection.close()
                    raise ValueError(
                        "旧数据库存在 WAL/SHM，请先关闭旧应用并完成检查点后再迁移。"
                    )
                # sqlite3_deserialize cannot open WAL-mode images even after a
                # clean checkpoint. SQLite documents resetting header bytes 18/19.
                original = original[:18] + b"\x01\x01" + original[20:]
                self.connection.deserialize(original)
            else:
                self.connection.close()
                raise ValueError("数据库文件格式无效，未修改原文件。")
            if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                self.connection.close()
                raise ValueError("数据库完整性检查失败。")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 6:
                self.connection.close()
                raise ValueError("数据库版本高于本应用支持的版本。")
            if version in {5, 6}:
                from skyagent_manager.backup import validate_snapshot

                try:
                    validate_snapshot(self.connection.serialize())
                except ValueError:
                    self.connection.close()
                    raise
            if legacy_exists:
                # Preserve a verified pre-migration snapshot without plaintext copies.
                atomic_write(
                    self.path.parent / "backups" / "before-encryption.skybackup",
                    encrypt(original, self.key),
                )
            elif version < 6:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
                atomic_write(
                    self.path.parent
                    / "backups"
                    / f"before-upgrade-v6-{stamp}.skybackup",
                    encrypt(self.connection.serialize(), self.key),
                )
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = MEMORY")
        self.connection.execute("PRAGMA temp_store = MEMORY")
        self._initialize()
        self._persist()
        self.connection.persist = self._persist
        from skyagent_manager.inventory import Inventory

        Inventory(self).recover()
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.connection.close()

    def _persist(self) -> None:
        atomic_write(self.path, encrypt(self.connection.serialize(), self.key))

    def replace_snapshot(self, snapshot: bytes) -> None:
        original = self.connection.serialize()
        persist = self.connection.persist
        # Initialization/recovery can commit transactions. Suppress intermediate
        # disk writes so the restored data and its safety block land together.
        self.connection.persist = None
        try:
            self.connection.deserialize(snapshot)
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA temp_store = MEMORY")
            self._initialize()
            from skyagent_manager.share_safety import RESTORE_BLOCK_KEY

            self.connection.execute(
                "INSERT OR REPLACE INTO settings(name, value) VALUES (?, ?)",
                (RESTORE_BLOCK_KEY, "restored-history-unverified"),
            )
            self.connection.commit()
            from skyagent_manager.inventory import Inventory

            Inventory(self).recover()
            self._persist()
        except Exception:
            self.connection.deserialize(original)
            self.connection.execute("PRAGMA foreign_keys = ON")
            raise
        finally:
            self.connection.persist = persist

    def _initialize(self) -> None:
        self._upgrade_stock_schema()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS stores (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS stores_name_active
                ON stores(lower(name)) WHERE archived = 0;

            CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL REFERENCES stores(id),
                display_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                phone_norm TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                authorized_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(store_id, phone)
            );

            CREATE TABLE IF NOT EXISTS benefits (
                id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL REFERENCES stores(id),
                member_id TEXT REFERENCES members(id) ON DELETE SET NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                code TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity >= 0),
                state TEXT NOT NULL DEFAULT '可用',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS benefits_store_code
                ON benefits(store_id, code) WHERE code <> '';
            CREATE INDEX IF NOT EXISTS benefits_store_state
                ON benefits(store_id, state, expires_at);

            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id TEXT NOT NULL REFERENCES stores(id),
                action TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(members)")
        }
        if "phone_norm" not in columns:
            self.connection.execute(
                "ALTER TABLE members ADD COLUMN phone_norm TEXT NOT NULL DEFAULT ''"
            )
        # Backfill old rows without merging records; legacy formatting collisions remain visible.
        for row in self.connection.execute(
            "SELECT id, phone FROM members WHERE phone_norm = ''"
        ):
            try:
                normalized = normalize_phone(row["phone"])
            except ValueError:
                normalized = row["phone"].strip()
            self.connection.execute(
                "UPDATE members SET phone_norm = ? WHERE id = ?",
                (normalized, row["id"]),
            )
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS members_store_phone_norm ON members(store_id, phone_norm);
            CREATE TRIGGER IF NOT EXISTS benefits_member_store_insert
            BEFORE INSERT ON benefits
            WHEN NEW.member_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM members WHERE id = NEW.member_id AND store_id = NEW.store_id
            )
            BEGIN SELECT RAISE(ABORT, 'benefit member must belong to the same store'); END;
            CREATE TRIGGER IF NOT EXISTS benefits_member_store_update
            BEFORE UPDATE OF member_id, store_id ON benefits
            WHEN NEW.member_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM members WHERE id = NEW.member_id AND store_id = NEW.store_id
            )
            BEGIN SELECT RAISE(ABORT, 'benefit member must belong to the same store'); END;
            CREATE TABLE IF NOT EXISTS store_sync (
                store_id TEXT PRIMARY KEY REFERENCES stores(id),
                api_url TEXT NOT NULL DEFAULT '',
                member_path TEXT NOT NULL DEFAULT '',
                benefit_path TEXT NOT NULL DEFAULT '',
                allow_http INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sync_results (
                store_id TEXT NOT NULL REFERENCES stores(id),
                entity TEXT NOT NULL CHECK(entity IN ('members', 'benefits')),
                record_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('success', 'failed')),
                summary TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(store_id, entity, record_id)
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL REFERENCES stores(id),
                label TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                phone_norm TEXT NOT NULL,
                token TEXT NOT NULL,
                is_new_user INTEGER CHECK(is_new_user IN (0,1) OR is_new_user IS NULL),
                note TEXT NOT NULL DEFAULT '',
                authorized_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(store_id, phone_norm),
                UNIQUE(store_id, token),
                UNIQUE(store_id, id)
            );
            CREATE TABLE IF NOT EXISTS account_import_results (
                store_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('success', 'failed')),
                summary TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(store_id, record_id),
                FOREIGN KEY(store_id, record_id) REFERENCES accounts(store_id, id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS stock (
                id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL REFERENCES stores(id),
                kind TEXT NOT NULL CHECK(kind IN ('silver','breakfast','room_upgrade','delayed_checkout','gift','invite','prop')),
                value TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','reserved','used')),
                UNIQUE(store_id,kind,value), UNIQUE(store_id,id)
            );
            CREATE TABLE IF NOT EXISTS local_tasks (
                id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL REFERENCES stores(id),
                state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed','canceled','interrupted')),
                created_at TEXT NOT NULL,
                UNIQUE(store_id,id)
            );
            CREATE TABLE IF NOT EXISTS local_task_items (
                store_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                stock_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved','used','released')),
                PRIMARY KEY(task_id,stock_id),
                FOREIGN KEY(store_id,task_id) REFERENCES local_tasks(store_id,id),
                FOREIGN KEY(store_id,stock_id) REFERENCES stock(store_id,id)
            );
            PRAGMA user_version = 6;
            """
        )
        self.connection.commit()

    def _upgrade_stock_schema(self) -> None:
        """Rebuild only the old stock CHECK, preserving IDs, rowids and task FKs.

        Startup already saved an encrypted pre-upgrade snapshot. Restore suppresses
        intermediate persistence and restores the original image on any failure.
        """
        if self.connection.execute("PRAGMA user_version").fetchone()[0] >= 6:
            return
        if not self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock'"
        ).fetchone():
            return
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.execute("BEGIN")
            self.connection.execute(
                "CREATE TABLE stock_upgrade_v6 ("
                "id TEXT PRIMARY KEY, store_id TEXT NOT NULL REFERENCES stores(id),"
                "kind TEXT NOT NULL CHECK(kind IN ('silver','breakfast','room_upgrade','delayed_checkout','gift','invite','prop')),"
                "value TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','reserved','used')),"
                "UNIQUE(store_id,kind,value), UNIQUE(store_id,id))"
            )
            self.connection.execute(
                "INSERT INTO stock_upgrade_v6(rowid,id,store_id,kind,value,state) "
                "SELECT rowid,id,store_id,kind,value,state FROM stock"
            )
            self.connection.execute("DROP TABLE stock")
            self.connection.execute("ALTER TABLE stock_upgrade_v6 RENAME TO stock")
            if self.connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("库存升级关联检查失败，未使用升级结果。")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def list_stores(self, include_archived: bool = False) -> list[sqlite3.Row]:
        where = "" if include_archived else "WHERE archived = 0"
        return list(
            self.connection.execute(
                f"SELECT * FROM stores {where} ORDER BY lower(name), created_at"
            )
        )

    def add_store(self, name: str) -> str:
        clean = name.strip()
        if not clean or len(clean) > 80:
            raise ValueError("门店名称不能为空，且不能超过 80 个字符。")
        store_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO stores(id, name, created_at) VALUES (?, ?, ?)",
                (store_id, clean, utc_now()),
            )
            self._activity_in_transaction(store_id, "添加门店", "已创建门店工作区")
        return store_id

    def archive_store(self, store_id: str) -> None:
        self._require_active_store(store_id)
        with self.connection:
            self.connection.execute(
                "UPDATE stores SET archived = 1 WHERE id = ?", (store_id,)
            )
            self._activity_in_transaction(store_id, "归档门店", "门店工作区已归档")

    def restore_store(self, store_id: str) -> None:
        row = self.connection.execute(
            "SELECT archived FROM stores WHERE id=?", (store_id,)
        ).fetchone()
        if row is None or not row["archived"]:
            raise ValueError("请选择已归档门店。")
        with self.connection:
            self.connection.execute(
                "UPDATE stores SET archived = 0 WHERE id = ?", (store_id,)
            )
            self._activity_in_transaction(store_id, "恢复门店", "已恢复门店工作区")

    def _require_active_store(self, store_id: str) -> None:
        if (
            self.connection.execute(
                "SELECT 1 FROM stores WHERE id=? AND archived=0", (store_id,)
            ).fetchone()
            is None
        ):
            raise ValueError("请选择未归档门店。")

    def _record(self, store_id: str, entity: str, record_id: str) -> sqlite3.Row:
        self._require_active_store(store_id)
        if entity not in {"members", "benefits"}:
            raise ValueError("记录类型无效。")
        row = self.connection.execute(
            f"SELECT * FROM {entity} WHERE store_id=? AND id=?", (store_id, record_id)
        ).fetchone()
        if row is None:
            raise ValueError("当前门店下找不到该记录。")
        return row

    def get_member(self, store_id: str, member_id: str) -> sqlite3.Row:
        return self._record(store_id, "members", member_id)

    def get_benefit(self, store_id: str, benefit_id: str) -> sqlite3.Row:
        return self._record(store_id, "benefits", benefit_id)

    def _member_fields(self, name: str, phone: str, note: str) -> tuple:
        return (
            clean_text(name, "姓名/标记", 128),
            clean_text(phone, "手机号", 64, required=True),
            normalize_phone(phone),
            clean_text(note, "备注", 2000),
        )

    def _check_member_duplicate(
        self, store_id: str, phone_norm: str, excluding: str = ""
    ) -> None:
        if self.connection.execute(
            "SELECT 1 FROM members WHERE store_id = ? AND phone_norm = ? AND id<>? LIMIT 1",
            (store_id, phone_norm, excluding),
        ).fetchone():
            raise ValueError("该手机号已存在于此门店。")

    def _invalidate_sync(self, store_id: str, entity: str, record_id: str) -> None:
        self.connection.execute(
            "DELETE FROM sync_results WHERE store_id=? AND entity=? AND record_id=?",
            (store_id, entity, record_id),
        )

    def _activity_in_transaction(
        self, store_id: str, action: str, summary: str
    ) -> None:
        self.connection.execute(
            "INSERT INTO activity(store_id, action, summary, created_at) VALUES (?, ?, ?, ?)",
            (store_id, action, summary, utc_now()),
        )

    def add_member(self, store_id: str, name: str, phone: str, note: str = "") -> str:
        self._require_active_store(store_id)
        name, clean_phone, phone_norm, note = self._member_fields(name, phone, note)
        self._check_member_duplicate(store_id, phone_norm)
        now = utc_now()
        member_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """INSERT INTO members(id, store_id, display_name, phone, phone_norm, note, authorized_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    member_id,
                    store_id,
                    name,
                    clean_phone,
                    phone_norm,
                    note,
                    now,
                    now,
                ),
            )
        return member_id

    def update_member(
        self, store_id: str, member_id: str, name: str, phone: str, note: str = ""
    ) -> None:
        current = self.get_member(store_id, member_id)
        name, phone, phone_norm, note = self._member_fields(name, phone, note)
        if phone_norm != current["phone_norm"]:
            self._check_member_duplicate(store_id, phone_norm, excluding=member_id)
        with self.connection:
            self.connection.execute(
                "UPDATE members SET display_name=?, phone=?, phone_norm=?, note=?, authorized_at=? WHERE store_id=? AND id=?",
                (name, phone, phone_norm, note, utc_now(), store_id, member_id),
            )
            self._invalidate_sync(store_id, "members", member_id)
            self._activity_in_transaction(
                store_id, "编辑会员", f"已更新会员记录 {member_id}"
            )

    def delete_member(self, store_id: str, member_id: str) -> None:
        self.get_member(store_id, member_id)
        with self.connection:
            self.connection.execute(
                "DELETE FROM sync_results WHERE store_id=? AND entity='benefits' AND record_id IN (SELECT id FROM benefits WHERE store_id=? AND member_id=?)",
                (store_id, store_id, member_id),
            )
            cursor = self.connection.execute(
                "UPDATE benefits SET member_id=NULL, updated_at=? WHERE store_id=? AND member_id=?",
                (utc_now(), store_id, member_id),
            )
            self.connection.execute(
                "DELETE FROM members WHERE store_id=? AND id=?", (store_id, member_id)
            )
            self._invalidate_sync(store_id, "members", member_id)
            self._activity_in_transaction(
                store_id,
                "删除会员",
                f"已删除记录 {member_id}，保留并解除关联 {cursor.rowcount} 条权益",
            )

    def import_members(
        self, store_id: str, rows: Iterable[dict[str, str]]
    ) -> tuple[int, int]:
        report = self.import_members_detailed(store_id, rows)
        return report.inserted, report.skipped

    def import_members_detailed(
        self, store_id: str, rows: Iterable[dict]
    ) -> ImportReport:
        self._require_active_store(store_id)
        report = ImportReport([])
        now = utc_now()
        with self.connection:
            for position, row in enumerate(rows, start=2):
                line = getattr(row, "line_number", position)
                try:
                    if None in row:
                        raise ValueError("此行字段数多于表头。")
                    name, phone, normalized, note = self._member_fields(
                        row_text(row, "name"),
                        row_text(row, "phone"),
                        row_text(row, "note"),
                    )
                    self._check_member_duplicate(store_id, normalized)
                except ValueError as exc:
                    report.rows.append(ImportRowResult(line, False, str(exc)))
                    continue
                self.connection.execute(
                    "INSERT INTO members(id, store_id, display_name, phone, phone_norm, note, authorized_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        store_id,
                        name,
                        phone,
                        normalized,
                        note,
                        now,
                        now,
                    ),
                )
                report.rows.append(ImportRowResult(line, True, "已导入"))
            self._activity_in_transaction(
                store_id,
                "导入会员",
                f"新增 {report.inserted} 条，跳过 {report.skipped} 条",
            )
        return report

    def list_members(
        self, store_id: str, *, search: str = "", duplicates_only: bool = False
    ) -> list[sqlite3.Row]:
        clauses, parameters = ["m.store_id=?"], [store_id]
        if search.strip():
            pattern = search_pattern(search.strip())
            clauses.append(
                "(m.display_name LIKE ? ESCAPE '\\' OR m.phone LIKE ? ESCAPE '\\' OR m.phone_norm LIKE ? ESCAPE '\\' OR m.note LIKE ? ESCAPE '\\')"
            )
            digits = "".join(ch for ch in search if ch.isdigit())
            phone_pattern = (
                search_pattern(digits)
                if digits and all(ch in "+0123456789 ()-" for ch in search.strip())
                else pattern
            )
            parameters.extend([pattern, pattern, phone_pattern, pattern])
        if duplicates_only:
            clauses.append(
                "EXISTS (SELECT 1 FROM members other WHERE other.store_id=m.store_id AND other.phone_norm=m.phone_norm AND other.id<>m.id)"
            )
        return list(
            self.connection.execute(
                "SELECT m.* FROM members m WHERE "
                + " AND ".join(clauses)
                + " ORDER BY m.created_at DESC, m.id",
                parameters,
            )
        )

    def _resolve_member(
        self, store_id: str, phone: str, preferred_member_id: str | None = None
    ) -> str | None:
        if not phone.strip():
            return None
        normalized = normalize_phone(phone)
        rows = list(
            self.connection.execute(
                "SELECT id FROM members WHERE store_id=? AND phone_norm=?",
                (store_id, normalized),
            )
        )
        if preferred_member_id and any(
            row["id"] == preferred_member_id for row in rows
        ):
            return preferred_member_id
        if not rows:
            raise ValueError("找不到该门店下对应的会员手机号。")
        if len(rows) > 1:
            raise ValueError("手机号对应多个历史会员，请先处理重复记录。")
        return rows[0]["id"]

    def _benefit_fields(
        self,
        store_id: str,
        kind: str,
        name: str,
        code: str,
        expires_at: str,
        quantity: int,
        note: str,
        member_phone: str,
        state: str,
        preferred_member_id: str | None = None,
    ) -> tuple:
        if kind not in BENEFIT_KIND_LABELS:
            raise ValueError("权益类型无效。")
        if state not in BENEFIT_STATES:
            raise ValueError("权益状态无效。")
        return (
            kind,
            clean_text(name, "权益名称", 128, required=True),
            clean_text(code, "券码", 256),
            clean_date(expires_at),
            clean_quantity(quantity),
            clean_text(note, "备注", 2000),
            self._resolve_member(store_id, member_phone, preferred_member_id),
            state,
        )

    def _check_code_duplicate(
        self, store_id: str, code: str, excluding: str = ""
    ) -> None:
        if (
            code
            and self.connection.execute(
                "SELECT 1 FROM benefits WHERE store_id=? AND code=? AND id<>?",
                (store_id, code, excluding),
            ).fetchone()
        ):
            raise ValueError("该券码已存在于此门店。")

    def add_benefit(
        self,
        store_id: str,
        kind: str,
        name: str,
        code: str = "",
        expires_at: str = "",
        quantity: int = 1,
        note: str = "",
        member_phone: str = "",
    ) -> str:
        self._require_active_store(store_id)
        kind, name, code, expires_at, quantity, note, member_id, _ = (
            self._benefit_fields(
                store_id,
                kind,
                name,
                code,
                expires_at,
                quantity,
                note,
                member_phone,
                "可用",
            )
        )
        self._check_code_duplicate(store_id, code)
        now = utc_now()
        benefit_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """INSERT INTO benefits
                   (id, store_id, member_id, kind, name, code, expires_at, quantity, state, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, '可用', ?, ?, ?)""",
                (
                    benefit_id,
                    store_id,
                    member_id,
                    kind,
                    name,
                    code,
                    expires_at,
                    quantity,
                    note,
                    now,
                    now,
                ),
            )
        return benefit_id

    def update_benefit(
        self,
        store_id: str,
        benefit_id: str,
        kind: str,
        name: str,
        code: str = "",
        expires_at: str = "",
        quantity: int = 1,
        note: str = "",
        member_phone: str = "",
        state: str = "可用",
    ) -> None:
        current = self.get_benefit(store_id, benefit_id)
        fields = self._benefit_fields(
            store_id,
            kind,
            name,
            code,
            expires_at,
            quantity,
            note,
            member_phone,
            state,
            current["member_id"],
        )
        self._check_code_duplicate(store_id, fields[2], excluding=benefit_id)
        with self.connection:
            self.connection.execute(
                "UPDATE benefits SET kind=?, name=?, code=?, expires_at=?, quantity=?, note=?, member_id=?, state=?, updated_at=? WHERE store_id=? AND id=?",
                (*fields, utc_now(), store_id, benefit_id),
            )
            self._invalidate_sync(store_id, "benefits", benefit_id)
            self._activity_in_transaction(
                store_id, "编辑权益", f"已更新权益记录 {benefit_id}"
            )

    def delete_benefit(self, store_id: str, benefit_id: str) -> None:
        self.get_benefit(store_id, benefit_id)
        with self.connection:
            self.connection.execute(
                "DELETE FROM benefits WHERE store_id=? AND id=?", (store_id, benefit_id)
            )
            self._invalidate_sync(store_id, "benefits", benefit_id)
            self._activity_in_transaction(
                store_id, "删除权益", f"已删除权益记录 {benefit_id}"
            )

    def import_benefits(
        self, store_id: str, rows: Iterable[dict[str, str]]
    ) -> tuple[int, int]:
        report = self.import_benefits_detailed(store_id, rows)
        return report.inserted, report.skipped

    def import_benefits_detailed(
        self, store_id: str, rows: Iterable[dict]
    ) -> ImportReport:
        self._require_active_store(store_id)
        report = ImportReport([])
        now = utc_now()
        with self.connection:
            for position, row in enumerate(rows, start=2):
                line = getattr(row, "line_number", position)
                try:
                    if None in row:
                        raise ValueError("此行字段数多于表头。")
                    kind_text = row_text(row, "kind") or "其他"
                    kind = BENEFIT_KINDS.get(kind_text, kind_text)
                    try:
                        quantity = int(row_text(row, "quantity", "1") or "1")
                    except ValueError:
                        raise ValueError("数量必须为整数。") from None
                    fields = self._benefit_fields(
                        store_id,
                        kind,
                        row_text(row, "name"),
                        row_text(row, "code"),
                        row_text(row, "expires_at"),
                        quantity,
                        row_text(row, "note"),
                        row_text(row, "phone"),
                        "可用",
                    )
                    self._check_code_duplicate(store_id, fields[2])
                except ValueError as exc:
                    report.rows.append(ImportRowResult(line, False, str(exc)))
                    continue
                kind, name, code, expires_at, quantity, note, member_id, state = fields
                self.connection.execute(
                    "INSERT INTO benefits(id, store_id, member_id, kind, name, code, expires_at, quantity, state, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        store_id,
                        member_id,
                        kind,
                        name,
                        code,
                        expires_at,
                        quantity,
                        state,
                        note,
                        now,
                        now,
                    ),
                )
                report.rows.append(ImportRowResult(line, True, "已导入"))
            self._activity_in_transaction(
                store_id,
                "导入权益",
                f"新增 {report.inserted} 条，跳过 {report.skipped} 条",
            )
        return report

    def list_benefits(
        self,
        store_id: str,
        *,
        search: str = "",
        state: str = "",
        kind: str = "",
        expiry: str = "all",
        today: date | None = None,
    ) -> list[sqlite3.Row]:
        if state and state not in BENEFIT_STATES:
            raise ValueError("权益状态无效。")
        if kind and kind not in BENEFIT_KIND_LABELS:
            raise ValueError("权益类型无效。")
        if expiry not in {"all", "past", "week", "none"}:
            raise ValueError("到期筛选无效。")
        clauses, parameters = ["b.store_id=?"], [store_id]
        if state:
            clauses.append("b.state=?")
            parameters.append(state)
        if kind:
            clauses.append("b.kind=?")
            parameters.append(kind)
        if search.strip():
            clauses.append(
                "(b.name LIKE ? ESCAPE '\\' OR b.code LIKE ? ESCAPE '\\' OR b.note LIKE ? ESCAPE '\\' OR m.display_name LIKE ? ESCAPE '\\' OR m.phone_norm LIKE ? ESCAPE '\\')"
            )
            parameters.extend([search_pattern(search.strip())] * 5)
        rows = list(
            self.connection.execute(
                """SELECT b.*, m.display_name AS member_name, m.phone AS member_phone
               FROM benefits b LEFT JOIN members m ON m.id = b.member_id
               WHERE """
                + " AND ".join(clauses)
                + " ORDER BY b.expires_at, b.created_at DESC, b.id",
                parameters,
            )
        )
        if expiry == "all":
            return rows
        if expiry == "none":
            return [row for row in rows if not row["expires_at"]]
        today = today or date.today()
        filtered = []
        for row in rows:
            try:
                expires = date.fromisoformat(clean_date(row["expires_at"]))
            except ValueError:
                continue
            if (expiry == "past" and expires < today) or (
                expiry == "week" and today <= expires <= today + timedelta(days=7)
            ):
                filtered.append(row)
        return filtered

    def set_benefit_state(
        self, benefit_id: str, state: str, *, store_id: str | None = None
    ) -> None:
        if state not in BENEFIT_STATES:
            raise ValueError("权益状态无效。")
        if store_id is None:
            row = self.connection.execute(
                "SELECT store_id FROM benefits WHERE id=?", (benefit_id,)
            ).fetchone()
            if row is None:
                raise ValueError("找不到该权益记录。")
            store_id = row["store_id"]
        self.get_benefit(store_id, benefit_id)
        with self.connection:
            self.connection.execute(
                "UPDATE benefits SET state = ?, updated_at = ? WHERE store_id=? AND id = ?",
                (state, utc_now(), store_id, benefit_id),
            )
            self._invalidate_sync(store_id, "benefits", benefit_id)
            self._activity_in_transaction(
                store_id, "更改权益状态", f"记录 {benefit_id} 的状态设为“{state}”"
            )

    def get_setting(self, name: str, default: str = "") -> str:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE name=?", (name,)
        ).fetchone()
        return row["value"] if row else default

    def set_backup_settings(self, enabled: bool, keep: int) -> None:
        if not 1 <= keep <= 90:
            raise ValueError("备份保留份数应为 1 到 90。")
        with self.connection:
            self.connection.executemany(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                [
                    ("backup_enabled", "1" if enabled else "0"),
                    ("backup_keep", str(keep)),
                ],
            )

    def remember_store(self, store_id: str) -> None:
        if self.get_setting("last_store_id") == store_id:
            return
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                ("last_store_id", store_id),
            )

    def get_store_sync(self, store_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM store_sync WHERE store_id=?", (store_id,)
        ).fetchone()
        return (
            dict(row)
            if row
            else {
                "api_url": "",
                "member_path": "",
                "benefit_path": "",
                "allow_http": False,
            }
        )

    def configure_store(self, store_id: str, name: str, config: dict) -> None:
        self._require_active_store(store_id)
        if not name.strip() or len(name.strip()) > 80:
            raise ValueError("门店名称不能为空，且不能超过 80 个字符。")
        with self.connection:
            self.connection.execute(
                "UPDATE stores SET name=? WHERE id=?", (name.strip(), store_id)
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO store_sync VALUES (?, ?, ?, ?, ?)",
                (
                    store_id,
                    config["api_url"],
                    config["member_path"],
                    config["benefit_path"],
                    int(config["allow_http"]),
                ),
            )
            self.connection.execute(
                "DELETE FROM sync_results WHERE store_id=?", (store_id,)
            )
            self.connection.execute(
                "DELETE FROM account_import_results WHERE store_id=?", (store_id,)
            )
            self._activity_in_transaction(store_id, "门店设置", "已更新门店和同步配置")

    def sync_items(
        self, store_id: str, entity: str, *, failed_only: bool = False
    ) -> list[dict]:
        store = next((s for s in self.list_stores() if s["id"] == store_id), None)
        if store is None:
            raise ValueError("请选择未归档门店。")
        if entity not in {"members", "benefits"}:
            raise ValueError("同步对象无效。")
        rows = (
            self.list_members(store_id)
            if entity == "members"
            else self.list_benefits(store_id)
        )
        failed = {
            r["record_id"]
            for r in self.list_sync_results(store_id)
            if r["entity"] == entity and r["state"] == "failed"
        }
        items = []
        for row in rows:
            if failed_only and row["id"] not in failed:
                continue
            if entity == "members":
                item = {
                    "id": row["id"],
                    "name": row["display_name"],
                    "phone": row["phone_norm"],
                    "note": row["note"],
                }
            else:
                item = {
                    field: row[field]
                    for field in (
                        "id",
                        "member_id",
                        "kind",
                        "name",
                        "code",
                        "expires_at",
                        "quantity",
                        "state",
                        "note",
                    )
                }
            items.append(item)
        return items

    def save_sync_result(
        self, store_id: str, entity: str, record_id: str, ok: bool, summary: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO sync_results VALUES (?, ?, ?, ?, ?, ?)",
                (
                    store_id,
                    entity,
                    record_id,
                    "success" if ok else "failed",
                    summary[:100],
                    utc_now(),
                ),
            )

    def save_sync_results(self, store_id: str, entity: str, results: list) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT OR REPLACE INTO sync_results VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        store_id,
                        entity,
                        r.record_id,
                        "success" if r.ok else "failed",
                        r.summary[:100],
                        utc_now(),
                    )
                    for r in results
                ],
            )

    def list_sync_results(self, store_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM sync_results WHERE store_id=? ORDER BY updated_at DESC",
                (store_id,),
            )
        )

    def add_activity(self, store_id: str, action: str, summary: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO activity(store_id, action, summary, created_at) VALUES (?, ?, ?, ?)",
                (store_id, action, summary[:300], utc_now()),
            )

    def list_activity(self, store_id: str, limit: int = 500) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM activity WHERE store_id = ? ORDER BY id DESC LIMIT ?",
                (store_id, limit),
            )
        )

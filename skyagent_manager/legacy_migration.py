"""Read-only, bounded legacy workspace parsing and atomic confirmed migration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backup import BackupService
from skyagent_manager.db import mask_code, mask_phone
from skyagent_manager.imports import ImportReport, ImportRowResult

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 20000
FILES = ("params_register.json", "results_register.json")
KIND_MAP = {
    "silver": "silver",
    "breakfast": "breakfast",
    "upgrade": "room_upgrade",
    "delay": "delayed_checkout",
    "gift": "gift",
}


@dataclass(frozen=True)
class MigrationRow:
    source: str
    number: int
    kind: str
    value: object = field(repr=False)
    state: str = "available"
    note: str = field(default="", repr=False)

    def masked(self):
        if self.kind == "account":
            return f"{mask_phone(self.value.phone)} / {mask_code(self.value.token)}"
        return mask_code(self.value)


@dataclass(frozen=True)
class MigrationPlan:
    directory: Path
    fingerprints: tuple[tuple[str, str], ...]
    rows: tuple[MigrationRow, ...] = field(repr=False)
    skipped: tuple[ImportRowResult, ...]
    warnings: tuple[str, ...]
    source_bytes: int = 0
    directory_identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class WorkspaceBatchPlan:
    root: Path
    plans: tuple[MigrationPlan, ...] = field(repr=False)
    root_identity: tuple[int, int] | None = None


def _directory_identity(directory):
    try:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.resolve() != directory:
            raise ValueError()
        return info.st_dev, info.st_ino
    except (OSError, ValueError, RuntimeError):
        raise ValueError("旧工作区目录已变化或不是原真实目录，请重新预览。") from None


def _read(directory, filename):
    path = directory / filename
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("迁移源必须是普通文件，不能是链接或设备。")
    except FileNotFoundError:
        return None
    if path.is_symlink():
        raise ValueError("迁移源文件不能是符号链接。")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("无法安全读取旧 JSON 文件。") from None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("迁移源必须是普通文件。")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("每个旧 JSON 文件最多 20 MiB。")
    return data


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("旧 JSON 存在重复键，无法确定内容。")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("JSON 非有限数值无效。")


def _decode(data):
    try:
        return json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("旧 JSON 编码、格式或结构无效；未修改源文件。") from None


def preview_workspace(directory: Path) -> MigrationPlan:
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("请选择一个旧门店工作区的真实目录。")
    directory = directory.resolve()
    identity = _directory_identity(directory)
    rows, skipped, fingerprints, warnings = [], [], [], []
    documents = {}
    source_bytes = 0
    for filename in FILES:
        data = _read(directory, filename)
        fingerprints.append(
            (
                filename,
                hashlib.sha256(data).hexdigest() if data is not None else "missing",
            )
        )
        if data is not None:
            source_bytes += len(data)
            documents[filename] = _decode(data)
    if not documents:
        raise ValueError(
            "目录没有 params_register.json 或 results_register.json；请选择具体旧门店目录，不是多门店根目录。"
        )

    def append(source, number, kind, value, state="available", note=""):
        if len(rows) + len(skipped) >= MAX_ROWS:
            raise ValueError("单次迁移最多 20000 条记录。")
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            skipped.append(
                ImportRowResult(number, False, f"{source}：值为空或格式无效")
            )
            return
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("旧邀请码备注格式无效或超过 2000 字符。")
        rows.append(
            MigrationRow(source, number, kind, value.strip(), state, note.strip())
        )

    params = documents.get("params_register.json", {})
    if not isinstance(params, dict):
        raise ValueError("params_register.json 必须是对象。")
    inventories = params.get("inventories")
    if inventories is None:
        gift = params.get("share_codes", [])
        if isinstance(gift, str):
            gift = [value.strip() for value in gift.split(",") if value.strip()]
        inventories = {
            "silver": params.get("silver_card_pool", []),
            "breakfast": params.get("breakfast_pool", []),
            "upgrade": params.get("upgrade_pool", []),
            "delay": params.get("delay_pool", []),
            "gift": gift,
        }
    if not isinstance(inventories, dict):
        raise ValueError("旧资源库存必须是对象。")
    for old_kind, kind in KIND_MAP.items():
        items = inventories.get(old_kind, [])
        if not isinstance(items, list) or len(items) > MAX_ROWS:
            raise ValueError("旧资源列表类型无效或超过上限。")
        for number, item in enumerate(items, 1):
            source = f"inventories.{old_kind}"
            value, status = item, ""
            if isinstance(item, dict):
                value = item.get("value") or item.get("url") or item.get("code")
                status = item.get("status", "")
            if not isinstance(status, str):
                raise ValueError("旧资源状态格式无效。")
            if status and not (
                status.startswith("√")
                or "成功" in status
                or "已用" in status
                or status in {"可用", "未使用", "失败", "领取失败×"}
            ):
                if len(rows) + len(skipped) >= MAX_ROWS:
                    raise ValueError("单次迁移最多 20000 条记录。")
                skipped.append(
                    ImportRowResult(number, False, f"{source}：状态无法安全映射，跳过")
                )
                continue
            used = status.startswith("√") or "成功" in status or "已用" in status
            append(source, number, kind, value, "used" if used else "available")
    library = params.get("invite_code_library", [])
    if not isinstance(library, list) or len(library) > MAX_ROWS:
        raise ValueError("旧邀请码库类型无效或超过上限。")
    for number, item in enumerate(library, 1):
        append(
            "invite_code_library",
            number,
            "invite",
            item.get("code") if isinstance(item, dict) else None,
            note=(item.get("note") or item.get("label") or "")
            if isinstance(item, dict)
            else "",
        )
    if not library and params.get("invite_code"):
        append("invite_code", 1, "invite", params["invite_code"])
    accounts = documents.get("results_register.json", [])
    if not isinstance(accounts, list) or len(accounts) > MAX_ROWS:
        raise ValueError("results_register.json 必须是账号行列表，最多 20000 行。")
    for number, row in enumerate(accounts, 1):
        if len(rows) + len(skipped) >= MAX_ROWS:
            raise ValueError("单次迁移最多 20000 条记录。")
        try:
            if (
                not isinstance(row, list)
                or len(row) not in {3, 4, 5, 6, 8, 9, 10, 11}
                or any(not isinstance(value, str) for value in row)
            ):
                raise ValueError()
            account = AccountInput(
                row[1],
                row[2],
                row[0],
                "旧 JSON 迁移；历史业务结果未作为当前执行或同步结果",
                None,
            )
            account.validated()
        except ValueError:
            skipped.append(
                ImportRowResult(
                    number,
                    False,
                    "results_register.json：账号行结构或手机号/Token 无效",
                )
            )
            continue
        rows.append(MigrationRow("results_register.json", number, "account", account))
    warnings.extend(
        [
            "仅迁移账号、五类库存及邀请码；不创建门店，不覆盖目标配置。",
            "不迁移 Secret、密码、代理、自动执行配置、历史权益结果或历史入库状态。账号新用户标记为未知，上传前需确认。",
            "邀请码备注仅随新增邀请码迁移；重复项不覆盖备注，旧多选状态不迁移，保留目标门店现有选择；未知资源状态跳过。",
            "重复项跳过；相同资源状态冲突或手机号/Token 对应冲突会中止整个事务。",
        ]
    )
    if set(inventories) - set(KIND_MAP):
        warnings.append("发现不支持的库存类型，未迁移。")
    if _directory_identity(directory) != identity:
        raise ValueError("旧工作区目录在预览期间已变化，请重新预览。")
    return MigrationPlan(
        directory,
        tuple(fingerprints),
        tuple(rows),
        tuple(skipped),
        tuple(warnings),
        source_bytes,
        identity,
    )


def _validate_plan(plan):
    if _directory_identity(plan.directory) != plan.directory_identity:
        raise ValueError("旧工作区目录在预览后已被替换，请重新预览；未导入。")
    for filename, expected in plan.fingerprints:
        data = _read(plan.directory, filename)
        actual = hashlib.sha256(data).hexdigest() if data is not None else "missing"
        if actual != expected:
            raise ValueError("旧文件在预览后发生变化，请重新预览；未导入。")
    if _directory_identity(plan.directory) != plan.directory_identity:
        raise ValueError("旧工作区目录在复核期间已变化，请重新预览；未导入。")


def _migration_backup(database):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    backup = BackupService(database)
    return backup.create(backup.directory / f"before-json-migration-{stamp}.skybackup")


def _apply_rows(database, store_id, plan):
    report = list(plan.skipped)
    connection = database.connection
    accounts = Accounts(database)
    for row in plan.rows:
        inserted = False
        if row.kind == "account":
            _, phone, token, *_ = row.value.validated()
            existing = connection.execute(
                "SELECT phone_norm,token FROM accounts WHERE store_id=? AND (phone_norm=? OR token=?)",
                (store_id, phone, token),
            ).fetchall()
            if existing and (
                len(existing) != 1
                or existing[0]["phone_norm"] != phone
                or existing[0]["token"] != token
            ):
                raise ValueError(
                    "账号手机号与 Token 对应关系冲突，迁移已回滚；请审阅目标账号。"
                )
            if not existing:
                accounts._add(store_id, row.value)
                inserted = True
        else:
            existing = connection.execute(
                "SELECT state FROM stock WHERE store_id=? AND kind=? AND value=?",
                (store_id, row.kind, row.value),
            ).fetchone()
            if existing and existing["state"] != row.state:
                raise ValueError("同值资源状态冲突，迁移已回滚；不会覆盖目标状态。")
            if not existing:
                stock_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stock VALUES(?,?,?,?,?)",
                    (stock_id, store_id, row.kind, row.value, row.state),
                )
                if row.kind == "invite" and row.note:
                    connection.execute(
                        "INSERT INTO settings(name,value) VALUES(?,?)",
                        (
                            f"invite_note/{store_id}/{stock_id}",
                            row.note,
                        ),
                    )
                inserted = True
        report.append(
            ImportRowResult(
                row.number,
                inserted,
                f"{row.source}：" + ("已迁移" if inserted else "重复，未覆盖"),
            )
        )
    result = ImportReport(report)
    database._activity_in_transaction(
        store_id,
        "旧 JSON 迁移",
        f"新增 {result.inserted} 条，跳过 {result.skipped} 条；源文件未修改",
    )
    return result


def apply_plan(database, store_id, plan: MigrationPlan, *, authorized: bool = False):
    if not authorized:
        raise ValueError("需确认授权与目标门店后才能迁移。")
    database._require_active_store(store_id)
    _validate_plan(plan)
    if not plan.rows:
        raise ValueError("没有可以迁移的记录。")
    safety = _migration_backup(database)
    with database.connection:
        result = _apply_rows(database, store_id, plan)
    return result, safety


def _workspace_paths(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("请选择真实的多门店根目录，不接受符号链接。")
    root = root.resolve()
    candidates = []
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, 1):
            if count > 1000:
                raise ValueError("根目录最多检查1000个直接子项，不递归扫描。")
            if entry.is_dir(follow_symlinks=False):
                directory = Path(entry.path)
                if any(
                    (directory / filename).exists()
                    or (directory / filename).is_symlink()
                    for filename in FILES
                ):
                    candidates.append(directory)
                    if len(candidates) > 50:
                        raise ValueError("单次最多迁移50个旧门店目录。")
    return root, tuple(sorted(candidates))


def preview_workspaces(root):
    root, candidates = _workspace_paths(root)
    identity = _directory_identity(root)
    if not candidates:
        raise ValueError("未发现直接子目录中的旧门店文件；单门店请用迁移旧JSON。")
    plans = []
    for directory in candidates:
        plans.append(preview_workspace(directory))
        if sum(len(plan.rows) + len(plan.skipped) for plan in plans) > MAX_ROWS:
            raise ValueError("多门店合计最多20000条记录。")
        if sum(plan.source_bytes for plan in plans) > 2 * MAX_BYTES:
            raise ValueError("多门店源文件合计最多40 MiB。")
    if _directory_identity(root) != identity:
        raise ValueError("旧工作区根目录在预览期间已变化，请重新预览。")
    return WorkspaceBatchPlan(root, tuple(plans), identity)


def apply_workspace_batch(database, batch, mappings, *, authorized=False):
    if authorized is not True:
        raise ValueError("需明确授权及每个目标门店映射。")
    mappings = tuple(mappings)
    if not mappings or len(mappings) > len(batch.plans):
        raise ValueError("请选择至少一个旧门店映射。")
    indices, targets = set(), set()
    for index, store_id in mappings:
        if (
            type(index) is not int
            or not 0 <= index < len(batch.plans)
            or index in indices
            or store_id in targets
        ):
            raise ValueError("源目录和目标门店必须一对一，不自动合并。")
        indices.add(index)
        targets.add(store_id)
        database._require_active_store(store_id)
        if not batch.plans[index].rows:
            raise ValueError("选中旧门店没有可迁移记录，请选择跳过。")
    if _directory_identity(batch.root) != batch.root_identity:
        raise ValueError("旧工作区根目录在预览后已被替换，请重新预览。")
    _, candidates = _workspace_paths(batch.root)
    if candidates != tuple(plan.directory for plan in batch.plans):
        raise ValueError("旧门店目录列表已变化，请重新预览。")
    for plan in batch.plans:
        _validate_plan(plan)
    safety = _migration_backup(database)
    reports = []
    with database.connection:
        for index, store_id in mappings:
            report = _apply_rows(database, store_id, batch.plans[index])
            reports.extend(
                ImportRowResult(
                    row.row_number, row.imported, f"工作区{index + 1}：{row.reason}"
                )
                for row in report.rows
            )
    return ImportReport(reports), safety

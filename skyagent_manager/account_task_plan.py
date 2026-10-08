"""Offline account operation planning; no execution, credentials or uploads."""

from dataclasses import dataclass

OPERATIONS = {
    "register": "新用户注册",
    "reward": "奖励金",
    "silver": "银卡",
    "breakfast": "早餐券",
    "upgrade": "升房券",
    "delay": "延迟券",
    "gift": "订房礼包",
}
RESOURCE_KINDS = {
    "silver": "silver",
    "breakfast": "breakfast",
    "upgrade": "room_upgrade",
    "delay": "delayed_checkout",
    "gift": "gift",
}


@dataclass(frozen=True)
class OperationProgress:
    completed: int = 0
    unknown: bool = False


@dataclass(frozen=True)
class OperationPlan:
    operation: str
    target: int
    completed: int
    remaining: int
    state: str
    available: int | None = None
    shortage: int | None = None


@dataclass(frozen=True)
class AccountTaskPlan:
    route: str
    operations: tuple[OperationPlan, ...]
    executable: bool = False


def plan_account_task(
    *,
    has_token,
    task="all",
    breakfast_count=1,
    delay_count=1,
    progress=None,
    stock=None,
):
    """Unknown external results block repeats; counts never authorize execution.

    Original controller rerun behavior is missing. These conservative rules are
    explicit new design, not a claim of byte-for-byte original equivalence.
    """
    if (
        type(has_token) is not bool
        or not isinstance(task, str)
        or task not in {*OPERATIONS, "all"}
    ):
        raise ValueError("账号状态或任务选择无效。")
    if any(
        type(count) is not int or count not in {1, 2}
        for count in (breakfast_count, delay_count)
    ):
        raise ValueError("早餐及延迟数量必须为 1 或 2。")
    progress = {} if progress is None else progress
    if not isinstance(progress, dict) or set(progress) - OPERATIONS.keys():
        raise ValueError("任务历史结构无效。")
    for value in progress.values():
        if (
            not isinstance(value, OperationProgress)
            or type(value.completed) is not int
            or not 0 <= value.completed <= 2
            or type(value.unknown) is not bool
        ):
            raise ValueError("任务历史数量或状态无效。")
    if stock is not None and (
        not isinstance(stock, dict)
        or set(stock) - set(RESOURCE_KINDS.values())
        or any(
            type(value) is not int or not 0 <= value <= 1000000
            for value in stock.values()
        )
    ):
        raise ValueError("可用库存数量无效。")
    selected = tuple(OPERATIONS) if task == "all" else (task,)
    route = "register" if "register" in selected and not has_token else "claim"
    registration = progress.get("register", OperationProgress())
    registration_blocked = route == "register" and (
        registration.unknown or registration.completed > 0
    )
    plans = []
    for operation in selected:
        target = (
            breakfast_count
            if operation == "breakfast"
            else delay_count
            if operation == "delay"
            else 1
        )
        history = progress.get(operation, OperationProgress())
        remaining = max(0, target - history.completed)
        resource = RESOURCE_KINDS.get(operation)
        available = stock.get(resource, 0) if stock is not None and resource else None
        shortage = max(0, remaining - available) if available is not None else None
        if history.unknown:
            state = "待核对，禁止重放"
        elif operation == "register" and has_token:
            state = "已有 Token，跳过注册"
            remaining = 0
        elif remaining == 0:
            state = "已完成，跳过"
        elif registration_blocked:
            state = "注册结果或 Token 待核对，阻断"
        elif route == "claim" and not has_token:
            state = "缺少 Token，阻断"
        elif shortage:
            state = "可用资源不足，阻断"
        elif route == "register" and operation != "register":
            state = "等待注册及 Token，仅计划"
        else:
            state = "待接授权接口，仅计划"
        plans.append(
            OperationPlan(
                operation,
                target,
                history.completed,
                remaining,
                state,
                available,
                shortage,
            )
        )
    return AccountTaskPlan(route, tuple(plans))


def available_stock_counts(database, store_id):
    """Store-isolated counts only; never fetch raw stock values or reserve items."""
    return {
        row["kind"]: row["total"]
        for row in database.connection.execute(
            "SELECT kind,count(*) AS total FROM stock WHERE store_id=? AND state='available' GROUP BY kind",
            (store_id,),
        )
        if row["kind"] in RESOURCE_KINDS.values()
    }


@dataclass(frozen=True)
class BatchTaskPlan:
    accounts: tuple[AccountTaskPlan, ...]
    demand: tuple[tuple[str, int], ...]
    shortages: tuple[tuple[str, int], ...]
    executable: bool = False


def plan_account_batch(
    *, token_flags, stock, task="all", breakfast_count=1, delay_count=1
):
    """Aggregate demands once, not once-per-account stock availability.

    Includes hypothetical claims after registration, excludes token-blocked
    direct claims. No account IDs, raw tokens or resource values are needed.
    """
    if (
        not isinstance(token_flags, (tuple, list))
        or not 1 <= len(token_flags) <= 200
        or any(type(flag) is not bool for flag in token_flags)
    ):
        raise ValueError("批量计划需 1 至 200 个账号状态。")
    # Validate even if no selected operation requires stock.
    plan_account_task(
        has_token=token_flags[0],
        task=task,
        breakfast_count=breakfast_count,
        delay_count=delay_count,
        stock=stock,
    )
    if stock is None:
        raise ValueError("批量计划必须提供库存快照。")
    accounts = tuple(
        plan_account_task(
            has_token=flag,
            task=task,
            breakfast_count=breakfast_count,
            delay_count=delay_count,
        )
        for flag in token_flags
    )
    demand = {kind: 0 for kind in RESOURCE_KINDS.values()}
    for account in accounts:
        for operation in account.operations:
            if operation.operation in RESOURCE_KINDS and operation.state in {
                "待接授权接口，仅计划",
                "等待注册及 Token，仅计划",
            }:
                demand[RESOURCE_KINDS[operation.operation]] += operation.remaining
    return BatchTaskPlan(
        accounts,
        tuple(demand.items()),
        tuple(
            (kind, max(0, quantity - stock.get(kind, 0)))
            for kind, quantity in demand.items()
        ),
    )


def batch_report_data(batch, stock):
    """Anonymous account ordinals, not account identifiers or execution results."""
    headers = [
        "模式",
        "范围",
        "分流",
        "操作或资源",
        "目标或需求",
        "已完成",
        "剩余",
        "计划状态",
        "可用库存",
        "资源缺口",
    ]
    rows = []
    for index, account in enumerate(batch.accounts, 1):
        for operation in account.operations:
            rows.append(
                [
                    "仅计划，未执行；历史未接入",
                    f"账号序号 {index}",
                    "注册计划" if account.route == "register" else "领取计划",
                    OPERATIONS[operation.operation],
                    str(operation.target),
                    str(operation.completed),
                    str(operation.remaining),
                    operation.state,
                    "",
                    "",
                ]
            )
    labels = {
        "silver": "银卡",
        "breakfast": "早餐",
        "room_upgrade": "升房",
        "delayed_checkout": "延迟",
        "gift": "礼包",
    }
    shortages = dict(batch.shortages)
    for kind, demand in batch.demand:
        rows.append(
            [
                "仅计划，未执行；历史未接入",
                "整批资源快照",
                "",
                labels[kind],
                str(demand),
                "",
                "",
                "不分配，不预占；不证明可领取",
                str(stock.get(kind, 0)),
                str(shortages[kind]),
            ]
        )
    return "导出账号任务计划（未执行）", headers, rows

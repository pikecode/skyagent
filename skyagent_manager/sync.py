"""Explicit, bounded synchronization against a configured ledger API contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import requests
from PySide6.QtCore import QThread, Signal

ACCOUNT_PATH = "/api/open/pool/accounts/bulk-import"
MAX_RESPONSE_SIZE = 2 * 1024 * 1024


def _unique_response_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("响应包含重复 JSON 键")
        result[key] = value
    return result


def _reject_response_constant(_value):
    raise ValueError("响应包含非有限 JSON 数值")


def _validate_response_depth(data):
    pending = [(data, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            raise ValueError("响应嵌套超过安全限制")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


def validate_config(
    api_url: str, member_path: str, benefit_path: str, allow_http: bool
) -> dict:
    url = api_url.strip().rstrip("/")
    if not url:
        if member_path.strip() or benefit_path.strip():
            raise ValueError("填写同步路径前请先配置后端基础地址。")
        return dict(api_url="", member_path="", benefit_path="", allow_http=False)
    parts = urlsplit(url)
    try:
        _ = parts.port
    except ValueError:
        raise ValueError("后端端口格式无效。") from None
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("请填写完整后端地址，不包含账户、密码、查询参数或片段。")
    if parts.scheme == "http" and not allow_http:
        raise ValueError("此地址使用 HTTP；请在设置中明确启用明文传输，或改用 HTTPS。")
    if parts.path.rstrip("/") == ACCOUNT_PATH:
        url = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    paths = [member_path.strip(), benefit_path.strip()]
    for path in paths:
        if not path:
            continue
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or "\\" in path
            or path.rstrip("/") == ACCOUNT_PATH
        ):
            raise ValueError(
                "会员/权益路径必须是站内绝对路径，不能使用账号 token 导入路径。"
            )
    return dict(
        api_url=url,
        member_path=paths[0],
        benefit_path=paths[1],
        allow_http=bool(allow_http),
    )


@dataclass(frozen=True)
class ItemResult:
    record_id: str
    ok: bool
    summary: str


def parse_results(items: list[dict], data: object) -> list[ItemResult]:
    if (
        not isinstance(data, dict)
        or data.get("ok") is not True
        or not isinstance(data.get("results"), list)
    ):
        return [
            ItemResult(item["id"], False, "响应不符合已配置的批量合约")
            for item in items
        ]
    by_index = {}
    invalid = set()
    malformed = False
    for entry in data["results"]:
        if not isinstance(entry, dict) or type(entry.get("index")) is not int:
            malformed = True
            continue
        index = entry["index"]
        if not 0 <= index < len(items):
            malformed = True
            continue
        if index in by_index:
            invalid.add(index)
        by_index[index] = entry
    results = []
    for index, item in enumerate(items):
        entry = by_index.get(index)
        if (
            malformed
            or index in invalid
            or entry is None
            or type(entry.get("ok")) is not bool
        ):
            results.append(ItemResult(item["id"], False, "结果缺失或索引格式无效"))
        else:
            ok = entry["ok"]
            # Arbitrary server error strings can contain tokens or personal data.
            results.append(
                ItemResult(item["id"], ok, "已同步" if ok else "后端拒绝该记录")
            )
    return results


class SyncClient:
    batch_size = 200

    def __init__(self, config: dict, entity: str, secret: str, *, session=None):
        if entity not in {"members", "benefits"}:
            raise ValueError("同步对象无效。")
        self.config = validate_config(
            config["api_url"],
            config["member_path"],
            config["benefit_path"],
            bool(config["allow_http"]),
        )
        path = self.config["member_path" if entity == "members" else "benefit_path"]
        if not self.config["api_url"] or not path or not secret:
            raise ValueError("请先填写后端地址、此类数据的接口路径和 Secret。")
        self.url = self.config["api_url"] + path
        self.secret = secret
        self.session = session or requests.Session()
        self.session.trust_env = False

    def submit(self, items: list[dict]) -> list[ItemResult]:
        if len(items) > self.batch_size:
            raise ValueError("单批同步最多 200 条，请通过同步任务分批提交。")
        if not items:
            return []
        try:
            with self.session.post(
                self.url,
                json={"items": self.request_items(items)},
                headers={
                    "x-skyhotel-import-secret": self.secret,
                    "Content-Type": "application/json",
                },
                timeout=(10, 30),
                verify=True,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    reason = f"HTTP {response.status_code}；请检查后端合约"
                    return [ItemResult(item["id"], False, reason) for item in items]
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if len(payload) + len(chunk) > MAX_RESPONSE_SIZE:
                        return [
                            ItemResult(
                                item["id"],
                                False,
                                "响应超过安全限制；结果可能未知，重试前确认后端去重",
                            )
                            for item in items
                        ]
                    payload.extend(chunk)
                data = json.loads(
                    payload,
                    object_pairs_hook=_unique_response_object,
                    parse_constant=_reject_response_constant,
                )
                _validate_response_depth(data)
                return parse_results(items, data)
        except (requests.RequestException, ValueError, RecursionError):
            return [
                ItemResult(
                    item["id"],
                    False,
                    "网络或响应异常；结果可能未知，重试前确认后端去重",
                )
                for item in items
            ]

    def close(self):
        self.session.close()

    def request_items(self, items: list[dict]) -> list[dict]:
        return items


class AccountImportClient(SyncClient):
    """A separate account contract; never reinterpret members as token accounts."""

    def __init__(self, config: dict, secret: str, *, session=None):
        validated = validate_config(
            config["api_url"], "", "", bool(config["allow_http"])
        )
        # Reuse bounded transport only after validating the ordinary base address.
        super().__init__(
            {**validated, "member_path": "/account-transport", "benefit_path": ""},
            "members",
            secret,
            session=session,
        )
        self.url = validated["api_url"] + ACCOUNT_PATH

    def request_items(self, items: list[dict]) -> list[dict]:
        # Stable local IDs identify results but are not part of the original API.
        return [
            {
                field: item[field]
                for field in (
                    "phone",
                    "token",
                    "is_online",
                    "is_new_user",
                    "is_enabled",
                    "remark",
                )
            }
            for item in items
        ]


class SyncWorker(QThread):
    batch_ready = Signal(str, str, object)
    progress = Signal(int, int)
    completed = Signal(str)

    def __init__(
        self,
        store_id: str,
        entity: str,
        items: list[dict],
        client: SyncClient,
        parent=None,
    ):
        super().__init__(parent)
        self.store_id, self.entity, self.items, self.client = (
            store_id,
            entity,
            items,
            client,
        )

    def run(self):
        completed = 0
        try:
            for start in range(0, len(self.items), self.client.batch_size):
                if self.isInterruptionRequested():
                    self.batch_ready.emit(
                        self.store_id,
                        self.entity,
                        [
                            ItemResult(item["id"], False, "已取消，尚未提交")
                            for item in self.items[start:]
                        ],
                    )
                    break
                batch = self.items[start : start + self.client.batch_size]
                results = self.client.submit(batch)
                self.batch_ready.emit(self.store_id, self.entity, results)
                completed += len(results)
                self.progress.emit(completed, len(self.items))
            self.completed.emit(f"同步结束：已处理 {completed}/{len(self.items)} 条。")
        except Exception:
            self.batch_ready.emit(
                self.store_id,
                self.entity,
                [
                    ItemResult(item["id"], False, "任务异常，提交结果可能未知")
                    for item in self.items[completed:]
                ],
            )
            self.completed.emit("同步任务异常，未提交项目可重新选择同步。")
        finally:
            self.client.close()

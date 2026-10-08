"""Offline, explicit link import. Never resolves DNS or follows redirects."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from skyagent_manager.imports import MAX_BYTES, MAX_ROWS, CsvRow

URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；！？、（）【】《》]+", re.I)
STATES = {
    "": "available",
    "available": "available",
    "未使用": "available",
    "未用": "available",
    "used": "used",
    "已使用": "used",
    "已用": "used",
}


def valid_link(value):
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parts = urlsplit(value)
        return (
            parts.scheme.lower() in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and (parts.port is None or 1 <= parts.port <= 65535)
            and not any(ord(char) < 33 or char == "\\" for char in value)
        )
    except ValueError:
        return False


def link_source(value):
    if not valid_link(value):
        return "无效链接"
    host = urlsplit(value).hostname.lower().rstrip(".")
    if host == "yaduo.com" or host.endswith(".yaduo.com"):
        return "亚朵域名"
    if host in {"aiyaduo.cn", "www.aiyaduo.cn"}:
        return "AIYD 域名"
    if host in {"awanduo.com", "www.awanduo.com"}:
        return "Awanduo 域名"
    return "其他域名（未验证）"


def parse_link_text(text, kind):
    if kind not in {
        "silver",
        "breakfast",
        "room_upgrade",
        "delayed_checkout",
        "gift",
        "prop",
    }:
        raise ValueError("链接导入不支持邀请码，请使用普通文本导入。")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("链接文本最多 20 MiB。")
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(rows) >= MAX_ROWS:
            raise ValueError("链接导入最多 20000 条非空行。")
        value_part, separator, state = line.strip().partition("\t")
        candidates = URL_PATTERN.findall(value_part)
        value = candidates[0].rstrip(".,;:!?)]}") if len(candidates) == 1 else ""
        # Multiple links, invalid URLs and unknown statuses must not become usable stock.
        valid = valid_link(value) and (not separator or state.strip() in STATES)
        rows.append(
            CsvRow(
                {
                    "kind": kind,
                    "value": value if valid else "",
                    "state": STATES.get(state.strip(), "available"),
                },
                number,
            )
        )
    if not rows:
        raise ValueError("请输入至少一条非空链接记录。")
    return rows


def read_link_file(path, kind):
    """Read a bounded UTF-8 TXT file, without exposing its path or raw contents."""
    path = Path(path)
    if path.suffix.lower() != ".txt":
        raise ValueError("离线链接文件仅支持 TXT，CSV 请使用原资源文件入口。")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ValueError("链接文件无法读取，请检查文件及权限。") from None
    if len(data) > MAX_BYTES:
        raise ValueError("链接文本最多 20 MiB。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("链接文件必须使用 UTF-8 编码。") from None
    return parse_link_text(text, kind)

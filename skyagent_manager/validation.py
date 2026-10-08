"""Shared ledger validation for manual input, edits and CSV rows."""

from __future__ import annotations

import re
from datetime import date


def clean_text(value: str, label: str, maximum: int, *, required: bool = False) -> str:
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f"{label}不能为空。")
    if len(cleaned) > maximum:
        raise ValueError(f"{label}不能超过 {maximum} 个字符。")
    if "\x00" in cleaned:
        raise ValueError(f"{label}不能包含空字符。")
    return cleaned


def clean_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("到期日必须为 YYYY-MM-DD，或留空。")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("到期日不是有效日期。") from None
    return value


def clean_quantity(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError("数量必须为 0 到 1,000,000 的整数。")
    return value


def row_text(row: dict, field: str, default: str = "") -> str:
    value = row.get(field)
    return default if value is None else str(value).strip()


def search_pattern(value: str) -> str:
    # Treat the user's percent and underscore characters literally.
    return (
        "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )

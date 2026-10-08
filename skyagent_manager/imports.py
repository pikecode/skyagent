"""Bounded CSV reading and reports that never include imported sensitive values."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

MAX_ROWS = 20_000
MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ImportRowResult:
    row_number: int
    imported: bool
    reason: str


@dataclass
class ImportReport:
    rows: list[ImportRowResult]

    @property
    def inserted(self) -> int:
        return sum(row.imported for row in self.rows)

    @property
    def skipped(self) -> int:
        return len(self.rows) - self.inserted


class CsvRow(dict):
    def __init__(self, values: dict, line_number: int):
        super().__init__(values)
        self.line_number = line_number


def read_csv(path: Path, entity: str) -> list[CsvRow]:
    if entity not in {"members", "benefits", "accounts", "stock"}:
        raise ValueError("导入对象无效。")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("CSV 文件最多为 20 MiB。")
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.reader(stream, strict=True)
            headers = next(reader, [])
            if not headers or any(not header.strip() for header in headers):
                raise ValueError("CSV 需要非空表头。")
            headers = [header.strip() for header in headers]
            if len(headers) != len(set(headers)):
                raise ValueError("CSV 表头不能重复。")
            required = {
                "members": ("phone",),
                "benefits": ("name",),
                "accounts": ("phone", "token"),
                "stock": ("kind", "value"),
            }[entity]
            if any(field not in headers for field in required):
                raise ValueError(f"CSV 至少需要 {', '.join(required)} 列。")
            rows = []
            while True:
                line_number = reader.line_num + 1
                try:
                    values = next(reader)
                except StopIteration:
                    break
                if not values:
                    continue
                if len(rows) >= MAX_ROWS:
                    raise ValueError("单次最多导入 20,000 行。")
                fields = {
                    header: values[index] if index < len(values) else None
                    for index, header in enumerate(headers)
                }
                if len(values) > len(headers):
                    fields[None] = values[len(headers) :]
                rows.append(CsvRow(fields, line_number))
            return rows
    except UnicodeError:
        raise ValueError("CSV 必须使用 UTF-8 编码。") from None
    except csv.Error:
        raise ValueError("CSV 格式无效，请检查引号和字段长度。") from None

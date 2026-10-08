from __future__ import annotations

import pytest

from skyagent_manager.db import StoreDatabase
from skyagent_manager.imports import read_csv


@pytest.fixture
def database(tmp_path):
    database = StoreDatabase(tmp_path / "ledger.sqlite3", key=b"i" * 32)
    yield database
    database.close()


def test_import_row_numbers_multiline_duplicates_and_private_report(database, tmp_path):
    store = database.add_store("A")
    path = tmp_path / "members.csv"
    path.write_text(
        'name,phone,note\n\nAlpha,138-1234-5678,"line one\nline two"\nDuplicate,13812345678,\nInvalid,not-a-phone,\n',
        encoding="utf-8-sig",
    )
    rows = read_csv(path, "members")
    assert [row.line_number for row in rows] == [3, 5, 6]
    report = database.import_members_detailed(store, rows)
    assert (report.inserted, report.skipped) == (1, 2)
    assert [row.row_number for row in report.rows] == [3, 5, 6]
    assert "已存在" in report.rows[1].reason
    assert "138" not in repr(report) and "not-a-phone" not in repr(report)
    assert database.list_members(store)[0]["note"] == "line one\nline two"
    assert database.list_activity(store)[0]["action"] == "导入会员"


def test_missing_optional_values_and_extra_cells(database, tmp_path):
    store = database.add_store("A")
    path = tmp_path / "members.csv"
    path.write_text(
        "phone,name,note\n13812345678\n13912345678,Name,,extra\n", encoding="utf-8"
    )
    report = database.import_members_detailed(store, read_csv(path, "members"))
    assert report.inserted == 1 and report.skipped == 1
    assert database.list_members(store)[0]["display_name"] == ""
    assert "字段数" in report.rows[1].reason


def test_benefit_import_reports_each_reason_without_sensitive_values(database):
    store = database.add_store("A")
    database.add_member(store, "Member", "13812345678")
    rows = [
        {
            "name": "Valid",
            "code": "sensitive-code",
            "phone": "138-1234-5678",
            "expires_at": "2024-02-29",
        },
        {"name": "Duplicate", "code": "sensitive-code"},
        {"name": "Bad date", "expires_at": "2026-02-29"},
        {"name": "Negative", "quantity": "-1"},
        {"name": "Bad number", "quantity": "a"},
        {"name": "Unmatched", "phone": "13912345678"},
        {"name": "Long note", "note": "x" * 2001},
    ]
    report = database.import_benefits_detailed(store, rows)
    assert (report.inserted, report.skipped) == (1, 6)
    assert [row.row_number for row in report.rows] == list(range(2, 9))
    assert "sensitive-code" not in repr(report) and "13912345678" not in repr(report)
    assert "已存在" in report.rows[1].reason
    assert "日期" in report.rows[2].reason
    assert "数量" in report.rows[3].reason
    assert "整数" in report.rows[4].reason
    assert "找不到" in report.rows[5].reason
    assert "2000" in report.rows[6].reason


@pytest.mark.parametrize(
    "contents",
    [
        "phone,phone\n1,2\n",
        "name\nAlpha\n",
        ",phone\n,13812345678\n",
        'phone\n"unterminated',
    ],
)
def test_csv_structure_errors_prevent_import(tmp_path, contents):
    path = tmp_path / "invalid.csv"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        read_csv(path, "members")


def test_csv_streaming_row_and_size_limits(tmp_path, monkeypatch):
    path = tmp_path / "members.csv"
    path.write_text("phone\n13812345678\n13912345678\n13712345678\n", encoding="utf-8")
    monkeypatch.setattr("skyagent_manager.imports.MAX_ROWS", 2)
    with pytest.raises(ValueError, match="行"):
        read_csv(path, "members")
    monkeypatch.setattr("skyagent_manager.imports.MAX_BYTES", 10)
    with pytest.raises(ValueError, match="MiB"):
        read_csv(path, "members")


def test_import_disk_failure_rolls_back_rows_and_activity(database, monkeypatch):
    store = database.add_store("A")
    original = database.path.read_bytes()
    original_activity = [dict(row) for row in database.list_activity(store)]

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("skyagent_manager.db.atomic_write", fail)
    with pytest.raises(OSError):
        database.import_members_detailed(store, [{"phone": "13812345678"}])
    assert database.list_members(store) == []
    assert [dict(row) for row in database.list_activity(store)] == original_activity
    assert database.path.read_bytes() == original

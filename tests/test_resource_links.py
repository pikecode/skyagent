import pytest
from PySide6.QtWidgets import QMessageBox

from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.resource_links import (
    link_source,
    parse_link_text,
    read_link_file,
    valid_link,
)


def test_parse_preserves_line_numbers_states_and_punctuation():
    rows = parse_link_text(
        "\n早餐：https://example.invalid/a。\nhttps://example.invalid/b\t已用\n",
        "breakfast",
    )
    assert [r.line_number for r in rows] == [2, 3]
    assert rows[0]["value"] == "https://example.invalid/a"
    assert rows[1]["state"] == "used"


@pytest.mark.parametrize(
    "text",
    [
        "not-a-url",
        "https://example.invalid/a https://example.invalid/b",
        "https://example.invalid/a\t未知",
        "https://user:secret@example.invalid/a",
        "https://example.invalid:99999/a",
        "https://example.invalid/\\path",
    ],
)
def test_invalid_rows_never_become_available(text):
    rows = parse_link_text(text, "gift")
    assert rows[0]["value"] == ""
    assert text not in repr(rows)


@pytest.mark.parametrize(
    "url,source",
    [
        ("https://sub.yaduo.com/a", "亚朵域名"),
        ("http://aiyaduo.cn/a", "AIYD 域名"),
        ("http://awanduo.com:8181/a", "Awanduo 域名"),
        ("https://yaduo.com.evil.invalid/a", "其他域名（未验证）"),
    ],
)
def test_source_is_domain_hint_only(url, source):
    assert valid_link(url) and link_source(url) == source


def test_import_reports_duplicates_unknown_status_without_secret(tmp_path):
    db = StoreDatabase(tmp_path / "links.db", key=b"l" * 32)
    try:
        sid = db.add_store("A")
        rows = parse_link_text(
            "https://example.invalid/private-value\tused\n"
            "https://example.invalid/private-value\tavailable\n"
            "https://example.invalid/other\tUNKNOWN",
            "gift",
        )
        report = Inventory(db).import_rows(sid, rows)
        assert report.inserted == 1 and report.skipped == 2
        assert Inventory(db).list(sid)[0]["state"] == "used"
        assert "private-value" not in repr(report)
    finally:
        db.close()


def test_invites_and_empty_input_are_rejected():
    for text, kind in [("abc", "invite"), ("\n", "gift")]:
        with pytest.raises(ValueError):
            parse_link_text(text, kind)


def test_link_ui_uses_preview_and_keeps_plain_import(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from skyagent_manager.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "ui.db", key=b"i" * 32)
    sid = db.add_store("A")
    window = MainWindow(db.path, database=db)
    page = window.inventory_page
    previews = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    monkeypatch.setattr(page, "preview_csv", lambda rows: previews.append(rows) or True)
    try:
        page.kind.setCurrentIndex(page.kind.findData("breakfast"))
        page.import_link_text("说明 https://example.invalid/a\t已用\n无效内容")
        assert len(previews) == 1
        assert page.reports[sid].inserted == 1 and page.reports[sid].skipped == 1
        assert Inventory(db).list(sid)[0]["value"] == "https://example.invalid/a"
        page.import_text("raw-code-is-still-supported")
        assert len(Inventory(db).list(sid)) == 2
    finally:
        window.close()
        app.processEvents()


def test_link_file_utf8_bom_and_crlf(tmp_path):
    path = tmp_path / "links.txt"
    path.write_bytes(
        b"\xef\xbb\xbfhttps://example.invalid/a\tused\r\n\r\nhttps://example.invalid/b\r\n"
    )
    rows = read_link_file(path, "gift")
    assert [row.line_number for row in rows] == [1, 3]
    assert [row["state"] for row in rows] == ["used", "available"]


@pytest.mark.parametrize("mode", ["csv", "encoding", "missing", "oversize"])
def test_link_file_rejects_invalid_without_leaking_path(tmp_path, monkeypatch, mode):
    path = tmp_path / ("private-path.csv" if mode == "csv" else "private-path.txt")
    if mode != "missing":
        path.write_bytes(b"\xff" if mode == "encoding" else b"x" * 20)
    if mode == "oversize":
        monkeypatch.setattr("skyagent_manager.resource_links.MAX_BYTES", 10)
    with pytest.raises(ValueError) as error:
        read_link_file(path, "gift")
    assert "private-path" not in str(error.value)


@pytest.mark.parametrize(
    "mode",
    [
        "save",
        "file_cancel",
        "auth_cancel",
        "preview_cancel",
        "store_change",
        "kind_change",
    ],
)
def test_link_file_ui_confirmation_boundaries(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFileDialog

    from skyagent_manager.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "file-ui.db", key=b"u" * 32)
    sid = db.add_store("A")
    other = db.add_store("B")
    window = MainWindow(db.path, database=db)
    page = window.inventory_page
    page.kind.setCurrentIndex(page.kind.findData("gift"))
    path = tmp_path / "links.txt"
    path.write_text("https://example.invalid/a\tused", encoding="utf-8")
    errors, previews = [], []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args: ("" if mode == "file_cancel" else str(path), ""),
    )

    def confirm(*args):
        if mode == "store_change":
            window.store_combo.setCurrentIndex(window.store_combo.findData(other))
        if mode == "kind_change":
            page.kind.setCurrentIndex(page.kind.findData("breakfast"))
        return QMessageBox.No if mode == "auth_cancel" else QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    monkeypatch.setattr(
        page,
        "preview_csv",
        lambda rows: previews.append(rows) or mode != "preview_cancel",
    )
    try:
        before = db.list_activity(sid)
        page.import_links_file()
        if mode == "save":
            assert len(previews) == 1 and len(Inventory(db).list(sid)) == 1
            assert Inventory(db).list(sid)[0]["state"] == "used"
        else:
            assert not Inventory(db).list(sid) and not Inventory(db).list(other)
            assert db.list_activity(sid) == before
        assert bool(errors) == (mode in {"store_change", "kind_change"})
    finally:
        window.close()
        app.processEvents()

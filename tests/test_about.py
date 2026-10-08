import os
import tomllib
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QTextBrowser

from skyagent_manager import __version__
from skyagent_manager.about_dialog import FEATURE_STATUS, AboutDialog
from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.db import StoreDatabase


def test_version_matches_project_metadata():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["version"] == __version__ == "0.2.28"


def test_version_cli_has_no_startup_side_effects(tmp_path, monkeypatch, capsys):
    import requests

    import skyagent_manager.main as entry
    from skyagent_manager.security import KeyVault

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "version must not initialize GUI, data, credentials or network"
        )

    directory = tmp_path / "uncreated"
    monkeypatch.setattr(entry, "QApplication", forbidden)
    monkeypatch.setattr(entry, "open_window", forbidden)
    monkeypatch.setattr(entry, "data_path", forbidden)
    monkeypatch.setattr(KeyVault, "__init__", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(
        entry.sys, "argv", ["skyagent", "--data-dir", str(directory), "--version"]
    )
    with pytest.raises(SystemExit) as error:
        entry.main()
    assert error.value.code == 0
    assert capsys.readouterr().out == f"SkyAgent Manager {__version__}\n"
    assert not directory.exists()


def test_about_offline_read_only_no_secrets(tmp_path, monkeypatch):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "db", key=b"a" * 32)
    sid = db.add_store("A")
    Accounts(db).add(sid, AccountInput("13812345678", "private-about-test-token"))
    before = db.path.read_bytes()
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("about must not request network")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    try:
        dialog = AboutDialog(db)
        text = "\n".join(widget.text() for widget in dialog.findChildren(QLabel))
        text += "\n".join(
            widget.toPlainText() for widget in dialog.findChildren(QTextBrowser)
        )
        assert __version__ in text and "schema升级v6" in text
        assert str(db.path.parent.resolve()) in text
        assert "private-about-test-token" not in text and "13812345678" not in text
        assert "虚构" in text and "不检查或下载更新" in text
        assert all(
            not browser.openExternalLinks()
            for browser in dialog.findChildren(QTextBrowser)
        )
        assert db.path.read_bytes() == before
        dialog.close()
        app.processEvents()
    finally:
        db.close()


def test_feature_status_does_not_claim_production_completion():
    assert any(
        "礼包资格/有效期核验及后端同步仍未完成" in state and "未生产实测" in state
        for _, state in FEATURE_STATUS
    )
    assert any("未实际部署联调" in state for _, state in FEATURE_STATUS)
    assert any("Windows" in state and "待验收" in state for _, state in FEATURE_STATUS)


def test_about_exposes_restored_share_block_read_only(tmp_path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "restored.db", key=b"a" * 32)
    try:
        db.add_store("A")
        db.replace_snapshot(db.connection.serialize())
        before = db.path.read_bytes()
        dialog = AboutDialog(db)
        text = "\n".join(widget.text() for widget in dialog.findChildren(QLabel))
        assert "恢复后真实分享已阻断" in text and "无解除入口" in text
        assert db.path.read_bytes() == before
        dialog.close()
        app.processEvents()
    finally:
        db.close()

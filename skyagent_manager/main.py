from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from skyagent_manager import __version__
from skyagent_manager.security import MissingDatabaseKeyError
from skyagent_manager.store_selector import create_startup_window


def data_path(directory: Path | None = None) -> Path:
    override = directory or os.getenv("SKYAGENT_MANAGER_DATA_DIR")
    if override:
        return Path(override).expanduser() / "manager.sqlite3"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "SkyAgentManager"
    elif sys.platform.startswith("win"):
        base = (
            Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "SkyAgentManager"
        )
    else:
        base = (
            Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
            / "skyagent-manager"
        )
    return base / "manager.sqlite3"


def open_window(path: Path, *, window_factory=None):
    """Hold the directory lock for the full window lifetime; preserve old data."""
    factory = window_factory or create_startup_window
    while True:
        lock = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(path.parent / "manager.lock"))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                QMessageBox.warning(
                    None, "应用已运行", "此数据目录已被另一个应用实例使用。"
                )
                return None
            window = factory(path)
            if window is None:
                lock.unlock()
                return None
            return window, lock
        except MissingDatabaseKeyError as exc:
            if lock is not None:
                lock.unlock()
            answer = QMessageBox.question(
                None,
                "数据库密钥缺失",
                f"{exc}\n\n是否选择新的空数据目录？打开后点击“恢复备份”，选择 .skyportable 并输入备份口令。\n"
                "下次使用该目录请通过 --data-dir 指定；不会修改原数据库。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None
            selected = QFileDialog.getExistingDirectory(None, "选择新的空数据目录")
            if not selected:
                return None
            directory = Path(selected).expanduser().resolve()
            try:
                if not directory.is_dir() or any(directory.iterdir()):
                    raise ValueError("恢复目录必须为空。原数据目录及已有文件均未修改。")
            except (OSError, ValueError) as error:
                QMessageBox.critical(None, "无法使用恢复目录", str(error))
                return None
            path = directory / "manager.sqlite3"
        except Exception as exc:
            if lock is not None:
                lock.unlock()
            QMessageBox.critical(None, "无法打开台账", str(exc))
            return None


def main() -> int:
    parser = argparse.ArgumentParser(description="SkyAgent 门店权益台账")
    parser.add_argument(
        "--version", action="version", version=f"SkyAgent Manager {__version__}"
    )
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--self-check", action="store_true")
    checks.add_argument("--sync-self-check", action="store_true")
    parser.add_argument("--self-check-report", type=Path)
    parser.add_argument("--data-dir", type=Path, help="Use this data directory")
    options = parser.parse_args()
    if options.self_check_report and not (
        options.self_check or options.sync_self_check
    ):
        parser.error("--self-check-report requires a self-check mode")
    if options.sync_self_check:
        from skyagent_manager.contract_check import main as check_sync

        return check_sync(options.self_check_report)
    if options.self_check:
        from skyagent_manager.self_check import run

        return run(options.self_check_report)
    app = QApplication(sys.argv)
    app.setApplicationName("SkyAgent Manager")
    app.setOrganizationName("SkyAgentManager")
    opened = open_window(data_path(options.data_dir))
    if opened is None:
        return 1
    window, lock = opened
    try:
        window.show()
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())

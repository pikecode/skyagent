from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from skyagent_manager.about_dialog import AboutDialog
from skyagent_manager.account_benefit_page import AccountBenefitPage
from skyagent_manager.account_page import AccountPage
from skyagent_manager.accounts import Accounts
from skyagent_manager.backend_query_page import BackendQueryPage
from skyagent_manager.backup import BackupService
from skyagent_manager.db import (
    BENEFIT_KIND_LABELS,
    BENEFIT_KINDS,
    BENEFIT_STATES,
    StoreDatabase,
    mask_code,
    mask_phone,
)
from skyagent_manager.imports import read_csv
from skyagent_manager.inventory_page import InventoryPage
from skyagent_manager.ledger_dialogs import (
    BenefitDialog,
    ImportPreviewDialog,
    ImportReportDialog,
    MemberDialog,
)
from skyagent_manager.legacy_migration import (
    apply_plan,
    apply_workspace_batch,
    preview_workspace,
    preview_workspaces,
)
from skyagent_manager.local_task_runner import LocalTaskRunner
from skyagent_manager.migration_dialog import MigrationDialog, WorkspaceBatchDialog
from skyagent_manager.security import PORTABLE_MAGIC
from skyagent_manager.sync import (
    AccountImportClient,
    SyncClient,
    SyncWorker,
    validate_config,
)


class StoreSettingsDialog(QDialog):
    def __init__(self, name: str, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("门店设置")
        self.resize(600, 320)
        form = QFormLayout(self)
        self.name = QLineEdit(name)
        self.url = QLineEdit(config["api_url"])
        self.url.setPlaceholderText("后端基础地址，例如 https://backend.example.com")
        self.members = QLineEdit(config["member_path"])
        self.benefits = QLineEdit(config["benefit_path"])
        self.members.setPlaceholderText("后端提供的会员导入路径；未知时留空")
        self.benefits.setPlaceholderText("后端提供的权益导入路径；未知时留空")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.secret.setPlaceholderText("留空保留现有 Secret；新值保存到系统凭据库")
        self.allow_http = QCheckBox("允许此门店使用 HTTP（数据与 Secret 将明文传输）")
        self.allow_http.setChecked(bool(config["allow_http"]))
        form.addRow("门店名称", self.name)
        form.addRow("后端基础地址", self.url)
        form.addRow("会员接口路径", self.members)
        form.addRow("权益接口路径", self.benefits)
        form.addRow("Secret", self.secret)
        form.addRow("", self.allow_http)
        note = QLabel(
            "接口需接受台账字段和逐项结果。原账号接口需要 token，不能用作会员/权益同步。"
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class BackupSettingsDialog(QDialog):
    def __init__(self, database: StoreDatabase, parent=None):
        super().__init__(parent)
        self.setWindowTitle("备份设置")
        form = QFormLayout(self)
        self.enabled = QCheckBox("每天自动备份一次（应用运行期间）")
        self.enabled.setChecked(database.get_setting("backup_enabled", "1") == "1")
        self.keep = QSpinBox()
        self.keep.setRange(1, 90)
        self.keep.setValue(int(database.get_setting("backup_keep", "7")))
        form.addRow(self.enabled)
        form.addRow("保留自动备份份数", self.keep)
        note = QLabel(
            "普通/自动备份依赖原系统密钥。跨机备份使用独立口令，可在另一台机器恢复；口令丢失无法恢复。两种备份均不包含 Secret。"
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class ExportPreviewDialog(QDialog):
    def __init__(
        self,
        headers: list[str],
        rows: list[list[str]],
        reveal_label: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("导出预览")
        self.resize(850, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("请确认导出内容。预览最多显示前 100 行。"))
        self.reveal = QCheckBox(reveal_label) if reveal_label else None
        if self.reveal:
            layout.addWidget(self.reveal)
        self.table = QTableWidget(min(100, len(rows)), len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.rows = rows
        self.headers = headers
        layout.addWidget(self.table)
        self._populate()
        if self.reveal:
            self.reveal.toggled.connect(self._populate)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate(self, *_args) -> None:
        visible_rows = self.rows[:100]
        self.table.setRowCount(len(visible_rows))
        for row_index, row in enumerate(visible_rows):
            for col_index, value in enumerate(row):
                shown = value
                if (
                    self.reveal
                    and not self.reveal.isChecked()
                    and self.headers[col_index] == "券码"
                ):
                    shown = mask_code(value)
                item = QTableWidgetItem(shown)
                item.setToolTip("敏感值已遮蔽" if shown != value else "")
                self.table.setItem(row_index, col_index, item)
        self.table.resizeColumnsToContents()


class MainWindow(QMainWindow):
    def __init__(
        self,
        database_path: Path,
        *,
        database: StoreDatabase | None = None,
        initial_store_id: str = "",
    ):
        super().__init__()
        self.db = database or StoreDatabase(database_path)
        self.backups = BackupService(self.db)
        self.sync_worker = None
        self.close_pending = False
        self.action_buttons = []
        self.import_reports = {}
        if initial_store_id:
            self.db._require_active_store(initial_store_id)
        self.current_store = initial_store_id
        self.current_archived = False
        self.setWindowTitle("SkyAgent 门店权益台账")
        self.resize(1120, 720)
        self._build_ui()
        self._reload_stores()
        self.backup_timer = QTimer(self)
        self.backup_timer.timeout.connect(self._automatic_backup)
        self.backup_timer.start(60 * 60 * 1000)
        QTimer.singleShot(0, self._automatic_backup)

    def closeEvent(self, event) -> None:
        if self.sync_worker is not None:
            self.close_pending = True
            self.sync_worker.requestInterruption()
            self.status.setText("正在取消当前执行，请等待结果保存后自动关闭。")
            event.ignore()
            return
        self.backup_timer.stop()
        self.backend_query_page.clear()
        self.db.close()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("当前门店"))
        self.store_combo = QComboBox()
        self.store_combo.currentIndexChanged.connect(self._store_changed)
        toolbar.addWidget(self.store_combo, 1)
        add_store = QPushButton("添加门店")
        add_store.clicked.connect(self._add_store)
        toolbar.addWidget(add_store)
        self.action_buttons.append(add_store)
        store_settings = QPushButton("门店设置")
        self.store_settings_button = store_settings
        store_settings.clicked.connect(self._store_settings)
        toolbar.addWidget(store_settings)
        self.action_buttons.append(store_settings)
        self.archive_store_button = QPushButton("归档门店")
        self.archive_store_button.clicked.connect(self._archive_store)
        toolbar.addWidget(self.archive_store_button)
        self.show_archived = QCheckBox("显示已归档")
        self.show_archived.toggled.connect(lambda _checked: self._reload_stores())
        toolbar.addWidget(self.show_archived)
        layout.addLayout(toolbar)
        self.backend_target = QLabel()
        backend_bar = QHBoxLayout()
        backend_bar.addWidget(self.backend_target, 1)
        directory_button = QPushButton("数据目录")
        directory_button.clicked.connect(self._show_data_directory)
        backend_bar.addWidget(directory_button)
        about_button = QPushButton("关于 / 还原状态")
        about_button.clicked.connect(self._show_about)
        backend_bar.addWidget(about_button)
        layout.addLayout(backend_bar)

        backup_bar = QHBoxLayout()
        for text, callback in [
            ("手动备份", self._manual_backup),
            ("跨机备份", self._portable_backup),
            ("恢复备份", self._restore_backup),
            ("备份设置", self._backup_settings),
            ("迁移旧 JSON", self._migrate_json),
            ("迁移多门店", self._migrate_multi_json),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            self.action_buttons.append(button)
            backup_bar.addWidget(button)
        self.status = QLabel("本地数据库已加密")
        backup_bar.addWidget(self.status, 1)
        self.cancel_sync = QPushButton("取消同步")
        self.cancel_sync.setEnabled(False)
        self.cancel_sync.clicked.connect(self._cancel_sync)
        backup_bar.addWidget(self.cancel_sync)
        layout.addLayout(backup_bar)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.account_page = AccountPage(self)
        self.tabs.addTab(self.account_page, "账号管理")
        self.inventory_page = InventoryPage(self)
        self.tabs.addTab(self.inventory_page, "资源 / 模拟任务")
        self.account_benefit_page = AccountBenefitPage(self)
        self.tabs.addTab(self.account_benefit_page, "账号权益 / 授权直连")
        self.backend_query_page = BackendQueryPage(self)
        self.tabs.addTab(self.backend_query_page, "后台账号 / 人工短信")
        self.members_table = self._make_table(
            ["姓名 / 标记", "手机号（遮蔽）", "备注", "登记时间"]
        )
        self.benefits_table = self._make_table(
            ["类型", "名称", "券码（遮蔽）", "到期日", "数量", "会员", "状态", "备注"]
        )
        self.activity_table = self._make_table(["时间（UTC）", "操作", "摘要"])
        self.sync_table = self._make_table(
            ["对象", "记录 ID", "结果", "说明", "时间（UTC）"]
        )
        self.member_search = QLineEdit()
        self.member_search.setPlaceholderText("搜索姓名、手机号或备注")
        self.member_search.setClearButtonEnabled(True)
        self.member_search.textChanged.connect(self._refresh_members)
        self.member_duplicates = QCheckBox("仅重复号码")
        self.member_duplicates.toggled.connect(self._refresh_members)
        self.member_count = QLabel()
        self.benefit_search = QLineEdit()
        self.benefit_search.setPlaceholderText("搜索名称、券码、会员或备注")
        self.benefit_search.setClearButtonEnabled(True)
        self.benefit_search.textChanged.connect(self._refresh_benefits)
        self.benefit_state_filter = QComboBox()
        self.benefit_state_filter.addItem("所有状态", "")
        for state in sorted(BENEFIT_STATES):
            self.benefit_state_filter.addItem(state, state)
        self.benefit_state_filter.currentIndexChanged.connect(self._refresh_benefits)
        self.benefit_kind_filter = QComboBox()
        self.benefit_kind_filter.addItem("所有类型", "")
        for label, kind in BENEFIT_KINDS.items():
            self.benefit_kind_filter.addItem(label, kind)
        self.benefit_kind_filter.currentIndexChanged.connect(self._refresh_benefits)
        self.benefit_expiry_filter = QComboBox()
        for label, value in [
            ("所有到期日", "all"),
            ("到期日已过", "past"),
            ("7天内到期", "week"),
            ("无到期日", "none"),
        ]:
            self.benefit_expiry_filter.addItem(label, value)
        self.benefit_expiry_filter.currentIndexChanged.connect(self._refresh_benefits)
        self.benefit_count = QLabel()
        self.tabs.addTab(
            self._table_page(
                self.members_table,
                [
                    ("添加会员", self._add_member),
                    ("编辑会员", self._edit_member),
                    ("删除会员", self._delete_member),
                    ("导入 CSV", self._import_members),
                    ("导入结果", lambda: self._show_last_import("members")),
                    ("导出当前列表", self._export_members),
                ],
            ),
            "会员台账",
        )
        self.tabs.addTab(
            self._table_page(
                self.benefits_table,
                [
                    ("登记权益", self._add_benefit),
                    ("编辑权益", self._edit_benefit),
                    ("删除权益", self._delete_benefit),
                    ("导入 CSV", self._import_benefits),
                    ("导入结果", lambda: self._show_last_import("benefits")),
                    ("更改状态", self._change_benefit_state),
                    ("导出当前列表", self._export_benefits),
                ],
            ),
            "权益库存",
        )
        self.tabs.addTab(
            self._table_page(self.activity_table, [("刷新", self._refresh_all)]),
            "操作记录",
        )
        self.tabs.addTab(
            self._table_page(
                self.sync_table,
                [
                    ("同步会员", lambda: self._start_sync("members")),
                    ("同步权益", lambda: self._start_sync("benefits")),
                    ("重试失败项", self._retry_sync),
                ],
            ),
            "同步结果",
        )
        self.setCentralWidget(root)

    def _migrate_multi_json(self):
        if self.sync_worker is not None or not self.db.list_stores():
            return
        directory = QFileDialog.getExistingDirectory(
            self, "选择多门店根目录（仅检查直接子目录）"
        )
        if not directory or self.sync_worker is not None:
            return
        try:
            batch = preview_workspaces(Path(directory))
            dialog = WorkspaceBatchDialog(batch, self.db.list_stores(), self)
            if dialog.exec() != QDialog.Accepted or self.sync_worker is not None:
                return
            report, safety = apply_workspace_batch(
                self.db,
                batch,
                dialog.mappings(),
                authorized=dialog.authorized.isChecked(),
            )
            self._reload_stores(self.current_store)
            self.status.setText(
                f"多门店迁移新增 {report.inserted} 条，跳过 {report.skipped} 条；源文件未修改。"
            )
            QMessageBox.information(
                self,
                "整批迁移完成",
                f"迁移前加密备份：\n{safety}\n未迁移配置、Secret或历史任务；不会自动上传。",
            )
            result_dialog = ImportReportDialog(report, self)
            result_dialog.setWindowTitle("多门店迁移脱敏结果")
            result_dialog.exec()
        except Exception as error:
            self._error(str(error))

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _table_page(
        self, table: QTableWidget, actions: list[tuple[str, object]]
    ) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        for label, callback in actions:
            button = QPushButton(label)
            button.clicked.connect(callback)
            self.action_buttons.append(button)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        if table is self.members_table:
            filter_widgets = [
                self.member_search,
                self.member_duplicates,
                self.member_count,
            ]
        elif table is self.benefits_table:
            filter_widgets = [
                self.benefit_search,
                self.benefit_state_filter,
                self.benefit_kind_filter,
                self.benefit_expiry_filter,
                self.benefit_count,
            ]
        else:
            filter_widgets = []
        if filter_widgets:
            filters = QHBoxLayout()
            for widget in filter_widgets:
                filters.addWidget(widget)
            layout.addLayout(filters)
        layout.addWidget(table, 1)
        return page

    def _reload_stores(self, select_id: str = "") -> None:
        select_id = (
            select_id or self.current_store or self.db.get_setting("last_store_id")
        )
        stores = self.db.list_stores(include_archived=self.show_archived.isChecked())
        self.store_combo.blockSignals(True)
        self.store_combo.clear()
        for store in stores:
            suffix = "（已归档）" if store["archived"] else ""
            self.store_combo.addItem(store["name"] + suffix, store["id"])
        self.store_combo.blockSignals(False)
        target = next(
            (
                i
                for i in range(self.store_combo.count())
                if self.store_combo.itemData(i) == select_id
            ),
            0,
        )
        if stores:
            self.store_combo.setCurrentIndex(target)
            self.current_store = str(self.store_combo.currentData())
            self.archive_store_button.setEnabled(True)
            selected = next(
                (store for store in stores if store["id"] == self.current_store), None
            )
            self.current_archived = bool(selected["archived"]) if selected else False
            self.archive_store_button.setText(
                "恢复门店" if self.current_archived else "归档门店"
            )
            self.tabs.setEnabled(not self.current_archived)
            self.store_settings_button.setEnabled(
                not self.current_archived and self.sync_worker is None
            )
        else:
            self.current_store = ""
            self.current_archived = False
            self.archive_store_button.setEnabled(False)
            self.tabs.setEnabled(False)
            self.store_settings_button.setEnabled(False)
        self._remember_and_display_store()
        self._refresh_all()

    def _store_changed(self, index: int) -> None:
        self.current_store = (
            str(self.store_combo.itemData(index) or "") if index >= 0 else ""
        )
        selected = next(
            (
                store
                for store in self.db.list_stores(include_archived=True)
                if store["id"] == self.current_store
            ),
            None,
        )
        self.current_archived = bool(selected["archived"]) if selected else False
        self.archive_store_button.setText(
            "恢复门店" if self.current_archived else "归档门店"
        )
        self.tabs.setEnabled(bool(self.current_store) and not self.current_archived)
        self.store_settings_button.setEnabled(
            bool(self.current_store)
            and not self.current_archived
            and self.sync_worker is None
        )
        self._remember_and_display_store()
        self._refresh_all()

    def _remember_and_display_store(self) -> None:
        if self.current_store:
            try:
                self.db.remember_store(self.current_store)
            except Exception as exc:
                self._error(f"无法保存上次门店：{exc}")
        target = self.db.get_store_sync(self.current_store)["api_url"]
        self.backend_target.setText(f"同步后端：{target or '尚未配置'}")

    def _show_data_directory(self) -> None:
        directory = self.db.path.parent.resolve()
        QMessageBox.information(
            self,
            "当前数据目录",
            f"{directory}\n\n再次启动时可使用 --data-dir 指定此目录。\n"
            "不要直接移动加密数据库或删除系统凭据；跨机迁移请先创建跨机口令备份。",
        )

    def _add_store(self) -> None:
        name, accepted = QInputDialog.getText(self, "添加门店", "门店名称")
        if not accepted:
            return
        try:
            store_id = self.db.add_store(name)
        except Exception as exc:
            self._error(str(exc))
            return
        self._reload_stores(store_id)

    def _archive_store(self) -> None:
        if not self.current_store:
            return
        if self.current_archived:
            try:
                self.db.restore_store(self.current_store)
            except Exception as exc:
                self._error(f"恢复门店失败，可能存在同名的启用门店：{exc}")
                return
            restored_id = self.current_store
            self._reload_stores(restored_id)
            return
        answer = QMessageBox.question(
            self,
            "归档门店",
            "归档后门店将从列表隐藏，数据会保留。继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.db.archive_store(self.current_store)
        except Exception as exc:
            self._error(str(exc))
            return
        self.current_store = ""
        self._reload_stores()

    def _show_about(self):
        AboutDialog(self.db, self).exec()

    def _refresh_all(self, *_args) -> None:
        self.account_page.refresh()
        self.inventory_page.refresh()
        self.account_benefit_page.refresh()
        self.backend_query_page.refresh()
        self._refresh_members()
        self._refresh_benefits()
        self._refresh_activity()
        self._refresh_sync()

    def _migrate_json(self) -> None:
        if self.sync_worker is not None or not self.db.list_stores():
            return
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择一个旧门店目录（包含 params_register.json / results_register.json）",
        )
        if not directory:
            return
        try:
            plan = preview_workspace(Path(directory))
            dialog = MigrationDialog(
                plan, self.db.list_stores(), self.current_store, self
            )
            if dialog.exec() != QDialog.Accepted:
                return
            report, safety = apply_plan(
                self.db,
                dialog.target.currentData(),
                plan,
                authorized=dialog.authorized.isChecked(),
            )
            self._reload_stores(dialog.target.currentData())
            self.status.setText(
                f"迁移新增 {report.inserted} 条，跳过 {report.skipped} 条；源文件未修改。"
            )
            QMessageBox.information(
                self,
                "迁移完成",
                f"已保存迁移前加密备份：\n{safety}\n不会自动上传账号或运行任务。",
            )
            result_dialog = ImportReportDialog(report, self)
            result_dialog.setWindowTitle("旧 JSON 迁移结果")
            result_dialog.table.setHorizontalHeaderLabels(
                ["源列表记录号", "结果", "来源 / 原因"]
            )
            result_dialog.exec()
        except Exception as exc:
            self._error(str(exc))

    @staticmethod
    def _fill(
        table: QTableWidget, rows: list[list[str]], raw_rows: list[object] | None = None
    ) -> None:
        current = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        selected_id = current.data(Qt.UserRole) if current is not None else None
        table.setRowCount(len(rows))
        table.setCurrentItem(None)
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                item = QTableWidgetItem(value)
                if raw_rows and col_index == 0:
                    item.setData(Qt.UserRole, raw_rows[row_index]["id"])
                table.setItem(row_index, col_index, item)
        table.resizeColumnsToContents()
        if selected_id and raw_rows:
            for index, raw in enumerate(raw_rows):
                if raw["id"] == selected_id:
                    table.selectRow(index)
                    break

    def _member_rows(self):
        return (
            self.db.list_members(
                self.current_store,
                search=self.member_search.text(),
                duplicates_only=self.member_duplicates.isChecked(),
            )
            if self.current_store
            else []
        )

    def _benefit_rows(self):
        return (
            self.db.list_benefits(
                self.current_store,
                search=self.benefit_search.text(),
                state=self.benefit_state_filter.currentData(),
                kind=self.benefit_kind_filter.currentData(),
                expiry=self.benefit_expiry_filter.currentData(),
            )
            if self.current_store
            else []
        )

    def _refresh_members(self, *_args) -> None:
        rows = self._member_rows()
        self.member_count.setText(f"显示 {len(rows)} 条")
        self._fill(
            self.members_table,
            [
                [r["display_name"], mask_phone(r["phone"]), r["note"], r["created_at"]]
                for r in rows
            ],
            rows,
        )

    def _refresh_benefits(self, *_args) -> None:
        rows = self._benefit_rows()
        self.benefit_count.setText(f"显示 {len(rows)} 条")
        self._fill(
            self.benefits_table,
            [
                [
                    BENEFIT_KIND_LABELS.get(r["kind"], "其他"),
                    r["name"],
                    mask_code(r["code"]),
                    r["expires_at"],
                    str(r["quantity"]),
                    r["member_name"]
                    or (mask_phone(r["member_phone"]) if r["member_phone"] else ""),
                    r["state"],
                    r["note"],
                ]
                for r in rows
            ],
            rows,
        )

    def _refresh_activity(self) -> None:
        rows = self.db.list_activity(self.current_store) if self.current_store else []
        self._fill(
            self.activity_table,
            [[r["created_at"], r["action"], r["summary"]] for r in rows],
        )

    def _log(self, action: str, summary: str) -> None:
        if self.current_store:
            self.db.add_activity(self.current_store, action, summary)
            self._refresh_activity()

    def _add_member(self) -> None:
        if not self.current_store:
            return
        dialog = MemberDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        if not dialog.authorized.isChecked():
            self._error("请先确认你有权保存和管理这条会员资料。")
            return
        try:
            self.db.add_member(
                self.current_store,
                dialog.name.text(),
                dialog.phone.text(),
                dialog.note.text(),
            )
        except Exception as exc:
            self._error(str(exc))
            return
        self._log("添加会员", "已登记一条会员资料")
        self._refresh_members()

    def _selected_id(self, table: QTableWidget) -> str:
        if (
            self.sync_worker is not None
            or not self.current_store
            or self.current_archived
        ):
            raise ValueError("请在未归档门店中操作，并等待同步任务完成。")
        row = table.currentRow()
        item = table.item(row, 0) if row >= 0 else None
        if item is None or not item.data(Qt.UserRole):
            raise ValueError("请先选择一条记录。")
        return str(item.data(Qt.UserRole))

    def _edit_member(self) -> None:
        try:
            member_id = self._selected_id(self.members_table)
            record = self.db.get_member(self.current_store, member_id)
        except ValueError as exc:
            self._error(str(exc))
            return
        dialog = MemberDialog(self, record=record)
        while dialog.exec() == QDialog.Accepted:
            if not dialog.authorized.isChecked():
                self._error("请先确认你有权保存和管理这条会员资料。")
                continue
            try:
                self.db.update_member(
                    self.current_store,
                    member_id,
                    dialog.name.text(),
                    dialog.phone.text(),
                    dialog.note.text(),
                )
            except Exception as exc:
                self._error(str(exc))
                continue
            self._refresh_all()
            return

    def _delete_member(self) -> None:
        try:
            member_id = self._selected_id(self.members_table)
            record = self.db.get_member(self.current_store, member_id)
            answer = QMessageBox.question(
                self,
                "删除会员",
                f"删除会员 {mask_phone(record['phone'])} 的本地记录？\n关联权益会保留并解除关联。后端数据和历史备份不会随之删除。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            self.db.delete_member(self.current_store, member_id)
        except Exception as exc:
            self._error(str(exc))
            return
        self._refresh_all()

    def _edit_benefit(self) -> None:
        try:
            benefit_id = self._selected_id(self.benefits_table)
            record = dict(self.db.get_benefit(self.current_store, benefit_id))
            record["member_phone"] = (
                self.db.get_member(self.current_store, record["member_id"])["phone"]
                if record["member_id"]
                else ""
            )
        except ValueError as exc:
            self._error(str(exc))
            return
        dialog = BenefitDialog(self, record=record)
        while dialog.exec() == QDialog.Accepted:
            try:
                self.db.update_benefit(
                    self.current_store,
                    benefit_id,
                    BENEFIT_KINDS[dialog.kind.currentText()],
                    dialog.name.text(),
                    dialog.code.text(),
                    dialog.expiry.text(),
                    dialog.quantity.value(),
                    dialog.note.text(),
                    dialog.phone.text(),
                    dialog.state.currentText(),
                )
            except Exception as exc:
                self._error(str(exc))
                continue
            self._refresh_all()
            return

    def _delete_benefit(self) -> None:
        try:
            benefit_id = self._selected_id(self.benefits_table)
            record = self.db.get_benefit(self.current_store, benefit_id)
            answer = QMessageBox.question(
                self,
                "删除权益",
                f"删除权益“{record['name']}”的本地记录？\n后端数据和历史备份不会随之删除。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            self.db.delete_benefit(self.current_store, benefit_id)
        except Exception as exc:
            self._error(str(exc))
            return
        self._refresh_all()

    def _finish_import(self, entity: str, report) -> None:
        self.import_reports[(self.current_store, entity)] = report
        self._refresh_all()
        ImportReportDialog(report, self).exec()

    def _show_last_import(self, entity: str) -> None:
        report = self.import_reports.get((self.current_store, entity))
        if report is None:
            self._error("当前门店在本次运行期间尚未导入此类数据。")
            return
        ImportReportDialog(report, self).exec()

    def _import_members(self) -> None:
        if not self.current_store:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入会员 CSV", "", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        if (
            QMessageBox.question(
                self,
                "确认授权",
                "请确认文件中的会员资料已获授权，且你有权导入到此门店。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        try:
            entries = read_csv(Path(path), "members")
            preview_rows = [
                [
                    str(row.get("name") or ""),
                    mask_phone(str(row.get("phone") or "")),
                    str(row.get("note") or ""),
                ]
                for row in entries[:50]
            ]
            if (
                ImportPreviewDialog(
                    ["姓名 / 标记", "手机号", "备注"],
                    preview_rows,
                    self,
                    total=len(entries),
                ).exec()
                != QDialog.Accepted
            ):
                return
            report = self.db.import_members_detailed(self.current_store, entries)
        except Exception as exc:
            self._error(str(exc))
            return
        self._finish_import("members", report)

    def _add_benefit(self) -> None:
        if not self.current_store:
            return
        dialog = BenefitDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.db.add_benefit(
                self.current_store,
                BENEFIT_KINDS[dialog.kind.currentText()],
                dialog.name.text(),
                dialog.code.text(),
                dialog.expiry.text(),
                dialog.quantity.value(),
                dialog.note.text(),
                dialog.phone.text(),
            )
        except Exception as exc:
            self._error(str(exc))
            return
        self._log("登记权益", "已登记一条权益库存记录")
        self._refresh_benefits()

    def _import_benefits(self) -> None:
        if not self.current_store:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入权益 CSV", "", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        if (
            QMessageBox.question(
                self,
                "确认导入",
                "请确认你有权将这些权益记录导入到当前门店。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        try:
            entries = read_csv(Path(path), "benefits")
            preview_rows = [
                [
                    str(row.get("kind") or "其他"),
                    str(row.get("name") or ""),
                    mask_code(str(row.get("code") or "")),
                    str(row.get("expires_at") or ""),
                    mask_phone(str(row.get("phone") or "")) if row.get("phone") else "",
                ]
                for row in entries[:50]
            ]
            if (
                ImportPreviewDialog(
                    ["类型", "名称", "券码", "到期日", "会员手机号"],
                    preview_rows,
                    self,
                    total=len(entries),
                ).exec()
                != QDialog.Accepted
            ):
                return
            report = self.db.import_benefits_detailed(self.current_store, entries)
        except Exception as exc:
            self._error(str(exc))
            return
        self._finish_import("benefits", report)

    def _change_benefit_state(self) -> None:
        row = self.benefits_table.currentRow()
        if row < 0:
            self._error("请先选择一条权益记录。")
            return
        benefit_id = str(self.benefits_table.item(row, 0).data(Qt.UserRole))
        state, accepted = QInputDialog.getItem(
            self, "更改权益状态", "新状态", sorted(BENEFIT_STATES), 0, False
        )
        if not accepted:
            return
        try:
            self.db.set_benefit_state(benefit_id, state, store_id=self.current_store)
        except Exception as exc:
            self._error(str(exc))
            return
        self._refresh_all()

    def _export_members(self) -> None:
        if not self.current_store:
            return
        rows = self._member_rows()
        data = [
            [r["display_name"], mask_phone(r["phone"]), r["note"], r["created_at"]]
            for r in rows
        ]
        self._export_csv(
            "导出会员", ["姓名 / 标记", "手机号", "备注", "登记时间"], data
        )

    def _export_benefits(self) -> None:
        if not self.current_store:
            return
        rows = self._benefit_rows()
        data = [
            [
                BENEFIT_KIND_LABELS.get(r["kind"], "其他"),
                r["name"],
                r["code"],
                r["expires_at"],
                str(r["quantity"]),
                r["state"],
                r["note"],
            ]
            for r in rows
        ]
        self._export_csv(
            "导出权益",
            ["类型", "名称", "券码", "到期日", "数量", "状态", "备注"],
            data,
            reveal_label="在导出的 CSV 中包含完整券码",
        )

    def _export_csv(
        self,
        title: str,
        headers: list[str],
        rows: list[list[str]],
        reveal_label: str = "",
    ) -> None:
        preview = ExportPreviewDialog(headers, rows, reveal_label, self)
        if preview.exec() != QDialog.Accepted:
            return
        include_codes = bool(preview.reveal and preview.reveal.isChecked())
        path, _ = QFileDialog.getSaveFileName(
            self, title, f"{title}.csv", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        exported = (
            rows
            if include_codes
            else [
                [
                    mask_code(value) if headers[i] == "券码" else value
                    for i, value in enumerate(row)
                ]
                for row in rows
            ]
        )
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow([self._csv_safe(value) for value in headers])
                writer.writerows(
                    [[self._csv_safe(value) for value in row] for row in exported]
                )
        except OSError as exc:
            self._error(str(exc))
            return
        self._log(
            title,
            f"已导出 {len(exported)} 行；完整券码导出：{'是' if include_codes else '否'}",
        )
        QMessageBox.information(self, title, f"已导出 {len(exported)} 行。")

    def _store_settings(self) -> None:
        if not self.current_store:
            self._error("请先添加或选择门店。")
            return
        if self.current_archived:
            self._error("请先恢复门店，再修改配置。")
            return
        store = next(
            s
            for s in self.db.list_stores(include_archived=True)
            if s["id"] == self.current_store
        )
        dialog = StoreSettingsDialog(
            store["name"], self.db.get_store_sync(self.current_store), self
        )
        if dialog.exec() != QDialog.Accepted:
            return
        account = f"store/{self.current_store}/secret"
        changed_secret = False
        old_secret = None
        try:
            config = validate_config(
                dialog.url.text(),
                dialog.members.text(),
                dialog.benefits.text(),
                dialog.allow_http.isChecked(),
            )
            if dialog.secret.text():
                if self.db.vault is None:
                    raise ValueError("系统凭据库不可用。")
                old_secret = self.db.vault.get(account)
                self.db.vault.set(account, dialog.secret.text())
                changed_secret = True
            self.db.configure_store(self.current_store, dialog.name.text(), config)
        except Exception as exc:
            if changed_secret:
                try:
                    if old_secret is None:
                        self.db.vault.delete(account)
                    else:
                        self.db.vault.set(account, old_secret)
                except Exception:
                    self._error(
                        "设置保存失败，Secret 回滚也失败；请重新保存门店 Secret。"
                    )
                    return
            self._error(str(exc))
            return
        self._reload_stores(self.current_store)

    def _backup_settings(self) -> None:
        dialog = BackupSettingsDialog(self.db, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.db.set_backup_settings(dialog.enabled.isChecked(), dialog.keep.value())
            self._automatic_backup()
        except Exception as exc:
            self._error(str(exc))

    def _automatic_backup(self) -> None:
        try:
            path = self.backups.automatic()
            if path:
                self.status.setText(f"已自动备份：{path.name}")
        except Exception as exc:
            self.status.setText(f"自动备份失败：{exc}")

    def _manual_backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "保存加密备份", "台账.skybackup", "加密备份 (*.skybackup)"
        )
        if not path:
            return
        try:
            self.backups.create(Path(path))
        except Exception as exc:
            self._error(str(exc))
            return
        QMessageBox.information(
            self, "备份完成", "已保存加密备份。恢复需要本机原系统账户及数据目录的密钥。"
        )

    def _restore_backup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择加密备份",
            str(self.backups.directory),
            "加密备份 (*.skybackup *.skyportable)",
        )
        if not path:
            return
        password = None
        try:
            with Path(path).open("rb") as stream:
                portable = stream.read(len(PORTABLE_MAGIC)) == PORTABLE_MAGIC
            if portable:
                password, accepted = QInputDialog.getText(
                    self, "跨机备份口令", "输入创建备份时的口令", QLineEdit.Password
                )
                if not accepted:
                    return
            _, counts = self.backups.inspect(Path(path), password)
            answer = QMessageBox.question(
                self,
                "确认恢复",
                f"备份包含 {counts['stores']} 个门店、{counts['members']} 条会员、{counts['benefits']} 条权益、{counts['accounts']} 条账号、{counts.get('stock', 0)} 条资源/邀请码、{counts.get('local_tasks', 0)} 个本地模拟任务。\n"
                "恢复会替换当前台账，当前数据会先另存加密备份。Secret 不在备份中；跨机恢复后请核对后端地址并重新配置 Secret。\n"
                "恢复可能丢失较新的分享提交记录，因此恢复后将持续阻断真实分享，需人工对账，当前没有解除入口；查询、台账和模拟仍可使用。继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            safety = self.backups.restore(Path(path), password)
        except Exception as exc:
            self._error(str(exc))
            return
        self.current_store = ""
        self._reload_stores()
        self.status.setText(
            f"恢复完成；真实分享已阻断，需人工对账；恢复前快照：{safety.name}"
        )
        self.import_reports.clear()
        self.account_page.reports.clear()
        self.inventory_page.reports.clear()
        self.backend_query_page.clear()
        self.account_benefit_page._changed()

    def _portable_backup(self) -> None:
        password, accepted = QInputDialog.getText(
            self,
            "跨机加密备份",
            "设置独立长口令（至少 12 字符）。丢失无法恢复；不会设置应用启动密码。",
            QLineEdit.Password,
        )
        if not accepted:
            return
        confirmation, accepted = QInputDialog.getText(
            self, "确认备份口令", "再次输入口令", QLineEdit.Password
        )
        if not accepted:
            return
        if password != confirmation:
            self._error("两次口令不一致，没有创建备份。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存跨机加密备份",
            "台账.skyportable",
            "跨机加密备份 (*.skyportable)",
        )
        if not path:
            return
        try:
            self.backups.create_portable(Path(path), password)
        except Exception as exc:
            self._error(str(exc))
            return
        QMessageBox.information(
            self,
            "跨机备份完成",
            "已保存。请分开保管备份与口令；在新机器选择“恢复备份”并输入口令。Secret 需重新配置。",
        )

    def _refresh_sync(self) -> None:
        rows = (
            self.db.list_sync_results(self.current_store) if self.current_store else []
        )
        self._fill(
            self.sync_table,
            [
                [
                    "会员" if r["entity"] == "members" else "权益",
                    r["record_id"],
                    "成功" if r["state"] == "success" else "失败",
                    r["summary"],
                    r["updated_at"],
                ]
                for r in rows
            ],
        )

    def _retry_sync(self) -> None:
        entity, accepted = QInputDialog.getItem(
            self, "重试失败项", "同步对象", ["会员", "权益"], 0, False
        )
        if accepted:
            self._start_sync(
                "members" if entity == "会员" else "benefits", failed_only=True
            )

    def _start_sync(self, entity: str, *, failed_only: bool = False) -> None:
        if self.sync_worker is not None or not self.current_store:
            return
        try:
            if entity == "accounts":
                items = Accounts(self.db).sync_items(
                    self.current_store,
                    None if failed_only else self.account_page.checked_ids(),
                    failed_only=failed_only,
                )
            else:
                items = self.db.sync_items(
                    self.current_store, entity, failed_only=failed_only
                )
            if not items:
                self._error("没有需要同步的记录。")
                return
            config = self.db.get_store_sync(self.current_store)
            if self.db.vault is None:
                raise ValueError("系统凭据库不可用。")
            secret = self.db.vault.get(f"store/{self.current_store}/secret") or ""
            client = (
                AccountImportClient(config, secret)
                if entity == "accounts"
                else SyncClient(config, entity, secret)
            )
        except Exception as exc:
            self._error(str(exc))
            return
        answer = QMessageBox.question(
            self,
            "确认同步",
            f"将向 {client.url} 提交 {len(items)} 条{'账号（含完整 Token）' if entity == 'accounts' else '会员' if entity == 'members' else '权益'}记录，包含完整手机号或券码。\n"
            + (
                "已检查的账号后端是系统级 Secret、逐条新增，不保证门店隔离或重复去重。请核对实际后端归属；提交后将阻止未知结果重复上传。"
                if entity == "accounts"
                else "请确认后端接受合约并支持去重。"
            )
            + "\n请确认已获授权上传。继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            client.close()
            return
        if entity == "accounts":
            try:
                Accounts(self.db).reserve_upload(
                    self.current_store, [item["id"] for item in items]
                )
            except Exception as exc:
                client.close()
                self._error(str(exc))
                return
        self.sync_error = False
        self.sync_worker = SyncWorker(self.current_store, entity, items, client, self)
        self.sync_worker.batch_ready.connect(self._sync_batch)
        self.sync_worker.progress.connect(
            lambda done, total: self.status.setText(f"同步进度：{done}/{total}")
        )
        self.sync_worker.completed.connect(self.status.setText)
        self.sync_worker.finished.connect(self._sync_finished)
        for button in self.action_buttons:
            button.setEnabled(False)
        self.store_combo.setEnabled(False)
        self.show_archived.setEnabled(False)
        self.archive_store_button.setEnabled(False)
        self.cancel_sync.setEnabled(True)
        self.sync_worker.start()

    def _start_local_task(self, task_id, mode):
        if (
            self.sync_worker is not None
            or not self.current_store
            or self.current_archived
        ):
            return
        try:
            worker = LocalTaskRunner(
                self.db, self.current_store, task_id, mode=mode, parent=self
            )
        except Exception as exc:
            self._error(str(exc))
            return
        self.sync_error = False
        self.sync_worker = worker
        worker.progress.connect(self._local_task_progress)
        worker.completed.connect(self.status.setText)
        worker.error.connect(self._local_task_error)
        worker.finished.connect(self._sync_finished)
        for button in self.action_buttons:
            button.setEnabled(False)
        self.store_combo.setEnabled(False)
        self.show_archived.setEnabled(False)
        self.archive_store_button.setEnabled(False)
        self.cancel_sync.setText("取消模拟执行")
        self.cancel_sync.setEnabled(True)
        self.status.setText("本地模拟执行中；不请求外部接口，不上传账号。")
        worker.start()

    def _local_task_progress(self, done, total):
        self.status.setText(f"本地模拟进度：{done}/{total}（本次剩余项）")
        self.inventory_page.refresh()

    def _local_task_error(self, message):
        self.sync_error = True
        self._error(message)

    def _cancel_sync(self) -> None:
        if self.sync_worker is not None:
            self.sync_worker.requestInterruption()
            self.cancel_sync.setEnabled(False)
            self.status.setText("正在取消；当前处理结果保存后停止。")

    def _sync_batch(self, store_id: str, entity: str, results: list) -> None:
        try:
            if entity == "accounts":
                Accounts(self.db).save_results(store_id, results)
            else:
                self.db.save_sync_results(store_id, entity, results)
        except Exception as exc:
            self.sync_error = True
            self._cancel_sync()
            self._error(f"同步结果保存失败，服务端可能已接收数据：{exc}")
        self._refresh_sync()
        self.account_page.refresh()

    def _sync_finished(self) -> None:
        worker = self.sync_worker
        self.sync_worker = None
        self.cancel_sync.setEnabled(False)
        self.cancel_sync.setText("取消同步")
        for button in self.action_buttons:
            button.setEnabled(True)
        self.store_combo.setEnabled(True)
        self.show_archived.setEnabled(True)
        self._reload_stores(self.current_store)
        if self.sync_error:
            self.status.setText(
                "分享结果未全部保存；已保留防重记录，禁止重发，请人工核实。"
                if getattr(worker, "operation_scope", "") == "direct-coupon-share"
                else "模拟执行未全部保存；请核对本地任务及未完成预占。"
                if isinstance(worker, LocalTaskRunner)
                else "同步结果未全部保存；重试前请核对后端去重。"
            )
        worker.deleteLater()
        if self.close_pending:
            QTimer.singleShot(0, self.close)

    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "操作未完成", message)

    @staticmethod
    def _csv_safe(value: str) -> str:
        value = str(value)
        if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

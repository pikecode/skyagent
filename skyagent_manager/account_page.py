"""Masked authorized-account UI with explicit import and upload confirmation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from skyagent_manager.account_task_plan import (
    OPERATIONS,
    available_stock_counts,
    batch_report_data,
    plan_account_batch,
    plan_account_task,
)
from skyagent_manager.accounts import AccountInput, Accounts, parse_account_text
from skyagent_manager.db import mask_code, mask_phone
from skyagent_manager.imports import read_csv
from skyagent_manager.ledger_dialogs import ImportReportDialog


class AccountDialog(QDialog):
    def __init__(self, parent=None, record=None):
        super().__init__(parent)
        record = dict(record) if record is not None else {}
        self.setWindowTitle("编辑账号" if record else "添加账号")
        form = QFormLayout(self)
        self.label = QLineEdit(record.get("label", ""))
        self.phone = QLineEdit(record.get("phone", ""))
        self.token = QLineEdit(record.get("token", ""))
        self.token.setMaxLength(4096)
        self.token.setEchoMode(QLineEdit.Password)
        self.phone.setEchoMode(QLineEdit.Password)
        reveal = QCheckBox("显示完整手机号和 Token")
        reveal.toggled.connect(
            lambda visible: [
                widget.setEchoMode(QLineEdit.Normal if visible else QLineEdit.Password)
                for widget in (self.phone, self.token)
            ]
        )
        self.new_user = QComboBox()
        for label, value in [("未知（不能上传）", None), ("是", True), ("否", False)]:
            self.new_user.addItem(label, value)
        value = record.get("is_new_user")
        self.new_user.setCurrentIndex(0 if value is None else 1 if value else 2)
        self.note = QLineEdit(record.get("note", ""))
        self.authorized = QCheckBox("我确认已获授权保存、管理该账号及 Token")
        for label, widget in [
            ("来源标识 / COM", self.label),
            ("手机号", self.phone),
            ("Token", self.token),
            ("新用户", self.new_user),
            ("备注", self.note),
        ]:
            form.addRow(label, widget)
        form.addRow(reveal)
        form.addRow(self.authorized)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def value(self):
        if not self.authorized.isChecked():
            raise ValueError("请先确认账号管理授权。")
        return AccountInput(
            self.phone.text(),
            self.token.text(),
            self.label.text(),
            self.note.text(),
            self.new_user.currentData(),
        )


class AccountTable(QTableWidget):
    def __init__(self, page):
        super().__init__(0, 7)
        self.page = page
        self.setHorizontalHeaderLabels(
            [
                "勾选 / 来源",
                "手机号",
                "Token（遮蔽）",
                "新用户",
                "备注",
                "入库状态",
                "说明",
            ]
        )
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls():
            urls = mime.urls()
            if len(urls) != 1 or not urls[0].isLocalFile():
                self.page.window._error("每次仅导入一个本地账号文件。")
                event.ignore()
                return
            self.page.import_file(Path(urls[0].toLocalFile()))
        else:
            self.page.import_text(mime.text())
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            self.page.import_text(QApplication.clipboard().text())
        else:
            super().keyPressEvent(event)


class AccountPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.repository = Accounts(window.db)
        self.reports = {}
        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)
        layout = QVBoxLayout(content)
        toolbar = QHBoxLayout()
        for label, callback in [
            ("添加", lambda: self.edit(False)),
            ("编辑", lambda: self.edit(True)),
            ("删除勾选", self.delete),
            ("导入文件", self.choose_file),
            ("导入文本", self.paste_dialog),
            ("粘贴", lambda: self.import_text(QApplication.clipboard().text())),
            ("导入结果", self.show_report),
            ("导出列表（无Token）", self.export),
            ("预览账号任务（不执行）", self.preview_task),
            ("勾选账号入库", lambda: window._start_sync("accounts")),
            (
                "重试明确拒绝项",
                lambda: window._start_sync("accounts", failed_only=True),
            ),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            window.action_buttons.append(button)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索来源、手机号、备注（不搜索 Token）")
        self.search.textChanged.connect(self.refresh)
        self.count = QLabel()
        all_button = QPushButton("勾选当前列表")
        all_button.clicked.connect(self.check_all)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.count)
        filters.addWidget(all_button)
        layout.addLayout(filters)
        self.table = AccountTable(self)
        layout.addWidget(self.table)
        layout.addWidget(
            QLabel("仅管理已授权账号。导入只存本地；入库会发送完整 Token，需单独确认。")
        )

    def allowed(self):
        if (
            not self.window.current_store
            or self.window.current_archived
            or self.window.sync_worker is not None
        ):
            self.window._error("请先选择未归档门店，并等待同步任务结束。")
            return False
        return True

    def preview_task(self):
        if not self.allowed():
            return
        ids = self.checked_ids()
        item = self.table.item(self.table.currentRow(), 0)
        if not ids and item is None:
            self.window._error("请先选择一个账号行。")
            return
        ids = ids or [item.data(Qt.UserRole)]
        if len(ids) > 200:
            self.window._error("批量预览最多 200 个勾选账号。")
            return
        token_flags = tuple(
            bool(self.repository.get(self.window.current_store, aid)["token"])
            for aid in ids
        )
        has_token = token_flags[0]
        stock = available_stock_counts(self.window.db, self.window.current_store)
        store_id = self.window.current_store
        dialog = QDialog(self)
        dialog.setWindowTitle("账号任务计划（不执行）")
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "仅预览当前账号及当前门店库存快照：不注册、不领取、不预占资源、不上传。库存充足不代表可领取。历史结果尚未接入，不能用于判断真实完成状态。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        batch_summary = QLabel()
        batch_summary.setWordWrap(True)
        layout.addWidget(batch_summary)
        task = QComboBox()
        task.addItem("全部任务", "all")
        for key, label in OPERATIONS.items():
            task.addItem(label, key)
        layout.addWidget(task)
        counts = []
        for label in ("早餐数量", "延迟数量"):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            count = QComboBox()
            count.addItem("1", 1)
            count.addItem("2", 2)
            row.addWidget(count)
            layout.addLayout(row)
            counts.append(count)
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(
            ["操作", "目标数量", "剩余数量", "计划状态", "可用资源", "缺少资源"]
        )
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(table)

        def refresh():
            plan = plan_account_task(
                has_token=has_token,
                task=task.currentData(),
                breakfast_count=counts[0].currentData(),
                delay_count=counts[1].currentData(),
                stock=stock,
            )
            batch = plan_account_batch(
                token_flags=token_flags,
                stock=stock,
                task=task.currentData(),
                breakfast_count=counts[0].currentData(),
                delay_count=counts[1].currentData(),
            )
            labels = {
                "silver": "银卡",
                "breakfast": "早餐",
                "room_upgrade": "升房",
                "delayed_checkout": "延迟",
                "gift": "礼包",
            }
            shortages = dict(batch.shortages)
            batch_summary.setText(
                f"本次快照 {len(ids)} 个账号（勾选优先）；下表展示首个账号。批量资源汇总：\n"
                + "；".join(
                    f"{labels[kind]}需求 {quantity}/可用 {stock.get(kind, 0)}/缺 {shortages[kind]}"
                    for kind, quantity in batch.demand
                )
                + "\n仅是假设需求，不执行、不分配资源；批量历史未接入。"
            )
            table.setRowCount(len(plan.operations))
            for index, operation in enumerate(plan.operations):
                for column, value in enumerate(
                    (
                        OPERATIONS[operation.operation],
                        operation.target,
                        operation.remaining,
                        operation.state,
                        "不适用"
                        if operation.available is None
                        else operation.available,
                        "不适用" if operation.shortage is None else operation.shortage,
                    )
                ):
                    table.setItem(index, column, QTableWidgetItem(str(value)))
            table.resizeColumnsToContents()

        for control in (task, *counts):
            control.currentIndexChanged.connect(refresh)
        refresh()
        export = QPushButton("导出匿名计划报告（未执行）")

        def export_plan():
            if not self.allowed():
                return
            if self.window.current_store != store_id:
                self.window._error("门店已变化，请关闭并重新预览。")
                return
            batch = plan_account_batch(
                token_flags=token_flags,
                stock=stock,
                task=task.currentData(),
                breakfast_count=counts[0].currentData(),
                delay_count=counts[1].currentData(),
            )
            self.window._export_csv(*batch_report_data(batch, stock))

        export.clicked.connect(export_plan)
        layout.addWidget(export)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(760, 450)
        dialog.exec()

    def checked_ids(self):
        return [
            self.table.item(i, 0).data(Qt.UserRole)
            for i in range(self.table.rowCount())
            if self.table.item(i, 0).checkState() == Qt.Checked
        ]

    def check_all(self):
        for i in range(self.table.rowCount()):
            self.table.item(i, 0).setCheckState(Qt.Checked)

    def refresh(self, *_args):
        selected = set(self.checked_ids())
        rows = self.repository.list(self.window.current_store, self.search.text())
        self.table.setRowCount(len(rows))
        self.table.setCurrentItem(None)
        for index, row in enumerate(rows):
            values = [
                row["label"],
                mask_phone(row["phone"]),
                mask_code(row["token"]),
                "未知"
                if row["is_new_user"] is None
                else "是"
                if row["is_new_user"]
                else "否",
                row["note"],
                "后台已有账号（禁止重复上传）"
                if self.repository.import_hold(self.window.current_store, row)
                == "backend-existing"
                else "已提交（禁止重复）"
                if self.repository.import_hold(self.window.current_store, row)
                == "success"
                else "待后台核对（禁止重试）"
                if self.repository.import_hold(self.window.current_store, row)
                else {None: "待入库", "success": "已入库", "failed": "入库失败"}[
                    row["import_state"]
                ],
                row["import_summary"] or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, row["id"])
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.Checked if row["id"] in selected else Qt.Unchecked
                    )
                self.table.setItem(index, column, item)
        self.count.setText(f"显示 {len(rows)} 条")
        self.table.resizeColumnsToContents()

    def edit(self, editing):
        if not self.allowed():
            return
        record = None
        if editing:
            item = self.table.item(self.table.currentRow(), 0)
            if item is None:
                self.window._error("请选择要编辑的账号行。")
                return
            record = self.repository.get(
                self.window.current_store, item.data(Qt.UserRole)
            )
        dialog = AccountDialog(self, record)
        while dialog.exec() == QDialog.Accepted:
            try:
                value = dialog.value()
                if record is None:
                    self.repository.add(self.window.current_store, value)
                else:
                    self.repository.update(
                        self.window.current_store, record["id"], value
                    )
            except Exception as exc:
                self.window._error(str(exc))
            else:
                self.window._refresh_all()
                return

    def delete(self):
        if not self.allowed():
            return
        ids = self.checked_ids()
        if not ids:
            self.window._error("请勾选账号。")
            return
        if (
            QMessageBox.question(
                self,
                "删除账号",
                f"本地删除 {len(ids)} 条？不会删除后端或历史备份。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.repository.delete(self.window.current_store, ids)
        except Exception as exc:
            self.window._error(str(exc))
            return
        self.window._refresh_all()

    def choose_file(self):
        if not self.allowed():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入账号", "", "账号文件 (*.csv *.txt)"
        )
        if path:
            self.import_file(Path(path))

    def import_file(self, path):
        if not self.allowed():
            return
        try:
            if path.suffix.lower() == ".csv":
                rows = read_csv(path, "accounts")
            else:
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("账号文件最多 20 MiB。")
                rows = parse_account_text(path.read_text(encoding="utf-8-sig"))
            self.import_rows(rows)
        except Exception as exc:
            self.window._error(
                "账号文件读取失败，请检查 UTF-8 格式或大小。"
                if isinstance(exc, UnicodeError)
                else str(exc)
            )

    def paste_dialog(self):
        if not self.allowed():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("粘贴已授权账号（输入内容可见）")
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setPlaceholderText(
            "每行：COM标识（可选） 手机号 Token；不要粘贴未授权账号。"
        )
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.import_text(text.toPlainText())

    def import_text(self, text):
        if not self.allowed():
            return
        try:
            self.import_rows(parse_account_text(text))
        except Exception as exc:
            self.window._error(str(exc))

    def import_rows(self, rows):
        if not rows:
            self.window._error("没有账号记录。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("账号导入预览")
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                f"共 {len(rows)} 条，预览前 50 条，手机号与 Token 已遮蔽；不会自动上传。"
            )
        )
        table = QTableWidget(min(50, len(rows)), 3)
        table.setHorizontalHeaderLabels(["行号", "手机号", "Token（遮蔽）"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for index, row in enumerate(rows[:50]):
            for column, value in enumerate(
                [
                    str(getattr(row, "line_number", index + 2)),
                    mask_phone(str(row.get("phone") or "")),
                    mask_code(str(row.get("token") or "")),
                ]
            ):
                table.setItem(index, column, QTableWidgetItem(value))
        layout.addWidget(table)
        authorized = QCheckBox("我确认已获授权保存和管理这些账号及 Token")
        layout.addWidget(authorized)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        if not authorized.isChecked():
            self.window._error("未确认账号管理授权，没有导入。")
            return
        report = self.repository.import_rows(self.window.current_store, rows)
        self.reports[self.window.current_store] = report
        self.window._refresh_all()
        self.show_report()

    def show_report(self):
        report = self.reports.get(self.window.current_store)
        if report is None:
            self.window._error("本次运行没有此门店的账号导入报告。")
            return
        ImportReportDialog(report, self).exec()

    def export(self):
        rows = self.repository.list(self.window.current_store, self.search.text())
        self.window._export_csv(
            "导出账号（不含Token）",
            ["来源", "手机号（遮蔽）", "新用户", "备注"],
            [
                [
                    r["label"],
                    mask_phone(r["phone"]),
                    "未知" if r["is_new_user"] is None else str(bool(r["is_new_user"])),
                    r["note"],
                ]
                for r in rows
            ],
        )

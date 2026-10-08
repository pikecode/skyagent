"""Ledger editors, import previews and privacy-preserving result reports."""

from __future__ import annotations

import csv

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from skyagent_manager.db import BENEFIT_KIND_LABELS, BENEFIT_KINDS, BENEFIT_STATES
from skyagent_manager.imports import ImportReport


class MemberDialog(QDialog):
    def __init__(self, parent=None, *, record=None):
        super().__init__(parent)
        record = dict(record) if record is not None else {}
        editing = bool(record)
        self.setWindowTitle("编辑会员" if editing else "添加会员")
        form = QFormLayout(self)
        self.name = QLineEdit(record.get("display_name", ""))
        self.phone = QLineEdit(record.get("phone", ""))
        self.phone.setPlaceholderText("仅用于本地台账匹配")
        self.note = QLineEdit()
        self.note.setMaxLength(max(32767, len(record.get("note", ""))))
        self.note.setText(record.get("note", ""))
        self.authorized = QCheckBox("我确认已获授权保存和管理这条会员资料")
        form.addRow("姓名 / 标记", self.name)
        form.addRow("手机号", self.phone)
        if editing:
            self.phone.setEchoMode(QLineEdit.Password)
            reveal = QCheckBox("显示完整手机号")
            reveal.toggled.connect(
                lambda checked: self.phone.setEchoMode(
                    QLineEdit.Normal if checked else QLineEdit.Password
                )
            )
            form.addRow(reveal)
        form.addRow("备注", self.note)
        form.addRow(self.authorized)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class BenefitDialog(QDialog):
    def __init__(self, parent=None, *, record=None):
        super().__init__(parent)
        record = dict(record) if record is not None else {}
        editing = bool(record)
        self.setWindowTitle("编辑权益" if editing else "登记权益")
        form = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItems(list(BENEFIT_KINDS))
        self.kind.setCurrentText(
            BENEFIT_KIND_LABELS.get(record.get("kind"), "其他" if editing else "早餐券")
        )
        self.name = QLineEdit(record.get("name", ""))
        self.code = QLineEdit(record.get("code", ""))
        self.code.setPlaceholderText("可留空；券码保存在本机加密数据库")
        self.expiry = QLineEdit(record.get("expires_at", ""))
        self.expiry.setPlaceholderText("YYYY-MM-DD，可留空")
        self.quantity = QSpinBox()
        self.quantity.setRange(0, max(1_000_000, record.get("quantity", 1)))
        self.quantity.setValue(record.get("quantity", 1))
        self.phone = QLineEdit(record.get("member_phone") or "")
        self.phone.setPlaceholderText("可选：关联本门店的会员手机号")
        self.note = QLineEdit()
        self.note.setMaxLength(max(32767, len(record.get("note", ""))))
        self.note.setText(record.get("note", ""))
        self.state = QComboBox()
        self.state.addItems(sorted(BENEFIT_STATES))
        self.state.setCurrentText(record.get("state", "可用"))
        form.addRow("类型", self.kind)
        form.addRow("名称", self.name)
        form.addRow("券码", self.code)
        form.addRow("到期日", self.expiry)
        form.addRow("数量", self.quantity)
        form.addRow("会员手机号", self.phone)
        if editing:
            self.code.setEchoMode(QLineEdit.Password)
            self.phone.setEchoMode(QLineEdit.Password)
            reveal = QCheckBox("显示完整券码与手机号")

            def set_visibility(checked):
                for field in (self.code, self.phone):
                    field.setEchoMode(
                        QLineEdit.Normal if checked else QLineEdit.Password
                    )

            reveal.toggled.connect(set_visibility)
            form.addRow(reveal)
            form.addRow("状态", self.state)
        form.addRow("备注", self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class ImportPreviewDialog(QDialog):
    def __init__(
        self,
        headers: list[str],
        rows: list[list[str]],
        parent=None,
        *,
        total: int | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("导入预览")
        self.resize(820, 400)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"共 {len(rows) if total is None else total} 行，显示前 50 行；手机号和券码已遮蔽。"
            )
        )
        table = QTableWidget(min(50, len(rows)), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        for row_index, row in enumerate(rows[:50]):
            for col_index, value in enumerate(row):
                table.setItem(row_index, col_index, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("导入", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ImportReportDialog(QDialog):
    def __init__(self, report: ImportReport, parent=None):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("导入结果")
        self.resize(780, 440)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"新增 {report.inserted} 条，跳过 {report.skipped} 条。显示前 1,000 条结果，可导出全部；报告不含手机号或券码。"
            )
        )
        self.failed_only = QCheckBox("仅显示跳过行")
        self.failed_only.setChecked(report.skipped > 0)
        self.failed_only.toggled.connect(self._populate)
        layout.addWidget(self.failed_only)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["CSV 行号", "结果", "原因"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        export = QPushButton("导出全部结果 CSV")
        export.clicked.connect(self._export)
        layout.addWidget(export)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self._populate()

    def _populate(self, *_args):
        rows = [
            row
            for row in self.report.rows
            if not self.failed_only.isChecked() or not row.imported
        ][:1000]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, text in enumerate(
                [str(row.row_number), "已导入" if row.imported else "跳过", row.reason]
            ):
                self.table.setItem(index, column, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()

    def _export(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "保存导入结果", "导入结果.csv", "CSV 文件 (*.csv)"
        )
        if not filename:
            return
        try:
            with open(filename, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow(["CSV 行号", "结果", "原因"])
                writer.writerows(
                    (row.row_number, "已导入" if row.imported else "跳过", row.reason)
                    for row in self.report.rows
                )
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

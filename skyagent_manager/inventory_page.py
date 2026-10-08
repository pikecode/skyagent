"""Masked stock, invitation library and explicitly manual local simulations."""

import csv
import io
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from skyagent_manager.db import mask_code
from skyagent_manager.imports import CsvRow, read_csv
from skyagent_manager.inventory import KINDS, Inventory
from skyagent_manager.ledger_dialogs import ImportPreviewDialog, ImportReportDialog
from skyagent_manager.pending_gift_dialog import PendingGiftDialog
from skyagent_manager.resource_links import link_source, parse_link_text, read_link_file
from skyagent_manager.security import atomic_write

STATE_LABELS = {
    "available": "可用",
    "reserved": "已预占",
    "used": "已使用",
    "running": "等待模拟结果",
    "succeeded": "模拟成功",
    "failed": "模拟失败",
    "canceled": "已取消",
    "interrupted": "已中断",
}


class InventoryPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.repository = Inventory(window.db)
        self.store_id = ""
        self.reports = {}
        self.pending_dialog = None
        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)
        layout = QVBoxLayout(content)
        note = QLabel(
            "本地模拟：预占后等待手动提交成功、失败或取消。成功会消耗本地资源，不领取真实权益、不上传账号。邀请码仅保存本地选择。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.pending_button = QPushButton("查看待核验礼包库（不联网、不可领取）")
        self.pending_button.clicked.connect(self.show_pending_gifts)
        layout.addWidget(self.pending_button)
        window.action_buttons.append(self.pending_button)
        filters = QHBoxLayout()
        self.kind = QComboBox()
        for key, label in KINDS.items():
            self.kind.addItem(label, key)
        self.state = QComboBox()
        for label, value in [
            ("所有状态", ""),
            ("可用", "available"),
            ("已预占", "reserved"),
            ("已使用", "used"),
        ]:
            self.state.addItem(label, value)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索资源内容（列表仍遮蔽）")
        for widget in (self.kind, self.state, self.search):
            filters.addWidget(widget)
        layout.addLayout(filters)
        toolbar = QHBoxLayout()
        for index, (label, callback) in enumerate(
            [
                ("导入文本", self.import_dialog),
                ("导入链接文本（离线）", self.import_links_dialog),
                ("导入链接 TXT（离线）", self.import_links_file),
                ("导入 TXT/CSV", self.import_file),
                ("查看导入报告", self.show_report),
                ("删除选中", self.delete),
                ("编辑邀请码", self.edit_invite),
                ("导出筛选列表", self.export),
                ("选用邀请码", lambda: self.select_invite(False)),
                ("清除邀请码选择", lambda: self.select_invite(True)),
                ("勾选可见可用资源", self.check_visible),
                ("预占并创建模拟任务", self.start),
            ]
        ):
            if index == 5:
                layout.addLayout(toolbar)
                toolbar = QHBoxLayout()
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
            window.action_buttons.append(button)
        layout.addLayout(toolbar)
        self.selected_invite = QLabel()
        layout.addWidget(self.selected_invite)
        tabs = QTabWidget()
        self.table = self._table(["勾选", "类型", "内容（遮蔽）", "状态", "邀请码备注"])
        tabs.addTab(self.table, "资源 / 邀请码")
        task_page = QWidget()
        task_layout = QVBoxLayout(task_page)
        task_bar = QHBoxLayout()
        for label, outcome in [
            ("提交模拟成功", "succeeded"),
            ("提交模拟失败", "failed"),
            ("取消选中任务", "canceled"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, outcome=outcome: self.finish(outcome)
            )
            task_bar.addWidget(button)
            window.action_buttons.append(button)
        task_layout.addLayout(task_bar)
        export_task_button = QPushButton("导出选中模拟任务报告")
        export_task_button.clicked.connect(self.export_task)
        window.action_buttons.append(export_task_button)
        task_layout.addWidget(export_task_button)
        runner_bar = QHBoxLayout()
        self.simulation_mode = QComboBox()
        for label, mode in [
            ("全部模拟成功", "success"),
            ("全部模拟失败", "failure"),
            ("交替模拟成功/失败", "alternating"),
        ]:
            self.simulation_mode.addItem(label, mode)
        runner_bar.addWidget(self.simulation_mode)
        run_button = QPushButton("自动执行选中模拟任务")
        run_button.clicked.connect(self.run_task)
        window.action_buttons.append(run_button)
        runner_bar.addWidget(run_button)
        task_layout.addLayout(runner_bar)
        self.task_table = self._table(
            ["任务 ID", "状态", "资源数量", "创建时间（UTC）", "已处理 / 总数"]
        )
        task_layout.addWidget(self.task_table)
        item_bar = QHBoxLayout()
        for label, succeeded in [("选中项模拟成功", True), ("选中项模拟失败", False)]:
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, succeeded=succeeded: self.complete_item(succeeded)
            )
            window.action_buttons.append(button)
            item_bar.addWidget(button)
        task_layout.addLayout(item_bar)
        self.item_table = self._table(["资源 ID", "内容（遮蔽）", "处理状态"])
        task_layout.addWidget(self.item_table)
        self.task_table.itemSelectionChanged.connect(self.refresh_items)
        tabs.addTab(task_page, "本地模拟任务")
        layout.addWidget(tabs)
        self.kind.currentIndexChanged.connect(self.refresh)
        self.state.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        return table

    def allowed(self):
        return bool(
            self.window.current_store
            and not self.window.current_archived
            and self.window.sync_worker is None
        )

    def _run(self, callback):
        if not self.allowed():
            return
        try:
            callback()
            self.window._refresh_all()
        except Exception as exc:
            self.window._error(str(exc))

    def refresh(self, *_args):
        sid = self.window.current_store
        checked = set(self.checked_ids()) if self.store_id == sid else set()
        if self.store_id != sid:
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self.store_id = sid
        rows = [
            r
            for r in self.repository.list(sid)
            if r["kind"] == self.kind.currentData()
            and (not self.state.currentData() or r["state"] == self.state.currentData())
            and self.search.text().strip().casefold()
            in (r["value"] + " " + self.repository.invite_note(sid, r["id"])).casefold()
        ]
        self.table.setRowCount(len(rows))
        self.table.setCurrentItem(None)
        for index, row in enumerate(rows):
            check = QTableWidgetItem()
            check.setData(Qt.UserRole, row["id"])
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if row["state"] == "available":
                check.setFlags(check.flags() | Qt.ItemIsUserCheckable)
                check.setCheckState(
                    Qt.Checked if row["id"] in checked else Qt.Unchecked
                )
            self.table.setItem(index, 0, check)
            for col, value in enumerate(
                [
                    KINDS[row["kind"]],
                    mask_code(row["value"]),
                    STATE_LABELS[row["state"]],
                    self.repository.invite_note(sid, row["id"])
                    if row["kind"] == "invite"
                    else "",
                ],
                1,
            ):
                self.table.setItem(index, col, QTableWidgetItem(value))
        tasks = self.repository.tasks(sid)
        selected = self.task_table.item(self.task_table.currentRow(), 0)
        selected_id = selected.text() if selected else ""
        self.task_table.setRowCount(len(tasks))
        self.task_table.setCurrentItem(None)
        for index, task in enumerate(tasks):
            for col, value in enumerate(
                [
                    task["id"],
                    STATE_LABELS[task["state"]],
                    str(task["total"]),
                    task["created_at"],
                    f"{task['completed'] or 0} / {task['total']}",
                ]
            ):
                self.task_table.setItem(index, col, QTableWidgetItem(value))
            if task["id"] == selected_id and sid == self.store_id:
                self.task_table.setCurrentCell(index, 0)
        invite_ids = self.repository.selected_invites(sid)
        invites = [r for r in self.repository.list(sid) if r["id"] in invite_ids]
        self.selected_invite.setText(
            "当前邀请码："
            + (
                f"已选 {len(invites)} 条 / "
                + "、".join(mask_code(r["value"]) for r in invites[:3])
                if invites
                else "未选择"
            )
        )
        self.table.resizeColumnsToContents()
        self.task_table.resizeColumnsToContents()
        self.refresh_items()

    def show_pending_gifts(self):
        if not self.allowed():
            return
        try:
            self.pending_dialog = PendingGiftDialog(self.window)
            self.pending_dialog.exec()
        except Exception:
            self.window._error("待核验礼包库无法打开；未联网、未领取。")
        finally:
            self.pending_dialog = None

    def run_task(self):
        if not self.allowed():
            return
        selected = self.task_table.item(self.task_table.currentRow(), 0)
        if not selected:
            self.window._error("请选择一个待完成的本地模拟任务。")
            return
        task_id, mode = selected.text(), self.simulation_mode.currentData()
        if (
            QMessageBox.question(
                self,
                "自动本地模拟",
                "按所选模式自动提交剩余任务项；模拟成功会消耗本地资源。没有真实领取或自动账号上传，继续？",
            )
            == QMessageBox.Yes
        ):
            self.window._start_local_task(task_id, mode)

    def refresh_items(self):
        selected = self.task_table.item(self.task_table.currentRow(), 0)
        rows = (
            self.repository.task_items(self.window.current_store, selected.text())
            if selected
            else []
        )
        self.item_table.setRowCount(len(rows))
        self.item_table.setCurrentItem(None)
        for index, row in enumerate(rows):
            for column, value in enumerate(
                [
                    row["stock_id"],
                    mask_code(row["value"]),
                    {"reserved": "待处理", "used": "模拟成功", "released": "已释放"}[
                        row["state"]
                    ],
                ]
            ):
                self.item_table.setItem(index, column, QTableWidgetItem(value))
        self.item_table.resizeColumnsToContents()

    def complete_item(self, succeeded):
        if not self.allowed():
            return
        task = self.task_table.item(self.task_table.currentRow(), 0)
        item = self.item_table.item(self.item_table.currentRow(), 0)
        if not task or not item:
            self.window._error("请选择任务和一个任务项。")
            return
        task_id, stock_id = task.text(), item.text()
        if (
            QMessageBox.question(
                self,
                "逐项本地模拟",
                "成功会消耗当前资源；失败会释放。不会调用外部接口，确认提交？",
            )
            == QMessageBox.Yes
        ):
            self._run(
                lambda: self.repository.complete_item(
                    self.window.current_store, task_id, stock_id, succeeded=succeeded
                )
            )

    def checked_ids(self):
        return [
            self.table.item(i, 0).data(Qt.UserRole)
            for i in range(self.table.rowCount())
            if self.table.item(i, 0)
            and self.table.item(i, 0).checkState() == Qt.Checked
        ]

    def check_visible(self):
        if self.allowed():
            for i in range(self.table.rowCount()):
                item = self.table.item(i, 0)
                if item.flags() & Qt.ItemIsUserCheckable:
                    item.setCheckState(Qt.Checked)

    def selected_id(self):
        item = self.table.item(self.table.currentRow(), 0)
        if not item:
            raise ValueError("请先选择一条资源或邀请码。")
        return item.data(Qt.UserRole)

    def import_text(self, text):
        if not self.allowed():
            return
        if len(text.encode("utf-8")) > 20 * 1024 * 1024:
            raise ValueError("单次导入最多 20 MiB。")
        values = [
            (index, line.strip())
            for index, line in enumerate(text.splitlines(), 1)
            if line.strip()
        ]
        if not values or len(values) > 20000:
            raise ValueError("请输入 1 至 20000 条资源，每行一条。")
        if (
            QMessageBox.question(
                self,
                "确认导入",
                f"将向当前门店的{self.kind.currentText()}导入 {len(values)} 条；重复值跳过。请确认已获授权。",
            )
            != QMessageBox.Yes
        ):
            return
        self._run(lambda: self._import(values))

    def _import(self, values):
        report = self.repository.import_rows(
            self.window.current_store,
            [
                CsvRow({"kind": self.kind.currentData(), "value": value}, index)
                for index, value in values
            ],
        )
        self.reports[self.window.current_store] = report
        self.window.status.setText(
            f"资源新增 {report.inserted} 条，跳过 {report.skipped} 条；未上传，可查看导入报告。"
        )

    def import_dialog(self):
        if not self.allowed():
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "导入资源（输入内容可见）", "每行一条，不解析逗号或连字符；空行忽略"
        )
        if ok:
            try:
                self.import_text(text)
            except Exception as exc:
                self.window._error(str(exc))

    def import_links_dialog(self):
        if not self.allowed():
            return
        text, ok = QInputDialog.getMultiLineText(
            self,
            "离线链接导入（输入内容可见）",
            "每行一条链接，可带说明文字；可追加制表符及 available/used 或 未用/已用。\n"
            "仅提取文本，不打开链接、不解析短链接；未知状态或多链接行跳过。",
        )
        if ok:
            try:
                self.import_link_text(text)
            except Exception as exc:
                self.window._error(str(exc))

    def import_link_text(self, text):
        if not self.allowed():
            return
        rows = parse_link_text(text, self.kind.currentData())
        self._confirm_link_rows(rows)

    def import_links_file(self):
        if not self.allowed():
            return
        store_id, kind = self.window.current_store, self.kind.currentData()
        name, _ = QFileDialog.getOpenFileName(
            self, "离线链接文件（不访问链接）", "", "UTF-8 链接文件 (*.txt)"
        )
        if not name:
            return
        try:
            if store_id != self.window.current_store or kind != self.kind.currentData():
                raise ValueError("选择文件期间门店或资源类型已变化，请重新导入。")
            rows = read_link_file(name, kind)
            self._confirm_link_rows(rows)
        except Exception as exc:
            self.window._error(str(exc))

    def _confirm_link_rows(self, rows):
        if not self.allowed():
            return
        store_id, kind = self.window.current_store, self.kind.currentData()
        counts = {}
        for row in rows:
            source = link_source(row["value"])
            counts[source] = counts.get(source, 0) + 1
        if (
            QMessageBox.question(
                self,
                "确认离线链接导入",
                "请确认已获授权。域名识别不代表链接有效或可领取；不进行网络请求。\n"
                + "\n".join(
                    f"{source}：{count} 行" for source, count in counts.items()
                ),
            )
            != QMessageBox.Yes
        ):
            return
        if store_id != self.window.current_store or kind != self.kind.currentData():
            raise ValueError("确认期间门店或资源类型已变化，请重新导入。")
        if self.preview_csv(rows):
            if store_id != self.window.current_store or kind != self.kind.currentData():
                raise ValueError("预览期间门店或资源类型已变化，请重新导入。")
            self._run(lambda: self._import_csv(rows))

    def import_file(self):
        if not self.allowed():
            return
        name, _ = QFileDialog.getOpenFileName(
            self, "导入 UTF-8 资源文件", "", "资源文件 (*.txt *.csv)"
        )
        if not name:
            return
        try:
            if Path(name).suffix.lower() == ".csv":
                rows = read_csv(Path(name), "stock")
                if (
                    QMessageBox.question(
                        self,
                        "确认资源 CSV",
                        f"向当前门店导入 {len(rows)} 条；类型使用 CSV 的 kind 列，不受页面类型筛选影响。确认已获授权？",
                    )
                    != QMessageBox.Yes
                ):
                    return
                if not self.preview_csv(rows):
                    return
                self._run(lambda: self._import_csv(rows))
                return
            with Path(name).open("rb") as stream:
                data = stream.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("单次导入最多 20 MiB。")
            self.import_text(data.decode("utf-8-sig"))
        except UnicodeError:
            self.window._error("文本必须使用 UTF-8 编码。")
        except Exception as exc:
            self.window._error(str(exc))

    def preview_csv(self, rows):
        store_id = self.window.current_store
        display = []
        for index, row in enumerate(rows[:50], 1):
            kind = row.get("kind")
            value = row.get("value")
            state = row.get("state", "")
            note = row.get("note", "")
            kind = kind.strip() if isinstance(kind, str) else ""
            state = (
                (state.strip() or "available") if isinstance(state, str) else "invalid"
            )
            display.append(
                [
                    str(getattr(row, "line_number", index)),
                    KINDS.get(kind, "类型无效，将跳过"),
                    mask_code(value) if isinstance(value, str) else "内容格式无效",
                    {"available": "可用", "used": "已使用（不可预占）"}.get(
                        state, "状态无效，将跳过"
                    ),
                    f"有备注（{len(note)} 字符，内容不展示）"
                    if isinstance(note, str) and note
                    else "无备注"
                    if isinstance(note, str)
                    else "备注格式无效",
                ]
            )
        dialog = ImportPreviewDialog(
            ["源行号", "类型", "资源内容（遮蔽）", "目标状态", "备注情况"],
            display,
            self,
            total=len(rows),
        )
        dialog.setWindowTitle("资源 CSV 脱敏预览：确认目标门店后保存")
        store_name = next(
            (
                row["name"]
                for row in self.window.db.list_stores()
                if row["id"] == store_id
            ),
            "",
        )
        target = QLabel(f"目标门店：{store_name}（{store_id[:8]}）")
        target.setTextFormat(Qt.PlainText)
        dialog.layout().insertWidget(0, target)
        accepted = dialog.exec() == QDialog.Accepted
        if accepted and store_id != self.window.current_store:
            raise ValueError("预览期间门店已变化，请重新选择文件并确认。")
        return accepted and self.allowed()

    def _import_csv(self, rows):
        report = self.repository.import_rows(self.window.current_store, rows)
        self.reports[self.window.current_store] = report
        self.window.status.setText(
            f"资源新增 {report.inserted} 条，跳过 {report.skipped} 条；未上传，可查看导入报告。"
        )

    def show_report(self):
        report = self.reports.get(self.window.current_store)
        if report is None:
            self.window._error("当前门店本次运行没有资源导入报告。")
            return
        dialog = ImportReportDialog(report, self)
        dialog.setWindowTitle("资源导入结果")
        dialog.table.setHorizontalHeaderLabels(["源文件行号", "结果", "原因"])
        dialog.exec()

    def delete(self):
        if (
            self.allowed()
            and QMessageBox.question(
                self, "删除资源", "只删除当前选中且未关联任务的可用资源，确认删除？"
            )
            == QMessageBox.Yes
        ):
            self._run(
                lambda: self.repository.delete(
                    self.window.current_store, self.selected_id()
                )
            )

    def edit_invite(self):
        if not self.allowed() or self.kind.currentData() != "invite":
            return
        try:
            rid = self.selected_id()
        except ValueError as exc:
            self.window._error(str(exc))
            return
        value, ok = QInputDialog.getText(
            self, "编辑邀请码", "输入新邀请码（输入遮蔽）", QLineEdit.Password
        )
        if ok:
            note, accepted = QInputDialog.getText(
                self,
                "邀请码备注",
                "备注（可见，最多 2000 字符）",
                QLineEdit.Normal,
                self.repository.invite_note(self.window.current_store, rid),
            )
            if not accepted:
                return
            self._run(
                lambda: self.repository.edit_invite(
                    self.window.current_store, rid, value, note
                )
            )

    def select_invite(self, clear):
        self._run(
            lambda: self.repository.select_invites(
                self.window.current_store,
                [] if clear else self.checked_ids() or [self.selected_id()],
            )
        )

    def export(self):
        if not self.allowed():
            return
        full = QMessageBox.question(
            self,
            "导出内容",
            "是否导出完整资源/邀请码？选择否仅导出遮蔽内容。完整 CSV 是明文敏感文件，请妥善保存。",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
        )
        if full == QMessageBox.Cancel:
            return
        name, _ = QFileDialog.getSaveFileName(
            self, "导出当前筛选列表", "库存.csv", "CSV (*.csv)"
        )
        if not name:
            return
        try:
            self.export_to(Path(name), full=full == QMessageBox.Yes)
            self.window.status.setText("已导出当前筛选资源；CSV 为明文，未上传。")
        except Exception as exc:
            self.window._error(str(exc))

    def export_to(self, path, *, full=False):
        if not self.allowed():
            raise ValueError("当前门店不可导出。")
        path = Path(path)
        if (
            path.resolve() == self.window.db.path.resolve()
            or path.suffix.lower() != ".csv"
        ):
            raise ValueError("请使用 CSV 文件，不能覆盖数据库。")
        ids = {
            self.table.item(i, 0).data(Qt.UserRole)
            for i in range(self.table.rowCount())
        }
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["kind", "value", "state", "note"])

        def safe(value):
            return (
                "'" + value
                if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                else value
            )

        for row in self.repository.list(self.window.current_store):
            if row["id"] in ids:
                writer.writerow(
                    [
                        row["kind"],
                        safe(row["value"] if full else mask_code(row["value"])),
                        row["state"],
                        safe(
                            self.repository.invite_note(
                                self.window.current_store, row["id"]
                            )
                        ),
                    ]
                )
        atomic_write(path, stream.getvalue().encode("utf-8-sig"))

    def export_task(self):
        if not self.allowed():
            return
        selected = self.task_table.item(self.task_table.currentRow(), 0)
        if selected is None:
            self.window._error("请选择一个本地模拟任务。")
            return
        store_id, task_id = self.window.current_store, selected.text()
        name, _ = QFileDialog.getSaveFileName(
            self, "导出脱敏模拟任务报告", "模拟任务报告.csv", "CSV (*.csv)"
        )
        if not name:
            return
        try:
            if self.window.current_store != store_id:
                raise ValueError("目标门店已变化，请重新选择任务。")
            self.export_task_to(Path(name), task_id)
            self.window.status.setText(
                "已导出本地模拟任务报告；不是实际业务成功凭证，未上传。"
            )
        except Exception as exc:
            self.window._error(str(exc))

    def export_task_to(self, path, task_id):
        if not self.allowed():
            raise ValueError("当前门店不可导出。")
        path = Path(path)
        if (
            path.suffix.lower() != ".csv"
            or path.resolve() == self.window.db.path.resolve()
        ):
            raise ValueError("请使用 CSV 文件，不能覆盖数据库。")
        store_id = self.window.current_store
        task = next(
            (row for row in self.repository.tasks(store_id) if row["id"] == task_id),
            None,
        )
        if task is None:
            raise ValueError("当前门店不存在此模拟任务。")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "scope",
                "store_id",
                "task_id",
                "task_state",
                "created_at_utc",
                "completed",
                "total",
                "stock_id",
                "kind",
                "item_state",
            ]
        )
        for item in self.repository.task_items(store_id, task_id):
            values = [
                "local-simulation-only",
                store_id,
                task_id,
                task["state"],
                task["created_at"],
                str(task["completed"] or 0),
                str(task["total"]),
                item["stock_id"],
                item["kind"],
                item["state"],
            ]
            writer.writerow(
                [
                    "'" + value
                    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                    else value
                    for value in values
                ]
            )
        atomic_write(path, stream.getvalue().encode("utf-8-sig"))

    def start(self):
        if not self.allowed():
            return
        ids = self.checked_ids()
        if not ids:
            self.window._error("请勾选可用资源。")
            return
        if (
            QMessageBox.question(
                self,
                "本地模拟预占",
                f"预占 {len(ids)} 条本地资源；等待手动提交结果，不执行真实业务。继续？",
            )
            == QMessageBox.Yes
        ):
            self._run(lambda: self.repository.start(self.window.current_store, ids))

    def finish(self, outcome):
        if not self.allowed():
            return
        item = self.task_table.item(self.task_table.currentRow(), 0)
        if not item:
            self.window._error("请先选择一个本地模拟任务。")
            return
        task_id = item.text()
        message = (
            "模拟成功会永久标记关联本地资源为已使用。"
            if outcome == "succeeded"
            else "将释放该任务预占的本地资源。"
        )
        if (
            QMessageBox.question(self, "提交本地模拟结果", message + "继续？")
            == QMessageBox.Yes
        ):
            self._run(
                lambda: self.repository.finish(
                    self.window.current_store, task_id, outcome
                )
            )

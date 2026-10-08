"""Offline, masked saved coupon outputs; never calls a share/query adapter."""

from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from skyagent_manager.account_benefits import KINDS
from skyagent_manager.coupon_sharing import CouponShareOutputs
from skyagent_manager.gift_sharing import GiftShareOutputs, PendingGiftInventory
from skyagent_manager.share_safety import require_share_writes_allowed


class SavedCouponSharesDialog(QDialog):
    def __init__(self, page, *, gift=False):
        super().__init__(page)
        self.page, self.window = page, page.window
        self.owner = page.session._identity(
            self.window.current_store, page.account.currentData()
        )[1]
        self.mode = page.mode.currentData()
        self.database = self.window.db
        self.path = self.database.path.resolve()
        self.rows = ()
        self.gift = gift
        self.repository = (
            GiftShareOutputs(self.database)
            if gift
            else CouponShareOutputs(self.database)
        )
        self.setWindowTitle(
            "本地礼包成功记录（不联网）" if gift else "本地已保存分享（不联网）"
        )
        self.resize(900, 480)
        layout = QVBoxLayout(self)
        note = QLabel(
            "仅显示当前账号已加密保存的礼包生成成功码。可另行确认登记待核验，不能领取或转可用库存；资格/有效期未知。完整码导出是敏感明文，需单独确认。"
            if gift
            else "仅显示当前门店/账号已加密保存的成功结果，不重新查询或生成链接。原券有效期不等于分享链接当前可领取证明。旧版未保存有效期的结果不能从此入口加入可用库存。完整链接导出是敏感明文，需单独确认。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QHBoxLayout()
        self.action_buttons = []
        for label, callback in [
            ("刷新本地记录", self.refresh),
            (
                "确认登记待核验礼包" if gift else "确认选中结果入资源库存",
                self.import_selected,
            ),
            ("导出脱敏记录", self.export_masked),
            (
                "确认导出选中完整礼包码" if gift else "确认导出选中完整链接",
                self.export_full,
            ),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button)
            self.action_buttons.append(button)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["操作摘要", "类型", "保存时间", "有效期", "库存限制", "礼包码（隐藏）"]
            if gift
            else [
                "操作摘要",
                "类型",
                "保存时间",
                "原券有效期",
                "库存处理",
                "链接（隐藏）",
            ]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def _guard(self):
        if (
            self.window.db is not self.database
            or self.database.path.resolve() != self.path
            or not self.page.allowed()
            or self.page.mode.currentData() != self.mode
            or self.page.session._identity(
                self.window.current_store, self.page.account.currentData()
            )[1]
            != self.owner
        ):
            raise ValueError(
                "门店、账号、Token、模式或任务状态已变化，请关闭后重新打开。"
            )

    def invalidate(self):
        self.rows = ()
        self.table.setRowCount(0)
        for button in self.action_buttons:
            button.setEnabled(False)
        self.status.setText("上下文已变化，旧列表已清除；请关闭后重新打开。")

    def _error(self, message):
        self.status.setText(message)

    def refresh(self):
        self.rows = ()
        self.table.setRowCount(0)
        try:
            self._guard()
            self.rows = self.repository.list_saved(self.owner[0], self.owner[1])
            self.table.setRowCount(len(self.rows))
            for index, row in enumerate(self.rows):
                values = (
                    [
                        row.operation_id[:8],
                        "订房礼包",
                        row.created_at,
                        "未知",
                        "已登记待核验，不可用"
                        if row.pending_id
                        else "未登记，不能推断可用",
                        "完整码隐藏",
                    ]
                    if self.gift
                    else [
                        row.operation_id[:8],
                        KINDS[row.kind],
                        row.created_at,
                        row.expiry or "未知（旧版未保存）",
                        "已处理入库存" if row.stock_id else "未入库存",
                        (urlsplit(row.url).hostname or "") + "（完整链接隐藏）",
                    ]
                )
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    if column == 0:
                        cell.setData(Qt.UserRole, row.operation_id)
                    self.table.setItem(index, column, cell)
            self.table.resizeColumnsToContents()
            self.status.setText(
                f"本地已保存成功结果 {len(self.rows)} 条；未查询第三方，不代表仍可领取"
            )
        except Exception as exc:
            self.rows = ()
            self.table.setRowCount(0)
            self._error(str(exc))

    def _selected(self):
        self._guard()
        cell = self.table.item(self.table.currentRow(), 0)
        if cell is None:
            raise ValueError("请先选择一条已保存结果。")
        row = next(
            (row for row in self.rows if row.operation_id == cell.data(Qt.UserRole)),
            None,
        )
        if (
            row is None
            or self.repository.load_operation(
                self.owner[0], self.owner[1], row.operation_id
            )
            != row
        ):
            raise ValueError("结果已变化，请刷新后重新选择。")
        return row

    def import_selected(self):
        if self.gift:
            self._register_pending()
            return
        try:
            row = self._selected()
            require_share_writes_allowed(self.database)
            if row.stock_id:
                raise ValueError("该结果已处理入库存，不重复导入。")
            if not row.expiry:
                raise ValueError(
                    "旧版记录未保存有效期，不能推断可用状态；请另行核实，不重新分享。"
                )
            if (
                QMessageBox.question(
                    self,
                    "确认已保存分享入资源库存",
                    f"将当前账号已保存的{KINDS[row.kind]}链接加入本门店资源库存。\n"
                    "仅这一条链接，不按查询数量复制；法宝独立分类，不推断实际扣减数量。\n"
                    "不生成新分享，不领取，不上传；已保存结果不证明链接仍可领取。继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            if self._selected() != row:
                raise ValueError("确认期间选中结果已变化，未入库存。")
            CouponShareOutputs(self.database).import_operation(
                self.owner[0], self.owner[1], self.owner, row.operation_id, expected=row
            )
            self.window.inventory_page.refresh()
            self.window._refresh_activity()
            self.refresh()
            self.status.setText("已入本地资源库存；未领取、未同步后台，不重新生成分享")
        except Exception as exc:
            self._error(str(exc))

    def _register_pending(self):
        try:
            row = self._selected()
            require_share_writes_allowed(self.database)
            if row.pending_id:
                raise ValueError("该礼包已登记待核验，不重复登记。")
            if (
                QMessageBox.question(
                    self,
                    "确认登记待核验礼包",
                    "将选中已保存成功结果登记到本门店独立待核验礼包库。资格/有效期未知，不加入可用库存、不预占、不领取、不生成新码、不上传。没有转可用或解除入口。继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            if self._selected() != row:
                raise ValueError("确认期间结果或归属已变化，未登记。")
            PendingGiftInventory(self.database).register(
                self.owner[0],
                self.owner[1],
                self.owner,
                row.operation_id,
                expected=row,
            )
            self.refresh()
            self.window._refresh_activity()
            self.status.setText(
                "已登记待核验；日期/资格未知，未加入可用库存、未领取、未上传"
            )
        except Exception as exc:
            self._error(str(exc))

    def export_masked(self):
        self._export(False)

    def export_full(self):
        self._export(True)

    def _export(self, full):
        try:
            self._guard()
            rows = (self._selected(),) if full else self.rows
            if not rows:
                raise ValueError("没有可导出的已保存结果。")
            if full:
                require_share_writes_allowed(self.database)
                if (
                    QMessageBox.question(
                        self,
                        "确认导出敏感完整礼包码"
                        if self.gift
                        else "确认导出敏感完整分享链接",
                        "将把选中一条完整礼包码写入明文CSV，持有者可能使用权益，请安全保管。不重新生成、不领取、不上传，不证明有效期或资格。继续？"
                        if self.gift
                        else "仅导出当前选中结果的完整链接到明文CSV，获得文件的人可能使用该链接，请安全保管。\n不重新分享、不打开、不上传，也不保证链接仍有效；导出后不修改入库或分享状态。继续？",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    != QMessageBox.Yes
                ):
                    return
                if self._selected() != rows[0]:
                    raise ValueError("确认期间选中结果已变化，未导出。")
            name, _ = QFileDialog.getSaveFileName(
                self,
                "导出已保存礼包码"
                if self.gift and full
                else "导出已保存分享链接"
                if full
                else "导出已保存分享脱敏记录",
                "已保存礼包.csv" if self.gift else "已保存分享.csv",
                "CSV 文件 (*.csv)",
            )
            if not name:
                return
            self._guard()
            if full and self._selected() != rows[0]:
                raise ValueError("选择文件期间结果已变化，未导出。")
            self.repository.export_to(
                name, self.owner[0], self.owner[1], self.owner, rows, full=full
            )
            self.status.setText(
                f"已导出{len(rows)}条{'敏感完整礼包码（明文）' if full and self.gift else '敏感完整链接（明文）' if full else '脱敏记录'}；未修改分享或库存状态"
            )
        except Exception as exc:
            self._error(str(exc))

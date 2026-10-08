"""Read-only store-local gift quarantine; never displays or claims gift codes."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from skyagent_manager.gift_sharing import PendingGiftInventory


class PendingGiftDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.database = window, window.db
        self.store_id = window.current_store
        self.path = self.database.path.resolve()
        self.rows = ()
        self.setWindowTitle("待核验礼包库（不联网、不可领取）")
        self.resize(850, 430)
        layout = QVBoxLayout(self)
        note = QLabel(
            "仅列出当前门店已登记的礼包成功结果引用，完整码不在此显示。有效期/领取资格未知，与可用资源及模拟任务隔离，无转可用/删除/领取入口。可从原账号礼包成功记录单独确认导出。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.refresh_button = QPushButton("刷新本地待核验记录")
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_button)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["登记摘要", "账号摘要", "操作摘要", "登记时间", "状态"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        window.store_combo.currentIndexChanged.connect(self.invalidate)
        self.refresh()

    def invalidate(self, *_args):
        self.rows = ()
        self.table.setRowCount(0)
        self.refresh_button.setEnabled(False)
        self.status.setText("门店上下文已变化，请关闭后重新打开。")

    def refresh(self):
        self.rows = ()
        self.table.setRowCount(0)
        try:
            if (
                self.window.db is not self.database
                or self.database.path.resolve() != self.path
                or self.window.current_store != self.store_id
                or self.window.current_archived
                or self.window.sync_worker is not None
            ):
                raise ValueError("门店、数据库或任务状态已变化，请关闭后重新打开。")
            self.rows = PendingGiftInventory(self.database).list(self.store_id)
            self.table.setRowCount(len(self.rows))
            for index, entry in enumerate(self.rows):
                for column, value in enumerate(
                    (
                        entry.pending_id[:8],
                        entry.account_id[:8],
                        entry.operation_id[:8],
                        entry.registered_at,
                        "待核验（不可用/不可领取）",
                    )
                ):
                    self.table.setItem(index, column, QTableWidgetItem(value))
            self.table.resizeColumnsToContents()
            self.status.setText(
                f"本地待核验 {len(self.rows)} 条；未请求第三方，不证明有效或可领取"
            )
        except Exception:
            self.rows = ()
            self.table.setRowCount(0)
            self.status.setText(
                "待核验列表无法读取或上下文已变化；未显示部分结果，不据此重新生成。"
            )

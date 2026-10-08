"""Explicit single-workspace to existing-store migration preview."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from skyagent_manager.inventory import KINDS


class MigrationDialog(QDialog):
    def __init__(self, plan, stores, selected="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("旧 JSON 迁移：脱敏预览与门店映射")
        self.resize(850, 620)
        layout = QVBoxLayout(self)
        source = QLabel(
            f"源目录：{plan.directory}\n可迁移 {len(plan.rows)} 条，解析跳过 {len(plan.skipped)} 条；只展示前 50 条。"
        )
        source.setWordWrap(True)
        layout.addWidget(source)
        layout.addWidget(QLabel("明确选择现有目标门店（不会自动创建或合并门店）："))
        self.target = QComboBox()
        for store in stores:
            self.target.addItem(store["name"], store["id"])
        index = self.target.findData(selected)
        if index >= 0:
            self.target.setCurrentIndex(index)
        layout.addWidget(self.target)
        table = QTableWidget(min(50, len(plan.rows)), 5)
        table.setHorizontalHeaderLabels(
            ["来源", "记录号", "类型", "内容（遮蔽）", "目标状态"]
        )
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for index, row in enumerate(plan.rows[:50]):
            values = [
                row.source,
                str(row.number),
                "账号" if row.kind == "account" else KINDS[row.kind],
                row.masked(),
                "新用户未知"
                if row.kind == "account"
                else "已使用"
                if row.state == "used"
                else "可用",
            ]
            for column, value in enumerate(values):
                table.setItem(index, column, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        warnings = QLabel("\n".join(plan.warnings))
        warnings.setWordWrap(True)
        layout.addWidget(warnings)
        self.authorized = QCheckBox(
            "我确认源数据授权、目标门店映射及上述不迁移项；导入前创建加密备份"
        )
        layout.addWidget(self.authorized)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save = buttons.button(QDialogButtonBox.Save)
        save.setText("备份并迁移")
        save.setEnabled(False)
        self.authorized.toggled.connect(
            lambda checked: save.setEnabled(
                checked and bool(self.target.currentData()) and bool(plan.rows)
            )
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class WorkspaceBatchDialog(QDialog):
    def __init__(self, batch, stores, parent=None):
        super().__init__(parent)
        self.setWindowTitle("多门店旧JSON：一对一映射与整批迁移")
        self.resize(950, 650)
        layout = QVBoxLayout(self)
        note = QLabel(
            "仅发现根目录直接子工作区，不递归、忽略链接目录。目标默认跳过，须手动映射现有活跃门店；不创建、不合并、不迁移配置/Secret/历史任务，不上传。一次备份后同一事务导入，任一冲突全部回滚。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(len(batch.plans), 4)
        self.table.setHorizontalHeaderLabels(
            ["旧工作区", "可迁移", "解析跳过", "目标门店（默认跳过）"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.targets = []
        for index, plan in enumerate(batch.plans):
            for column, value in enumerate(
                (plan.directory.name, str(len(plan.rows)), str(len(plan.skipped)))
            ):
                self.table.setItem(index, column, QTableWidgetItem(value))
            combo = QComboBox()
            combo.addItem("跳过此工作区", None)
            if plan.rows:
                for store in stores:
                    combo.addItem(store["name"], store["id"])
            self.targets.append(combo)
            self.table.setCellWidget(index, 3, combo)
        layout.addWidget(self.table)
        preview_rows = [
            (index, row) for index, plan in enumerate(batch.plans) for row in plan.rows
        ][:50]
        preview = QTableWidget(len(preview_rows), 4)
        preview.setHorizontalHeaderLabels(
            ["工作区编号", "记录号", "类型", "内容（遮蔽，最多50条）"]
        )
        preview.setEditTriggers(QTableWidget.NoEditTriggers)
        for index, (workspace, row) in enumerate(preview_rows):
            for column, value in enumerate(
                (
                    str(workspace + 1),
                    str(row.number),
                    "账号（新用户未知）" if row.kind == "account" else KINDS[row.kind],
                    row.masked(),
                )
            ):
                preview.setItem(index, column, QTableWidgetItem(value))
        layout.addWidget(preview)
        self.authorized = QCheckBox(
            "我确认源数据授权、逐项门店映射及不迁移项；导入前加密备份，冲突全批回滚"
        )
        layout.addWidget(self.authorized)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("备份并整批迁移")
        self.save_button.setEnabled(False)
        self.authorized.toggled.connect(self._changed)
        for combo in self.targets:
            combo.currentIndexChanged.connect(self._changed)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def mappings(self):
        return tuple(
            (index, combo.currentData())
            for index, combo in enumerate(self.targets)
            if combo.currentData() is not None
        )

    def _changed(self):
        mappings = self.mappings()
        self.save_button.setEnabled(
            self.authorized.isChecked()
            and bool(mappings)
            and len({target for _, target in mappings}) == len(mappings)
        )

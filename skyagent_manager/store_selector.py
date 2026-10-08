"""Read-only startup confirmation of the local store and configured backend."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)


class StoreSelectorDialog(QDialog):
    def __init__(self, database, parent=None):
        super().__init__(parent)
        self.database = database
        self.selected_store_id = None
        self.setWindowTitle("SkyAgent — 选择门店")
        self.resize(720, 430)
        layout = QVBoxLayout(self)
        note = QLabel(
            "请选择本次进入的门店并核对后端目标；这里只读配置，不登录、不上传、不读取Secret。不是启动密码。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.store_list = QListWidget()
        layout.addWidget(self.store_list)
        self.show_archived = QCheckBox("显示已归档门店（不能进入业务）")
        layout.addWidget(self.show_archived)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.PlainText)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.details)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.enter_button = buttons.button(QDialogButtonBox.Ok)
        self.enter_button.setText("进入选中门店")
        self.cancel_button = buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.setText("退出")
        buttons.accepted.connect(self.enter)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.store_list.currentItemChanged.connect(self.describe)
        self.store_list.itemDoubleClicked.connect(lambda *_: self.enter())
        self.show_archived.toggled.connect(self.reload)
        self.reload()

    def reload(self):
        current = self.store_list.currentItem()
        remembered = (
            current.data(Qt.UserRole)
            if current
            else self.database.get_setting("last_store_id")
        )
        self.store_list.clear()
        preferred = 0
        stores = self.database.list_stores(
            include_archived=self.show_archived.isChecked()
        )
        for index, store in enumerate(stores):
            item = QListWidgetItem(
                store["name"] + ("（已归档）" if store["archived"] else "")
            )
            item.setData(Qt.UserRole, store["id"])
            self.store_list.addItem(item)
            if store["id"] == remembered and not store["archived"]:
                preferred = index
        if stores:
            self.store_list.setCurrentRow(preferred)
        self.describe()

    def current_store(self):
        item = self.store_list.currentItem()
        if item is None:
            return None
        return next(
            (
                store
                for store in self.database.list_stores(include_archived=True)
                if store["id"] == item.data(Qt.UserRole)
            ),
            None,
        )

    def describe(self, *_):
        store = self.current_store()
        active = self.database.list_stores()
        self.enter_button.setText(
            "进入选中门店" if active else "进入门店管理 / 备份恢复"
        )
        self.enter_button.setEnabled(
            not active or (store is not None and not store["archived"])
        )
        if store is None:
            self.details.setText(
                "没有活动门店。进入后可添加门店、恢复归档门店或恢复备份。"
                if not active
                else "请选择门店。"
            )
            return
        config = self.database.get_store_sync(store["id"])
        self.details.setText(
            f"门店：{store['name']}\n状态：{'已归档，不能进入业务' if store['archived'] else '活动'}\n"
            f"后端基础地址：{config['api_url'] or '尚未配置（仅本地管理）'}\n"
            "账号入库路径：/api/open/pool/accounts/bulk-import\n"
            "本地门店隔离不等于后端租户隔离；进入不会自动同步。"
        )

    def enter(self):
        store = self.current_store()
        if not self.database.list_stores():
            self.selected_store_id = ""
        elif store is None or store["archived"]:
            self.describe()
            return
        else:
            self.selected_store_id = store["id"]
        self.accept()


def create_startup_window(path):
    from skyagent_manager.db import StoreDatabase
    from skyagent_manager.main_window import MainWindow

    database = StoreDatabase(path)
    transferred = False
    try:
        dialog = StoreSelectorDialog(database)
        if dialog.exec() != QDialog.Accepted:
            return None
        selected = dialog.selected_store_id
        if selected is None:
            raise ValueError("请选择有效门店或门店管理入口。")
        if selected:
            database._require_active_store(selected)
        window = MainWindow(path, database=database, initial_store_id=selected)
        transferred = True
        return window
    finally:
        if not transferred:
            database.close()

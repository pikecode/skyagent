"""One explicitly selected ADMIN credential, followed by separate local saving."""

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from skyagent_manager.accounts import Accounts
from skyagent_manager.db import mask_phone


class CredentialWorker(QThread):
    ready = Signal(object)
    error = Signal(str)

    def __init__(self, client, identifier, parent):
        super().__init__(parent)
        self.client, self.identifier = client, identifier
        self.cancelled = False

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            result = self.client.account_credential(
                self.identifier, authorized=True, cancelled=self.isInterruptionRequested
            )
            if not self.isInterruptionRequested():
                self.ready.emit(result)
        except Exception:
            if not self.isInterruptionRequested():
                self.error.emit(
                    "账号Token读取失败；请核对ADMIN权限，不展示响应、不自动重试。"
                )
        finally:
            self.cancelled = self.isInterruptionRequested()


class BackendAccountSaveWidget(QWidget):
    def __init__(self, backend):
        super().__init__(backend)
        self.backend, self.window = backend, backend.window
        self.pending = self.owner = self.worker = None
        layout = QVBoxLayout(self)
        self.result = QLabel(
            "管理员单账号读取：Token不展示，仅内存暂存；保存需另行确认。"
        )
        self.result.setWordWrap(True)
        layout.addWidget(self.result)
        for label, callback in (
            ("确认读取选中账号Token（ADMIN）", self.start),
            ("确认加密保存后台账号", self.save),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            layout.addWidget(button)
            self.window.action_buttons.append(button)
        backend.table.itemSelectionChanged.connect(self.invalidate)

    def selected(self):
        item = self.backend.table.item(self.backend.table.currentRow(), 0)
        return item.text() if item is not None and item.isSelected() else None

    def invalidate(self):
        self.pending = self.owner = None
        self.result.setText("未保存Token已丢弃；请确认选中账号后重新读取。")

    def controls(self):
        return (
            self.backend.table,
            self.backend.username,
            self.backend.password,
            self.backend.search,
            self.backend.page_number,
            self.backend.captcha_answer,
            self.backend.exchange_task_id,
        )

    def start(self):
        window, backend = self.window, self.backend
        if window.sync_worker is not None or window.current_archived:
            return
        backend.refresh()
        identifier, client, binding = self.selected(), backend.client, backend.binding
        if not identifier or client is None:
            window._error("请先登录后台并选中一个已授权账号。")
            return
        if (
            QMessageBox.question(
                self,
                "确认管理员凭据读取",
                "仅读取选中账号的完整Token，需要后台ADMIN及账号访问权限。HTTP会明文传输，建议HTTPS。Token仅内存暂存，不自动保存或上传；不是领取/分享。继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        backend.refresh()
        if (
            window.sync_worker is not None
            or window.current_archived
            or backend.binding != binding
            or backend.client is not client
            or self.selected() != identifier
        ):
            self.invalidate()
            window._error("账号、门店或会话已变化，请重新确认。")
            return
        self.invalidate()
        self.owner = (binding, identifier, client)
        worker = CredentialWorker(client, identifier, self)
        self.worker = window.sync_worker = worker
        backend.client = None
        window.sync_error = False
        worker.ready.connect(self.ready)
        worker.error.connect(self.result.setText)
        worker.finished.connect(self.finished)
        worker.finished.connect(window._sync_finished)
        for control in (
            *window.action_buttons,
            *self.controls(),
            window.store_combo,
            window.show_archived,
            window.archive_store_button,
        ):
            control.setEnabled(False)
        window.cancel_sync.setEnabled(True)
        window.cancel_sync.setText("取消账号Token读取")
        self.result.setText("正在读取；不会自动保存、导出或上传。")
        worker.start()

    def ready(self, result):
        worker = self.worker
        if (
            worker.isInterruptionRequested()
            or self.window.close_pending
            or self.owner is None
            or self.selected() != self.owner[1]
            or result.identifier != self.owner[1]
        ):
            return
        self.backend.refresh()
        if self.backend.binding != self.owner[0]:
            self.invalidate()
            return
        self.pending = result
        self.result.setText(
            f"已读取 {mask_phone(result.phone)} 的Token；未保存、不显示原文。"
        )

    def finished(self):
        worker = self.worker
        if (
            worker.isInterruptionRequested()
            or self.window.close_pending
            or self.owner is None
            or self.owner[0] != self.backend.binding
        ):
            worker.client.close()
            self.invalidate()
            self.result.setText("读取已取消，结果已丢弃；请重新登录后台。")
        else:
            self.backend.client = worker.client
        for control in self.controls():
            control.setEnabled(True)
        worker.client = None
        self.worker = None

    def valid_owner(self):
        self.backend.refresh()
        return (
            self.owner is not None
            and self.pending is not None
            and self.window.sync_worker is None
            and not self.window.current_archived
            and self.owner[0] == self.backend.binding
            and self.owner[1] == self.selected()
            and self.owner[2] is self.backend.client
        )

    def save(self):
        if not self.valid_owner():
            self.invalidate()
            return
        pending = self.pending
        if (
            QMessageBox.question(
                self,
                "确认本地加密保存后台账号",
                f"将 {mask_phone(pending.phone)} 保存到当前门店；新用户资格未知，同号/同Token不覆盖。此账号已存在后台，将持久阻断重复上传；不代表本地上传成功。继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        if not self.valid_owner() or self.pending is not pending:
            self.invalidate()
            return
        try:
            Accounts(self.window.db).save_backend_account(
                self.window.current_store, pending.phone, pending.token, authorized=True
            )
        except Exception:
            self.window._error(
                "保存失败或已存在同号/Token；未自动覆盖，请审阅本地账号。"
            )
            return
        self.invalidate()
        self.result.setText("已加密保存；资格未知，禁止重复上传，未调用入库接口。")
        self.window._refresh_all()

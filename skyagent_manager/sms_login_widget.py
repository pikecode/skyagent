"""Explicit, single-phone manual login using the existing backend session."""

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.db import mask_phone


class SmsWorker(QThread):
    ready = Signal(object)
    error = Signal(str)

    def __init__(self, client, phone, code, parent):
        super().__init__(parent)
        self.client, self.phone, self.code = client, phone, code
        self.cancelled = False

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            result = (
                self.client.send_account_login_code(self.phone, authorized=True)
                if self.code is None
                else self.client.account_login_by_code(
                    self.phone, self.code, authorized=True
                )
            )
            if not self.isInterruptionRequested():
                self.ready.emit(result)
        except Exception:
            if not self.isInterruptionRequested():
                self.error.emit(
                    "短信请求失败或结果未知；不展示响应、不自动重试。发码可能已发送，至少等待一分钟。"
                )
        finally:
            self.cancelled = self.isInterruptionRequested()
            self.phone = self.code = None


class SmsLoginWidget(QWidget):
    def __init__(self, backend):
        super().__init__(backend)
        self.backend, self.window = backend, backend.window
        self.worker = self.pending = self.owner = None
        layout = QVBoxLayout(self)
        note = QLabel(
            "人工短信登录：使用后台会话，会向已授权号码发短信并获取账号 Token；不是后台密码登录，不自动保存或入库。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        bar = QHBoxLayout()
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("已授权手机号（输入可见）")
        self.phone.setMaxLength(11)
        self.code = QLineEdit()
        self.code.setPlaceholderText("人工短信验证码")
        self.code.setEchoMode(QLineEdit.Password)
        self.code.setMaxLength(32)
        bar.addWidget(self.phone)
        bar.addWidget(self.code)
        layout.addLayout(bar)
        actions = QHBoxLayout()
        for label, callback in [
            ("确认并发送短信", lambda: self.start(False)),
            ("验证码登录", lambda: self.start(True)),
            ("确认加密保存账号", self.save),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.window.action_buttons.append(button)
        layout.addLayout(actions)
        self.result = QLabel("尚未短信登录；Token 不展示，保存需单独确认。")
        self.result.setWordWrap(True)
        layout.addWidget(self.result)
        self.phone.textChanged.connect(self.invalidate)

    def invalidate(self):
        self.pending = self.owner = None
        self.code.clear()

    def clear(self):
        self.invalidate()
        self.phone.clear()
        self.result.setText("短信会话已清除，未保存结果已丢弃。")

    def start(self, login):
        window, backend = self.window, self.backend
        if window.sync_worker is not None or window.current_archived:
            return
        backend.refresh()
        if backend.client is None:
            window._error("请先在上方登录后台。")
            return
        phone, code = self.phone.text(), self.code.text()
        if (
            len(phone) != 11
            or not phone.isascii()
            or not phone.isdigit()
            or not phone.startswith("1")
            or (login and not code)
        ):
            window._error("请填写有效手机号及人工验证码。")
            return
        binding, client = backend.binding, backend.client
        if (
            QMessageBox.question(
                self,
                "确认号码授权及外部操作",
                f"向后台 {binding[1]} 提交 {mask_phone(phone)} 的{'验证码登录' if login else '短信发码'}。请确认号码授权；HTTP 会明文传输。不会自动入库。继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            self.code.clear()
            return
        backend.refresh()
        if (
            window.sync_worker is not None
            or binding != backend.binding
            or backend.client is not client
            or phone != self.phone.text()
        ):
            window._error("目标或会话已变化，请重新确认。")
            return
        self.invalidate()
        self.owner = (binding, phone)
        worker = SmsWorker(client, phone, code if login else None, self)
        self.worker = window.sync_worker = worker
        backend.client = None
        window.sync_error = False
        worker.ready.connect(self.ready)
        worker.error.connect(self.result.setText)
        worker.finished.connect(self.finished)
        worker.finished.connect(window._sync_finished)
        for button in window.action_buttons:
            button.setEnabled(False)
        for control in (
            window.store_combo,
            window.show_archived,
            window.archive_store_button,
            self.phone,
            self.code,
            backend.username,
            backend.password,
            backend.search,
        ):
            control.setEnabled(False)
        window.cancel_sync.setEnabled(True)
        window.cancel_sync.setText("取消短信操作")
        self.result.setText("处理中；取消不能撤回已经发送的短信。")
        worker.start()

    def ready(self, result):
        if (
            self.worker.cancelled
            or self.worker.isInterruptionRequested()
            or self.window.close_pending
        ):
            return
        if type(result) is int:
            self.result.setText(
                f"后台确认发码；冷却至少 {max(60, result // 1000)} 秒，请人工输入验证码。"
            )
        else:
            self.pending = result
            self.result.setText(
                "短信登录成功，Token 仅内存保存。新用户提示不等于入库资格；可单独确认加密保存。"
            )

    def finished(self):
        worker = self.worker
        if (
            worker.cancelled
            or worker.isInterruptionRequested()
            or self.window.close_pending
        ):
            worker.client.close()
            self.clear()
            self.result.setText(
                "操作已取消，结果丢弃；短信可能已发送。请重新登录后台。"
            )
        else:
            self.backend.client = worker.client
        for control in (
            self.phone,
            self.code,
            self.backend.username,
            self.backend.password,
            self.backend.search,
        ):
            control.setEnabled(True)
        worker.client = None
        self.worker = None

    def save(self):
        if (
            self.window.sync_worker is not None
            or self.pending is None
            or self.owner is None
        ):
            return
        binding, phone = self.owner
        self.backend.refresh()
        if (
            binding != self.backend.binding
            or phone != self.phone.text()
            or self.window.current_archived
        ):
            self.invalidate()
            return
        pending = self.pending
        token = pending.token
        if (
            QMessageBox.question(
                self,
                "确认本地加密保存",
                f"将 {mask_phone(phone)} 的 Token 保存到当前门店。新用户标记保持未知，不自动上传；同号或同 Token 不自动覆盖。继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self.backend.refresh()
        if (
            binding != self.backend.binding
            or phone != self.phone.text()
            or self.pending is not pending
            or self.window.current_store != binding[0]
            or self.window.sync_worker is not None
            or self.window.current_archived
        ):
            self.invalidate()
            return
        try:
            Accounts(self.window.db).add(
                binding[0], AccountInput(phone, token, is_new_user=None)
            )
        except Exception:
            self.window._error(
                "账号保存失败或已有同号/Token；未自动覆盖，请审阅本地账号。"
            )
            return
        self.invalidate()
        self.result.setText("已加密保存到当前门店；新用户资格待确认，未上传。")
        self.window._refresh_all()

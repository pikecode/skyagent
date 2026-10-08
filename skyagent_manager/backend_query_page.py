"""Explicit login and paginated read-only backend account view."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from skyagent_manager.backend_account_save import BackendAccountSaveWidget
from skyagent_manager.backend_query import (
    BackendCaptchaRequired,
    BackendQueryClient,
    BackendQueryError,
    RemoteAccountDetail,
    RemoteExchangeTask,
    RemoteGiftPage,
    RemoteGiftReceiveLogs,
    RemoteOrderDetail,
    RemoteOrderPage,
)
from skyagent_manager.db import mask_phone
from skyagent_manager.sms_login_widget import SmsLoginWidget


class BackendQueryWorker(QThread):
    ready = Signal(object)
    captcha = Signal(object)
    error = Signal(str)
    completed = Signal(str)

    def __init__(
        self,
        client,
        page,
        credentials=None,
        parent=None,
        detail_id=None,
        search="",
        orders_id=None,
        orders_page=1,
        order_detail=None,
        gift_page=None,
        exchange_task_id=None,
        gift_log_id=None,
        gift_filters=None,
    ):
        super().__init__(parent)
        self.client = client
        self.page = page
        self.credentials = credentials
        self.detail_id = detail_id
        self.search = search
        self.orders_id, self.orders_page = orders_id, orders_page
        self.cancelled = False
        self.order_detail = order_detail
        self.gift_page = gift_page
        self.exchange_task_id = exchange_task_id
        self.gift_log_id = gift_log_id
        self.gift_filters = gift_filters or ("", "", "")

    def run(self):
        retained = False
        try:
            if self.isInterruptionRequested():
                return
            if self.credentials is not None:
                self.client.login(*self.credentials)
                self.credentials = None
            if self.isInterruptionRequested():
                return
            result = (
                self.client.gift_receive_logs(self.gift_log_id, authorized=True)
                if self.gift_log_id is not None
                else self.client.exchange_task(self.exchange_task_id, authorized=True)
                if self.exchange_task_id is not None
                else self.client.gift_codes(
                    self.gift_page,
                    authorized=True,
                    **{
                        key: value
                        for key, value in zip(
                            ("keyword", "status", "source"),
                            self.gift_filters,
                            strict=True,
                        )
                        if value
                    },
                )
                if self.gift_page is not None
                else self.client.account_order_detail(
                    *self.order_detail, authorized=True
                )
                if self.order_detail
                else self.client.account_orders(
                    self.orders_id, self.orders_page, authorized=True
                )
                if self.orders_id
                else self.client.account_detail(self.detail_id)
                if self.detail_id
                else self.client.accounts_page(self.page, search=self.search)
            )
            if self.isInterruptionRequested():
                return
            retained = True
            self.ready.emit((result, self.client))
            self.completed.emit("只读后台查询完成；未映射本地账号或解除入库阻断。")
        except BackendCaptchaRequired as exc:
            try:
                if not self.isInterruptionRequested():
                    image = self.client.captcha_image(exc.identifier)
                    if not self.isInterruptionRequested():
                        self.captcha.emit((exc.identifier, image))
            except BackendQueryError as error:
                self.error.emit(str(error))
            except Exception:
                self.error.emit("验证码加载异常，已停止。")
        except BackendQueryError as exc:
            self.error.emit(str(exc))
        except Exception:
            self.error.emit("后台查询异常，已停止；不展示敏感响应。")
        finally:
            self.cancelled = self.isInterruptionRequested()
            self.credentials = None
            if not retained:
                self.client.close()
            if self.isInterruptionRequested():
                self.completed.emit("后台查询已取消，结果已丢弃，会话已清除。")


class BackendQueryPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.client = None
        self.binding = None
        self.captcha_id = ""
        self.last_page = None
        self.last_gift_page = None
        self.client_factory = BackendQueryClient
        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)
        layout = QVBoxLayout(content)
        note = QLabel(
            "账号列表/详情只读：独立后台登录，不是应用启动密码。管理员可另行确认读取单账号Token并单独加密保存，禁止重复上传；不批量下载、不解除入库阻断。人工短信也需另外授权及保存确认。验证码不自动识别或重试。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QHBoxLayout()
        self.username = QLineEdit()
        self.username.setPlaceholderText("后台用户名")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("后台密码（不保存）")
        self.page_number = QSpinBox()
        self.page_number.setRange(1, 10000)
        for widget in (self.username, self.password, self.page_number):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.search = QLineEdit()
        self.search.setMaxLength(256)
        self.search.setPlaceholderText(
            "后台手机号/关键词（输入可见，查询时发送到后台）"
        )
        layout.addWidget(self.search)
        captcha_bar = QHBoxLayout()
        self.captcha_image = QSvgWidget()
        self.captcha_image.setFixedSize(120, 42)
        self.captcha_image.hide()
        self.captcha_answer = QLineEdit()
        self.captcha_answer.setPlaceholderText(
            "出现验证码后手动输入五位，再填写密码并登录"
        )
        self.captcha_answer.setMaxLength(5)
        captcha_bar.addWidget(self.captcha_image)
        captcha_bar.addWidget(self.captcha_answer)
        layout.addLayout(captcha_bar)
        buttons = QHBoxLayout()
        for label, callback in [
            ("登录并查询", lambda: self.start(True)),
            ("查询指定页", lambda: self.start(False)),
            ("上一页", lambda: self.navigate(-1)),
            ("下一页", lambda: self.navigate(1)),
            ("查询选中账号详情", self.detail),
            ("清除本地会话", self.clear),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            window.action_buttons.append(button)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["后端账号 ID", "手机号（遮蔽）"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        self.result = QLabel("尚未查询")
        layout.addWidget(self.result)
        self.detail_result = QLabel("详情仅后台已有汇总，不主动扫描或领取。")
        self.detail_result.setWordWrap(True)
        layout.addWidget(self.detail_result)
        self.assets_note = QLabel(
            "折扣券资产尚未查询；仅后台快照，不代表实时权益，不支持分享或入库。"
        )
        self.assets_note.setWordWrap(True)
        layout.addWidget(self.assets_note)
        self.assets_table = QTableWidget(0, 6)
        self.assets_table.setHorizontalHeaderLabels(
            [
                "券码（遮蔽）",
                "名称/说明",
                "金额说明",
                "有效期原文",
                "有效期提示",
                "状态原文",
            ]
        )
        self.assets_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.assets_table)
        self.sms_login = SmsLoginWidget(self)
        layout.addWidget(self.sms_login)
        self.account_save = BackendAccountSaveWidget(self)
        layout.addWidget(self.account_save)
        order_controls = QHBoxLayout()
        self.order_page = QSpinBox()
        self.order_page.setRange(1, 10000)
        order_controls.addWidget(QLabel("账号订单页码（每页10条）"))
        order_controls.addWidget(self.order_page)
        order_button = QPushButton("确认查询选中账号订单")
        order_button.clicked.connect(self.orders)
        self.window.action_buttons.append(order_button)
        order_controls.addWidget(order_button)
        layout.addLayout(order_controls)
        self.order_note = QLabel(
            "订单尚未查询；查询可能访问第三方并补写后端缓存，不操作支付或礼包。"
        )
        self.order_note.setWordWrap(True)
        layout.addWidget(self.order_note)
        self.order_table = QTableWidget(0, 5)
        self.order_table.setHorizontalHeaderLabels(
            ["订单号（遮蔽）", "酒店", "入住原文", "离店原文", "状态原文"]
        )
        self.order_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.order_table)
        detail_button = QPushButton("确认查询选中订单详情")
        detail_button.clicked.connect(self.order_detail)
        self.window.action_buttons.append(detail_button)
        layout.addWidget(detail_button)
        self.order_detail_note = QLabel(
            "礼包资格未知：还需后端已支付订单项、无既有礼包码及有效账号上下文。"
        )
        self.order_detail_note.setWordWrap(True)
        layout.addWidget(self.order_detail_note)
        gift_controls = QHBoxLayout()
        self.gift_page = QSpinBox()
        self.gift_page.setRange(1, 10000)
        gift_controls.addWidget(QLabel("全局礼包库页码（每页20条）"))
        gift_controls.addWidget(self.gift_page)
        gift_button = QPushButton("确认查询管理员全局礼包库")
        gift_button.clicked.connect(
            lambda: self.start(False, gift_page=self.gift_page.value())
        )
        window.action_buttons.append(gift_button)
        gift_controls.addWidget(gift_button)
        layout.addLayout(gift_controls)
        gift_filter_controls = QHBoxLayout()
        self.gift_keyword = QLineEdit()
        self.gift_keyword.setMaxLength(128)
        self.gift_keyword.setPlaceholderText(
            "礼包码/订单号关键词（输入可见，将发送后台）"
        )
        self.gift_status = QComboBox()
        for label, value in (
            ("全部状态", ""),
            ("ACTIVE", "ACTIVE"),
            ("EXHAUSTED", "EXHAUSTED"),
            ("INVALID", "INVALID"),
        ):
            self.gift_status.addItem(label, value)
        self.gift_source = QComboBox()
        for label, value in (("全部来源", ""), ("SCAN", "SCAN"), ("IMPORT", "IMPORT")):
            self.gift_source.addItem(label, value)
        for control in (self.gift_keyword, self.gift_status, self.gift_source):
            gift_filter_controls.addWidget(control)
        layout.addLayout(gift_filter_controls)
        gift_navigation = QHBoxLayout()
        for label, callback in (
            ("礼包上一页", lambda: self.navigate_gifts(-1)),
            ("礼包下一页", lambda: self.navigate_gifts(1)),
            ("清空礼包筛选", self.clear_gift_filters),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            window.action_buttons.append(button)
            gift_navigation.addWidget(button)
        layout.addLayout(gift_navigation)
        self.gift_note = QLabel("全局礼包库未查询；不按本地门店隔离，不领取、不导入。")
        self.gift_note.setWordWrap(True)
        layout.addWidget(self.gift_note)
        self.gift_table = QTableWidget(0, 5)
        self.gift_table.setHorizontalHeaderLabels(
            ["礼包码（遮蔽）", "来源", "后台状态", "已领取次数", "领取上限"]
        )
        self.gift_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.gift_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.gift_table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.gift_table)
        log_button = QPushButton("确认查询选中礼包的领取记录（ADMIN）")
        log_button.clicked.connect(self.gift_logs)
        window.action_buttons.append(log_button)
        layout.addWidget(log_button)
        self.gift_log_note = QLabel("礼包记录未查询；不执行领取，不代表原任务历史。")
        self.gift_log_note.setWordWrap(True)
        layout.addWidget(self.gift_log_note)
        self.gift_log_table = QTableWidget(0, 2)
        self.gift_log_table.setHorizontalHeaderLabels(
            ["领取账号手机号（遮蔽）", "后台记录时间"]
        )
        self.gift_log_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.gift_log_table)
        self.gift_table.itemSelectionChanged.connect(self.clear_gift_logs)
        self.gift_keyword.textChanged.connect(self.gift_filters_changed)
        self.gift_status.currentIndexChanged.connect(self.gift_filters_changed)
        self.gift_source.currentIndexChanged.connect(self.gift_filters_changed)
        task_controls = QHBoxLayout()
        self.exchange_task_id = QLineEdit()
        self.exchange_task_id.setMaxLength(128)
        self.exchange_task_id.setPlaceholderText("已有签到券兑换任务 ID（不创建任务）")
        task_controls.addWidget(self.exchange_task_id)
        task_button = QPushButton("确认查询已有兑换任务")
        task_button.clicked.connect(
            lambda: self.start(False, exchange_task_id=self.exchange_task_id.text())
        )
        window.action_buttons.append(task_button)
        task_controls.addWidget(task_button)
        layout.addLayout(task_controls)
        self.exchange_task_note = QLabel(
            "兑换任务未查询；非原领取任务，不更新本地历史。"
        )
        self.exchange_task_note.setWordWrap(True)
        layout.addWidget(self.exchange_task_note)
        self.exchange_task_id.textChanged.connect(
            lambda: self.exchange_task_note.setText(
                "兑换任务ID已变化，请重新确认查询。"
            )
        )
        self.username.textEdited.connect(self.clear)
        self.search.textChanged.connect(self._search_changed)

    def clear_assets(self):
        self.account_save.invalidate()
        self.clear_gift_logs()
        self.last_gift_page = None
        self.exchange_task_note.setText(
            "兑换任务未查询；非原领取任务，不更新本地历史。"
        )
        self.gift_table.setRowCount(0)
        self.gift_note.setText("全局礼包库未查询；不按本地门店隔离，不领取、不导入。")
        self.order_detail_note.setText("订单详情未查询；礼包生成资格未知，不生成礼包。")
        self.order_table.setRowCount(0)
        self.order_note.setText("订单尚未查询，不生成礼包或操作支付。")
        self.assets_table.setRowCount(0)
        self.assets_note.setText("折扣券资产尚未查询；仅后台快照，不支持分享或入库。")

    def clear_gift_logs(self):
        self.gift_log_table.setRowCount(0)
        self.gift_log_note.setText("礼包记录未查询；不执行领取，不代表原任务历史。")

    def gift_filters(self):
        return (
            self.gift_keyword.text().strip(),
            self.gift_status.currentData(),
            self.gift_source.currentData(),
        )

    def gift_filters_changed(self, *_):
        self.last_gift_page = None
        self.gift_page.setValue(1)
        self.gift_table.setRowCount(0)
        self.clear_gift_logs()
        self.gift_note.setText("礼包筛选已变化，请重新确认查询；未自动请求。")

    def clear_gift_filters(self):
        if self.window.sync_worker is not None:
            return
        self.gift_keyword.clear()
        self.gift_status.setCurrentIndex(0)
        self.gift_source.setCurrentIndex(0)

    def navigate_gifts(self, direction):
        if self.window.sync_worker is not None:
            return
        current = self.last_gift_page
        if current is None:
            self.window._error("请先确认查询当前礼包筛选条件。")
            return
        target = current.page + direction
        if (
            direction not in {-1, 1}
            or not 1 <= target <= 10000
            or (direction > 0 and not current.has_more)
        ):
            self.window._error("没有更多礼包页面可查询。")
            return
        self.gift_page.setValue(target)
        self.start(False, gift_page=target)

    def selected_gift_id(self):
        item = self.gift_table.item(self.gift_table.currentRow(), 0)
        return (
            item.data(Qt.UserRole) if item is not None and item.isSelected() else None
        )

    def gift_logs(self):
        if self.window.sync_worker is not None:
            return
        identifier = self.selected_gift_id()
        if not identifier:
            self.window._error("请先查询全局礼包库并选中一条礼包记录。")
            return
        self.start(False, gift_log_id=identifier)

    def _search_changed(self):
        self.last_page = None
        self.page_number.setValue(1)
        self.table.setRowCount(0)
        self.clear_assets()
        self.detail_result.setText("搜索条件已变化，请重新查询。")
        self.result.setText("搜索条件已变化，请重新查询。")

    def navigate(self, direction):
        if self.window.sync_worker is not None:
            return
        if self.last_page is None:
            self.window._error("请先查询当前搜索条件。")
            return
        target = self.last_page.page + direction
        if not 1 <= target <= 10000 or (direction > 0 and not self.last_page.has_more):
            self.window._error("没有更多可查询页面。")
            return
        self.page_number.setValue(target)
        self.start(False)

    def clear(self):
        if self.window.sync_worker is not None:
            return
        if self.client is not None:
            self.client.close()
        self.sms_login.clear()
        self.clear_gift_filters()
        self.exchange_task_id.clear()
        self.client = None
        self.last_page = None
        self.search.clear()
        self.password.clear()
        self.captcha_id = ""
        self.captcha_answer.clear()
        self.captcha_image.hide()
        self.table.setRowCount(0)
        self.clear_assets()
        self.detail_result.setText("详情仅后台已有汇总，不主动扫描或领取。")
        self.result.setText("尚未查询（会话仅内存保存）")

    def refresh(self):
        sid = self.window.current_store
        config = self.window.db.get_store_sync(sid) if sid else {}
        binding = (
            sid,
            config.get("api_url"),
            config.get("allow_http"),
            self.window.current_archived,
        )
        if binding != self.binding:
            self.clear()
            self.username.clear()
            self.page_number.setValue(1)
            self.binding = binding

    def detail(self):
        selected = self.table.item(self.table.currentRow(), 0)
        if selected is None:
            self.window._error("请先查询并选中后台账号。")
            return
        self.start(False, detail_id=selected.text())

    def orders(self):
        selected = self.table.item(self.table.currentRow(), 0)
        if selected is None:
            self.window._error("请先查询并选中后台账号。")
            return
        self.start(False, orders_id=selected.text())

    def order_detail(self):
        selected = self.order_table.item(self.order_table.currentRow(), 0)
        if selected is None:
            self.window._error("请先查询并选中订单。")
            return
        self.start(False, order_detail=selected.data(Qt.UserRole))

    def start(
        self,
        login,
        detail_id=None,
        orders_id=None,
        order_detail=None,
        gift_page=None,
        exchange_task_id=None,
        gift_log_id=None,
    ):
        window = self.window
        if (
            window.sync_worker is not None
            or not window.current_store
            or window.current_archived
        ):
            return
        self.refresh()
        search = self.search.text()
        config = window.db.get_store_sync(window.current_store)
        gift_filters = self.gift_filters()
        if gift_log_id is not None and self.selected_gift_id() != gift_log_id:
            window._error("礼包选择已变化，请重新选择。")
            return
        if exchange_task_id is not None:
            import re

            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", exchange_task_id):
                window._error("请填写有效的已有兑换任务 ID。")
                return
            binding, client = self.binding, self.client
            if client is None:
                window._error("请先登录后台。")
                return
            if (
                QMessageBox.question(
                    self,
                    "确认已有签到券兑换任务查询",
                    "仅查询已有兑换任务状态，需要创建者或ADMIN权限，不按本地门店隔离。不创建、不轮询，不更新本地任务历史；completed不代表原领取任务成功。继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            self.refresh()
            if (
                self.binding != binding
                or self.client is not client
                or window.sync_worker is not None
                or self.exchange_task_id.text() != exchange_task_id
            ):
                window._error("后台目标或任务 ID 已变化，请重新确认。")
                return
        if (
            orders_id
            or order_detail
            or gift_page is not None
            or gift_log_id is not None
        ):
            binding, client = self.binding, self.client
            if client is None:
                window._error("请先登录后台。")
                return
            if (
                QMessageBox.question(
                    self,
                    "确认管理员全局礼包库"
                    if gift_page is not None or gift_log_id is not None
                    else "确认账号订单查询",
                    "此查询需要ADMIN权限，读取选中礼包的全局领取记录，不按本地门店隔离。响应可能包含完整手机号/礼包码，桌面仅显示遮蔽手机号和时间，HTTP有明文风险；不执行领取、不落盘、不当作原任务成功证据。继续？"
                    if gift_log_id is not None
                    else "此查询需要后台 ADMIN 权限，返回全局礼包库，不按本地门店隔离。关键词（礼包码/订单号）和筛选将发送后台，HTTP会明文传输；仅查询指定页并遮蔽礼包码，不领取、不生成、不导入。继续？"
                    if gift_page is not None
                    else "此查询会由后台访问第三方，可能补写账号缓存。只查询指定页，不生成礼包、不支付、不导入订单。继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            self.refresh()
            if (
                self.binding != binding
                or self.client is not client
                or window.sync_worker is not None
                or (gift_log_id is not None and self.selected_gift_id() != gift_log_id)
                or (gift_page is not None and self.gift_filters() != gift_filters)
            ):
                window._error("后台目标已变化，请重新确认。")
                return
        credentials = None
        if login:
            binding = self.binding
            if not self.username.text().strip() or not self.password.text():
                window._error("请填写后台用户名和密码。")
                return
            if (
                QMessageBox.question(
                    self,
                    "确认后台登录",
                    f"将向配置的后台 {config['api_url']} 发送用户名和密码，并只读查询账号。HTTP 会明文传输凭据，后台权限不等于本地门店隔离。继续吗？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                self.password.clear()
                return
            self.refresh()
            if window.sync_worker is not None or self.binding != binding:
                self.password.clear()
                window._error("门店或后台配置已变化，请重新确认登录。")
                return
            if self.captcha_id and len(self.captcha_answer.text().strip()) != 5:
                window._error("请手动输入五位验证码。")
                return
            credentials = (
                self.username.text(),
                self.password.text(),
                self.captcha_id,
                self.captcha_answer.text(),
            )
            self.clear()
            self.search.setText(search)
            try:
                client = self.client_factory(
                    config["api_url"], allow_http=config["allow_http"]
                )
            except Exception:
                window._error("后台地址不可用，请检查门店配置。")
                return
        else:
            if self.client is None:
                window._error("请先登录后台。")
                return
            client = self.client
        self.client = None
        self.password.clear()
        if not orders_id and not order_detail and gift_log_id is None:
            self.table.setRowCount(0)
        if gift_log_id is not None:
            self.clear_gift_logs()
            self.gift_log_note.setText("正在查询选中礼包的后台记录；不执行领取。")
        elif not order_detail:
            self.clear_assets()
        else:
            self.order_detail_note.setText("正在查询订单详情；不操作支付或礼包。")
        self.last_page = None
        self.detail_result.setText("尚未加载详情")
        self.result.setText("查询中")
        worker = BackendQueryWorker(
            client,
            self.page_number.value(),
            credentials,
            self,
            detail_id=detail_id,
            search=search,
            orders_id=orders_id,
            orders_page=self.order_page.value(),
            order_detail=order_detail,
            gift_page=gift_page,
            exchange_task_id=exchange_task_id,
            gift_log_id=gift_log_id,
            gift_filters=gift_filters,
        )
        worker.binding = self.binding
        window.sync_error = False
        window.sync_worker = worker
        worker.ready.connect(self._ready)
        worker.captcha.connect(self._captcha)
        worker.error.connect(self._error)
        worker.completed.connect(window.status.setText)
        worker.finished.connect(self._finished)
        worker.finished.connect(window._sync_finished)
        for button in window.action_buttons:
            button.setEnabled(False)
        window.store_combo.setEnabled(False)
        window.show_archived.setEnabled(False)
        window.archive_store_button.setEnabled(False)
        window.cancel_sync.setEnabled(True)
        for widget in (
            self.username,
            self.password,
            self.page_number,
            self.captcha_answer,
            self.search,
            self.exchange_task_id,
            self.gift_table,
            self.gift_keyword,
            self.gift_status,
            self.gift_source,
            self.gift_page,
        ):
            widget.setEnabled(False)
        worker.start()

    def _ready(self, payload):
        result, client = payload
        if (
            self.window.sync_worker.cancelled
            or self.window.sync_worker.isInterruptionRequested()
        ):
            client.close()
            return
        self.client = client
        if isinstance(result, RemoteGiftReceiveLogs):
            self.refresh()
            if (
                result.identifier != self.window.sync_worker.gift_log_id
                or self.selected_gift_id() != result.identifier
                or self.binding != self.window.sync_worker.binding
                or self.window.current_archived
            ):
                self.clear_gift_logs()
                client.close()
                self.client = None
                self.result.setText("礼包记录身份或选择已变化，结果丢弃。")
                return
            self.gift_log_table.setRowCount(len(result.items))
            for index, row in enumerate(result.items):
                self.gift_log_table.setItem(
                    index, 0, QTableWidgetItem(row.masked_phone)
                )
                self.gift_log_table.setItem(index, 1, QTableWidgetItem(row.created_at))
            self.gift_log_note.setText(
                f"后台领取记录 {len(result.items)} 条；仅遮蔽手机号和时间，无原始内容；"
                "这不是原任务历史或逐项领取成功证据，未写本地库存。"
            )
            self.result.setText("礼包后台记录查询完成，不执行领取、不落盘。")
            return
        if isinstance(result, RemoteExchangeTask):
            self.exchange_task_note.setText(
                f"签到券兑换任务：后台状态 {result.state}，进度 {result.progress}%；"
                "仅后台状态快照，completed不证明逐账号兑换成功或原领取任务完成；未更新本地历史。"
            )
            self.result.setText("已有兑换任务状态已查询，不创建、不自动轮询、不落盘。")
            return
        if isinstance(result, RemoteGiftPage):
            self.refresh()
            if (
                self.binding != self.window.sync_worker.binding
                or self.gift_filters() != self.window.sync_worker.gift_filters
            ):
                self.gift_filters_changed()
                client.close()
                self.client = None
                self.result.setText("礼包筛选或后台目标变化，结果已丢弃。")
                return
            self.last_gift_page = result
            self.gift_table.setRowCount(len(result.items))
            for index, row in enumerate(result.items):
                for column, value in enumerate(
                    (
                        row.masked_code,
                        row.source,
                        row.status,
                        str(row.received),
                        str(row.maximum),
                    )
                ):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(Qt.UserRole, row.identifier)
                    self.gift_table.setItem(index, column, item)
            self.gift_note.setText(
                f"全局礼包库第 {result.page} 页，本页 {len(result.items)} 条，总数 {result.total}；"
                + ("有后续页。" if result.has_more else "无后续页。")
                + "后台 ACTIVE 不保证实时可领取；未写入本地库存。"
            )
            self.result.setText("全局礼包库查询完成；不领取、不生成、不落盘。")
            return
        if isinstance(result, RemoteOrderDetail):
            self.order_detail_note.setText(
                f"订单状态原文：{result.state_text or '未知'}；支付状态码：{result.payment_state if result.payment_state is not None else '未知'}；"
                "礼包资格仍未知：第三方状态不等于后台 paymentStatus=PAID，还需无既有礼包码、有效订单项和账号上下文。未生成礼包。"
            )
            self.result.setText("订单详情已查询，不落盘、不支付。")
            return
        if isinstance(result, RemoteOrderPage):
            from skyagent_manager.db import mask_code

            self.order_table.setRowCount(len(result.items))
            for index, row in enumerate(result.items):
                for column, value in enumerate(
                    (mask_code(row.folio_id), row.hotel, row.start, row.end, row.state)
                ):
                    item = QTableWidgetItem(value or "未知")
                    if column == 0:
                        item.setData(
                            Qt.UserRole, (result.account_id, row.folio_id, row.chain_id)
                        )
                    self.order_table.setItem(index, column, item)
            self.order_note.setText(
                f"订单第 {result.page} 页，本页 {len(result.items)} 条，总数 {result.total}；"
                + ("有后续页，可手动指定页查询。" if result.has_more else "无后续页。")
                + "未导入、未生成礼包或操作支付。"
            )
            self.result.setText("账号订单查询完成；只展示，不落盘。")
            return
        if isinstance(result, RemoteAccountDetail):
            self.assets_table.setRowCount(len(result.discount_assets or ()))
            self.assets_note.setText(
                "后端未提供折扣券资产字段，不能视为无权益。"
                if result.discount_assets is None
                else f"后台折扣券快照 {len(result.discount_assets)} 条；可能过期或未刷新，不支持分享或入库。"
            )
            for index, asset in enumerate(result.discount_assets or ()):
                for column, value in enumerate(
                    (
                        asset.masked_code,
                        asset.description,
                        asset.value,
                        asset.expiry,
                        asset.expiry_tip,
                        asset.state,
                    )
                ):
                    self.assets_table.setItem(
                        index, column, QTableWidgetItem(value or "未知")
                    )
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem(result.identifier))
            self.table.setItem(0, 1, QTableWidgetItem(mask_phone(result.phone)))
            self.detail_result.setText(
                f"启用：{'是' if result.enabled else '否'}；在线：{'是' if result.online else '否'}；新用户：{'是' if result.new_user else '否'}；"
                f"早餐：{result.breakfast}；升房：{result.room_upgrade}；延迟退房：{result.late_checkout}。仅后台存量汇总，可能不是实时权益。"
            )
            self.result.setText("只读详情完成；未映射本地账号、未解除入库阻断。")
            return
        self.last_page = result
        self.table.setRowCount(len(result.items))
        for index, item in enumerate(result.items):
            self.table.setItem(index, 0, QTableWidgetItem(item.identifier))
            self.table.setItem(index, 1, QTableWidgetItem(mask_phone(item.phone)))
        self.result.setText(
            f"第 {result.page} 页，本页 {len(result.items)} 条，总数 {result.total}；"
            + ("还有后续页" if result.has_more else "无后续页")
        )
        self.table.resizeColumnsToContents()

    def _captcha(self, payload):
        if self.window.sync_worker.isInterruptionRequested():
            return
        self.captcha_id, image = payload
        self.captcha_image.load(image)
        self.captcha_image.show()
        self.captcha_answer.clear()
        self.result.setText("请输入图片验证码并重新填写密码，再手动登录。")

    def _error(self, message):
        self.result.setText("查询失败；会话已清除")
        self.window._error(message)

    def _finished(self):
        if (
            self.window.sync_worker.cancelled
            or self.window.sync_worker.isInterruptionRequested()
        ):
            if self.client is not None:
                self.client.close()
            self.client = None
            self.table.setRowCount(0)
            self.clear_assets()
            self.captcha_id = ""
            self.captcha_image.hide()
            self.detail_result.setText("查询已取消，详情结果已丢弃。")
            self.result.setText("查询已取消，会话已清除")
        for widget in (
            self.username,
            self.password,
            self.page_number,
            self.captcha_answer,
            self.search,
            self.exchange_task_id,
            self.gift_table,
            self.gift_keyword,
            self.gift_status,
            self.gift_source,
            self.gift_page,
        ):
            widget.setEnabled(True)
        if self.client is None and self.result.text() == "查询中":
            self.result.setText("查询已取消，会话已清除")

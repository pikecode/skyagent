"""Explicit simulation/direct benefits and offline saved outputs; no URL opening."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from skyagent_manager.account_benefits import (
    KINDS,
    STATUSES,
    AccountBenefitSession,
    SimulatedBenefitAdapter,
)
from skyagent_manager.accounts import Accounts
from skyagent_manager.coupon_sharing import (
    COUPON_KINDS,
    CouponShareOutputs,
    DirectCouponShareAdapter,
    DirectPropShareAdapter,
)
from skyagent_manager.db import mask_code, mask_phone
from skyagent_manager.direct_benefits import DirectBenefitQueryAdapter
from skyagent_manager.direct_gifts import (
    DirectGiftOrderQueryAdapter,
    DirectGiftShareAdapter,
)
from skyagent_manager.gift_sharing import GiftShareOutputs
from skyagent_manager.saved_share_dialog import SavedCouponSharesDialog
from skyagent_manager.share_journal import ShareJournal
from skyagent_manager.share_safety import require_share_writes_allowed


class BenefitQueryWorker(QThread):
    ready = Signal(object)
    error = Signal(str)

    def __init__(self, adapter, token, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.token = token

    def run(self):
        try:
            items = AccountBenefitSession.fetch_items(
                self.adapter, self.token, cancelled=self.isInterruptionRequested
            )
            if not self.isInterruptionRequested():
                self.ready.emit(items)
        except Exception:
            if not self.isInterruptionRequested():
                self.error.emit("权益查询失败，未保留部分结果；敏感响应不展示。")
        finally:
            self.token = None


class BenefitShareWorker(QThread):
    completed = Signal(object)
    operation_scope = "direct-coupon-share"

    def __init__(self, adapter, token, item, parent=None):
        super().__init__(parent)
        self.adapter, self.token, self.item = adapter, token, item

    def run(self):
        result = None
        try:
            result = self.adapter.share(
                self.token, self.item, cancelled=self.isInterruptionRequested
            )
            if self.isInterruptionRequested():
                result = None
        except Exception:
            pass  # Never propagate request URLs, tokens or response bodies.
        finally:
            self.token = self.item = None
            self.completed.emit(result)


class AccountBenefitPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.session = AccountBenefitSession(window.db, SimulatedBenefitAdapter())
        self.binding = None
        self.query_worker = None
        self.query_owner = None
        self.share_worker = None
        self.share_attempt = None
        self.share_adapter_factory = DirectCouponShareAdapter
        self.prop_share_adapter_factory = DirectPropShareAdapter
        self.gift_query_adapter_factory = DirectGiftOrderQueryAdapter
        self.gift_share_adapter_factory = DirectGiftShareAdapter
        self.saved_dialog = None
        layout = QVBoxLayout(self)
        self.note = QLabel(
            "模拟模式：以下权益全部为虚构数据，不代表账号真实权益。分享只生成 example.invalid 虚构地址，不打开网页、不写入库存、不上传。"
        )
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.mode = QComboBox()
        self.mode.addItem("离线模拟（默认，不请求网络）", "simulation")
        self.mode.addItem("授权直连（权益/订单查询及单项分享，未实测生产）", "direct")
        layout.addWidget(self.mode)
        controls = QHBoxLayout()
        local_controls = QHBoxLayout()
        self.account = QComboBox()
        controls.addWidget(self.account, 1)
        self.kind = QComboBox()
        self.kind.addItem("全部类型", "")
        for kind, label in KINDS.items():
            self.kind.addItem(label, kind)
        controls.addWidget(self.kind)
        self.shareable = QCheckBox("仅可分享")
        controls.addWidget(self.shareable)
        for label, callback in [
            ("查询模拟权益", self.query),
            ("生成模拟分享", self.share),
            ("确认已保存分享入资源库存", self.import_saved_share),
            ("导出筛选报告（脱敏）", self.export_report),
            ("查看本地已保存分享（不联网）", self.open_saved_shares),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            if callback in (self.import_saved_share, self.open_saved_shares):
                local_controls.addWidget(button)
            else:
                controls.addWidget(button)
            window.action_buttons.append(button)
            if label == "查询模拟权益":
                self.query_button = button
            elif label == "生成模拟分享":
                self.share_button = button
        layout.addLayout(controls)
        local_controls.addStretch(1)
        layout.addLayout(local_controls)
        gift_controls = QHBoxLayout()
        for label, callback in [
            ("确认直连查询订单（不生成礼包）", self.query_orders),
            ("查看本地礼包成功记录（不联网）", self.open_saved_gifts),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            gift_controls.addWidget(button)
            window.action_buttons.append(button)
        gift_controls.addStretch(1)
        layout.addLayout(gift_controls)
        filters = QHBoxLayout()
        self.keyword = QLineEdit()
        self.keyword.setMaxLength(256)
        self.keyword.setPlaceholderText("按名称、类型或来源筛选（不搜索完整券码）")
        filters.addWidget(self.keyword, 1)
        self.state = QComboBox()
        self.state.addItem("全部状态", "")
        for state, label in STATUSES.items():
            self.state.addItem(label, state)
        filters.addWidget(self.state)
        clear_filters = QPushButton("清空筛选")
        clear_filters.clicked.connect(self.clear_filters)
        filters.addWidget(clear_filters)
        layout.addLayout(filters)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "类型",
                "名称（模拟）",
                "券码（遮蔽）",
                "数量",
                "有效期",
                "可分享",
                "来源",
                "状态",
            ]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.table)
        self.result = QLabel("尚未查询")
        layout.addWidget(self.result)
        self.account.currentIndexChanged.connect(self._changed)
        self.kind.currentIndexChanged.connect(self.populate)
        self.shareable.toggled.connect(self.populate)
        self.keyword.textChanged.connect(self.populate)
        self.state.currentIndexChanged.connect(self.populate)
        self.populate()
        self.mode.currentIndexChanged.connect(self.mode_changed)

    def mode_changed(self):
        self._changed()
        direct = self.mode.currentData() == "direct"
        self.session.adapter = (
            DirectBenefitQueryAdapter() if direct else SimulatedBenefitAdapter()
        )
        self.query_button.setText("确认直连查询权益" if direct else "查询模拟权益")
        self.share_button.setText(
            "确认分享 / 生成选中礼包" if direct else "生成模拟分享"
        )
        self.note.setText(
            "授权直连：历史协议未生产实测。券/法宝分享或选中订单生成礼包均须独立确认，加密保存，不自动上传。订单查询不生成礼包，订单存在不证明资格；礼包有效期未知，只保存/导出，不入可用库存。超时/取消/重启不重发，恢复后阻断。"
            if direct
            else "模拟模式：以下权益全部为虚构数据，不代表账号真实权益。分享只生成 example.invalid 虚构地址，不打开网页、不写入库存、不上传。"
        )
        self.table.setHorizontalHeaderLabels(
            [
                "类型",
                "名称（直连快照）" if direct else "名称（模拟）",
                "券码（遮蔽）",
                "数量",
                "有效期",
                "可分享",
                "来源",
                "状态",
            ]
        )

    def clear_filters(self):
        for widget in (self.kind, self.state, self.keyword, self.shareable):
            widget.blockSignals(True)
        self.kind.setCurrentIndex(0)
        self.state.setCurrentIndex(0)
        self.keyword.clear()
        self.shareable.setChecked(False)
        for widget in (self.kind, self.state, self.keyword, self.shareable):
            widget.blockSignals(False)
        self.populate()

    def _changed(self, *_args):
        if self.saved_dialog is not None:
            self.saved_dialog.invalidate()
        if self.query_worker is not None:
            self.query_worker.requestInterruption()
        if self.share_worker is not None:
            self.share_worker.requestInterruption()
        self.query_owner = None
        self.session.clear()
        self.binding = None
        self.result.setText("尚未查询")
        self.populate()

    def refresh(self):
        sid = self.window.current_store
        old = self.account.currentData()
        self.account.blockSignals(True)
        self.account.clear()
        rows = Accounts(self.window.db).list(sid)
        for row in rows:
            self.account.addItem(
                mask_phone(row["phone"]) + " / " + row["id"][:8], row["id"]
            )
        index = self.account.findData(old)
        if index >= 0:
            self.account.setCurrentIndex(index)
        self.account.blockSignals(False)
        aid = self.account.currentData()
        row = next((row for row in rows if row["id"] == aid), None)
        identity = None
        if row and not self.window.current_archived:
            _, identity = self.session._identity(sid, aid)
        # Offline history can be open without a query binding. In that case
        # both old binding and the new empty-store identity are None, so the
        # query-change check alone does not invalidate the previous owner's UI.
        if self.saved_dialog is not None and identity != self.saved_dialog.owner:
            self.saved_dialog.invalidate()
        if identity != self.binding:
            self._changed()

    def allowed(self):
        return bool(
            self.window.current_store
            and self.account.currentData()
            and not self.window.current_archived
            and self.window.sync_worker is None
        )

    def query_orders(self):
        if self.mode.currentData() != "direct":
            self.window._error("请先选择已授权直连模式；订单查询不会生成礼包。")
            return
        self.query(query_adapter=self.gift_query_adapter_factory())

    def query(self, *, query_adapter=None):
        if not self.allowed():
            return
        try:
            token, owner = self.session._identity(
                self.window.current_store, self.account.currentData()
            )
        except Exception as exc:
            self.window._error(str(exc))
            return
        if self.mode.currentData() == "direct":
            adapter = self.session.adapter
            if (
                QMessageBox.question(
                    self,
                    "确认已授权账号直连查询",
                    "将向 https://api2.yaduo.com 发送当前账号完整Token，只读最近第一页最多20个订单，不生成礼包、不支付、不领取。结果不证明生成资格或有效期；替换当前查询快照。历史协议未生产实测，不自动翻页/重试。继续？"
                    if query_adapter is not None
                    else "将向 https://miniapp.yaduo.com 发送当前账号完整Token，按恢复协议查询早餐/升房/延迟券及道具。仅限已授权账号；不领取、不分享、不自动保存。未实测当前生产兼容性，遇验证码/风控停止、不重试。继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            try:
                if (
                    not self.allowed()
                    or self.session.adapter is not adapter
                    or self.session._identity(
                        self.window.current_store, self.account.currentData()
                    )[1]
                    != owner
                ):
                    raise ValueError()
            except Exception:
                self.window._error("账号、模式或Token已变化，请重新确认查询。")
                return
        self.session.clear()
        self.binding = None
        self.query_owner = owner
        worker = BenefitQueryWorker(query_adapter or self.session.adapter, token, self)
        self.query_worker = worker
        self.window.sync_worker = worker
        self.window.sync_error = False
        worker.ready.connect(self._ready)
        worker.error.connect(self._query_error)
        worker.finished.connect(self._finished)
        worker.finished.connect(self.window._sync_finished)
        for button in self.window.action_buttons:
            button.setEnabled(False)
        self.window.store_combo.setEnabled(False)
        self.window.show_archived.setEnabled(False)
        self.window.archive_store_button.setEnabled(False)
        self.window.cancel_sync.setEnabled(True)
        self.window.cancel_sync.setText("取消权益查询")
        self.account.setEnabled(False)
        self.mode.setEnabled(False)
        self.result.setText(
            "正在授权直连查询，可取消"
            if self.mode.currentData() == "direct"
            else "正在查询模拟权益，可取消"
        )
        self.populate()
        worker.start()

    def _ready(self, items):
        if self.query_worker.isInterruptionRequested() or self.query_owner is None:
            return
        try:
            self.session.publish(
                self.window.current_store,
                self.account.currentData(),
                self.query_owner,
                items,
            )
            self.binding = self.session.owner
            self.result.setText(
                f"已查询 {len(items)} 条{'第三方权益快照' if self.mode.currentData() == 'direct' else '虚构权益'}；仅当前会话有效"
            )
            self.populate()
        except Exception:
            self._query_error("账号、门店或 Token 已变化，查询结果已丢弃。")

    def _query_error(self, message):
        self.session.clear()
        self.binding = None
        self.result.setText(message)
        self.populate()

    def _finished(self):
        if self.query_worker.isInterruptionRequested():
            self.session.clear()
            self.binding = None
            self.result.setText("权益查询已取消，结果已丢弃")
            self.populate()
        self.query_worker = None
        self.query_owner = None
        self.account.setEnabled(True)
        self.mode.setEnabled(True)

    def filtered_items(self):
        sources = {"coupon": "券", "prop": "道具", "order": "订单"}
        keyword = self.keyword.text().strip().casefold()
        return [
            item
            for item in self.session.items
            if (not self.kind.currentData() or item.kind == self.kind.currentData())
            and (
                not self.shareable.isChecked()
                or item.can_share()
                or item.can_generate_gift()
            )
            and (
                not self.state.currentData()
                or item.effective_status() == self.state.currentData()
            )
            and (
                not keyword
                or keyword
                in " ".join(
                    (item.name, KINDS[item.kind], sources[item.source], item.source)
                ).casefold()
            )
        ]

    def populate(self, *_args):
        sources = {"coupon": "券", "prop": "道具", "order": "订单"}
        rows = self.filtered_items()
        self.summary.setText(
            f"显示 {len(rows)} / {len(self.session.items)} 条（{'直连查询快照' if getattr(self.session.adapter, 'direct', False) else '模拟结果'}）"
        )
        self.table.setRowCount(len(rows))
        self.table.setCurrentItem(None)
        for index, row in enumerate(rows):
            for column, value in enumerate(
                [
                    KINDS[row.kind],
                    row.name,
                    mask_code(row.code),
                    str(row.count),
                    row.expiry,
                    self._share_label(row),
                    sources[row.source],
                    STATUSES[row.effective_status()],
                ]
            ):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, (row.source, row.identifier))
                self.table.setItem(index, column, item)
        self.table.resizeColumnsToContents()

    def export_report(self):
        if not self.allowed():
            return
        try:
            _, owner = self.session._identity(
                self.window.current_store, self.account.currentData()
            )
            if owner != self.session.owner:
                self._changed()
                raise ValueError("账号或 Token 已变化，请重新查询后导出。")
            rows = self.filtered_items()
            if not rows:
                raise ValueError("当前筛选没有可导出的权益。")
        except Exception as exc:
            self.window._error(str(exc))
            return
        # Export only displayed, masked data. Never include resource IDs or shares.
        self.window._export_csv(*self.report_data(rows))

    def report_data(self, rows):
        """Masked report payload shared by the preview and offline checks."""
        return (
            "导出直连权益快照（无分享链接）"
            if getattr(self.session.adapter, "direct", False)
            else "导出模拟权益筛选报告（无分享链接）",
            [
                "模式",
                "类型",
                "名称",
                "券码（遮蔽）",
                "数量",
                "有效期",
                "可分享",
                "来源",
                "状态",
            ],
            [
                [
                    "直连查询快照，非领取/分享凭证"
                    if getattr(self.session.adapter, "direct", False)
                    else "离线模拟，非真实权益",
                    KINDS[row.kind],
                    row.name,
                    mask_code(row.code),
                    str(row.count),
                    row.expiry,
                    "是" if row.can_share() else "否",
                    {"coupon": "券", "prop": "道具", "order": "订单"}[row.source],
                    STATUSES[row.effective_status()],
                ]
                for row in rows
            ],
        )

    def share(self):
        if not self.allowed():
            return
        if getattr(self.session.adapter, "direct", False):
            self._direct_share()
            return
        selected = self.table.item(self.table.currentRow(), 0)
        if selected is None:
            self.window._error("请先选择一个模拟权益。")
            return
        if (
            QMessageBox.question(
                self,
                "生成模拟分享",
                "仅生成不可用于真实领取的虚构地址，不向第三方提交。继续？",
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.session.share(
                self.window.current_store,
                self.account.currentData(),
                tuple(selected.data(Qt.UserRole)),
            )
            self.result.setText("已生成模拟分享；虚构地址不展示、不打开、不写入库存")
            self.window._refresh_activity()
        except Exception as exc:
            self.window._error(str(exc))

    def _share_label(self, item):
        if getattr(self.session.adapter, "direct", False):
            try:
                journal = ShareJournal(self.window.db)
                record = (
                    journal.state_for_owner(
                        self.window.current_store, self.account.currentData(), item
                    )
                    if item.source == "prop" and item.prop_context_valid()
                    else journal.state(item)
                )
                if record:
                    return (
                        "已保存，禁重发"
                        if record["state"] == "confirmed"
                        else "待核实，禁重发"
                    )
            except Exception:
                return "记录异常，禁重发"
        if getattr(self.session.adapter, "direct", False) and item.can_generate_gift():
            return "可尝试生成（资格未验证）"
        return "是" if item.can_share() else "否"

    def _selected_direct_item(self):
        selected = self.table.item(self.table.currentRow(), 0)
        if selected is None:
            raise ValueError("请先查询并选择一项权益或订单。")
        key = tuple(selected.data(Qt.UserRole))
        item = next(
            (
                item
                for item in self.session.items
                if (item.source, item.identifier) == key
            ),
            None,
        )
        token, owner = self.session._identity(
            self.window.current_store, self.account.currentData()
        )
        if owner != self.session.owner or item is None:
            raise ValueError("查询归属已变化，请重新查询。")
        if not (
            (item.source == "coupon" and item.kind in COUPON_KINDS)
            or (
                item.source == "prop"
                and item.kind == "prop"
                and item.prop_context_valid()
            )
            or (
                item.source == "order"
                and item.kind == "gift"
                and item.gift_context_valid()
            )
        ):
            raise ValueError("仅支持三类券及上下文明确的法宝/订单；未知上下文未开放。")
        return token, owner, item

    def _direct_share(self):
        try:
            token, owner, item = self._selected_direct_item()
            require_share_writes_allowed(self.window.db)
            if not (item.can_share() or item.can_generate_gift()):
                raise ValueError("该券当前不可分享、已过期或有效期未知。")
            if (
                ShareJournal(self.window.db).state_for_owner(owner[0], owner[1], item)
                is not None
            ):
                raise ValueError(
                    "该权益已有提交记录，禁止重发；可查看本地已保存成功结果。"
                )
            adapter = self.session.adapter
            if (
                QMessageBox.question(
                    self,
                    "确认已授权单项权益分享",
                    (
                        "将向 https://api2.yaduo.com 提交当前账号完整Token及选中订单/门店ID，单次生成礼包码。订单存在不证明资格，由第三方决定；不支付、不领取。有效期未知，只保存/导出，不加入可用库存。\n"
                        if item.kind == "gift"
                        else "将向 https://miniapp.yaduo.com 提交当前账号完整Token及权益分享参数，生成一个链接。法宝按类型上下文提交，不按单张码消耗，不能证明只消耗一份。\n"
                    )
                    + "这是第三方写操作，仅限已获授权的账号与权益；历史协议未生产实测。提交前加密保存防重记录；超时、取消或重启后禁止重发，需人工核实。\n"
                    "成功链接加密保存，不自动打开、不领取、不入库存、不上传后台。确认提交？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            if (
                not self.allowed()
                or self.session.adapter is not adapter
                or self._selected_direct_item()[1:] != (owner, item)
            ):
                raise ValueError("确认期间账号、模式或选中券已变化，请重新确认。")
            factory = (
                self.gift_share_adapter_factory
                if item.kind == "gift"
                else self.prop_share_adapter_factory
                if item.source == "prop"
                else self.share_adapter_factory
            )
            worker = BenefitShareWorker(factory(), token, item, self)
            operation = ShareJournal(self.window.db).reserve(
                owner[0], owner[1], owner, item
            )
        except Exception as exc:
            self.window._error(str(exc))
            return
        self.share_worker = worker
        self.share_attempt = (owner, item, operation, adapter)
        self.window.sync_worker = worker
        self.window.sync_error = False
        worker.completed.connect(self._share_completed)
        worker.finished.connect(self._share_finished)
        worker.finished.connect(self.window._sync_finished)
        for button in self.window.action_buttons:
            button.setEnabled(False)
        self.window.store_combo.setEnabled(False)
        self.window.show_archived.setEnabled(False)
        self.window.archive_store_button.setEnabled(False)
        self.window.cancel_sync.setEnabled(True)
        self.window.cancel_sync.setText("取消分享等待（不能撤回已发请求）")
        self.account.setEnabled(False)
        self.mode.setEnabled(False)
        self.result.setText("分享已预留，正在单次提交；取消不能撤回已发请求")
        try:
            worker.start()
        except Exception:
            self._share_completed(None)
            self._share_finished()
            self.window._sync_finished()

    def _share_completed(self, url):
        if self.share_attempt is None:
            return
        owner, item, operation, adapter = self.share_attempt
        try:
            valid = (
                bool(url)
                and not self.share_worker.isInterruptionRequested()
                and not self.window.close_pending
            )
            if valid:
                valid = (
                    self.session.adapter is adapter
                    and self.session.owner == owner
                    and self.session._identity(
                        self.window.current_store, self.account.currentData()
                    )[1]
                    == owner
                )
            if valid:
                outputs = (
                    GiftShareOutputs(self.window.db)
                    if item.kind == "gift"
                    else CouponShareOutputs(self.window.db)
                )
                outputs.save(owner[0], owner[1], owner, item, operation, url)
                self.result.setText(
                    "已确认并加密保存礼包码；有效期及领取资格未知，不入可用库存、不自动同步"
                    if item.kind == "gift"
                    else "已确认并加密保存分享链接；尚未入资源库存或同步后台，不会再次提交"
                )
            else:
                ShareJournal(self.window.db).finish(item, operation)
                self.result.setText(
                    "分享结果不确定或已取消；防重记录已保存，禁止重发，请人工核实"
                )
        except Exception:
            # Invalid results or failed durable success saves remain blocked.
            try:
                if ShareJournal(self.window.db).state(item)["state"] == "pending":
                    ShareJournal(self.window.db).finish(item, operation)
            except Exception:
                self.window.sync_error = True
            self.result.setText(
                "分享结果无法确认或保存；保留防重记录，禁止重发，请人工核实"
            )
        self.populate()

    def _share_finished(self):
        if self.share_attempt is not None:
            _owner, item, operation, _adapter = self.share_attempt
            try:
                if ShareJournal(self.window.db).state(item)["state"] == "pending":
                    ShareJournal(self.window.db).finish(item, operation)
            except Exception:
                self.window.sync_error = True
        self.share_worker = self.share_attempt = None
        self.account.setEnabled(True)
        self.mode.setEnabled(True)

    def import_saved_share(self):
        if not self.allowed():
            return
        try:
            if not getattr(self.session.adapter, "direct", False):
                raise ValueError("模拟结果不能进入真实资源库存。")
            _token, owner, item = self._selected_direct_item()
            if item.kind == "gift":
                raise ValueError(
                    "礼包有效期及领取资格未知；仅可从本地礼包成功记录查看/导出，不加入可用库存。"
                )
            require_share_writes_allowed(self.window.db)
            saved = CouponShareOutputs(self.window.db).load(owner[0], owner[1], item)
            adapter = self.session.adapter
            if saved.stock_id:
                raise ValueError("该分享结果已处理入库存，不重复导入。")
            if (
                QMessageBox.question(
                    self,
                    "确认分享结果入资源库存",
                    f"将已加密保存的{KINDS[item.kind]}分享链接加入当前门店资源库存。\n"
                    "只导入这一条已保存链接，不按查询数量复制；法宝独立分类，不推断实际扣减数量。\n"
                    "不生成新链接、不领取、不上传后台；分享成功不代表链接仍可领取，状态需人工核实。继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            if (
                not self.allowed()
                or self.session.adapter is not adapter
                or self._selected_direct_item()[1:] != (owner, item)
            ):
                raise ValueError("确认期间归属或选中券已变化，未入库存。")
            CouponShareOutputs(self.window.db).import_stock(
                owner[0], owner[1], owner, item
            )
            self.window.inventory_page.refresh()
            self.window._refresh_activity()
            self.result.setText(
                "已将一条保存的分享链接加入本地资源库存；未领取、未同步后台"
            )
        except Exception as exc:
            self.window._error(str(exc))

    def open_saved_shares(self):
        self._open_saved_results()

    def open_saved_gifts(self):
        self._open_saved_results(gift=True)

    def _open_saved_results(self, *, gift=False):
        if not self.allowed():
            return
        try:
            self.saved_dialog = SavedCouponSharesDialog(self, gift=gift)
            self.saved_dialog.exec()
        except Exception:
            self.window._error("本地已保存分享列表无法打开，未请求第三方接口。")
        finally:
            self.saved_dialog = None

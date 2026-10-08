import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from skyagent_manager.backend_query import (
    BackendCaptchaRequired,
    BackendQueryError,
    RemoteAccount,
    RemoteAccountDetail,
    RemotePage,
)
from skyagent_manager.db import StoreDatabase
from skyagent_manager.main_window import MainWindow
from skyagent_manager.sync import validate_config


class Client:
    def __init__(self, *_args, **_kwargs):
        self.closed = False
        self.pages = []
        self.mode = "normal"
        self.on_query = lambda: None

    def login(self, *_args):
        if self.mode == "error":
            raise BackendQueryError("鉴权失败")
        if self.mode == "captcha":
            raise BackendCaptchaRequired("12345678-1234-1234-1234-123456789abc")

    def captcha_image(self, identifier):
        return b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="42"><rect width="10" height="10" fill="#fff"/></svg>'

    def accounts_page(self, page, *, search=""):
        self.pages.append(page)
        self.on_query()
        return RemotePage((RemoteAccount("backend-a", "13812345678"),), page, 1, False)

    def account_detail(self, identifier):
        return RemoteAccountDetail(
            identifier, "13812345678", True, False, True, 2, 1, 3
        )

    def close(self):
        self.closed = True


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    db = StoreDatabase(tmp_path / "query.db", key=b"q" * 32)
    sid = db.add_store("A")
    db.add_store("B")
    db.configure_store(
        sid, "A", validate_config("https://test.example.invalid", "", "", False)
    )
    window = MainWindow(db.path, database=db)
    errors = []
    monkeypatch.setattr(window, "_error", errors.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    client = Client()
    window.backend_query_page.client_factory = lambda *args, **kwargs: client
    yield app, window, client, errors
    if window.sync_worker is not None:
        window.sync_worker.requestInterruption()
        window.sync_worker.wait(5000)
        app.processEvents()
    window.close()
    app.processEvents()


def wait(window):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if window.sync_worker is None else None)
    timer.start(5)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    timer.stop()
    assert window.sync_worker is None


def login(page):
    page.username.setText("test")
    page.password.setText("private-password")
    page.start(True)


def test_login_query_masks_and_clears_on_store_change(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    before = window.db.path.read_bytes()
    login(page)
    assert not window.store_combo.isEnabled()
    assert not page.password.text() and not page.password.isEnabled()
    wait(window)
    assert window.store_combo.isEnabled() and page.password.isEnabled()
    assert page.table.item(0, 0).text() == "backend-a"
    assert "13812345678" not in page.table.item(0, 1).text()
    assert window.db.path.read_bytes() == before
    page.page_number.setValue(2)
    page.start(False)
    wait(window)
    assert client.pages == [1, 2] and not errors
    window.store_combo.setCurrentIndex(1)
    assert client.closed and page.client is None and page.table.rowCount() == 0


@pytest.mark.parametrize("mode", ["error", "cancel"])
def test_failure_and_cancel_discard_session(context, mode):
    _app, window, client, errors = context
    page = window.backend_query_page
    client.mode = mode
    if mode == "cancel":
        client.on_query = lambda: window.sync_worker.requestInterruption()
    login(page)
    wait(window)
    assert client.closed and page.client is None and page.table.rowCount() == 0
    assert window.store_combo.isEnabled()
    assert bool(errors) == (mode == "error")


def test_login_confirmation_rejects_store_change(context, monkeypatch):
    _app, window, client, errors = context

    def switch(*args):
        window.store_combo.setCurrentIndex(1)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", switch)
    login(window.backend_query_page)
    assert window.sync_worker is None and not client.pages
    assert "变化" in errors[-1]
    assert not window.backend_query_page.password.text()


def test_captcha_requires_manual_second_login_and_clears(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    client.mode = "captcha"
    login(page)
    wait(window)
    assert client.closed and not client.pages and page.client is None
    assert page.captcha_id and page.captcha_image.renderer().isValid()
    assert not page.password.text() and page.captcha_answer.isEnabled()
    next_client = Client()
    page.client_factory = lambda *args, **kwargs: next_client
    page.password.setText("synthetic-password")
    page.start(True)
    assert window.sync_worker is None and "五位" in errors[-1]
    page.captcha_answer.setText("ABC12")
    page.start(True)
    wait(window)
    assert next_client.pages == [1] and not page.captcha_id
    page.clear()
    assert next_client.closed and not page.captcha_answer.text()


def test_selected_detail_read_only_masked_and_clear(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    before = window.db.path.read_bytes()
    login(page)
    wait(window)
    page.table.setCurrentCell(0, 0)
    page.detail()
    wait(window)
    assert "早餐：2" in page.detail_result.text()
    assert "可能不是实时" in page.detail_result.text()
    assert "13812345678" not in page.table.item(0, 1).text()
    assert window.db.path.read_bytes() == before and not errors
    page.clear()
    assert "早餐：2" not in page.detail_result.text() and client.closed


def test_real_discount_snapshot_display_and_search_clears(context):
    from skyagent_manager.backend_query import RemoteDiscountAsset

    _app, window, client, errors = context
    page = window.backend_query_page
    client.account_detail = lambda identifier: RemoteAccountDetail(
        identifier,
        "13812345678",
        True,
        True,
        False,
        0,
        0,
        0,
        (
            RemoteDiscountAsset(
                "abc••••1234", "后台折扣券", "30元", "未知日期", "", "AVAILABLE"
            ),
        ),
    )
    before = window.db.path.read_bytes()
    login(page)
    wait(window)
    page.table.setCurrentCell(0, 0)
    page.detail()
    wait(window)
    assert page.assets_table.rowCount() == 1
    assert page.assets_table.item(0, 1).text() == "后台折扣券"
    assert "快照" in page.assets_note.text() and "不支持分享" in page.assets_note.text()
    assert window.db.path.read_bytes() == before and not errors
    page.search.setText("changed")
    assert page.assets_table.rowCount() == 0


def test_search_navigation_and_boundary_guards(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    requests = []

    def query(number, *, search=""):
        requests.append((number, search))
        return RemotePage(
            (RemoteAccount(f"backend-{number}", "13812345678"),),
            number,
            21,
            number == 1,
        )

    client.accounts_page = query
    page.username.setText("test")
    page.search.setText("synthetic keyword")
    page.password.setText("private-password")
    page.start(True)
    assert not page.search.isEnabled()
    wait(window)
    assert requests == [(1, "synthetic keyword")]
    page.navigate(1)
    wait(window)
    assert requests[-1] == (2, "synthetic keyword")
    page.navigate(1)
    assert window.sync_worker is None and len(requests) == 2
    assert "没有更多" in errors[-1]
    page.navigate(-1)
    wait(window)
    assert requests[-1] == (1, "synthetic keyword")
    page.search.setText("changed")
    assert page.page_number.value() == 1 and page.table.rowCount() == 0
    assert page.client is client
    page.navigate(1)
    assert len(requests) == 3 and "先查询" in errors[-1]
    page.clear()
    assert not page.search.text() and client.closed

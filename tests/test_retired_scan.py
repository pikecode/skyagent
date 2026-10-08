import inspect

from PySide6.QtWidgets import QPushButton
from test_backend_query_ui import context as context

from skyagent_manager import backend_query
from skyagent_manager.about_dialog import FEATURE_STATUS
from skyagent_manager.backend_query_page import BackendQueryPage, BackendQueryWorker


def test_retired_scan_has_no_client_worker_or_page_entry():
    assert not hasattr(backend_query.BackendQueryClient, "scan_refresh")
    assert not hasattr(backend_query, "RemoteScanRefresh")
    assert not hasattr(BackendQueryPage, "scan_refresh")
    assert "scan_id" not in inspect.signature(BackendQueryPage.start).parameters
    assert "scan_id" not in inspect.signature(BackendQueryWorker).parameters
    assert "未实现" in dict(FEATURE_STATUS)["真实扫描刷新"]


def test_backend_page_has_no_real_scan_button(context):
    _app, window, _client, errors = context
    labels = [
        button.text() for button in window.backend_query_page.findChildren(QPushButton)
    ]
    assert not any("真实扫描" in label for label in labels)
    assert "确认查询已有兑换任务" in labels
    assert "确认查询管理员全局礼包库" in labels
    assert not errors

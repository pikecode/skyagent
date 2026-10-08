from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QPushButton
from test_backend_query_ui import context as context


def test_backend_controls_reachable_in_720_height_window(context):
    app, window, _client, errors = context
    window.resize(1120, 720)
    window.tabs.setCurrentWidget(window.backend_query_page)
    window.show()
    app.processEvents()
    assert window.minimumSizeHint().height() <= 720
    assert window.height() == 720
    page = window.backend_query_page
    scroll = page.scroll_area
    assert scroll.verticalScrollBar().maximum() > 0
    task_button = next(
        button
        for button in page.findChildren(QPushButton)
        if button.text() == "确认查询已有兑换任务"
    )
    for target in (page.username, task_button, page.username):
        scroll.ensureWidgetVisible(target)
        app.processEvents()
        position = target.mapTo(scroll.viewport(), QPoint(0, 0))
        assert scroll.viewport().rect().contains(position)
        assert position.y() + target.height() <= scroll.viewport().height()
    assert not errors


def test_all_tabs_still_fit_720_height_and_no_network(context):
    app, window, client, errors = context
    window.resize(1120, 720)
    window.show()
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        app.processEvents()
        assert window.height() == 720
        assert window.minimumSizeHint().height() <= 720
    assert client.pages == [] and window.sync_worker is None and not errors

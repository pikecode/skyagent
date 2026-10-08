import pytest
from test_backend_query_ui import context as context
from test_backend_query_ui import login, wait

from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backend_query import SmsLoginResult


def test_manual_sms_login_and_separate_encrypted_save(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    calls = []
    client.send_account_login_code = lambda phone, **kwargs: (
        calls.append(("send", phone, kwargs)) or 60000
    )
    client.account_login_by_code = lambda phone, code, **kwargs: (
        calls.append(("login", phone, code, kwargs))
        or SmsLoginResult("synthetic-manual-sms-token", False, True)
    )
    login(page)
    wait(window)
    sms = page.sms_login
    sms.phone.setText("13812345678")
    sms.start(False)
    wait(window)
    assert "冷却" in sms.result.text()
    assert not Accounts(window.db).list(window.current_store)
    sms.code.setText("123456")
    sms.start(True)
    wait(window)
    assert sms.pending and not sms.code.text()
    assert not Accounts(window.db).list(window.current_store)
    assert "synthetic-manual-sms-token" not in sms.result.text()
    sms.save()
    rows = Accounts(window.db).list(window.current_store)
    assert len(rows) == 1 and rows[0]["is_new_user"] is None
    assert rows[0]["token"] == "synthetic-manual-sms-token"
    assert b"synthetic-manual-sms-token" not in window.db.path.read_bytes()
    assert sms.pending is None and not errors
    assert calls[0][2] == {"authorized": True}
    assert not window.db.list_sync_results(window.current_store)


def test_sms_cancel_discards_login_token(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    login(page)
    wait(window)
    sms = page.sms_login

    def login_code(*args, **kwargs):
        sms.worker.requestInterruption()
        return SmsLoginResult("synthetic-discarded-token", False, False)

    client.account_login_by_code = login_code
    sms.phone.setText("13812345678")
    sms.code.setText("123456")
    sms.start(True)
    wait(window)
    assert sms.pending is None and client.closed
    assert not Accounts(window.db).list(window.current_store)
    assert "可能已发送" in sms.result.text() and not errors


def test_sms_authorization_decline_no_request(context, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _app, window, client, errors = context
    page = window.backend_query_page
    login(page)
    wait(window)
    calls = []
    client.send_account_login_code = lambda *args, **kwargs: calls.append(args)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    sms = page.sms_login
    sms.phone.setText("13812345678")
    sms.start(False)
    assert not calls and window.sync_worker is None and not errors


def test_sms_save_decline_and_number_change_discard(context, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _app, window, client, errors = context
    page = window.backend_query_page
    login(page)
    wait(window)
    client.account_login_by_code = lambda *args, **kwargs: SmsLoginResult(
        "synthetic-no-save-token", False, False
    )
    sms = page.sms_login
    sms.phone.setText("13812345678")
    sms.code.setText("123456")
    sms.start(True)
    wait(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    sms.save()
    assert sms.pending and not Accounts(window.db).list(window.current_store)
    sms.phone.setText("13912345678")
    assert sms.pending is None
    assert not Accounts(window.db).list(window.current_store) and not errors


@pytest.mark.parametrize("change", ["phone", "store", "duplicate"])
def test_save_confirmation_change_and_duplicate_never_overwrite(
    context, monkeypatch, change
):
    from PySide6.QtWidgets import QMessageBox

    _app, window, client, errors = context
    page = window.backend_query_page
    login(page)
    wait(window)
    sid = window.current_store
    client.account_login_by_code = lambda *args, **kwargs: SmsLoginResult(
        "synthetic-boundary-token", False, True
    )
    sms = page.sms_login
    sms.phone.setText("13812345678")
    sms.code.setText("123456")
    sms.start(True)
    wait(window)
    if change == "duplicate":
        Accounts(window.db).add(
            sid,
            AccountInput("13812345678", "synthetic-original-token", is_new_user=False),
        )

    def confirm(*args):
        if change == "phone":
            sms.phone.setText("13912345678")
        elif change == "store":
            other = window.db.add_store("other")
            window._reload_stores(other)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    sms.save()
    rows = Accounts(window.db).list(sid)
    assert len(rows) == int(change == "duplicate")
    if rows:
        assert rows[0]["token"] == "synthetic-original-token" and errors
    else:
        assert sms.pending is None
    assert not window.db.list_sync_results(sid)


def test_close_during_sms_waits_and_discards_token(context):
    from threading import Event

    _app, window, client, errors = context
    page = window.backend_query_page
    login(page)
    wait(window)
    entered, release = Event(), Event()

    def login_code(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return SmsLoginResult("synthetic-close-token", False, False)

    client.account_login_by_code = login_code
    sms = page.sms_login
    sms.phone.setText("13812345678")
    sms.code.setText("123456")
    sms.start(True)
    try:
        assert entered.wait(2)
        window.close()
        assert window.close_pending and window.sync_worker is not None
    finally:
        release.set()
        wait(window)
    assert sms.pending is None and client.closed and not errors

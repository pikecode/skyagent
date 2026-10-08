import pytest
from PySide6.QtWidgets import QMessageBox
from test_backend_query import Response, Session
from test_backend_query_ui import context as context
from test_backend_query_ui import login, wait
from test_legacy_migration import db as db

from skyagent_manager.accounts import AccountInput, Accounts
from skyagent_manager.backend_query import (
    BackendQueryClient,
    BackendQueryError,
    RemoteAccountCredential,
)
from skyagent_manager.backup import BackupService

TOKEN = "synthetic-backend-account-token"


def credential_client(monkeypatch, token=TOKEN):
    session = Session()
    client = BackendQueryClient("https://test.example.invalid", session=session)
    client.login("test", "synthetic-password")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path.endswith("/token"):
            return {"token": token, "ignored": "private-extra"}
        return {
            "id": "a1",
            "phone": "13812345678",
            "is_enabled": True,
            "is_online": True,
            "is_new_user": True,
            "breakfast_coupons": 0,
            "room_upgrade_coupons": 0,
            "late_checkout_coupons": 0,
        }

    monkeypatch.setattr(client, "_request", request)
    return client, calls, request


def test_credential_projection_and_identity_recheck(monkeypatch):
    client, calls, _ = credential_client(monkeypatch)
    result = client.account_credential("a1", authorized=True)
    assert result == RemoteAccountCredential("a1", "13812345678", TOKEN)
    assert TOKEN not in repr(result) and "13812345678" not in repr(result)
    assert calls == [
        ("GET", "/api/pool/accounts/a1"),
        ("GET", "/api/pool/accounts/a1/token"),
        ("GET", "/api/pool/accounts/a1"),
    ]


@pytest.mark.parametrize("token", [None, 123, "short", "x" * 4097, "private token"])
def test_invalid_credentials_redacted(monkeypatch, token):
    client, calls, _ = credential_client(monkeypatch, token)
    with pytest.raises(BackendQueryError) as error:
        client.account_credential("a1", authorized=True)
    assert "private token" not in str(error.value)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["decline", "no-session", "invalid-id"])
def test_credential_guard_no_requests(monkeypatch, mode):
    client, calls, _ = credential_client(monkeypatch)
    if mode == "no-session":
        client._token = ""
    with pytest.raises(BackendQueryError):
        client.account_credential(
            "../other" if mode == "invalid-id" else "a1", authorized=mode != "decline"
        )
    assert not calls


@pytest.mark.parametrize("at", [0, 1, 2, 3])
def test_credential_cancel_request_boundaries(monkeypatch, at):
    client, calls, _ = credential_client(monkeypatch)
    with pytest.raises(BackendQueryError):
        client.account_credential(
            "a1", authorized=True, cancelled=lambda: len(calls) >= at
        )
    assert len(calls) == at


def test_credential_phone_change_discards(monkeypatch):
    client, calls, request = credential_client(monkeypatch)

    def changed(*args, **kwargs):
        result = request(*args, **kwargs)
        if len(calls) == 3:
            result["phone"] = "13912345678"
        return result

    monkeypatch.setattr(client, "_request", changed)
    with pytest.raises(BackendQueryError, match="变化"):
        client.account_credential("a1", authorized=True)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_credential_http_error_no_fallback_or_retry(monkeypatch, status):
    client = BackendQueryClient("https://test.example.invalid", session=Session())
    client.login("test", "synthetic-password")
    from skyagent_manager.backend_query import RemoteAccountDetail

    monkeypatch.setattr(
        client,
        "account_detail",
        lambda identifier: RemoteAccountDetail(
            identifier, "13812345678", True, True, False, 0, 0, 0
        ),
    )
    client.session.response = Response({"message": TOKEN}, status)
    with pytest.raises(BackendQueryError) as error:
        client.account_credential("a1", authorized=True)
    assert TOKEN not in str(error.value)
    assert len(client.session.calls) == 2
    assert client.session.calls[-1][1].endswith("/api/pool/accounts/a1/token")


def test_save_backend_account_atomic_hold_and_backup(db, tmp_path):
    sid = db.list_stores()[0]["id"]
    accounts = Accounts(db)
    rid = accounts.save_backend_account(sid, "13812345678", TOKEN, authorized=True)
    row = accounts.get(sid, rid)
    assert row["is_new_user"] is None and accounts.list(sid)[0]["import_state"] is None
    assert accounts.import_hold(sid, row) == "backend-existing"
    assert TOKEN.encode() not in db.path.read_bytes()
    with pytest.raises(ValueError, match="禁止重复上传"):
        accounts.sync_items(sid, [rid])
    accounts.update(
        sid, rid, AccountInput("13812345678", TOKEN + "-updated", is_new_user=True)
    )
    with pytest.raises(ValueError, match="禁止重复上传"):
        accounts.sync_items(sid, [rid])
    backup = BackupService(db)
    path = backup.create(tmp_path / "backend-copy.skybackup")
    backup.restore(path)
    assert accounts.import_hold(sid, accounts.get(sid, rid)) == "backend-existing"


@pytest.mark.parametrize("mode", ["decline", "duplicate", "disk-failure"])
def test_save_backend_account_failure_no_partial_write(db, monkeypatch, mode):
    sid = db.list_stores()[0]["id"]
    accounts = Accounts(db)
    if mode == "duplicate":
        accounts.add(sid, AccountInput("13812345678", TOKEN))
    before, memory = db.path.read_bytes(), db.connection.serialize()
    if mode == "disk-failure":

        def fail():
            raise OSError("synthetic failure")

        monkeypatch.setattr(db.connection, "persist", fail)
    with pytest.raises((ValueError, OSError)):
        accounts.save_backend_account(
            sid, "13812345678", TOKEN, authorized=mode != "decline"
        )
    assert db.path.read_bytes() == before and db.connection.serialize() == memory


def setup_widget(context):
    _app, window, client, errors = context
    page = window.backend_query_page
    client.account_credential = lambda identifier, **kwargs: RemoteAccountCredential(
        identifier, "13812345678", TOKEN
    )
    login(page)
    wait(window)
    page.table.selectRow(0)
    return window, client, errors, page.account_save


def test_gui_read_then_separate_encrypted_save(context):
    window, client, errors, widget = setup_widget(context)
    before = window.db.path.read_bytes()
    widget.start()
    assert not widget.backend.table.isEnabled()
    wait(window)
    assert widget.pending and window.db.path.read_bytes() == before
    assert TOKEN not in widget.result.text()
    widget.save()
    rows = Accounts(window.db).list(window.current_store)
    assert len(rows) == 1 and rows[0]["token"] == TOKEN
    assert (
        Accounts(window.db).import_hold(window.current_store, rows[0])
        == "backend-existing"
    )
    assert widget.pending is None and not errors
    assert "后台已有账号" in window.account_page.table.item(0, 5).text()


@pytest.mark.parametrize("mode", ["decline", "cancel", "identity-mismatch", "error"])
def test_gui_no_implicit_save(context, monkeypatch, mode):
    window, client, errors, widget = setup_widget(context)
    calls = []

    def read(identifier, **kwargs):
        calls.append(identifier)
        if mode == "cancel":
            window.sync_worker.requestInterruption()
        if mode == "error":
            raise ValueError(TOKEN)
        return RemoteAccountCredential(
            "other" if mode == "identity-mismatch" else identifier, "13812345678", TOKEN
        )

    client.account_credential = read
    if mode == "decline":
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    widget.start()
    if mode != "decline":
        wait(window)
    assert widget.pending is None and not Accounts(window.db).list(window.current_store)
    assert TOKEN not in widget.result.text() and not errors
    assert bool(calls) == (mode != "decline")


@pytest.mark.parametrize(
    "mode", ["store", "selection", "session", "save-decline", "duplicate"]
)
def test_gui_pending_identity_and_save_guards(context, monkeypatch, mode):
    window, client, errors, widget = setup_widget(context)
    widget.start()
    wait(window)
    if mode == "store":
        window.store_combo.setCurrentIndex(1)
    elif mode == "selection":
        widget.backend.table.clearSelection()
    elif mode == "session":
        widget.backend.clear()
    elif mode == "save-decline":
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    else:
        Accounts(window.db).add(
            window.current_store, AccountInput("13812345678", TOKEN + "-original")
        )
    widget.save()
    rows = Accounts(window.db).list(window.current_store)
    assert len(rows) == (1 if mode == "duplicate" else 0)
    if rows:
        assert rows[0]["token"] == TOKEN + "-original" and errors


@pytest.mark.parametrize("phase", ["read", "save"])
@pytest.mark.parametrize("change", ["store", "session", "selection"])
def test_gui_confirmation_target_changes(context, monkeypatch, phase, change):
    window, client, errors, widget = setup_widget(context)
    calls = []
    client.account_credential = lambda identifier, **kwargs: (
        calls.append(identifier)
        or RemoteAccountCredential(identifier, "13812345678", TOKEN)
    )
    if phase == "save":
        widget.start()
        wait(window)
    calls.clear()

    def confirm(*args):
        if change == "store":
            window.store_combo.setCurrentIndex(1)
        elif change == "session":
            widget.backend.clear()
        else:
            widget.backend.table.clearSelection()
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    widget.start() if phase == "read" else widget.save()
    assert not calls and window.sync_worker is None
    assert not Accounts(window.db).list(window.current_store)


def test_backend_existing_hold_survives_delete_and_readd(db):
    sid = db.list_stores()[0]["id"]
    accounts = Accounts(db)
    rid = accounts.save_backend_account(sid, "13812345678", TOKEN, authorized=True)
    accounts.delete(sid, [rid])
    rid = accounts.add(
        sid, AccountInput("13812345678", TOKEN + "-new", is_new_user=True)
    )
    with pytest.raises(ValueError, match="禁止重复上传"):
        accounts.sync_items(sid, [rid])

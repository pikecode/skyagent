from __future__ import annotations

from datetime import date

import pytest

from skyagent_manager.db import StoreDatabase


@pytest.fixture
def ledger(tmp_path):
    database = StoreDatabase(tmp_path / "ledger.sqlite3", key=b"l" * 32)
    store = database.add_store("A")
    member = database.add_member(store, "Alpha", "13812345678", "原备注")
    benefit = database.add_benefit(
        store, "breakfast", "早餐", "coupon-A", "2026-09-30", member_phone="13812345678"
    )
    yield database, store, member, benefit
    database.close()


def test_member_edit_preserves_id_and_creation_and_invalidates_sync(
    ledger, monkeypatch
):
    database, store, member, benefit = ledger
    before = dict(database.get_member(store, member))
    database.save_sync_result(store, "members", member, True, "已同步")
    monkeypatch.setattr(
        "skyagent_manager.db.utc_now", lambda: "2026-09-30T12:00:00+00:00"
    )
    database.update_member(store, member, "Beta", "139 1234 5678", "新备注")
    after = database.get_member(store, member)
    assert after["id"] == member and after["created_at"] == before["created_at"]
    assert after["authorized_at"] == "2026-09-30T12:00:00+00:00"
    assert after["display_name"] == "Beta" and after["phone_norm"] == "13912345678"
    assert database.get_benefit(store, benefit)["member_id"] == member
    assert database.list_sync_results(store) == []
    assert database.list_activity(store)[0]["action"] == "编辑会员"


def test_member_edit_rejects_duplicate_without_changes(ledger):
    database, store, member, _ = ledger
    database.add_member(store, "Other", "13912345678")
    original = database.path.read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        database.update_member(store, member, "Beta", "139-1234-5678")
    assert database.get_member(store, member)["display_name"] == "Alpha"
    assert database.path.read_bytes() == original


def test_delete_member_preserves_benefits_and_clears_linked_sync(ledger):
    database, store, member, benefit = ledger
    other = database.add_benefit(store, "gift", "无会员礼包")
    for entity, identifier in [
        ("members", member),
        ("benefits", benefit),
        ("benefits", other),
    ]:
        database.save_sync_result(store, entity, identifier, True, "已同步")
    database.delete_member(store, member)
    assert database.list_members(store) == []
    assert database.get_benefit(store, benefit)["member_id"] is None
    assert len(database.list_benefits(store)) == 2
    assert [row["record_id"] for row in database.list_sync_results(store)] == [other]
    audit = database.list_activity(store)[0]
    assert audit["action"] == "删除会员" and "13812345678" not in audit["summary"]


def test_edit_and_delete_benefit_reset_sync_and_keep_member(ledger):
    database, store, member, benefit = ledger
    second = database.add_member(store, "Second", "13912345678")
    created = database.get_benefit(store, benefit)["created_at"]
    database.save_sync_result(store, "benefits", benefit, True, "已同步")
    database.update_benefit(
        store,
        benefit,
        "gift",
        "礼品",
        "coupon-B",
        "2026-10-07",
        4,
        "备注",
        "13912345678",
        "已预留",
    )
    edited = database.get_benefit(store, benefit)
    assert edited["created_at"] == created and edited["member_id"] == second
    assert (edited["kind"], edited["code"], edited["quantity"], edited["state"]) == (
        "gift",
        "coupon-B",
        4,
        "已预留",
    )
    assert database.list_sync_results(store) == []
    database.delete_benefit(store, benefit)
    assert not database.list_benefits(store)
    assert len(database.list_members(store)) == 2
    assert database.get_member(store, member)


@pytest.mark.parametrize(
    "action",
    ["member-edit", "member-delete", "benefit-edit", "benefit-delete", "state"],
)
def test_cross_store_mutations_are_rejected(ledger, action):
    database, _, member, benefit = ledger
    other = database.add_store("B")
    calls = {
        "member-edit": lambda: database.update_member(
            other, member, "Name", "13812345678"
        ),
        "member-delete": lambda: database.delete_member(other, member),
        "benefit-edit": lambda: database.update_benefit(other, benefit, "gift", "Gift"),
        "benefit-delete": lambda: database.delete_benefit(other, benefit),
        "state": lambda: database.set_benefit_state(benefit, "已使用", store_id=other),
    }
    original = database.path.read_bytes()
    with pytest.raises(ValueError):
        calls[action]()
    assert database.path.read_bytes() == original


def test_archived_store_is_read_only(ledger):
    database, store, member, benefit = ledger
    database.archive_store(store)
    for operation in [
        lambda: database.update_member(store, member, "Name", "13812345678"),
        lambda: database.delete_member(store, member),
        lambda: database.delete_benefit(store, benefit),
        lambda: database.add_member(store, "New", "13912345678"),
        lambda: database.add_benefit(store, "gift", "Gift"),
        lambda: database.import_members_detailed(store, []),
        lambda: database.import_benefits_detailed(store, []),
        lambda: database.set_benefit_state(benefit, "已使用", store_id=store),
    ]:
        with pytest.raises(ValueError, match="未归档"):
            operation()


@pytest.mark.parametrize(
    "operation", ["member-edit", "member-delete", "benefit-edit", "benefit-delete"]
)
def test_ledger_write_failure_rolls_back_audit_and_records(
    ledger, monkeypatch, operation
):
    database, store, member, benefit = ledger
    original = database.path.read_bytes()
    snapshot = database.connection.serialize()
    calls = {
        "member-edit": lambda: database.update_member(
            store, member, "New", "13912345678"
        ),
        "member-delete": lambda: database.delete_member(store, member),
        "benefit-edit": lambda: database.update_benefit(store, benefit, "gift", "New"),
        "benefit-delete": lambda: database.delete_benefit(store, benefit),
    }

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("skyagent_manager.db.atomic_write", fail)
    with pytest.raises(OSError):
        calls[operation]()
    assert database.path.read_bytes() == original
    assert database.connection.serialize() == snapshot


def test_search_literal_wildcards_and_duplicate_review(ledger):
    database, store, _, _ = ledger
    second = database.add_member(store, "Beta", "13912345678", "100%_literal")
    assert [row["id"] for row in database.list_members(store, search="%_")] == [second]
    assert database.list_members(store, search="138-1234")[0]["display_name"] == "Alpha"
    assert database.list_members(store, duplicates_only=True) == []
    with database.connection:
        database.connection.execute(
            "INSERT INTO members(id,store_id,display_name,phone,phone_norm,note,authorized_at,created_at) VALUES ('legacy', ?, 'Legacy', '138-1234-5678', '13812345678', '', '', '')",
            (store,),
        )
    assert len(database.list_members(store, duplicates_only=True)) == 2
    with pytest.raises(ValueError, match="多个历史会员"):
        database.add_benefit(store, "gift", "Gift", member_phone="13812345678")


def test_benefit_state_kind_and_expiry_filters(ledger):
    database, store, _, benefit = ledger
    past = database.add_benefit(store, "gift", "Expired", expires_at="2026-09-29")
    edge = database.add_benefit(store, "gift", "Edge", expires_at="2026-10-07")
    database.add_benefit(store, "gift", "Future", expires_at="2026-10-08")
    no_expiry = database.add_benefit(store, "gift", "No expiry")
    today = date(2026, 9, 30)
    assert {
        row["id"] for row in database.list_benefits(store, expiry="week", today=today)
    } == {benefit, edge}
    assert [
        row["id"] for row in database.list_benefits(store, expiry="past", today=today)
    ] == [past]
    assert [row["id"] for row in database.list_benefits(store, expiry="none")] == [
        no_expiry
    ]
    database.set_benefit_state(edge, "已预留", store_id=store)
    assert [
        row["id"]
        for row in database.list_benefits(
            store, kind="gift", state="已预留", expiry="week", today=today
        )
    ] == [edge]


@pytest.mark.parametrize(
    "expiry,quantity",
    [
        ("2026-02-29", 1),
        ("20260930", 1),
        ("2026-13-01", 1),
        ("", -1),
        ("", 1_000_001),
        ("", True),
    ],
)
def test_manual_and_edit_validation_rejects_invalid_inputs(ledger, expiry, quantity):
    database, store, _, benefit = ledger
    with pytest.raises(ValueError):
        database.add_benefit(
            store, "gift", "Gift", expires_at=expiry, quantity=quantity
        )
    with pytest.raises(ValueError):
        database.update_benefit(
            store, benefit, "gift", "Gift", expires_at=expiry, quantity=quantity
        )

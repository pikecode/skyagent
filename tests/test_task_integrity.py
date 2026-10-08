import sqlite3

import pytest

from skyagent_manager.backup import validate_snapshot
from skyagent_manager.db import StoreDatabase
from skyagent_manager.inventory import Inventory
from skyagent_manager.security import encrypt


@pytest.mark.parametrize(
    "corruption",
    ["duplicate", "terminal_reserved", "success_released", "used_available", "empty"],
)
def test_invalid_task_snapshot_rejected_without_overwrite(tmp_path, corruption):
    path = tmp_path / "manager.db"
    key = b"i" * 32
    db = StoreDatabase(path, key=key)
    sid = db.add_store("A")
    stock = Inventory(db)
    stock.import_values(sid, "gift", ["resource"])
    rid = stock.list(sid)[0]["id"]
    stock.start(sid, [rid])
    conn = sqlite3.connect(":memory:")
    conn.deserialize(db.connection.serialize())
    if corruption == "duplicate":
        conn.execute(
            "INSERT INTO local_tasks VALUES(?,?,?,?)",
            ("another", sid, "running", "test"),
        )
        conn.execute(
            "INSERT INTO local_task_items VALUES(?,?,?,?)",
            (sid, "another", rid, "reserved"),
        )
    elif corruption == "terminal_reserved":
        conn.execute("UPDATE local_tasks SET state='canceled'")
    elif corruption == "success_released":
        conn.execute("UPDATE local_tasks SET state='succeeded'")
        conn.execute("UPDATE local_task_items SET state='released'")
        conn.execute("UPDATE stock SET state='available'")
    elif corruption == "used_available":
        conn.execute("UPDATE local_tasks SET state='succeeded'")
        conn.execute("UPDATE local_task_items SET state='used'")
        conn.execute("UPDATE stock SET state='available'")
    else:
        conn.execute("DELETE FROM local_task_items")
        conn.execute("UPDATE stock SET state='available'")
    conn.commit()
    snapshot = conn.serialize()
    conn.close()
    db.close()
    with pytest.raises(ValueError):
        validate_snapshot(snapshot)
    path.write_bytes(encrypt(snapshot, key))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        StoreDatabase(path, key=key)
    assert path.read_bytes() == before


def test_failed_resource_reused_by_new_task_is_valid(tmp_path):
    db = StoreDatabase(tmp_path / "db", key=b"v" * 32)
    try:
        sid = db.add_store("A")
        stock = Inventory(db)
        stock.import_values(sid, "gift", ["resource"])
        rid = stock.list(sid)[0]["id"]
        task = stock.start(sid, [rid])
        stock.complete_item(sid, task, rid, succeeded=False)
        stock.start(sid, [rid])
        assert validate_snapshot(db.connection.serialize())["local_tasks"] == 2
    finally:
        db.close()

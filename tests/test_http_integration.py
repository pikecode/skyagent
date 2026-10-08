from __future__ import annotations

import pytest

from skyagent_manager.contract_check import check_contract, local_backend, main
from skyagent_manager.db import StoreDatabase
from skyagent_manager.sync import SyncClient, SyncWorker, validate_config


def test_real_loopback_http_contract():
    assert check_contract()["ok"]


def test_contract_report_without_stdout(tmp_path, monkeypatch):
    import json

    report = tmp_path / "report.json"
    monkeypatch.setattr("skyagent_manager.contract_check.sys.stdout", None)
    assert main(report) == 0
    assert (
        json.loads(report.read_text(encoding="utf-8"))["scope"]
        == "loopback-only-synthetic"
    )


@pytest.mark.parametrize(
    "entity,path", [("members", "/members"), ("benefits", "/benefits")]
)
def test_http_payload_result_persistence_and_failed_retry(tmp_path, entity, path):
    database = StoreDatabase(tmp_path / "manager.sqlite3", key=b"h" * 32)
    try:
        sid = database.add_store("本地联调")
        database.add_member(sid, "accepted-synthetic", "00000000000")
        database.add_member(sid, "reject-synthetic", "00000000001")
        database.add_benefit(sid, "breakfast", "accepted-synthetic", "synthetic-code-a")
        database.add_benefit(sid, "breakfast", "reject-synthetic", "synthetic-code-b")
        with local_backend() as (url, state):
            config = validate_config(url, "/members", "/benefits", True)
            client = SyncClient(config, entity, "synthetic-secret")
            try:
                items = database.sync_items(sid, entity)
                results = client.submit(items)
                database.save_sync_results(sid, entity, results)
                assert state["requests"][0] == (path, {"items": items})
                assert sorted(r.ok for r in results) == [False, True]
                failed = database.sync_items(sid, entity, failed_only=True)
                assert len(failed) == 1 and failed[0]["name"] == "reject-synthetic"
                count = len(state["requests"])
                client.submit(failed)
                assert len(state["requests"]) == count + 1
            finally:
                client.close()
    finally:
        database.close()


def test_real_http_worker_splits_batches():
    with local_backend() as (url, state):
        config = validate_config(url, "/members", "/benefits", True)
        client = SyncClient(config, "members", "synthetic-secret")
        items = [{"id": str(i), "name": "synthetic"} for i in range(401)]
        worker = SyncWorker("test-store", "members", items, client)
        worker.run()
        assert [len(payload["items"]) for _, payload in state["requests"]] == [
            200,
            200,
            1,
        ]
        assert len(state["records"]) == 401


@pytest.mark.parametrize("mode", ["missing-result", "duplicate-index"])
def test_real_http_malformed_indices(mode):
    with local_backend() as (url, state):
        state["mode"] = mode
        client = SyncClient(
            validate_config(url, "/members", "/benefits", True),
            "members",
            "synthetic-secret",
        )
        try:
            results = client.submit([{"id": "a"}])
            assert not results[0].ok
        finally:
            client.close()

import json
import runpy
from pathlib import Path

import pytest


def runner():
    return runpy.run_path(
        str(
            Path(__file__).resolve().parents[1] / "packaging" / "workflow_acceptance.py"
        )
    )


def test_complete_synthetic_workflow_and_private_evidence(tmp_path):
    module = runner()
    output = tmp_path / "evidence"
    report = module["run"](output)
    assert report["ok"], report
    assert len(report["steps"]) == 7
    assert report["protocol_requests"] == {
        "query": 4,
        "share_success": 1,
        "share_lost_reply": 1,
    }
    assert report["network"] == "blocked" and not report["production_verified"]
    assert json.loads((output / "report.json").read_text()) == report
    assert {file.name for file in output.iterdir()} == {
        "report.json",
        "masked-history.csv",
    }
    for file in output.iterdir():
        content = file.read_text(encoding="utf-8-sig")
        assert module["TOKEN"] not in content and module["URL"] not in content
        assert "13900000000" not in content
    with pytest.raises(FileExistsError):
        module["run"](output)


def test_failure_report_does_not_claim_success_or_leak(tmp_path, monkeypatch):
    module = runner()

    def broken(*args, **kwargs):
        raise RuntimeError(module["TOKEN"])

    monkeypatch.setattr(module["Accounts"], "import_rows", broken)
    output = tmp_path / "failed"
    report = module["run"](output)
    assert not report["ok"] and report["steps"] == []
    assert module["TOKEN"] not in (output / "report.json").read_text()

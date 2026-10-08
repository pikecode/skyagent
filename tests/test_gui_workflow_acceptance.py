import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["coupon", "gift"])
def test_gui_workflow_cli_and_preserved_evidence(tmp_path, kind):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "gui-evidence"
    command = [
        sys.executable,
        str(root / "packaging" / "gui_workflow_acceptance.py"),
        "--offscreen",
        "--kind",
        kind,
        "--output",
        str(output),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=root
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] and report["qt_platform"] == "offscreen"
    assert len(report["steps"]) == 7
    assert report["protocol_requests"] == {
        "query": 1 if kind == "gift" else 4,
        "share": 1,
    }
    assert report["resource_kind"] == kind
    assert report["duplicate_generation_blocked"]
    if kind == "gift":
        assert not report["gift_qualification_verified"]
        assert not report["gift_consumable_stock"]
    assert (
        not report["production_verified"]
        and not report["human_mouse_keyboard_acceptance"]
    )
    assert any(not row["accepted"] for row in report["confirmations"])
    assert len(list(output.glob("*.png"))) == (5 if kind == "gift" else 4)
    for filename in ("report.json", "masked-history.csv"):
        content = (output / filename).read_text(encoding="utf-8-sig")
        assert "13900000000" not in content
        assert "synthetic-workflow-authorized-token" not in content
        assert "synthetic-workflow-output" not in content
        assert "synthetic-workflow-private-gift-code" not in content
        assert "synthetic-workflow-folio" not in content
        assert "synthetic-workflow-chain" not in content
    before = {file.name: file.read_bytes() for file in output.iterdir()}
    repeated = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=root
    )
    assert repeated.returncode != 0
    assert {file.name: file.read_bytes() for file in output.iterdir()} == before

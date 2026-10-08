import hashlib
import json
import runpy
import zipfile
from pathlib import Path

import pytest


def module():
    return runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "packaging" / "windows_release.py")
    )


def test_windows_archive_contains_whole_runtime_and_instructions(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "SkyAgentManager.exe").write_bytes(b"synthetic-not-an-executable")
    (bundle / "_internal" / "runtime.dll").write_bytes(b"synthetic-not-a-runtime")
    output = tmp_path / "test.zip"
    digest = module()["archive_bundle"](bundle, output)
    assert digest == hashlib.sha256(output.read_bytes()).hexdigest()
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {
            "SkyAgentManager/SkyAgentManager.exe",
            "SkyAgentManager/_internal/runtime.dll",
            "WINDOWS_README.txt",
        }
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        module()["archive_bundle"](bundle, output)
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "payload", [{"ok": False}, {"ok": 1}, [], {"ok": True, "version": "wrong"}]
)
def test_report_must_be_success_and_expected_version(tmp_path, payload):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        module()["check_report"](report, version="0.2.28")


def test_native_windows_gate_does_not_create_fake_package(tmp_path, monkeypatch):
    data = module()
    monkeypatch.setattr(data["sys"], "platform", "darwin")
    output = tmp_path / "uncreated"
    with pytest.raises(RuntimeError, match="native Windows"):
        data["release"](output=output)
    assert not output.exists()


def test_missing_runtime_does_not_publish_zip(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "SkyAgentManager.exe").write_bytes(b"synthetic")
    output = tmp_path / "uncreated.zip"
    with pytest.raises(ValueError, match="runtime"):
        module()["archive_bundle"](bundle, output)
    assert not output.exists()


@pytest.mark.parametrize("passed", [True, False])
def test_release_requires_frozen_reports_before_manifest(tmp_path, monkeypatch, passed):
    data = module()
    monkeypatch.setattr(data["sys"], "platform", "win32")
    monkeypatch.setattr(data["platform"], "machine", lambda: "AMD64")
    bundle = tmp_path / "native-substitute"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "SkyAgentManager.exe").write_bytes(b"synthetic-not-an-executable")
    calls = []

    def substitute_process(arguments, **kwargs):
        calls.append(arguments[1])
        assert kwargs["check"] and kwargs["timeout"] == 120
        payload = {"ok": passed if arguments[1] == "--self-check" else True}
        if arguments[1] == "--self-check":
            payload["version"] = "0.2.28"
        Path(arguments[-1]).write_text(json.dumps(payload))

    monkeypatch.setattr(data["subprocess"], "run", substitute_process)
    output = tmp_path / "candidate"
    if not passed:
        with pytest.raises(ValueError, match="did not pass"):
            data["release"](output=output, existing_build=bundle)
        assert not list(output.glob("*.zip"))
        assert not (output / "release.json").exists()
    else:
        manifest = data["release"](output=output, existing_build=bundle)
        assert manifest["ok"] and manifest["target"] == "Windows-x64"
        assert not manifest["production_verified"] and not manifest["signed"]
        archive = output / manifest["archive"]
        assert manifest["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
        assert manifest["sha256"] in (output / "SHA256SUMS.txt").read_text()
        with pytest.raises(FileExistsError):
            data["release"](output=output, existing_build=bundle)
    assert calls == ["--self-check", "--sync-self-check"]

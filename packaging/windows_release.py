"""Build/verify a Windows x64 development ZIP on a native Windows host."""

import argparse
import hashlib
import json
import platform
import struct
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_report(path, *, version=None):
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Frozen verification report missing or oversized")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("ok") is not True:
        raise ValueError("Frozen verification did not pass")
    if version is not None and report.get("version") != version:
        raise ValueError("Frozen version does not match source metadata")
    return report


def archive_bundle(bundle, destination):
    """Include the whole onedir runtime; the exe alone is not distributable."""
    files = sorted(bundle.rglob("*"))
    if not (bundle / "SkyAgentManager.exe").is_file():
        raise ValueError("Windows executable missing")
    if not (bundle / "_internal").is_dir():
        raise ValueError("Windows onedir runtime missing")
    if any(path.is_symlink() for path in files):
        raise ValueError("Unexpected symlink in Windows bundle")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_file():
                archive.write(
                    path, "SkyAgentManager/" + path.relative_to(bundle).as_posix()
                )
        archive.write(ROOT / "packaging" / "WINDOWS_README.txt", "WINDOWS_README.txt")
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest


def release(*, output=None, existing_build=None):
    if sys.platform != "win32":
        raise RuntimeError(
            "Windows release requires native Windows; no cross-compilation"
        )
    if struct.calcsize("P") != 8 or platform.machine().upper() not in {
        "AMD64",
        "X86_64",
    }:
        raise RuntimeError("This release entry point requires Windows x64 Python")
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    output = Path(output or ROOT / "dist" / f"windows-release-{version}").resolve()
    if output.exists():
        raise FileExistsError(
            "Existing release preserved; choose a new output directory"
        )
    bundle = (
        Path(existing_build).resolve()
        if existing_build
        else output / "bundle" / "SkyAgentManager"
    )
    if existing_build and not (bundle / "SkyAgentManager.exe").is_file():
        raise ValueError("Existing Windows bundle is missing")
    output.mkdir(parents=True)
    # Frozen checks must pass before ZIP creation; only complete releases get
    # a success manifest. Failed candidate directories are kept for diagnosis.
    if existing_build is None:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "packaging" / "build.py"),
                "--output",
                str(output / "bundle"),
            ],
            cwd=ROOT,
            check=True,
        )
    executable = bundle / "SkyAgentManager.exe"
    if not executable.is_file():
        raise ValueError("Windows build produced no executable")
    startup_report = output / "frozen-check.json"
    sync_report = output / "frozen-sync-check.json"
    for flag, report_path in (
        ("--self-check", startup_report),
        ("--sync-self-check", sync_report),
    ):
        subprocess.run(
            [str(executable), flag, "--self-check-report", str(report_path)],
            cwd=ROOT,
            check=True,
            timeout=120,
        )
    check_report(startup_report, version=version)
    check_report(sync_report)
    archive = output / f"SkyAgentManager-{version}-Windows-x64-dev.zip"
    digest = archive_bundle(bundle, archive)
    (output / "SHA256SUMS.txt").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    manifest = {
        "ok": True,
        "version": version,
        "target": "Windows-x64",
        "archive": archive.name,
        "sha256": digest,
        "signed": False,
        "frozen_self_check": True,
        "loopback_contract_check": True,
        "native_credentials_verified": False,
        "human_desktop_acceptance": False,
        "production_verified": False,
    }
    (output / "release.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--existing-build",
        type=Path,
        help="Verify/package an existing native Windows onedir build",
    )
    options = parser.parse_args()
    try:
        report = release(output=options.output, existing_build=options.existing_build)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        print(
            "Windows release not completed. Native Windows x64, a fresh output directory and passing frozen checks are required.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

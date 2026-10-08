import sys
import tomllib
from pathlib import Path

root = Path(SPECPATH).parent
with (root / "pyproject.toml").open("rb") as stream:
    release_version = tomllib.load(stream)["project"]["version"]
backend = "keyring.backends.macOS" if sys.platform == "darwin" else "keyring.backends.Windows"
a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=[backend],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "ruff"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="SkyAgentManager", console=False,
)
collection = COLLECT(exe, a.binaries, a.datas, name="SkyAgentManager")
if sys.platform == "darwin":
    app = BUNDLE(
        collection, name="SkyAgentManager.app",
        bundle_identifier="com.skyagent.manager",
        info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": release_version, "CFBundleVersion": release_version},
    )

"""Build on the target OS; isolate generated files and PyInstaller cache."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, help="New output directory for this build")
options = parser.parse_args()
if sys.platform not in {"darwin", "win32"}:
    raise SystemExit("Build on Windows or macOS, not via cross-compilation.")
environment = dict(os.environ)
environment["PYINSTALLER_CONFIG_DIR"] = str(root / "build" / "pyinstaller-cache")
output = options.output.resolve() if options.output else root / "dist" / sys.platform
if (output / "SkyAgentManager").exists() or (output / "SkyAgentManager.app").exists():
    raise SystemExit("Existing build preserved. Use --output with a new directory.")
subprocess.run(
    [
        sys.executable,
        "-m",
        "PyInstaller",
        str(root / "packaging" / "SkyAgentManager.spec"),
        "--distpath",
        str(output),
        "--workpath",
        str(root / "build" / sys.platform),
    ],
    cwd=root,
    env=environment,
    check=True,
)

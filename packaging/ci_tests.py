"""Bound CI pytest execution and surface stalled tests without hiding failures."""

import argparse
import os
import re
import signal
import subprocess
import sys
from pathlib import Path


def diagnostic(output):
    failures = re.findall(r"^FAILED (tests[/\\][^\r\n]+)", output, re.M)
    if failures:
        return failures[-1][:500]
    active = re.findall(r"^tests[/\\][^\r\n]+", output, re.M)
    message = active[-1][:500] if active else "No active test identified"
    frames = re.findall(r'File "([^"]+)", line (\d+) in ([^\r\n]+)', output)
    relevant = [
        re.split(r"[/\\]", filename)[-1] + ":" + line + " " + function
        for filename, line, function in frames
        if "skyagent_manager" in filename or re.search(r"[/\\]tests[/\\]", filename)
    ]
    return message + ("; stack: " + ", ".join(relevant[-8:]) if relevant else "")


def run(timeout=180):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    root = Path(__file__).resolve().parents[1]
    reports = root / "build"
    reports.mkdir(exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-vv",
        "--junitxml=build/ci-tests.xml",
        "-o",
        "faulthandler_timeout=60",
    ]
    log_path = reports / "ci-tests.log"
    # Windows pipe readers can remain blocked after a parent is killed if a
    # GUI descendant inherited the pipe. A file plus an explicit process-tree
    # kill keeps the timeout bounded and preserves the faulthandler traceback.
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=10)
            code = 124
    with log_path.open("rb") as stream:
        stream.seek(max(0, log_path.stat().st_size - 8 * 1024 * 1024))
        output = stream.read().decode("utf-8", errors="replace")
    if code:
        reason = "timed out" if code == 124 else "failed"
        message = f"Pytest {reason}; last active test: {diagnostic(output)}"
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Desktop tests {reason}::{escaped}")
        # Report every failing test summary, not just the last one, so platform
        # compatibility issues can be fixed together even without log access.
        failures = re.findall(r"^FAILED (tests[/\\][^\r\n]+)", output, re.M)
        for failure in dict.fromkeys(failures):
            escaped = failure[:1000].replace("%", "%25").replace("\r", "%0D")
            print(f"::error title=Pytest failure::{escaped}")
    print("\n".join(line[:1000] for line in output.splitlines()[-200:]))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 1800:
        parser.error("Timeout must be between 60 and 1800 seconds")
    return run(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())

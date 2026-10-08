"""Bound CI pytest execution and surface stalled tests without hiding failures."""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def diagnostic(output):
    failures = re.findall(r"^FAILED (tests[/\\][^\r\n]+)", output, re.M)
    if failures:
        return failures[-1][:500]
    active = re.findall(r"^tests[/\\][^\r\n]+", output, re.M)
    return active[-1][:500] if active else "No active test identified"


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
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        output = result.stdout + result.stderr
        code = result.returncode
    except subprocess.TimeoutExpired as error:

        def decoded(value):
            return (
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else value or ""
            )

        output = decoded(error.stdout) + decoded(error.stderr)
        code = 124
    (reports / "ci-tests.log").write_text(output, encoding="utf-8")
    if code:
        reason = "timed out" if code == 124 else "failed"
        message = f"Pytest {reason}; last active test: {diagnostic(output)}"
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Desktop tests {reason}::{escaped}")
    print(output)
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

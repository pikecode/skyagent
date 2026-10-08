import runpy
import subprocess
from pathlib import Path


def test_ci_stall_diagnostic_identifies_last_test_without_stack_payload():
    module = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "packaging" / "ci_tests.py")
    )
    log = "tests/test_a.py::test_one PASSED [1%]\ntests/test_a.py::test_two\nTimeout!\nprivate response payload\n"
    assert module["diagnostic"](log) == "tests/test_a.py::test_two"
    assert module["diagnostic"]("no test started") == "No active test identified"
    assert (
        module["diagnostic"](
            "tests/test_z.py::test_pass PASSED\nFAILED tests/test_a.py::test_bad - assertion"
        )
        == "tests/test_a.py::test_bad - assertion"
    )
    assert (
        module["diagnostic"]("ERROR tests/test_a.py::test_setup - ValueError")
        == "tests/test_a.py::test_setup - ValueError"
    )


def test_windows_timeout_kills_only_owned_process_tree(tmp_path, monkeypatch, capsys):
    module = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "packaging" / "ci_tests.py")
    )
    module["run"].__globals__["__file__"] = str(tmp_path / "packaging" / "ci_tests.py")
    monkeypatch.setattr(module["sys"], "platform", "win32")
    calls = []

    class Process:
        pid = 12345

        def __init__(self, *args, **kwargs):
            kwargs["stdout"].write(b"tests/test_sample.py::test_stall\n")
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("pytest", timeout)
            return -9

    monkeypatch.setattr(module["subprocess"], "Popen", Process)
    monkeypatch.setattr(
        module["subprocess"], "run", lambda arguments, **kwargs: calls.append(arguments)
    )
    assert module["run"](60) == 124
    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]
    assert "::error title=Desktop tests timed out" in capsys.readouterr().out

import runpy
from pathlib import Path


def test_ci_stall_diagnostic_identifies_last_test_without_stack_payload():
    module = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "packaging" / "ci_tests.py")
    )
    log = "tests/test_a.py::test_one PASSED [1%]\ntests/test_a.py::test_two\nTimeout!\nprivate response payload\n"
    assert module["diagnostic"](log) == "tests/test_a.py::test_two"
    assert module["diagnostic"]("no test started") == "No active test identified"

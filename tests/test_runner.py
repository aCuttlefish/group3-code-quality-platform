import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from fr04.runner import execute_cases, render_test_file, validate_case_arguments, validate_source


SHIPPING = '''def shipping_fee(amount: int) -> int:
    if amount < 0 or amount > 1000:
        raise ValueError("amount must be within 0..1000")
    return 0 if amount > 99 else 10
'''


def case(identifier, value, expected):
    return {"id": identifier, "args": [value], "kwargs": {}, "expected": expected}


class RunnerTests(unittest.TestCase):
    def test_t1_actual_boundary_failure_and_exceptions(self):
        values = [0, 98, 99, 100, 1000, -1, 1001]
        cases = [case(str(v), v, {"kind": "exception", "type": "ValueError"} if v < 0 or v > 1000 else {"kind": "return", "value": 0 if v >= 99 else 10}) for v in values]
        report = execute_cases(SHIPPING, "shipping_fee", cases)
        self.assertEqual(report["status"], "completed")
        self.assertEqual([r["status"] for r in report["results"]], ["passed", "passed", "assertion_failed", "passed", "passed", "passed", "passed"])
        self.assertEqual(report["results"][2]["actual"], {"kind": "return", "value": 10})

    def test_t2_changed_requirement(self):
        report = execute_cases(SHIPPING, "shipping_fee", [case("99", 99, {"kind": "return", "value": 10})])
        self.assertEqual(report["results"][0]["status"], "passed")

    def test_runtime_error_and_wrong_exception(self):
        source = "def divide(x):\n    return 10 / x\n"
        cases = [case("unexpected", 0, {"kind": "return", "value": 0}), case("wrong", 0, {"kind": "exception", "type": "ValueError"})]
        results = execute_cases(source, "divide", cases)["results"]
        self.assertEqual([r["status"] for r in results], ["runtime_error", "assertion_failed"])
        self.assertEqual(results[0]["actual"]["type"], "ZeroDivisionError")

    def test_rejects_unsafe_source(self):
        sources = ["import os\ndef f(x): return x", "@str\ndef f(x): return x",
                   "f(1)\ndef f(x): return x", "def f(x): return x.__class__",
                   "def f(x): return open('secret')", "def f(x): return eval(x)",
                   "def f(x):\n    global y\n    return x", "def f(x): return (lambda: x)()",
                   "def f(x): return '{0.__class__}'.format(x)", "def f(x=1): return x"]
        for source in sources:
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_source(source, "f")

    def test_validation_and_rendering_do_not_run_function(self):
        started = time.monotonic()
        source = "def forever(x):\n    while True:\n        pass\n"
        metadata = validate_source(source, "forever")
        generated = render_test_file("forever", [case("loop", 0, {"kind": "return", "value": 1})])
        self.assertIn("forever(x)", metadata["signature"])
        self.assertIn("self.assertEqual", generated)
        compile(generated, "test_generated.py", "exec")
        self.assertLess(time.monotonic() - started, 1)

    def test_timeout_terminates_and_keeps_remaining_cases(self):
        source = "def forever(x):\n    while True:\n        pass\n"
        cases = [case("first", 0, {"kind": "return", "value": 1}), case("second", 1, {"kind": "return", "value": 1})]
        report = execute_cases(source, "forever", cases, timeout_seconds=0.5)
        self.assertEqual(report["status"], "execution_timeout")
        self.assertEqual([r["status"] for r in report["results"]], ["execution_timeout", "not_run"])
        self.assertLess(report["duration_ms"], 3000)

    def test_signature_binding(self):
        metadata = validate_source("def f(x, /, y, *, z): return x+y+z", "f")
        validate_case_arguments(metadata, [{"id": "valid", "args": [1, 2], "kwargs": {"z": 3}}])
        with self.assertRaises(ValueError):
            validate_case_arguments(metadata, [{"id": "missing", "args": [1], "kwargs": {}}])

    def test_safe_methods_and_fresh_process(self):
        source = "def f(x):\n    x.append(2)\n    return len(x)\n"
        cases = [case("a", [1], {"kind": "return", "value": 2}), case("b", [1], {"kind": "return", "value": 2})]
        self.assertTrue(all(r["status"] == "passed" for r in execute_cases(source, "f", cases)["results"]))

    def test_downloaded_unittest_file_is_executable(self):
        cases = [case("normal", 100, {"kind": "return", "value": 0}), case("exception", -1, {"kind": "exception", "type": "ValueError"})]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject.py").write_text(SHIPPING, encoding="utf-8")
            (root / "test_generated.py").write_text(render_test_file("shipping_fee", cases), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "unittest", "test_generated.py", "-v"], cwd=directory, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_process_launch_failure_is_environment_error(self):
        cases = [case("first", 0, {"kind": "return", "value": 10}), case("second", 1, {"kind": "return", "value": 10})]
        with patch("fr04.runner.subprocess.Popen", side_effect=OSError("test process launch failure")):
            report = execute_cases(SHIPPING, "shipping_fee", cases)
        self.assertEqual(report["status"], "environment_error")
        self.assertEqual([item["status"] for item in report["results"]], ["environment_error", "not_run"])


if __name__ == "__main__":
    unittest.main()

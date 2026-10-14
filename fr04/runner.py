"""Restricted local teaching runner; this is not a production security sandbox.

Validation and test rendering never execute submitted code. Only execute_cases
starts fresh child interpreters, with a shared deadline and a clean environment.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


EXCEPTION_TYPES = {
    "ValueError", "TypeError", "ZeroDivisionError", "OverflowError",
    "IndexError", "KeyError", "RuntimeError", "AssertionError",
    "NotImplementedError", "ArithmeticError", "LookupError",
}
SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "float",
    "int", "isinstance", "len", "list", "max", "min", "pow", "range",
    "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
} | EXCEPTION_TYPES
SAFE_METHODS = {
    "append", "extend", "insert", "pop", "remove", "clear", "copy",
    "count", "index", "reverse", "sort", "get", "keys", "values",
    "items", "update", "setdefault", "add", "discard", "union",
    "intersection", "difference", "issubset", "issuperset",
    "lower", "upper", "strip", "lstrip", "rstrip", "split", "rsplit",
    "join", "replace", "startswith", "endswith", "find", "rfind",
    "isdigit", "isalpha", "isalnum", "isspace", "capitalize", "title",
    "casefold", "partition", "rpartition", "splitlines", "zfill",
}
ALLOWED_NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.If, ast.For,
    ast.While, ast.Break, ast.Continue, ast.Pass, ast.Raise, ast.Assert,
    ast.Try, ast.ExceptHandler, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.SetComp,
    ast.DictComp, ast.comprehension, ast.Subscript, ast.Slice, ast.Attribute,
    ast.Call, ast.keyword, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.IfExp, ast.JoinedStr, ast.FormattedValue, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr,
    ast.BitXor, ast.LShift, ast.RShift, ast.UAdd, ast.USub, ast.Not,
    ast.Invert, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
}


def _is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def validate_source(code: str, function_name: str) -> dict:
    """Parse a single pure-function subset without importing or running it."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("请输入 Python 函数代码。")
    if len(code.splitlines()) > 200 or len(code) > 40_000:
        raise ValueError("首版仅支持不超过 200 行、40000 字符的单文件代码。")
    if not function_name or not function_name.isidentifier() or "__" in function_name:
        raise ValueError("请填写有效的函数名称，名称不能含双下划线。")
    if function_name in SAFE_BUILTINS:
        raise ValueError("函数名称不能与受支持的内置函数或异常同名。")
    try:
        tree = ast.parse(code, filename="subject.py")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError(f"Python 语法错误：{getattr(exc, 'msg', str(exc))}") from None
    statements = [n for n in tree.body if not _is_docstring(n)]
    if len(statements) != 1 or not isinstance(statements[0], ast.FunctionDef):
        raise ValueError("首版仅允许一个独立函数及可选说明字符串；请移除 import、类、顶层赋值和调用。")
    fn = statements[0]
    if fn.name != function_name:
        raise ValueError(f"代码中的函数为 {fn.name}，请检查所选函数名称。")
    if fn.decorator_list:
        raise ValueError("首版不支持装饰器。")
    if fn.args.vararg or fn.args.kwarg:
        raise ValueError("首版不支持 *args 或 **kwargs 参数，请使用明确的参数名称。")
    if fn.args.defaults or any(d is not None for d in fn.args.kw_defaults):
        raise ValueError("首版不支持默认参数，请将所有输入明确写入测试用例。")
    # Annotations are retained for display but must not cause definition-time work.
    annotations = [a.annotation for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]
    if fn.returns:
        annotations.append(fn.returns)
    annotation_nodes = {id(n) for a in annotations if a for n in ast.walk(a)}
    for node in ast.walk(tree):
        if id(node) in annotation_nodes:
            # Future annotations in the trusted worker prevent evaluation.
            if type(node) not in {ast.Name, ast.Constant, ast.Subscript, ast.Tuple, ast.Load, ast.BinOp, ast.BitOr}:
                raise ValueError("类型标注请使用 int、str、list 等简单类型，不支持调用或计算。")
            if isinstance(node, ast.Constant) and node.value is not None and node.value is not Ellipsis and not isinstance(node.value, str):
                raise ValueError("类型标注中仅支持类型名称、字符串、None 或省略号。")
            if isinstance(node, ast.BinOp) and not isinstance(node.op, ast.BitOr):
                raise ValueError("类型标注不支持运算；联合类型可使用 int | None。")
            if isinstance(node, ast.Name) and node.id not in {"int", "float", "str", "bool", "list", "tuple", "dict", "set"}:
                raise ValueError("类型标注仅支持内置数据类型；复杂标注可删除或使用字符串标注。")
            continue
        if type(node) not in ALLOWED_NODES:
            raise ValueError(f"第 {getattr(node, 'lineno', '?')} 行使用了首版不支持的语法：{type(node).__name__}。")
        if isinstance(node, ast.FunctionDef) and node is not fn:
            raise ValueError("首版不支持嵌套函数。")
        if isinstance(node, ast.Name) and "__" in node.id:
            raise ValueError("不允许访问含双下划线的名称。")
        if isinstance(node, ast.arg) and "__" in node.arg:
            raise ValueError("参数名称不能含双下划线。")
        if isinstance(node, ast.Attribute) and node.attr not in SAFE_METHODS:
            raise ValueError(f"第 {node.lineno} 行的属性 {node.attr} 不在受支持的方法列表中。")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in SAFE_BUILTINS | {function_name}:
                    raise ValueError(f"第 {node.lineno} 行不允许调用 {node.func.id}；请仅使用纯函数及受支持的内置函数。")
            elif not isinstance(node.func, ast.Attribute):
                raise ValueError(f"第 {node.lineno} 行不支持动态函数调用。")
            if any(k.arg is None for k in node.keywords):
                raise ValueError("首版不支持 ** 动态展开调用参数。")
        if isinstance(node, ast.comprehension) and node.is_async:
            raise ValueError("首版不支持异步推导式。")
    parameters = []
    for name, items in (("positional_only", fn.args.posonlyargs), ("positional_or_keyword", fn.args.args), ("keyword_only", fn.args.kwonlyargs)):
        for arg in items:
            parameters.append({"name": arg.arg, "kind": name, "annotation": ast.unparse(arg.annotation) if arg.annotation else None})
    return {"function_name": function_name, "parameters": parameters, "signature": f"{function_name}({ast.unparse(fn.args)})", "line_count": len(code.splitlines())}


def validate_case_arguments(metadata: dict, cases: list) -> None:
    kinds = {"positional_only": inspect.Parameter.POSITIONAL_ONLY,
             "positional_or_keyword": inspect.Parameter.POSITIONAL_OR_KEYWORD,
             "keyword_only": inspect.Parameter.KEYWORD_ONLY}
    signature = inspect.Signature([inspect.Parameter(p["name"], kinds[p["kind"]]) for p in metadata["parameters"]])
    for case in cases:
        try:
            signature.bind(*case.get("args", []), **case.get("kwargs", {}))
        except TypeError as exc:
            raise ValueError(f"用例 {case.get('id', '?')} 的参数与函数签名不匹配：{exc}") from None


def render_test_file(function_name: str, cases: list) -> str:
    """Generate a standard-library unittest file. This does not run anything."""
    if not function_name.isidentifier() or "__" in function_name:
        raise ValueError("函数名称无效。")
    lines = ["# 将被测代码保存为 subject.py，与本文件放在同一目录。",
             "# 执行：python -m unittest test_generated.py -v",
             "import unittest", f"from subject import {function_name}", "",
             "", "class GeneratedTests(unittest.TestCase):"]
    for index, case in enumerate(cases, 1):
        exp = case["expected"]
        lines += [f"    def test_{index:02d}(self):", f"        # 用例：{str(case['id']).replace(chr(10), ' ').replace(chr(13), ' ')}",
                  f"        args = {case.get('args', [])!r}", f"        kwargs = {case.get('kwargs', {})!r}"]
        if exp["kind"] == "exception":
            if exp["type"] not in EXCEPTION_TYPES:
                raise ValueError("异常类型不在支持范围内。")
            lines += ["        try:", f"            {function_name}(*args, **kwargs)",
                      "        except Exception as exc:", f"            self.assertEqual(type(exc).__name__, {exp['type']!r})",
                      "        else:", f"            self.fail('预期抛出 {exp['type']}，但函数正常返回')"]
        else:
            lines += [f"        actual = {function_name}(*args, **kwargs)", f"        self.assertEqual(actual, {exp['value']!r})"]
        lines.append("")
    if not cases:
        lines.append("    pass")
    lines += ["", 'if __name__ == "__main__":', "    unittest.main(verbosity=2)", ""]
    return "\n".join(lines)


# Trusted worker code is kept separate from source and runs under -I -S. The
# result is written to a private file, so program output cannot spoof results.
_WORKER = r'''
import builtins
import __future__
import json
import math
import sys
import time

def safe_value(value, depth=0):
    if depth > 10:
        return "<嵌套结果过深，已截断>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() < 4096 else "<整数过大，已省略>"
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:4000] + ("<已截断>" if len(value) > 4000 else "")
    if isinstance(value, (list, tuple)):
        return [safe_value(item, depth + 1) for item in value[:200]] + (["<已截断>"] if len(value) > 200 else [])
    if isinstance(value, dict):
        return {str(key)[:200]: safe_value(item, depth + 1) for index, (key, item) in enumerate(value.items()) if index < 200}
    return "<不支持显示的返回类型：" + type(value).__name__ + ">"

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    request = json.load(handle)
safe_builtins = {name: getattr(builtins, name) for name in request["safe_builtins"]}
namespace = {"__builtins__": safe_builtins}
started = time.monotonic()
try:
    compiled = compile(request["code"], "subject.py", "exec", flags=__future__.annotations.compiler_flag, dont_inherit=True)
    exec(compiled, namespace, namespace)
    result = namespace[request["function_name"]](*request["case"].get("args", []), **request["case"].get("kwargs", {}))
    actual = {"kind": "return", "value": safe_value(result)}
    expected = request["case"]["expected"]
    passed = expected["kind"] == "return" and result == expected["value"]
    status = "passed" if passed else "assertion_failed"
    message = "结果符合预期" if passed else "实际返回值与确认的预期不一致"
except BaseException as exc:
    actual = {"kind": "exception", "type": type(exc).__name__, "message": str(exc)[:4000]}
    expected = request["case"]["expected"]
    if expected["kind"] == "exception":
        passed = type(exc).__name__ == expected["type"]
        status = "passed" if passed else "assertion_failed"
        message = "异常类型符合预期" if passed else "实际异常类型与预期不一致"
    else:
        status = "runtime_error"
        message = "函数抛出了未预期的异常"
payload = {"actual": actual, "status": status, "message": message, "duration_ms": round((time.monotonic() - started) * 1000, 2)}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
'''


def _windows_job(process):
    """Limit child memory and close all job processes when the handle closes."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        return None
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x100 | 0x8  # kill-on-close, memory, process count
    limits.BasicLimitInformation.ActiveProcessLimit = 1
    limits.ProcessMemoryLimit = 256 * 1024 * 1024
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
        kernel.CloseHandle(handle)
        return None
    return lambda: kernel.CloseHandle(handle)


def execute_cases(code: str, function_name: str, cases: list, timeout_seconds: float = 10) -> dict:
    """Execute validated cases under one wall-clock budget in fresh processes.

    The caller must enforce user confirmation before calling this function.
    Exception expectations use exact exception type names, like generated tests.
    """
    started = time.monotonic()
    metadata = validate_source(code, function_name)
    validate_case_arguments(metadata, cases)
    if not isinstance(cases, list) or not 1 <= len(cases) <= 10:
        raise ValueError("每次执行需要 1 至 10 条用例。")
    if timeout_seconds <= 0 or timeout_seconds > 10:
        raise ValueError("执行时间必须大于 0 且不超过 10 秒。")
    for case in cases:
        expected = case.get("expected", {})
        if expected.get("kind") not in ("return", "exception"):
            raise ValueError("用例需要返回值或异常类型预期。")
        if expected["kind"] == "exception" and expected.get("type") not in EXCEPTION_TYPES:
            raise ValueError("异常类型不在支持范围内。")
    deadline = started + timeout_seconds
    report = {"status": "completed", "results": [], "stdout": "", "stderr": "", "duration_ms": 0,
              "isolation": {"mode": "local_restricted_process", "fresh_process_per_case": True,
                            "environment_allowlist": True, "timeout_seconds": timeout_seconds,
                            "windows_job_object": "not_attempted" if os.name == "nt" else "not_applicable",
                            "windows_memory_limit_mib": 256}}
    launched_count = limited_count = 0
    # Never inherit DEEPSEEK_API_KEY, proxies, user profile, or Python path hooks.
    environment = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR"}}
    environment["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="fr04-run-") as directory:
        root = Path(directory)
        worker = root / "worker.py"
        worker.write_text(_WORKER, encoding="utf-8")
        environment.update({"TEMP": directory, "TMP": directory})
        stop = False
        for index, case in enumerate(cases):
            result = {"id": case["id"], "expected": case["expected"], "actual": None, "duration_ms": 0}
            if stop:
                result.update(status="not_run", message="前一用例已超时或执行环境失败，本例未运行")
                report["results"].append(result)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.update(status="execution_timeout", message="本次执行已达到总时间限制")
                report["status"] = "execution_timeout"
                report["results"].append(result)
                stop = True
                continue
            request = root / f"request-{index}.json"
            output = root / f"result-{index}.json"
            request.write_text(json.dumps({"code": code, "function_name": function_name, "case": case, "safe_builtins": sorted(SAFE_BUILTINS)}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            process = None
            close_job = None
            case_started = time.monotonic()
            try:
                process = subprocess.Popen([sys.executable, "-I", "-S", str(worker), str(request), str(output)],
                    cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                launched_count += 1
                close_job = _windows_job(process)
                limited_count += int(close_job is not None)
                result["memory_limit_applied"] = close_job is not None
                stdout, stderr = process.communicate(timeout=max(0.001, deadline - time.monotonic()))
                report["stdout"] = (report["stdout"] + stdout.decode("utf-8", "replace"))[:16000]
                report["stderr"] = (report["stderr"] + stderr.decode("utf-8", "replace"))[:16000]
                if process.returncode != 0 or not output.is_file() or output.stat().st_size > 2_000_000:
                    raise RuntimeError(f"执行子进程未生成有效结果（退出码 {process.returncode}）")
                result.update(json.loads(output.read_text(encoding="utf-8")))
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=1)
                result.update(status="execution_timeout", message=f"已终止超时函数；总时间限制为 {timeout_seconds:g} 秒")
                report["status"] = "execution_timeout"
                stop = True
            except (OSError, RuntimeError, ValueError) as exc:
                result.update(status="environment_error", message=f"执行环境失败：{exc}")
                report["status"] = "environment_error"
                stop = True
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate(timeout=1)
                if close_job:
                    close_job()
            result["duration_ms"] = round((time.monotonic() - case_started) * 1000, 2)
            report["results"].append(result)
    if os.name == "nt" and launched_count:
        report["isolation"]["windows_job_object"] = "applied" if limited_count == launched_count else "partial" if limited_count else "unavailable"
    report["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return report

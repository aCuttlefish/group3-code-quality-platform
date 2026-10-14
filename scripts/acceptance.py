"""Independently verify canonical FR-04 fixtures through the running HTTP API.

Real mode calls the model afresh for every attempt; demo mode never counts as
real-model acceptance. Use --confirm-fixtures to authorize execution only after
this script independently verifies all generated expectations against fixtures.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import SAMPLE_CODE, SAMPLE_SPEC  # No Application/TaskStore is constructed.

REQUIRED_INPUTS = {0, 98, 99, 100, 1000, -1, 1001}


class CriticalViolation(ValueError):
    """An integrity/confirmation violation vetoes the whole acceptance run."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def requirement(scenario):
    return SAMPLE_SPEC if scenario == "S1" else SAMPLE_SPEC.replace("大于等于 99", "严格大于 99")


def payload(scenario, **extra):
    return {"code": SAMPLE_CODE, "spec": requirement(scenario), "function_name": "shipping_fee", "target_count": 7, **extra}


def expectation(value, scenario):
    if value < 0 or value > 1000:
        return {"kind": "exception", "type": "ValueError"}
    free = value >= 99 if scenario == "S1" else value > 99
    return {"kind": "return", "value": 0 if free else 10}


def same_json(left, right):
    return json.dumps(left, ensure_ascii=False, sort_keys=True, allow_nan=False) == json.dumps(right, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fixture_inputs(cases, scenario):
    """Reject wrong expectations, duplicate semantic inputs, or missed boundaries."""
    if not isinstance(cases, list) or not 7 <= len(cases) <= 10:
        raise ValueError("验收需要包含 7 至 10 条用例")
    found, ids = {}, set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in ids:
            raise ValueError("用例编号无效或重复")
        ids.add(case["id"])
        args, kwargs = case.get("args"), case.get("kwargs")
        if isinstance(args, list) and len(args) == 1 and kwargs == {}:
            value = args[0]
        elif args == [] and isinstance(kwargs, dict) and set(kwargs) == {"amount"}:
            value = kwargs["amount"]
        else:
            raise ValueError(f"{case['id']} 必须准确传入一个 amount 参数")
        if type(value) is not int:
            raise ValueError(f"{case['id']} 输入超出本次仅讨论整数的范围")
        if value in found:
            raise ValueError(f"输入 {value} 重复（含位置/关键字不同传参方式）")
        expected = expectation(value, scenario)
        if not same_json(case.get("expected"), expected):
            raise ValueError(f"{case['id']} 输入 {value} 的预期错误：应为 {expected}")
        found[value] = case
    missing = REQUIRED_INPUTS - set(found)
    if missing:
        raise ValueError(f"缺少必测输入：{sorted(missing)}")
    return found


def verify_ready(task, scenario, mode):
    if task.get("code") != SAMPLE_CODE or task.get("spec") != requirement(scenario) or task.get("function_name") != "shipping_fee":
        raise CriticalViolation("输入快照与固定验收样例不一致")
    if task.get("run") is not None or task.get("confirmation") is not None or task.get("execution_started_at"):
        raise CriticalViolation("人工确认前已存在执行或确认记录")
    if task.get("status") != "ready":
        raise ValueError(f"生成状态为 {task.get('status')}：{task.get('error') or task.get('questions')}")
    if not task.get("case_version") or not task.get("test_file"):
        raise ValueError("缺少用例版本或测试文件预览")
    generation = task.get("generation") or {}
    if mode == "real":
        if task.get("generation_source") != "deepseek" or not generation.get("calls"):
            raise ValueError("真实验收必须有 DeepSeek 生成来源和实际调用记录")
    elif task.get("generation_source") != "demo":
        raise ValueError("演示模式生成来源不符")
    return fixture_inputs(task.get("cases"), scenario)


def verify_run(task, scenario, cases_by_value, original_case_version):
    run = task.get("run") or {}
    code_hash = hashlib.sha256(SAMPLE_CODE.encode("utf-8")).hexdigest()
    spec_hash = hashlib.sha256(requirement(scenario).encode("utf-8")).hexdigest()
    if task.get("code") != SAMPLE_CODE or task.get("spec") != requirement(scenario) or task.get("code_hash") != code_hash:
        raise CriticalViolation("执行后的源码/说明快照或源码哈希发生变化")
    if task.get("spec_hash") != spec_hash:
        raise CriticalViolation("说明快照哈希不匹配")
    source_error = str((task.get("error") or {}).get("message", ""))
    if "源码" in source_error and ("完整性" in source_error or "校验" in source_error):
        raise CriticalViolation("服务报告源码完整性异常：" + source_error)
    if run and (run.get("code_hash_before") != code_hash or run.get("code_hash_after") != code_hash or run.get("source_unchanged") is not True):
        raise CriticalViolation("源码哈希或执行前后完整性校验失败")
    if not same_json(task.get("cases"), list(cases_by_value.values())):
        raise CriticalViolation("确认后的用例数据发生变化")
    confirmation = task.get("confirmation") or {}
    if task.get("case_version") != original_case_version or confirmation.get("case_version") != original_case_version:
        raise CriticalViolation("执行或确认所绑定的用例版本发生变化")
    if confirmation.get("code_hash") != code_hash or confirmation.get("spec_hash") != spec_hash:
        raise CriticalViolation("确认未绑定当前代码与说明版本")
    try:
        confirmed_at = datetime.fromisoformat(confirmation["timestamp"])
        started_at = datetime.fromisoformat(task["execution_started_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CriticalViolation("缺少有效确认/执行时间戳") from exc
    if confirmed_at.tzinfo is None or started_at.tzinfo is None or confirmed_at > started_at:
        raise CriticalViolation("确认时间戳无时区或确认事件晚于执行开始")
    if task.get("status") != "completed" or run.get("status") != "completed":
        raise ValueError(f"执行未完成：{task.get('status')} / {task.get('error')}")
    results = run.get("results")
    if not isinstance(results, list) or len(results) != len(cases_by_value):
        raise CriticalViolation("逐条结果数量与确认用例不一致")
    by_id = {row.get("id"): row for row in results if isinstance(row, dict)}
    if len(by_id) != len(cases_by_value):
        raise CriticalViolation("执行结果编号重复或缺失")
    for value, case in cases_by_value.items():
        row = by_id.get(case["id"])
        if not row or not same_json(row.get("expected"), case["expected"]):
            raise CriticalViolation(f"输入 {value} 的执行预期被更改或结果缺失")
        should_fail = scenario == "S1" and value == 99
        wanted_status = "assertion_failed" if should_fail else "passed"
        if row.get("status") != wanted_status:
            raise CriticalViolation(f"输入 {value} 的状态应为 {wanted_status}，实际为 {row.get('status')}")
        # This is an independent oracle for the unchanged, deliberately buggy
        # canonical source: its implementation uses >99 in BOTH scenarios.
        wanted_actual = expectation(value, "S2")
        actual = row.get("actual") or {}
        if wanted_actual["kind"] == "exception":
            actual = {"kind": actual.get("kind"), "type": actual.get("type")}
        if not same_json(actual, wanted_actual):
            raise CriticalViolation(f"输入 {value} 的真实结果与固定源码不一致：{actual}")


class Client:
    def __init__(self, base_url):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("验收脚本仅连接本机 HTTP 服务")
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, timeout=5):
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=max(0.01, timeout)) as response:
                return json.loads(response.read(4_000_001))
        except urllib.error.HTTPError as exc:
            message = exc.read(2000).decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {message}") from None

    def poll(self, task, active_status, started, limit):
        while task.get("status") == active_status:
            remaining = limit - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"{active_status} 超过外部计时上限 {limit} 秒，任务 {task['id']}")
            time.sleep(min(0.2, remaining))
            remaining = limit - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"{active_status} 超过外部计时上限 {limit} 秒，任务 {task['id']}")
            task = self.request("/api/tasks/" + task["id"], timeout=min(3, remaining))
        elapsed = time.monotonic() - started
        if elapsed > limit:
            raise TimeoutError(f"{active_status} 外部耗时 {elapsed:.3f} 秒，超过 {limit} 秒")
        return task, elapsed


def attempt(client, mode, scenario, round_number, confirm_fixtures, retry_of=None):
    record = {"scenario": scenario, "round": round_number, "mode": mode, "started_at": utc_now(), "passed": False, "critical_violation": False, "error": None, "task_id": None}
    try:
        started = time.monotonic()
        if mode == "demo":
            task = client.request("/api/demo-generations", {"scenario": scenario})
        else:
            extra = {"retry_of": retry_of} if retry_of else {}
            task = client.request("/api/generations", payload(scenario, **extra))
        record["task_id"] = task["id"]
        record["task"] = task
        task, elapsed = client.poll(task, "generating", started, 65)
        record.update(task=task, generation_elapsed_seconds=round(elapsed, 3))
        cases = verify_ready(task, scenario, mode)
        if retry_of and mode == "real" and task.get("retry_of") != retry_of:
            raise ValueError("恢复任务未引用原超时任务")
        if not confirm_fixtures:
            print(f"\n{scenario} 第 {round_number} 次，已独立核对以下输入与预期：", flush=True)
            for value, case in sorted(cases.items()):
                print(f"  {value}: {json.dumps(case['expected'], ensure_ascii=False)}", flush=True)
            if input("确认执行这些固定验收用例？输入 yes：").strip().lower() != "yes":
                raise ValueError("未获得执行确认，用例已保留且未执行")
        record["confirmation_basis"] = "--confirm-fixtures + independent fixture validation" if confirm_fixtures else "interactive yes + independent fixture validation"
        case_version = task["case_version"]
        started = time.monotonic()
        task = client.request("/api/tasks/" + task["id"] + "/run", {"confirmed": True, "case_version": case_version})
        task, elapsed = client.poll(task, "running", started, 12)
        record.update(task=task, execution_elapsed_seconds=round(elapsed, 3))
        verify_run(task, scenario, cases, case_version)
        record["passed"] = True
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, EOFError) as exc:
        record["error"] = str(exc)
        record["critical_violation"] = isinstance(exc, CriticalViolation)
    record["finished_at"] = utc_now()
    print(f"{scenario} #{round_number}: {'PASS' if record['passed'] else 'FAIL'} | task={record['task_id'] or '-'}" + (f" | {record['error']}" if record["error"] else ""), flush=True)
    return record


def fault_attempt(client):
    record = {"scenario": "T3_timeout", "started_at": utc_now(), "passed": False, "critical_violation": False, "task_id": None, "error": None}
    try:
        started = time.monotonic()
        task = client.request("/api/generations", payload("S1", fault_timeout=True))
        record.update(task_id=task["id"], task=task)
        task, elapsed = client.poll(task, "generating", started, 65)
        record.update(task=task, generation_elapsed_seconds=round(elapsed, 3))
        if task.get("code") != SAMPLE_CODE or task.get("spec") != SAMPLE_SPEC:
            raise CriticalViolation("超时后未完整保留原输入")
        if task.get("confirmation") is not None or task.get("run") is not None or task.get("execution_started_at") or task.get("cases"):
            raise CriticalViolation("超时任务不应有确认、执行记录或可运行用例")
        if task.get("status") != "generation_timeout" or task.get("generation_source") != "fault_injection":
            raise ValueError("故障注入未明确报告 generation_timeout")
        if (task.get("error") or {}).get("type") != "injected_timeout":
            raise ValueError("故障注入类型不符")
        if elapsed < 59:
            raise ValueError("故障注入未等待完整 60 秒生成预算")
        record["passed"] = True
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        record["error"] = str(exc)
        record["critical_violation"] = isinstance(exc, CriticalViolation)
    record["finished_at"] = utc_now()
    print(f"T3 timeout: {'PASS' if record['passed'] else 'FAIL'} | task={record['task_id'] or '-'}" + (f" | {record['error']}" if record["error"] else ""), flush=True)
    return record


def verify_timeout_preserved(client, timeout_record):
    record = {"scenario": "T3_timeout_preservation", "task_id": timeout_record.get("task_id"), "passed": False, "critical_violation": False, "error": None}
    try:
        original = timeout_record.get("task")
        if not original or original.get("status") != "generation_timeout":
            raise ValueError("缺少已完成的超时记录，无法核对恢复后的留存情况")
        current = client.request("/api/tasks/" + original["id"])
        record["task_after_recovery"] = current
        # Compare the entire JSON record, including status/source/input/cases,
        # confirmation/run, error details, timestamps, and generation evidence.
        if not same_json(current, original):
            raise CriticalViolation("恢复后原超时任务记录被改写；历史状态、输入与执行证据必须保持不变")
        record["passed"] = True
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        record["error"] = str(exc)
        record["critical_violation"] = isinstance(exc, CriticalViolation)
    record["checked_at"] = utc_now()
    print(f"T3 original record: {'PASS' if record['passed'] else 'FAIL'}" + (f" | {record['error']}" if record["error"] else ""), flush=True)
    return record


def aggregate_results(attempts, rounds, mode, fault_requested=False, fault=None, recovery=None, preservation=None):
    """Apply the frequency threshold only after the non-negotiable veto check."""
    records = list(attempts) + [record for record in (fault, recovery, preservation) if record]
    critical = [{"scenario": record.get("scenario"), "task_id": record.get("task_id"), "error": record.get("error")} for record in records if record.get("critical_violation")]
    counts = {scenario: sum(bool(record.get("passed")) and not record.get("critical_violation", False) for record in attempts if record.get("scenario") == scenario) for scenario in ("S1", "S2")}
    threshold = 2 if rounds == 3 else 1
    scenarios_pass = all(count >= threshold for count in counts.values())
    t3_pass = not fault_requested or all(record and record.get("passed") for record in (fault, recovery, preservation))
    overall = bool(scenarios_pass and t3_pass and not critical)
    result = {
        "validation_scope": "model_and_flow_only; execution_isolation=local_teaching_process",
        "success_counts": counts, "required_success_per_scenario": threshold,
        "critical_violations": critical, "critical_veto": bool(critical),
        "overall_pass": overall,
        "formal_accepted": mode == "real" and rounds == 3 and fault_requested and overall,
    }
    if fault_requested:
        result["t3_real_accepted"] = bool(mode == "real" and t3_pass and not critical)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("demo", "real"), default="demo", help="real 调用 DeepSeek；demo 仅验证演示闭环")
    parser.add_argument("--rounds", type=int, choices=(1, 3), default=1, help="1=冒烟检查；3=每个场景三次正式频次")
    parser.add_argument("--confirm-fixtures", action="store_true", help="授权在独立核对固定验收用例后执行；未指定则逐次交互确认")
    parser.add_argument("--fault", action="store_true", help="额外执行实际等待 60 秒的超时注入与恢复")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    try:
        client = Client(args.base_url)
        config = client.request("/api/config")
        if args.mode == "real" and not config.get("configured"):
            print("真实验收未开始：请在项目 .env 配置 DEEPSEEK_API_KEY 并保存，然后重试。", file=sys.stderr)
            return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"无法连接服务：{exc}；请先运行 python app.py。", file=sys.stderr)
        return 2
    directory = ROOT / "data" / "acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + f"_{args.mode}.json")
    summary = {"started_at": utc_now(), "mode": args.mode, "rounds": args.rounds, "fault_requested": args.fault, "model": config.get("model"), "validation_scope": "model_and_flow_only; execution_isolation=local_teaching_process", "attempts": [], "formal_accepted": False, "overall_pass": False}

    def save():
        destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    save()
    print(f"模式：{args.mode}；每场景 {args.rounds} 次；{'固定样例确认已授权' if args.confirm_fixtures else '逐次交互确认'}。", flush=True)
    if args.mode == "demo":
        print("演示结果不计入真实模型验收。", flush=True)
    for scenario in ("S1", "S2"):
        for number in range(1, args.rounds + 1):
            summary["attempts"].append(attempt(client, args.mode, scenario, number, args.confirm_fixtures))
            save()
    if args.fault:
        failed_task = fault_attempt(client)
        summary["fault"] = failed_task
        save()
        if failed_task["task_id"]:
            recovery = attempt(client, args.mode, "S1", "T3_recovery", args.confirm_fixtures, retry_of=failed_task["task_id"])
        else:
            recovery = {"passed": False, "error": "未创建超时任务，无法恢复"}
        summary["recovery"] = recovery
        summary["timeout_preservation"] = verify_timeout_preserved(client, failed_task)
        if args.mode == "demo":
            summary["t3_note"] = "超时注入与演示恢复仅验证流程；演示端点不绑定 retry_of，未验证真实 AI 恢复。"
        save()
    summary.update(aggregate_results(summary["attempts"], args.rounds, args.mode, args.fault, summary.get("fault"), summary.get("recovery"), summary.get("timeout_preservation")))
    counts = summary["success_counts"]
    summary["finished_at"] = utc_now()
    save()
    print(f"S1={counts['S1']}/{args.rounds}；S2={counts['S2']}/{args.rounds}；本次检查 {'PASS' if summary['overall_pass'] else 'FAIL'}。", flush=True)
    if summary["critical_veto"]:
        print("存在确认、源码完整性或结果真实性的关键违规，本次验收整体否决。", flush=True)
    print("真实模型与流程判据：" + ("通过；执行隔离仍为本地教学版" if summary["formal_accepted"] else "未完成或未通过（需 real、3 轮、--fault，且满足判据）；执行隔离仍为本地教学版"), flush=True)
    print(f"完整记录：{destination}", flush=True)
    return 0 if summary["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

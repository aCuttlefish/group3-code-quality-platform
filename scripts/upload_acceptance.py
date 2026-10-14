"""Check real AI generation from an uploaded file with two requirement sources."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.acceptance import Client, CriticalViolation, expectation, fixture_inputs, same_json, utc_now

EXAMPLE_FILE = ROOT / "examples" / "shipping_fee_comments.py"
MANUAL_SPEC = """输入 amount 仅讨论整数，合法金额为 0 至 1000，包含两个端点。
金额严格大于 99 时运费为 0，否则运费为 10。
金额小于 0 或大于 1000 时抛出 ValueError。
非整数输入不在本次讨论范围内。"""


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def without_rule_numbers(text):
    """Allow the backend to add R1/R2 labels; keep all requirement words."""
    value = re.sub(r"(?im)(^|\n)\s*R\d+\s*[:：]\s*", r"\1", text)
    return " ".join(value.split())


def check_source_evidence(task, inspected, source):
    if task.get("code") != inspected["code"] or task.get("filename") != inspected["filename"]:
        raise CriticalViolation("上传后的代码或文件名未保留")
    if task.get("spec_source") != source:
        raise CriticalViolation("任务说明来源与选择不一致")
    if task.get("manual_spec") != MANUAL_SPEC:
        raise CriticalViolation("未保留未经覆盖的手写说明原文")
    if task.get("comments_text") != inspected["comments_text"]:
        raise CriticalViolation("任务注释证据与上传提取结果不一致")
    source_text = inspected["comments_text"] if source == "comments" else MANUAL_SPEC
    if not isinstance(task.get("spec"), str) or without_rule_numbers(task["spec"]) != without_rule_numbers(source_text):
        raise CriticalViolation("实际生成依据没有来自选定来源，或规范化时改变了说明内容")
    if task.get("code_hash") != digest(inspected["code"]) or task.get("spec_hash") != digest(task["spec"]):
        raise CriticalViolation("代码或实际说明快照哈希不一致")
    materials = {key: task[key] for key in ("spec_source", "manual_spec", "filename", "comments_text")}
    material_hash = digest(json.dumps(materials, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if task.get("material_hash") != material_hash:
        raise CriticalViolation("上传文件名、来源及原始说明的材料哈希不一致")


def check_generation(task, inspected, source):
    check_source_evidence(task, inspected, source)
    if task.get("confirmation") is not None or task.get("run") is not None or task.get("execution_started_at"):
        raise CriticalViolation("确认之前已有执行或确认记录")
    if task.get("status") != "ready":
        raise ValueError(f"生成未就绪：{task.get('status')}；{task.get('error') or task.get('questions')}")
    generation = task.get("generation") or {}
    if task.get("generation_source") != "deepseek" or not generation.get("calls"):
        raise ValueError("缺少真实 DeepSeek 调用证据")
    if not task.get("case_version") or not task.get("test_file"):
        raise ValueError("缺少待确认的用例版本或测试文件")
    first_request = generation["calls"][0].get("request") or {}
    messages = first_request.get("messages") or []
    if len(messages) < 2:
        raise ValueError("模型调用记录缺少提示词")
    input_record = json.loads(messages[1]["content"])
    if input_record.get("requirements") != task["spec"]:
        raise CriticalViolation("模型收到的实际说明与任务快照不一致")
    if "code" in input_record or inspected["code"] in messages[1]["content"]:
        raise CriticalViolation("生成提示词包含完整实现，未遵守按说明生成的接口约定")
    return fixture_inputs(task.get("cases"), "S1" if source == "comments" else "S2")


def check_run(task, ready, inspected, source, cases):
    check_source_evidence(task, inspected, source)
    run = task.get("run") or {}
    confirmation = task.get("confirmation") or {}
    for key in ("case_version", "code_hash", "spec_hash", "material_hash"):
        if task.get(key) != ready.get(key) or confirmation.get(key) != ready.get(key):
            raise CriticalViolation("确认或执行未绑定原始版本：" + key)
    if not same_json(task.get("cases"), ready.get("cases")):
        raise CriticalViolation("确认后的用例被改写")
    try:
        confirmed = datetime.fromisoformat(confirmation["timestamp"])
        started = datetime.fromisoformat(task["execution_started_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CriticalViolation("确认或执行时间戳缺失") from exc
    if confirmed.tzinfo is None or started.tzinfo is None or confirmed > started:
        raise CriticalViolation("执行开始早于确认或时间戳缺少时区")
    if task.get("status") != "completed" or run.get("status") != "completed":
        raise ValueError(f"执行未完成：{task.get('status')}；{task.get('error')}")
    if run.get("source_unchanged") is not True or run.get("code_hash_before") != ready["code_hash"] or run.get("code_hash_after") != ready["code_hash"]:
        raise CriticalViolation("执行前后上传源码发生变化")
    results = run.get("results")
    if not isinstance(results, list) or len(results) != len(cases):
        raise CriticalViolation("实际结果数量与确认用例不符")
    by_id = {row.get("id"): row for row in results}
    if len(by_id) != len(cases):
        raise CriticalViolation("结果编号重复")
    for value, case in cases.items():
        row = by_id.get(case["id"])
        if not row or not same_json(row.get("expected"), case["expected"]):
            raise CriticalViolation("实际执行改变了预期或遗漏结果")
        expected_status = "assertion_failed" if source == "comments" and value == 99 else "passed"
        if row.get("status") != expected_status:
            raise CriticalViolation(f"输入 {value} 应为 {expected_status}，实际为 {row.get('status')}")
        actual = row.get("actual") or {}
        expected_actual = expectation(value, "S2")
        if expected_actual["kind"] == "exception":
            actual = {"kind": actual.get("kind"), "type": actual.get("type")}
        if not same_json(actual, expected_actual):
            raise CriticalViolation(f"输入 {value} 的真实结果不符合上传的固定源码")


def run_mode(client, inspected, source, confirm_fixtures):
    record = {"source": source, "started_at": utc_now(), "task_id": None, "passed": False, "critical_violation": False, "error": None}
    try:
        started = time.monotonic()
        task = client.request("/api/generations", {
            "code": inspected["code"], "filename": inspected["filename"],
            "spec": MANUAL_SPEC, "spec_source": source,
            "function_name": "shipping_fee", "target_count": 7,
        })
        record.update(task_id=task["id"], task=task)
        task, elapsed = client.poll(task, "generating", started, 65)
        record.update(task=task, generation_elapsed_seconds=round(elapsed, 3))
        cases = check_generation(task, inspected, source)
        ready = task
        record["expected_at_99"] = cases[99]["expected"]["value"]
        if not confirm_fixtures:
            print(f"\n来源 {source}：已独立核对 7 条边界用例，99 的预期为 {record['expected_at_99']}。", flush=True)
            if input("确认执行这些固定验收用例？输入 yes：").strip().lower() != "yes":
                raise ValueError("未确认执行；生成任务已保留")
        record["confirmation_basis"] = "--confirm-fixtures + independent oracle" if confirm_fixtures else "interactive yes + independent oracle"
        started = time.monotonic()
        task = client.request(f"/api/tasks/{task['id']}/run", {"confirmed": True, "case_version": ready["case_version"]})
        task, elapsed = client.poll(task, "running", started, 12)
        record.update(task=task, execution_elapsed_seconds=round(elapsed, 3))
        check_run(task, ready, inspected, source, cases)
        record["passed"] = True
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, EOFError) as exc:
        record["error"] = str(exc)
        record["critical_violation"] = isinstance(exc, CriticalViolation)
    record["finished_at"] = utc_now()
    print(f"{source}: {'PASS' if record['passed'] else 'FAIL'} | task={record['task_id'] or '-'}" + (f" | {record['error']}" if record["error"] else ""), flush=True)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--confirm-fixtures", action="store_true", help="授权在独立核对固定预期后运行；未指定则逐次交互确认")
    args = parser.parse_args(argv)
    client = Client(args.base_url)
    folder = ROOT / "data" / "acceptance"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_upload_sources_real.json")
    summary = {"started_at": utc_now(), "validation_scope": "real_model_upload_and_requirement_sources; execution_isolation=local_teaching_process", "attempts": [], "overall_pass": False, "error": None}

    def save():
        destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    save()
    try:
        config = client.request("/api/config")
        if not config.get("configured"):
            raise ValueError("未配置 DEEPSEEK_API_KEY，未发起生成任务")
        raw = EXAMPLE_FILE.read_bytes()
        inspected = client.request("/api/source/inspect", {"filename": EXAMPLE_FILE.name, "content_base64": base64.b64encode(raw).decode("ascii")})
        summary["inspection"] = inspected
        if inspected.get("filename") != EXAMPLE_FILE.name or inspected.get("code") != raw.decode("utf-8-sig"):
            raise CriticalViolation("上传解析后的文件名或源码与原文件不一致")
        comments = inspected.get("comments_text")
        if not isinstance(comments, str) or not all(part in comments for part in ("大于等于 99", "0 至 1000", "ValueError")):
            raise ValueError("未完整提取样例 docstring 的需求说明")
        save()
        for source in ("comments", "manual"):
            summary["attempts"].append(run_mode(client, inspected, source, args.confirm_fixtures))
            save()
        first, second = summary["attempts"]
        if first["passed"] and second["passed"]:
            if first["task_id"] == second["task_id"] or first["task"]["code_hash"] != second["task"]["code_hash"]:
                raise CriticalViolation("两种来源未创建不同任务，或使用了不同源码")
            if first["task"]["spec_hash"] == second["task"]["spec_hash"] or first["task"]["case_version"] == second["task"]["case_version"]:
                raise CriticalViolation("切换说明来源后说明/用例版本没有变化")
            if first["expected_at_99"] != 0 or second["expected_at_99"] != 10:
                raise CriticalViolation("切换来源后阈值 99 的预期没有按说明变化")
            old = client.request(f"/api/tasks/{first['task_id']}")
            if not same_json(old, first["task"]):
                raise CriticalViolation("创建手写来源任务后，原注释来源任务记录被改写")
            summary["cross_source_checks"] = {"same_uploaded_code": True, "different_task_and_spec_and_case_versions": True, "expected_at_99": {"comments": 0, "manual": 10}, "original_comments_task_unchanged": True}
            summary["overall_pass"] = True
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        summary["error"] = str(exc)
        summary["critical_violation"] = isinstance(exc, CriticalViolation)
        print(f"FAIL: {exc}", flush=True)
    summary["critical_veto"] = bool(summary.get("critical_violation") or any(record.get("critical_violation") for record in summary["attempts"]))
    summary["overall_pass"] = bool(summary["overall_pass"] and not summary["critical_veto"])
    summary["finished_at"] = utc_now()
    save()
    print(f"上传与说明来源真实验收：{'PASS' if summary['overall_pass'] else 'FAIL'}；执行隔离仍为本地教学版。", flush=True)
    print(f"记录：{destination}", flush=True)
    return 0 if summary["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

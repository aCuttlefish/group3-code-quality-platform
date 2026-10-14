"""Local FR-04 web application. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import uuid

from fr04 import generator, runner, materials

ROOT = Path(__file__).resolve().parent
ACTIVE_STATES = {"generating", "running"}
ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")

SAMPLE_CODE = '''def shipping_fee(amount):
    if amount < 0 or amount > 1000:
        raise ValueError("amount out of range")
    return 0 if amount > 99 else 10
'''
SAMPLE_SPEC = '''R1：输入 amount 仅讨论整数，合法金额为 0 至 1000，包含端点。
R2：金额大于等于 99 时运费为 0，否则为 10。
R3：金额小于 0 或大于 1000 时抛出 ValueError。非整数输入不在本次范围内。'''


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def digest(value) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, kind: str = "invalid_input"):
        super().__init__(message)
        self.status = status
        self.kind = kind


class TaskStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        # A restart never invents a successful result for an interrupted task.
        for path in self.directory.glob("*/task.json"):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
                if task.get("status") in ACTIVE_STATES:
                    was_running = task["status"] == "running"
                    task["status"] = "environment_error" if was_running else "generation_failed"
                    task["error"] = {"type": "interrupted", "message": "服务重启中断了任务；原输入和记录已保留，请重试。"}
                    task["finished_at"] = now()
                    self.save(task)
            except (ValueError, OSError, KeyError):
                continue

    def path(self, task_id: str) -> Path:
        if not ID_PATTERN.fullmatch(task_id):
            raise ApiError("任务不存在。", 404, "not_found")
        return self.directory / task_id

    def load(self, task_id: str) -> dict:
        with self.lock:
            try:
                return json.loads((self.path(task_id) / "task.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                raise ApiError("任务不存在或记录损坏。", 404, "not_found") from None

    def save(self, task: dict) -> None:
        with self.lock:
            directory = self.path(task["id"])
            directory.mkdir(exist_ok=True)
            temp = directory / "task.json.tmp"
            temp.write_text(json.dumps(task, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            os.replace(temp, directory / "task.json")

    def create(self, task: dict) -> None:
        with self.lock:
            directory = self.path(task["id"])
            directory.mkdir(exist_ok=False)
            (directory / "subject.py").write_bytes(task["code"].encode("utf-8"))
            (directory / "spec.txt").write_bytes(task["spec"].encode("utf-8"))
            self.save(task)

    def list(self) -> list[dict]:
        items = []
        with self.lock:
            for path in self.directory.glob("*/task.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    items.append({k: item.get(k) for k in (
                        "id", "status", "created_at", "finished_at", "generation_source",
                        "function_name", "spec_hash", "code_hash", "error", "retry_of", "filename", "spec_source",
                    )})
                except (ValueError, OSError):
                    continue
        return sorted(items, key=lambda t: t["created_at"], reverse=True)[:100]


class Application:
    def __init__(self, data_dir: Path | None = None, *, generate_fn=None, execute_fn=None, config_fn=None):
        self.store = TaskStore(data_dir or ROOT / "data" / "tasks")
        self.generate_fn = generate_fn or generator.generate_cases
        self.execute_fn = execute_fn or runner.execute_cases
        self.config_fn = config_fn or (lambda: generator.load_config(ROOT))
        self.work_lock = threading.Lock()
        self.threads: set[threading.Thread] = set()

    def config(self) -> dict:
        config = self.config_fn()
        return {
            "configured": bool(config.get("api_key")),
            "model": config.get("model", "deepseek-flash"),
            "base_url": config.get("base_url", "https://api.deepseek.com"),
            "runner": "本地受限子进程，仅限自有课程样例；非生产安全沙箱",
            "active": self.work_lock.locked(),
        }

    def inspect_source(self, payload: dict) -> dict:
        try:
            filename = payload.get("filename") or None
            if "content_base64" in payload:
                if "code" in payload:
                    raise ValueError("上传内容与代码文本不能同时提交。")
                code = materials.decode_upload(filename, payload["content_base64"])
            else:
                code = payload.get("code", "")
            return materials.inspect_source(code, filename)
        except ValueError as exc:
            raise ApiError(str(exc)) from None

    def validate_input(self, payload: dict) -> dict:
        code, spec, name = (payload.get(k, "") for k in ("code", "spec", "function_name"))
        if not all(isinstance(value, str) for value in (code, spec, name)):
            raise ApiError("代码、说明和函数名必须为文本。")
        spec_source = payload.get("spec_source", "manual")
        if spec_source not in ("manual", "comments"):
            raise ApiError("说明来源必须为手写说明或文件注释。")
        if len(spec) > 2000:
            raise ApiError("手写功能说明最多 2000 个字符。")
        if not name.isidentifier():
            raise ApiError("请输入合法的 Python 函数名。")
        count = payload.get("target_count", 7)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            raise ApiError("用例数必须为 1～10 的整数。")
        fault = payload.get("fault_timeout", False)
        if not isinstance(fault, bool):
            raise ApiError("故障模拟选项必须为布尔值。")
        try:
            inspected = materials.inspect_source(code, payload.get("filename") or None)
            if name != inspected["function_name"]:
                raise ValueError("目标函数名与文件中的独立函数不一致。")
            raw_spec = spec if spec_source == "manual" else inspected["comments_text"]
            if spec_source == "comments" and not raw_spec.strip():
                raise ValueError("文件中没有可用的注释或 docstring，请补充注释或改用手写功能说明。")
            effective_spec = materials.normalize_spec(raw_spec)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        retry_of = payload.get("retry_of")
        if retry_of is not None:
            previous = self.store.load(retry_of)
            legacy_materials = "manual_spec" not in previous and "spec_source" not in previous
            if (previous["code"] != code or (not legacy_materials and previous["spec"] != effective_spec)
                    or previous["function_name"] != name
                    or previous.get("manual_spec", previous["spec"]) != spec
                    or previous.get("spec_source", "manual") != spec_source
                    or (previous.get("filename") or None) != inspected["filename"]):
                raise ApiError("重试必须引用原输入；修改输入后请创建新的生成任务。")
        return {
            "code": code, "spec": effective_spec, "manual_spec": spec, "spec_source": spec_source,
            "filename": inspected["filename"], "comments_text": inspected["comments_text"],
            "comment_items": inspected["comment_items"], "function_name": name, "target_count": count,
            "fault_timeout": fault, "metadata": inspected["metadata"], "retry_of": retry_of,
        }

    def _new_task(self, values: dict, source: str) -> dict:
        return {
            "id": uuid.uuid4().hex, "status": "generating", "created_at": now(),
            "generation_source": source, **values,
            "code_hash": digest(values["code"]), "spec_hash": digest(values["spec"]),
            "material_hash": digest({key: values[key] for key in ("spec_source", "manual_spec", "filename", "comments_text")}),
            "cases": [], "case_version": None, "test_file": "", "questions": [],
            "error": None, "generation": None, "confirmation": None, "run": None,
        }

    def _start_worker(self, callback, *args):
        def work():
            try:
                callback(*args)
            finally:
                self.work_lock.release()
                self.threads.discard(threading.current_thread())
        thread = threading.Thread(target=work, daemon=True)
        self.threads.add(thread)
        thread.start()

    def generate(self, payload: dict) -> dict:
        values = self.validate_input(payload)
        config = self.config_fn()
        if not config.get("api_key") and not values["fault_timeout"]:
            raise ApiError("请设置用户环境变量 DEEPSEEK_API_KEY 或填写项目 .env，也可使用明确标注的演示流程。", 400, "missing_api_key")
        if not self.work_lock.acquire(blocking=False):
            raise ApiError("已有任务正在处理，请等待完成后再提交。", 409, "busy")
        try:
            task = self._new_task(values, "fault_injection" if values["fault_timeout"] else "deepseek")
            self.store.create(task)
            self._start_worker(self._generate_worker, task["id"], config)
        except Exception:
            self.work_lock.release()
            raise
        return task

    def _generate_worker(self, task_id: str, config: dict):
        task = self.store.load(task_id)
        started = time.monotonic()
        try:
            result = self.generate_fn(
                task["code"], task["function_name"], task["spec"], task["target_count"],
                config, timeout_seconds=60, fault_timeout=task["fault_timeout"],
            )
            task["generation"] = result
            task["status"] = result["status"]
            task["questions"] = result.get("questions", [])
            task["error"] = result.get("error")
            if result["status"] == "ready":
                self._prepare_cases(task, result["cases"])
        except ValueError as exc:
            task["status"] = "validation_failed"
            task["error"] = {"type": "validation_error", "message": str(exc)[:500]}
        except Exception as exc:
            task["status"] = "generation_failed"
            safe_message = str(exc).replace(config.get("api_key") or "\0", "[REDACTED]")[:500]
            task["error"] = {"type": "internal_error", "message": "生成流程失败：" + safe_message}
        task["generation_finished_at"] = now()
        task["generation_duration_ms"] = round((time.monotonic() - started) * 1000)
        self.store.save(task)

    def _prepare_cases(self, task: dict, cases: list):
        # The same canonical data is previewed, hashed, rendered and executed.
        cases = generator.validate_cases(cases, task["function_name"], task["spec"], task["target_count"])
        if hasattr(runner, "validate_case_arguments"):
            runner.validate_case_arguments(task["metadata"], cases)
        task["cases"] = cases
        task["case_version"] = digest({
            "code_hash": task["code_hash"], "spec_hash": task["spec_hash"],
            "material_hash": task["material_hash"],
            "function_name": task["function_name"], "cases": cases,
        })
        task["test_file"] = runner.render_test_file(task["function_name"], cases)
        (self.store.path(task["id"]) / "test_generated.py").write_text(task["test_file"], encoding="utf-8")
        task["status"] = "ready"

    def demo(self, scenario: str) -> dict:
        if scenario not in {"S1", "S2"}:
            raise ApiError("演示场景必须为 S1 或 S2。")
        spec = SAMPLE_SPEC if scenario == "S1" else SAMPLE_SPEC.replace("大于等于 99", "严格大于 99")
        values = self.validate_input({"code": SAMPLE_CODE, "spec": spec, "function_name": "shipping_fee", "target_count": 7})
        task = self._new_task(values, "demo")
        cases = []
        for i, value in enumerate([0, 98, 99, 100, 1000, -1, 1001], 1):
            exception = value < 0 or value > 1000
            expected = {"kind": "exception", "type": "ValueError"} if exception else {
                "kind": "return", "value": 0 if value >= (99 if scenario == "S1" else 100) else 10,
            }
            cases.append({
                "id": f"TC{i:02}", "scenario": "exception" if exception else "boundary",
                "args": [value], "kwargs": {}, "expected": expected,
                "rule_ids": ["R3"] if exception else ["R1", "R2"],
                "reason": "固定验收样例；由演示数据提供，未调用模型。",
            })
        self.store.create(task)
        self._prepare_cases(task, cases)
        task["generation"] = {"status": "ready", "calls": [], "model": None, "prompt_version": None,
                              "note": "固定演示数据，不计入真实模型验收。"}
        task["generation_finished_at"] = now()
        self.store.save(task)
        return task

    def run(self, task_id: str, payload: dict) -> dict:
        with self.store.lock:
            task = self.store.load(task_id)
            if task["status"] != "ready":
                raise ApiError("仅可执行尚未运行的已生成用例。请重新生成以创建新任务。", 409, "invalid_state")
            if payload.get("confirmed") is not True:
                raise ApiError("请先核对用例并明确确认执行。", 400, "confirmation_required")
            if payload.get("case_version") != task["case_version"]:
                raise ApiError("用例版本已变化，请重新核对并确认。", 409, "version_mismatch")
            if not self.work_lock.acquire(blocking=False):
                raise ApiError("已有任务正在处理，请稍后再试。", 409, "busy")
            try:
                task["confirmation"] = {
                    "timestamp": now(), "case_version": task["case_version"],
                    "code_hash": task["code_hash"], "spec_hash": task["spec_hash"],
                    "material_hash": task.get("material_hash"),
                    "event": "user_confirmed_run",
                }
                task["status"] = "running"
                self.store.save(task)  # Durable confirmation always precedes execution.
                self._start_worker(self._run_worker, task_id)
            except Exception:
                self.work_lock.release()
                raise
        return task

    def _run_worker(self, task_id: str):
        task = self.store.load(task_id)
        start = time.monotonic()
        task["execution_started_at"] = now()
        try:
            original_path = self.store.path(task_id) / "subject.py"
            before_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
            if before_hash != task["code_hash"]:
                raise ValueError("源码快照校验失败，已停止执行。")
            result = self.execute_fn(task["code"], task["function_name"], task["cases"], timeout_seconds=10)
            result["code_hash_before"] = before_hash
            result["code_hash_after"] = hashlib.sha256(original_path.read_bytes()).hexdigest()
            result["source_unchanged"] = before_hash == result["code_hash_after"]
            if not result["source_unchanged"]:
                raise ValueError("源码完整性校验失败。")
            task["run"] = result
            task["status"] = result["status"]
            if result["status"] == "completed":
                failed = any(row["status"] != "passed" for row in result["results"])
                task["summary"] = "测试执行完成，存在不符合当前说明的结果" if failed else "测试执行完成，全部通过"
            else:
                task["summary"] = "执行超时，已终止" if result["status"] == "execution_timeout" else "执行环境失败"
        except Exception as exc:
            task["status"] = "environment_error"
            task["error"] = {"type": "execution_error", "message": str(exc)[:500]}
        task["finished_at"] = now()
        task["execution_duration_ms"] = round((time.monotonic() - start) * 1000)
        self.store.save(task)


class Handler(BaseHTTPRequestHandler):
    server_version = "FR04/1.0"

    @property
    def app(self) -> Application:
        return self.server.app

    def log_message(self, format, *args):
        # Do not print request bodies, source code or any credentials.
        return

    def _send(self, status, body, content_type="application/json; charset=utf-8", filename=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _validate_origin(self):
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            raise ApiError("仅允许本机访问。", 403, "origin_rejected")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
            raise ApiError("不允许跨站请求。", 403, "origin_rejected")

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise ApiError("请使用 application/json 请求。", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError("无效请求长度。") from None
        if not 0 < length <= 200000:
            raise ApiError("请求为空或超过大小限制。", 413)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ApiError("请求不是有效 JSON。") from None
        if not isinstance(value, dict):
            raise ApiError("请求必须为 JSON 对象。")
        return value

    def _dispatch(self, post=False):
        try:
            self._validate_origin()
            path = urlparse(self.path).path
            if post:
                body = self._body()
                if path == "/api/source/inspect":
                    return self._send(200, self.app.inspect_source(body))
                if path == "/api/generations":
                    return self._send(202, self.app.generate(body))
                if path == "/api/demo-generations":
                    return self._send(201, self.app.demo(body.get("scenario", "S1")))
                match = re.fullmatch(r"/api/tasks/([a-f0-9]{32})/run", path)
                if match:
                    return self._send(202, self.app.run(match[1], body))
            else:
                if path == "/api/config":
                    return self._send(200, self.app.config())
                if path == "/api/tasks":
                    return self._send(200, {"tasks": self.app.store.list()})
                match = re.fullmatch(r"/api/tasks/([a-f0-9]{32})(?:/(report|test-file))?", path)
                if match:
                    task = self.app.store.load(match[1])
                    if match[2] == "report":
                        return self._send(200, task, filename=f"fr04-{task['id'][:8]}.json")
                    if match[2] == "test-file":
                        if not task["test_file"]:
                            raise ApiError("尚未生成测试文件。", 409)
                        return self._send(200, task["test_file"], "text/x-python; charset=utf-8", "test_generated.py")
                    return self._send(200, task)
                static = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path in static:
                    name, mime = static[path]
                    return self._send(200, (ROOT / "static" / name).read_bytes(), mime)
                if path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
            raise ApiError("资源不存在。", 404, "not_found")
        except ApiError as exc:
            self._send(exc.status, {"error": {"type": exc.kind, "message": str(exc)}})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self._send(500, {"error": {"type": "internal_error", "message": "服务内部错误，请检查本地运行环境后重试。"}})

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch(post=True)


def create_server(port=8765, app=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.app = app or Application()
    return server


def main():
    parser = argparse.ArgumentParser(description="FR-04 local test generation demo")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.port)
    print(f"FR-04 ready: http://127.0.0.1:{server.server_port}", flush=True)
    print("Local classroom samples only. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

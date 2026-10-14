"""Application-level invariants, independent of live DeepSeek availability."""
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.executions = []
        self.generated = []
        self.application = app.Application(
            Path(self.temp.name), generate_fn=self.generate_fake,
            execute_fn=self.execute_fake,
            config_fn=lambda: {"api_key": "test-secret-not-real", "base_url": "https://api.deepseek.com", "model": "test-model"},
        )

    def tearDown(self):
        for worker in list(self.application.threads):
            worker.join(5)
        self.temp.cleanup()

    def execute_fake(self, code, name, cases, **kwargs):
        self.executions.append((code, name, cases))
        records = [self.application.store.load(item["id"]) for item in self.application.store.list()]
        self.assertTrue(any(record.get("confirmation") for record in records))
        return {"status": "completed", "results": [
            {"id": case["id"], "status": "passed", "expected": case["expected"],
             "actual": case["expected"], "duration_ms": 1, "message": "test stub"} for case in cases
        ], "stdout": "", "stderr": "", "duration_ms": 1}

    def generate_fake(self, code, function_name, spec, target_count, config, **kwargs):
        self.generated.append(spec)
        if kwargs.get("fault_timeout"):
            return {"status": "generation_timeout", "cases": [], "calls": [],
                    "error": {"type": "generation_timeout", "message": "模拟超时"}}
        fixture = self.application.demo("S1")
        return {"status": "ready", "cases": fixture["cases"], "calls": [], "error": None}

    def wait(self, task_id):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            task = self.application.store.load(task_id)
            if task["status"] not in app.ACTIVE_STATES:
                return task
            time.sleep(0.01)
        self.fail("worker did not finish")

    def test_no_execution_without_confirmation_or_current_version(self):
        task = self.application.demo("S1")
        self.assertEqual(self.executions, [])
        for payload in ({"case_version": task["case_version"]}, {"confirmed": True, "case_version": "old"}):
            with self.assertRaises(app.ApiError):
                self.application.run(task["id"], payload)
        self.assertEqual(self.executions, [])
        self.assertIsNone(self.application.store.load(task["id"])["confirmation"])
        self.application.run(task["id"], {"confirmed": True, "case_version": task["case_version"]})
        completed = self.wait(task["id"])
        self.assertEqual(len(self.executions), 1)
        self.assertLessEqual(completed["confirmation"]["timestamp"], completed["execution_started_at"])
        self.assertTrue(completed["run"]["source_unchanged"])
        with self.assertRaises(app.ApiError):
            self.application.run(task["id"], {"confirmed": True, "case_version": task["case_version"]})

    def test_changed_spec_has_new_version_and_preserves_old(self):
        s1 = self.application.demo("S1")
        s2 = self.application.demo("S2")
        self.assertEqual(s1["code_hash"], s2["code_hash"])
        self.assertNotEqual(s1["spec_hash"], s2["spec_hash"])
        self.assertNotEqual(s1["case_version"], s2["case_version"])
        case1 = next(c for c in s1["cases"] if c["args"] == [99])
        case2 = next(c for c in s2["cases"] if c["args"] == [99])
        self.assertEqual(case1["expected"]["value"], 0)
        self.assertEqual(case2["expected"]["value"], 10)
        self.assertEqual(self.application.store.load(s1["id"]), s1)

    def test_retry_preserves_failed_task_and_never_executes(self):
        values = {"code": app.SAMPLE_CODE, "spec": app.SAMPLE_SPEC, "function_name": "shipping_fee", "target_count": 7, "fault_timeout": True}
        first = self.wait(self.application.generate(values)["id"])
        self.assertEqual(first["status"], "generation_timeout")
        values["fault_timeout"] = False
        values["retry_of"] = first["id"]
        second = self.wait(self.application.generate(values)["id"])
        self.assertEqual(second["status"], "ready")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.application.store.load(first["id"]), first)
        self.assertEqual(self.executions, [])
        self.assertIsNone(second["confirmation"])

    def test_modified_source_snapshot_cannot_execute(self):
        task = self.application.demo("S1")
        source = self.application.store.path(task["id"]) / "subject.py"
        source.write_text("changed", encoding="utf-8")
        self.application.run(task["id"], {"confirmed": True, "case_version": task["case_version"]})
        result = self.wait(task["id"])
        self.assertEqual(result["status"], "environment_error")
        self.assertEqual(self.executions, [])

    def test_restart_marks_active_task_interrupted(self):
        task = self.application.demo("S1")
        task["status"] = "generating"
        self.application.store.save(task)
        restarted = app.TaskStore(Path(self.temp.name))
        saved = restarted.load(task["id"])
        self.assertEqual(saved["status"], "generation_failed")
        self.assertEqual(saved["code"], app.SAMPLE_CODE)
        self.assertIsNone(saved["run"])

    def test_config_never_returns_key(self):
        self.assertNotIn("test-secret-not-real", json.dumps(self.application.config()))

    def test_selected_material_is_normalized_without_mixing_sources(self):
        code = 'def identity(value):\n    """返回输入的整数，不改变数值。"""\n    return value\n'
        payload = {"code": code, "spec": "所有整数输入都应返回 0。", "function_name": "identity",
                   "filename": "identity.py", "spec_source": "comments", "comments_text": "伪造注释"}
        comments = self.application.validate_input(payload)
        self.assertEqual(comments["spec"], "R1：返回输入的整数，不改变数值。")
        self.assertEqual(comments["manual_spec"], payload["spec"])
        self.assertNotIn("伪造", comments["comments_text"])
        payload["spec_source"] = "manual"
        manual = self.application.validate_input(payload)
        self.assertEqual(manual["spec"], "R1：所有整数输入都应返回 0。")
        self.assertNotIn("不改变数值", manual["spec"])
        self.assertNotEqual(self.application._new_task(comments, "deepseek")["material_hash"],
                            self.application._new_task(manual, "deepseek")["material_hash"])

    def test_comments_required_and_source_changes_cannot_reuse_retry(self):
        payload = {"code": app.SAMPLE_CODE, "spec": app.SAMPLE_SPEC, "function_name": "shipping_fee",
                   "spec_source": "comments"}
        with self.assertRaisesRegex(app.ApiError, "没有可用"):
            self.application.validate_input(payload)
        payload["spec_source"] = "unknown"
        with self.assertRaises(app.ApiError):
            self.application.validate_input(payload)
        payload["spec_source"] = "manual"
        task = self.application.demo("S1")
        payload.update(retry_of=task["id"], filename="different.py")
        with self.assertRaisesRegex(app.ApiError, "重试必须"):
            self.application.validate_input(payload)

    def test_comments_generation_preserves_material_and_waits_for_confirmation(self):
        def generate(code, name, spec, count, config, **options):
            self.generated.append(spec)
            return {"status": "ready", "cases": [{"id": "TC01", "scenario": "normal", "args": [5],
                "kwargs": {}, "expected": {"kind": "return", "value": 5}, "rule_ids": ["R1"],
                "reason": "返回原输入"}], "calls": [], "error": None}
        self.application.generate_fn = generate
        code = 'def identity(value):\n    # 返回输入的整数，不改变数值。\n    return value\n'
        payload = {"code": code, "function_name": "identity", "spec_source": "comments",
                   "spec": "这是未选中的手写说明。", "filename": "identity.py", "target_count": 1}
        task = self.wait(self.application.generate(payload)["id"])
        self.assertEqual(task["status"], "ready")
        self.assertEqual(self.generated, ["R1：返回输入的整数，不改变数值。"])
        self.assertEqual(task["manual_spec"], payload["spec"])
        self.assertEqual(task["comment_items"][0]["line"], 2)
        self.assertEqual(self.executions, [])
        self.assertIsNone(task["confirmation"])
        self.assertIsNone(task["run"])
        self.application.run(task["id"], {"confirmed": True, "case_version": task["case_version"]})
        completed = self.wait(task["id"])
        self.assertEqual(completed["confirmation"]["material_hash"], task["material_hash"])
        self.assertEqual(completed["status"], "completed")

    def test_legacy_retry_preserves_raw_input_and_normalizes_new_task_only(self):
        task = self.application.demo("S1")
        task["spec"] = "  " + app.SAMPLE_SPEC + "\n"
        for key in ("manual_spec", "spec_source", "filename", "comments_text", "comment_items", "material_hash"):
            task.pop(key, None)
        self.application.store.save(task)
        payload = {"code": task["code"], "spec": task["spec"], "function_name": task["function_name"], "retry_of": task["id"]}
        values = self.application.validate_input(payload)
        self.assertEqual(values["manual_spec"], task["spec"])
        self.assertEqual(values["spec"], app.SAMPLE_SPEC)
        self.assertEqual(self.application.store.load(task["id"]), task)
        payload["spec"] = app.SAMPLE_SPEC
        with self.assertRaises(app.ApiError):
            self.application.validate_input(payload)

    def test_uploaded_python_identifiers_match_generation_validation(self):
        for name in ("_identity", "计算"):
            code = f'def {name}(value):\n    return value\n'
            inspected = self.application.inspect_source({"code": code})
            values = self.application.validate_input({"code": code, "function_name": inspected["function_name"], "spec": "返回原值。"})
            self.assertEqual(values["function_name"], name)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        application = app.Application(Path(self.temp.name), config_fn=lambda: {"api_key": "", "model": "test-model"})
        self.server = app.create_server(0, application)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_cross_origin_mutation_rejected(self):
        req = Request(self.base + "/api/demo-generations", data=b'{"scenario":"S1"}', headers={
            "Content-Type": "application/json", "Origin": "https://foreign.example",
        })
        with self.assertRaises(HTTPError) as caught:
            urlopen(req, timeout=3)
        self.assertEqual(caught.exception.code, 403)

    def test_page_static_assets_resolve(self):
        with urlopen(self.base + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
        self.assertIn('src="/app.js"', html)
        self.assertIn('href="/style.css"', html)
        for path, content_type in (("/app.js", "text/javascript"), ("/style.css", "text/css")):
            with urlopen(self.base + path, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers["Content-Type"])
                self.assertGreater(len(response.read()), 100)

    def test_upload_inspects_without_saving_or_executing_and_rejects_invalid_files(self):
        code = 'def identity(value):\n    """返回原值。"""\n    return value\n'
        body = {"filename": "identity.py", "content_base64": base64.b64encode(code.encode()).decode()}
        request = Request(self.base + "/api/source/inspect", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            inspected = json.load(response)
        self.assertEqual(inspected["code"], code)
        self.assertEqual(inspected["function_name"], "identity")
        self.assertEqual(inspected["comments_text"], "返回原值。")
        self.assertEqual(self.server.app.store.list(), [])
        for replacement in ({"filename": "not-python.txt"}, {"content_base64": "invalid!"}):
            invalid = {**body, **replacement}
            request = Request(self.base + "/api/source/inspect", data=json.dumps(invalid).encode(),
                              headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 400)

    def test_real_generation_requires_key_and_demo_is_labeled(self):
        values = {"code": app.SAMPLE_CODE, "spec": app.SAMPLE_SPEC, "function_name": "shipping_fee"}
        req = Request(self.base + "/api/generations", data=json.dumps(values).encode(), headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(req, timeout=3)
        self.assertEqual(caught.exception.code, 400)
        req = Request(self.base + "/api/demo-generations", data=b'{"scenario":"S1"}', headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=3) as response:
            task = json.load(response)
        self.assertEqual(task["generation_source"], "demo")
        self.assertIsNone(task["run"])
        with urlopen(self.base + "/api/tasks/" + task["id"] + "/report", timeout=3) as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(json.load(response)["id"], task["id"])


if __name__ == "__main__":
    unittest.main()

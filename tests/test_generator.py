import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from fr04.generator import PROMPT_VERSION, _messages, _user_environment_value, generate_cases, load_config, validate_cases


SPEC = "R1: 输入整数 x 范围为 0 到 10。\nR2: 返回 x 的两倍。\nR3: 超出范围抛出 ValueError。"
CODE = 'def double(x: int) -> int:\n    "SECRET_IMPLEMENTATION_SENTINEL"\n    return x * 3\n'
CONFIG = {"api_key": "test-private-key", "base_url": "https://api.deepseek.com", "model": "test-model"}


def case(value=0):
    return {"id": "TC01", "scenario": "boundary", "args": [value], "kwargs": {}, "expected": {"kind": "return", "value": value * 2}, "rule_ids": ["R1", "R2"], "reason": "依据 R2 返回输入的两倍"}


def response(data):
    return {"raw": json.dumps({"model": "test-model", "choices": [{"message": {"content": json.dumps(data, ensure_ascii=False)}, "finish_reason": "stop"}], "usage": {"total_tokens": 123}}, ensure_ascii=False), "http_status": 200}


class GeneratorTests(unittest.TestCase):
    def generate(self, **kwargs):
        values = dict(code=CODE, function_name="double", spec=SPEC, target_count=1, config=CONFIG, timeout_seconds=1)
        values.update(kwargs)
        return generate_cases(**values)

    def test_signature_only_and_audited_response(self):
        with patch("fr04.generator._http_post", return_value=response({"status": "ready", "cases": [case()]})) as call:
            result = self.generate()
        self.assertEqual(result["status"], "ready")
        body = call.call_args.args[1]
        prompt = json.dumps(body, ensure_ascii=False)
        self.assertIn("def double(x: int) -> int", prompt)
        self.assertNotIn("SECRET_IMPLEMENTATION_SENTINEL", prompt)
        self.assertNotIn("return x * 3", prompt)
        self.assertNotIn(CONFIG["api_key"], json.dumps(result))
        self.assertEqual(result["calls"][0]["usage"]["total_tokens"], 123)
        self.assertEqual(result["calls"][0]["request"]["response_format"], {"type": "json_object"})
        self.assertEqual(result["calls"][0]["request"]["thinking"], {"type": "disabled"})

    def test_invalid_arguments_are_repaired_before_ready(self):
        invalid = case()
        invalid["args"] = []
        with patch("fr04.generator._http_post", side_effect=[response({"status": "ready", "cases": [invalid]}), response({"status": "ready", "cases": [case()]})]) as call:
            result = self.generate()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(call.call_count, 2)
        self.assertIn("参数与函数签名不匹配", result["calls"][0]["validation_error"])
        with patch("fr04.generator._http_post", return_value=response({"status": "ready", "cases": [invalid]})):
            result = self.generate()
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["cases"], [])

    def test_prompt_preserves_original_threshold_neighbors_without_fixture_answers(self):
        prompt = _messages("def calculate(value)", "R1: 整数范围为 2 到 20。R2: 输入严格大于 8 时返回 4，否则返回 3。", 7)
        instruction = prompt[0]["content"]
        self.assertEqual(PROMPT_VERSION, "fr04-data-v2")
        self.assertIn("t-1、t、t+1", instruction)
        self.assertIn("L-1、U+1", instruction)
        self.assertIn("禁止将 >t 归一化成 >=t+1", instruction)
        self.assertIn("取并集并去重", instruction)
        self.assertIn("不得为增加 normal 标签而牺牲边界点", instruction)
        for fixture_detail in ("shipping_fee", "运费", "98", "99", "1000", "1001"):
            self.assertNotIn(fixture_detail, json.dumps(prompt, ensure_ascii=False))

    def test_one_repair_uses_validation_error_and_shared_deadline(self):
        invalid = case()
        invalid["rule_ids"] = ["R99"]
        with patch("fr04.generator._http_post", side_effect=[response({"status": "ready", "cases": [invalid]}), response({"status": "ready", "cases": [case()]})]) as call:
            result = self.generate()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["calls"]), 2)
        self.assertEqual(result["calls"][1]["kind"], "repair")
        self.assertIn("不存在", call.call_args_list[1].args[1]["messages"][-1]["content"])
        self.assertEqual(call.call_args_list[0].args[2], call.call_args_list[1].args[2])
        self.assertEqual(len(result["calls"][0]["request"]["messages"]), 2)

    def test_two_bad_outputs_stop(self):
        with patch("fr04.generator._http_post", return_value=response({"status": "ready", "cases": []})) as call:
            result = self.generate()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["cases"], [])

    def test_failure_never_silently_returns_demo_and_redacts_error(self):
        with patch("fr04.generator._http_post", side_effect=RuntimeError("failure " + CONFIG["api_key"])) as call:
            result = self.generate()
        self.assertEqual(result["status"], "generation_failed")
        self.assertEqual(result["cases"], [])
        self.assertEqual(call.call_count, 1)
        self.assertNotIn(CONFIG["api_key"], json.dumps(result))

    def test_missing_key_never_calls_provider(self):
        with patch("fr04.generator._http_post") as call:
            result = self.generate(config={**CONFIG, "api_key": ""})
        self.assertEqual(result["error"]["type"], "missing_api_key")
        call.assert_not_called()

    def test_needs_spec_preserves_questions(self):
        with patch("fr04.generator._http_post", return_value=response({"status": "needs_spec", "questions": ["越界时返回还是抛出异常？"], "cases": []})):
            result = self.generate()
        self.assertEqual(result["status"], "needs_spec")
        self.assertEqual(result["questions"], ["越界时返回还是抛出异常？"])
        self.assertEqual(result["cases"], [])

    def test_unnumbered_spec_requests_rules_without_model(self):
        with patch("fr04.generator._http_post") as call:
            result = self.generate(spec="返回输入的两倍")
        self.assertEqual(result["status"], "needs_spec")
        call.assert_not_called()

    def test_rule_and_duplicate_validation(self):
        invalid = case()
        invalid["rule_ids"] = ["R4"]
        with self.assertRaisesRegex(ValueError, "不存在"):
            validate_cases([invalid], "double", SPEC)
        duplicate = {**case(), "id": "TC02"}
        with self.assertRaisesRegex(ValueError, "输入重复"):
            validate_cases([case(), duplicate], "double", SPEC)
        invalid = case()
        invalid["expected"] = {"kind": "return", "value": float("nan")}
        with self.assertRaisesRegex(ValueError, "NaN"):
            validate_cases([invalid], "double", SPEC)
        invalid["expected"] = {"kind": "exception", "type": ["ValueError"]}
        with self.assertRaises(ValueError):
            validate_cases([invalid], "double", SPEC)

    def test_env_reload_and_nonempty_process_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "process-key", "DEEPSEEK_MODEL": "", "DEEPSEEK_BASE_URL": ""}, clear=False), patch("fr04.generator._user_environment_value", return_value=""):
            path = Path(directory)
            (path / ".env").write_text('DEEPSEEK_API_KEY="file-key"\nDEEPSEEK_MODEL=test-one\n', encoding="utf-8")
            self.assertEqual(load_config(path)["api_key"], "process-key")
            self.assertEqual(load_config(path)["model"], "test-one")
            (path / ".env").write_text("DEEPSEEK_MODEL=test-two # trailing comment\n", encoding="utf-8")
            self.assertEqual(load_config(path)["model"], "test-two")

    def test_empty_env_file_key_uses_user_registry_and_precedence(self):
        user_values = {"DEEPSEEK_API_KEY": "user-key", "DEEPSEEK_BASE_URL": "https://user.example", "DEEPSEEK_MODEL": "user-model"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, dict.fromkeys(user_values, ""), clear=False), patch("fr04.generator._user_environment_value", side_effect=user_values.get) as read_user:
            path = Path(directory)
            (path / ".env").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config, {"api_key": "user-key", "base_url": "https://user.example", "model": "user-model"})
            self.assertEqual({call.args[0] for call in read_user.call_args_list}, set(user_values))
            (path / ".env").write_text("DEEPSEEK_API_KEY=file-key\nDEEPSEEK_BASE_URL=https://file.example\nDEEPSEEK_MODEL=file-model\n", encoding="utf-8")
            read_user.reset_mock()
            self.assertEqual(load_config(path)["api_key"], "file-key")
            read_user.assert_not_called()
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "process-key"}):
                self.assertEqual(load_config(path)["api_key"], "process-key")
            read_user.assert_not_called()

    def test_registry_reads_exact_allowed_name_and_missing_is_safe(self):
        handle = MagicMock()
        fake_registry = types.SimpleNamespace(HKEY_CURRENT_USER=1, KEY_READ=2, OpenKey=MagicMock(return_value=handle), QueryValueEx=MagicMock(return_value=(" user-key ", 1)))
        with patch.dict("sys.modules", {"winreg": fake_registry}):
            self.assertEqual(_user_environment_value("DEEPSEEK_API_KEY"), "user-key")
            fake_registry.OpenKey.assert_called_once_with(1, "Environment", 0, 2)
            fake_registry.QueryValueEx.assert_called_once_with(handle.__enter__.return_value, "DEEPSEEK_API_KEY")
            self.assertEqual(_user_environment_value("UNRELATED_SECRET"), "")
            self.assertEqual(fake_registry.QueryValueEx.call_count, 1)
            fake_registry.QueryValueEx.side_effect = FileNotFoundError()
            self.assertEqual(_user_environment_value("DEEPSEEK_MODEL"), "")
        with patch.dict("sys.modules", {"winreg": None}):
            self.assertEqual(_user_environment_value("DEEPSEEK_API_KEY"), "")

    def test_injected_timeout_is_bounded_and_explicit(self):
        start = time.monotonic()
        result = self.generate(timeout_seconds=0.04, fault_timeout=True)
        self.assertEqual(result["status"], "generation_timeout")
        self.assertEqual(result["error"]["type"], "injected_timeout")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(result["calls"], [])

    def test_stalled_http_body_cannot_exceed_total_deadline(self):
        received = []

        class SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                # A slow response body is inside the same generation deadline.
                for unused in range(40):
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except (OSError, ConnectionError):
                        break
                    time.sleep(0.015)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        server_thread.start()
        try:
            config = {**CONFIG, "base_url": f"http://127.0.0.1:{server.server_port}"}
            start = time.monotonic()
            result = self.generate(config=config, timeout_seconds=0.08)
            duration = time.monotonic() - start
            self.assertEqual(result["status"], "generation_timeout")
            self.assertLess(duration, 0.4)
            self.assertEqual(len(received), 1)
            self.assertEqual(len(result["calls"]), 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

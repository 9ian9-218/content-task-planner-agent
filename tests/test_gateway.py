"""Compatibility checks for the user-approved gateway; no external requests."""
import io
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from content_planner.__main__ import main
from content_planner.model import LiveModel
from content_planner.tools import definitions
from content_planner import Planner
from tests.provider_fixture import respond
from tests.test_acceptance import AS_OF, BRIEF


class GatewayTests(unittest.TestCase):
    def test_approved_gateway_is_the_default_and_uses_chat_completions_tools(self):
        environment = {key: value for key, value in os.environ.items() if key not in ["MODEL_BASE_URL", "MODEL_NAME"]}
        seen = []

        def transport(request, timeout):
            seen.append((request.full_url, json.loads(request.data), request.get_header("Authorization")))
            response = {"model": "claude-opus-4.6", "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"status": "needs_input", "reason_code": "MISSING_INFORMATION", "summary": "受控协议测试。"})}}]}
            return io.BytesIO(json.dumps(response).encode())

        with patch.dict(os.environ, environment, clear=True), patch("urllib.request.urlopen", side_effect=transport):
            decision = LiveModel(api_key="test-only-key").decide(
                [{"role": "system", "content": "protocol check"}, {"role": "user", "content": "test"}], definitions(), timeout=1)
        url, payload, authorization = seen[0]
        self.assertEqual(url, "http://100.73.185.103:8045/v1/chat/completions")
        self.assertEqual(payload["model"], "claude-opus-4.6")
        self.assertEqual(len(payload["tools"]), 5)
        self.assertNotIn("thinking", payload)
        self.assertEqual(authorization, "Bearer test-only-key")
        self.assertNotIn("test-only-key", json.dumps(payload))
        self.assertEqual(decision["model"], "claude-opus-4.6")

    def test_cli_loads_gateway_config_without_key_and_records_port_without_credentials(self):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ["MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"]}
        for endpoint in ["http://100.73.185.103:8045/v1",
                         "http://operator:removed@100.73.185.103:8045/v1?credential=removed#secret"]:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / ".env.local"
                config.write_text(f"MODEL_API_KEY=\nMODEL_BASE_URL={endpoint}\nMODEL_NAME=claude-opus-4.6\n", encoding="utf-8")
                output = root / "result"
                capture = io.StringIO()
                source = Path(__file__).resolve().parents[1] / "examples/cases/C01-normal/input.json"
                with patch.dict(os.environ, environment, clear=True), patch("urllib.request.urlopen") as transport, redirect_stdout(capture):
                    code = main(["run", "--input", str(source), "--mode", "live", "--as-of", "2026-11-01T10:00:00+08:00",
                                 "--db", str(root / "state.sqlite3"), "--output-dir", str(output), "--env-file", str(config)])
                result = json.loads(capture.getvalue())
                manifest_text = (output / "manifest.json").read_text(encoding="utf-8")
                manifest = json.loads(manifest_text)
                self.assertEqual(code, 3)
                self.assertEqual(result["reason_code"], "MODEL_UNAVAILABLE")
                self.assertEqual(result["model_requests_sent"], 0)
                transport.assert_not_called()
                self.assertEqual(manifest["model_name"], "claude-opus-4.6")
                self.assertEqual(manifest["model_endpoint"], "http://100.73.185.103:8045/v1")
                self.assertNotIn("operator", manifest_text)
                self.assertNotIn("removed", manifest_text)
                self.assertNotIn("secret", manifest_text)

    def test_gateway_failure_retains_http_status_retry_hint_and_paid_results_without_secrets(self):
        from urllib.error import HTTPError
        for status, header, expected in [(503, "26", 26), (429, "10", 10), (401, None, None), (503, "invalid", None)]:
            with self.subTest(status=status, retry_after=header), tempfile.TemporaryDirectory() as directory:
                requests = []

                def transport(request, timeout):
                    requests.append(request)
                    if len(requests) == 4:
                        raise HTTPError(request.full_url, status, "test-only-key must stay private",
                                        {} if header is None else {"Retry-After": header}, io.BytesIO(b'test-only-key'))
                    return respond(request, timeout)

                planner = Planner(Path(directory) / "state.sqlite3", as_of=AS_OF)
                try:
                    with patch("urllib.request.urlopen", side_effect=transport):
                        result = planner.run(BRIEF, LiveModel(api_key="test-only-key"))
                    self.assertEqual(result["status"], "incomplete")
                    self.assertEqual(result["reason_code"], "MODEL_UNAVAILABLE")
                    self.assertEqual(result["model_error"]["http_status"], status)
                    self.assertEqual(result["model_error"]["retry_after_seconds"], expected)
                    self.assertEqual(result["model_error"]["retryable"], status in [429, 503])
                    self.assertFalse(result["model_error"]["outcome_unknown"])
                    self.assertEqual(result["cost"]["spent_units"], 8)
                    self.assertEqual(len(result["scripts"]), 1)
                    self.assertEqual(planner.list_plans(), [])
                    self.assertEqual(result["model_requests_sent"], 4)
                    self.assertNotIn("test-only-key", json.dumps(result))
                finally:
                    planner.close()

    def test_finish_contract_distinguishes_missing_materials_from_infeasible_constraints(self):
        from content_planner.model import ModelError
        cases = [("incomplete", "BUDGET_EXCEEDED", True), ("incomplete", "DEADLINE_EXCEEDED", True),
                 ("needs_input", "MATERIALS_MISSING", True), ("needs_input", "BUDGET_EXCEEDED", False),
                 ("incomplete", "budget_exceeded", False), ("needs_input", "NO_MATERIALS_FOUND", False)]
        for status, reason, valid in cases:
            with self.subTest(status=status, reason=reason):
                value = {"status": status, "reason_code": reason, "summary": "依据实际报价或检索结果停止。"}
                call = {"id": "contract-finish", "type": "function", "function": {"name": "finish_task", "arguments": json.dumps(value)}}
                response = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}]}
                with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
                    model = LiveModel(api_key="test-only-key")
                    if valid:
                        self.assertEqual(model.decide([], definitions())["finish"], value)
                    else:
                        with self.assertRaises(ModelError):
                            model.decide([], definitions())

    def test_excessively_nested_model_json_cannot_crash_or_poison_the_request(self):
        nested = "[" * 2000 + "0" + "]" * 2000
        malformed_call = {"id": "nested", "type": "function", "function": {"name": "search_materials", "arguments": nested}}
        valid_envelope = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [malformed_call]}}]}).encode()
        stopped = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"status": "incomplete", "reason_code": "INVALID_ARGUMENTS", "summary": "参数无法安全解析，停止。"})}}]}
        for raw, expected in [(nested.encode(), "MODEL_RESPONSE_INVALID"), (valid_envelope, "INVALID_ARGUMENTS")]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                planner = Planner(Path(directory) / "state.sqlite3", as_of=AS_OF)
                try:
                    with patch("urllib.request.urlopen", side_effect=[io.BytesIO(raw), io.BytesIO(json.dumps(stopped).encode())]):
                        result = planner.run(BRIEF, LiveModel(api_key="test-only-key"))
                    self.assertEqual(result["status"], "incomplete")
                    self.assertEqual(result["reason_code"], expected)
                    self.assertEqual(planner.list_plans(), [])
                    repeated = planner.run(BRIEF, LiveModel(api_key=""))
                    self.assertEqual(repeated["status"], "incomplete")
                    self.assertNotEqual(repeated["reason_code"], "REQUEST_IN_PROGRESS")
                finally:
                    planner.close()

    def test_model_can_end_through_a_native_finish_action_with_an_explicit_reason(self):
        finish = {"status": "incomplete", "reason_code": "SAVE_FAILED", "summary": "保存连续失败，保留已有脚本与费用。"}
        call = {"id": "end-1", "type": "function", "function": {"name": "finish_task", "arguments": json.dumps(finish)}}
        response = {"model": "native-finish-fixture", "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}]}
        captured = []

        def transport(request, timeout):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps(response).encode())

        with patch("urllib.request.urlopen", side_effect=transport):
            decision = LiveModel(api_key="test-only-key").decide([{"role": "user", "content": "无法保存，请明确结束。"}], definitions())
        self.assertEqual(decision["finish"], finish)
        self.assertNotIn("tool_calls", decision)
        names = [item["function"]["name"] for item in captured[0]["tools"]]
        self.assertEqual(names, ["search_materials", "estimate_cost", "generate_script", "save_plan", "finish_task"])

    def test_legacy_text_finish_obeys_the_same_state_and_reason_contract(self):
        from content_planner.model import ModelError
        for value in [{"status": "completed", "reason_code": "SAVE_FAILED", "summary": "未保存不能完成。"},
                      {"status": "needs_input", "reason_code": "BUDGET_EXCEEDED", "summary": "预算不可行。"},
                      {"status": "incomplete", "reason_code": "budget_exceeded", "summary": "非标准错误码。"},
                      {"status": "incomplete"}]:
            with self.subTest(value=value):
                response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
                with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
                    with self.assertRaises(ModelError):
                        LiveModel(api_key="test-only-key").decide([], definitions())

    def test_invalid_or_mixed_finish_actions_cannot_execute_tools_or_claim_saved(self):
        finish = {"status": "incomplete", "reason_code": "SAVE_FAILED", "summary": "保存失败。"}
        payloads = [dict(finish, status="completed"), dict(finish, status=[]), dict(finish, reason_code=""),
                    dict(finish, summary=None), dict(finish, extra="unvalidated"), [], None]
        for value in payloads + ["mixed"]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                call = {"id": "finish-invalid", "type": "function", "function": {"name": "finish_task", "arguments": json.dumps(finish if value == "mixed" else value)}}
                calls = [call]
                if value == "mixed":
                    calls.append({"id": "must-not-run", "type": "function", "function": {"name": "search_materials", "arguments": json.dumps({"query": "P01", "decision_summary": "不应执行。"})}})
                response = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": calls}}]}
                planner = Planner(Path(directory) / "state.sqlite3", as_of=AS_OF)
                try:
                    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
                        result = planner.run(BRIEF, LiveModel(api_key="test-only-key"))
                    self.assertEqual(result["status"], "incomplete")
                    self.assertEqual(result["reason_code"], "MODEL_RESPONSE_INVALID")
                    self.assertEqual(result["tool_attempts"], 0)
                    self.assertEqual(result["cost"]["spent_units"], 0)
                    self.assertEqual(planner.list_plans(), [])
                finally:
                    planner.close()

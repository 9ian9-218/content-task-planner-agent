from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from content_planner import Planner
from content_planner.model import LiveModel
from tests.test_acceptance import AS_OF, BRIEF


class ModelTests(unittest.TestCase):
    def test_live_without_credentials_returns_incomplete_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, model=LiveModel(api_key=""))
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["reason_code"], "MODEL_UNAVAILABLE")
            self.assertEqual(result["model_mode"], "live")
            self.assertEqual(result["steps"], [])
            self.assertEqual(planner.list_plans(), [])

    def test_live_adapter_round_trips_actual_tool_observations_over_http(self):
        calls = [{"id": "call-1", "type": "function", "function": {"name": "search_materials", "arguments": '{"query":"P01","decision_summary":"查资料"}'}}]
        first = {"model": "transport-fixture", "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": calls, "reasoning_content": "must not be returned"}}], "usage": {"total_tokens": 20}}
        second = {"model": "transport-fixture", "choices": [{"finish_reason": "stop", "message": {"content": '{"status":"needs_input","reason_code":"MATERIALS_MISSING","summary":"资料为空"}'}}]}
        requests = []

        def transport(request, timeout):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps(first if len(requests) == 1 else second).encode())

        model = LiveModel(api_key="test-only-key", model="transport-fixture")
        messages = [{"role": "system", "content": "tool test"}, {"role": "user", "content": "P01"}]
        with patch("urllib.request.urlopen", side_effect=transport):
            decision = model.decide(messages, [], timeout=2)
            self.assertEqual(decision["tool_calls"], calls)
            messages += [{"role": "assistant", "content": None, "tool_calls": calls},
                         {"role": "tool", "tool_call_id": "call-1", "content": '{"materials":[]}'}]
            finished = model.decide(messages, [], timeout=2)
        self.assertEqual(finished["finish"]["status"], "needs_input")
        self.assertEqual(requests[1]["messages"][-1]["content"], '{"materials":[]}')
        self.assertNotIn("reasoning_content", json.dumps(decision))

    def test_transport_failure_and_malformed_response_return_incomplete(self):
        from urllib.error import URLError
        responses = [URLError("test-only transport failure"), io.BytesIO(b'{"choices":[]}')]
        for response in responses:
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as temp:
                planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
                self.addCleanup(planner.close)
                with patch("urllib.request.urlopen", side_effect=[response]):
                    result = planner.run(BRIEF, model=LiveModel(api_key="test-only-key"))
                self.assertEqual(result["status"], "incomplete")
                self.assertIn(result["reason_code"], ["MODEL_UNAVAILABLE", "MODEL_RESPONSE_INVALID"])
                self.assertEqual(result["steps"], [])

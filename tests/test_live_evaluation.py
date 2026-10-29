"""Public evaluation-command behavior with controlled external HTTP responses."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.provider_fixture import respond
from tools import evaluate


class LiveEvaluationTests(unittest.TestCase):
    def test_live_report_records_gateway_and_actual_response_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"MODEL_API_KEY": "test-only-key", "MODEL_BASE_URL": "http://100.73.185.103:8045/v1", "MODEL_NAME": "fixture-request"}
            with patch.object(evaluate, "ROOT", root), patch.dict("os.environ", environment, clear=True), \
                    patch("sys.argv", ["tools/evaluate.py", "--mode", "live", "--max-model-requests", "100"]), \
                    patch("urllib.request.urlopen", side_effect=respond), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = evaluate.main()
            self.assertEqual(code, 0)
            manifest = json.loads((root / "examples/live/C01-normal/manifest.json").read_text())
            self.assertEqual(manifest["model_name"], "fixture-request")
            self.assertEqual(manifest["model_endpoint"], "http://100.73.185.103:8045/v1")
            self.assertEqual(manifest["response_models"], ["http-fixture"])
            summary = json.loads((root / "examples/live-evaluation-summary.json").read_text())
            self.assertEqual(summary["model_name"], "fixture-request")
            self.assertEqual(summary["response_models"], ["http-fixture"])
            self.assertNotIn("test-only-key", json.dumps(summary))

    def test_report_rejects_a_protocol_failure_even_after_an_actual_save_failure(self):
        from content_planner import Planner
        from content_planner.model import MockModel
        from tests.test_acceptance import AS_OF, BRIEF

        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(Path(directory) / "state.sqlite3", as_of=AS_OF, scenario={"failures": {"save_plan": "always"}})
            try:
                result = planner.run(BRIEF, MockModel())
                self.assertTrue(all(evaluate.validate_result(BRIEF, result, None).values()))
                result.update(reason_code="MODEL_RESPONSE_INVALID")
                result["errors"].append("MODEL_RESPONSE_INVALID")
                checks = evaluate.validate_result(BRIEF, result, None)
                self.assertFalse(all(checks.values()))
                self.assertFalse(checks["model_protocol_valid"])
            finally:
                planner.close()

    def test_full_live_matrix_includes_missing_information_repeat_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"MODEL_API_KEY": "test-only-key", "MODEL_NAME": "full-matrix-fixture"}
            with patch.object(evaluate, "ROOT", root), patch.dict("os.environ", environment, clear=True), \
                    patch("sys.argv", ["tools/evaluate.py", "--mode", "live", "--all-cases", "--max-model-requests", "120"]), \
                    patch("urllib.request.urlopen", side_effect=respond), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                evaluate.main()
            summary = json.loads((root / "examples/live-evaluation-summary.json").read_text())
            self.assertEqual(summary["cases_total"], 18)
            rows = {row["case_id"]: row for row in summary["cases"]}
            self.assertEqual(rows["C02a-missing-budget"]["model_requests_sent"], 0)
            self.assertEqual(rows["C06-repeat"]["model_requests_sent"], 0)
            self.assertEqual(rows["C14-resume"]["model_requests_sent"], 1)
            self.assertTrue((root / "examples/live/C14-resume/first/output.json").is_file())

    def test_live_usage_includes_setup_runs_without_recounting_cached_history(self):
        def measured_transport(request, timeout):
            body = json.loads(respond(request, timeout).read())
            body["usage"] = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
            return io.BytesIO(json.dumps(body).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(evaluate, "ROOT", root), patch.dict("os.environ", {"MODEL_API_KEY": "test-only-key"}, clear=True), \
                    patch("sys.argv", ["tools/evaluate.py", "--mode", "live", "--all-cases", "--max-model-requests", "120"]), \
                    patch("urllib.request.urlopen", side_effect=measured_transport) as transport, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                evaluate.main()
            summary = json.loads((root / "examples/live-evaluation-summary.json").read_text())
            rows = {row["case_id"]: row for row in summary["cases"]}
            self.assertEqual(rows["C06-repeat"]["model_requests_sent"], 0)
            self.assertEqual(rows["C06-repeat"]["setup_model_requests_sent"], 4)
            self.assertEqual(rows["C06-repeat"]["total_model_requests_sent"], 4)
            self.assertEqual(rows["C14-resume"]["model_requests_sent"], 1)
            self.assertEqual(rows["C14-resume"]["setup_model_requests_sent"], 5)
            self.assertEqual(rows["C14-resume"]["total_model_requests_sent"], 6)
            self.assertEqual(summary["model_requests_sent"], transport.call_count)
            self.assertEqual(rows["C06-repeat"]["token_usage"]["total_tokens"], 60)
            self.assertEqual(rows["C14-resume"]["token_usage"]["total_tokens"], 90)
            self.assertEqual(summary["token_usage"]["total_tokens"], 15 * transport.call_count)

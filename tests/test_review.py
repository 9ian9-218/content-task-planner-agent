"""Regression tests from the delivery and boundary audit."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from content_planner import Planner
from content_planner.model import LiveModel, MockModel
from tests.test_acceptance import AS_OF, BRIEF


ROOT = Path(__file__).resolve().parents[1]


class ReviewTests(unittest.TestCase):
    def test_configuration_shapes_are_rejected_without_running_business_tools(self):
        cases = [[], False, {"profiles": []}, {"profiles": None}, {"profiles": {"standard": None}},
                 {"failures": []}, {"failures": {"unknown_tool": "always"}},
                 {"failures": {"save_plan": "once"}}, {"unexpected": True}]
        for scenario in cases:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                source, config = directory / "input.json", directory / "scenario.json"
                source.write_text(json.dumps(BRIEF), encoding="utf-8")
                config.write_text(json.dumps(scenario), encoding="utf-8")
                process = subprocess.run([sys.executable, "-m", "content_planner", "run", "--input", str(source),
                                          "--scenario", str(config), "--as-of", AS_OF, "--db", str(directory / "state.sqlite3")],
                                         cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(process.returncode, 2, process.stderr)
                result = json.loads(process.stdout)
                self.assertEqual(result["reason_code"], "INVALID_CONFIGURATION")
                self.assertEqual(result["steps"], [])
                self.assertEqual(result["cost"]["spent_units"], 0)

    def test_replanned_artifacts_mark_old_success_as_unselected(self):
        from tests.test_guards import RecklessModel

        class ChangeAfterPaid(RecklessModel):
            changed = False

            def decide(self, messages, tools, timeout=30):
                if messages[-1]["role"] == "tool":
                    result = json.loads(messages[-1]["content"])
                    if result["tool_name"] == "generate_script" and result["ok"] and not self.changed:
                        self.changed = True
                        args = {"material_ids": result["data"]["source_ids"], "profile": "economy", "decision_summary": "换方案，旧结果仍保留。"}
                        return {"tool_calls": [{"id": "change", "type": "function", "function": {"name": "estimate_cost", "arguments": json.dumps(args)}}]}
                return super().decide(messages, tools, timeout)

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(dict(BRIEF, deadline="2026-11-01T12:00:00+08:00"), ChangeAfterPaid())
            old = next(op for op in result["steps"] if op["tool"] == "generate_script" and op["status"] == "succeeded")
            self.assertFalse(old["selected"])
            self.assertEqual(old["status"], "succeeded")
            self.assertFalse(next(iter(result["scripts"].values()))["selected"])
            self.assertEqual(result["cost"]["spent_units"], 8)

    def test_material_permutations_reuse_one_paid_script(self):
        class ReorderMaterials(MockModel):
            reordered = False
            generated_again = False

            def decide(self, messages, tools, timeout=30):
                if messages[-1]["role"] == "tool":
                    observation = json.loads(messages[-1]["content"])
                    if observation["tool_name"] == "generate_script" and observation["ok"] and not self.reordered:
                        self.reordered = True
                        arguments = {"material_ids": list(reversed(observation["data"]["source_ids"])), "profile": "standard", "decision_summary": "相同素材集合变换顺序再询价。"}
                        name = "estimate_cost"
                    elif observation["tool_name"] == "estimate_cost" and self.reordered and not self.generated_again:
                        self.generated_again = True
                        arguments = {key: observation["data"][key] for key in ["quote_id", "material_ids", "profile"]}
                        arguments["decision_summary"] = "相同素材集合的生成应该复用。"
                        name = "generate_script"
                    else:
                        return super().decide(messages, tools, timeout)
                    return {"tool_calls": [{"id": f"reordered-{len(messages)}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                return super().decide(messages, tools, timeout)

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(dict(BRIEF, topic="P01 字幕与 P02 旅行模板"), ReorderMaterials())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["cost"]["spent_units"], 8)
            self.assertEqual(len(result["scripts"]), 1)
            self.assertEqual(result["saved_plan"]["source_ids"], ["P01", "P02"])
            self.assertEqual([op["result"]["charged_units"] for op in result["steps"] if op["tool"] == "generate_script"], [8, 0])

    def test_export_cannot_overwrite_the_database_or_input_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            input_path = directory / "input.json"
            original = json.dumps(BRIEF)
            input_path.write_text(original, encoding="utf-8")
            for database, destination in [(directory / "out/output.json", directory / "out"),
                                          (directory / "state.sqlite3", directory)]:
                with self.subTest(destination=str(destination)):
                    process = subprocess.run([sys.executable, "-m", "content_planner", "run", "--input", str(input_path),
                                              "--as-of", AS_OF, "--db", str(database), "--output-dir", str(destination)],
                                             cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(process.returncode, 2, process.stderr)
                    result = json.loads(process.stdout)
                    self.assertEqual(result["reason_code"], "INVALID_CONFIGURATION")
                    self.assertEqual(input_path.read_text(), original)
                    self.assertFalse(database.exists())

    def test_incomplete_http_body_is_a_model_error_and_preserves_progress(self):
        from http.client import IncompleteRead
        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            with patch("urllib.request.urlopen", side_effect=IncompleteRead(b'{"choices":', 100)):
                result = planner.run(BRIEF, model=LiveModel(api_key="test-only-key"))
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["reason_code"], "MODEL_UNAVAILABLE")
            self.assertEqual(result["cost"]["spent_units"], 0)
            self.assertEqual(planner.list_plans(), [])

    def test_truncated_or_invalid_tool_call_responses_never_execute(self):
        valid = {"id": "call-1", "type": "function", "function": {"name": "search_materials", "arguments": json.dumps({"query": "P01", "decision_summary": "查询资料"})}}
        cases = [("length", [valid]), ("content_filter", [valid]), ("tool_calls", [dict(valid, id="")]),
                 ("tool_calls", [dict(valid, type="unknown")]), ("tool_calls", [valid, valid]),
                 ("tool_calls", []), ("stop", [valid])]
        for finish_reason, calls in cases:
            with self.subTest(reason=finish_reason, calls=len(calls)), tempfile.TemporaryDirectory() as temp:
                response = {"choices": [{"finish_reason": finish_reason, "message": {"tool_calls": calls}}]}
                planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
                self.addCleanup(planner.close)
                from urllib.error import URLError
                with patch("urllib.request.urlopen", side_effect=[io.BytesIO(json.dumps(response).encode()), URLError("fixture exhausted")]):
                    result = planner.run(BRIEF, LiveModel(api_key="test-only-key"))
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["reason_code"], "MODEL_RESPONSE_INVALID")
                self.assertEqual(result["steps"], [])

    def test_http_loop_completes_and_counts_only_each_runs_requests(self):
        from tests.provider_fixture import respond
        model = LiveModel(api_key="test-only-key")
        with tempfile.TemporaryDirectory() as temp, patch("urllib.request.urlopen", side_effect=respond) as transport:
            first_planner = Planner(Path(temp) / "first.sqlite3", as_of=AS_OF)
            second_planner = Planner(Path(temp) / "second.sqlite3", as_of=AS_OF, scenario={"profiles": {"standard": {"units": 12}}})
            self.addCleanup(first_planner.close)
            self.addCleanup(second_planner.close)
            first = first_planner.run(BRIEF, model)
            second = second_planner.run(dict(BRIEF, request_id="second-request"), model)
            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "completed")
            self.assertEqual(first["model_requests_sent"], 4)
            self.assertEqual(second["model_requests_sent"], 5)
            self.assertEqual(second["saved_plan"]["script"]["profile"], "economy")
            self.assertEqual(second["cost"]["spent_units"], 8)
            self.assertEqual(transport.call_count, 9)
            self.assertNotIn("test-only-key", json.dumps(second))

    def test_saved_plan_remains_complete_when_the_final_snapshot_write_fails(self):
        import sqlite3
        original_connect = sqlite3.connect

        class FailAfterPlanCommit(sqlite3.Connection):
            plan_pending = False
            plan_committed = False

            def execute(self, sql, parameters=()):
                if self.plan_committed and sql.startswith("INSERT INTO requests"):
                    raise sqlite3.OperationalError("injected final snapshot failure")
                if sql.startswith("INSERT INTO plans"):
                    self.plan_pending = True
                return super().execute(sql, parameters)

            def __exit__(self, kind, value, traceback):
                result = super().__exit__(kind, value, traceback)
                if kind is None and self.plan_pending:
                    self.plan_committed = True
                return result

        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=FailAfterPlanCommit)

        with tempfile.TemporaryDirectory() as temp, patch("sqlite3.connect", side_effect=connect):
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            first = planner.run(BRIEF, MockModel())
            self.assertEqual(first["status"], "completed")
            self.assertTrue(first["persistence_error"])
            self.assertIsNotNone(planner.get_plan(first["plan_id"]))
            cached = planner.run(BRIEF, MockModel())
            self.assertEqual(cached["status"], "completed")
            self.assertEqual(cached["cost"]["spent_units"], 8)
            self.assertEqual(cached["save_status"], "saved")
            self.assertEqual(len(cached["steps"]), 4)
            self.assertEqual(cached["estimated_finish_at"], "2026-11-01T10:43:00+08:00")

    def test_resume_rejects_a_changed_prompt_instead_of_misreporting_its_version(self):
        from content_planner.agent import SYSTEM_PROMPT
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            first = Planner(path, as_of=AS_OF, scenario={"failures": {"save_plan": "always"}})
            try:
                stopped = first.run(BRIEF, MockModel())
            finally:
                first.close()
            resumed = Planner(path, as_of=AS_OF)
            self.addCleanup(resumed.close)
            with patch("content_planner.agent.SYSTEM_PROMPT", SYSTEM_PROMPT + "\n新版本提示词"):
                result = resumed.run(BRIEF, MockModel(), resume=True)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["reason_code"], "PROMPT_VERSION_MISMATCH")
            self.assertEqual(result["model_decisions"], 0)
            self.assertEqual(result["cost"]["spent_units"], 8)
            self.assertEqual(set(result["scripts"]), set(stopped["scripts"]))
            self.assertEqual(resumed.list_plans(), [])

    def test_error_results_preserve_identity_and_a_consistent_public_shape(self):
        required = {"request_id", "status", "reason_code", "missing_fields", "invalid_fields", "steps", "task_plan",
                    "scripts", "saved_plan", "plan_id", "save_status", "cost", "trace", "decisions",
                    "model_mode", "model_decisions", "model_requests_sent", "tool_attempts"}
        cases = [{key: value for key, value in BRIEF.items() if key != "budget_units"}, dict(BRIEF, budget_units=True), BRIEF]
        for index, brief in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
                self.addCleanup(planner.close)
                result = planner.run(brief, LiveModel(api_key="") if index == 2 else MockModel())
                self.assertEqual(result["request_id"], BRIEF["request_id"])
                self.assertTrue(required <= set(result), required - set(result))
                self.assertEqual(result["plan_id"], None)
                self.assertEqual(result["cost"]["spent_units"], 0)

    def test_non_finite_runtime_limits_are_invalid_before_storage_is_created(self):
        for timeout in [float("nan"), float("inf"), -float("inf"), True, 0, -1]:
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as temp:
                database = Path(temp) / "state.sqlite3"
                with self.assertRaises(ValueError):
                    Planner(database, as_of=AS_OF, wall_timeout_seconds=timeout)
                self.assertFalse(database.exists())

    def test_evaluation_gate_rejects_budget_violations_and_inconsistent_ledgers(self):
        from tools.evaluate import validate_result
        import copy
        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, MockModel())
            saved = planner.get_plan(result["plan_id"])
            self.assertTrue(all(validate_result(BRIEF, result, saved).values()))
            constrained = validate_result(dict(BRIEF, budget_units=7), result, saved)
            self.assertFalse(constrained["completion_constraints"])
            broken = copy.deepcopy(result)
            broken["cost"]["spent_units"] = 7
            self.assertFalse(validate_result(BRIEF, broken, saved)["ledger_cost_consistent"])

    def test_commit_overshoot_is_reported_without_hiding_an_actually_saved_plan(self):
        import sqlite3
        original_connect = sqlite3.connect
        clock = [0.0]

        class SlowCommit(sqlite3.Connection):
            saving = False

            def execute(self, sql, parameters=()):
                if sql.startswith("INSERT INTO plans"):
                    self.saving = True
                return super().execute(sql, parameters)

            def __exit__(self, kind, value, traceback):
                result = super().__exit__(kind, value, traceback)
                if kind is None and self.saving:
                    clock[0] = 2.0
                    self.saving = False
                return result

        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=SlowCommit)

        with tempfile.TemporaryDirectory() as temp, patch("sqlite3.connect", side_effect=connect), patch("time.perf_counter", side_effect=lambda: clock[0]):
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF, wall_timeout_seconds=1)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, MockModel())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["save_status"], "saved")
            self.assertTrue(result["runtime_limit_exceeded"])
            self.assertEqual(result["runtime_limit_seconds"], 1)
            self.assertEqual(result["elapsed_seconds"], 2)
            self.assertIsNotNone(planner.get_plan(result["plan_id"]))

    def test_budget_and_deadline_guard_holds_for_36_adversarial_combinations(self):
        from tests.test_acceptance import RecklessModel
        from content_planner.tools import parse_time
        for fee in [0, 8, 12]:
            for budget in [0, 7, 8, 10]:
                for deadline in ["2026-11-01T10:10:00+08:00", "2026-11-01T10:43:00+08:00", "2026-11-01T11:00:00+08:00"]:
                    with self.subTest(fee=fee, budget=budget, deadline=deadline), tempfile.TemporaryDirectory() as temp:
                        planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF, scenario={"profiles": {"standard": {"units": fee}}})
                        try:
                            result = planner.run(dict(BRIEF, budget_units=budget, deadline=deadline), RecklessModel())
                            saved = planner.list_plans()
                        finally:
                            planner.close()
                        if result["status"] == "completed":
                            self.assertLessEqual(result["cost"]["spent_units"], budget)
                            self.assertLessEqual(parse_time(result["estimated_finish_at"]), parse_time(deadline))
                            self.assertEqual(len(saved), 1)
                        else:
                            self.assertEqual(result["cost"]["spent_units"], 0)
                            self.assertEqual(saved, [])

    def test_deepseek_v1_alias_disables_thinking_in_tool_requests(self):
        from tests.provider_fixture import respond
        seen = []

        def transport(request, timeout):
            seen.append(json.loads(request.data))
            return respond(request, timeout)

        model = LiveModel(api_key="test-only-key", base_url="https://api.deepseek.com/v1")
        messages = [{"role": "system", "content": "protocol test"}, {"role": "user", "content": json.dumps({"brief": BRIEF})}]
        with patch("urllib.request.urlopen", side_effect=transport):
            model.decide(messages, [], timeout=1)

        self.assertEqual(seen[0]["thinking"], {"type": "disabled"})

    def test_business_time_overflow_is_incomplete_without_poisoning_the_request(self):
        cases = [(AS_OF, {"profiles": {"standard": {"minutes": 10 ** 20}}}),
                 ("9999-12-31T23:59:00+08:00", {})]
        for as_of, scenario in cases:
            with self.subTest(as_of=as_of), tempfile.TemporaryDirectory() as temp:
                planner = Planner(Path(temp) / "state.sqlite3", as_of=as_of, scenario=scenario)
                self.addCleanup(planner.close)
                brief = dict(BRIEF, deadline="9999-12-31T23:59:59+08:00")
                result = planner.run(brief, MockModel())
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["reason_code"], "TIME_RANGE_EXCEEDED")
                self.assertEqual(result["cost"]["spent_units"], 0)
                self.assertEqual(planner.list_plans(), [])
                cached = planner.run(brief, MockModel())
                self.assertEqual(cached["status"], "incomplete")
                self.assertTrue(cached["cached"])

    def test_authorization_is_not_forwarded_to_a_redirect_target(self):
        import urllib.request
        from tests.provider_fixture import respond

        def transport(request, timeout):
            self.assertEqual(request.get_header("Authorization"), "Bearer test-only-key")
            redirected = urllib.request.HTTPRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "https://redirect-target.invalid/chat/completions")
            self.assertIsNone(redirected.get_header("Authorization"))
            return respond(request, timeout)

        model = LiveModel(api_key="test-only-key")
        messages = [{"role": "system", "content": "protocol test"}, {"role": "user", "content": json.dumps({"brief": BRIEF})}]
        with patch("urllib.request.urlopen", side_effect=transport):
            model.decide(messages, [], timeout=1)

    def test_invalid_failure_configuration_does_not_poison_a_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source, config = directory / 'input.json', directory / 'scenario.json'
            source.write_text(json.dumps(BRIEF), encoding='utf-8')
            command = [sys.executable, '-m', 'content_planner', 'run', '--mode', 'mock', '--input', str(source),
                       '--scenario', str(config), '--as-of', AS_OF, '--db', str(directory / 'state.sqlite3')]
            for scenario, expected_code in [({'failures': []}, 2), ({}, 0)]:
                config.write_text(json.dumps(scenario), encoding='utf-8')
                result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')),
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                output = json.loads(result.stdout)
                if expected_code == 0:
                    self.assertEqual(output['status'], 'completed')
                    self.assertEqual(output['cost']['spent_units'], 8)

    def test_http_failure_after_paid_generation_preserves_the_script_and_cost(self):
        from http.client import IncompleteRead
        from tests.provider_fixture import respond

        def transport(request, timeout):
            payload = json.loads(request.data)
            if payload['messages'][-1]['role'] == 'tool':
                observation = json.loads(payload['messages'][-1]['content'])
                if observation['tool_name'] == 'generate_script' and observation['ok']:
                    raise IncompleteRead(b'incomplete response', 100)
            return respond(request, timeout)

        with tempfile.TemporaryDirectory() as temp, patch('urllib.request.urlopen', side_effect=transport):
            planner = Planner(Path(temp) / 'state.sqlite3', as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, LiveModel(api_key='test-only-key'))
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['reason_code'], 'MODEL_UNAVAILABLE')
            self.assertEqual(result['cost']['spent_units'], 8)
            self.assertEqual(len(result['scripts']), 1)
            self.assertEqual(result['model_requests_sent'], 4)
            self.assertEqual(planner.list_plans(), [])

import json
import os
from pathlib import Path
import tempfile
import unittest

from content_planner import Planner
from content_planner.model import MockModel
from tests.test_acceptance import AS_OF, BRIEF, RecklessModel


class InvalidActionModel:
    mode = "mock"
    action_source = "adversarial_model_stub"

    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments

    def decide(self, messages, tools, timeout=30):
        if messages[-1]["role"] == "tool":
            error = json.loads(messages[-1]["content"])["error_code"]
            return {"finish": {"status": "incomplete", "reason_code": error, "summary": "收到拒绝后结束。"}}
        return {"tool_calls": [{"id": "invalid-call", "type": "function", "function": {"name": self.name, "arguments": self.arguments}}]}


class GuardTests(unittest.TestCase):
    def test_invalid_model_actions_are_observable_errors_without_side_effects(self):
        cases = [("__class__", "{}"), ("generate_script", "{"), ("generate_script", "{}"),
                 ("search_materials", '{"query":42}'),
                 ("estimate_cost", '{"material_ids":["Z99"],"profile":"standard","decision_summary":"测试未知资料"}')]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments), tempfile.TemporaryDirectory() as temp:
                planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
                self.addCleanup(planner.close)
                result = planner.run(BRIEF, model=InvalidActionModel(name, arguments))
                self.assertEqual(result["status"], "incomplete")
                self.assertTrue(result["errors"])
                self.assertEqual(result["cost"]["spent_units"], 0)
                self.assertEqual(planner.list_plans(), [])

    def test_tool_limit_counts_each_action_in_a_multi_call_response(self):
        class ManyCalls(MockModel):
            def decide(self, messages, tools, timeout=30):
                single = super().decide(messages, tools, timeout)
                call = single["tool_calls"][0]
                return {"tool_calls": [dict(call, id=f"batch-{n}") for n in range(20)]}

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF, max_tool_calls=2)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, model=ManyCalls())
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["reason_code"], "STEP_LIMIT")
            self.assertEqual(result["tool_attempts"], 2)

    def test_elapsed_runtime_limit_prevents_tools_after_a_slow_model_response(self):
        import time

        class SlowModel(MockModel):
            def decide(self, messages, tools, timeout=30):
                time.sleep(0.03)
                return super().decide(messages, tools, timeout)

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF, wall_timeout_seconds=0.02)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, model=SlowModel())
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["reason_code"], "RUN_TIMEOUT")
            self.assertEqual(result["steps"], [])

    def test_repeated_successful_script_operation_is_reused_without_charging(self):
        class RepeatScript(MockModel):
            repeated = False

            def decide(self, messages, tools, timeout=30):
                if messages[-1]["role"] == "tool":
                    observed = json.loads(messages[-1]["content"])
                    if observed["tool_name"] == "generate_script" and observed["ok"] and not self.repeated:
                        self.repeated = True
                        return {"tool_calls": [dict(messages[-2]["tool_calls"][0], id="repeat-script")]}
                return super().decide(messages, tools, timeout)

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, model=RepeatScript())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["cost"]["spent_units"], 8)
            self.assertEqual(len([s for s in result["steps"] if s["tool"] == "generate_script" and s["result"]["charged_units"]]), 1)
            self.assertTrue(any(s["result"].get("reused") for s in result["steps"]))

    def test_changing_plan_after_paid_work_keeps_the_spent_cost(self):
        class ChangeAfterPaid(RecklessModel):
            changed = False

            def decide(self, messages, tools, timeout=30):
                if messages[-1]["role"] == "tool":
                    observed = json.loads(messages[-1]["content"])
                    if observed["tool_name"] == "generate_script" and observed["ok"] and not self.changed:
                        self.changed = True
                        args = {"material_ids": observed["data"]["source_ids"], "profile": "economy", "decision_summary": "验证已发生费用不会因为换方案清零。"}
                        return {"tool_calls": [{"id": "change-plan", "function": {"name": "estimate_cost", "arguments": json.dumps(args)}}]}
                return super().decide(messages, tools, timeout)

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(dict(BRIEF, deadline="2026-11-01T12:00:00+08:00"), model=ChangeAfterPaid())
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["cost"]["spent_units"], 8)
            self.assertEqual(result["cost"]["projected_total_units"], 16)
            self.assertIn("BUDGET_EXCEEDED", result["errors"])
            self.assertEqual(len(result["scripts"]), 1)
            self.assertEqual(planner.list_plans(), [])

    def test_in_progress_request_is_not_executed_by_a_second_runner(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        ready, release = threading.Event(), threading.Event()

        class BlockingModel(MockModel):
            def decide(self, messages, tools, timeout=30):
                ready.set()
                release.wait(3)
                return super().decide(messages, tools, timeout)

        with tempfile.TemporaryDirectory() as temp, ThreadPoolExecutor(max_workers=1) as pool:
            path = Path(temp) / "state.sqlite3"

            def first_run():
                planner = Planner(path, as_of=AS_OF)
                try:
                    return planner.run(BRIEF, model=BlockingModel())
                finally:
                    planner.close()

            pending = pool.submit(first_run)
            self.assertTrue(ready.wait(3))
            planner = Planner(path, as_of=AS_OF)
            self.addCleanup(planner.close)
            try:
                second = planner.run(BRIEF, model=MockModel())
                self.assertEqual(second["status"], "incomplete")
                self.assertEqual(second["reason_code"], "REQUEST_IN_PROGRESS")
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5)["status"], "completed")
            self.assertEqual(len(planner.list_plans()), 1)

    def test_calls_in_one_model_response_do_not_claim_to_observe_each_other(self):
        class BatchOnce(MockModel):
            batched = False

            def decide(self, messages, tools, timeout=30):
                decision = super().decide(messages, tools, timeout)
                if not self.batched:
                    self.batched = True
                    call = decision["tool_calls"][0]
                    return {"tool_calls": [dict(call, id="batch-a"), dict(call, id="batch-b")]}
                return decision

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, model=BatchOnce())
            self.assertEqual([e["observed_after_step"] for e in result["trace"][:2]], [None, None])
            self.assertEqual(result["trace"][2]["observed_after_step"], 2)
            self.assertEqual(len(result["decisions"]), 4)

    def test_prior_sources_remain_available_after_an_empty_supplementary_search(self):
        class Supplement(MockModel):
            searched_again = False

            def decide(self, messages, tools, timeout=30):
                if messages[-1]["role"] == "tool":
                    observed = json.loads(messages[-1]["content"])
                    if observed["tool_name"] == "search_materials" and not self.searched_again:
                        self.searched_again = True
                        args = {"query": "Z99", "decision_summary": "补查另一个主题。"}
                        return {"tool_calls": [{"id": "supplement", "function": {"name": "search_materials", "arguments": json.dumps(args)}}]}
                    if observed["tool_name"] == "search_materials" and not observed["data"]["materials"]:
                        args = {"material_ids": ["P01"], "profile": "standard", "decision_summary": "补查为空，继续使用先前检索成功的资料。"}
                        return {"tool_calls": [{"id": "use-prior", "function": {"name": "estimate_cost", "arguments": json.dumps(args)}}]}
                return super().decide(messages, tools, timeout)

        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(BRIEF, model=Supplement())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["saved_plan"]["source_ids"], ["P01"])

    @unittest.skipIf(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0), "requires POSIX permission enforcement for a non-root user")
    def test_real_storage_failure_preserves_success_and_never_reports_saved(self):
        import os

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)

            class ReadOnlyBeforeSave(MockModel):
                def decide(self, messages, tools, timeout=30):
                    if messages[-1]["role"] == "tool":
                        observed = json.loads(messages[-1]["content"])
                        if observed["tool_name"] == "generate_script" and observed["ok"]:
                            os.chmod(directory, 0o500)
                    return super().decide(messages, tools, timeout)

            planner = Planner(directory / "state.sqlite3", as_of=AS_OF)
            try:
                result = planner.run(BRIEF, model=ReadOnlyBeforeSave())
                self.assertEqual(planner.list_plans(), [])
            finally:
                os.chmod(directory, 0o700)
                planner.close()
            self.assertEqual(result["status"], "incomplete")
            self.assertIn("SAVE_FAILED", result["errors"])
            self.assertTrue(result["persistence_error"])
            self.assertEqual(result["cost"]["spent_units"], 8)
            self.assertEqual(len(result["scripts"]), 1)
            self.assertIsNone(result["plan_id"])

    def test_model_cannot_execute_paid_work_after_an_infeasible_deadline_quote(self):
        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            self.addCleanup(planner.close)
            result = planner.run(dict(BRIEF, deadline="2026-11-01T10:10:00+08:00"), model=RecklessModel())
            self.assertEqual(result["status"], "incomplete")
            self.assertIn("DEADLINE_EXCEEDED", result["errors"])
            self.assertEqual(result["cost"]["spent_units"], 0)
            self.assertEqual(planner.list_plans(), [])

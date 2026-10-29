import tempfile
import json
import unittest
from pathlib import Path

from content_planner import Planner
from content_planner.model import MockModel


BRIEF = {"request_id": "test-001", "topic": "P01 字幕功能介绍", "budget_units": 10,
         "deadline": "2026-11-01T11:00:00+08:00", "preference": "quality_first"}
AS_OF = "2026-11-01T10:00:00+08:00"


class RecklessModel(MockModel):
    """External model double that requests a paid action despite an invalid quote."""
    action_source = "adversarial_model_stub"

    def decide(self, messages, tools, timeout=30):
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            if result["tool_name"] == "estimate_cost" and result["ok"]:
                args = {k: result["data"][k] for k in ["quote_id", "material_ids", "profile"]}
                args["decision_summary"] = "故意请求执行以验证程序约束。"
                return {"tool_calls": [{"id": "reckless", "type": "function", "function": {"name": "generate_script", "arguments": json.dumps(args)}}]}
        return super().decide(messages, tools, timeout)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"

    def planner(self, **kwargs):
        planner = Planner(db_path=self.path, as_of=AS_OF, **kwargs)
        self.addCleanup(planner.close)
        return planner

    def test_normal_request_produces_a_retrievable_saved_plan(self):
        planner = self.planner()
        result = planner.run(BRIEF, model=MockModel())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["cost"]["spent_units"], 8)
        self.assertEqual(result["estimated_finish_at"], "2026-11-01T10:43:00+08:00")
        self.assertEqual(planner.get_plan(result["plan_id"])["script"]["source_ids"], ["P01"])
        self.assertEqual(len(planner.list_plans()), 1)

    def test_model_cannot_bypass_the_budget_guard(self):
        planner = self.planner(scenario={"profiles": {"standard": {"units": 12}}})
        result = planner.run(BRIEF, model=RecklessModel())
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("BUDGET_EXCEEDED", result["errors"])
        self.assertEqual(result["cost"]["spent_units"], 0)
        self.assertEqual(result["scripts"], {})
        self.assertEqual(planner.list_plans(), [])

    def test_explicit_resume_reuses_script_after_save_failure(self):
        first = self.planner(scenario={"failures": {"save_plan": "always"}}).run(BRIEF, model=MockModel())
        planner = self.planner()
        resumed = planner.run(BRIEF, model=MockModel(), resume=True)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["cost"]["spent_units"], 8)
        self.assertEqual(set(resumed["scripts"]), set(first["scripts"]))
        self.assertEqual(resumed["model_decisions"], 1)
        self.assertEqual(resumed["estimated_finish_at"], "2026-11-01T10:44:00+08:00")
        self.assertEqual(len(planner.list_plans()), 1)

    def test_missing_required_information_does_not_execute_tools(self):
        for field in ["topic", "budget_units", "deadline"]:
            with self.subTest(field=field):
                planner = self.planner()
                brief = {k: v for k, v in BRIEF.items() if k != field}
                result = planner.run(brief, model=MockModel())
                self.assertEqual(result["status"], "needs_input")
                self.assertIn(field, result["missing_fields"])
                self.assertEqual(result["steps"], [])
                self.assertEqual(planner.list_plans(), [])

    def test_request_id_can_be_derived_from_normalized_business_input(self):
        planner = self.planner()
        brief = {k: v for k, v in BRIEF.items() if k != "request_id"}
        first = planner.run(brief, model=MockModel())
        second = planner.run(dict(brief, topic="  P01 字幕功能介绍  ", deadline="2026-11-01T03:00:00Z"), model=MockModel())
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second["cached"])

    def test_observed_over_budget_quote_changes_the_selected_profile(self):
        planner = self.planner(scenario={"profiles": {"standard": {"units": 12}}})
        result = planner.run(BRIEF, model=MockModel())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["saved_plan"]["script"]["profile"], "economy")
        self.assertEqual(result["cost"]["spent_units"], 8)
        self.assertEqual(result["estimated_finish_at"], "2026-11-01T10:24:00+08:00")
        observed = [event["result"]["data"] for event in result["trace"] if event["tool"] == "estimate_cost"]
        self.assertEqual([quote["projected_total_units"] for quote in observed], [12, 8])

    def test_decision_limit_returns_incomplete_without_saving(self):
        planner = self.planner(max_decisions=2)
        result = planner.run(BRIEF, model=MockModel())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reason_code"], "STEP_LIMIT")
        self.assertEqual(result["model_decisions"], 2)
        self.assertEqual(result["cost"]["spent_units"], 0)
        self.assertEqual(planner.list_plans(), [])

    def test_unaffordable_request_stops_without_charging(self):
        planner = self.planner()
        result = planner.run(dict(BRIEF, budget_units=5), model=MockModel())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reason_code"], "BUDGET_EXCEEDED")
        self.assertEqual(result["cost"]["spent_units"], 0)
        self.assertEqual(result["cost"]["projected_total_units"], 8)
        self.assertEqual(planner.list_plans(), [])

    def test_failed_save_preserves_script_and_reports_incomplete(self):
        planner = self.planner(scenario={"failures": {"save_plan": "always"}})
        result = planner.run(BRIEF, model=MockModel())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["save_status"], "failed")
        self.assertIn("SAVE_FAILED", result["errors"])
        self.assertEqual(result["cost"]["spent_units"], 8)
        self.assertEqual(len(result["scripts"]), 1)
        self.assertIsNone(result["plan_id"])
        self.assertEqual(planner.list_plans(), [])

    def test_repeated_execution_after_reopening_reuses_the_saved_result(self):
        first = self.planner().run(BRIEF, model=MockModel())
        planner = self.planner()
        second = planner.run(BRIEF, model=MockModel())
        self.assertEqual(second["plan_id"], first["plan_id"])
        self.assertEqual(second["model_decisions"], 0)
        self.assertTrue(second["cached"])
        self.assertEqual(second["cost"]["spent_units"], 8)
        self.assertEqual(len(planner.list_plans()), 1)

    def test_same_request_id_with_changed_budget_is_a_conflict(self):
        planner = self.planner()
        first = planner.run(BRIEF, model=MockModel())
        result = planner.run(dict(BRIEF, budget_units=20), model=MockModel())
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["reason_code"], "REQUEST_ID_CONFLICT")
        self.assertEqual(planner.get_plan(first["plan_id"])["cost_units"], 8)
        self.assertEqual(len(planner.list_plans()), 1)

    def test_invalid_input_is_rejected_before_tools(self):
        cases = [("budget_units", True, "invalid_request"), ("budget_units", -1, "invalid_request"),
                 ("budget_units", 1.5, "invalid_request"), ("topic", 42, "invalid_request"),
                 ("topic", "   ", "needs_input"), ("deadline", "tomorrow", "invalid_request"),
                 ("deadline", "2026-11-01T11:00:00", "needs_input"),
                 ("request_id", "", "invalid_request"), ("request_id", None, "invalid_request"),
                 ("preference", "always_spend", "invalid_request")]
        for index, (field, value, status) in enumerate(cases):
            with self.subTest(field=field, value=value):
                case = dict(BRIEF, request_id=f"validation-{index}")
                case[field] = value
                result = self.planner().run(case, model=MockModel())
                self.assertEqual(result["status"], status)
                self.assertEqual(result["steps"], [])

    def test_empty_materials_request_more_information_without_generating(self):
        planner = self.planner()
        result = planner.run(dict(BRIEF, topic="不存在的合成产品 Z99"), model=MockModel())
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["cost"]["spent_units"], 0)
        self.assertEqual(result["scripts"], {})
        self.assertEqual(planner.list_plans(), [])

    def test_script_does_not_claim_features_from_unselected_materials(self):
        cases = [("fact-p01", "P01 字幕介绍", "模板"), ("fact-p02", "P02 旅行模板介绍", "字幕")]
        for request_id, topic, unsupported in cases:
            with self.subTest(topic=topic):
                result = self.planner().run(dict(BRIEF, request_id=request_id, topic=topic), model=MockModel())
                self.assertEqual(result["status"], "completed")
                self.assertNotIn(unsupported, result["saved_plan"]["script"]["text"])

    def test_task_plan_reports_dependencies_and_actual_step_states(self):
        result = self.planner().run(BRIEF, model=MockModel())
        self.assertEqual([t["status"] for t in result["saved_plan"]["tasks"]], ["succeeded"] * 4)
        self.assertEqual(result["task_plan"][-1]["depends_on"], ["generate_script"])
        failed = self.planner(scenario={"failures": {"save_plan": "always"}}).run(dict(BRIEF, request_id="tasks-failed"), model=MockModel())
        self.assertEqual(failed["task_plan"][-1]["status"], "failed")
        self.assertEqual(failed["task_plan"][-2]["status"], "succeeded")

    def test_cost_first_preference_can_finish_before_the_standard_deadline(self):
        result = self.planner().run(dict(BRIEF, preference="cost_first"), model=MockModel())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["saved_plan"]["script"]["profile"], "economy")
        self.assertEqual(result["estimated_finish_at"], "2026-11-01T10:23:00+08:00")

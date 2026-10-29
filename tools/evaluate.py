"""Execute documented scenarios and measure results; Mock is not LLM evidence."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from content_planner import Planner
from content_planner.__main__ import export_result, load_environment
from content_planner.model import LiveModel, MockModel
from content_planner.tools import fingerprint, parse_time


AS_OF = "2026-11-01T10:00:00+08:00"
BRIEF = {"request_id": "evaluation-001", "topic": "P01 字幕功能介绍", "budget_units": 10,
         "deadline": "2026-11-01T11:00:00+08:00", "preference": "quality_first"}


def validate_result(brief, result, saved):
    """Public report gate: passing requires constraints, accounting and save evidence."""
    cost = result["cost"]
    steps = result["steps"]
    completed = result["status"] == "completed"
    typed = all(type(cost[key]) is int and cost[key] >= 0 for key in ["spent_units", "remaining_units", "projected_total_units"])
    checks = {"model_protocol_valid": result.get("reason_code") != "MODEL_RESPONSE_INVALID",
              "nonnegative_integer_costs": typed,
              "ledger_cost_consistent": typed and cost["spent_units"] == sum(op["result"]["charged_units"] for op in steps),
              "projected_total_consistent": typed and cost["projected_total_units"] == cost["spent_units"] + cost["remaining_units"],
              "step_states_consistent": all((op["status"] == "succeeded") == op["result"]["ok"] for op in steps),
              "save_evidence_consistent": bool(saved) == completed and (not completed or
                  result["save_status"] == "saved" and saved["plan_id"] == result["plan_id"] and saved == result["saved_plan"])}
    try:
        checks["completion_constraints"] = not completed or (typed and cost["projected_total_units"] <= brief["budget_units"] and
            parse_time(result["estimated_finish_at"]) <= parse_time(brief["deadline"]))
    except (TypeError, ValueError, KeyError, AttributeError):
        checks["completion_constraints"] = False
    return checks


def cases():
    return [
        ("C01-normal", dict(BRIEF), {}, "completed", None, 8),
        ("C02a-missing-budget", {k: v for k, v in BRIEF.items() if k != "budget_units"}, {}, "needs_input", "MISSING_INFORMATION", 0),
        ("C02b-missing-deadline", {k: v for k, v in BRIEF.items() if k != "deadline"}, {}, "needs_input", "MISSING_INFORMATION", 0),
        ("C03-save-failure", dict(BRIEF), {"failures": {"save_plan": "always"}}, "incomplete", "SAVE_FAILED", 8),
        ("C04-replan-budget", dict(BRIEF), {"profiles": {"standard": {"units": 12}}}, "completed", None, 8),
        ("C05-no-feasible-plan", dict(BRIEF, budget_units=5), {}, "incomplete", "BUDGET_EXCEEDED", 0),
        ("C06-repeat", dict(BRIEF), {}, "completed", None, 8),
        ("C07-deadline", dict(BRIEF, deadline="2026-11-01T10:10:00+08:00"), {}, "incomplete", "DEADLINE_EXCEEDED", 0),
        ("C08-step-limit", dict(BRIEF), {}, "incomplete", "STEP_LIMIT", 0),
        ("C10-empty-materials", dict(BRIEF, topic="不存在的合成产品 Z99"), {}, "needs_input", "MATERIALS_MISSING", 0),
        ("C11a-budget-boundary", dict(BRIEF, budget_units=8), {}, "completed", None, 8),
        ("C11b-deadline-boundary", dict(BRIEF, deadline="2026-11-01T10:43:00+08:00"), {}, "completed", None, 8),
        ("C12-model-failure", dict(BRIEF), {}, "incomplete", "MODEL_UNAVAILABLE", 0),
        ("C13-id-conflict", dict(BRIEF), {}, "invalid_request", "REQUEST_ID_CONFLICT", 0),
        ("C14-resume", dict(BRIEF), {}, "completed", None, 8),
        ("C15-search-failure", dict(BRIEF), {"failures": {"search_materials": "always"}}, "incomplete", "SEARCH_FAILED", 0),
        ("C16-generation-failure", dict(BRIEF), {"failures": {"generate_script": "always"}}, "incomplete", "GENERATION_FAILED", 0),
        ("C17-cost-first", dict(BRIEF, preference="cost_first"), {}, "completed", None, 8),
    ]


def model_usage(*results):
    """Sum reported usage for new live requests only; failed requests may still be billed."""
    decisions, requests = [], 0
    for result in results:
        if result and not result.get("cached") and result.get("model_mode") == "live":
            requests += result.get("model_requests_sent", 0)
            decisions += [item for item in result.get("decisions", []) if item.get("run_id") == result.get("run_id")]
    fields = ["prompt_tokens", "completion_tokens", "total_tokens"]
    valid = [item["usage"] for item in decisions if isinstance(item.get("usage"), dict) and
             all(type(item["usage"].get(key)) is int and item["usage"][key] >= 0 for key in fields)]
    return dict({key: sum(usage[key] for usage in valid) for key in fields}, reported_responses=len(valid),
                accepted_responses=len(decisions), unreported_request_attempts=max(0, requests - len(valid)))


def benchmark(samples):
    fresh, cached = [], []
    for _ in range(samples):
        with tempfile.TemporaryDirectory() as temp:
            planner = Planner(Path(temp) / "state.sqlite3", as_of=AS_OF)
            try:
                started = time.perf_counter()
                first = planner.run(BRIEF, MockModel())
                fresh.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                second = planner.run(BRIEF, MockModel())
                cached.append((time.perf_counter() - started) * 1000)
                if first["status"] != "completed" or not second.get("cached"):
                    raise RuntimeError("benchmark did not exercise the intended paths")
            finally:
                planner.close()

    def metrics(values):
        return {"n": len(values), "median_ms": round(statistics.median(values), 3),
                "p95_ms": round(sorted(values)[math.ceil(0.95 * len(values)) - 1], 3),
                "samples_ms": [round(v, 3) for v in values]}

    return {"mode": "mock", "scope": "Planner.run wall time on a local temporary SQLite database; no LLM/network calls, no process startup or constructor time",
            "fresh": metrics(fresh), "cached": metrics(cached)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--all-cases", action="store_true", help="Include the complete acceptance matrix in live mode.")
    parser.add_argument("--max-model-requests", type=int, help="Required live batch dispatch cap; not a currency budget.")
    parser.add_argument("--model-timeout", type=float, default=30)
    args = parser.parse_args()
    if args.mode == "live" and (args.max_model_requests is None or args.max_model_requests <= 0):
        print(json.dumps({"status": "not_run", "reason_code": "REQUEST_CAP_REQUIRED", "model_requests_sent": 0}))
        return 2
    request_budget = {"remaining": args.max_model_requests} if args.mode == "live" else None
    load_environment(ROOT / ".env.local")
    root = ROOT / "examples" / args.mode
    root.mkdir(parents=True, exist_ok=True)
    if args.mode == "live" and not LiveModel().api_key:
        status = {"status": "not_run", "reason": "MODEL_API_KEY_NOT_CONFIGURED", "requests_sent": 0, "success_rate": None,
                  "checked_at": datetime.now(timezone.utc).isoformat(), "note": "未调用真实模型；此文件不是模型运行 Trace。"}
        (root / "configuration-status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(status, ensure_ascii=False))
        return 2
    selected = cases() if args.mode == "mock" or args.all_cases else [case for case in cases() if case[0] in ["C01-normal", "C03-save-failure", "C04-replan-budget"]]
    source_hash = fingerprint({p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in sorted((ROOT / "src").rglob("*.py"))})
    rows, outputs, comparison = [], {}, {}
    batch_stop_reason = None
    for case_id, brief, scenario, expected_status, expected_reason, expected_spent in selected:
        case_dir = ROOT / "examples/cases" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "input.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (case_dir / "scenario.json").write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest = {"executed_at": datetime.now(timezone.utc).isoformat(), "command": [sys.executable, "tools/evaluate.py", "--mode", args.mode],
                    "case_id": case_id, "mode": args.mode, "python": platform.python_version(), "source_sha256": source_hash,
                    "business_as_of": AS_OF, "scenario": scenario, "state_isolation": "one temporary SQLite file per case; first/repeat or failure/resume share that case's file"}
        before = None
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "state.sqlite3"
            model = MockModel() if args.mode == "mock" else LiveModel(request_budget=request_budget)
            if case_id == "C12-model-failure":
                model = LiveModel(api_key="")
                manifest.update(mode="live", action_source="configuration_probe", transport="not_sent")
            endpoint = urlsplit(getattr(model, "base_url", ""))
            manifest.update(model_name=getattr(model, "model", "deterministic_mock"),
                            model_endpoint=urlunsplit((endpoint.scheme, endpoint.netloc.rsplit("@", 1)[-1], endpoint.path, "", "")))
            manifest.setdefault("action_source", model.action_source)
            if case_id in ["C06-repeat", "C13-id-conflict", "C14-resume"]:
                first_scenario = {"failures": {"save_plan": "always"}} if case_id == "C14-resume" else {}
                first_planner = Planner(database, as_of=AS_OF, scenario=first_scenario, model_timeout_seconds=args.model_timeout)
                try:
                    before = first_planner.run(brief, model=model)
                finally:
                    first_planner.close()
                export_result(before, brief, root / case_id / "first", dict(manifest, scenario=first_scenario,
                    model_requests_sent=before.get("model_requests_sent", 0), token_usage=model_usage(before)))
                if case_id == "C13-id-conflict":
                    brief = dict(brief, budget_units=20)
                    (case_dir / "input.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            planner = Planner(database, as_of=AS_OF, scenario=scenario, max_decisions=2 if case_id == "C08-step-limit" else 12,
                              model_timeout_seconds=args.model_timeout)
            try:
                result = planner.run(brief, model=model, resume=case_id == "C14-resume")
                stored = planner.list_plans()
                saved = planner.get_plan(result["plan_id"]) if result.get("plan_id") else None
            finally:
                planner.close()
        checks = {"status": result["status"] == expected_status, "reason": result.get("reason_code") == expected_reason,
                  "spent": result["cost"]["spent_units"] == expected_spent,
                  "saved_consistently": bool(saved) == (result["status"] == "completed")}
        checks.update(validate_result(brief, result, saved))
        if case_id == "C04-replan-budget":
            quotes = [event["result"]["data"] for event in result["trace"] if event["tool"] == "estimate_cost" and event["result"]["ok"]]
            checks["observed_replan"] = len(quotes) >= 2 and quotes[0]["projected_total_units"] == 12 and result.get("saved_plan", {}).get("script", {}).get("profile") == "economy"
            if quotes and result.get("saved_plan"):
                initial = quotes[0]["projected_total_units"]
                final = result["cost"]["spent_units"]
                comparison["replanning"] = {"initial_rejected_quote_units": initial, "final_spent_units": final,
                                             "reduction_against_rejected_quote_percent": round((initial - final) / initial * 100, 2),
                                             "remaining_budget_units": brief["budget_units"] - final,
                                             "decision_evidence_mode": args.mode}
        if case_id == "C06-repeat":
            checks["reused_same_plan"] = before["plan_id"] == result["plan_id"] and len(stored) == 1 and result["model_decisions"] == 0
            comparison["repeat"] = {"stored_plans": len(stored), "additional_cost_units": result["cost"]["spent_units"] - before["cost"]["spent_units"],
                                     "new_model_decisions": result["model_decisions"], "new_tool_attempts": result.get("tool_attempts")}
        if case_id == "C14-resume":
            checks["script_reused"] = set(result["scripts"]) == set(before["scripts"]) and result["model_decisions"] == 1
            comparison["resume"] = {"additional_cost_units": result["cost"]["spent_units"] - before["cost"]["spent_units"],
                                     "new_tool_attempts": result.get("tool_attempts"), "new_model_decisions": result["model_decisions"],
                                     "script_ids_unchanged": set(result["scripts"]) == set(before["scripts"])}
        if case_id == "C03-save-failure":
            if args.mode == "live":
                checks["reason"] = "SAVE_FAILED" in result["errors"] and result.get("reason_code") not in ["MODEL_UNAVAILABLE", "MODEL_RESPONSE_INVALID", "RUN_TIMEOUT", "STEP_LIMIT"]
            checks["successful_script_retained"] = len(result["scripts"]) == 1 and not stored and result["save_status"] == "failed"
        violation = result["status"] == "completed" and (result["cost"]["projected_total_units"] > brief["budget_units"] or parse_time(result["estimated_finish_at"]) > parse_time(brief["deadline"]))
        manifest.update(actual_result_mode=result["model_mode"], model_requests_sent=result.get("model_requests_sent", 0),
                        data_versions=result.get("data_versions"), prompt_sha256=result.get("prompt_sha256"),
                        token_usage=model_usage(result),
                        response_models=sorted({d["response_model"] for d in result.get("decisions", []) if d.get("response_model")}))
        export_result(result, brief, root / case_id, manifest)
        outputs[case_id] = result
        rows.append({"case_id": case_id, "expected_status": expected_status, "actual_status": result["status"],
                     "reason_code": result.get("reason_code"), "checks": checks, "passed": all(checks.values()),
                     "stored_plan_count": len(stored), "constraint_violation": violation, "false_saved": result["status"] == "completed" and not saved,
                     "spent_units": result["cost"]["spent_units"], "elapsed_seconds": result.get("elapsed_seconds"),
                     "model_requests_sent": result.get("model_requests_sent", 0), "evidence": f"{args.mode}/{case_id}/output.json"})
        rows[-1]["response_models"] = manifest["response_models"]
        rows[-1].update(setup_model_requests_sent=before.get("model_requests_sent", 0) if before else 0,
                        total_model_requests_sent=result.get("model_requests_sent", 0) + (before.get("model_requests_sent", 0) if before else 0),
                        token_usage=model_usage(before, result))
        if args.mode == "live":
            print(json.dumps({"case_id": case_id, "passed": rows[-1]["passed"], "status": result["status"],
                              "reason": result.get("reason_code"), "requests": result.get("model_requests_sent", 0)}, ensure_ascii=False), file=sys.stderr, flush=True)
            if result.get("model_error"):
                batch_stop_reason = "PROVIDER_UNAVAILABLE"
                break
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode, "source_sha256": source_hash,
               "python": platform.python_version(), "cases_total": len(rows), "cases_passed": sum(row["passed"] for row in rows),
               "selected_cases_total": len(selected), "not_run_cases": [case[0] for case in selected[len(rows):]],
               "batch_stop_reason": batch_stop_reason,
               "expected_behavior_match_rate": sum(row["passed"] for row in rows) / len(rows),
               "status_counts": dict(Counter(row["actual_status"] for row in rows)),
               "constraint_violations": sum(row["constraint_violation"] for row in rows), "false_saved": sum(row["false_saved"] for row in rows),
               "model_requests_sent": sum(row["total_model_requests_sent"] for row in rows), "comparisons": comparison, "cases": rows,
               "request_cap": args.max_model_requests if args.mode == "live" else None,
               "model_timeout_seconds": args.model_timeout,
               "request_budget_remaining": request_budget["remaining"] if request_budget else None,
               "token_usage": {key: sum(row["token_usage"][key] for row in rows) for key in ["prompt_tokens", "completion_tokens", "total_tokens", "reported_responses", "accepted_responses", "unreported_request_attempts"]},
               "model_name": getattr(model, "model", "deterministic_mock"),
               "model_endpoint": manifest["model_endpoint"],
               "response_models": sorted({name for row in rows for name in row["response_models"]}),
               "limitations": ["Synthetic materials, prices and business durations", "Mock decisions do not prove LLM capabilities", "Quote reduction is not measured real-world spending savings"]}
    if args.mode == "mock" and args.samples > 0:
        summary["latency_benchmark"] = benchmark(args.samples)
    path = ROOT / "examples" / ("evaluation-summary.json" if args.mode == "mock" else "live-evaluation-summary.json")
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key not in ["cases", "latency_benchmark"]}, ensure_ascii=False, indent=2))
    return 0 if summary["cases_passed"] == summary["cases_total"] and batch_stop_reason is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Repeat one declared live scenario; this measures repeatability, not generalization."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from content_planner import Planner
from content_planner.__main__ import export_result, load_environment
from content_planner.model import LiveModel
from content_planner.tools import fingerprint
from tools.evaluate import AS_OF, BRIEF, model_usage, validate_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-model-requests", type=int)
    parser.add_argument("--model-timeout", type=float, default=30)
    args = parser.parse_args()
    if args.max_model_requests is None or args.max_model_requests <= 0 or not 1 <= args.rounds <= 5:
        print(json.dumps({"status": "not_run", "reason_code": "REQUEST_CAP_REQUIRED", "model_requests_sent": 0}))
        return 2
    load_environment(ROOT / ".env.local")
    if not LiveModel().api_key:
        print(json.dumps({"status": "not_run", "reason_code": "MODEL_API_KEY_NOT_CONFIGURED", "model_requests_sent": 0}))
        return 2
    source_hash = fingerprint({p.relative_to(ROOT).as_posix(): p.read_text() for p in sorted((ROOT / "src").rglob("*.py"))})
    destination = ROOT / "examples/short-validation/replanning"
    destination.mkdir(parents=True, exist_ok=True)
    budget = {"remaining": args.max_model_requests}
    brief = dict(BRIEF, request_id="repeated-replanning")
    scenario = {"profiles": {"standard": {"units": 12}}}
    rows = []
    for index in range(args.rounds):
        model = LiveModel(request_budget=budget)
        with tempfile.TemporaryDirectory(prefix="planner-replanning-") as directory:
            planner = Planner(Path(directory) / "state.sqlite3", as_of=AS_OF, scenario=scenario, model_timeout_seconds=args.model_timeout)
            try:
                result = planner.run(brief, model)
                saved = planner.get_plan(result["plan_id"]) if result.get("plan_id") else None
            finally:
                planner.close()
        quotes = [event["result"]["data"]["projected_total_units"] for event in result["trace"]
                  if event["tool"] == "estimate_cost" and event["result"]["ok"]]
        checks = validate_result(brief, result, saved)
        checks.update(completed=result["status"] == "completed", quotes_observed=quotes == [12, 8],
                      expected_cost=result["cost"]["spent_units"] == 8,
                      economy_selected=bool(saved) and saved["script"]["profile"] == "economy",
                      actual_remote_actions=bool(result["trace"]) and all(e["action_source"] == "remote_model" for e in result["trace"]))
        endpoint = urlsplit(model.base_url)
        manifest = {"executed_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "command": [sys.executable, "tools/evaluate_replanning.py"] + sys.argv[1:],
            "source_sha256": source_hash, "mode": "live", "action_source": model.action_source,
            "scenario": scenario, "business_as_of": AS_OF, "model_name": model.model,
            "model_endpoint": urlunsplit((endpoint.scheme, endpoint.netloc.rsplit("@", 1)[-1], endpoint.path, "", "")),
            "model_requests_sent": result["model_requests_sent"], "token_usage": model_usage(result),
            "data_versions": result.get("data_versions"), "prompt_sha256": result.get("prompt_sha256")}
        export_result(result, brief, destination / f"round-{index + 1}", manifest)
        rows.append({"round": index + 1, "checks": checks, "passed": all(checks.values()),
                     "observed_quote_units": quotes, "model_requests_sent": result["model_requests_sent"],
                     "token_usage": model_usage(result), "elapsed_seconds": result["elapsed_seconds"],
                     "evidence": f"round-{index + 1}/output.json"})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "source_sha256": source_hash,
        "rounds": args.rounds, "rounds_passed": sum(r["passed"] for r in rows), "passed": all(r["passed"] for r in rows),
        "model_requests_sent": sum(r["model_requests_sent"] for r in rows),
        "request_cap": args.max_model_requests, "request_budget_remaining": budget["remaining"],
        "model_name": model.model, "results": rows,
        "scope": "Same input/scenario repeated on isolated databases. Three repeats do not establish a general model success rate or independent held-out evaluation."}
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Exercise independent CLI processes against one database; no remote model calls."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from content_planner import Planner
from content_planner.tools import fingerprint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--output", default="examples/hardening/concurrency.json")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100 or not 2 <= args.clients <= 32:
        parser.error("rounds: 1..100; clients: 2..32")
    started = datetime.now(timezone.utc).isoformat()
    rows = []
    durations = []
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    source_hash = fingerprint({p.relative_to(ROOT).as_posix(): p.read_text() for p in sorted((ROOT / "src").rglob("*.py"))})
    with tempfile.TemporaryDirectory(prefix="planner-contention-") as directory:
        state = Path(directory) / "shared.sqlite3"
        for index in range(args.rounds):
            brief = {"request_id": f"contention-{index}", "topic": "P01 字幕功能介绍", "budget_units": 10,
                     "deadline": "2026-11-01T11:00:00+08:00", "preference": "quality_first"}
            source = Path(directory) / "input.json"
            source.write_text(json.dumps(brief))
            command = [sys.executable, "-m", "content_planner", "run", "--input", str(source), "--db", str(state),
                       "--mode", "mock", "--as-of", "2026-11-01T10:00:00+08:00"]
            before = time.perf_counter()
            processes = []
            clients = []
            try:
                for _ in range(args.clients):
                    processes.append(subprocess.Popen(command, env=environment, cwd=ROOT, text=True,
                                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                for process in processes:
                    output, error = process.communicate(timeout=30)
                    try:
                        result = json.loads(output)
                        clients.append({"exit_code": process.returncode, "status": result["status"],
                            "reason_code": result.get("reason_code"), "cached": result.get("cached"),
                            "model_decisions": result["model_decisions"], "tool_attempts": result["tool_attempts"],
                            "plan_id": result.get("plan_id"), "spent_units": result["cost"]["spent_units"]})
                    except (ValueError, KeyError):
                        clients.append({"exit_code": process.returncode, "invalid_output": True, "stderr": error[:500]})
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
            elapsed = round(time.perf_counter() - before, 6)
            durations.append(elapsed)
            planner = Planner(state, read_only=True)
            try:
                plans = [plan for plan in planner.list_plans() if plan["request_id"] == brief["request_id"]]
            finally:
                planner.close()
            checks = {"one_plan": len(plans) == 1,
                      "one_execution": sum(client.get("tool_attempts", 0) for client in clients) == 4,
                      "known_client_outcomes": all(client.get("status") == "completed" or
                            client.get("reason_code") == "REQUEST_IN_PROGRESS" for client in clients),
                      "single_charge": len(plans) == 1 and plans[0]["cost_units"] == 8,
                      "same_saved_id": len({c["plan_id"] for c in clients if c.get("plan_id")}) == 1}
            rows.append({"round": index + 1, "seconds": elapsed, "checks": checks, "clients": clients,
                         "saved_plan_count": len(plans), "passed": all(checks.values())})
            print(json.dumps({"round": index + 1, "passed": rows[-1]["passed"], "seconds": elapsed}), flush=True)
    summary = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
        "source_sha256": source_hash, "mode": "mock", "remote_model_requests": 0,
        "scope": "Single host; one SQLite file on the native temporary filesystem. Each round submits one identical brief from independent CLI processes. Not a 24-hour soak or distinct-task load test.",
        "rounds": args.rounds, "clients_per_round": args.clients, "client_runs": args.rounds * args.clients,
        "rounds_passed": sum(row["passed"] for row in rows), "median_round_seconds": statistics.median(durations),
        "max_round_seconds": max(durations), "passed": all(row["passed"] for row in rows), "results": rows}
    destination = ROOT / args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Kill real processes at eight public observer boundaries; synthetic business only."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from content_planner import Planner
from content_planner.model import MockModel
from content_planner.tools import fingerprint

AS_OF = "2026-11-01T10:00:00+08:00"
NAMES = ["search_materials", "estimate_cost", "generate_script", "save_plan"]


class ControlledModel(MockModel):
    mode = "live"
    action_source = "controlled_recovery_model_fixture"


def worker(arguments):
    database, ready, stage, tool, brief_file = arguments
    def observer(event):
        matches = event["event"] == stage and (
            event.get("next_tools") == [tool] if stage == "model_decision_committed" else event.get("tool") == tool)
        if matches:
            Path(ready).write_text("reached public crash boundary")
            until = time.monotonic() + 20
            while time.monotonic() < until:
                time.sleep(.01)
            raise RuntimeError("parent did not terminate worker")
    planner = Planner(database, as_of=AS_OF, lease_seconds=.4, heartbeat_interval=.05, event_sink=observer)
    try:
        result = planner.run(json.loads(Path(brief_file).read_text()), ControlledModel())
        print(json.dumps(result), flush=True)
    finally:
        planner.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2:])
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--output", default="examples/hardening/recovery")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20:
        parser.error("rounds must be 1..20")
    destination = ROOT / args.output
    destination.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    source_hash = fingerprint({p.relative_to(ROOT).as_posix(): p.read_text() for p in sorted((ROOT / "src").rglob("*.py"))})
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_")}
    rows = []
    for stage in ["model_decision_committed", "tool_result_uncommitted"]:
        for tool in NAMES:
            for round_id in range(args.rounds):
                before = time.monotonic()
                label = f"{stage}-{tool}-{round_id + 1:02d}"
                brief = {"request_id": label, "topic": "P01 字幕功能介绍", "budget_units": 10,
                         "deadline": "2026-11-01T11:00:00+08:00", "preference": "quality_first"}
                with tempfile.TemporaryDirectory(prefix="planner-recovery-") as directory:
                    root = Path(directory)
                    database, ready, source = [root / n for n in ["state.sqlite3", "ready", "input.json"]]
                    source.write_text(json.dumps(brief))
                    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(database), str(ready),
                                                stage, tool, str(source)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        deadline = time.monotonic() + 10
                        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        if not ready.exists():
                            raise RuntimeError("worker did not reach " + label)
                        process.kill()
                        process.communicate(timeout=5)
                        time.sleep(.5)
                        # Only the killed owner needs accelerated expiry. Recovery uses the real
                        # deployment lease so host scheduling does not redefine the experiment.
                        planner = Planner(database, as_of=AS_OF)
                        try:
                            status = planner.status(brief["request_id"])
                            result = planner.run(brief, ControlledModel(), resume=True)
                            plans = planner.list_plans()
                        finally:
                            planner.close()
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)
                checks = {"recoverable": status["runtime_state"] == "recoverable", "completed": result["status"] == "completed",
                          "one_saved_plan": len(plans) == 1 and plans[0] == result["saved_plan"],
                          "single_charge": result["cost"]["spent_units"] == 8,
                          "decision_reused": result["replayed_decisions"] == 1,
                          "four_model_attempts": result["task_counters"]["model_attempts"] == 4,
                          "tool_budget_preserved": result["task_counters"]["tool_attempts"] == (5 if stage == "tool_result_uncommitted" else 4),
                          "only_four_committed_steps": len(result["steps"]) == 4}
                (destination / f"{label}.json").write_text(json.dumps({"input": brief, "status_before_resume": status,
                    "actual_output": result, "checks": checks}, ensure_ascii=False, indent=2) + "\n")
                rows.append({"stage": stage, "tool": tool, "round": round_id + 1, "seconds": round(time.monotonic() - before, 6),
                             "checks": checks, "passed": all(checks.values()), "evidence": f"{label}.json"})
            print(json.dumps({"stage": stage, "tool": tool, "passed": sum(row["passed"] for row in rows[-args.rounds:]),
                              "rounds": args.rounds}), flush=True)
    summary = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "mode": "controlled_model_fixture", "remote_requests": 0, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(), "source_sha256": source_hash, "fault_positions": 8, "rounds_per_position": args.rounds,
        "crashed_owner_lease_seconds": .4, "recovered_owner_lease_seconds": 60,
        "checks_total": len(rows), "checks_passed": sum(row["passed"] for row in rows), "passed": all(row["passed"] for row in rows),
        "scope": "Real subprocess SIGKILL on native local temporary filesystem; four committed model decisions and four uncommitted local tool results. This does not prove remote side-effect reconciliation or power-loss durability.",
        "results": rows}
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

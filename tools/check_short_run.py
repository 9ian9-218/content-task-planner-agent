"""Bounded mixed-task load and resource checks, using real processes and SQLite."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from content_planner import Planner
from content_planner.agent import empty_result
from content_planner.model import MockModel
from content_planner.state import StateError
from content_planner.tools import fingerprint
from tools.evaluate import AS_OF, BRIEF, validate_result


def resources():
    status = Path("/proc/self/status").read_text()
    rss = int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmRSS:")))
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {"rss_kib": rss, "fds": len(list(Path("/proc/self/fd").iterdir())),
            "threads": threading.active_count(), "cpu_seconds": usage.ru_utime + usage.ru_stime}


def demand(index, request_id):
    brief = dict(BRIEF, request_id=request_id)
    variant = index % 7
    scenario, status, reason, spent = {}, "completed", None, 8
    if variant == 1:
        scenario = {"profiles": {"standard": {"units": 12}}}
    elif variant == 2:
        brief["budget_units"] = 5
        status, reason, spent = "incomplete", "BUDGET_EXCEEDED", 0
    elif variant == 3:
        brief.pop("deadline")
        status, reason, spent = "needs_input", "MISSING_INFORMATION", 0
    elif variant in [4, 5]:
        scenario = {"failures": {"save_plan": "always"}}
        if variant == 4:
            status, reason = "incomplete", "SAVE_FAILED"
    elif variant == 6:
        brief["topic"] = "没有对应资料的 Z99"
        status, reason, spent = "needs_input", "MATERIALS_MISSING", 0
    return brief, scenario, status, reason, spent, variant


def worker(directory, client, destination):
    shared, output = Path(directory), Path(destination)
    client = int(client)
    baseline = resources()
    (shared / f"ready-{client}").write_text("ready")
    until = time.monotonic() + 30
    while not (shared / "start.json").is_file():
        if time.monotonic() >= until:
            raise RuntimeError("start barrier timed out")
        time.sleep(.01)
    control = json.loads((shared / "start.json").read_text())
    deadline = control["deadline"]
    samples, rows = [], []
    next_sample = time.monotonic()
    cases = 0
    while time.monotonic() < deadline and cases < 5000:
        request_id = f"short-{client}-{cases}"
        brief, scenario, expected_status, expected_reason, expected_spent, variant = demand(cases + client, request_id)
        before = time.monotonic()
        planner = None
        attempts = capacity_rejections = busy_rejections = 0
        try:
            while True:
                attempts += 1
                try:
                    if planner is None:
                        planner = Planner(shared / "state.sqlite3", as_of=AS_OF, scenario=scenario, journal_mode="wal")
                    result = planner.run(brief, MockModel())
                except StateError as exc:
                    if exc.code != "STATE_BUSY":
                        raise
                    result = empty_result("incomplete", "STATE_BUSY", "mock", brief)
                code = result.get("reason_code")
                capacity_rejections += int(code == "CAPACITY_EXCEEDED")
                busy_rejections += int(code == "STATE_BUSY")
                if code not in ["CAPACITY_EXCEEDED", "STATE_BUSY"] or time.monotonic() >= deadline:
                    break
                time.sleep(.015 * (1 + client % 3))
            admitted = result.get("reason_code") not in ["CAPACITY_EXCEEDED", "STATE_BUSY"]
            first = result
        finally:
            if planner is not None:
                planner.close()
        if admitted and variant == 5:
            planner = None
            try:
                while True:
                    attempts += 1
                    try:
                        if planner is None:
                            planner = Planner(shared / "state.sqlite3", as_of=AS_OF)
                        result = planner.run(brief, MockModel(), resume=True)
                    except StateError as exc:
                        if exc.code != "STATE_BUSY":
                            raise
                        result = dict(first, reason_code="STATE_BUSY")
                    code = result.get("reason_code")
                    capacity_rejections += int(code == "CAPACITY_EXCEEDED")
                    busy_rejections += int(code == "STATE_BUSY")
                    if code not in ["CAPACITY_EXCEEDED", "STATE_BUSY"] or time.monotonic() >= deadline + 5:
                        break
                    time.sleep(.015 * (1 + client % 3))
            finally:
                if planner is not None:
                    planner.close()
        checks = {}
        if admitted:
            planner = Planner(shared / "state.sqlite3", as_of=AS_OF, read_only=True)
            try:
                saved = planner.get_plan(result["plan_id"]) if result.get("plan_id") else None
            finally:
                planner.close()
            checks = validate_result(brief, result, saved)
            checks.update(status=result["status"] == expected_status, reason=result.get("reason_code") == expected_reason,
                          spent=result["cost"]["spent_units"] == expected_spent)
            if variant == 5:
                checks.update(failure_retained=first["reason_code"] == "SAVE_FAILED" and first["cost"]["spent_units"] == 8,
                              resume_only_save=result["tool_attempts"] == 1 and set(first["scripts"]) == set(result["scripts"]))
            if result["status"] == "completed":
                planner = Planner(shared / "state.sqlite3", as_of=AS_OF)
                try:
                    repeated = planner.run(brief, MockModel())
                finally:
                    planner.close()
                checks["repeat_no_new_work"] = repeated["cached"] and repeated["plan_id"] == result["plan_id"] and repeated["model_decisions"] == 0 and repeated["tool_attempts"] == 0 and repeated["cost"]["spent_units"] == expected_spent
            if cases < 7:
                (output / f"example-{client}-{variant}.json").write_text(json.dumps({"input": brief, "scenario": scenario,
                    "actual_output": result, "checks": checks}, ensure_ascii=False, indent=2) + "\n")
        rows.append({"request_id": request_id, "variant": variant, "admitted": admitted,
                     "admission_attempts": attempts, "status": result["status"], "reason_code": result.get("reason_code"),
                     "capacity_rejections": capacity_rejections, "busy_rejections": busy_rejections,
                     "plan_id": result.get("plan_id"), "spent_units": result["cost"]["spent_units"],
                     "seconds": round(time.monotonic() - before, 6), "checks": checks,
                     "passed": all(checks.values()) if admitted else None})
        cases += 1
        if time.monotonic() >= next_sample:
            samples.append(dict(resources(), at_seconds=round(time.monotonic() - control["started"], 3)))
            next_sample = time.monotonic() + 1
        pause = min(max(0, control["request_interval"] - (time.monotonic() - before)), max(0, deadline - time.monotonic()))
        if pause:
            time.sleep(pause)
    end = resources()
    report = {"client": client, "pid": os.getpid(), "baseline": baseline, "end": end,
              "samples": samples, "cases": rows, "finished_at_monotonic": time.monotonic()}
    (output / f"client-{client}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(*sys.argv[2:])
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=float, default=300)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--minimum-completed", type=int, default=300)
    parser.add_argument("--request-interval", type=float, default=2, help="Seconds between new demands per client; zero is unpaced saturation.")
    parser.add_argument("--output", default="examples/short-validation/load")
    args = parser.parse_args()
    if not .5 <= args.duration_seconds <= 600 or not 1 <= args.clients <= 8 or args.minimum_completed < 1:
        parser.error("duration 0.5..600 seconds, clients 1..8, minimum-completed >=1")
    if not math.isfinite(args.request_interval) or not 0 <= args.request_interval <= 5:
        parser.error("request interval must be 0..5 seconds")
    if not Path("/proc/self/status").is_file():
        parser.error("Linux /proc is required for this resource check")
    destination = (ROOT / args.output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source_hash = fingerprint({p.relative_to(ROOT).as_posix(): p.read_text() for p in sorted((ROOT / "src").rglob("*.py"))})
    started_at = datetime.now(timezone.utc).isoformat()
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_")}
    processes = []
    reports = []
    with tempfile.TemporaryDirectory(prefix="planner-short-load-") as temporary:
        shared = Path(temporary)
        try:
            for client in range(args.clients):
                processes.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(shared),
                    str(client), str(destination)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            until = time.monotonic() + 25
            while not all((shared / f"ready-{i}").exists() for i in range(args.clients)):
                if time.monotonic() > until or any(p.poll() is not None for p in processes):
                    raise RuntimeError("worker initialization failed")
                time.sleep(.01)
            started = time.monotonic()
            control = {"started": started, "deadline": started + args.duration_seconds, "request_interval": args.request_interval}
            staging = shared / "starting.json"
            staging.write_text(json.dumps(control))
            staging.replace(shared / "start.json")
            next_update = started + 15
            while any(p.poll() is None for p in processes):
                if time.monotonic() > control["deadline"] + 25:
                    raise RuntimeError("short-load deadline exceeded")
                if time.monotonic() >= next_update:
                    print(json.dumps({"elapsed_seconds": round(time.monotonic() - started, 1), "target_seconds": args.duration_seconds}), flush=True)
                    next_update = time.monotonic() + 15
                time.sleep(.05)
            for index, process in enumerate(processes):
                output, error = process.communicate(timeout=5)
                if process.returncode:
                    raise RuntimeError(f"worker {index} failed: " + error[-1000:])
                reports.append(json.loads((destination / f"client-{index}.json").read_text()))
            planner = Planner(shared / "state.sqlite3", as_of=AS_OF, read_only=True)
            try:
                plans = planner.list_plans()
            finally:
                planner.close()
            database_size = (shared / "state.sqlite3").stat().st_size
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
    cases = [case for report in reports for case in report["cases"]]
    admitted = [case for case in cases if case["admitted"]]
    expected_plans = [case for case in admitted if case["status"] == "completed"]
    samples = [sample for report in reports for sample in report["samples"]]
    growth = []
    for report in reports:
        warmed = report["samples"][len(report["samples"]) // 3:]
        if warmed:
            growth.append(statistics.median(s["rss_kib"] for s in warmed[-5:]) - statistics.median(s["rss_kib"] for s in warmed[:5]))
    resource_checks = {"no_thread_leaks": all(r["end"]["threads"] == r["baseline"]["threads"] for r in reports),
        "no_fd_leaks": all(r["end"]["fds"] <= r["baseline"]["fds"] + 2 for r in reports),
        "peak_worker_rss_under_128_mib": max(s["rss_kib"] for s in samples) <= 128 * 1024,
        "rss_growth_after_warmup_under_16_mib": max(growth, default=0) <= 16 * 1024}
    measured = max(r["finished_at_monotonic"] for r in reports) - started
    failures = sum(not case["passed"] for case in admitted)
    checks = dict(resource_checks, duration_reached=measured >= args.duration_seconds,
        minimum_completed=len(admitted) >= args.minimum_completed, no_invariant_failures=failures == 0,
        unique_saved_ids=len({plan["plan_id"] for plan in plans}) == len(plans),
        expected_plan_count=len(plans) == len(expected_plans),
        saved_ids_match={plan["plan_id"] for plan in plans} == {case["plan_id"] for case in expected_plans})
    latencies = sorted(case["seconds"] for case in admitted)
    summary = {"started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": source_hash, "python": platform.python_version(), "platform": platform.platform(),
        "mode": "mock", "remote_model_requests": 0, "clients": args.clients, "max_active_tasks_configured": 2,
        "journal_mode": "wal", "storage": "native local Linux temporary filesystem; verified SQLite fix level",
        "target_load_seconds": args.duration_seconds, "measured_load_seconds": round(measured, 3),
        "request_interval_per_client_seconds": args.request_interval,
        "offered_new_demands_per_second_upper_bound": args.clients / args.request_interval if args.request_interval else None,
        "resume_drain_allowance_seconds": 5,
        "minimum_completed_required": args.minimum_completed, "completed_checks": len(admitted),
        "invariant_failures": failures, "not_admitted_at_deadline": len(cases) - len(admitted),
        "admission_attempts": sum(case["admission_attempts"] for case in cases),
        "capacity_rejections": sum(case["capacity_rejections"] for case in cases),
        "busy_rejections": sum(case["busy_rejections"] for case in cases),
        "database_plan_count": len(plans), "expected_saved_plan_count": len(expected_plans), "database_bytes": database_size,
        "resource_checks": resource_checks, "max_worker_rss_kib": max(s["rss_kib"] for s in samples),
        "max_worker_warmed_rss_growth_kib": max(growth, default=0),
        "median_task_seconds": statistics.median(latencies) if latencies else None,
        "p95_task_seconds": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None,
        "checks": checks, "passed": all(checks.values()),
        "scope": "Bounded Linux mixed-task processes and one native temporary SQLite database. Task timings include admission waits. No remote model calls or long-term SLA claim."}
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

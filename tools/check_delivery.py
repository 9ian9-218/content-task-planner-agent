"""Audit exam evidence and formal submission fields; never calls a model or edits evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from content_planner.tools import fingerprint
from tools.evaluate import validate_result

REQUIRED = ["README.md", "SUBMISSION.md", "CONTEXT.md", "requirements.txt", ".env.example", "runtime.lock.json",
            "docs/01-requirements.md", "docs/02-solution.md", "docs/03-evaluation.md", "docs/04-ai-usage.md",
            "docs/05-retrospective.md", "docs/15-short-validation-and-delivery.md", "docs/16-acceptance-and-submission.md",
            "data/materials.json", "data/tool_catalog.json", "submission.json"]
CORE = ["C01-normal", "C02a-missing-budget", "C02b-missing-deadline", "C03-save-failure", "C04-replan-budget", "C06-repeat", "C08-step-limit", "C14-resume"]


def valid_request_budget(report):
    cap, sent, remaining = [report.get(name) for name in ["request_cap", "model_requests_sent", "request_budget_remaining"]]
    return (all(type(value) is int for value in [cap, sent, remaining]) and
            cap > 0 and 0 <= sent <= cap and remaining == cap - sent)


def audit(root):
    root = Path(root).resolve()
    problems, blockers = [], []
    def problem(code, path, note=""):
        problems.append({"code": code, "path": str(path), "note": note})
    def read(path):
        try:
            return json.loads((root / path).read_text())
        except (OSError, ValueError):
            problem("MISSING_OR_INVALID_JSON", path)
            return None
    for name in REQUIRED:
        if not (root / name).is_file():
            problem("REQUIRED_FILE_MISSING", name)
    for directory in ["src", "tests"]:
        if not any((root / directory).rglob("*.py")):
            problem("REQUIRED_CODE_MISSING", directory)
    source = fingerprint({p.relative_to(root).as_posix(): p.read_text() for p in sorted((root / "src").rglob("*.py"))})
    test_hash = fingerprint({p.relative_to(root).as_posix(): p.read_text() for p in sorted((root / "tests").rglob("*.py"))})
    reports = {}
    specs = [
        ("tests", "examples/test-summary.json", lambda s: s.get("exit_code") == 0 and s.get("passed") == s.get("tests_run") and s.get("passed", 0) > 0 and s.get("skipped") == 0 and s.get("inputs_unchanged") and s.get("tests_sha256") == test_hash),
        ("offline", "examples/evaluation-summary.json", lambda s: s.get("cases_passed") == s.get("cases_total") == 18 and s.get("constraint_violations") == s.get("false_saved") == 0),
        ("live", "examples/live-evaluation-summary.json", lambda s: s.get("cases_passed") == s.get("cases_total") == 18 and s.get("constraint_violations") == s.get("false_saved") == 0 and valid_request_budget(s)),
        ("adversarial", "examples/live/adversarial-summary.json", lambda s: s.get("cases_passed") == s.get("cases_total") == 2 and valid_request_budget(s)),
        ("replanning", "examples/short-validation/replanning/summary.json", lambda s: s.get("rounds_passed") == s.get("rounds") == 3 and s.get("passed") and valid_request_budget(s)),
        ("short_load", "examples/short-validation/load/summary.json", lambda s: s.get("passed") and s.get("clients") == 8 and s.get("target_load_seconds", 0) >= 300 and s.get("measured_load_seconds", 0) >= 300 and s.get("completed_checks", 0) >= 300 and s.get("invariant_failures") == 0),
        ("contention", "examples/hardening/concurrency.json", lambda s: s.get("passed") and s.get("rounds_passed", 0) >= 20 and s.get("clients_per_round", 0) >= 16),
        ("recovery", "examples/hardening/recovery/summary.json", lambda s: s.get("passed") and s.get("checks_passed") == s.get("checks_total") == 80),
    ]
    for label, path, gate in specs:
        report = read(path)
        if report is not None:
            reports[label] = {"path": path, "passed": bool(gate(report)), "source_matches": report.get("source_sha256") == source}
            if not reports[label]["passed"]:
                problem("VALIDATION_NOT_PASSED", path)
            if not reports[label]["source_matches"]:
                problem("SOURCE_FINGERPRINT_MISMATCH", path)
    paths = []
    for mode in ["mock", "live"]:
        for case in CORE:
            directory = Path("examples") / mode / case
            result, brief, manifest = [read(directory / name) for name in ["output.json", "input.json", "manifest.json"]]
            if result is None or brief is None or manifest is None:
                continue
            try:
                checks = validate_result(brief, result, result.get("saved_plan"))
                trace = [json.loads(line) for line in (root / directory / "trace.jsonl").read_text().splitlines()]
                actual = result.get("trace", [])
                checks["trace_file_matches"] = trace == actual if actual else bool(trace) and trace[0].get("event_type") == "terminal_without_tool"
                checks["manifest_source_matches"] = manifest.get("source_sha256") == source
                if result["status"] == "completed":
                    saves = [event for event in actual if event["tool"] == "save_plan" and event["result"]["ok"]]
                    checks["save_receipt"] = bool(saves) and saves[-1]["result"]["data"] == result["saved_plan"]
                if case == "C06-repeat":
                    checks["repeat_no_new_work"] = result["cached"] and result["model_decisions"] == result["tool_attempts"] == result["model_requests_sent"] == 0
                if mode == "live" and case in ["C01-normal", "C04-replan-budget"]:
                    checks["remote_decisions"] = result["model_mode"] == "live" and all(event["action_source"] == "remote_model" and event.get("decision_summary") for event in actual)
                    paths.append(tuple(event["tool"] for event in actual))
                if not all(checks.values()):
                    problem("EVIDENCE_INCONSISTENT", directory, ",".join(key for key, value in checks.items() if not value))
                index_path = root / directory / "current.json"
                if index_path.exists():
                    index = json.loads(index_path.read_text())
                    generation = root / directory / index["directory"]
                    generation.resolve().relative_to((root / directory).resolve())
                    saved_manifest = json.loads((root / directory / index["manifest"]).read_text())
                    if saved_manifest != manifest or any(hashlib.sha256((generation / name).read_bytes()).hexdigest() != digest for name, digest in saved_manifest["artifacts_sha256"].items()):
                        problem("EXPORT_GENERATION_INVALID", directory)
            except (OSError, ValueError, KeyError, TypeError):
                problem("EVIDENCE_INCONSISTENT", directory, "required output/trace fields missing or inconsistent")
    if len(set(paths)) < 2:
        problem("DYNAMIC_PATHS_MISSING", "examples/live", "two distinct remote-model tool paths required")
    link_count = 0
    for path in [root / "README.md", root / "SUBMISSION.md", *sorted((root / "docs").glob("*.md"))]:
        if not path.is_file():
            continue
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            target = target.split("#", 1)[0].strip("<>")
            if not target or "://" in target:
                continue
            link_count += 1
            if not (path.parent / target).exists():
                problem("BROKEN_DOCUMENT_LINK", path.relative_to(root), target)
    private_files = [str(p.relative_to(root)) for folder in ["src", "tests", "tools", "data", "docs", "examples"]
                     for p in (root / folder).rglob(".env*") if p.name != ".env.example"]
    if private_files:
        problem("PRIVATE_CONFIGURATION_IN_DELIVERY", private_files[0])
    metadata = read("submission.json") or {}
    remote = metadata.get("github_url")
    if not isinstance(remote, str) or not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?", remote):
        blockers.append("GITHUB_URL_REQUIRED")
    try:
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (subprocess.SubprocessError, OSError):
        commit = None
        blockers.append("FINAL_COMMIT_REQUIRED")
    if metadata.get("visibility") == "private" and metadata.get("reviewer_access_confirmed") is not True:
        blockers.append("PRIVATE_REVIEWER_ACCESS_REQUIRED")
    if metadata.get("visibility") not in ["public", "private"]:
        blockers.append("REPOSITORY_VISIBILITY_UNCONFIRMED")
    candidate = metadata.get("candidate", {})
    if type(candidate.get("actual_work_hours")) not in [int, float] or candidate["actual_work_hours"] <= 0:
        blockers.append("ACTUAL_PERSONAL_TIME_UNRECORDED")
    if not isinstance(candidate.get("verified_participation"), list) or not isinstance(candidate.get("personal_code_changes"), str) or not isinstance(candidate.get("verification_disclosure"), str):
        blockers.append("PERSONAL_CONTRIBUTION_DISCLOSURE_REQUIRED")
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "problem_number": 2,
            "source_sha256": source, "tests_sha256": test_hash, "reports": reports,
            "technical_materials_ready": not problems, "formal_submission_ready": not problems and not blockers,
            "github_url": remote, "final_commit_sha": commit, "document_local_links_checked": link_count,
            "problems": problems, "formal_blockers": blockers,
            "scope": "Exam delivery and declared bounded CLI validation; no assertion of production certification or independent candidate ability."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=Path("examples/delivery-check.json"))
    parser.add_argument("--require-formal", action="store_true")
    args = parser.parse_args()
    report = audit(args.root)
    output = args.output if args.output.is_absolute() else args.root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "reports"}, ensure_ascii=False, indent=2))
    return 0 if report["formal_submission_ready" if args.require_formal else "technical_materials_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

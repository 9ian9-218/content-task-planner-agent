"""Run the real acceptance suite and save its output and measured counts."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def digest(directory):
    sys.path.insert(0, str(ROOT / "src"))
    from content_planner.tools import fingerprint
    return fingerprint({p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in sorted((ROOT / directory).rglob("*.py"))})


def main():
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    source_before, tests_before = digest("src"), digest("tests")
    started_at = datetime.now(timezone.utc).isoformat()
    before = time.perf_counter()
    result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    elapsed = time.perf_counter() - before
    source_after, tests_after = digest("src"), digest("tests")
    unchanged = source_before == source_after and tests_before == tests_after
    verification_exit = result.returncode if result.returncode else (0 if unchanged else 1)
    match = re.search(r"Ran (\d+) tests?", result.stdout)
    skipped = re.search(r"skipped=(\d+)", result.stdout)
    count = int(match.group(1)) if match else None
    summary = {"started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
               "command": command, "PYTHONPATH": "src", "python": platform.python_version(),
               "platform": platform.platform(), "exit_code": verification_exit, "test_command_exit_code": result.returncode,
               "inputs_unchanged": unchanged, "tests_run": count,
               "skipped": int(skipped.group(1)) if skipped else 0,
               "passed": count - (int(skipped.group(1)) if skipped else 0) if count is not None and verification_exit == 0 else None,
               "wall_seconds": round(elapsed, 6), "source_sha256": source_before, "tests_sha256": tests_before,
               "source_after_sha256": source_after, "tests_after_sha256": tests_after}
    destination = ROOT / "examples"
    destination.mkdir(exist_ok=True)
    (destination / "test-results.txt").write_text("Command: " + " ".join(command) + "\nPYTHONPATH=src\nStarted: " + started_at + "\n\n" + result.stdout, encoding="utf-8")
    (destination / "test-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(result.stdout, end="")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return verification_exit


if __name__ == "__main__":
    raise SystemExit(main())

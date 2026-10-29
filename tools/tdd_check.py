"""Record real red/green commands; never generate passing results by assumption."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["red", "green"])
    parser.add_argument("label")
    parser.add_argument("test")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = root / "examples" / "tdd"
    destination.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "unittest", args.test, "-v"]
    env = dict(os.environ, PYTHONPATH=str(root / "src") + os.pathsep + str(root))
    started = datetime.now(timezone.utc).isoformat()
    before = time.perf_counter()
    result = subprocess.run(command, cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    elapsed = time.perf_counter() - before
    name = f"{args.label}-{args.phase}.txt"
    sequence = 2
    while (destination / name).exists():
        name = f"{args.label}-{args.phase}-{sequence}.txt"
        sequence += 1
    (destination / name).write_text(result.stdout, encoding="utf-8")
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    record = {"phase": args.phase, "label": args.label, "command": command,
              "started_at": started, "exit_code": result.returncode,
              "elapsed_seconds": round(elapsed, 6), "source_sha256": digest.hexdigest(), "output": name}
    with (destination / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(result.stdout, end="")
    print(json.dumps(record, ensure_ascii=False))
    return 0 if (result.returncode != 0 if args.phase == "red" else result.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())

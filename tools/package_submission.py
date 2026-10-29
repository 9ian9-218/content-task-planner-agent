"""Build a credential-free, hash-manifested submission directory and ZIP."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_delivery import audit

TOP_FILES = ["README.md", "CONTEXT.md", "requirements.txt", ".env.example", ".gitignore", "runtime.lock.json", "submission.json", "SUBMISSION.md"]
DIRECTORIES = ["src", "tests", "tools", "data", "docs", "examples", ".github"]
EXCLUDED = {".git", ".local", ".venv", "__pycache__", "node_modules"}


def submission_files(root):
    paths = [root / name for name in TOP_FILES if (root / name).is_file() and not (root / name).is_symlink()]
    for folder in DIRECTORIES:
        if not (root / folder).is_dir() or (root / folder).is_symlink():
            continue
        for directory, folders, names in os.walk(root / folder, followlinks=False):
            folders[:] = [name for name in folders if name not in EXCLUDED and not Path(directory, name).is_symlink()]
            for name in names:
                path = Path(directory, name)
                if path.is_symlink() or name.startswith(".env") and name != ".env.example" or path.suffix in [".pyc", ".pyo", ".db", ".pem", ".key"] or ".sqlite3" in name:
                    continue
                paths.append(path)
    return sorted(paths)


def configured_keys(root):
    values = {os.environ["MODEL_API_KEY"].encode()} if os.environ.get("MODEL_API_KEY") else set()
    path = root / ".env.local"
    if path.is_file():
        for line in path.read_text().splitlines():
            key, separator, value = line.strip().partition("=")
            value = value.strip().strip("\"'")
            if separator and key == "MODEL_API_KEY" and value:
                values.add(value.encode())
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    root, destination = args.root.resolve(), args.destination.resolve()
    if destination == root or any(destination.is_relative_to(root / name) for name in DIRECTORIES):
        parser.error("destination must be outside the submission source directories")
    if destination.exists():
        parser.error("destination already exists; refusing to overwrite")
    report = audit(root)
    if args.require_ready and not report["technical_materials_ready"]:
        print(json.dumps({"status": "blocked", "reason_code": "TECHNICAL_MATERIALS_NOT_READY", "problems": report["problems"]}, ensure_ascii=False))
        return 1
    files, keys = submission_files(root), configured_keys(root)
    for path in files:
        if path.stat().st_size > 100 * 1024 * 1024:
            print(json.dumps({"status": "blocked", "reason_code": "FILE_SIZE_LIMIT", "path": path.relative_to(root).as_posix()}))
            return 1
        if any(key in path.read_bytes() for key in keys):
            print(json.dumps({"status": "blocked", "reason_code": "SECRET_VALUE_IN_ARTIFACT", "path": path.relative_to(root).as_posix()}))
            return 1
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                if sum(info.file_size for info in archive.infolist()) > 200 * 1024 * 1024 or any(any(key in archive.read(info) for key in keys) for info in archive.infolist()):
                    print(json.dumps({"status": "blocked", "reason_code": "UNSAFE_NESTED_ARCHIVE", "path": path.relative_to(root).as_posix()}))
                    return 1
    repository = destination / "repository"
    repository.mkdir(parents=True)
    hashes = {}
    for path in files:
        relative = path.relative_to(root)
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[relative.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "problem_number": 2,
                "source_sha256": report["source_sha256"], "tests_sha256": report["tests_sha256"],
                "technical_materials_ready": report["technical_materials_ready"],
                "formal_submission_ready": report["formal_submission_ready"], "formal_blockers": report["formal_blockers"],
                "files_sha256": hashes, "note": "Manifest excludes itself. Git metadata, secrets, caches and local databases are excluded."}
    (repository / "PACKAGE-MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    archive_path = destination / "content-task-planner-agent.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(repository.rglob("*")):
            if path.is_file():
                archive.write(path, "content-task-planner-agent/" + path.relative_to(repository).as_posix())
    result = {"status": "packaged", "repository_directory": str(repository), "archive": str(archive_path),
              "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(), "archive_bytes": archive_path.stat().st_size,
              "file_count": len(hashes), "technical_materials_ready": report["technical_materials_ready"],
              "formal_submission_ready": report["formal_submission_ready"], "formal_blockers": report["formal_blockers"]}
    (destination / "package-receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (destination / "delivery-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

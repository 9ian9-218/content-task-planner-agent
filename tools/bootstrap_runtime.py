"""Install only the checksum-pinned interpreter into an isolated project directory."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def safe_archive_member(member, destination):
    # Python 3.10.12's backported filter resolves relative symbolic links at the archive root.
    # Validate their actual target first, then retain the standard permission/path filtering.
    if member.issym() or member.islnk():
        root = Path(destination).resolve()
        if Path(member.linkname).is_absolute():
            raise ValueError("absolute archive link")
        base = (root / member.name).parent if member.issym() else root
        (base / member.linkname).resolve().relative_to(root)
        checked = copy.copy(member)
        checked.linkname = "python"
        filtered = tarfile.data_filter(checked, destination)
        filtered.linkname = member.linkname
        return filtered
    return tarfile.data_filter(member, destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--destination", type=Path, default=ROOT / ".local/runtime/python")
    args = parser.parse_args()
    lock = json.loads((ROOT / "runtime.lock.json").read_text(encoding="utf-8"))
    target = args.destination.resolve()
    if target.exists():
        marker = target / "runtime.provenance.json"
        if marker.is_file() and json.loads(marker.read_text())["sha256"] == lock["sha256"] and (target / "bin/python3").is_file():
            print(json.dumps({"status": "ready", "reused": True, "destination": str(target)}))
            return 0
        print(json.dumps({"status": "failed", "reason_code": "RUNTIME_DESTINATION_EXISTS"}))
        return 2
    temporary = None
    try:
        archive = args.archive
        if archive is None:
            temporary = tempfile.TemporaryDirectory(prefix="content-planner-download-")
            archive = Path(temporary.name) / "python.tar.gz"
            with urllib.request.urlopen(lock["url"], timeout=60) as response, archive.open("wb") as output:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > lock["size"]:
                        raise ValueError("size mismatch")
                    output.write(chunk)
        checksum = hashlib.sha256()
        with archive.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                checksum.update(chunk)
        if archive.stat().st_size != lock["size"] or checksum.hexdigest() != lock["sha256"]:
            print(json.dumps({"status": "failed", "reason_code": "RUNTIME_CHECKSUM_MISMATCH"}))
            return 2
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".runtime-stage-", dir=target.parent) as stage:
            with tarfile.open(archive) as package:
                if any(not (m.name == "python" or m.name.startswith("python/")) for m in package.getmembers()):
                    raise ValueError("unexpected archive root")
                package.extractall(stage, filter=safe_archive_member)
            unpacked = Path(stage) / "python"
            if not (unpacked / "bin/python3").is_file():
                raise ValueError("missing interpreter")
            (unpacked / "runtime.provenance.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
            unpacked.rename(target)
        print(json.dumps({"status": "ready", "destination": str(target), "sha256": checksum.hexdigest()}))
        return 0
    except (OSError, ValueError, RuntimeError, tarfile.TarError):
        print(json.dumps({"status": "failed", "reason_code": "RUNTIME_SETUP_FAILED"}))
        return 2
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

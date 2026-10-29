"""Reproducible CLI; all result files come from actual executions."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import uuid
from urllib.parse import urlsplit, urlunsplit

from .agent import Planner, empty_result
from .model import LiveModel, MockModel
from .runtime import diagnose_runtime
from .state import Store, StateError
from .tools import ROOT, fingerprint


def read_json_input(path):
    with Path(path).open("rb") as handle:
        raw = handle.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("input exceeds 1 MB")
    try:
        value = json.loads(raw)
    except RecursionError:
        raise ValueError("JSON nesting exceeds limit") from None
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError("JSON nesting exceeds 32 levels")
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        pending.extend((child, depth + 1) for child in children)
    return value


def load_environment(path):
    """Read only project variables, without executing shell or expanding values."""
    if not path.is_file():
        return
    allowed = {"MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key in allowed and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


def _write_export_files(result, original_input, destination, manifest):
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in [("input.json", original_input), ("manifest.json", manifest), ("output.json", result)]:
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    trace = result.get("trace", [])
    exported_trace = trace or [{"event_type": "terminal_without_tool", "action_source": "runtime", "tool": None,
                               "status": result["status"], "reason_code": result.get("reason_code"),
                               "missing_fields": result.get("missing_fields", []), "note": "本次未执行业务工具，该事件是程序终态记录。"}]
    (directory / "trace.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in exported_trace), encoding="utf-8")
    (directory / "decisions.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in result.get("decisions", [])), encoding="utf-8")
    lines = ["# 实际执行 Trace", "", f"模式：{result['model_mode']}；状态：{result['status']}；缓存：{result.get('cached', False)}", "",
             "| 步骤 | 动作来源及已观察步骤 | 工具和参数 | 返回摘要 | 实际后续执行或终止 | 公开依据 |", "| --- | --- | --- | --- | --- | --- |"]
    for index, event in enumerate(trace):
        following = trace[index + 1]["tool"] if index + 1 < len(trace) else f"结束：{result.get('reason_code') or result['status']}"
        observed = event["result"]
        summary = {"ok": observed["ok"], "error_code": observed["error_code"], "data": observed["data"]}
        cells = [str(index + 1), event["action_source"] + f" / 已观察 {event.get('observed_after_step')}", event["tool"] + " " + json.dumps(event["parameters"], ensure_ascii=False),
                 json.dumps(summary, ensure_ascii=False), following, event["decision_summary"]]
        lines.append("| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
    lines += ["", f"终止原因：{result.get('reason_code') or result['status']}。{result.get('summary', '')}"]
    (directory / "trace.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if result.get("saved_plan"):
        plan = result["saved_plan"]
        content = f"# {plan['goal']}\n\n{plan['outline']}\n\n## 脚本草稿\n\n{plan['script']['text']}\n\n素材：{', '.join(plan['source_ids'])}\n\n模拟费用：{plan['cost_units']}\n\n保存回执：{plan['plan_id']}\n\n仅表示方案已保存，活动未执行。\n"
        (directory / "plan.md").write_text(content, encoding="utf-8")
    elif (directory / "plan.md").exists():
        (directory / "plan.md").unlink()


def export_result(result, original_input, destination, manifest):
    """Publish an immutable generation; current.json is the authoritative commit point."""
    directory = Path(destination)
    generations = directory / ".runs"
    generations.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix=".export-", dir=generations) as temporary:
        staged = Path(temporary)
        _write_export_files(result, original_input, staged, manifest)
        hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(staged.iterdir()) if path.name != "manifest.json"}
        published_manifest = dict(manifest, export_id=generation, artifacts_sha256=hashes)
        (staged / "manifest.json").write_text(json.dumps(published_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for path in staged.iterdir():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        final = generations / generation
        os.replace(staged, final)
    # Root files remain convenient mirrors for existing consumers. Verify current.json for consistency.
    files = {path.name: path.read_bytes() for path in final.iterdir()}
    files["current.json"] = (json.dumps({"export_id": generation, "directory": f".runs/{generation}",
        "manifest": f".runs/{generation}/manifest.json"}, ensure_ascii=False, indent=2) + "\n").encode()
    for name, payload in files.items():
        descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, directory / name)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if "plan.md" not in files:
        (directory / "plan.md").unlink(missing_ok=True)
    return {"export_id": generation, "directory": str(final)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="内容任务规划 Agent；业务工具为合成模拟。")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--production", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--db", required=True)
    backup.add_argument("--output", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--input", required=True)
    restore.add_argument("--db", required=True)
    for command in ["status", "cancel"]:
        control = commands.add_parser(command)
        control.add_argument("--db", required=True)
        control.add_argument("--request-id", required=True)
    run = commands.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--mode", choices=["mock", "live"], default="mock")
    run.add_argument("--scenario")
    run.add_argument("--as-of")
    run.add_argument("--db", default=".local/state.sqlite3")
    run.add_argument("--output-dir")
    run.add_argument("--max-steps", type=int, default=12)
    run.add_argument("--max-tool-calls", type=int, default=12)
    run.add_argument("--timeout", type=float, default=180)
    run.add_argument("--model-timeout", type=float, default=30)
    run.add_argument("--journal-mode", choices=["delete", "wal"])
    run.add_argument("--env-file", default=".env.local")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--production", action="store_true", help="Enforce runtime prerequisites; this is not a production certification.")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = diagnose_runtime()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if args.production and not report["release_ready"] else 0
    if args.command in ["status", "cancel"]:
        planner = None
        try:
            if not Path(args.db).is_file():
                raise StateError("STATE_NOT_FOUND")
            planner = Planner(args.db, read_only=args.command == "status")
            result = planner.status(args.request_id) if args.command == "status" else planner.cancel(args.request_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result.get("runtime_state", result.get("status")) == "not_found" else 0
        except (sqlite3.Error, OSError) as exc:
            print(json.dumps({"status": "failed", "reason_code": getattr(exc, "code", "STATE_UNAVAILABLE")}))
            return 2
        finally:
            if planner:
                planner.close()
    if args.command in ["backup", "restore"]:
        try:
            if args.command == "backup":
                if not Path(args.db).is_file():
                    raise StateError("STATE_NOT_FOUND")
                receipt = Store.restore(args.db, args.output)
            else:
                receipt = Store.restore(args.input, args.db)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        except (sqlite3.Error, OSError) as exc:
            print(json.dumps({"status": "failed", "reason_code": getattr(exc, "code", "BACKUP_FAILED")}))
            return 2
    if args.production and not diagnose_runtime()["release_ready"]:
        print(json.dumps(empty_result("invalid_request", "UNSUPPORTED_RUNTIME", args.mode,
                                      summary="运行时未通过版本门槛；请先执行 doctor --production。"), ensure_ascii=False))
        return 2
    if args.output_dir:
        targets = [Path(args.output_dir, name).resolve() for name in
                   ["input.json", "output.json", "manifest.json", "trace.jsonl", "decisions.jsonl", "trace.md", "plan.md", "current.json"]]
        protected = [Path(value).resolve() for value in [args.input, args.db, args.env_file, args.scenario] if value]
        protected += [Path(str(Path(args.db).resolve()) + suffix) for suffix in ["-journal", "-wal", "-shm"]]
        collisions = any(target == source or (target.exists() and source.exists() and target.samefile(source))
                         for target in targets for source in protected)
        if collisions:
            print(json.dumps(empty_result("invalid_request", "INVALID_CONFIGURATION", args.mode,
                                          summary="导出文件与输入、配置或数据库路径冲突；尚未执行工具，也不会写入该输出目录。"), ensure_ascii=False))
            return 2
    model = MockModel() if args.mode == "mock" else LiveModel()
    original_input, scenario = None, {}
    try:
        load_environment(Path(args.env_file))
        model = MockModel() if args.mode == "mock" else LiveModel()
        original_input = read_json_input(args.input)
        scenario = read_json_input(args.scenario) if args.scenario else {}
    except (OSError, ValueError):
        result = empty_result("invalid_request", "INVALID_INPUT", args.mode, summary="输入或配置文件无法读取，或不是有效 JSON。")
    else:
        planner = None
        try:
            if not isinstance(scenario, dict):
                raise ValueError("scenario must be a JSON object")
            planner = Planner(args.db, as_of=args.as_of, scenario=scenario, max_decisions=args.max_steps,
                              max_tool_calls=args.max_tool_calls, wall_timeout_seconds=args.timeout, model_timeout_seconds=args.model_timeout,
                              journal_mode=args.journal_mode)
            result = planner.run(original_input, model=model, resume=args.resume)
        except StateError as exc:
            if exc.code == "STATE_BUSY":
                result = empty_result("incomplete", exc.code, args.mode, original_input, "状态库繁忙，尚未派发模型或工具；可稍后重试。")
                result["retryable"] = True
            else:
                result = empty_result("invalid_request", exc.code, args.mode, original_input, "状态库版本或格式不受支持，未执行任务。")
        except (sqlite3.Error, OSError):
            result = empty_result("incomplete", "PERSISTENCE_FAILED", args.mode, original_input, "本地状态无法保存，不能宣称方案已保存。")
        except (ValueError, TypeError, KeyError):
            result = empty_result("invalid_request", "INVALID_CONFIGURATION", args.mode, original_input, "运行配置或合成费用/耗时表无效。")
        finally:
            if planner:
                planner.close()
    manifest = {"executed_at": datetime.now(timezone.utc).isoformat(), "command": [sys.executable, "-m", "content_planner"] + (argv if argv is not None else sys.argv[1:]),
                "mode": args.mode, "action_source": model.action_source, "python": platform.python_version(),
                "business_as_of": result.get("as_of"), "scenario": scenario, "state_database": str(args.db),
                "source_sha256": fingerprint({path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in sorted((ROOT / "src").rglob("*.py"))})}
    endpoint = urlsplit(getattr(model, "base_url", ""))
    manifest.update(model_name=getattr(model, "model", "deterministic_mock"),
                    model_endpoint=urlunsplit((endpoint.scheme, endpoint.netloc.rsplit("@", 1)[-1], endpoint.path, "", "")),
                    model_requests_sent=result.get("model_requests_sent", 0), actual_result_mode=result["model_mode"],
                    data_versions=result.get("data_versions"), prompt_sha256=result.get("prompt_sha256"))
    if args.output_dir:
        try:
            export_result(result, original_input, args.output_dir, manifest)
        except OSError:
            result["export_errors"] = ["EXPORT_FAILED"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 4 if result.get("export_errors") else (0 if result["status"] == "completed" else (2 if result["status"] in ["needs_input", "invalid_request"] else 3))


if __name__ == "__main__":
    raise SystemExit(main())

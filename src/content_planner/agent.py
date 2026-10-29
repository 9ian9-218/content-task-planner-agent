from datetime import datetime, timedelta, timezone
import copy
import json
import math
import sqlite3
import time
import uuid

from .state import Store, StateError, is_busy
from .model import ModelError
from .runtime import diagnose_runtime
from .tools import Tools, definitions, fingerprint, parse_time


SYSTEM_PROMPT = """你是内容任务规划 Agent。根据用户需求选择工具，观察工具返回后选择下一步。
硬性预算、截止时间不可修改。quality_first 优先询价 standard，cost_first 优先询价 economy；不可行时可询价其他档位。
只有估算工具知道最终报价。资料事实必须引用实际素材；业务工具是本地合成模拟。
工具失败后保留已有成功结果，可重试、换方案或停止。保存成功才代表完成。
每个动作用 decision_summary 写一句公开依据，无需内部思维过程。
若无法继续，单独调用 finish_task，status 为 needs_input 或 incomplete，reason_code 引用实际观察原因，summary 简要说明。
仅缺少用户必要字段或检索不到资料时选 needs_input，分别使用 MISSING_INFORMATION、MATERIALS_MISSING。
所有已知档位都超预算时选 incomplete / BUDGET_EXCEEDED，均超过截止时选 incomplete / DEADLINE_EXCEEDED；不得改为待补预算。
工具失败选 incomplete 并使用实际工具 error_code。错误码必须使用工具契约列出的原始大写形式。
finish_task 不与其他动作同时调用；不要用普通文字或 Markdown 代码块结束任务。保存工具成功才可完成。
"""


def empty_result(status, code, mode, brief=None, summary=""):
    brief = brief if isinstance(brief, dict) else {}
    request_id = brief.get("request_id")
    budget = brief.get("budget_units")
    return {"request_id": request_id if isinstance(request_id, str) and request_id.strip() else None,
            "status": status, "reason_code": code, "summary": summary,
            "missing_fields": [], "invalid_fields": [], "steps": [], "task_plan": [], "trace": [], "decisions": [],
            "operations": [], "scripts": {}, "quotes": {}, "materials": [], "plan_id": None,
            "saved_plan": None, "save_status": "not_attempted", "model_mode": mode,
            "model_decisions": 0, "model_requests_sent": 0, "tool_attempts": 0, "errors": [code] if code else [],
            "elapsed_seconds": 0, "estimated_finish_at": None, "business_now": None, "cached": False,
            "cost": {"spent_units": 0, "remaining_units": 0, "projected_total_units": 0,
                     "budget_units": budget if type(budget) is int and budget >= 0 else None}}


class Planner:
    def __init__(self, db_path, as_of=None, scenario=None, max_decisions=12, max_tool_calls=12, wall_timeout_seconds=180,
                 lease_seconds=60, heartbeat_interval=10, max_active_tasks=2, read_only=False, event_sink=None, model_timeout_seconds=30,
                 journal_mode=None):
        self.as_of = as_of or datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat()
        if parse_time(self.as_of).tzinfo is None or type(max_decisions) is not int or type(max_tool_calls) is not int or type(wall_timeout_seconds) not in [int, float] or not math.isfinite(wall_timeout_seconds) or min(max_decisions, max_tool_calls, wall_timeout_seconds) <= 0:
            raise ValueError("runtime limits must be positive and as-of must have timezone")
        if any(type(v) not in [int, float] or not math.isfinite(v) or v <= 0 for v in [lease_seconds, heartbeat_interval]) or heartbeat_interval >= lease_seconds / 2 or type(max_active_tasks) is not int or max_active_tasks <= 0:
            raise ValueError("invalid lease or capacity policy")
        if event_sink is not None and not callable(event_sink):
            raise ValueError("event_sink must be callable")
        if type(model_timeout_seconds) not in [int, float] or not math.isfinite(model_timeout_seconds) or model_timeout_seconds <= 0:
            raise ValueError("model timeout must be finite and positive")
        if journal_mode not in [None, "delete", "wal"] or journal_mode == "wal" and not diagnose_runtime()["checks"]["sqlite_fix_level_confirmed"]:
            raise ValueError("journal mode must be delete or explicitly verified wal")
        self.event_sink = event_sink
        self.store = Store(db_path, lease_seconds, heartbeat_interval, max_active_tasks, read_only, journal_mode=journal_mode)
        self.scenario = {} if scenario is None else scenario
        self.max_decisions = max_decisions
        self.max_tool_calls = max_tool_calls
        self.wall_timeout_seconds = wall_timeout_seconds
        self.model_timeout_seconds = model_timeout_seconds

    def close(self):
        self.store.close()

    def _emit(self, context, event, **fields):
        if self.event_sink is not None:
            value = {"event": event, "request_id": context["request_id"], "run_id": context["run_id"],
                     "recorded_at": datetime.now(timezone.utc).isoformat(), **fields}
            try:
                self.event_sink(copy.deepcopy(value))
            except Exception:
                context.setdefault("observer_errors", [])
                if len(context["observer_errors"]) < 8:
                    context["observer_errors"].append("OBSERVER_FAILED")

    def get_plan(self, plan_id):
        return self.store.get_plan(plan_id)

    def list_plans(self):
        return self.store.list_plans()

    def status(self, request_id):
        existing, observed_lease = self.store.observe(request_id)
        if existing is None:
            return {"request_id": request_id, "runtime_state": "not_found"}
        state = observed_lease if existing["status"] == "running" else "terminal"
        pending = existing.get("model_inflight") or existing.get("tool_inflight")
        uncertain = pending and ((pending.get("kind") == "model" and pending.get("model_mode") != "mock") or
                                 (pending.get("kind") == "tool" and pending.get("replay_policy") != "local_transactional"))
        if state == "expired":
            runtime_state = "reconcile_required" if uncertain else "recoverable"
        else:
            runtime_state = "running" if state == "active" else ("legacy_recovery_required" if state == "legacy" else existing["status"])
        if state != "active" and uncertain:
            runtime_state = "reconcile_required"
        if self.store.cancel_requested(request_id):
            runtime_state = "cancelled" if existing.get("reason_code") == "CANCELLED" else "cancel_requested"
            if state == "expired" and uncertain:
                runtime_state = "cancel_requested_reconcile_required"
        confirmed = self._blocked_result(existing, "STATUS_ONLY", existing.get("model_mode", "mock"))
        return {"request_id": request_id, "task_status": existing["status"], "runtime_state": runtime_state,
                "lease_state": state, "lease_generation": existing.get("lease_generation"), "cost": confirmed["cost"],
                "pending_actions": [pending] if pending else [], "plan_id": existing.get("plan_id")}

    def cancel(self, request_id):
        return self.store.cancel(request_id)

    def run(self, brief, model, resume=False):
        try:
            return self._run(brief, model, resume)
        finally:
            self.store.release()

    def _blocked_result(self, existing, code, mode):
        result = empty_result("incomplete", code, mode, existing.get("brief"))
        result.update(copy.deepcopy(existing))
        operations = result.get("operations", [])
        spent = sum(op["result"]["charged_units"] for op in operations)
        quote = next((op["result"]["data"] for op in reversed(operations) if op["tool"] == "estimate_cost" and op["result"]["ok"]), None)
        reusable = quote and any(s["profile"] == quote["profile"] and s["source_ids"] == quote["material_ids"] for s in result.get("scripts", {}).values())
        remaining = 0 if not quote or reusable else quote["remaining_units"]
        result.update(status="incomplete", reason_code=code, cached=True, model_decisions=0, model_requests_sent=0,
                      tool_attempts=0, steps=operations, summary="本次未派发模型或业务动作，保留已确认成果；原因：" + code)
        result["cost"] = {"spent_units": spent, "remaining_units": remaining, "projected_total_units": spent + remaining,
                          "budget_units": existing["brief"]["budget_units"]}
        result["save_status"] = "saved" if result.get("plan_id") else ("failed" if "SAVE_FAILED" in result.get("errors", []) else "not_attempted")
        result["task_plan"] = Tools(result, self.scenario).task_plan()
        return result

    def _run(self, brief, model, resume=False):
        started = time.perf_counter()
        starting_model_requests = getattr(model, "request_count", 0)
        if not isinstance(brief, dict):
            return empty_result("invalid_request", "INVALID_INPUT", model.mode, summary="活动需求必须是 JSON 对象。")
        brief = dict(brief)
        if isinstance(brief.get("topic"), str):
            brief["topic"] = brief["topic"].strip()
        missing = [name for name in ["topic", "budget_units", "deadline"] if brief.get(name) is None or brief.get(name) == ""]
        if missing:
            result = empty_result("needs_input", "MISSING_INFORMATION", model.mode, brief)
            result.update(missing_fields=missing, elapsed_seconds=round(time.perf_counter() - started, 6))
            return result
        invalid = []
        if not isinstance(brief["topic"], str) or len(brief["topic"]) > 200:
            invalid.append("topic")
        if type(brief["budget_units"]) is not int or brief["budget_units"] < 0:
            invalid.append("budget_units")
        if "request_id" in brief and (not isinstance(brief["request_id"], str) or not brief["request_id"].strip() or len(brief["request_id"]) > 128):
            invalid.append("request_id")
        if brief.get("preference", "quality_first") not in ["quality_first", "cost_first"]:
            invalid.append("preference")
        timezone_missing = False
        try:
            deadline = parse_time(brief["deadline"])
            timezone_missing = deadline.tzinfo is None or deadline.utcoffset() is None
            brief["deadline"] = deadline.astimezone(timezone(timedelta(hours=8))).isoformat() if not timezone_missing else deadline.isoformat()
        except (TypeError, ValueError, AttributeError, OverflowError):
            invalid.append("deadline")
        if invalid or timezone_missing:
            result = empty_result("invalid_request" if invalid else "needs_input", "INVALID_INPUT" if invalid else "MISSING_TIMEZONE", model.mode, brief)
            result.update(invalid_fields=invalid, missing_fields=["deadline.timezone"] if timezone_missing else [],
                          elapsed_seconds=round(time.perf_counter() - started, 6))
            return result
        brief.setdefault("preference", "quality_first")
        brief.setdefault("request_id", "request-" + fingerprint(brief)[:24])
        existing, observed_lease = self.store.observe(brief["request_id"])
        if existing:
            if existing["input_fingerprint"] != fingerprint(brief):
                return empty_result("invalid_request", "REQUEST_ID_CONFLICT", model.mode, brief)
            if existing["status"] != "completed" and self.store.cancel_requested(brief["request_id"]):
                return self._blocked_result(existing, "CANCELLED", model.mode)
            if existing["status"] == "running":
                lease_state = observed_lease
                if lease_state == "active":
                    return self._blocked_result(existing, "REQUEST_IN_PROGRESS", model.mode)
                if lease_state == "legacy":
                    return self._blocked_result(existing, "LEGACY_RECOVERY_REQUIRED", model.mode)
                if not resume:
                    return self._blocked_result(existing, "RECOVERY_REQUIRED", model.mode)
            if resume and existing["status"] != "completed" and existing.get("prompt_sha256") != fingerprint(SYSTEM_PROMPT):
                existing.update(status="incomplete", reason_code="PROMPT_VERSION_MISMATCH", cached=True,
                                model_decisions=0, model_requests_sent=0, tool_attempts=0,
                                summary="提示词版本已改变，未继续执行；已有成功结果和费用保留，请检查版本后处理。",
                                attempted_prompt_sha256=fingerprint(SYSTEM_PROMPT))
                return existing
            if not resume or existing["status"] == "completed":
                existing.update(cached=True, original_model_decisions=existing["model_decisions"], model_decisions=0,
                                original_model_requests_sent=existing.get("model_requests_sent", 0), model_requests_sent=0,
                                tool_attempts=0, elapsed_seconds=round(time.perf_counter() - started, 6),
                                cache_note="返回持久化历史结果；trace 是原运行记录，本次没有模型或业务工具执行。")
                return existing
        context = existing or {"run_id": uuid.uuid4().hex, "request_id": brief["request_id"], "brief": dict(brief),
                   "input_fingerprint": fingerprint(brief), "as_of": self.as_of, "operations": [], "trace": [],
                   "materials": [], "quotes": {}, "scripts": {}, "plan_id": None, "saved_plan": None,
                   "status": "running", "reason_code": None, "model_mode": model.mode,
                   "model_decisions": 0, "errors": []}
        context["cached"] = False
        context["replayed_decisions"] = 0
        if existing and "task_limits" not in context:
            return self._blocked_result(existing, "LEGACY_RECOVERY_REQUIRED", model.mode)
        context.setdefault("task_limits", {"max_model_attempts": self.max_decisions, "max_tool_attempts": self.max_tool_calls})
        context.setdefault("task_counters", {"model_attempts": 0, "tool_attempts": 0})
        context.setdefault("missing_fields", [])
        context.setdefault("invalid_fields", [])
        context.setdefault("decisions", [])
        context.setdefault("prompt_sha256", fingerprint(SYSTEM_PROMPT))
        context["executed_at"] = datetime.now(timezone.utc).isoformat()
        trace_offset = len(context["trace"])
        if existing:
            context.update(status="running", reason_code=None, model_decisions=0, resumed=True,
                           run_id=uuid.uuid4().hex, model_mode=model.mode)
        tools = Tools(context, self.scenario)
        if existing and parse_time(self.as_of) > tools.now():
            elapsed_minutes = sum(op["result"]["elapsed_business_minutes"] for op in context["operations"])
            context["as_of"] = (parse_time(self.as_of) - timedelta(minutes=elapsed_minutes)).isoformat()
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"brief": brief, "resume": bool(existing), "previous_errors": context["errors"]}, ensure_ascii=False)}]
        if existing:
            for index, event in enumerate(context["trace"]):
                if not event["result"]["ok"]:
                    continue
                call_id = event.get("tool_call_id", f"history-{index}")
                messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": event["tool"], "arguments": json.dumps(event["parameters"], ensure_ascii=False)}}]})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(event["result"], ensure_ascii=False)})
        try:
            if not self.store.claim(context, resume=bool(existing)):
                return self._blocked_result(self.store.get_request(brief["request_id"]) or context, "REQUEST_IN_PROGRESS", model.mode)
        except StateError as exc:
            result = self._blocked_result(existing or context, exc.code, model.mode)
            if exc.code == "STATE_BUSY":
                result["retryable"] = True
            return result
        except sqlite3.OperationalError as exc:
            if is_busy(exc):
                result = self._blocked_result(existing or context, "STATE_BUSY", model.mode)
                result["retryable"] = True
                return result
            raise

        def summarize():
            elapsed = time.perf_counter() - started
            context["elapsed_seconds"] = round(elapsed, 6)
            context["runtime_limit_seconds"] = self.wall_timeout_seconds
            context["model_timeout_seconds"] = self.model_timeout_seconds
            context["runtime_limit_exceeded"] = elapsed >= self.wall_timeout_seconds
            context["finished_at"] = datetime.now(timezone.utc).isoformat()
            context["model_requests_sent"] = getattr(model, "request_count", 0) - starting_model_requests
            context["tool_attempts"] = len(context["trace"]) - trace_offset
            latest_quote = next((op["result"]["data"] for op in reversed(context["operations"]) if op["tool"] == "estimate_cost" and op["result"]["ok"]), None)
            tools.select_artifacts(latest_quote)
            try:
                projection = tools.projection(latest_quote["material_ids"], latest_quote["profile"]) if latest_quote else None
            except OverflowError:
                projection = None
            try:
                business_now = tools.now().isoformat()
            except OverflowError:
                business_now = None
                context.update(status="incomplete", reason_code="TIME_RANGE_EXCEEDED")
                if "TIME_RANGE_EXCEEDED" not in context["errors"]:
                    context["errors"].append("TIME_RANGE_EXCEEDED")
            remaining = projection["remaining_units"] if projection and context["status"] != "completed" else 0
            context["cost"] = {"spent_units": tools.spent(), "remaining_units": remaining,
                               "projected_total_units": tools.spent() + remaining, "budget_units": brief["budget_units"],
                               "budget_gap_units": max(0, tools.spent() + remaining - brief["budget_units"])}
            context["business_now"] = business_now
            context["estimated_finish_at"] = business_now if context["status"] == "completed" else (projection["estimated_finish_at"] if projection else None)
            context["steps"] = context["operations"]
            context["task_plan"] = tools.task_plan()
            context["save_status"] = "saved" if context["plan_id"] else ("failed" if "SAVE_FAILED" in context["errors"] else "not_attempted")

        while context["status"] == "running":
            if self.store.cancel_requested(context["request_id"]):
                context.update(status="incomplete", reason_code="CANCELLED")
                context["errors"].append("CANCELLED")
                break
            try:
                self.store.assert_owner()
            except StateError:
                context.update(status="incomplete", reason_code="LEASE_LOST", persistence_error=True)
                break
            remaining_seconds = self.wall_timeout_seconds - (time.perf_counter() - started)
            if remaining_seconds <= 0:
                context.update(status="incomplete", reason_code="RUN_TIMEOUT")
                break
            pending = context.get("pending_decision")
            if pending:
                context["replayed_decisions"] += 1
            else:
                if context["model_decisions"] >= self.max_decisions or len(context["trace"]) - trace_offset >= self.max_tool_calls:
                    context.update(status="incomplete", reason_code="STEP_LIMIT")
                    break
                if (context["task_counters"]["model_attempts"] >= context["task_limits"]["max_model_attempts"] or
                        context["task_counters"]["tool_attempts"] >= context["task_limits"]["max_tool_attempts"]):
                    context.update(status="incomplete", reason_code="TASK_BUDGET_EXHAUSTED" if existing else "STEP_LIMIT")
                    break
                context["model_decisions"] += 1
                context["task_counters"]["model_attempts"] += 1
                observed_before_decision = len(context["trace"]) or None
                context["model_inflight"] = {"action_id": uuid.uuid4().hex, "kind": "model", "run_id": context["run_id"],
                                             "model_mode": model.mode, "started_at": datetime.now(timezone.utc).isoformat()}
                try:
                    self.store.persist(context)
                except sqlite3.Error as exc:
                    code = exc.code if isinstance(exc, StateError) else "PERSISTENCE_FAILED"
                    context.update(status="incomplete", reason_code=code, persistence_error=True)
                    context["errors"].append(code)
                    break
                try:
                    decision = model.decide(messages, definitions(), timeout=min(self.model_timeout_seconds, remaining_seconds))
                except ModelError as exc:
                    if not (exc.details or {}).get("outcome_unknown"):
                        context["model_inflight"] = None
                    context.update(status="incomplete", reason_code=exc.code, summary=str(exc))
                    if exc.details:
                        context["model_error"] = exc.details
                    context["errors"].append(exc.code)
                    break
                context["model_inflight"] = None
                context["decisions"].append({"run_id": context["run_id"], "decision_index": context["model_decisions"],
                                             "recorded_at": datetime.now(timezone.utc).isoformat(), "model_mode": model.mode,
                                             "action_source": model.action_source, "observed_after_step": observed_before_decision,
                                             "tool_calls": decision.get("tool_calls", []), "finish": decision.get("finish"),
                                             "response_model": decision.get("model"), "usage": decision.get("usage"),
                                             "system_fingerprint": decision.get("system_fingerprint")})
                pending = {"decision": decision, "next_call": 0, "decision_index": context["model_decisions"],
                           "decision_run_id": context["run_id"], "observed_after_step": observed_before_decision,
                           "model_mode": model.mode, "action_source": model.action_source}
                context["pending_decision"] = pending
                try:
                    self.store.persist(context)
                except sqlite3.Error as exc:
                    selected = decision.get("tool_calls", [])
                    saving = len(selected) == 1 and selected[0]["function"]["name"] == "save_plan"
                    code = exc.code if isinstance(exc, StateError) else ("SAVE_FAILED" if saving else "PERSISTENCE_FAILED")
                    context.update(status="incomplete", reason_code=code, persistence_error=True,
                                   summary="执行准备无法持久化，未派发后续工具；此前成功成果和费用保留。")
                    context["errors"].append(code)
                    break
                known_tools = {item["function"]["name"] for item in definitions()}
                self._emit(context, "model_decision_committed", next_tools=[call["function"]["name"] if call["function"]["name"] in known_tools else "UNKNOWN_TOOL" for call in decision.get("tool_calls", [])])
            decision = pending["decision"]
            observed_before_decision = pending["observed_after_step"]
            if time.perf_counter() - started >= self.wall_timeout_seconds:
                context.update(status="incomplete", reason_code="RUN_TIMEOUT")
                break
            calls = decision.get("tool_calls", [])[pending["next_call"]:]
            if self.store.cancel_requested(context["request_id"]):
                context.update(status="incomplete", reason_code="CANCELLED")
                context["errors"].append("CANCELLED")
                break
            if not calls and decision.get("finish"):
                finish = decision["finish"]
                context["pending_decision"] = None
                context.update(status=finish["status"] if finish["status"] in ["needs_input", "incomplete"] else "incomplete",
                               reason_code=finish.get("reason_code", "MODEL_STOPPED"), summary=finish.get("summary", ""))
                break
            if not calls:
                context.update(status="incomplete", reason_code="MODEL_RESPONSE_INVALID", pending_decision=None)
                context["errors"].append("MODEL_RESPONSE_INVALID")
                break
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            for call_index, call in enumerate(calls, start=pending["next_call"]):
                if self.store.cancel_requested(context["request_id"]):
                    context.update(status="incomplete", reason_code="CANCELLED")
                    context["errors"].append("CANCELLED")
                    break
                try:
                    self.store.assert_owner()
                except StateError:
                    context.update(status="incomplete", reason_code="LEASE_LOST", persistence_error=True)
                    break
                if time.perf_counter() - started >= self.wall_timeout_seconds:
                    context.update(status="incomplete", reason_code="RUN_TIMEOUT")
                    break
                if len(context["trace"]) - trace_offset >= self.max_tool_calls:
                    context.update(status="incomplete", reason_code="STEP_LIMIT")
                    break
                if context["task_counters"]["tool_attempts"] >= context["task_limits"]["max_tool_attempts"]:
                    context.update(status="incomplete", reason_code="TASK_BUDGET_EXHAUSTED" if existing else "STEP_LIMIT")
                    break
                try:
                    args = json.loads(call["function"]["arguments"])
                except (json.JSONDecodeError, TypeError, RecursionError):
                    args = None
                name = call["function"]["name"]
                logical_args = {key: value for key, value in args.items() if key != "decision_summary"} if isinstance(args, dict) else args
                if isinstance(logical_args, dict) and isinstance(logical_args.get("material_ids"), list) and all(isinstance(v, str) for v in logical_args["material_ids"]):
                    logical_args["material_ids"] = sorted(logical_args["material_ids"])
                failed_attempts = sum(op.get("run_id") == context["run_id"] and op["tool"] == name and
                                      op["parameters"] == logical_args and op["result"].get("retryable", False)
                                      for op in context["operations"])
                if failed_attempts >= 3:
                    context.update(status="incomplete", reason_code="RETRY_EXHAUSTED", pending_decision=None)
                    context["errors"].append("RETRY_EXHAUSTED")
                    break
                context["task_counters"]["tool_attempts"] += 1
                context["tool_inflight"] = {"action_id": uuid.uuid4().hex, "kind": "tool", "tool": name,
                    "operation_key": fingerprint([context["request_id"], name, logical_args, context["data_versions"]]),
                    "replay_policy": tools.execution_domain, "run_id": context["run_id"], "started_at": datetime.now(timezone.utc).isoformat()}
                try:
                    self.store.persist(context)
                except sqlite3.Error as exc:
                    code = exc.code if isinstance(exc, StateError) else ("SAVE_FAILED" if name == "save_plan" else "PERSISTENCE_FAILED")
                    context.update(status="incomplete", reason_code=code, persistence_error=True)
                    context["errors"].append(code)
                    break
                before_step = copy.deepcopy(context)
                result = tools.execute(name, args)
                self._emit(context, "tool_result_uncommitted", tool=name if name in {item["function"]["name"] for item in definitions()} else "UNKNOWN_TOOL")
                if not result["ok"]:
                    context["errors"].append(result["error_code"])
                context["trace"].append({"step": len(context["trace"]) + 1, "model_mode": pending["model_mode"],
                                         "run_id": context["run_id"], "tool_call_id": call["id"],
                                         "decision_index": pending["decision_index"], "decision_run_id": pending["decision_run_id"], "recorded_at": datetime.now(timezone.utc).isoformat(),
                                         "action_source": pending["action_source"], "tool": name, "parameters": args,
                                         "decision_summary": args.get("decision_summary", "") if isinstance(args, dict) else "工具参数不是有效 JSON 对象。", "result": result,
                                         "observed_after_step": observed_before_decision})
                pending["next_call"] = call_index + 1
                context["tool_inflight"] = None
                if pending["next_call"] == len(decision.get("tool_calls", [])):
                    context["pending_decision"] = None
                if name == "save_plan" and result["ok"]:
                    context["status"] = "completed"
                    summarize()
                try:
                    self.store.persist(context, context["saved_plan"] if name == "save_plan" and result["ok"] else None)
                except sqlite3.Error as exc:
                    failed_event = context["trace"][-1]
                    context.clear()
                    context.update(before_step)
                    code = exc.code if isinstance(exc, StateError) else ("SAVE_FAILED" if name == "save_plan" else "PERSISTENCE_FAILED")
                    result = dict(result, ok=False, data={}, error_code=code, charged_units=0,
                                  elapsed_business_minutes=tools.catalog["failure_minutes"])
                    failed_event["result"] = result
                    context["operations"].append({"operation_id": result["operation_id"], "tool": name,
                                                  "parameters": args, "status": "failed", "result": result})
                    context["trace"].append(failed_event)
                    context["errors"].append(code)
                    context.update(status="incomplete", reason_code=code, persistence_error=True,
                                   summary="本地事务未提交；保留此前成功结果。失败状态可能无法写入数据库，请保存本次输出。")
                    break
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
                if context["status"] == "completed":
                    break
        summarize()
        if not context.get("persistence_error"):
            try:
                self.store.persist(context)
            except sqlite3.Error:
                context["persistence_error"] = True
                context["errors"].append("FINAL_SNAPSHOT_FAILED")
        return context

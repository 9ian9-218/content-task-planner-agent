import json
import http.client
import os
import urllib.request
import urllib.error
from urllib.parse import urlsplit


FINISH_CODES = ["MISSING_INFORMATION", "MATERIALS_MISSING", "BUDGET_EXCEEDED", "DEADLINE_EXCEEDED", "SEARCH_FAILED", "ESTIMATE_FAILED",
                "GENERATION_FAILED", "SAVE_FAILED", "UNKNOWN_TOOL", "INVALID_ARGUMENTS", "UNKNOWN_PROFILE", "UNKNOWN_MATERIAL",
                "QUOTE_MISMATCH", "DEPENDENCY_MISMATCH", "TIME_RANGE_EXCEEDED", "PERSISTENCE_FAILED", "MODEL_STOPPED"]


def _valid_finish(value):
    return (isinstance(value, dict) and set(value) == {"status", "reason_code", "summary"} and
            all(isinstance(value[key], str) and value[key].strip() for key in value) and
            value["status"] in ["needs_input", "incomplete"] and value["reason_code"] in FINISH_CODES and
            (value["status"] == "needs_input") == (value["reason_code"] in ["MISSING_INFORMATION", "MATERIALS_MISSING"]))


class ModelError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


class LiveModel:
    mode = "live"
    action_source = "remote_model"

    def __init__(self, api_key=None, base_url=None, model=None, request_budget=None):
        self.api_key = os.environ.get("MODEL_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.environ.get("MODEL_BASE_URL", "http://100.73.185.103:8045/v1")
        self.model = model or os.environ.get("MODEL_NAME", "claude-opus-4.6")
        self.request_count = 0
        if request_budget is not None and (not isinstance(request_budget, dict) or type(request_budget.get("remaining")) is not int or request_budget["remaining"] < 0):
            raise ValueError("request budget must contain a non-negative integer remaining count")
        self.request_budget = request_budget

    def decide(self, messages, tools, timeout=30):
        if self.request_budget is not None and self.request_budget["remaining"] <= 0:
            raise ModelError("EVALUATION_BUDGET_EXHAUSTED", "评估批次的请求额度已耗尽，未派发新请求。")
        if not self.api_key:
            raise ModelError("MODEL_UNAVAILABLE", "未配置 MODEL_API_KEY；live 模式不会切换到 Mock。")
        finish_tool = {"type": "function", "function": {"name": "finish_task", "description": "无法继续时明确结束；报告资料缺口或未完成原因。不得用此动作宣称已保存。",
                       "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["needs_input", "incomplete"], "description": "仅缺字段或资料选 needs_input；预算/截止无法满足或工具失败选 incomplete。"},
                           "reason_code": {"type": "string", "enum": FINISH_CODES, "description": "使用列出的标准大写错误码，不得自行改写。"}, "summary": {"type": "string"}},
                           "required": ["status", "reason_code", "summary"], "additionalProperties": False}}}
        payload = {"model": self.model, "messages": messages, "tools": list(tools) + [finish_tool], "stream": False,
                   "temperature": 0, "max_tokens": 2000, "tool_choice": "auto"}
        if urlsplit(self.base_url).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        url = self.base_url.rstrip("/") + "/chat/completions"
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"})
        request.add_unredirected_header("Authorization", "Bearer " + self.api_key)
        try:
            if self.request_budget is not None:
                self.request_budget["remaining"] -= 1
            self.request_count += 1
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            value = str((exc.headers or {}).get("Retry-After", "")).strip()
            retry_after = int(value) if value.isascii() and value.isdigit() and len(value) <= 9 else None
            details = {"http_status": exc.code, "retry_after_seconds": retry_after,
                       "category": "transient" if exc.code in [429, 503] else "permanent",
                       "retryable": exc.code in [429, 503], "outcome_unknown": False}
            raise ModelError("MODEL_UNAVAILABLE", f"模型接口请求失败（HTTP {exc.code}），未切换 Mock。", details) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            status = getattr(exc, "code", None)
            raise ModelError("MODEL_UNAVAILABLE", f"模型接口请求失败（HTTP {status or '不可用'}），未切换 Mock。",
                             {"category": "unknown", "outcome_unknown": True, "retryable": False}) from None
        try:
            if len(raw) > 2_000_000:
                raise ValueError("response too large")
            result = json.loads(raw)
            choice = result["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") not in ["tool_calls", "stop"]:
                raise ValueError("incomplete or unsupported finish reason")
            metadata = {"model": result.get("model"), "usage": result.get("usage"), "system_fingerprint": result.get("system_fingerprint")}
            if message.get("tool_calls"):
                calls = message["tool_calls"]
                if choice["finish_reason"] != "tool_calls" or not isinstance(calls, list) or any(c.get("type") != "function" or not isinstance(c["id"], str) or not c["id"].strip() or not isinstance(c["function"]["name"], str) or not c["function"]["name"].strip() or not isinstance(c["function"]["arguments"], str) for c in calls):
                    raise ValueError("invalid tool calls")
                if len({call["id"] for call in calls}) != len(calls):
                    raise ValueError("duplicate tool call ids")
                if any(call["function"]["name"] == "finish_task" for call in calls):
                    if len(calls) != 1:
                        raise ValueError("finish must be the only action")
                    finish = json.loads(calls[0]["function"]["arguments"])
                    if not _valid_finish(finish):
                        raise ValueError("invalid finish action")
                    return dict(metadata, finish=finish)
                return dict(metadata, tool_calls=calls)
            if choice["finish_reason"] != "stop":
                raise ValueError("missing tool calls")
            finish = json.loads(message.get("content") or "{}")
            if not _valid_finish(finish):
                raise ValueError("invalid finish")
            return dict(metadata, finish=finish)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
            raise ModelError("MODEL_RESPONSE_INVALID", "模型返回格式不符合工具调用或结束契约。") from None


class MockModel:
    """Deterministic offline policy. It is never evidence of LLM decisions."""
    mode = "mock"
    action_source = "mock_policy"

    def decide(self, messages, tools, timeout=30):
        brief = json.loads(messages[1]["content"])["brief"]
        observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        latest = {item["tool_name"]: item for item in observations}
        if observations and not observations[-1]["ok"]:
            return {"finish": {"status": "incomplete", "reason_code": observations[-1]["error_code"], "summary": "工具失败，保留此前成功的结果。"}}
        if "search_materials" not in latest:
            name, arguments, why = "search_materials", {"query": brief["topic"]}, "先获取可引用的素材事实。"
        elif "estimate_cost" not in latest:
            ids = [m["id"] for m in latest["search_materials"]["data"]["materials"]]
            if not ids:
                return {"finish": {"status": "needs_input", "reason_code": "MATERIALS_MISSING", "summary": "检索没有可用资料，请补充素材或调整主题。"}}
            preferred = "economy" if brief.get("preference") == "cost_first" else "standard"
            name, arguments, why = "estimate_cost", {"material_ids": ids, "profile": preferred}, "根据用户软偏好先询价候选方案。"
        elif "generate_script" not in latest:
            quote = latest["estimate_cost"]["data"]
            if not quote["feasible"]:
                tried = {item["data"]["profile"] for item in observations if item["tool_name"] == "estimate_cost" and item["ok"]}
                alternatives = [p for p in ["standard", "economy"] if p not in tried]
                if not alternatives:
                    return {"finish": {"status": "incomplete", "reason_code": "DEADLINE_EXCEEDED" if quote["deadline_gap_minutes"] else "BUDGET_EXCEEDED", "summary": "已询价的候选均不满足硬约束，需要用户调整需求。"}}
                name, arguments, why = "estimate_cost", {"material_ids": quote["material_ids"], "profile": alternatives[0]}, "观察到候选超限，询价另一个可用方案。"
            else:
                name, arguments, why = "generate_script", {k: quote[k] for k in ["quote_id", "material_ids", "profile"]}, "依据可行报价生成草稿。"
        else:
            script = latest["generate_script"]["data"]
            name, arguments, why = "save_plan", {"quote_id": script["quote_id"], "script_id": script["script_id"], "outline": "展示已核验功能，并提供可编辑脚本。"}, "草稿已生成，保存方案。"
        arguments["decision_summary"] = why
        return {"tool_calls": [{"id": f"mock-{len(messages)}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}

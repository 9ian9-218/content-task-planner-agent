"""Controlled HTTP responses. No network calls and no claim of LLM inference."""
import io
import json


def respond(request, timeout):
    payload = json.loads(request.data)
    observations = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
    if not observations:
        name, args = "search_materials", {"query": "P01"}
    else:
        last = observations[-1]
        data = last["data"]
        if not last["ok"]:
            message = {"role": "assistant", "content": json.dumps({"status": "incomplete", "reason_code": last["error_code"], "summary": "工具失败，保留成功结果。"})}
            return io.BytesIO(json.dumps({"model": "http-fixture", "choices": [{"finish_reason": "stop", "message": message}]}).encode())
        if last["tool_name"] == "search_materials":
            name, args = "estimate_cost", {"material_ids": ["P01"], "profile": "standard"}
        elif last["tool_name"] == "estimate_cost":
            if not data["feasible"]:
                name, args = "estimate_cost", {"material_ids": ["P01"], "profile": "economy"}
            else:
                name, args = "generate_script", {key: data[key] for key in ["quote_id", "material_ids", "profile"]}
        else:
            name, args = "save_plan", {"quote_id": data["quote_id"], "script_id": data["script_id"], "outline": "HTTP协议受控测试的活动提纲。"}
    args["decision_summary"] = "依据实际工具返回选择本次动作，受控协议测试。"
    call = {"id": f"fixture-{len(payload['messages'])}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return io.BytesIO(json.dumps({"model": "http-fixture", "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [call]}}]}).encode())

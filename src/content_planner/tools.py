from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import uuid


ROOT = Path(__file__).resolve().parents[2]


class ToolRejected(Exception):
    def __init__(self, code, data=None):
        super().__init__(code)
        self.code = code
        self.data = data or {}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def definitions():
    fields = {
        "search_materials": {"query": {"type": "string"}},
        "estimate_cost": {"material_ids": {"type": "array", "items": {"type": "string"}}, "profile": {"type": "string", "enum": ["standard", "economy"]}},
        "generate_script": {"quote_id": {"type": "string"}, "material_ids": {"type": "array", "items": {"type": "string"}}, "profile": {"type": "string", "enum": ["standard", "economy"]}},
        "save_plan": {"quote_id": {"type": "string"}, "script_id": {"type": "string"}, "outline": {"type": "string"}},
    }
    descriptions = {
        "search_materials": "检索合成素材事实和来源。空结果表示资料缺口，不得编造产品事实。",
        "estimate_cost": "对一个候选档位询价并估算时间；最终报价只有本工具确认。超限不是工具故障。",
        "generate_script": "使用同一候选的有效报价和素材生成本地模板草稿，按模拟费用表计费。",
        "save_plan": "核验并保存已有脚本与活动提纲；只有成功回执才代表方案已保存。",
    }
    return [{"type": "function", "function": {"name": name, "description": descriptions[name],
            "parameters": {"type": "object", "properties": dict(properties, decision_summary={"type": "string", "description": "一句话说明依据，无需内部思维过程"}),
                           "required": list(properties) + ["decision_summary"], "additionalProperties": False}}}
            for name, properties in fields.items()]


class Tools:
    # Local template/lookup operations have no remote side effect; simulated fees commit with SQLite.
    execution_domain = "local_transactional"

    def __init__(self, context, scenario=None):
        self.context = context
        self.scenario = {} if scenario is None else scenario
        if not isinstance(self.scenario, dict) or not set(self.scenario) <= {"profiles", "failures"}:
            raise ValueError("scenario must be an object")
        profiles = self.scenario.get("profiles", {})
        failures = self.scenario.get("failures", {})
        tool_names = {item["function"]["name"] for item in definitions()}
        if not isinstance(profiles, dict) or not isinstance(failures, dict):
            raise ValueError("profiles and failures must be objects")
        if any(name not in tool_names or value != "always" for name, value in failures.items()):
            raise ValueError("failures accepts known tools with mode 'always' only")
        self.materials = json.loads((ROOT / "data/materials.json").read_text(encoding="utf-8"))
        self.catalog = json.loads((ROOT / "data/tool_catalog.json").read_text(encoding="utf-8"))
        for profile, values in profiles.items():
            if profile not in self.catalog["profiles"] or not isinstance(values, dict) or not set(values) <= {"units", "minutes", "description"}:
                raise ValueError("unknown profile or configuration field")
            self.catalog["profiles"][profile].update(values)
        if context.get("catalog_snapshot"):
            self.catalog = context["catalog_snapshot"]
            self.materials = context["materials_snapshot"]
        else:
            context["catalog_snapshot"] = self.catalog
            context["materials_snapshot"] = self.materials
        for profile in self.catalog["profiles"].values():
            if any(type(profile[key]) is not int or profile[key] < 0 for key in ["units", "minutes"]):
                raise ValueError("fees and duration must be non-negative integers")
            if not isinstance(profile["description"], str) or not profile["description"].strip():
                raise ValueError("profile description must be a nonempty string")
        if any(type(self.catalog[key]) is not int or self.catalog[key] < 0 for key in ["free_tool_minutes", "failure_units", "failure_minutes"]) or self.catalog["failure_units"] != 0:
            raise ValueError("unsupported fee configuration")
        self.free_minutes = self.catalog["free_tool_minutes"]
        context["data_versions"] = {"materials": self.materials["version"], "catalog": self.catalog["version"],
                                    "catalog_sha256": fingerprint(self.catalog), "materials_sha256": fingerprint(self.materials)}

    def now(self, extra_minutes=0):
        elapsed = sum(item["result"]["elapsed_business_minutes"] for item in self.context["operations"])
        return parse_time(self.context["as_of"]) + timedelta(minutes=elapsed + extra_minutes)

    def spent(self):
        return sum(item["result"]["charged_units"] for item in self.context["operations"])

    def task_plan(self, saving=False):
        names = ["search_materials", "estimate_cost", "generate_script", "save_plan"]
        tasks = []
        for index, name in enumerate(names):
            last = next((op for op in reversed(self.context["operations"]) if op["tool"] == name), None)
            status = "succeeded" if saving and name == "save_plan" else (last["status"] if last else "pending")
            tasks.append({"task": name, "depends_on": names[index - 1:index] if index else [], "status": status})
        return tasks

    def select_artifacts(self, latest_quote):
        saved = self.context.get("saved_plan")
        active = saved["script"] if saved else latest_quote
        profile = active.get("profile") if active else None
        ids = (active.get("source_ids") or active.get("material_ids")) if active else []
        quote_id = active.get("quote_id") if active else None
        for script in self.context["scripts"].values():
            script["selected"] = script["profile"] == profile and script["source_ids"] == ids
        for quote in self.context["quotes"].values():
            quote["selected"] = quote["quote_id"] == quote_id
        latest_quote_op = next((op["operation_id"] for op in reversed(self.context["operations"])
                                if op["tool"] == "estimate_cost" and op["result"]["ok"] and op["result"]["data"]["quote_id"] == quote_id), None)
        for operation in self.context["operations"]:
            data = operation["result"]["data"]
            selected = False
            if operation["result"]["ok"]:
                if operation["tool"] == "generate_script":
                    selected = self.context["scripts"][data["script_id"]]["selected"]
                    data["selected"] = selected
                elif operation["tool"] == "estimate_cost":
                    selected = operation["operation_id"] == latest_quote_op
                    data["selected"] = selected
                elif operation["tool"] == "search_materials":
                    selected = bool(set(ids or []) & {material["id"] for material in data["materials"]})
                elif operation["tool"] == "save_plan":
                    selected = bool(saved)
            operation["selected"] = selected

    def execute(self, name, arguments):
        args = {key: value for key, value in arguments.items() if key != "decision_summary"} if isinstance(arguments, dict) else None
        try:
            schemas = {item["function"]["name"]: item["function"]["parameters"] for item in definitions()}
            if name not in schemas:
                raise ToolRejected("UNKNOWN_TOOL")
            schema = schemas[name]
            if not isinstance(arguments, dict) or set(arguments) != set(schema["required"]):
                raise ToolRejected("INVALID_ARGUMENTS")
            for key, value in arguments.items():
                spec = schema["properties"][key]
                if spec["type"] == "string" and (not isinstance(value, str) or not value.strip()):
                    raise ToolRejected("INVALID_ARGUMENTS")
                if spec["type"] == "array" and (not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value) or len(set(value)) != len(value)):
                    raise ToolRejected("INVALID_ARGUMENTS")
                if "enum" in spec and value not in spec["enum"]:
                    raise ToolRejected("UNKNOWN_PROFILE")
            if "material_ids" in args:
                args["material_ids"] = sorted(args["material_ids"])
                seen_ids = {m["id"] for m in self.context["materials"]}
                if not set(args["material_ids"]) <= seen_ids:
                    raise ToolRejected("UNKNOWN_MATERIAL")
            self.guard(name, args)
            previous = next((op for op in self.context["operations"] if name == "generate_script" and op["tool"] == name and op["parameters"] == args and op["result"]["ok"]), None)
            if previous:
                data, charged, minutes, error = previous["result"]["data"], 0, 0, None
            elif self.scenario.get("failures", {}).get(name):
                data, charged, minutes = {}, self.catalog["failure_units"], self.catalog["failure_minutes"]
                error = {"search_materials": "SEARCH_FAILED", "estimate_cost": "ESTIMATE_FAILED", "generate_script": "GENERATION_FAILED", "save_plan": "SAVE_FAILED"}[name]
            else:
                data, charged, minutes = getattr(self, name)(**args)
                error = None
        except ToolRejected as exc:
            data, charged, minutes, error = exc.data, 0, 0, exc.code
        except OverflowError:
            data, charged, minutes, error = {}, 0, 0, "TIME_RANGE_EXCEEDED"
        result = {"ok": error is None, "tool_name": name, "data": data, "error_code": error,
                  "retryable": error in {"SEARCH_FAILED", "ESTIMATE_FAILED", "GENERATION_FAILED", "SAVE_FAILED"},
                  "charged_units": charged, "elapsed_business_minutes": minutes, "operation_id": uuid.uuid4().hex}
        if error is None and name == "generate_script" and previous:
            result.update(reused=True, reused_from_operation_id=previous["operation_id"])
        operation = {"operation_id": result["operation_id"], "run_id": self.context["run_id"], "tool": name, "parameters": args,
                     "status": "succeeded" if result["ok"] else "failed", "result": result}
        self.context["operations"].append(operation)
        return result

    def guard(self, name, args):
        if name == "generate_script":
            quote = self.context["quotes"].get(args["quote_id"])
            if not quote or quote["material_ids"] != args["material_ids"] or quote["profile"] != args["profile"]:
                raise ToolRejected("QUOTE_MISMATCH")
            projection = self.projection(args["material_ids"], args["profile"])
            if projection["budget_gap_units"]:
                raise ToolRejected("BUDGET_EXCEEDED", projection)
            if projection["deadline_gap_minutes"]:
                raise ToolRejected("DEADLINE_EXCEEDED", projection)
        elif name == "save_plan":
            script = self.context["scripts"].get(args["script_id"])
            quote = self.context["quotes"].get(args["quote_id"])
            if not script or not quote or script["quote_id"] != quote["quote_id"]:
                raise ToolRejected("DEPENDENCY_MISMATCH")
            if self.spent() > self.context["brief"]["budget_units"]:
                raise ToolRejected("BUDGET_EXCEEDED")
            if self.now(self.free_minutes) > parse_time(self.context["brief"]["deadline"]):
                raise ToolRejected("DEADLINE_EXCEEDED")

    def projection(self, material_ids, profile, extra_minutes=0):
        settings = self.catalog["profiles"][profile]
        reusable = any(s["source_ids"] == material_ids and s["profile"] == profile for s in self.context["scripts"].values())
        remaining = 0 if reusable else settings["units"]
        finish = self.now(extra_minutes + (0 if reusable else settings["minutes"]) + self.free_minutes)
        total = self.spent() + remaining
        return {"remaining_units": remaining, "projected_total_units": total,
                "estimated_finish_at": finish.isoformat(), "budget_gap_units": max(0, total - self.context["brief"]["budget_units"]),
                "deadline_gap_minutes": max(0, (finish - parse_time(self.context["brief"]["deadline"])).total_seconds() / 60)}

    def search_materials(self, query):
        found = [m for m in self.materials["materials"] if any(k.lower() in query.lower() for k in m["keywords"])]
        retained = {m["id"]: m for m in self.context["materials"]}
        retained.update({m["id"]: m for m in found})
        self.context["materials"] = list(retained.values())
        return {"materials": found, "profiles": {k: v["description"] for k, v in self.catalog["profiles"].items()}}, 0, self.free_minutes

    def estimate_cost(self, material_ids, profile):
        data = {"quote_id": "q-" + fingerprint([material_ids, profile])[:16], "material_ids": material_ids,
                "profile": profile, **self.projection(material_ids, profile, self.free_minutes)}
        data["feasible"] = data["budget_gap_units"] == 0 and data["deadline_gap_minutes"] == 0
        self.context["quotes"][data["quote_id"]] = data
        return data, 0, self.free_minutes

    def generate_script(self, quote_id, material_ids, profile):
        sources = [m for m in self.context["materials"] if m["id"] in material_ids]
        facts = [fact for material in sources for fact in material["facts"]]
        script = {"script_id": "s-" + fingerprint([material_ids, profile])[:16], "quote_id": quote_id,
                  "profile": profile, "source_ids": material_ids, "title": self.context["brief"]["topic"],
                  "text": "；".join(facts) + "。欢迎根据实际需要编辑内容。", "generator": "local_template"}
        if profile == "standard":
            script["text"] = "今天介绍以下已核验功能：" + script["text"] + " 建议使用前核对素材，并按需修改脚本。"
        self.context["scripts"][script["script_id"]] = script
        settings = self.catalog["profiles"][profile]
        return script, settings["units"], settings["minutes"]

    def save_plan(self, quote_id, script_id, outline):
        script = self.context["scripts"][script_id]
        plan = {"plan_id": "plan-" + uuid.uuid4().hex, "request_id": self.context["request_id"],
                "goal": self.context["brief"]["topic"], "outline": outline, "script": script,
                "source_ids": script["source_ids"], "cost_units": self.spent(), "saved_at": self.now(self.free_minutes).isoformat(),
                "tasks": self.task_plan(saving=True)}
        self.context["plan_id"] = plan["plan_id"]
        self.context["saved_plan"] = plan
        return plan, 0, self.free_minutes

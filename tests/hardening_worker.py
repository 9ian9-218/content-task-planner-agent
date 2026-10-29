"""Subprocess fixture: pause at a public model seam after a confirmed paid result."""
import json
from pathlib import Path
import sys
import time

from content_planner import Planner
from content_planner.model import MockModel


class WaitAfterGeneration(MockModel):
    mode = sys.argv[5] if len(sys.argv) > 5 else "mock"
    action_source = "controlled_subprocess_model_fixture"
    blocked = False

    def decide(self, messages, tools, timeout=30):
        if messages[-1]["role"] == "tool" and not self.blocked and not (len(sys.argv) > 6 and sys.argv[6] in ["decision", "tool"]):
            observation = json.loads(messages[-1]["content"])
            if observation["tool_name"] == "generate_script" and observation["ok"]:
                self.blocked = True
                Path(sys.argv[2]).write_text("confirmed script persisted")
                until = time.monotonic() + 30
                while not Path(sys.argv[3]).exists() and time.monotonic() < until:
                    time.sleep(0.01)
        return super().decide(messages, tools, timeout)


if __name__ == "__main__":
    def observer(event):
        decision_stage = sys.argv[6] == "decision" and event["event"] == "model_decision_committed" and event.get("next_tools") == ["save_plan"]
        tool_stage = sys.argv[6] == "tool" and event["event"] == "tool_result_uncommitted" and event.get("tool") == "generate_script"
        if decision_stage or tool_stage:
            Path(sys.argv[2]).write_text("model decision committed")
            until = time.monotonic() + 30
            while not Path(sys.argv[3]).exists() and time.monotonic() < until:
                time.sleep(.01)
    options = {"event_sink": observer, "max_decisions": 4 if sys.argv[6] == "decision" else 12} if len(sys.argv) > 6 else {}
    capacity_case = len(sys.argv) > 6 and sys.argv[6] == "capacity"
    planner = Planner(sys.argv[1], as_of="2026-11-01T10:00:00+08:00",
                      lease_seconds=60 if capacity_case else .6,
                      heartbeat_interval=10 if capacity_case else .1, **options)
    try:
        result = planner.run(json.loads(Path(sys.argv[4]).read_text()), WaitAfterGeneration())
        print(json.dumps(result), flush=True)
    finally:
        planner.close()

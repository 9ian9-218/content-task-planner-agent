import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_acceptance import AS_OF, BRIEF


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_cli_exports_the_actual_result_and_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            input_path = directory / "input.json"
            input_path.write_text(json.dumps(BRIEF), encoding="utf-8")
            destination = directory / "output"
            command = [sys.executable, "-m", "content_planner", "run", "--input", str(input_path),
                       "--mode", "mock", "--as-of", AS_OF, "--db", str(directory / "state.sqlite3"),
                       "--output-dir", str(destination)]
            result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["status"], "completed")
            exported = json.loads((destination / "output.json").read_text())
            self.assertEqual(exported["plan_id"], output["plan_id"])
            trace = [json.loads(line) for line in (destination / "trace.jsonl").read_text().splitlines()]
            self.assertTrue(trace[-1]["result"]["ok"])
            self.assertTrue((destination / "plan.md").exists())
            self.assertEqual(json.loads((destination / "manifest.json").read_text())["mode"], "mock")

    def test_cli_reports_invalid_input_and_unwritable_storage_as_json(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            input_path = directory / "input.json"
            for body, database, code, reason in [('{', directory / 'ok.sqlite3', 2, 'INVALID_INPUT'),
                                                 (json.dumps(BRIEF), directory / 'not-a-directory' / 'db.sqlite3', 3, 'PERSISTENCE_FAILED')]:
                input_path.write_text(body, encoding="utf-8")
                (directory / "not-a-directory").write_text("blocking file", encoding="utf-8")
                result = subprocess.run([sys.executable, "-m", "content_planner", "run", "--input", str(input_path), "--db", str(database)],
                                        cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(json.loads(result.stdout)["reason_code"], reason)

    def test_cli_rejects_negative_synthetic_fees_instead_of_reducing_spent(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            input_path, scenario_path = directory / "input.json", directory / "scenario.json"
            input_path.write_text(json.dumps(BRIEF), encoding="utf-8")
            scenario_path.write_text('{"profiles":{"standard":{"units":-8}}}', encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "content_planner", "run", "--input", str(input_path), "--scenario", str(scenario_path),
                                     "--as-of", AS_OF, "--db", str(directory / "state.sqlite3")], cwd=ROOT,
                                    env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["reason_code"], "INVALID_CONFIGURATION")
            self.assertEqual(output["cost"]["spent_units"], 0)

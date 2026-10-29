"""Stability behavior through the public CLI, Planner and external seams."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import tarfile
import hashlib
import unittest
from unittest.mock import patch

from content_planner.__main__ import main
from content_planner import Planner
from content_planner.model import MockModel
from tests.test_acceptance import AS_OF, BRIEF


def invoke(*arguments):
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(io.StringIO()):
        code = main(list(arguments))
    return code, json.loads(output.getvalue())


class HardeningTests(unittest.TestCase):
    def test_doctor_blocks_unsupported_release_runtime_without_reading_credentials_or_network(self):
        cases = [("3.10.12", "3.37.2", False), ("3.14.1", "3.51.3", True),
                 ("3.14.1", "3.51.2", False), ("3.14.1", "3.50.7", True)]
        for python, sqlite, ready in cases:
            with self.subTest(python=python, sqlite=sqlite), \
                    patch("platform.python_version", return_value=python), patch("sqlite3.sqlite_version", sqlite), \
                    patch.dict(os.environ, {"MODEL_API_KEY": "private-doctor-sentinel"}), patch("urllib.request.urlopen") as transport:
                code, result = invoke("doctor", "--production")
                self.assertEqual(code, 0 if ready else 2)
                self.assertEqual(result["release_ready"], ready)
                self.assertEqual(result["runtime"]["python"], python)
                self.assertEqual(result["runtime"]["sqlite"], sqlite)
                self.assertNotIn("private-doctor-sentinel", json.dumps(result))
                transport.assert_not_called()

    def test_runtime_installer_rejects_unverified_bytes_before_creating_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "untrusted.tar.gz"
            archive.write_bytes(b"not a verified interpreter")
            target = root / "installed"
            process = subprocess.run([sys.executable, "tools/bootstrap_runtime.py", "--archive", str(archive),
                                      "--destination", str(target)], cwd=Path(__file__).resolve().parents[1],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["reason_code"], "RUNTIME_CHECKSUM_MISMATCH")
            self.assertFalse(target.exists())

    def test_verified_backup_can_restore_saved_results_without_reexecuting_the_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, backup, restored = [root / name for name in ["state.sqlite3", "backup.sqlite3", "restored.sqlite3"]]
            planner = Planner(original, as_of=AS_OF)
            try:
                result = planner.run(BRIEF, MockModel())
            finally:
                planner.close()

            code, receipt = invoke("backup", "--db", str(original), "--output", str(backup))
            self.assertEqual(code, 0)
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["schema_version"], 3)
            code, receipt = invoke("restore", "--input", str(backup), "--db", str(restored))
            self.assertEqual(code, 0)
            planner = Planner(restored, as_of=AS_OF)
            try:
                cached = planner.run(BRIEF, MockModel())
                self.assertTrue(cached["cached"])
                self.assertEqual(cached["model_decisions"], 0)
                self.assertEqual(cached["cost"]["spent_units"], 8)
                self.assertEqual(planner.get_plan(result["plan_id"]), result["saved_plan"])
                self.assertEqual(len(planner.list_plans()), 1)
            finally:
                planner.close()

    def test_runtime_install_preserves_safe_relative_links_and_rejects_escaping_links(self):
        from tools import bootstrap_runtime
        for link, valid in [("../lib/python3", True), ("../../../outside", False)]:
            with self.subTest(link=link), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "python.tar.gz"
                with tarfile.open(archive, "w:gz") as package:
                    content = b"verified fixture runtime"
                    info = tarfile.TarInfo("python/lib/python3")
                    info.size = len(content)
                    info.mode = 0o755
                    package.addfile(info, io.BytesIO(content))
                    symbolic = tarfile.TarInfo("python/bin/python3")
                    symbolic.type = tarfile.SYMTYPE
                    symbolic.linkname = link
                    package.addfile(symbolic)
                (root / "runtime.lock.json").write_text(json.dumps({"size": archive.stat().st_size,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}))
                target = root / "installed"
                with patch.object(bootstrap_runtime, "ROOT", root), patch("sys.argv", ["bootstrap_runtime.py", "--archive", str(archive), "--destination", str(target)]), redirect_stdout(io.StringIO()):
                    code = bootstrap_runtime.main()
                self.assertEqual(code, 0 if valid else 2)
                self.assertEqual(target.exists(), valid)
                if valid:
                    self.assertEqual((target / "bin/python3").read_bytes(), b"verified fixture runtime")

    def test_backing_up_a_legacy_database_does_not_migrate_or_modify_its_source(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / 'legacy'
            with zipfile.ZipFile(Path(__file__).resolve().parents[1] / 'examples/hardening/pre-change/source.zip') as archive:
                archive.extractall(legacy)
            database = root / 'legacy.sqlite3'
            script = "from content_planner import Planner; from content_planner.model import MockModel; import json,sys; p=Planner(sys.argv[1],as_of=sys.argv[2]); p.run(json.loads(sys.stdin.read()),MockModel()); p.close()"
            process = subprocess.run([sys.executable, '-c', script, str(database), AS_OF], input=json.dumps(BRIEF),
                                     cwd=root, env=dict(os.environ, PYTHONPATH=str(legacy / 'src')), capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            code, receipt = invoke('backup', '--db', str(database), '--output', str(root / 'copy.sqlite3'))
            self.assertEqual(code, 0)
            self.assertEqual(receipt['schema_version'], 0)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_unknown_database_versions_fail_closed_without_changing_files(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'future.sqlite3'
            connection = sqlite3.connect(database)
            connection.execute('PRAGMA user_version=99')
            connection.close()
            before = database.read_bytes()
            source = root / 'input.json'
            source.write_text(json.dumps(BRIEF))
            commands = [
                ['run', '--input', str(source), '--db', str(database), '--env-file', str(root / 'no-env')],
                ['backup', '--db', str(database), '--output', str(root / 'backup.sqlite3')],
                ['restore', '--input', str(database), '--db', str(root / 'restored.sqlite3')],
            ]
            for arguments in commands:
                with self.subTest(command=arguments[0]):
                    code, result = invoke(*arguments)
                    self.assertEqual(code, 2)
                    self.assertEqual(result['reason_code'], 'UNSUPPORTED_SCHEMA_VERSION')
                    self.assertEqual(database.read_bytes(), before)
            self.assertFalse((root / 'backup.sqlite3').exists())
            self.assertFalse((root / 'restored.sqlite3').exists())

    def start_waiting_worker(self, root):
        import time
        database, ready, release, brief = [root / name for name in ['work.sqlite3', 'ready', 'release', 'brief.json']]
        brief.write_text(json.dumps(BRIEF))
        repository = Path(__file__).resolve().parents[1]
        environment = {k: v for k, v in os.environ.items() if not k.startswith('MODEL_')}
        environment['PYTHONPATH'] = str(repository / 'src')
        process = subprocess.Popen([sys.executable, str(repository / 'tests/hardening_worker.py'), str(database), str(ready), str(release), str(brief)],
                                   cwd=repository, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), process.stderr.read() if process.poll() is not None else 'worker did not reach model seam')
        return process, database, release

    def test_heartbeat_prevents_takeover_and_killed_worker_can_resume_confirmed_work(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process, database, release = self.start_waiting_worker(root)
            time.sleep(0.9)
            planner = Planner(database, as_of=AS_OF, lease_seconds=0.6, heartbeat_interval=0.1)
            try:
                active = planner.run(BRIEF, MockModel(), resume=True)
                self.assertEqual(active['reason_code'], 'REQUEST_IN_PROGRESS')
                self.assertEqual(active['cost']['spent_units'], 8)
                self.assertEqual(active['model_decisions'], 0)
                process.kill()
                process.wait(timeout=5)
                time.sleep(0.8)
                recovered = planner.run(BRIEF, MockModel(), resume=True)
                self.assertEqual(recovered['status'], 'completed')
                self.assertEqual(recovered['cost']['spent_units'], 8)
                self.assertEqual(recovered['tool_attempts'], 1)
                self.assertEqual(len(planner.list_plans()), 1)
                self.assertTrue(recovered['recovered'])
            finally:
                planner.close()

    @unittest.skipUnless(hasattr(__import__('signal'), 'SIGSTOP'), 'requires process suspension on supported Linux')
    def test_expired_worker_cannot_overwrite_a_new_generations_saved_result(self):
        import signal,time,sqlite3
        with tempfile.TemporaryDirectory() as directory:
            process, database, release = self.start_waiting_worker(Path(directory))
            # Pause outside SQLite's writer critical section. A stopped writer keeps OS locks;
            # a lease cannot safely steal those locks, so that is a separate operational case.
            with sqlite3.connect(database, timeout=5) as gate:
                gate.execute("BEGIN IMMEDIATE")
                os.kill(process.pid, signal.SIGSTOP)
            time.sleep(0.9)
            planner = Planner(database, as_of=AS_OF, lease_seconds=0.6, heartbeat_interval=0.1)
            try:
                saved = planner.run(BRIEF, MockModel(), resume=True)
                self.assertEqual(saved['status'], 'completed')
                release.write_text('continue old process')
                os.kill(process.pid, signal.SIGCONT)
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                old = json.loads(output)
                self.assertEqual(old['reason_code'], 'LEASE_LOST')
                cached = planner.run(BRIEF, MockModel())
                self.assertEqual(cached['plan_id'], saved['plan_id'])
                self.assertEqual(cached['cost']['spent_units'], 8)
                self.assertEqual(len(planner.list_plans()), 1)
            finally:
                planner.close()

    def test_interrupted_external_model_call_is_visible_and_not_blindly_retried(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, ready, release, brief = [root / n for n in ['work.sqlite3', 'ready', 'release', 'brief.json']]
            brief.write_text(json.dumps(BRIEF))
            repository = Path(__file__).resolve().parents[1]
            environment = {k: v for k, v in os.environ.items() if not k.startswith('MODEL_')}
            environment['PYTHONPATH'] = str(repository / 'src')
            process = subprocess.Popen([sys.executable, str(repository / 'tests/hardening_worker.py'), str(database), str(ready), str(release), str(brief), 'live'],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                until = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                process.kill(); process.wait(timeout=5)
                time.sleep(.8)
                planner = Planner(database, as_of=AS_OF, lease_seconds=.6, heartbeat_interval=.1)
                try:
                    report = planner.status(BRIEF['request_id'])
                    self.assertEqual(report['runtime_state'], 'reconcile_required')
                    self.assertEqual(report['pending_actions'][0]['kind'], 'model')
                    self.assertEqual(report['cost']['spent_units'], 8)
                    with patch('urllib.request.urlopen') as transport:
                        result = planner.run(BRIEF, MockModel(), resume=True)
                    self.assertEqual(result['reason_code'], 'OPERATION_OUTCOME_UNKNOWN')
                    self.assertEqual(result['cost']['spent_units'], 8)
                    self.assertEqual(result['model_decisions'], 0)
                    transport.assert_not_called()
                finally:
                    planner.close()
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_resuming_with_larger_cli_limits_cannot_reset_the_original_task_budget(self):
        for original_limit, expected_spent in [(2, 0), (3, 8)]:
            with self.subTest(limit=original_limit), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / 'work.sqlite3'
                first = Planner(database, as_of=AS_OF, max_decisions=original_limit)
                try:
                    stopped = first.run(BRIEF, MockModel())
                    self.assertEqual(stopped['reason_code'], 'STEP_LIMIT')
                finally:
                    first.close()
                class CountModel(MockModel):
                    calls = 0
                    def decide(self, messages, tools, timeout=30):
                        self.calls += 1
                        return super().decide(messages, tools, timeout)
                model = CountModel()
                second = Planner(database, as_of=AS_OF, max_decisions=99, max_tool_calls=99)
                try:
                    result = second.run(BRIEF, model, resume=True)
                    self.assertEqual(result['status'], 'incomplete')
                    self.assertEqual(result['reason_code'], 'TASK_BUDGET_EXHAUSTED')
                    self.assertEqual(result['cost']['spent_units'], expected_spent)
                    self.assertEqual(model.calls, 0)
                    self.assertEqual(second.list_plans(), [])
                    self.assertEqual(result['task_counters']['model_attempts'], original_limit)
                    self.assertEqual(result['task_limits']['max_model_attempts'], original_limit)
                finally:
                    second.close()

    def test_cancel_stops_new_work_and_cannot_be_undone_by_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            process, database, release = self.start_waiting_worker(Path(directory))
            planner = Planner(database, as_of=AS_OF, lease_seconds=.6, heartbeat_interval=.1)
            try:
                receipt = planner.cancel(BRIEF['request_id'])
                self.assertEqual(receipt['status'], 'accepted')
                release.write_text('stop now')
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                cancelled = json.loads(output)
                self.assertEqual(cancelled['reason_code'], 'CANCELLED')
                self.assertEqual(cancelled['cost']['spent_units'], 8)
                self.assertEqual(len(cancelled['scripts']), 1)
                self.assertEqual(planner.list_plans(), [])
                resumed = planner.run(BRIEF, MockModel(), resume=True)
                self.assertEqual(resumed['reason_code'], 'CANCELLED')
                self.assertEqual(resumed['model_decisions'], 0)
                self.assertEqual(planner.status(BRIEF['request_id'])['runtime_state'], 'cancelled')
            finally:
                planner.close()

    def test_cli_status_is_read_only_and_late_cancel_retains_the_saved_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite3'
            planner = Planner(database, as_of=AS_OF)
            try:
                saved = planner.run(BRIEF, MockModel())
            finally:
                planner.close()
            original = database.read_bytes()
            with patch('urllib.request.urlopen') as transport:
                code, status = invoke('status', '--db', str(database), '--request-id', BRIEF['request_id'])
            self.assertEqual(code, 0)
            self.assertEqual(status['runtime_state'], 'completed')
            self.assertEqual(status['cost']['spent_units'], 8)
            self.assertEqual(database.read_bytes(), original)
            transport.assert_not_called()
            code, cancel = invoke('cancel', '--db', str(database), '--request-id', BRIEF['request_id'])
            self.assertEqual(code, 0)
            self.assertEqual(cancel['status'], 'too_late')
            self.assertEqual(cancel['plan_id'], saved['plan_id'])
            missing = root / 'missing.sqlite3'
            code, result = invoke('status', '--db', str(missing), '--request-id', 'unknown')
            self.assertEqual(code, 2)
            self.assertFalse(missing.exists())

    def test_verifier_rejects_code_that_changes_during_the_test_run(self):
        import shutil
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(repository / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'tools').mkdir()
            shutil.copy2(repository / 'tools/verify.py', root / 'tools/verify.py')
            (root / 'tests').mkdir()
            test_source = "import unittest\nfrom pathlib import Path\nclass ChangeDuringTest(unittest.TestCase):\n def test_change(self):\n  p=Path(__file__).resolve().parents[1]/'src/content_planner/__init__.py'\n  p.write_text(p.read_text()+'\\n# changed during validation\\n')\n"
            (root / 'tests/test_mutation.py').write_text(test_source)
            result = subprocess.run([sys.executable, str(root / 'tools/verify.py')], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            summary = json.loads((root / 'examples/test-summary.json').read_text())
            self.assertFalse(summary['inputs_unchanged'])
            self.assertEqual(summary['test_command_exit_code'], 0)
            self.assertIsNone(summary['passed'])

    def test_confirmed_model_decision_is_replayed_after_crash_without_another_model_request(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, ready, release, brief = [root / n for n in ['work.sqlite3', 'ready', 'release', 'brief.json']]
            brief.write_text(json.dumps(BRIEF))
            repository = Path(__file__).resolve().parents[1]
            environment = {k: v for k, v in os.environ.items() if not k.startswith('MODEL_')}
            environment['PYTHONPATH'] = str(repository / 'src')
            process = subprocess.Popen([sys.executable, str(repository / 'tests/hardening_worker.py'), str(database), str(ready), str(release), str(brief), 'live', 'decision'],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                until = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), process.stderr.read() if process.poll() is not None else 'no committed decision event')
                process.kill(); process.wait(timeout=5)
                time.sleep(.8)
                class NoMoreModel(MockModel):
                    mode = 'live'
                    def decide(self, *args, **kwargs):
                        raise AssertionError('confirmed decision must not be purchased again')
                planner = Planner(database, as_of=AS_OF, max_decisions=99, lease_seconds=.6, heartbeat_interval=.1)
                try:
                    result = planner.run(BRIEF, NoMoreModel(), resume=True)
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(result['model_decisions'], 0)
                    self.assertEqual(result['model_requests_sent'], 0)
                    self.assertEqual(result['replayed_decisions'], 1)
                    self.assertEqual(result['tool_attempts'], 1)
                    self.assertEqual(result['cost']['spent_units'], 8)
                    self.assertEqual(result['task_counters']['model_attempts'], 4)
                    self.assertEqual(len(planner.list_plans()), 1)
                finally:
                    planner.close()
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_local_tool_result_lost_before_commit_replays_once_and_preserves_attempt_budget(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, ready, release, brief = [root / n for n in ['work.sqlite3', 'ready', 'release', 'brief.json']]
            brief.write_text(json.dumps(BRIEF))
            repository = Path(__file__).resolve().parents[1]
            environment = {k: v for k, v in os.environ.items() if not k.startswith('MODEL_')}
            environment['PYTHONPATH'] = str(repository / 'src')
            process = subprocess.Popen([sys.executable, str(repository / 'tests/hardening_worker.py'), str(database), str(ready), str(release), str(brief), 'live', 'tool'],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                until = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), process.stderr.read() if process.poll() is not None else 'no uncommitted tool event')
                process.kill(); process.wait(timeout=5)
                time.sleep(.8)
                class CountModel(MockModel):
                    mode = 'live'
                    calls = 0
                    def decide(self, *args, **kwargs):
                        self.calls += 1
                        return super().decide(*args, **kwargs)
                planner = Planner(database, as_of=AS_OF, lease_seconds=.6, heartbeat_interval=.1)
                model = CountModel()
                try:
                    status = planner.status(BRIEF['request_id'])
                    self.assertEqual(status['pending_actions'][0]['kind'], 'tool')
                    self.assertEqual(status['cost']['spent_units'], 0)
                    result = planner.run(BRIEF, model, resume=True)
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(result['cost']['spent_units'], 8)
                    self.assertEqual(result['task_counters']['tool_attempts'], 5)
                    self.assertEqual(len(result['abandoned_local_attempts']), 1)
                    self.assertEqual(result['replayed_decisions'], 1)
                    self.assertEqual(model.calls, 1)
                    self.assertEqual(len(planner.list_plans()), 1)
                finally:
                    planner.close()
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_invalid_tool_arguments_are_not_advertised_as_transient_retries(self):
        from tests.test_guards import InvalidActionModel
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(Path(directory) / 'state.sqlite3', as_of=AS_OF)
            try:
                result = planner.run(BRIEF, InvalidActionModel('generate_script', '{}'))
                rejected = result['trace'][0]['result']
                self.assertEqual(rejected['error_code'], 'INVALID_ARGUMENTS')
                self.assertFalse(rejected['retryable'])
            finally:
                planner.close()

    def test_transient_tool_retry_limit_preserves_successes_and_allows_explicit_resume(self):
        class RepeatingFailure(MockModel):
            def decide(self, messages, tools, timeout=30):
                if messages[-1]['role'] == 'tool' and json.loads(messages[-1]['content']).get('error_code') == 'SAVE_FAILED':
                    return {'tool_calls': messages[-2]['tool_calls']}
                return super().decide(messages, tools, timeout)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite3'
            planner = Planner(database, as_of=AS_OF, scenario={'failures': {'save_plan': 'always'}})
            try:
                failed = planner.run(BRIEF, RepeatingFailure())
                self.assertEqual(failed['reason_code'], 'RETRY_EXHAUSTED')
                self.assertEqual(len([op for op in failed['steps'] if op['tool'] == 'save_plan']), 3)
                self.assertEqual(failed['cost']['spent_units'], 8)
                self.assertEqual(len(failed['scripts']), 1)
                self.assertEqual(planner.list_plans(), [])
            finally:
                planner.close()
            planner = Planner(database, as_of=AS_OF)
            try:
                resumed = planner.run(BRIEF, MockModel(), resume=True)
                self.assertEqual(resumed['status'], 'completed')
                self.assertEqual(resumed['cost']['spent_units'], 8)
                self.assertEqual(resumed['tool_attempts'], 1)
                self.assertEqual(len(planner.list_plans()), 1)
            finally:
                planner.close()

    def test_transport_timeout_is_recorded_as_unknown_and_resume_cannot_purchase_it_again(self):
        from content_planner.model import LiveModel
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(Path(directory) / 'state.sqlite3', as_of=AS_OF)
            try:
                with patch('urllib.request.urlopen', side_effect=TimeoutError), patch.dict(os.environ, {'MODEL_API_KEY': 'never-export-this-key'}):
                    result = planner.run(BRIEF, LiveModel())
                self.assertEqual(result['reason_code'], 'MODEL_UNAVAILABLE')
                self.assertTrue(result['model_error']['outcome_unknown'])
                self.assertEqual(result['model_requests_sent'], 1)
                self.assertEqual(planner.status(BRIEF['request_id'])['runtime_state'], 'reconcile_required')
                with patch('urllib.request.urlopen') as transport:
                    repeated = planner.run(BRIEF, LiveModel(api_key='still-no-network'), resume=True)
                transport.assert_not_called()
                self.assertEqual(repeated['reason_code'], 'OPERATION_OUTCOME_UNKNOWN')
                self.assertEqual(repeated['task_counters']['model_attempts'], 1)
                self.assertNotIn('never-export-this-key', json.dumps(repeated))
            finally:
                planner.close()

    def test_cli_rejects_oversized_or_deep_json_before_opening_state_or_sending_requests(self):
        bodies = [json.dumps(dict(BRIEF, topic='x' * 1_000_001)), '[' * 2000 + '0' + ']' * 2000,
                  json.dumps(dict(BRIEF, extra=[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]))]
        for body in bodies:
            with self.subTest(size=len(body)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                input_file, database = root / 'input.json', root / 'state.sqlite3'
                input_file.write_text(body)
                with patch('urllib.request.urlopen') as transport:
                    code, result = invoke('run', '--input', str(input_file), '--db', str(database), '--mode', 'live')
                self.assertEqual(code, 2)
                self.assertEqual(result['reason_code'], 'INVALID_INPUT')
                self.assertEqual(result['model_requests_sent'], 0)
                self.assertFalse(database.exists())
                transport.assert_not_called()

    def test_interrupted_export_keeps_previous_committed_generation_verifiable(self):
        from content_planner.__main__ import export_result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner = Planner(root / 'state.sqlite3', as_of=AS_OF)
            try:
                result = planner.run(BRIEF, MockModel())
            finally:
                planner.close()
            destination = root / 'exports'
            export_result(result, BRIEF, destination, {'source_sha256': 'known-source'})
            index = json.loads((destination / 'current.json').read_text())
            previous = (destination / index['manifest']).read_bytes()
            original_replace = os.replace
            def interrupt(source, target):
                if Path(target).name == 'current.json':
                    raise OSError('simulated interrupted publication')
                return original_replace(source, target)
            with patch('os.replace', side_effect=interrupt), self.assertRaises(OSError):
                export_result(dict(result, summary='second attempt'), BRIEF, destination, {'source_sha256': 'known-source'})
            self.assertEqual(json.loads((destination / 'current.json').read_text()), index)
            self.assertEqual((destination / index['manifest']).read_bytes(), previous)
            manifest = json.loads(previous)
            for name, digest in manifest['artifacts_sha256'].items():
                self.assertEqual(hashlib.sha256((destination / index['directory'] / name).read_bytes()).hexdigest(), digest)
            text = (destination / index['directory'] / 'plan.md').read_text()
            self.assertIn('\n\n', text)
            self.assertNotIn('\\n', text)

    def test_production_run_refuses_unsupported_runtime_before_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, database = root / 'input.json', root / 'state.sqlite3'
            source.write_text(json.dumps(BRIEF))
            with patch('platform.python_version', return_value='3.10.12'), patch('urllib.request.urlopen') as transport:
                code, result = invoke('run', '--input', str(source), '--db', str(database), '--mode', 'live', '--production')
            self.assertEqual(code, 2)
            self.assertEqual(result['reason_code'], 'UNSUPPORTED_RUNTIME')
            self.assertFalse(database.exists())
            transport.assert_not_called()

    def test_export_index_cannot_overwrite_input_even_after_a_successful_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'current.json'
            source.write_text(json.dumps(BRIEF))
            original = source.read_bytes()
            database = root / 'state.sqlite3'
            code, result = invoke('run', '--input', str(source), '--db', str(database), '--as-of', AS_OF, '--output-dir', str(root))
            self.assertEqual(code, 2)
            self.assertEqual(result['reason_code'], 'INVALID_CONFIGURATION')
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(database.exists())

    def test_capacity_rejection_does_not_create_a_task_and_releases_after_workers_finish(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'shared.sqlite3'
            repository = Path(__file__).resolve().parents[1]
            environment = dict(os.environ, PYTHONPATH=str(repository / 'src'))
            workers = []
            try:
                for index in range(2):
                    brief, ready, release = [root / f'{name}-{index}' for name in ['brief', 'ready', 'release']]
                    brief.write_text(json.dumps(dict(BRIEF, request_id=f'capacity-{index}')))
                    process = subprocess.Popen([sys.executable, str(repository / 'tests/hardening_worker.py'), str(database), str(ready), str(release), str(brief), 'mock', 'capacity'],
                                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    workers.append((process, ready, release))
                until = time.monotonic() + 10
                while not all(ready.exists() for _, ready, _ in workers) and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(all(ready.exists() for _, ready, _ in workers))
                planner = Planner(database, as_of=AS_OF)
                try:
                    refused = planner.run(dict(BRIEF, request_id='third-task'), MockModel())
                    self.assertEqual(refused['reason_code'], 'CAPACITY_EXCEEDED')
                    self.assertEqual(refused['model_decisions'], 0)
                    self.assertEqual(planner.status('third-task')['runtime_state'], 'not_found')
                    for _, _, release in workers:
                        release.write_text('release')
                    for process, _, _ in workers:
                        output, error = process.communicate(timeout=10)
                        self.assertEqual(process.returncode, 0, error)
                        self.assertEqual(json.loads(output)['status'], 'completed', output)
                    accepted = planner.run(dict(BRIEF, request_id='third-task'), MockModel())
                    self.assertEqual(accepted['status'], 'completed')
                    self.assertEqual(len(planner.list_plans()), 3)
                finally:
                    planner.close()
            finally:
                for process, _, _ in workers:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)

    def test_shared_evaluation_budget_stops_network_dispatch_across_model_instances(self):
        from content_planner.model import LiveModel
        budget = {'remaining': 1}
        decision = MockModel().decide([{'role': 'system', 'content': ''}, {'role': 'user', 'content': json.dumps({'brief': BRIEF})}], [])
        payload = {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': decision['tool_calls']}}]}
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(Path(directory) / 'state.sqlite3', as_of=AS_OF)
            try:
                with patch('urllib.request.urlopen', side_effect=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode())) as transport:
                    first = planner.run(BRIEF, LiveModel(api_key='fixture', request_budget=budget))
                    second = planner.run(BRIEF, LiveModel(api_key='fixture', request_budget=budget), resume=True)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(first['reason_code'], 'EVALUATION_BUDGET_EXHAUSTED')
                self.assertEqual(second['reason_code'], 'EVALUATION_BUDGET_EXHAUSTED')
                self.assertEqual(first['model_requests_sent'], 1)
                self.assertEqual(second['model_requests_sent'], 0)
                self.assertEqual(budget['remaining'], 0)
                self.assertEqual(len(second['steps']), 1)
            finally:
                planner.close()

    def test_live_evaluation_requires_an_explicit_positive_request_cap(self):
        for cap in [[], ['--max-model-requests', '0']]:
            process = subprocess.run([sys.executable, 'tools/evaluate.py', '--mode', 'live', *cap],
                                     capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
                                     env=dict(os.environ, MODEL_API_KEY=""))
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)['reason_code'], 'REQUEST_CAP_REQUIRED')

    def test_cancel_wins_when_accepted_between_last_check_and_durable_action_admission(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite3'
            controller = Planner(database, as_of=AS_OF)
            race = {'armed': False, 'reads': 0, 'cancelled': False}
            real_clock = time.perf_counter
            def observer(event):
                if event['event'] == 'model_decision_committed' and event.get('next_tools') == ['generate_script']:
                    race['armed'] = True
            def clock():
                if race['armed'] and not race['cancelled']:
                    race['reads'] += 1
                    if race['reads'] == 2:
                        receipt = controller.cancel(BRIEF['request_id'])
                        self.assertEqual(receipt['status'], 'accepted')
                        race['cancelled'] = True
                return real_clock()
            planner = Planner(database, as_of=AS_OF, event_sink=observer)
            try:
                with patch('time.perf_counter', side_effect=clock):
                    result = planner.run(BRIEF, MockModel())
                self.assertTrue(race['cancelled'])
                self.assertEqual(result['reason_code'], 'CANCELLED')
                self.assertEqual(result['cost']['spent_units'], 0)
                self.assertEqual(len(result['scripts']), 0)
                self.assertEqual([op['tool'] for op in result['steps']], ['search_materials', 'estimate_cost'])
                self.assertEqual(planner.list_plans(), [])
            finally:
                planner.close()
                controller.close()

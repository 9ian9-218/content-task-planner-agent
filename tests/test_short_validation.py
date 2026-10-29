"""Public, bounded short-validation command; never uses a remote model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ShortValidationTests(unittest.TestCase):
    def test_delivery_audit_rejects_exceeded_or_inconsistent_model_request_budgets(self):
        import shutil
        reports = {
            'live': 'examples/live-evaluation-summary.json',
            'adversarial': 'examples/live/adversarial-summary.json',
            'replanning': 'examples/short-validation/replanning/summary.json',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            output = root / 'audit.json'
            def run_audit():
                process = subprocess.run([sys.executable, 'tools/check_delivery.py', '--root', str(root), '--output', str(output)],
                                         cwd=ROOT, env=dict(os.environ, MODEL_API_KEY=''), capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, 1, process.stderr)
                return json.loads(output.read_text())
            for label, relative in reports.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                original = json.loads((ROOT / relative).read_text())
                target.write_text(json.dumps(original))
                self.assertTrue(run_audit()['reports'][label]['passed'], label)
                for sent, remaining in [(25, 0), (10, 15), (True, 23)]:
                    with self.subTest(report=label, sent=sent, remaining=remaining):
                        target.write_text(json.dumps(dict(original, request_cap=24, model_requests_sent=sent,
                                                          request_budget_remaining=remaining)))
                        audit = run_audit()
                        self.assertFalse(audit['reports'][label]['passed'])
                        self.assertTrue(any(problem['code'] == 'VALIDATION_NOT_PASSED' and problem['path'] == relative
                                            for problem in audit['problems']))

    def test_short_load_command_verifies_mixed_results_and_resource_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report'
            process = subprocess.run([sys.executable, 'tools/check_short_run.py', '--duration-seconds', '1',
                                      '--clients', '2', '--minimum-completed', '1', '--output', str(output)],
                                     cwd=ROOT, env=dict(os.environ, MODEL_API_KEY=''), capture_output=True, text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertTrue(summary['passed'])
            self.assertEqual(summary['remote_model_requests'], 0)
            self.assertGreaterEqual(summary['completed_checks'], 1)
            self.assertEqual(summary['invariant_failures'], 0)
            self.assertTrue(summary['resource_checks']['no_thread_leaks'])
            self.assertTrue(summary['resource_checks']['no_fd_leaks'])
            self.assertGreaterEqual(summary['measured_load_seconds'], 1)
            self.assertEqual(summary['clients'], 2)
            self.assertEqual(summary['database_plan_count'], summary['expected_saved_plan_count'])

    def test_adversarial_evaluation_requires_a_request_cap_before_configuration_or_network(self):
        process = subprocess.run([sys.executable, 'tools/evaluate_adversarial.py'], cwd=ROOT,
                                 env=dict(os.environ, MODEL_API_KEY=''), capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)['reason_code'], 'REQUEST_CAP_REQUIRED')

    def test_replanning_repeat_command_records_each_actual_observation_and_a_shared_cap(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch
        from tests.provider_fixture import respond
        from tools import evaluate_replanning
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(evaluate_replanning, 'ROOT', root), \
                    patch.dict(os.environ, {'MODEL_API_KEY': 'fixture-only-key'}, clear=True), \
                    patch('sys.argv', ['tools/evaluate_replanning.py', '--rounds', '3', '--max-model-requests', '15']), \
                    patch('urllib.request.urlopen', side_effect=respond) as transport, redirect_stdout(io.StringIO()):
                code = evaluate_replanning.main()
            summary = json.loads((root / 'examples/short-validation/replanning/summary.json').read_text())
            self.assertEqual(code, 0)
            self.assertEqual(summary['rounds_passed'], 3)
            self.assertEqual(summary['model_requests_sent'], 15)
            self.assertEqual(summary['request_budget_remaining'], 0)
            self.assertEqual(transport.call_count, 15)
            self.assertEqual([row['observed_quote_units'] for row in summary['results']], [[12, 8]] * 3)
            self.assertNotIn('fixture-only-key', json.dumps(summary))

    def test_delivery_audit_rejects_claimed_completion_without_a_saved_plan(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            target = root / 'examples/mock/C01-normal'
            target.mkdir(parents=True)
            for name in ['input.json', 'output.json', 'trace.jsonl', 'manifest.json']:
                shutil.copy2(ROOT / 'examples/mock/C01-normal' / name, target / name)
            result = json.loads((target / 'output.json').read_text())
            result['saved_plan'] = None
            (target / 'output.json').write_text(json.dumps(result))
            output = root / 'audit.json'
            process = subprocess.run([sys.executable, 'tools/check_delivery.py', '--root', str(root), '--output', str(output)],
                                     cwd=ROOT, env=dict(os.environ, MODEL_API_KEY=''), capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 1)
            audit = json.loads(output.read_text())
            self.assertFalse(audit['technical_materials_ready'])
            self.assertTrue(any(problem['code'] == 'EVIDENCE_INCONSISTENT' for problem in audit['problems']))

    def test_initialized_database_cached_read_does_not_need_the_writer_lock(self):
        import sqlite3
        from content_planner import Planner
        from content_planner.model import MockModel
        from tests.test_acceptance import BRIEF, AS_OF
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite3'
            planner = Planner(database, as_of=AS_OF)
            try:
                first = planner.run(BRIEF, MockModel())
            finally:
                planner.close()
            with sqlite3.connect(database, timeout=.1) as writer:
                writer.execute('BEGIN IMMEDIATE')
                reader = Planner(database, as_of=AS_OF)
                try:
                    result = reader.run(BRIEF, MockModel())
                    self.assertTrue(result['cached'])
                    self.assertEqual(result['plan_id'], first['plan_id'])
                    self.assertEqual(result['cost']['spent_units'], 8)
                    self.assertEqual(result['model_decisions'], 0)
                finally:
                    reader.close()

    def test_busy_task_admission_returns_retryable_state_without_dispatching_model(self):
        import sqlite3
        from content_planner import Planner
        from content_planner.model import MockModel
        from tests.test_acceptance import BRIEF, AS_OF
        class NoModel(MockModel):
            def decide(self, *args, **kwargs):
                raise AssertionError('busy admission must not dispatch a model')
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite3'
            planner = Planner(database, as_of=AS_OF)
            try:
                with sqlite3.connect(database, timeout=.1) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    result = planner.run(BRIEF, NoModel())
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(result['reason_code'], 'STATE_BUSY')
                self.assertTrue(result['retryable'])
                self.assertEqual(result['model_decisions'], 0)
                self.assertEqual(planner.status(BRIEF['request_id'])['runtime_state'], 'not_found')
                completed = planner.run(BRIEF, MockModel())
                self.assertEqual(completed['status'], 'completed')
                self.assertEqual(completed['cost']['spent_units'], 8)
            finally:
                planner.close()

    def test_model_timeout_can_be_configured_while_remaining_inside_the_task_deadline(self):
        from unittest.mock import patch
        from content_planner import Planner
        from content_planner.model import LiveModel
        from tests.provider_fixture import respond
        from tests.test_acceptance import BRIEF, AS_OF
        timeouts = []
        def transport(request, timeout):
            timeouts.append(timeout)
            return respond(request, timeout)
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(Path(directory) / 'state.sqlite3', as_of=AS_OF, model_timeout_seconds=45, wall_timeout_seconds=90)
            try:
                with patch('urllib.request.urlopen', side_effect=transport):
                    result = planner.run(BRIEF, LiveModel(api_key='fixture'))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(len(timeouts), 4)
                self.assertTrue(all(40 < value <= 45 for value in timeouts))
                self.assertEqual(result['model_timeout_seconds'], 45)
            finally:
                planner.close()

    def test_package_command_excludes_credentials_databases_caches_and_links(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'project'
            for folder in ['src/content_planner', 'data', 'docs', '__pycache__']:
                (root / folder).mkdir(parents=True, exist_ok=True)
            (root / 'README.md').write_text('Synthetic package fixture')
            (root / '.env.example').write_text('MODEL_API_KEY=\n')
            (root / '.env.local').write_text('MODEL_API_KEY=private-packaging-sentinel\n')
            (root / 'src/content_planner/__init__.py').write_text('')
            (root / 'data/public.json').write_text('{"synthetic":true}')
            (root / 'data/private.sqlite3').write_bytes(b'private state')
            (root / '__pycache__/private.pyc').write_bytes(b'ignored cache')
            (root / 'data/credential-link').symlink_to(root / '.env.local')
            destination = parent / 'delivery'
            process = subprocess.run([sys.executable, 'tools/package_submission.py', '--root', str(root), '--destination', str(destination)],
                                     cwd=ROOT, env=dict(os.environ, MODEL_API_KEY=''), capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            result = json.loads(process.stdout)
            with zipfile.ZipFile(result['archive']) as archive:
                names = archive.namelist()
                self.assertTrue(any(name.endswith('/README.md') for name in names))
                self.assertTrue(any(name.endswith('/.env.example') for name in names))
                self.assertTrue(any(name.endswith('/data/public.json') for name in names))
                self.assertFalse(any('.env.local' in name or '.sqlite3' in name or '__pycache__' in name or 'credential-link' in name for name in names))
                self.assertFalse(any(b'private-packaging-sentinel' in archive.read(name) for name in names))
            self.assertFalse(result['formal_submission_ready'])
            self.assertEqual(result['archive_sha256'], __import__('hashlib').sha256(Path(result['archive']).read_bytes()).hexdigest())

    def test_explicit_wal_database_preserves_cached_reads_during_an_exclusive_writer(self):
        import sqlite3
        from content_planner import Planner
        from content_planner.model import MockModel
        from tests.test_acceptance import BRIEF, AS_OF
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite3'
            planner = Planner(database, as_of=AS_OF, journal_mode='wal')
            try:
                first = planner.run(BRIEF, MockModel())
            finally:
                planner.close()
            with sqlite3.connect(database) as writer:
                writer.execute('BEGIN EXCLUSIVE')
                reader = Planner(database, as_of=AS_OF)
                try:
                    result = reader.run(BRIEF, MockModel())
                    self.assertTrue(result['cached'])
                    self.assertEqual(result['plan_id'], first['plan_id'])
                    self.assertEqual(result['cost']['spent_units'], 8)
                finally:
                    reader.close()

    def test_cli_reports_busy_initialization_as_retryable_without_model_requests(self):
        from contextlib import redirect_stdout
        import io
        import sqlite3
        from unittest.mock import patch
        from content_planner import Planner
        from content_planner.__main__ import main
        from tests.test_acceptance import BRIEF, AS_OF
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite3'
            planner = Planner(database, as_of=AS_OF)
            planner.close()
            source = root / 'input.json'
            source.write_text(json.dumps(BRIEF))
            output = io.StringIO()
            with sqlite3.connect(database) as writer:
                writer.execute('BEGIN EXCLUSIVE')
                with patch('urllib.request.urlopen') as transport, redirect_stdout(output):
                    code = main(['run', '--input', str(source), '--db', str(database), '--as-of', AS_OF])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(result['reason_code'], 'STATE_BUSY')
            self.assertTrue(result['retryable'])
            self.assertEqual(result['model_requests_sent'], 0)
            transport.assert_not_called()

    def test_wal_is_rejected_on_unverified_sqlite_before_any_database_is_created(self):
        from unittest.mock import patch
        from content_planner import Planner
        from tests.test_acceptance import AS_OF
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.sqlite3'
            with patch('sqlite3.sqlite_version', '3.37.2'):
                with self.assertRaises(ValueError):
                    Planner(database, as_of=AS_OF, journal_mode='wal')
            self.assertFalse(database.exists())

    def test_live_batch_stops_dispatching_after_provider_cooldown_instead_of_repeating_requests(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io
        from unittest.mock import patch
        from urllib.error import HTTPError
        from tools import evaluate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def limited(request, timeout):
                raise HTTPError(request.full_url, 503, 'fixture cooldown', {'Retry-After': '299'}, io.BytesIO(b'fixture'))
            with patch.object(evaluate, 'ROOT', root), patch.dict(os.environ, {'MODEL_API_KEY': 'fixture-only'}, clear=True), \
                    patch('sys.argv', ['tools/evaluate.py', '--mode', 'live', '--all-cases', '--max-model-requests', '100']), \
                    patch('urllib.request.urlopen', side_effect=limited) as transport, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = evaluate.main()
            self.assertEqual(code, 1)
            self.assertEqual(transport.call_count, 1)
            summary = json.loads((root / 'examples/live-evaluation-summary.json').read_text())
            self.assertEqual(summary['selected_cases_total'], 18)
            self.assertEqual(len(summary['not_run_cases']), 17)
            self.assertEqual(summary['batch_stop_reason'], 'PROVIDER_UNAVAILABLE')
            self.assertEqual(summary['model_requests_sent'], 1)

    def test_full_capacity_refusal_does_not_compete_for_the_database_writer_lock(self):
        import sqlite3,time
        from content_planner import Planner
        from content_planner.model import MockModel
        from tests.test_acceptance import BRIEF, AS_OF
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite3'
            setup = Planner(database, as_of=AS_OF, journal_mode='wal')
            setup.close()
            processes = []
            try:
                for client in range(2):
                    brief, ready, release = [root / f'{name}-{client}' for name in ['brief', 'ready', 'release']]
                    brief.write_text(json.dumps(dict(BRIEF, request_id=f'held-{client}')))
                    process = subprocess.Popen([sys.executable, 'tests/hardening_worker.py', str(database), str(ready), str(release), str(brief), 'mock', 'capacity'],
                                               cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')),
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    processes.append((process, ready, release))
                until = time.monotonic() + 10
                while not all(ready.exists() for _, ready, _ in processes) and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(all(ready.exists() for _, ready, _ in processes))
                planner = Planner(database, as_of=AS_OF)
                try:
                    with sqlite3.connect(database) as writer:
                        writer.execute('BEGIN IMMEDIATE')
                        result = planner.run(dict(BRIEF, request_id='rejected-third'), MockModel())
                    self.assertEqual(result['reason_code'], 'CAPACITY_EXCEEDED')
                    self.assertEqual(result['model_decisions'], 0)
                finally:
                    planner.close()
            finally:
                for process, _, release in processes:
                    release.write_text('release')
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)

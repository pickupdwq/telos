"""Audit scope, actual evidence, task lifecycle and project regression tests."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import audit
import audit_probe
from test_telos import b, BridgeTests, synthetic_exam
from test_projects import ProjectTests
from test_closed_book import WorkflowTests, message
from test_providers import configured


def boundary():
    return {'environment': {'kind': 'test', 'name': 'isolated fixture'}, 'scope_paths': ['calc.py'],
            'requirements': ['Integer addition should preserve precision'], 'out_of_scope': ['Production systems'],
            'stop_conditions': ['Stop affected steps when required access is unavailable'],
            'mutation_policy': 'Read-only source. Only isolated evidence artifacts may be written.', 'targets': [],
            'checklist': [{'id': 'addition', 'kind': 'code', 'prompt': 'Check arithmetic and boundaries',
                           'expected': 'Integer precision is preserved', 'required': True}]}


QUALITY = [{'id': 'quality', 'argv': ['{python}', '-c',
    'import json,os; r=json.load(open(os.environ["TELOS_AUDIT_REPORT"])); assert len(r["results"])>=1']}]


class PublicAuditPromptTests(unittest.TestCase):
    def prompt(self, policy):
        bridge = Mock()
        bridge.encoded.side_effect = json.dumps
        state = {'session_id': 's' * 32, 'worktree': '/srv/source', 'base_commit': 'a' * 40}
        contract = {'audit': policy, 'dependency_paths': ['/srv/dependency']}
        return audit.public_prompt(bridge, state, contract, artifact_root=Path('/srv/artifacts'))

    def test_offline_audit_uses_report_help_and_avoids_probe_discovery(self):
        text = self.prompt(boundary())
        self.assertIn('offline source audit', text)
        self.assertIn('inspect the scoped source and produce complete evidence', text)
        self.assertIn('/srv/artifacts/report.json', text)
        self.assertIn('/srv/artifacts/audit-report-help.json', text)
        self.assertNotIn('audit-probe-help.json', text)
        self.assertNotIn('TELOS_AUDIT_URL', text)
        self.assertNotIn('Authorization: Bearer', text)
        self.assertIn('Git metadata and whole-root listings are not granted', text)
        self.assertIn('scoped file bytes/hashes', text)
        self.assertIn('do not run source Git or repository-root discovery', text)
        self.assertIn('Source root: /srv/source', text)
        self.assertIn('Source commit: ' + 'a' * 40, text)

    def test_online_and_mixed_audits_keep_scoped_authenticated_broker_instructions(self):
        online = {**boundary(), 'targets': [{'origin': 'http://127.0.0.1:8000', 'path_prefix': '/ui',
                                             'methods': ['GET']}]}
        mixed = copy.deepcopy(online)
        mixed['checklist'].append({'id': 'api', 'kind': 'api', 'prompt': 'Check API',
                                   'expected': 'Declared API behavior', 'required': True})
        for policy in (online, mixed):
            with self.subTest(policy=policy):
                text = self.prompt(policy)
                self.assertIn('authenticated local audit probe service for declared targets only', text)
                self.assertIn('Authorization: Bearer', text)
                self.assertIn('/srv/artifacts/audit-report-help.json', text)
                self.assertIn('/srv/artifacts/audit-probe-help.json', text)
                self.assertIn('never direct network access', text)


class AuditTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    scores = BridgeTests.scores

    def create(self, policy=None, **kwargs):
        return b.create_session(repo=str(self.repo), task='Audit addition with evidence', questions=self.q,
                                checks=QUALITY, task_type='audit', audit=policy or boundary(), **kwargs)['session_id']

    def data(self, sid):
        return b.load_session(b.run_dir(sid))

    def report(self, sid, status='passed', finding=False):
        state, contract = self.data(sid)
        root = audit.artifacts(b, state)
        (root / 'source-evidence.txt').write_text((Path(state['worktree']) / 'calc.py').read_text())
        report = b.read_json(root / 'report.json')
        report['results'][0].update(status='finding' if finding else status, actual='Observed integer arithmetic',
                                     steps=['Read calc.py and exercise integer boundaries'], evidence=['source-evidence.txt'])
        if finding:
            report['findings'] = [{'id': 'F1', 'check_id': 'addition', 'category': 'defect', 'severity': 'medium',
                                  'title': 'Reproducible fixture finding', 'expected': 'Expected behavior',
                                  'actual': 'Observed failure', 'steps': ['Run the supplied reproduction'],
                                  'evidence': ['source-evidence.txt'], 'location': 'calc.py:1'}]
        b.write_json(root / 'report.json', report)
        return report

    def evaluate(self, sid, error=None):
        state, _ = self.data(sid)
        state.update(phase='responded', thread_id='00000000-0000-4000-8000-000000000001')
        b.write_json(b.run_dir(sid) / 'state.json', state)
        real_popen = subprocess.Popen
        def launch(argv, **kwargs):
            return Mock(pid=os.getpid()) if '_job' in argv else real_popen(argv, **kwargs)
        with patch.object(b.subprocess, 'Popen', side_effect=launch):
            job = b.start_job(sid, 'evaluate')['job_id']
        with patch.object(b, 'closed_book_exam', side_effect=error if error else synthetic_exam):
            b.background_job(sid, job)
        return self.data(sid)[0]

    def review(self, sid, decisions=None):
        state, _ = self.data(sid)
        report = b.read_json(b.run_dir(sid) / f"attempt-{state['rounds']}.json")['audit']
        return {'findings': decisions or [], 'limitations': [r['id'] for r in report['report']['results']
                                                            if r['status'] in ('blocked', 'not_tested')],
                'visual_evidence_reviewed': report['screenshots'], 'coverage_rationale': 'Independently checked source and evidence'}

    def accept(self, sid, decisions=None):
        state, _ = self.data(sid)
        return b.grade(sid, state['packet_id'], self.scores(), [], audit_review=self.review(sid, decisions))

    def test_audit_accepts_findings_and_handoff_only_after_confirmation(self):
        sid = self.create(); self.report(sid, finding=True)
        state = self.evaluate(sid)
        self.assertEqual(state['phase'], 'awaiting_review', state)
        decisions = [{'id': 'F1', 'disposition': 'confirmed', 'rationale': 'Independently reproduced the failure'}]
        result = self.accept(sid, decisions)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['system_health'], 'not_asserted')
        self.assertEqual(result['commit'], state['base_commit'])
        self.assertTrue(Path(b.status(sid)['audit_report']).is_file())
        fixed = b.create_session(repo=str(self.repo), task='Fix confirmed F1', questions=self.q, checks=self.checks,
                                 repair_of={'session_id': sid, 'finding_ids': ['F1']}, allowed_paths=['calc.py'])
        _, contract = self.data(fixed['session_id'])
        self.assertEqual(contract['repair_of']['packet_id'], state['packet_id'])
        with self.assertRaises(b.GateError):
            b.create_session(repo=str(self.repo), task='Fix unknown', questions=self.q, checks=self.checks,
                             repair_of={'session_id': sid, 'finding_ids': ['unknown']}, allowed_paths=['calc.py'])

    def test_required_unverified_blocks_exam_optional_limitations_need_acknowledgement(self):
        sid = self.create(); self.report(sid, status='not_tested')
        state = self.evaluate(sid)
        self.assertEqual(state['phase'], 'not_accepted', state)
        self.assertEqual(b.review_packet(sid)['evidence']['exam']['skipped'], 'executable_checks_failed')
        policy = boundary(); policy['checklist'][0]['required'] = False
        sid = self.create(policy); self.report(sid, status='blocked'); state = self.evaluate(sid)
        review = self.review(sid); review['limitations'] = []
        with self.assertRaisesRegex(b.GateError, 'acknowledge'):
            b.grade(sid, state['packet_id'], self.scores(), [], audit_review=review)
        self.assertTrue(self.accept(sid)['accepted'])

    def test_incomplete_duplicate_missing_and_fake_evidence_are_rejected(self):
        for change in ('missing', 'duplicate', 'file', 'commit', 'finding'):
            sid = self.create(); report = self.report(sid)
            if change == 'missing': report['results'] = []
            if change == 'duplicate': report['results'] *= 2
            if change == 'file': report['results'][0]['evidence'] = ['missing.txt']
            if change == 'commit': report['source_commit'] = '0' * 40
            if change == 'finding': report['results'][0]['status'] = 'finding'
            state, contract = self.data(sid); root = audit.artifacts(b, state)
            b.write_json(root / 'report.json', report)
            with self.subTest(change=change), self.assertRaises(b.GateError):
                audit.validate_report(b, state, contract, root, audit.manifest(root, 1_000_000))

    def test_artifact_tampering_before_grade_and_after_acceptance_invalidates_evidence(self):
        sid = self.create(); self.report(sid); state = self.evaluate(sid)
        root = audit.artifacts(b, state); original = (root / 'source-evidence.txt').read_bytes()
        (root / 'source-evidence.txt').write_text('tampered')
        with self.assertRaisesRegex(b.GateError, 'changed'):
            self.accept(sid)
        (root / 'source-evidence.txt').write_bytes(original)
        self.assertTrue(self.accept(sid)['accepted'])
        packet = b.read_json(b.run_dir(sid) / 'attempt-1.json')
        (Path(packet['audit']['evidence_dir']) / 'source-evidence.txt').write_text('tampered private evidence')
        with self.assertRaisesRegex(b.GateError, 'changed'):
            b.status(sid)

    def test_source_mutations_and_ignored_files_cannot_be_submitted(self):
        sid = self.create(); state, _ = self.data(sid)
        (Path(state['worktree']) / 'calc.py').write_text('changed')
        with self.assertRaisesRegex(b.GateError, 'Audit source changed'):
            b.snapshot(state, 'scope')
        self.assertEqual((self.repo / 'calc.py').read_text(), 'def add(a, b): return a + b\n')

    def test_real_sandbox_read_scope_source_write_and_network_denial(self):
        (self.repo / 'private-module.py').write_text('not in scope')
        b.git(self.repo, 'add', '.'); b.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'second module')
        sid = self.create(); state, contract = self.data(sid)
        root = audit.artifacts(b, state); source = Path(state['worktree']) / 'calc.py'
        code = ('from pathlib import Path\nimport socket\n'
                f'assert "def add" in Path({str(source)!r}).read_text()\n'
                f'for p in [{str(source)!r},{str(Path(state["worktree"]) / "new.py")!r}]:\n'
                ' try: Path(p).write_text("forbidden")\n except PermissionError: pass\n else: raise AssertionError("source writable")\n'
                f'try: Path({str(Path(state["worktree"]) / "private-module.py")!r}).read_text()\n'
                'except PermissionError: pass\nelse: raise AssertionError("outside source readable")\n'
                f'Path({str(root / "result.txt")!r}).write_text("allowed")\n'
                'try: socket.create_connection(("127.0.0.1", 9),timeout=1)\n'
                'except PermissionError: pass\nelse: raise AssertionError("network allowed")\n')
        profile = b.sandbox_profile(writable=[root], protected=[state['worktree']],
                                     readable_cache=[root, *audit.source_paths(state, contract['audit'])])
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', code], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_links_and_budget_overflow_rejected(self):
        sid = self.create(); state, _ = self.data(sid); root = audit.artifacts(b, state)
        (root / 'link').symlink_to(self.repo / 'calc.py')
        with self.assertRaisesRegex(b.GateError, 'links'):
            audit.manifest(root, 1_000_000)
        (root / 'link').unlink()
        with self.assertRaisesRegex(b.GateError, 'budget'):
            audit.manifest(root, 1)
        (root / 'parent-link').symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(b.GateError, 'links'):
            audit.read_regular(root, 'parent-link/calc.py', 10000)

    def test_explicit_boundaries_required_and_no_unrestricted_network(self):
        for edit in ({'environment': {'kind': 'production', 'name': 'real'}}, {'scope_paths': ['../escape']},
                     {'targets': [{'origin': 'http://example.invalid', 'path_prefix': '/a/../b', 'methods': ['GET']}]},
                     {'limits': {'max_probes': True}}, {'stop_conditions': []}):
            with self.subTest(edit=edit), self.assertRaises(b.GateError):
                self.create({**boundary(), **edit})
        with self.assertRaises(b.GateError): self.create(command_network=True)
        with self.assertRaises(b.GateError): self.create(allowed_paths=['calc.py'])
        with self.assertRaises(b.GateError): self.create(policy={**boundary(), 'browser': {}})

    def test_browser_actions_and_declared_viewport_coverage_are_enforced(self):
        policy = boundary(); policy['checklist'][0]['kind'] = 'visual'
        policy['targets'] = [{'origin': 'http://127.0.0.1:8000', 'path_prefix': '/ui', 'methods': ['GET']}]
        policy['browser'] = {'module_path': str(Path(__file__).resolve()), 'executable_path': str(Path(sys.executable).resolve()),
                             'actions': ['screenshot'], 'viewports': [{'name': 'desktop', 'width': 1280, 'height': 800},
                                                                    {'name': 'mobile', 'width': 390, 'height': 844}]}
        sid = self.create(policy); state, contract = self.data(sid); root = audit.artifacts(b, state)
        request = {'request_id': 'probe', 'check_id': 'addition', 'kind': 'browser', 'url': 'http://127.0.0.1:8000/ui',
                   'viewport': 'desktop', 'steps': [{'action': 'click', 'selector': '#delete'}]}
        with self.assertRaisesRegex(b.GateError, 'action'):
            audit_probe.validate_request(contract['audit'], request)
        request.update(viewport='undeclared', steps=[])
        with self.assertRaisesRegex(b.GateError, 'Viewport'):
            audit_probe.validate_request(contract['audit'], request)
        report = self.report(sid)
        (root / 'screenshot.png').write_bytes(b'trusted-driver-fixture')
        b.write_json(root / 'probe.json', {'outcome': 'passed'})
        report['results'][0]['evidence'] = ['probe.json', 'screenshot.png']; b.write_json(root / 'report.json', report)
        files = audit.manifest(root, 1_000_000)
        b.write_json(b.run_dir(sid) / 'audit-probes.json', {'completed': [{'check_id': 'addition', 'kind': 'browser',
            'viewport': 'desktop', 'evidence': 'probe.json', 'files': {n: files[n]['sha256'] for n in ('probe.json', 'screenshot.png')}}]})
        with self.assertRaisesRegex(b.GateError, 'viewport'):
            audit.validate_report(b, state, contract, root, files)

    def test_browser_probe_forces_staging_chromium_tempdir_and_private_writable_root(self):
        policy = boundary(); policy['checklist'][0]['kind'] = 'visual'
        policy['targets'] = [{'origin': 'http://127.0.0.1:8000', 'path_prefix': '/ui', 'methods': ['GET']}]
        policy['browser'] = {'module_path': str(Path(__file__).resolve()), 'executable_path': str(Path(sys.executable).resolve()),
                             'actions': ['screenshot'], 'viewports': [{'name': 'desktop', 'width': 1280, 'height': 800}]}
        sid = self.create(policy); _, contract = self.data(sid)
        request = {'request_id': 'probe', 'check_id': 'addition', 'kind': 'browser', 'url': 'http://127.0.0.1:8000/ui',
                   'viewport': 'desktop', 'steps': []}
        audit_probe.validate_request(contract['audit'], request)
        folder = Path(tempfile.mkdtemp(prefix='telos-audit-test-')).resolve()
        captured = {}
        real_popen = subprocess.Popen

        class FakeProcess:
            returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def communicate(self, data, timeout=None):
                captured['payload'] = json.loads(data)
                return json.dumps({'outcome': 'passed'}).encode(), b''

        def launch(argv, **kwargs):
            if Path(argv[0]).name != 'sandbox-exec':
                return real_popen(argv, **kwargs)
            captured.update(argv=argv, kwargs=kwargs)
            return FakeProcess()

        try:
            # Simulate an inherited external value: the browser child env must
            # overwrite it with this probe's staging folder, never pass it on.
            inherited = {'PATH': os.environ.get('PATH', '/usr/bin'), 'MAC_CHROMIUM_TMPDIR': '/inherited/leak'}
            with patch.object(b, 'minimal_env', return_value=dict(inherited)), \
                    patch.object(audit_probe.subprocess, 'Popen', side_effect=launch):
                result = audit_probe.browser_probe(b, contract['audit'], request, folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self.assertEqual(result['kind'], 'browser'); self.assertEqual(result['outcome'], 'passed')
        env = captured['kwargs']['env']
        self.assertEqual(env['MAC_CHROMIUM_TMPDIR'], str(folder))
        self.assertEqual(env['HOME'], str(folder)); self.assertEqual(env['TMPDIR'], str(folder))
        self.assertEqual(Path(captured['kwargs']['cwd']).resolve(), folder)
        self.assertEqual(captured['payload']['folder'], str(folder))
        self.assertEqual(captured['kwargs']['stdin'], subprocess.PIPE)
        argv = captured['argv']
        self.assertEqual(argv[:2], ['/usr/bin/sandbox-exec', '-p'])
        self.assertEqual(argv[-1], str(Path(audit_probe.__file__).with_name('audit_browser.cjs')))
        profile = argv[2]
        writable = {json.loads(line[len('(allow file-write* (subpath '):].rstrip('))'))
                    for line in profile.splitlines() if line.startswith('(allow file-write* (subpath ')}
        self.assertEqual(writable, {str(folder)})
        self.assertNotIn('(deny network*)', profile.splitlines())

    def test_audit_archive_preserves_report_screenshots_and_acceptance(self):
        sid = self.create(); self.report(sid); self.evaluate(sid); self.accept(sid)
        result = b.storage_manager().manage('cleanup', sid, dry_run=False)
        self.assertTrue(b.status(sid)['accepted'])
        archive = b.read_json(Path(result['archive']) / 'manifest.json')
        self.assertTrue(any('audit-evidence-1-' in x and x.endswith('/source-evidence.txt') for x in archive['files']))

    def test_cancelled_audit_cleanup_preserves_unsubmitted_findings(self):
        sid = self.create(); report = self.report(sid, finding=True)
        state, _ = self.data(sid)
        b.return_control(sid, 'cancel')
        result = b.storage_manager().manage('cleanup', sid, dry_run=False)
        preserved = Path(result['archive']) / 'audit-artifacts' / sid
        self.assertEqual(b.read_json(preserved / 'report.json'), report)
        self.assertTrue((preserved / 'source-evidence.txt').is_file())
        self.assertFalse(audit.artifacts(b, state).exists())

    def test_exam_infrastructure_retry_preserves_old_evidence_and_can_complete(self):
        sid = self.create(); self.report(sid)
        state = self.evaluate(sid, b.InfrastructureError('temporary upstream outage'))
        self.assertEqual(state['rounds'], 0)
        state['next_retry_at'] = 0; b.write_json(b.run_dir(sid) / 'state.json', state)
        state = self.evaluate(sid)
        self.assertEqual(state['phase'], 'awaiting_review', state)
        self.assertEqual(len(list(b.run_dir(sid).glob('audit-evidence-*'))), 2)
        self.assertTrue(self.accept(sid)['accepted'])

    def test_mcp_owner_guard_and_schema(self):
        sid = self.create(); state, _ = self.data(sid)
        state['owner'] = {'thread_id': 'owner'}; b.write_json(b.run_dir(sid) / 'state.json', state)
        with self.assertRaisesRegex(b.GateError, 'another Codex thread'):
            b.call_tool('telos_audit_probe', {'session_id': sid, 'request': {}}, {'threadId': 'other'})
        self.assertIn('audit', b.TOOLS[0]['inputSchema']['properties'])
        self.assertIn('audit_review', next(t for t in b.TOOLS if t['name'] == 'telos_grade')['inputSchema']['properties'])


class AuditHTTPTests(AuditTests):
    def setUp(self):
        super().setUp()
        calls = self.calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                calls.append(self.path)
                if self.path == '/slow': time.sleep(2)
                self.send_response(302 if self.path == '/redirect' else 500)
                if self.path == '/redirect': self.send_header('Location', '/forbidden')
                self.end_headers()
                try: self.wfile.write(b'fixture bug')
                except BrokenPipeError: pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        super().tearDown()

    def policy(self):
        policy = boundary(); policy['checklist'][0]['kind'] = 'api'
        policy['targets'] = [{'origin': self.url, 'path_prefix': '/', 'methods': ['GET']}]
        return policy

    def request(self, path='/broken', **kwargs):
        return {'request_id': 'first', 'check_id': 'addition', 'kind': 'http', 'url': self.url + path, **kwargs}

    def test_business_failure_is_evidence_and_request_ids_are_idempotent(self):
        sid = self.create(self.policy())
        first = audit_probe.run(b, sid, self.request())
        self.assertEqual(first['status_code'], 500)
        self.assertEqual(audit_probe.run(b, sid, self.request()), first)
        self.assertEqual(self.calls, ['/broken'])
        with self.assertRaises(b.GateError): audit_probe.run(b, sid, self.request('/different'))
        report = self.report(sid, finding=True)
        report['results'][0]['evidence'] = [first['evidence']]
        report['findings'][0]['evidence'] = [first['evidence']]
        b.write_json(audit.artifacts(b, self.data(sid)[0]) / 'report.json', report)
        self.assertEqual(self.evaluate(sid)['phase'], 'awaiting_review')
        self.assertTrue(self.accept(sid, [{'id': 'F1', 'disposition': 'confirmed', 'rationale': 'Reproduced HTTP 500'}])['accepted'])

    def test_redirect_methods_scope_budget_pause_and_late_probe_enforced(self):
        policy = self.policy(); policy['limits'] = {'max_probes': 1}
        sid = self.create(policy)
        for request in (self.request(method='POST'), self.request(url='http://example.invalid'), self.request('/%2e%2e/escape')):
            with self.assertRaises(b.GateError): audit_probe.run(b, sid, request)
        result = audit_probe.run(b, sid, self.request('/redirect'))
        self.assertEqual(result['status_code'], 302)
        self.assertEqual(self.calls, ['/redirect'])
        with self.assertRaisesRegex(b.GateError, 'budget'): audit_probe.run(b, sid, self.request(request_id='second'))
        state, _ = self.data(sid); state['continuation'] = 'paused'; b.write_json(b.run_dir(sid) / 'state.json', state)
        with self.assertRaises(b.GateError): audit_probe.run(b, sid, self.request('/redirect'))
        state.update(continuation='active', phase='awaiting_review'); b.write_json(b.run_dir(sid) / 'state.json', state)
        with self.assertRaises(b.GateError): audit_probe.run(b, sid, self.request('/redirect'))

    def test_fabricated_or_modified_probe_evidence_is_not_trusted(self):
        sid = self.create(self.policy()); self.report(sid)
        state, contract = self.data(sid); root = audit.artifacts(b, state)
        with self.assertRaisesRegex(b.GateError, 'bridge-captured'):
            audit.validate_report(b, state, contract, root, audit.manifest(root, 1_000_000))
        result = audit_probe.run(b, sid, self.request()); report = self.report(sid)
        report['results'][0]['evidence'] = [result['evidence']]; b.write_json(root / 'report.json', report)
        (root / result['evidence']).write_text('fabricated replacement')
        with self.assertRaisesRegex(b.GateError, 'bridge-captured'):
            audit.validate_report(b, state, contract, root, audit.manifest(root, 1_000_000))

    def test_http_wall_clock_budget_and_uncertain_request_are_enforced(self):
        policy = self.policy(); policy['limits'] = {'probe_timeout_seconds': 1}
        sid = self.create(policy); start = time.monotonic()
        result = audit_probe.run(b, sid, self.request('/slow'))
        self.assertEqual(result['outcome'], 'blocked')
        self.assertIn('timeout', result['error'])
        self.assertLess(time.monotonic() - start, 1.8)
        request = self.request(request_id='uncertain')
        path = b.run_dir(sid) / 'audit-probes.json'; ledger = b.read_json(path)
        ledger['requests']['uncertain'] = {'hash': b.digest(request)}; b.write_json(path, ledger)
        before = list(self.calls)
        with self.assertRaisesRegex(b.GateError, 'uncertain'):
            audit_probe.run(b, sid, request)
        self.assertEqual(self.calls, before)


class AuditProjectTests(AuditTests):
    task = ProjectTests.task
    start = ProjectTests.start
    state = ProjectTests.state
    check_sync = ProjectTests.check_sync

    def test_overlapping_auditors_and_final_project_evidence(self):
        tasks = [self.task(name, task_type='audit', audit=boundary(), checks=QUALITY) for name in ('one', 'two')]
        for task in tasks: task['allowed_paths'] = []
        checks = [{'id': 'audit-project', 'argv': ['{python}', '-c',
                  'import os,json,pathlib; p=pathlib.Path(os.environ["TELOS_AUDIT_REPORTS"]); assert len(list(p.glob("*/report.json")))==2']}]
        pid = b.project_manager().create(repo=str(self.repo), task='Audit system', tasks=tasks, questions=self.q,
                                         checks=checks, decision_reason='Independent read-only inspections', strategy='parallel', notify=False)['project_id']
        self.start(pid)
        project = self.state(pid)
        self.assertTrue(all(t['phase'] == 'running' for t in project['tasks']), project)
        for task in project['tasks']:
            sid = task['session_id']; self.report(sid); self.evaluate(sid); self.accept(sid)
        b.project_manager().integrate(pid)
        packet = self.check_sync(pid)
        self.assertEqual(set(packet['evidence']['audits']), {'one', 'two'})
        result = b.project_manager().finish(pid, packet['packet_id'], self.scores(), [], 'Inspected both reports and integrated coverage')
        self.assertEqual(result['phase'], 'accepted')


class AuditExecutorTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    create = AuditTests.create
    data = AuditTests.data
    scores = AuditTests.scores
    model = WorkflowTests.model

    def test_real_executor_receives_public_boundaries_has_tools_and_keeps_private_exam_separate(self):
        cfg = configured(); cfg['job_timeout'] = 35; b.write_json(b.CONFIG, cfg)
        sid = self.create(); state, contract = self.data(sid)
        state.update(phase='running', job_kind='send', job_id='a' * 32)
        b.write_json(b.run_dir(sid) / 'state.json', state)
        code = '''import json,os,urllib.request,urllib.error
from pathlib import Path
root=Path(os.environ['TELOS_AUDIT_ARTIFACTS'])
source=Path(os.environ['TELOS_AUDIT_SOURCE'])/'calc.py'
content=source.read_text()
try: source.write_text('forbidden')
except PermissionError: pass
else: raise AssertionError('audit source was writable')
request=urllib.request.Request(os.environ['TELOS_AUDIT_URL'],data=b'{}',headers={'Authorization':'Bearer '+os.environ['TELOS_AUDIT_TOKEN'],'Content-Type':'application/json'})
try: urllib.request.urlopen(request)
except urllib.error.HTTPError as e: assert e.code==400
else: raise AssertionError('invalid probe accepted')
(root/'source-evidence.txt').write_text(content)
report=json.loads((root/'report.json').read_text())
report['results'][0].update(status='passed',actual='Source implements integer addition',steps=['Read scoped source'],evidence=['source-evidence.txt'])
(root/'report.json').write_text(json.dumps(report))
print('AUDIT_SOURCE_PROTECTED_AND_EVIDENCE_WRITTEN')
'''
        command = shlex.join([sys.executable, '-c', code])
        answers = json.dumps({'answers': [{'id': q['id'], 'answer': 'Reviewed integer arithmetic'} for q in self.q]})
        def reply(request, number):
            if number == 1:
                name = 'shell_command' if any(t.get('name') == 'shell_command' for t in request['tools']) else 'exec_command'
                args = {'command': command} if name == 'shell_command' else {'cmd': command}
                return {'type': 'function_call', 'id': 'audit_tool', 'call_id': 'audit_call', 'name': name, 'arguments': json.dumps(args)}
            return message(answers if request.get('tool_choice') == 'none' else 'Audit report and evidence ready.')
        with self.model(reply) as captured:
            result = b.peer_turn(state, contract, 'Perform the frozen public audit.')
            self.assertIn('AUDIT_SOURCE_PROTECTED_AND_EVIDENCE_WRITTEN', json.dumps(captured[-1]))
            self.assertIn('PUBLIC AUDIT CONTRACT', json.dumps(captured[0]))
            self.assertNotIn(self.q[0]['reference'], json.dumps(captured))
            self.assertNotIn(QUALITY[0]['argv'][2], json.dumps(captured))
            state.update(thread_id=result['thread_id'], phase='responded'); b.write_json(b.run_dir(sid) / 'state.json', state)
            real_popen = subprocess.Popen
            def launch(argv, **kwargs):
                return Mock(pid=os.getpid()) if '_job' in argv else real_popen(argv, **kwargs)
            with patch.object(b.subprocess, 'Popen', side_effect=launch):
                job = b.start_job(sid, 'evaluate')['job_id']
            b.background_job(sid, job)
            self.assertEqual(captured[-1]['tools'], [])
        final = self.data(sid)[0]
        self.assertEqual(final['phase'], 'awaiting_review', final)
        self.assertEqual(b.review_packet(sid)['evidence']['exam']['executor_thread_id'], result['thread_id'])


del BridgeTests, ProjectTests, WorkflowTests

if __name__ == '__main__':
    unittest.main()

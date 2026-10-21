"""Acceptance regressions with real Git and Seatbelt; no upstream model calls."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from test_telos import b, BridgeTests, synthetic_exam
from test_projects import ProjectTests
from test_callbacks import OWNER, OTHER


class IntegrityTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    scores = BridgeTests.scores

    def create(self, **kw):
        return b.create_session(**{'repo': str(self.repo), 'task': 'Implement addition.',
            'questions': self.q, 'checks': self.checks, 'notify': False,
            'use_experience': False, **kw})['session_id']

    def state(self, sid):
        return b.read_json(b.run_dir(sid) / 'state.json')

    def job(self, sid, kind, summary='Updated addition; checked positive inputs; negative inputs remain.'):
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            job = b.start_job(sid, kind, 'Complete the assigned change.')['job_id']
        with patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': summary}), \
                patch.object(b, 'closed_book_exam', side_effect=synthetic_exam), \
                patch.object(b, 'provider_credential', return_value=('', 'fixture')):
            b.background_job(sid, job)
        state = self.state(sid)
        self.assertNotEqual(state['phase'], 'error', state.get('last_error'))
        return state

    def test_checks_cannot_repair_bad_submission_before_acceptance(self):
        mutate = "from pathlib import Path; Path('calc.py').write_text('def add(a,b): return a+b\\n')"
        sid = self.create(checks=[{'id': 'project-tests', 'argv': ['{python}', '-c', mutate]}, *self.checks])
        (Path(self.state(sid)['worktree']) / 'calc.py').write_text('def add(a,b): return 0\n')
        self.job(sid, 'send')
        state = self.job(sid, 'evaluate')
        packet = b.review_packet(sid)['evidence']
        self.assertEqual([x['status'] for x in packet['checks']], ['failed', 'failed'])
        self.assertEqual(packet['exam']['skipped'], 'executable_checks_failed')
        self.assertEqual(state['phase'], 'not_accepted')
        self.assertIn('return 0', b.git(state['worktree'], 'show', packet['commit'] + ':calc.py'))
        with self.assertRaises(b.GateError):
            b.grade(sid, state['packet_id'], self.scores(), [])

    def test_read_only_source_blocks_replace_rename_and_shadow_but_allows_outputs(self):
        sid = self.create()
        state = self.state(sid)
        tree = Path(state['worktree'])
        (tree / 'nested').mkdir()
        (tree / 'nested/source.py').write_text('VALUE = 1\n')
        _, target = b.snapshot(state, 'readonly')
        code = '''import os
from pathlib import Path
root = Path.cwd()
scratch = Path(os.environ['TELOS_CHECK_OUTPUT'])
assert not scratch.is_relative_to(root)
(scratch / 'build.txt').write_text('built')
Path(os.environ['TMPDIR'], 'temp').write_text('temporary')
for action in [lambda: Path('calc.py').write_text('bad'),
               lambda: Path('calc.py').unlink(),
               lambda: Path('nested').rename('moved'),
               lambda: root.rename(root.with_name('moved-root')),
               lambda: (scratch / 'build.txt').replace(root / 'calc.py'),
               lambda: Path('calc').mkdir(),
               lambda: Path('calc.py').chmod(0o777)]:
    try: action()
    except OSError: pass
    else: raise AssertionError('source mutation escaped isolation')
from calc import add
assert add(2,3) == 5
print(scratch)
'''
        result = b.executable_checks({'checks': [{'id': 'isolation', 'argv': ['{python}', '-c', code]}]}, target)[0]
        self.assertEqual(result['status'], 'passed', result)
        self.assertFalse(Path(result['stdout'].strip()).exists(), 'scratch must be cleaned')
        self.assertEqual((target / 'calc.py').read_text(), 'def add(a, b): return a + b\n')

    def test_delivery_survives_evaluation_default_views_and_repair_history(self):
        sid = self.create()
        raw = json.dumps({'summary': 'Addition updated', 'changed_files': ['calc.py'],
            'checks_performed': ['local positive input check'], 'remaining_issues': ['Review negative inputs']})
        self.job(sid, 'send', raw)
        first = b.call_tool('telos_status', {'session_id': sid})['delivery']
        self.assertEqual(first['format'], 'structured')
        self.assertEqual(first['remaining_issues'], ['Review negative inputs'])
        self.job(sid, 'evaluate')
        review = b.call_tool('telos_review', {'session_id': sid})
        delivery = review['evidence']['delivery']
        self.assertEqual(delivery['commit'], review['submitted_commit'])
        self.assertEqual(delivery['job_id'], first['job_id'])
        self.assertEqual(delivery['trust'], 'executor_claims')
        self.assertEqual(b.call_tool('telos_status', {'session_id': sid})['delivery'], delivery)
        first_packet = b.read_json(b.run_dir(sid) / 'attempt-1.json')
        b.grade(sid, review['packet_id'], self.scores(), ['Inspect edge cases'])
        self.job(sid, 'send', 'Second delivery with checks and pending review.')
        self.assertEqual(b.read_json(b.run_dir(sid) / 'attempt-1.json'), first_packet)
        self.assertTrue((b.run_dir(sid) / ('delivery-' + first['job_id'] + '.json')).exists())
        self.assertEqual(b.call_tool('telos_status', {'session_id': sid})['delivery']['format'], 'text')

    def test_legacy_text_delivery_is_retained_without_inventing_checks(self):
        sid = self.create()
        marker = 'Legacy delivery: calc.py updated; check details unavailable.'
        self.job(sid, 'send', marker)
        self.job(sid, 'evaluate')
        summary = b.call_tool('telos_review', {'session_id': sid})['evidence']['delivery']
        self.assertEqual(summary['summary'], marker)
        self.assertIsNone(summary['checks_performed'])
        self.assertIsNone(summary['remaining_issues'])

    def test_status_owner_is_checked_before_any_side_effects(self):
        sid = self.create()
        state = self.state(sid)
        state['owner'] = {'thread_id': OWNER}
        b.write_json(b.run_dir(sid) / 'state.json', state)
        before = (b.run_dir(sid) / 'state.json').read_bytes()
        for meta in (None, {'threadId': OTHER}):
            for detail in ('summary', 'full'):
                with self.subTest(meta=meta, detail=detail), patch.object(b, 'status') as status:
                    with self.assertRaisesRegex(b.GateError, 'another Codex thread'):
                        b.call_tool('telos_status', {'session_id': sid, 'detail': detail}, meta)
                    status.assert_not_called()
        self.assertEqual((b.run_dir(sid) / 'state.json').read_bytes(), before)
        self.assertEqual(b.call_tool('telos_status', {'session_id': sid}, {'threadId': OWNER})['phase'], 'ready')
        manual = self.create()
        self.assertEqual(b.call_tool('telos_status', {'session_id': manual})['phase'], 'ready')

    def test_initial_public_fields_reject_private_material_before_creation(self):
        for field in ('task', 'context'):
            for private in (self.q[0]['reference'], self.checks[0]['argv'][2]):
                with self.subTest(field=field, private=private):
                    before = b.git(self.repo, 'worktree', 'list', '--porcelain')
                    with self.assertRaisesRegex(b.GateError, 'private'):
                        self.create(**{field: 'Public brief: ' + private})
                    self.assertEqual(b.git(self.repo, 'worktree', 'list', '--porcelain'), before)

    def test_initial_experience_context_is_checked(self):
        with patch.object(type(b.experience_manager()), 'context', return_value=(self.q[0]['reference'], ['x'])):
            with self.assertRaisesRegex(b.GateError, 'private'):
                self.create(use_experience=True)

    def test_valid_json_with_bad_state_fields_cannot_break_global_scheduling(self):
        sid = self.create()
        original = self.state(sid)
        path = b.run_dir(sid) / 'state.json'
        for field, value in [('phase', []), ('events', []), ('owner', []), ('turns', '1'),
                             ('job_started', 10**400), ('job_started', float('nan')),
                             ('job_pid', 10**100), ('job_process_start', []), ('last_event_id', [])]:
            with self.subTest(field=field, value_type=type(value).__name__):
                b.write_json(path, {**original, field: value})
                raw = path.read_bytes()
                self.assertTrue(b.project_manager().capacity(None, 'ark'))
                with self.assertRaisesRegex(b.GateError, 'quarantined'):
                    b.status(sid)
                self.assertEqual(path.read_bytes(), raw)


class ProjectIntegrityTests(unittest.TestCase):
    setUp = ProjectTests.setUp
    tearDown = ProjectTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child
    submit = ProjectTests.submit
    check_sync = ProjectTests.check_sync
    scores = ProjectTests.scores

    def test_initial_child_fields_reject_leaks_without_project_state(self):
        for field in ('task', 'context', 'message'):
            task = self.task('a')
            task[field] = 'Public brief: ' + self.q[0]['reference']
            with self.subTest(field=field), self.assertRaisesRegex(b.GateError, 'private'):
                self.create([task])
        self.assertFalse((b.STATE / 'projects').exists())

    def test_corrupt_child_is_quarantined_while_healthy_work_continues(self):
        pid = self.create([self.task('a'), self.task('b')])
        sid = self.state(pid)['tasks'][0]['session_id']
        root = b.run_dir(sid)
        root.mkdir(parents=True)
        for malformed in ('{broken-json', '[]', '{}', json.dumps({'phase': 'running', 'owner': []})):
            (root / 'state.json').write_text(malformed)
            view = b.project_manager().status(pid)
            self.assertEqual(view['tasks'][0]['phase'], 'quarantined')
            self.assertIsNone(view['tasks'][0]['accepted_commit'])
            self.assertTrue(view['usage']['incomplete'])
            self.assertIn('a', view['usage']['unavailable_tasks'])
            self.assertEqual((root / 'state.json').read_text(), malformed)
        started = self.start(pid)
        self.assertEqual(started['tasks'][1]['phase'], 'running')
        single = b.create_session(str(self.repo), 'Independent work', self.q, self.checks, notify=False)['session_id']
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            self.assertEqual(b.start_job(single, 'send', 'Do the assigned work')['phase'], 'running')
        with self.assertRaises(b.GateError):
            b.call_tool('telos_status', {'session_id': sid})

    def test_corrupt_running_child_still_reserves_capacity(self):
        pid = self.create([self.task('a'), self.task('b'), self.task('c')])
        self.start(pid)
        sid = self.state(pid)['tasks'][0]['session_id']
        (b.run_dir(sid) / 'state.json').write_text('{bad')
        self.assertFalse(b.project_manager().capacity(self.state(pid), 'ark'))

    def test_project_checks_cannot_mutate_integration_source(self):
        mutation = "from pathlib import Path; Path('calc.py').write_text('bad')"
        pid = self.create([self.task('a')], checks=[{'id': 'mutate', 'argv': ['{python}', '-c', mutation]}])
        self.start(pid)
        self.submit(pid, 'a')
        b.project_manager().integrate(pid)
        review = self.check_sync(pid)
        self.assertEqual(review['evidence']['checks'][0]['status'], 'failed')
        self.assertEqual(self.state(pid)['phase'], 'integration_failed')

    def test_jobs_defaults_and_small_explicit_budget_remain_unchanged(self):
        pid = self.create([self.task('t' + str(i)) for i in range(21)])
        self.assertEqual(self.state(pid)['max_jobs'], 40)
        small = self.create([self.task('a')], max_jobs=1)
        self.assertEqual(self.state(small)['max_jobs'], 1)

    def test_project_review_retains_commit_bound_child_delivery(self):
        pid = self.create([self.task('a')])
        self.start(pid)
        state = self.child(pid, 'a')
        sid = state['session_id']
        marker = 'Delivered a complete change; remaining external integration review.'
        with patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': marker}):
            b.background_job(sid, state['job_id'])
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            job = b.start_job(sid, 'evaluate')['job_id']
        with patch.object(b, 'closed_book_exam', side_effect=synthetic_exam):
            b.background_job(sid, job)
        state = self.child(pid, 'a')
        b.grade(sid, state['packet_id'], self.scores(), [])
        b.project_manager().integrate(pid)
        self.check_sync(pid)
        review = b.call_tool('telos_project_review', {'project_id': pid})
        attachment = review['evidence']['deliveries']['a']
        self.assertEqual(attachment['delivery']['summary'], marker)
        self.assertEqual(attachment['delivery']['commit'], attachment['commit'])
        self.assertEqual(attachment['packet_id'], state['packet_id'])

    def test_corrupt_courier_does_not_hide_healthy_children(self):
        pid = self.create([self.task('a')])
        path = b.run_dir(pid) / 'state.json'
        saved = path.read_bytes()
        path.write_text('[]')
        view = self.start(pid)
        self.assertEqual(view['tasks'][0]['phase'], 'running')
        self.assertEqual(view['return_health']['status'], 'quarantined')
        self.assertEqual(path.read_text(), '[]')
        path.write_bytes(saved)
        self.assertNotEqual(b.project_manager().status(pid)['return_health']['status'], 'quarantined')

    def test_missing_running_state_uses_lease_and_recovery_is_explicit(self):
        pid = self.create([self.task('a')], execution_mode='single')
        self.start(pid)
        sid = self.state(pid)['tasks'][0]['session_id']
        path = b.run_dir(sid) / 'state.json'
        saved = path.read_bytes()
        path.unlink()
        view = b.project_manager().status(pid)
        self.assertEqual(view['tasks'][0]['phase'], 'quarantined')
        self.assertFalse(b.project_manager().capacity(self.state(pid), 'ark'))
        path.write_bytes(saved)
        self.assertEqual(b.project_manager().status(pid)['tasks'][0]['phase'], 'running')
        self.assertFalse((b.STATE / 'quarantine' / (sid + '.json')).exists())

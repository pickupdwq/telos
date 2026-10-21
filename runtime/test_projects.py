"""Behavior tests: real Git worktrees/snapshots/checks, deterministic model boundary."""
import copy
import contextlib
from collections import Counter
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from test_telos import b, BridgeTests, synthetic_exam
from test_callbacks import FakeRPC, OWNER, OTHER
from projects import Projects
from telos import WATCH_EVENTS, digest
from usage import BudgetExceeded


class ProjectTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    scores = BridgeTests.scores

    def task(self, ident, deps=(), paths=None, **kw):
        return {'id': ident, 'task': 'Implement ' + ident, 'message': 'Implement the assigned change and run relevant checks.',
                'depends_on': list(deps), 'allowed_paths': paths or [ident + '.py'],
                'questions': copy.deepcopy(self.q), 'checks': copy.deepcopy(self.checks), 'assessment_mode': 'closed_book', **kw}

    def create(self, tasks=None, **kw):
        return b.project_manager().create(repo=str(self.repo), task='Complete project',
            tasks=tasks or [self.task('a'), self.task('b'), self.task('c')], questions=self.q,
            checks=kw.pop('checks', self.checks), decision_reason='Independent files can be implemented concurrently',
            strategy=kw.pop('strategy', 'parallel'), notify=kw.pop('notify', False), **kw)['project_id']

    def start(self, pid):
        # Keep real create_session/_start_job state and Git operations, only skip
        # launching model subprocesses. PIDs are this process for recovery checks.
        original = b._start_job
        def start(*args, **kw):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kw)
        with patch.object(b, '_start_job', start), patch.object(b, 'spawn_dispatch'):
            return b.project_manager().tick(pid)

    def state(self, pid):
        return b.project_manager().load(pid)

    def child(self, pid, tid):
        t = next(t for t in self.state(pid)['tasks'] if t['id'] == tid)
        return b.read_json(b.run_dir(t['session_id']) / 'state.json')

    def submit(self, pid, tid, changes=None, passed=True):
        s = self.child(pid, tid)
        for path, text in (changes or {tid + '.py': 'VALUE = 1\n'}).items():
            target = Path(s['worktree']) / path
            target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text)
        commit, snapshot = b.snapshot(s, 'test-' + tid)
        contract = b.read_json(b.run_dir(s['session_id']) / 'contract.json')
        checks = b.executable_checks(contract, snapshot)
        if not passed:
            checks[0]['status'] = 'failed'
        packet = {'session_id': s['session_id'], 'round': 1, 'commit': commit, 'contract_hash': s['contract_hash'],
                  'checks': checks, 'exam': {'answers': [{'id': 'q1', 'answer': '5'}, {'id': 'q2', 'answer': '3'}]}}
        if contract.get('workflow_version') == 3:
            packet['exam'] = synthetic_exam(s, contract, commit)
        root = b.run_dir(s['session_id'])
        b.write_json(root / 'attempt-1.json', packet)
        s.update(phase='awaiting_review', rounds=1, packet_id=b.digest(packet), submitted_commit=commit)
        b.write_json(root / 'state.json', s)
        return b.grade(s['session_id'], s['packet_id'], self.scores(), [])

    def check_sync(self, pid):
        real_popen = subprocess.Popen
        def launch(argv, **kwargs):
            return Mock(pid=os.getpid()) if '_project_check' in argv else real_popen(argv, **kwargs)
        with patch.object(b.subprocess, 'Popen', side_effect=launch):
            b.project_manager().check(pid)
        b.project_manager().verify_job(pid, self.state(pid)['verification_id'])
        return b.project_manager().review(pid)

    def test_auto_parallel_three_and_fourth_queued(self):
        pid = self.create([self.task(x) for x in 'abcd'])
        result = self.start(pid)
        self.assertEqual([t['phase'] for t in result['tasks']], ['running', 'running', 'running', 'pending'])
        self.assertEqual(len({self.child(pid, x)['worktree'] for x in 'abc'}), 3)
        self.start(pid)
        self.assertEqual(self.state(pid)['jobs_started'], 3)
        self.submit(pid, 'a')
        self.start(pid)
        self.assertEqual(self.child(pid, 'd')['phase'], 'running')

    def test_user_single_overrides_supervisor_parallel(self):
        pid = self.create(execution_mode='single')
        self.assertEqual(sum(t['phase'] == 'running' for t in self.start(pid)['tasks']), 1)

    def test_auto_single_strategy_runs_one(self):
        pid = self.create(strategy='single')
        self.assertEqual(sum(t['phase'] == 'running' for t in self.start(pid)['tasks']), 1)

    def test_dependency_waits_for_review_and_inherits_approved_code(self):
        pid = self.create([self.task('a'), self.task('b', ['a'])])
        self.start(pid)
        self.assertEqual(self.state(pid)['tasks'][1]['phase'], 'pending')
        self.submit(pid, 'a')
        self.start(pid)
        second = self.child(pid, 'b')
        self.assertEqual((Path(second['worktree']) / 'a.py').read_text(), 'VALUE = 1\n')
        contract = b.read_json(b.run_dir(second['session_id']) / 'contract.json')
        self.assertIn('a', contract['dependency_pins'])

    def test_cycle_unknown_and_overlapping_writers_rejected(self):
        for tasks in ([self.task('a', ['b']), self.task('b', ['a'])],
                      [self.task('a', ['missing'])],
                      [self.task('a', paths=['src']), self.task('b', paths=['src/api.py'])]):
            with self.assertRaises(b.GateError):
                self.create(tasks)

    def test_paths_cannot_escape_or_use_globs(self):
        for path in ['../file', '/tmp/file', '.git/config', 'src/../file', '*.py', 'src//file']:
            with self.subTest(path=path), self.assertRaises(b.GateError):
                self.create([self.task('a', paths=[path])])

    def test_out_of_scope_changes_rejected(self):
        pid = self.create([self.task('a')]); self.start(pid)
        s = self.child(pid, 'a')
        (Path(s['worktree']) / 'calc.py').write_text('raise Exception()\n')
        with self.assertRaisesRegex(b.GateError, 'outside task scope'):
            b.snapshot(s, 1)

    def test_parent_accepted_then_mutated_blocks_dependents(self):
        pid = self.create([self.task('a'), self.task('b', ['a'])]); self.start(pid); self.submit(pid, 'a')
        (Path(self.child(pid, 'a')['worktree']) / 'a.py').write_text('changed\n')
        self.start(pid)
        self.assertEqual(self.state(pid)['tasks'][1]['phase'], 'pending')
        with self.assertRaises(b.GateError):
            b.project_manager().integrate(pid)

    def test_failed_task_does_not_block_independent_work(self):
        pid = self.create([self.task('a'), self.task('b', ['a']), self.task('c')]); self.start(pid)
        self.submit(pid, 'a', passed=False); result = self.start(pid)
        self.assertEqual([t['phase'] for t in result['tasks']], ['not_accepted', 'pending', 'running'])

    def test_pause_cancel_and_late_results(self):
        pid = self.create([self.task('a'), self.task('b', ['a'])]); self.start(pid)
        b.project_manager().control(pid, 'pause')
        s = self.child(pid, 'a'); s.update(phase='responded')
        b.write_json(b.run_dir(s['session_id']) / 'state.json', s)
        self.start(pid)
        self.assertEqual(self.state(pid)['tasks'][1]['phase'], 'pending')
        with self.assertRaises(b.GateError):
            b.start_job(s['session_id'], 'send', 'continue')
        b.project_manager().control(pid, 'cancel')
        with self.assertRaises(b.GateError):
            b.project_manager().control(pid, 'resume')

    def test_provider_concurrency_shared_across_projects(self):
        one = self.create(); two = self.create()
        self.start(one); result = self.start(two)
        self.assertTrue(all(t['phase'] == 'pending' for t in result['tasks']))

    def test_budget_and_recovery_do_not_repeat_running_jobs(self):
        pid = self.create(max_jobs=1)
        self.start(pid)
        b.project_manager().recover()
        self.assertEqual(self.state(pid)['jobs_started'], 1)
        self.assertEqual(self.child(pid, 'a')['turns'], 1)
        self.assertEqual(self.state(pid)['tasks'][1]['phase'], 'pending')

    def test_task_budgets_freeze_from_task_or_configured_default(self):
        config = b.config()
        config['task_budget'] = {'max_requests': 2}
        b.write_json(b.CONFIG, config)
        pid = self.create([self.task('a', budget={'max_requests': 1}), self.task('b'),
                           self.task('c', budget=None)])
        by_id = {task['id']: task for task in self.state(pid)['tasks']}
        self.assertEqual(by_id['a']['budget'], {'max_requests': 1})
        self.assertEqual(by_id['b']['budget'], {'max_requests': 2})
        self.assertEqual(by_id['c']['budget'], {'max_requests': 2})
        self.start(pid)
        for task_id, expected in (('a', {'max_requests': 1}), ('b', {'max_requests': 2}),
                                  ('c', {'max_requests': 2})):
            child = self.child(pid, task_id)
            _, contract = b.load_session(b.run_dir(child['session_id']))
            self.assertEqual(contract['budget'], expected)
        for invalid in ({'max_requests': True}, {'max_requests': 0}, {'unknown': 1}, {}):
            with self.subTest(invalid=invalid), self.assertRaises(b.GateError):
                self.create([self.task('bad', budget=invalid)])

    def test_full_project_gate_real_git_and_tests(self):
        pid = self.create(); self.start(pid)
        for name in 'abc':
            self.assertTrue(self.submit(pid, name)['accepted'])
        before = b.git(self.repo, 'rev-parse', 'HEAD')
        result = b.project_manager().integrate(pid)
        self.assertFalse(result['accepted'])
        tree = Path(result['integration']['worktree'])
        self.assertTrue(all((tree / (name + '.py')).exists() for name in 'abc'))
        review = self.check_sync(pid)
        result = b.project_manager().finish(pid, review['packet_id'], self.scores(), [], 'Inspected integrated modules and real test evidence')
        self.assertTrue(result['accepted'])
        self.assertEqual(before, b.git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual(b.git(self.repo, 'status', '--porcelain'), '')
        (tree / 'a.py').write_text('changed\n')
        self.assertFalse(b.project_manager().status(pid)['accepted'])

    def test_children_pass_but_integration_fails(self):
        checks = [{'id': 'whole', 'argv': ['{python}', '-c', 'raise AssertionError("integration failure")']}]
        pid = self.create(checks=checks); self.start(pid)
        for name in 'abc': self.submit(pid, name)
        b.project_manager().integrate(pid)
        review = self.check_sync(pid)
        self.assertEqual(self.state(pid)['phase'], 'integration_failed')
        with self.assertRaises(b.GateError):
            b.project_manager().finish(pid, review['packet_id'], self.scores(), [], 'claim passed')

    def test_code_change_after_checks_requires_recheck(self):
        pid = self.create([self.task('a')]); self.start(pid); self.submit(pid, 'a')
        b.project_manager().integrate(pid); review = self.check_sync(pid)
        (Path(self.state(pid)['integration']['worktree']) / 'a.py').write_text('different\n')
        with self.assertRaises(b.GateError):
            b.project_manager().finish(pid, review['packet_id'], self.scores(), [], 'review')

    def test_corrupt_project_contract_rejected(self):
        pid = self.create(); p = self.state(pid); p['contract']['threshold'] = 1
        b.project_manager().save(p)
        with self.assertRaises(b.GateError): b.project_manager().status(pid)

    def test_supervisor_can_reduce_concurrency_without_killing_work(self):
        pid = self.create(); self.start(pid)
        result = b.project_manager().update(pid, 'Shared integration bottleneck; serialize future work', strategy='single')
        self.assertEqual(result['effective_parallel'], 1)
        self.assertEqual(sum(t['phase'] == 'running' for t in result['tasks']), 3)

    def test_add_repair_task_preserves_original_accepted_contract(self):
        pid = self.create([self.task('a')]); self.start(pid); self.submit(pid, 'a')
        original = self.child(pid, 'a')['contract_hash']
        with patch.object(b, '_start_job', return_value={}):
            result = b.project_manager().update(pid, 'Repair integration issue', add_tasks=[self.task('fix', ['a'], paths=['a.py'])])
        self.assertEqual(original, self.child(pid, 'a')['contract_hash'])
        self.assertEqual(len(result['tasks']), 2)

    def test_same_owner_batch_and_wrong_owner_rejected(self):
        db = {'thread': {'id': OWNER, 'status': {'type': 'idle'}, 'cwd': str(self.repo)}, 'queue': [], 'calls': []}
        with patch.object(b, 'CodexRPC', side_effect=lambda *_: FakeRPC(db)), patch.object(b, 'spawn_dispatch'):
            pid = self.create(notify=True, caller=OWNER); self.start(pid)
            # Mark all three completions before a single callback receipt.
            for name in 'abc':
                s = self.child(pid, name); s.update(phase='responded', job_id=name * 32)
                b.record_event(s); b.write_json(b.run_dir(s['session_id']) / 'state.json', s)
            b.project_manager().tick(pid)
            courier = b.read_json(b.run_dir(pid) / 'state.json'); eid = courier['last_event_id']
            b.deliver_once(pid); b.deliver_once(pid)
            self.assertEqual(len(db['queue']), 1)
            with self.assertRaises(b.GateError):
                b.call_tool('telos_project_review', {'project_id': pid}, {'threadId': OTHER})
            received = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertEqual(len(received['events']), 3)
            self.assertFalse(b.project_manager().receive(pid, eid)['proceed'])
            notification = json.dumps(db['queue'])
            self.assertNotIn(self.q[0]['reference'], notification)


class ProviderTests(unittest.TestCase):
    def test_openai_exam_uses_responses_without_tools_or_private_data(self):
        seen = []
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        def request(req, **kw):
            seen.append((req.full_url, json.loads(req.data)))
            return Response(json.dumps({'status': 'completed', 'model': 'test-model', 'output': [
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"answers":[{"id":"q","answer":"five"}]}'}]}]}).encode())
        contract = {'task': 'add', 'context': '', 'model': 'glm-5.3-flash', 'exam_provider': 'openai', 'exam_model': 'test-model',
                    'questions': [{'id': 'q', 'prompt': '2+3?', 'reference': 'PRIVATE ANSWER'}], 'checks': []}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-secret'}), patch.object(b, 'model_request', request):
            result = b.answer_exam(contract)
        self.assertEqual(seen[0][0], 'https://api.openai.com/v1/responses')
        self.assertEqual(result['requested_model'], 'test-model')
        self.assertNotIn('tools', seen[0][1]); self.assertNotIn('previous_response_id', seen[0][1])
        self.assertNotIn('PRIVATE ANSWER', json.dumps(seen))
        self.assertFalse(seen[0][1]['store'])

    def test_openai_missing_key_does_not_fallback_to_ark(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), patch.object(b, 'credential') as ark:
            with self.assertRaises(b.GateError): b.provider_credential('openai')
            ark.assert_not_called()

    def test_provider_validation(self):
        b.validate_backend('openai', 'gpt-example')
        for provider, model in [('openai', 'codex'), ('evil', 'model'), ('ark', 'gpt-example')]:
            with self.assertRaises(b.GateError): b.validate_backend(provider, model)



class RobustnessTests(ProjectTests):
    def test_reassignment_keeps_old_artifacts_and_frozen_requirements(self):
        pid = self.create([self.task('a')]); self.start(pid)
        old = self.child(pid, 'a'); old['phase'] = 'error'
        b.write_json(b.run_dir(old['session_id']) / 'state.json', old)
        original = b._start_job
        def launch(*args, **kwargs):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kwargs)
        with patch.object(b, '_start_job', launch):
            b.project_manager().reassign(pid, 'a', 'Retry a failed executor')
        new = self.child(pid, 'a')
        self.assertNotEqual(new['session_id'], old['session_id'])
        self.assertTrue(Path(old['worktree']).exists())
        self.assertEqual(new['contract_hash'], old['contract_hash'])
        self.assertEqual(self.state(pid)['jobs_started'], 2)
        self.assertEqual(b.read_json(b.run_dir(old['session_id']) / 'state.json')['continuation'], 'cancelled')

    def test_reassignment_shares_cumulative_task_usage_and_budget(self):
        pid = self.create([self.task('a', budget={'max_requests': 2})]); self.start(pid)
        old = self.child(pid, 'a')
        _, old_contract = b.load_session(b.run_dir(old['session_id']))
        ledger = b.usage_ledger(old, old_contract)
        ledger.finish(ledger.begin('work'), {'input_tokens': 10, 'output_tokens': 5})
        old['phase'] = 'error'
        b.write_json(b.run_dir(old['session_id']) / 'state.json', old)
        original = b._start_job
        def launch(*args, **kwargs):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kwargs)
        with patch.object(b, '_start_job', launch):
            b.project_manager().reassign(pid, 'a', 'Retry with the remaining budget')
        new = self.child(pid, 'a')
        self.assertEqual(new['usage_id'], old['usage_id'])
        _, new_contract = b.load_session(b.run_dir(new['session_id']))
        self.assertEqual(new_contract['budget'], {'max_requests': 2})
        self.assertEqual(b.usage_snapshot(new, new_contract)['totals']['requests'], 1)
        next_ledger = b.usage_ledger(new, new_contract)
        next_ledger.finish(next_ledger.begin('exam'), {'input_tokens': 1, 'output_tokens': 1})
        self.assertEqual(next_ledger.snapshot()['totals']['requests'], 2)
        with self.assertRaises(BudgetExceeded):
            next_ledger.begin('work')

    def test_project_usage_marks_unknown_detail_and_unreadable_ledgers(self):
        pid = self.create(); self.start(pid)
        children = {name: self.child(pid, name) for name in 'abc'}
        for name, raw in {
                'a': {'input_tokens': 10, 'output_tokens': 5},
                'b': {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6,
                      'input_tokens_details': {'cached_tokens': 1},
                      'output_tokens_details': {'reasoning_tokens': 1}}}.items():
            state = children[name]
            contract = b.read_json(b.run_dir(state['session_id']) / 'contract.json')
            ledger = b.usage_ledger(state, contract)
            ledger.finish(ledger.begin('work'), raw)
        b.usage_path(children['c'].get('usage_id') or children['c']['session_id']).write_text('{broken')

        usage = b.project_manager()._usage(self.state(pid))
        self.assertEqual(usage['input_tokens'], 14)
        self.assertEqual(usage['output_tokens'], 7)
        self.assertEqual(usage['requests'], 2)
        self.assertEqual(usage['unknown_fields']['cached_input_tokens'], 1)
        self.assertEqual(usage['unknown_fields']['reasoning_output_tokens'], 1)
        self.assertTrue(usage['incomplete'])
        self.assertEqual(usage['unavailable_tasks'], ['c'])

        for state in children.values():
            b.usage_path(state.get('usage_id') or state['session_id']).write_text('{broken')
        usage = b.project_manager()._usage(self.state(pid))
        self.assertIsNotNone(usage)
        self.assertEqual(usage['requests'], 0)
        self.assertTrue(usage['incomplete'])
        self.assertEqual(sorted(usage['unavailable_tasks']), ['a', 'b', 'c'])
        self.assertTrue(all(type(value) is int for value in usage['unknown_fields'].values()))

    def test_first_full_receive_never_acknowledges_or_recovers_state(self):
        db = {'thread': {'id': OWNER, 'status': {'type': 'idle'}, 'cwd': str(self.repo)}, 'queue': [], 'calls': []}
        with patch.object(b, 'CodexRPC', side_effect=lambda *_: FakeRPC(db)), patch.object(b, 'spawn_dispatch'):
            pid = self.create([self.task('a')], notify=True, caller=OWNER); self.start(pid)
            child = self.child(pid, 'a'); child.update(phase='responded', thread_id=OTHER)
            b.record_event(child); b.write_json(b.run_dir(child['session_id']) / 'state.json', child)
            b.project_manager().tick(pid)
            eid = b.read_json(b.run_dir(pid) / 'state.json')['last_event_id']
            paths = [b.project_manager().path(pid), b.run_dir(pid) / 'state.json',
                     b.run_dir(child['session_id']) / 'state.json']
            before = [path.read_bytes() for path in paths]
            for requested_id in (eid, 'missing', None):
                with patch.object(Projects, 'refresh', side_effect=AssertionError('read-only lookup must not recover')):
                    lookup = b.call_tool('telos_project_receive',
                        {'project_id': pid, 'event_id': requested_id, 'detail': 'full'}, {'threadId': OWNER})
                self.assertFalse(lookup['proceed'])
                self.assertTrue(lookup['read_only'])
                self.assertFalse(lookup['receipt_available'])
                self.assertEqual([path.read_bytes() for path in paths], before)
            received = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertTrue(received['proceed'])
            self.assertTrue(received['events'][0]['proceed'])
            self.assertEqual(received['events'][0]['event_id'], child['last_event_id'])

    def test_project_receive_full_detail_replays_original_batch_read_only(self):
        db = {'thread': {'id': OWNER, 'status': {'type': 'idle'}, 'cwd': str(self.repo)}, 'queue': [], 'calls': []}
        with patch.object(b, 'CodexRPC', side_effect=lambda *_: FakeRPC(db)), patch.object(b, 'spawn_dispatch'):
            pid = self.create(notify=True, caller=OWNER); self.start(pid)
            for name in 'abc':
                state = self.child(pid, name)
                state.update(phase='responded', job_id=name * 32)
                b.record_event(state)
                b.write_json(b.run_dir(state['session_id']) / 'state.json', state)
            b.project_manager().tick(pid)
            eid = b.read_json(b.run_dir(pid) / 'state.json')['last_event_id']
            summary = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertEqual(len(summary['events']), 3)

            original_turns = self.child(pid, 'a')['turns']
            changed = self.child(pid, 'a')
            changed.update(turns=original_turns + 77, last_error='changed after receipt')
            b.write_json(b.run_dir(changed['session_id']) / 'state.json', changed)

            replay = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid, 'detail': 'full'},
                                 {'threadId': OWNER})
        self.assertFalse(replay['proceed'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(replay['events']), 3)
        self.assertTrue(all(event['proceed'] is False for event in replay['events']))
        self.assertEqual({event['session_id'] for event in replay['events']},
                         {self.child(pid, name)['session_id'] for name in 'abc'})
        first = next(event for event in replay['events'] if event['task_id'] == 'a')
        self.assertEqual(first['result']['turns'], original_turns)
        self.assertNotEqual(first['result'].get('last_error'), 'changed after receipt')
        self.assertEqual(set(b.project_manager().load(pid)['receipts']), {eid})

    def test_project_receive_full_lookup_does_not_block_reminder_recovery(self):
        db = {'thread': {'id': OWNER, 'status': {'type': 'idle'}, 'cwd': str(self.repo)}, 'queue': [], 'calls': []}
        with patch.object(b, 'CodexRPC', side_effect=lambda *_: FakeRPC(db)), patch.object(b, 'spawn_dispatch'):
            pid = self.create([self.task('a')], notify=True, caller=OWNER); self.start(pid)
            child = self.child(pid, 'a'); child.update(phase='responded', thread_id=OTHER)
            b.record_event(child); b.write_json(b.run_dir(child['session_id']) / 'state.json', child)
            b.project_manager().tick(pid)
            eid = b.read_json(b.run_dir(pid) / 'state.json')['last_event_id']
            first = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertTrue(first['proceed'])
            child_id, session_id = first['events'][0]['event_id'], first['events'][0]['session_id']

            courier = b.read_json(b.run_dir(pid) / 'state.json')
            courier['events'][eid]['received_at'] = time.time() - 1000
            b.write_json(b.run_dir(pid) / 'state.json', courier)
            b.recover_events()
            courier_before = (b.run_dir(pid) / 'state.json').read_bytes()
            project_before = b.project_manager().path(pid).read_bytes()
            self.assertEqual(b.read_json(b.run_dir(pid) / 'state.json')['events'][eid]['reminder']['status'], 'pending')

            lookup = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid, 'detail': 'full'},
                                 {'threadId': OWNER})
            self.assertFalse(lookup['proceed']); self.assertTrue(lookup['replayed']); self.assertTrue(lookup['read_only'])
            self.assertFalse(lookup['events'][0]['proceed'])
            self.assertEqual((lookup['events'][0]['event_id'], lookup['events'][0]['session_id']), (child_id, session_id))
            self.assertEqual((b.run_dir(pid) / 'state.json').read_bytes(), courier_before)
            self.assertEqual(b.project_manager().path(pid).read_bytes(), project_before)

            recovered = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertTrue(recovered['proceed'])
            self.assertEqual((recovered['events'][0]['event_id'], recovered['events'][0]['session_id']),
                             (child_id, session_id))
            self.assertEqual(self.state(pid)['jobs_started'], 1)
            self.assertEqual(b.read_json(b.run_dir(pid) / 'state.json')['events'][eid]['reminder']['status'], 'processed')

            repeated = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertFalse(repeated['proceed']); self.assertTrue(repeated['replayed'])
            self.assertEqual(repeated['events'][0]['event_id'], child_id)
            self.assertEqual(self.state(pid)['jobs_started'], 1)

            with self.assertRaises(b.GateError):
                b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid, 'detail': 'full'},
                            {'threadId': OTHER})

            project = self.state(pid); project['continuation'] = 'paused'
            b.project_manager().save(project)
            paused = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertFalse(paused['proceed']); self.assertEqual(paused['events'], [])
            paused_full = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid, 'detail': 'full'},
                                      {'threadId': OWNER})
            self.assertFalse(paused_full['proceed']); self.assertTrue(paused_full['replayed'])
            self.assertEqual(paused_full['events'][0]['event_id'], child_id)

    def test_worker_can_read_own_files_but_not_sibling_cache(self):
        pid = self.create(); self.start(pid)
        a, other = self.child(pid, 'a'), self.child(pid, 'b')
        own = Path(a['worktree']) / 'calc.py'; sibling = Path(other['worktree']) / 'calc.py'
        code = f"from pathlib import Path; assert Path({str(own)!r}).resolve().read_text(); Path({str(sibling)!r}).read_text()"
        profile = b.sandbox_profile(writable=[a['worktree']], readable_cache=[a['worktree']])
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, __import__('sys').executable, '-c', code], cwd=a['worktree'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(sibling), result.stderr)
        self.assertIn('Operation not permitted', result.stderr)

    def test_concurrent_start_calls_do_not_duplicate_dispatch(self):
        pid = self.create()
        original = b._start_job
        def launch(*args, **kw):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kw)
        errors = []
        def run():
            try: b.project_manager().tick(pid)
            except Exception as e: errors.append(str(e))
        with patch.object(b, '_start_job', launch):
            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads: t.start()
            for t in threads: t.join(timeout=10)
        self.assertFalse(errors)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(self.state(pid)['jobs_started'], 3)
        self.assertTrue(all(self.child(pid, tid)['turns'] == 1 for tid in 'abc'))

    def test_failed_verification_process_is_recoverable(self):
        pid = self.create([self.task('a')]); self.start(pid); self.submit(pid, 'a')
        b.project_manager().integrate(pid)
        p = self.state(pid); p.update(phase='verifying', verification_pid=999999999)
        b.project_manager().save(p)
        b.project_manager().recover()
        self.assertEqual(self.state(pid)['phase'], 'integration_failed')

    def test_integration_conflict_resolution_includes_remaining_pins(self):
        # Build real conflicting commits to exercise the defensive merge path.
        pid = self.create([self.task('a'), self.task('b'), self.task('c')]); self.start(pid)
        p = self.state(pid); pins = {}
        for tid, value in [('a', '1'), ('b', '2'), ('c', '3')]:
            s = self.child(pid, tid); tree = Path(s['worktree'])
            (tree / ('calc.py' if tid != 'c' else 'c.py')).write_text('VALUE = ' + value + '\n')
            b.git(tree, 'add', '-A')
            b.git(tree, '-c', 'user.name=Test', '-c', 'user.email=test@local', 'commit', '-qm', tid)
            pins[tid] = b.git(tree, 'rev-parse', 'HEAD')
        tree, conflict = b.project_manager().merge_workspace(p, pins, 'conflict-test')
        self.assertEqual(conflict['files'], ['calc.py'])
        self.assertEqual(conflict['task_id'], 'b')
        self.assertFalse((Path(tree) / 'c.py').exists())
        # No accepted state is forged: this tests the merge helper's stop behavior.
        self.assertEqual(b.git(self.repo, 'rev-parse', 'HEAD'), p['base_commit'])

    def test_resume_publishes_integration_result_completed_while_paused(self):
        db = {'thread': {'id': OWNER, 'status': {'type': 'idle'}, 'cwd': str(self.repo)}, 'queue': [], 'calls': []}
        with patch.object(b, 'CodexRPC', side_effect=lambda *_: FakeRPC(db)), patch.object(b, 'spawn_dispatch'):
            pid = self.create(notify=True, caller=OWNER)
            b.project_manager().control(pid, 'pause')
            p = self.state(pid); p['phase'] = 'awaiting_final_review'
            b.project_manager().save(p)
            b.project_manager().control(pid, 'resume')
            courier = b.read_json(b.run_dir(pid) / 'state.json')
            self.assertIn('last_event_id', courier)
            self.assertEqual(courier['events'][courier['last_event_id']]['status'], 'pending')


class ProjectReadTests(ProjectTests):
    def test_status_reuses_reads_but_observes_changes_on_each_query(self):
        pid = self.create([self.task('a')]); self.start(pid)
        manager = b.project_manager()
        state = self.child(pid, 'a')
        root = b.run_dir(state['session_id'])
        _, contract = b.load_session(root)
        ledger = b.usage_ledger(state, contract)
        for count, detail in enumerate(('full', 'summary'), 1):
            ledger.finish(ledger.begin('work'), {'input_tokens': 7, 'output_tokens': 3}, latency_ms=1250)
            with patch.object(b, 'read_json', wraps=b.read_json) as reads, \
                    patch.object(b, 'usage_snapshot', wraps=b.usage_snapshot) as usage, \
                    patch.object(manager.b, 'usage_snapshot', usage):
                result = manager.status(pid, detail=detail)
            child_reads = Counter(Path(call.args[0]).name for call in reads.call_args_list
                                  if Path(call.args[0]).parent == root)
            self.assertLessEqual(child_reads['state.json'], 3)
            self.assertLessEqual(child_reads['contract.json'], 3)
            self.assertLessEqual(usage.call_count, 2)
            self.assertEqual(result['usage']['requests'], count)
            self.assertEqual(result['usage']['total_tokens'], count * 10)
            self.assertEqual(result['tasks'][0]['usage']['totals']['requests'], count)
        # The same manager must not reuse a previously valid contract on a
        # subsequent query, or retain a quarantine after the record is restored.
        original = (root / 'contract.json').read_bytes()
        b.write_json(root / 'contract.json', {**contract, 'task': 'Changed frozen contract'})
        damaged = manager.status(pid)
        self.assertEqual(damaged['tasks'][0]['phase'], 'quarantined')
        self.assertFalse(damaged['accepted'])
        self.assertTrue(damaged['usage']['incomplete'])
        self.assertIn('a', damaged['usage']['unavailable_tasks'])
        (root / 'contract.json').write_bytes(original)
        restored = manager.status(pid)
        self.assertEqual(restored['tasks'][0]['phase'], 'running')
        self.assertEqual(restored['usage']['total_tokens'], 20)

    def test_view_aggregate_uses_the_displayed_child_usage_snapshot(self):
        pid = self.create([self.task('a')]); self.start(pid)
        manager = b.project_manager(); project = manager.load(pid)
        state = self.child(pid, 'a')
        _, contract = b.load_session(b.run_dir(state['session_id']))
        ledger = b.usage_ledger(state, contract)
        ledger.finish(ledger.begin('work'), {'input_tokens': 7, 'output_tokens': 3}, latency_ms=1250)
        real_snapshot = b.usage_snapshot

        def finish_another_request(*args):
            snapshot = real_snapshot(*args)
            ledger.finish(ledger.begin('work'), {'input_tokens': 2, 'output_tokens': 1}, latency_ms=500)
            return snapshot

        with patch.object(manager.b, 'usage_snapshot', side_effect=finish_another_request):
            result = manager.view(project)
        self.assertEqual(result['tasks'][0]['usage']['totals']['total_tokens'], 10)
        self.assertEqual(result['usage']['total_tokens'], 10)
        self.assertEqual(result['usage']['request_latency']['observed_seconds'], 1.25)
        fresh = manager.view(project)
        self.assertEqual(fresh['tasks'][0]['usage']['totals']['total_tokens'], 13)
        self.assertEqual(fresh['usage']['total_tokens'], 13)
        self.assertEqual(fresh['usage']['request_latency']['observed_seconds'], 1.75)


class _ProjectViewBridge:
    """In-memory bridge for project-view identity tests; no runtime services."""

    GateError = b.GateError

    def __init__(self, root):
        self.STATE = root
        self.couriers = {}

    def run_dir(self, sid):
        return self.STATE / 'runs' / sid

    def read_json(self, path):
        return self.couriers[Path(path).parent.name]

    def load_session(self, root):
        return self.couriers[root.name], {}

    @staticmethod
    def return_health(state):
        continuation = state.get('continuation', 'active')
        if continuation != 'active':
            return {'status': continuation, 'next_action': None}
        if state.get('return_complete') or state.get('phase') == 'accepted':
            return {'status': 'complete', 'next_action': None}
        event = state.get('events', {}).get(state.get('last_event_id'), {})
        return {'status': event.get('status', 'ready'), 'next_action': None}


class ProjectEventViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridge = _ProjectViewBridge(self.root)
        self.projects = Projects(self.bridge)
        # These projection tests supply courier records only. Child-state
        # validation and missing-state isolation have separate integration tests.
        child_lookup = patch.object(self.projects, '_child_session', return_value=None)
        child_lookup.start()
        self.addCleanup(child_lookup.stop)
        self.pid = 'p' * 32

    def project(self, **overrides):
        p = {'project_id': self.pid, 'phase': 'running', 'continuation': 'active',
             'execution_mode': 'single', 'strategy': 'single',
             'decision_reason': 'deterministic project view fixture', 'max_parallel': 1,
             'max_jobs': 40, 'jobs_started': 0, 'base_commit': 'a' * 40,
             'contract_hash': 'c' * 64,
             'tasks': [{'id': 'a', 'task': 'Implement a', 'depends_on': [],
                        'allowed_paths': ['a.py'], 'session_id': 's' * 32,
                        'phase': 'running', 'revision': 1}]}
        p.update(overrides)
        return p

    def store_courier(self, **overrides):
        courier = {'session_id': self.pid, 'project_id': self.pid,
                   'notification_only': True, 'phase': 'project_update',
                   'continuation': 'active', 'events': {}}
        courier.update(overrides)
        self.bridge.couriers[self.pid] = courier
        return courier

    def test_full_view_uses_state_mapping_key_and_exports_only_identity(self):
        eid = 'e' * 32
        courier = self.store_courier(
            last_event_id=eid,
            events={eid: {'id': 'inner-event-id', 'job_id': 'child-action-id',
                          'status': 'queued',
                          'actions': {'telos_receive': {'result': 'private'}},
                          'last_error': 'private transport detail'}})
        p = self.project()
        p['tasks'][0]['event_id'] = 'child-action-id'
        courier_before = copy.deepcopy(courier)
        project_before = copy.deepcopy(p)
        full = self.projects.view(p, 'full')
        self.assertEqual(full['return_event'], {'id': eid, 'status': 'queued'})
        self.assertEqual(set(full['return_event']), {'id', 'status'})
        self.assertEqual(full['tasks'][0]['event_id'], 'child-action-id')
        self.assertEqual(full['return_health'], {'status': 'queued', 'next_action': None})
        self.assertEqual(courier, courier_before)
        self.assertEqual(p, project_before)

    def test_summary_view_propagates_return_event_identity_without_payload(self):
        eid = 'f' * 32
        self.store_courier(
            last_event_id=eid,
            events={eid: {'id': 'different-inner-id', 'status': 'received',
                          'actions': {'private': 'payload'},
                          'progress': {'status': 'waiting'}}})
        summary = self.projects.view(self.project(), 'summary')
        self.assertEqual(summary['return_event'], {'id': eid, 'status': 'received'})
        self.assertEqual(set(summary['return_event']), {'id', 'status'})
        self.assertEqual(summary['detail'], 'summary')

    def test_return_event_is_null_when_latest_event_cannot_be_resolved(self):
        cases = [
            ('missing last_event_id', {'events': {'a' * 32: {'status': 'queued'}}}),
            ('missing mapping key', {'last_event_id': 'b' * 32,
                                     'events': {'a' * 32: {'status': 'queued'}}}),
            ('inner id is not the mapping key', {'last_event_id': 'b' * 32,
                                                 'events': {'a' * 32: {'id': 'b' * 32,
                                                                       'status': 'queued'}}}),
        ]
        for label, state in cases:
            with self.subTest(label):
                self.store_courier(**state)
                p = self.project()
                self.assertIsNone(self.projects.view(p, 'full')['return_event'])
                self.assertIsNone(self.projects.view(p, 'summary')['return_event'])

    def test_historical_paused_cancelled_and_accepted_identities_stay_visible(self):
        cases = [
            ('processed after receipt', {'phase': 'running', 'return_complete': True},
             'processed', {'status': 'complete', 'next_action': None}, 'running'),
            ('received', {'phase': 'running'}, 'received',
             {'status': 'received', 'next_action': None}, 'running'),
            ('paused courier', {'phase': 'project_update', 'continuation': 'paused'},
             'queued', {'status': 'paused', 'next_action': None}, 'running'),
            ('cancelled courier', {'phase': 'project_update', 'continuation': 'cancelled'},
             'cancelled', {'status': 'cancelled', 'next_action': None}, 'running'),
            ('accepted project', {'phase': 'project_update'}, 'processed',
             {'status': 'complete', 'next_action': None}, 'accepted'),
        ]
        for index, (label, state, event_status, health, project_phase) in enumerate(cases):
            with self.subTest(label):
                eid = f'{index:032x}'
                courier = {'session_id': self.pid, 'project_id': self.pid,
                           'notification_only': True, 'phase': 'project_update',
                           'continuation': 'active', 'events': {}}
                courier.update(state)
                courier['last_event_id'] = eid
                courier['events'] = {eid: {'id': 'inner-' + eid, 'status': event_status,
                                           'actions': {'private': 'payload'}}}
                self.bridge.couriers[self.pid] = courier
                p = self.project(phase=project_phase)
                full = self.projects.view(p, 'full')
                summary = self.projects.view(p, 'summary')
                self.assertEqual(full['return_event'], {'id': eid, 'status': event_status})
                self.assertEqual(summary['return_event'], {'id': eid, 'status': event_status})
                self.assertEqual(full['return_health'], health)


class _ProjectNoticeBridge:
    """In-memory bridge for publish decisions; no runtime services or Git."""

    WATCH_EVENTS = WATCH_EVENTS

    def __init__(self, root):
        self.STATE = root
        self.couriers = {}
        self.writes = 0
        self.dispatches = []

    def run_dir(self, sid):
        return self.STATE / 'runs' / sid

    @staticmethod
    def digest(value):
        return digest(value)

    @contextlib.contextmanager
    def locked(self, sid):
        yield self.run_dir(sid)

    def load_session(self, root):
        return copy.deepcopy(self.couriers[root.name]), {}

    def write_json(self, path, value):
        self.writes += 1
        self.couriers[Path(path).parent.name] = copy.deepcopy(value)

    def record_event(self, state):
        eid = state['job_id']
        state.setdefault('events', {})[eid] = {
            'id': eid, 'job_id': eid, 'phase': state.get('phase'),
            'status': 'pending', 'attempts': 0, 'actions': {},
            'reminder': {'status': 'pending', 'attempts': 0}}
        state['last_event_id'] = eid

    def spawn_dispatch(self, sid):
        self.dispatches.append(sid)


class ProjectNoticeTests(unittest.TestCase):
    """Pure publish tests for supervisor wake-up suppression."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = _ProjectNoticeBridge(Path(self.tmp.name))
        self.projects = Projects(self.bridge)
        self.pid = 'a' * 32
        self.owner = {'thread_id': 'b' * 32}

    def task(self, ident, phase='running', **overrides):
        task = {'id': ident, 'phase': phase, 'event_id': None, 'error': None}
        task.update(overrides)
        return task

    def project(self, phases=('running',), **overrides):
        p = {'project_id': self.pid, 'phase': 'running', 'continuation': 'active',
             'owner': copy.deepcopy(self.owner),
             'tasks': [self.task(chr(97 + index), phase)
                       for index, phase in enumerate(phases)]}
        p.update(overrides)
        return p

    def courier(self, event_status=None):
        courier = {'session_id': self.pid, 'project_id': self.pid,
                   'notification_only': True, 'phase': 'project_update',
                   'continuation': 'active', 'events': {}}
        if event_status is not None:
            eid = 'c' * 32
            courier['last_event_id'] = eid
            courier['events'][eid] = {
                'id': eid, 'job_id': eid, 'phase': 'project_update',
                'created_at': 123.5, 'status': event_status, 'attempts': 2,
                'retry_at': 456.0, 'actions': {'telos_receive': {'result': 'private'}},
                'reminder': {'status': 'pending', 'attempts': 1, 'next_at': 789.0}}
        self.bridge.couriers[self.pid] = courier
        self.bridge.writes = 0
        self.bridge.dispatches.clear()
        return courier

    def test_fresh_running_only_transition_creates_no_courier(self):
        for status in (None, 'processed'):
            with self.subTest(latest_event=status):
                before = copy.deepcopy(self.courier(status))
                p = self.project(('pending', 'starting', 'running', 'accepted'))
                self.projects.publish(p)
                self.assertEqual(self.bridge.writes, 0)
                self.assertEqual(self.bridge.dispatches, [])
                self.assertEqual(self.bridge.couriers[self.pid], before)
                self.assertNotIn('notice_signature', p)
                p['notice_signature'] = 'previous'
                self.projects.publish(p)
                self.assertEqual(p['notice_signature'], 'previous')
                self.assertEqual(self.bridge.writes, 0)
                self.assertEqual(self.bridge.dispatches, [])

    def test_running_only_suppression_accepts_allowed_phases_and_empty_errors(self):
        cases = [
            ('running only', ('running',), [None]),
            ('allowed phase set', ('pending', 'starting', 'running', 'accepted'), [None] * 4),
            ('empty error text', ('running',), ['']),
        ]
        for label, phases, errors in cases:
            with self.subTest(label=label):
                self.courier()
                p = self.project(phases)
                for task, error in zip(p['tasks'], errors):
                    task['error'] = error
                self.projects.publish(p)
                self.assertEqual(self.bridge.writes, 0)
                self.assertEqual(self.bridge.dispatches, [])
                self.assertNotIn('notice_signature', p)

    def test_conservative_eligibility_boundary_creates_couriers(self):
        cases = [
            ('other project phase', {'phase': 'awaiting_review'}, ('running',), [None]),
            ('paused continuation', {'continuation': 'paused'}, ('running',), [None]),
            ('cancelled continuation', {'continuation': 'cancelled'}, ('running',), [None]),
            ('no running child', {}, ('pending', 'starting', 'accepted'), [None] * 3),
            ('ready child', {}, ('running', 'ready'), [None, None]),
            ('responded child', {}, ('running', 'responded'), [None, None]),
            ('awaiting review child', {}, ('running', 'awaiting_review'), [None, None]),
            ('not accepted child', {}, ('running', 'not_accepted'), [None, None]),
            ('setup error child', {}, ('running', 'setup_error'), [None, None]),
            ('stale child', {}, ('running', 'stale'), [None, None]),
            ('unknown child', {}, ('running', 'unknown'), [None, None]),
            ('child error', {}, ('running',), ['failed']),
            ('pending child error', {}, ('running', 'pending'), [None, 'setup failed']),
        ]
        for label, overrides, phases, errors in cases:
            with self.subTest(label=label):
                self.courier()
                p = self.project(phases, **overrides)
                for task, error in zip(p['tasks'], errors):
                    task['error'] = error
                self.projects.publish(p)
                self.assertEqual(self.bridge.writes, 1)
                self.assertEqual(self.bridge.dispatches, [self.pid])
                courier = self.bridge.couriers[self.pid]
                self.assertIn('last_event_id', courier)
                self.assertEqual(len(courier['events']), 1)
                self.assertIn('notice_signature', p)

    def test_outstanding_courier_keeps_identity_reminders_and_dispatch(self):
        for status in ('pending', 'sending', 'uncertain', 'queued', 'delivered', 'received'):
            with self.subTest(status=status):
                before = copy.deepcopy(self.courier(status))
                p = self.project(('running', 'pending'))
                self.projects.publish(p)
                self.assertEqual(self.bridge.writes, 1)
                self.assertEqual(self.bridge.dispatches, [self.pid])
                self.assertEqual(self.bridge.couriers[self.pid], before)
                self.assertIn('notice_signature', p)

    def test_signature_deduplication_and_suppressed_retries(self):
        self.courier()
        p = self.project(('running',))
        self.projects.publish(p)
        self.projects.publish(p)
        self.assertEqual(self.bridge.writes, 0)
        self.assertEqual(self.bridge.dispatches, [])
        self.assertNotIn('notice_signature', p)

        p['tasks'][0]['phase'] = 'responded'
        self.projects.publish(p)
        signature = p['notice_signature']
        courier = copy.deepcopy(self.bridge.couriers[self.pid])
        self.projects.publish(p)
        self.assertEqual(self.bridge.writes, 1)
        self.assertEqual(self.bridge.dispatches, [self.pid])
        self.assertEqual(p['notice_signature'], signature)
        self.assertEqual(self.bridge.couriers[self.pid], courier)

    def test_completion_and_integration_transitions_notify(self):
        cases = [
            ('child completion', {}, ('running', 'responded')),
            ('child review', {}, ('running', 'awaiting_review')),
            ('all accepted integration readiness', {}, ('accepted', 'accepted')),
            ('integrated', {'phase': 'integrated', 'integration': {'packet_id': 'd' * 64}},
             ('accepted', 'accepted')),
            ('integration failed', {'phase': 'integration_failed', 'integration': {'packet_id': 'e' * 64}},
             ('accepted', 'accepted')),
            ('awaiting final review', {'phase': 'awaiting_final_review',
                                       'integration': {'packet_id': 'f' * 64}},
             ('accepted', 'accepted')),
            ('acceptance invalidation', {'phase': 'changed_after_acceptance'}, ('accepted',)),
        ]
        for label, overrides, phases in cases:
            with self.subTest(label=label):
                self.courier()
                p = self.project(phases, **overrides)
                self.projects.publish(p)
                self.assertEqual(self.bridge.writes, 1)
                self.assertEqual(self.bridge.dispatches, [self.pid])
                self.assertIn('notice_signature', p)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (ProjectTests, ProviderTests, RobustnessTests, ProjectEventViewTests, ProjectNoticeTests):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite

if __name__ == '__main__':
    unittest.main()

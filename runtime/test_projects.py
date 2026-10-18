"""Behavior tests: real Git worktrees/snapshots/checks, deterministic model boundary."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch, Mock
from test_telos import b, BridgeTests, synthetic_exam
from test_callbacks import FakeRPC, OWNER, OTHER


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


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (ProjectTests, ProviderTests, RobustnessTests):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite

if __name__ == '__main__':
    unittest.main()

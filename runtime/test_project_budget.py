"""Behavior tests: audited, append-only unlimited job-budget policy."""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import test_telos
import project_budget
import progress_guard
import usage

b = test_telos.b
OWNER = '00000000-0000-4000-8000-000000000001'


class JobBudgetTests(unittest.TestCase):
    setUp = test_telos.BridgeTests.setUp
    tearDown = test_telos.BridgeTests.tearDown

    def create(self, max_jobs=1):
        task = {'id': 'a', 'task': 'Implement a', 'message': 'Implement the assigned change.',
                'depends_on': [], 'allowed_paths': ['a.py'], 'questions': copy.deepcopy(self.q),
                'checks': copy.deepcopy(self.checks), 'assessment_mode': 'closed_book'}
        pid = b.project_manager().create(repo=str(self.repo), task='Complete project', tasks=[task],
            questions=self.q, checks=self.checks, decision_reason='Single independent file',
            strategy='single', max_jobs=max_jobs, notify=False)['project_id']
        p = b.project_manager().load(pid)
        p['owner'] = {'thread_id': OWNER}
        b.project_manager().save(p)
        return pid

    def request(self, pid, request_id='req-1', reason='User authorized removing the job ceiling',
                authorization='Explicit user authorization recorded 2026-10-29'):
        return b.project_manager().budget(pid, request_id=request_id, reason=reason,
                                          user_authorization=authorization, caller=OWNER)

    def api(self, pid, **extra):
        args = {'project_id': pid, 'request_id': 'req-1',
                'reason': 'User authorized removing the job ceiling',
                'user_authorization': 'Explicit user authorization recorded 2026-10-29'}
        args.update(extra)
        return b.call_tool('telos_project_budget', args, {'threadId': OWNER})

    def test_legacy_cap_then_unlimited_preserves_everything(self):
        pid = self.create()
        manager = b.project_manager()
        p = manager.load(pid)
        p['jobs_started'] = p['max_jobs']
        tasks, contract_hash, max_jobs = copy.deepcopy(p['tasks']), p['contract_hash'], p['max_jobs']
        manager.save(p)
        with self.assertRaises(b.GateError):
            manager.capacity(p, 'ark')
        result = self.request(pid)
        self.assertFalse(result['replayed'])
        self.assertIsNone(result['job_budget']['effective_max_jobs'])
        self.assertEqual(result['job_budget']['initial_max_jobs'], max_jobs)
        self.assertEqual(result['job_budget']['jobs_started'], max_jobs)
        current = manager.load(pid)
        self.assertEqual((current['max_jobs'], current['jobs_started'], current['contract_hash']),
                         (max_jobs, max_jobs, contract_hash))
        self.assertEqual(current['tasks'], tasks)
        self.assertTrue(manager.capacity(current, 'ark'))
        self.assertEqual(current['jobs_started'], max_jobs)

    def test_replay_is_byte_identical_and_different_arguments_rejected(self):
        pid = self.create()
        first = self.request(pid)
        path = b.project_manager().path(pid)
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        time.sleep(0.02)
        second = self.request(pid)
        self.assertFalse(first['replayed'])
        self.assertTrue(second['replayed'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        with self.assertRaises(b.GateError):
            self.request(pid, reason='A different reason')
        with self.assertRaises(b.GateError):
            self.request(pid, request_id='req-2')
        with self.assertRaises(b.GateError):
            self.request(pid, request_id=' req-3 ')

    def test_damaged_policy_identity_and_digest_fail_closed(self):
        pid = self.create()
        self.request(pid)
        base = b.project_manager().load(pid)

        def mutate(fn):
            q = copy.deepcopy(base)
            fn(q)
            with self.assertRaises(ValueError):
                project_budget.validate_project(q)

        mutate(lambda q: q.update(project_id='another-project'))
        mutate(lambda q: q['job_budget_policy'].update(project_id='another-project'))
        mutate(lambda q: q['job_budget_policy'].update(owner_thread_id='other'))
        mutate(lambda q: q['job_budget_policy'].update(contract_hash='0' * 64))
        mutate(lambda q: q['job_budget_policy'].update(initial_max_jobs=99))
        mutate(lambda q: q['job_budget_policy'].update(effective_max_jobs=40))
        mutate(lambda q: q['job_budget_policy'].update(jobs_started=999))
        mutate(lambda q: q['job_budget_policy']['history'][0].update(user_authorization='tampered'))
        mutate(lambda q: q['job_budget_policy']['history'][0].update(arguments='0' * 64))
        mutate(lambda q: q['job_budget_policy']['history'][0].update(owner_thread_id='other'))
        mutate(lambda q: q['job_budget_policy']['history'][0].update(contract_hash='0' * 64))
        mutate(lambda q: q['job_budget_policy']['history'][0].pop('reason'))
        mutate(lambda q: q['job_budget_policy']['history'][0].update(extra=True))
        mutate(lambda q: q.update(job_budget_policy=None))
        corrupt = copy.deepcopy(base)
        corrupt['job_budget_policy']['history'][0]['arguments'] = '0' * 64
        b.write_json(b.project_manager().path(pid), corrupt)
        with self.assertRaises(b.GateError):
            b.project_manager().load(pid)

    def test_formal_api_uses_trusted_owner_metadata_only(self):
        pid = self.create()
        result = self.api(pid)
        self.assertFalse(result['replayed'])
        self.assertTrue(self.api(pid)['replayed'])
        with self.assertRaises(b.GateError):
            self.api(pid, caller='forged')
        with self.assertRaises(b.GateError):
            b.call_tool('telos_project_budget', {'project_id': pid, 'request_id': 'r2',
                'reason': 'x', 'user_authorization': 'y'}, None)
        with self.assertRaises(b.GateError):
            b.call_tool('telos_project_budget', {'project_id': pid, 'request_id': 'r3',
                'reason': 'x', 'user_authorization': 'y'}, {'threadId': 'other'})

    def test_concurrent_duplicate_calls_create_one_record(self):
        pid = self.create()
        args = {'project_id': pid, 'request_id': 'concurrent', 'reason': 'User authorized',
                'user_authorization': 'Explicit user authorization'}
        def invoke(_):
            return b.call_tool('telos_project_budget', dict(args), {'threadId': OWNER})
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(invoke, range(6)))
        self.assertEqual(sum(not r['replayed'] for r in results), 1)
        history = b.project_manager().load(pid)['job_budget_policy']['history']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['request_id'], 'concurrent')

    def test_40_to_41_real_launch_and_current_summary(self):
        pid = self.create(max_jobs=40)
        manager = b.project_manager()
        p = manager.load(pid)
        p['jobs_started'] = 40
        manager.save(p)
        self.request(pid)
        original = b._start_job
        def start(*args, **kw):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kw)
        owner = {'thread_id': OWNER, 'cwd': str(self.repo), 'socket': '/fixture',
                 'transport': 'cli_queue'}
        with patch.object(b, '_start_job', start), patch.object(b, 'spawn_dispatch'), \
                patch.object(b, 'bind_owner', return_value=owner):
            manager.tick(pid)
        current = manager.load(pid)
        self.assertEqual(current['jobs_started'], 41,
                         json.dumps(current['tasks'][0], default=str, sort_keys=True))
        task = current['tasks'][0]
        # A running job legitimately has no completion event yet; admission is
        # proven by the persisted child state, unchanged job identity and count.
        self.assertNotIn('start_error', task)
        child = b.read_json(b.STATE / 'runs' / task['session_id'] / 'state.json')
        self.assertIsInstance(child, dict)
        self.assertTrue(child)
        self.assertEqual(child.get('session_id', child.get('id')), task['session_id'])
        self.assertIn(child.get('job_kind', child.get('kind')), ('send', 'work'))
        self.assertEqual(child.get('project_id', pid), pid)
        self.assertIn(child.get('phase'), ('running', 'error', 'accepted', 'checkpoint'))
        self.assertGreater(current['jobs_started'], current['max_jobs'])
        self.assertGreaterEqual(current['jobs_started'], 41)
        summary = project_budget.summary(current)
        self.assertEqual(summary['jobs_started'], current['jobs_started'])
        self.assertEqual(summary['authorization_history'][0]['jobs_started'], 40)
        self.assertEqual(summary['initial_max_jobs'], 40)
        self.assertIsNone(summary['effective_max_jobs'])

    def test_unlimited_keeps_concurrency_and_progress_gates(self):
        pid = self.create(max_jobs=1)
        manager = b.project_manager()
        p = manager.load(pid)
        p['jobs_started'] = 1
        manager.save(p)
        self.request(pid)
        current = manager.load(pid)
        self.assertTrue(manager.capacity(current, 'ark'))
        # Project-level concurrency still gates admission.
        self.assertEqual(manager.limit(current), 1)
        with patch.object(type(manager), 'limit', return_value=0):
            self.assertFalse(manager.capacity(current, 'ark'))
        # Provider-level concurrency still gates admission.
        with patch.object(manager.b, 'config', return_value={'provider_max_parallel': 0}):
            self.assertFalse(manager.capacity(current, 'ark'))
        # A no-progress block is still enforced: nothing is launched or accepted.
        owner = {'thread_id': OWNER, 'cwd': str(self.repo), 'socket': '/fixture',
                 'transport': 'cli_queue'}
        with patch.object(progress_guard, 'check',
                          side_effect=b.GateError('No substantive progress', code='no_progress')), \
                patch.object(b, 'spawn_dispatch'), patch.object(b, 'bind_owner', return_value=owner):
            try:
                manager.tick(pid)
            except b.GateError:
                pass
        after = manager.load(pid)
        self.assertEqual(after['jobs_started'], 1)
        self.assertNotEqual(after['phase'], 'accepted')
        self.assertEqual([t.get('budget') for t in after['tasks']],
                         [t.get('budget') for t in current['tasks']])

    def test_terminal_and_discussion_projects_are_rejected(self):
        for field, value in (('phase', 'accepted'), ('continuation', 'cancelled'),
                             ('phase', 'cancelled')):
            pid = self.create()
            manager = b.project_manager()
            p = manager.load(pid)
            p[field] = value
            manager.save(p)
            with self.assertRaises(b.GateError):
                self.request(pid)
            self.assertNotIn('job_budget_policy', manager.load(pid))

    def test_discussion_project_has_no_budget_policy(self):
        pid = self.create()
        manager = b.project_manager()
        p = manager.load(pid)
        p['kind'] = 'discussion'
        manager.save(p)
        with self.assertRaises(b.GateError):
            self.request(pid)
        self.assertNotIn('job_budget_policy', manager.load(pid))

    def test_paused_project_and_child_budgets_are_unchanged(self):
        pid = self.create()
        manager = b.project_manager()
        manager.control(pid, 'pause')
        before = manager.load(pid)
        result = self.request(pid)
        self.assertEqual(result['continuation'], 'paused')
        after = manager.load(pid)
        self.assertEqual(after['continuation'], 'paused')
        self.assertEqual([t['phase'] for t in after['tasks']], [t['phase'] for t in before['tasks']])
        self.assertEqual([t.get('budget') for t in after['tasks']], [t.get('budget') for t in before['tasks']])
        self.assertEqual((after['execution_mode'], after['max_parallel'], after['max_jobs']),
                         (before['execution_mode'], before['max_parallel'], before['max_jobs']))
        self.assertEqual(manager.limit(after), manager.limit(before))
        self.assertNotEqual(after['phase'], 'accepted')

    def test_default_projects_stay_capped_and_local_checks_exempt(self):
        pid = self.create()
        manager = b.project_manager()
        status = manager.status(pid, 'summary')
        self.assertEqual(status['job_budget']['effective_max_jobs'], 1)
        self.assertEqual(status['job_budget']['authorization_history'], [])
        p = manager.load(pid); p['jobs_started'] = 1; manager.save(p)
        with self.assertRaises(b.GateError):
            manager.capacity(manager.load(pid), 'ark')
        self.assertTrue(manager.capacity(manager.load(pid), 'ark', local_checks=True))

    def test_child_request_budget_exhaustion_blocks_unlimited_launch(self):
        task = {'id': 'a', 'task': 'Implement a', 'message': 'Implement the assigned change.',
                'depends_on': [], 'allowed_paths': ['a.py'], 'questions': copy.deepcopy(self.q),
                'checks': copy.deepcopy(self.checks), 'assessment_mode': 'closed_book',
                'budget': {'max_requests': 1}}
        pid = b.project_manager().create(repo=str(self.repo), task='Complete project', tasks=[task],
            questions=self.q, checks=self.checks, decision_reason='Single independent file',
            strategy='single', max_jobs=1, notify=False)['project_id']
        manager = b.project_manager()
        p = manager.load(pid)
        p['owner'] = {'thread_id': OWNER}
        p['jobs_started'] = 1
        manager.save(p)
        self.request(pid)
        current = manager.load(pid)
        self.assertIsNone(project_budget.effective_max_jobs(current))
        self.assertTrue(manager.capacity(current, 'ark'))
        self.assertEqual(current['tasks'][0]['budget'], {'max_requests': 1})
        task_state = current['tasks'][0]
        usage_id = task_state.get('usage_id') or task_state['session_id']
        # Real isolated child ledger with the frozen limited request budget.
        ledger = usage.UsageLedger(b.usage_path(usage_id), {'max_requests': 1})
        first = ledger.begin('work')
        ledger.finish(first, {'input_tokens': 5, 'output_tokens': 3})
        self.assertEqual(ledger.snapshot()['budget_state']['state'], 'exceeded')
        owner = {'thread_id': OWNER, 'cwd': str(self.repo), 'socket': '/fixture',
                 'transport': 'cli_queue'}
        with patch.object(b, 'spawn_dispatch'), patch.object(b, 'bind_owner', return_value=owner):
            try:
                manager.tick(pid)
            except (b.GateError, usage.BudgetExceeded):
                pass
        # The exhausted child request budget leaves no running model job behind.
        self.assertEqual(usage.UsageLedger(b.usage_path(usage_id)).snapshot()['budget_state']['state'],
                         'exceeded')
        after = manager.load(pid)
        self.assertEqual(after['jobs_started'], 1)
        self.assertNotEqual(after['phase'], 'accepted')
        self.assertNotIn('running', {t['phase'] for t in after['tasks']})
        policy = project_budget.validate_project(after)
        self.assertIsNotNone(policy)
        self.assertEqual(policy['strategy'], 'unlimited')
        self.assertIsNone(policy['effective_max_jobs'])
        self.assertEqual([r['request_id'] for r in policy['history']], ['req-1'])
        self.assertEqual(policy['history'][0]['jobs_started'], 1)
        self.assertNotEqual([r['request_id'] for r in policy['history']], [])


if __name__ == '__main__':
    unittest.main()

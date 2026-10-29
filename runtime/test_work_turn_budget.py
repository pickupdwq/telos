"""Behavior tests: sibling unlimited work-turn policy on an unlimited job budget."""
import copy
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import test_telos
import project_budget
import continuation
import progress_guard
import usage
import telos

b = test_telos.b
OWNER = '00000000-0000-4000-8000-000000000001'


def project_fixture():
    return {'project_id': 'p1', 'contract_hash': 'a' * 64, 'max_jobs': 40,
            'jobs_started': 0,
            'owner': {'thread_id': OWNER}, 'continuation': 'active',
            'phase': 'running', 'kind': 'implementation',
            'tasks': [{'id': 'a', 'session_id': 's1', 'phase': 'running'},
                      {'id': 'b', 'session_id': 's2', 'phase': 'running'}]}


def with_v1(p=None):
    p = p or project_fixture()
    updated, _, _ = project_budget.authorize(
        p, owner_thread_id=OWNER, strategy='unlimited', request_id='v1-req',
        reason='User authorized unlimited jobs', user_authorization='User instruction 2026-10-29',
        now=time.time())
    return updated


def with_v1_work(p=None):
    p = with_v1(p)
    updated, replayed, summary = project_budget.authorize_work(
        p, owner_thread_id=OWNER, request_id='w-req',
        reason='User authorized unlimited work turns',
        user_authorization='Same user instruction 2026-10-29', now=time.time(),
        original_max_turns=20, turns_snapshot={'s1': 20, 's2': None})
    return updated


class WorkTurnPolicyModuleTests(unittest.TestCase):
    def test_sibling_policy_preserves_v1_object_and_legacy_shape(self):
        legacy = project_fixture()
        self.assertIsNone(project_budget.validate_work_project(legacy))
        self.assertIsNone(project_budget.work_turn_decision(
            legacy, session_id='s1', child_contract_hash='b' * 64,
            child_owner_thread_id=OWNER))
        v1 = with_v1()
        before = json.dumps(v1['job_budget_policy'], sort_keys=True)
        updated = with_v1_work(v1)
        self.assertEqual(json.dumps(updated['job_budget_policy'], sort_keys=True), before)
        self.assertEqual(updated['job_budget_policy'], v1['job_budget_policy'])
        self.assertEqual(updated['max_jobs'], 40)
        w = project_budget.validate_work_project(updated)
        self.assertEqual(w['original_max_turns'], 20)
        self.assertIsNone(w['effective_max_turns'])
        self.assertEqual(w['prior_policy_digest'],
                         project_budget.policy_digest(v1['job_budget_policy']))
        summary = project_budget.work_turn_summary(updated)
        self.assertEqual(summary['initial_max_turns'], 20)
        self.assertIsNone(summary['effective_max_turns'])

    def test_decision_requires_bound_current_child_and_can_repeat(self):
        p = with_v1_work()
        decision = project_budget.work_turn_decision(
            p, session_id='s1', child_contract_hash='b' * 64,
            child_owner_thread_id=OWNER)
        self.assertTrue(decision['exempt'])
        self.assertEqual(decision['scope'], 'work_turns')
        self.assertEqual(decision['project_id'], 'p1')
        self.assertEqual(decision['session_id'], 's1')
        self.assertEqual(decision['owner_thread_id'], OWNER)
        self.assertIsNotNone(project_budget.work_turn_decision(
            p, session_id='s2', child_contract_hash='b' * 64,
            child_owner_thread_id=OWNER))
        self.assertIsNone(project_budget.work_turn_decision(
            p, session_id='foreign', child_contract_hash='b' * 64,
            child_owner_thread_id=OWNER))
        self.assertIsNone(project_budget.work_turn_decision(
            p, session_id='s1', child_contract_hash='b' * 64,
            child_owner_thread_id='other'))
        paused = copy.deepcopy(p); paused['continuation'] = 'paused'
        self.assertIsNone(project_budget.work_turn_decision(
            paused, session_id='s1', child_contract_hash='b' * 64,
            child_owner_thread_id=OWNER))
        cancelled = copy.deepcopy(p); cancelled['phase'] = 'cancelled'
        self.assertIsNone(project_budget.work_turn_decision(
            cancelled, session_id='s1', child_contract_hash='b' * 64,
            child_owner_thread_id=OWNER))
        accepted_task = copy.deepcopy(p)
        accepted_task['tasks'][0]['phase'] = 'accepted'
        self.assertIsNone(project_budget.work_turn_decision(
            accepted_task, session_id='s1', child_contract_hash='b' * 64,
            child_owner_thread_id=OWNER))
        # Decision does not rewrite counts or identities.
        self.assertEqual(p['jobs_started'], 0)
        self.assertEqual(p['tasks'], project_fixture()['tasks'])

    def test_requires_prior_v1_and_rejects_missing_or_damaged(self):
        with self.assertRaises(ValueError):
            project_budget.authorize_work(
                project_fixture(), owner_thread_id=OWNER, request_id='w',
                reason='r', user_authorization='u', now=time.time(),
                original_max_turns=20, turns_snapshot={'s1': None})
        broken = with_v1_work()
        broken['job_budget_policy'] = None
        with self.assertRaises(ValueError):
            project_budget.validate_work_project(broken)
        absent_v1 = copy.deepcopy(with_v1_work())
        absent_v1.pop('job_budget_policy')
        with self.assertRaises(ValueError):
            project_budget.validate_work_project(absent_v1)
        null_sibling = with_v1(); null_sibling['work_turn_budget_policy'] = None
        with self.assertRaises(ValueError):
            project_budget.validate_work_project(null_sibling)
        # Missing v1 cannot be hidden by binding a canonical-null digest.
        no_v1 = copy.deepcopy(with_v1_work())
        no_v1.pop('job_budget_policy')
        w = no_v1['work_turn_budget_policy']
        w['prior_policy_digest'] = project_budget.policy_digest(None)
        w['history'][0]['prior_policy_digest'] = w['prior_policy_digest']
        rec = w['history'][0]
        rec['arguments'] = project_budget.work_arguments_digest(
            w['project_id'], rec['request_id'], rec['reason'],
            rec['user_authorization'], w['prior_policy_digest'])
        with self.assertRaises(ValueError):
            project_budget.validate_work_project(no_v1)

    def test_load_rejects_overstated_jobs_and_cross_scope_collision(self):
        p = with_v1_work()
        p['jobs_started'] = 62
        p['work_turn_budget_policy']['jobs_started'] = 62
        rec = p['work_turn_budget_policy']['history'][0]
        rec['jobs_started'] = 62
        project_budget.validate_work_project(p)
        high = copy.deepcopy(p)
        high['work_turn_budget_policy']['history'][0]['jobs_started'] = 999
        high['work_turn_budget_policy']['jobs_started'] = 999
        with self.assertRaises(ValueError):
            project_budget.validate_work_project(high)
        collide = copy.deepcopy(p)
        w = collide['work_turn_budget_policy']
        rec = w['history'][0]
        rec['request_id'] = 'v1-req'
        rec['arguments'] = project_budget.work_arguments_digest(
            w['project_id'], 'v1-req', rec['reason'], rec['user_authorization'],
            w['prior_policy_digest'])
        with self.assertRaises(ValueError):
            project_budget.validate_work_project(collide)

    def test_damaged_records_and_bindings_fail_closed(self):
        base = with_v1_work()
        for mutate in (
            lambda q: q['work_turn_budget_policy'].update(project_id='p2'),
            lambda q: q['work_turn_budget_policy'].update(owner_thread_id='other'),
            lambda q: q['work_turn_budget_policy'].update(contract_hash='0' * 64),
            lambda q: q['work_turn_budget_policy'].update(prior_policy_digest='0' * 64),
            lambda q: q['work_turn_budget_policy'].update(original_max_turns=21),
            lambda q: q['work_turn_budget_policy'].update(effective_max_turns=21),
            lambda q: q['work_turn_budget_policy']['history'][0].update(arguments='0' * 64),
            lambda q: q['work_turn_budget_policy']['history'][0].update(scope='jobs'),
            lambda q: q['work_turn_budget_policy']['history'][0].update(prior_max_turns=19),
            lambda q: q['work_turn_budget_policy']['history'][0].update(owner_thread_id='other'),
            lambda q: q['work_turn_budget_policy']['history'][0].update(jobs_started=-1),
            lambda q: q['work_turn_budget_policy']['history'][0].pop('reason'),
        ):
            q = copy.deepcopy(base)
            mutate(q)
            with self.assertRaises(ValueError):
                project_budget.validate_work_project(q)
        # A digest computed under the job scope cannot authorize work turns.
        swapped = copy.deepcopy(base)
        rec = swapped['work_turn_budget_policy']['history'][0]
        rec['arguments'] = project_budget.arguments_digest(
            'p1', 'unlimited', rec['request_id'], rec['reason'], rec['user_authorization'])
        with self.assertRaises(ValueError):
            project_budget.validate_work_project(swapped)

    def test_idempotence_cross_scope_ids_and_concurrent_upgrade(self):
        p = with_v1_work()
        before = json.dumps(p['work_turn_budget_policy'], sort_keys=True)
        again, replayed, _ = project_budget.authorize_work(
            p, owner_thread_id=OWNER, request_id='w-req',
            reason='User authorized unlimited work turns',
            user_authorization='Same user instruction 2026-10-29', now=time.time(),
            original_max_turns=20, turns_snapshot={'s1': 20, 's2': None})
        self.assertTrue(replayed)
        self.assertEqual(json.dumps(again['work_turn_budget_policy'], sort_keys=True), before)
        with self.assertRaises(ValueError):
            project_budget.authorize_work(
                p, owner_thread_id=OWNER, request_id='w-req', reason='changed',
                user_authorization='Same user instruction 2026-10-29', now=time.time(),
                original_max_turns=20, turns_snapshot={'s1': 20, 's2': None})
        with self.assertRaises(ValueError):
            project_budget.authorize_work(
                p, owner_thread_id=OWNER, request_id='w-req-2', reason='r',
                user_authorization='u', now=time.time(), original_max_turns=20,
                turns_snapshot={'s1': 20, 's2': None})
        with self.assertRaises(ValueError):
            project_budget.authorize_work(
                p, owner_thread_id=OWNER, request_id='v1-req', reason='r',
                user_authorization='u', now=time.time(), original_max_turns=20,
                turns_snapshot={'s1': 20, 's2': None})
        legacy = with_v1()
        with self.assertRaises(ValueError):
            project_budget.authorize(
                p, owner_thread_id=OWNER, strategy='unlimited', request_id='w-req',
                reason='x', user_authorization='y', now=time.time())
        # Atomic managed concurrency is covered by the bridge API test below.


class WorkTurnBridgeTests(unittest.TestCase):
    setUp = test_telos.BridgeTests.setUp
    tearDown = test_telos.BridgeTests.tearDown

    def create(self):
        task = {'id': 'a', 'task': 'Implement a', 'message': 'Implement the assigned change.',
                'depends_on': [], 'allowed_paths': ['a.py'], 'questions': copy.deepcopy(self.q),
                'checks': copy.deepcopy(self.checks), 'assessment_mode': 'closed_book',
                'auto_continue_checkpoints': True, 'work_reply_version': 2}
        pid = b.project_manager().create(repo=str(self.repo), task='Complete project', tasks=[task],
            questions=self.q, checks=self.checks, decision_reason='Single independent file',
            strategy='single', max_jobs=61, notify=False)['project_id']
        manager = b.project_manager()
        p = manager.load(pid)
        p['owner'] = {'thread_id': OWNER}
        manager.save(p)
        owner = {'thread_id': OWNER, 'cwd': str(self.repo), 'socket': '/fixture',
                 'transport': 'cli_queue'}
        original = b._start_job
        def start(*args, **kw):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kw)
        with patch.object(b, 'spawn_dispatch'), patch.object(b, 'bind_owner', return_value=owner), \
                patch.object(b, '_start_job', start):
            manager.tick(pid)
        p = manager.load(pid)
        self.assertNotIn('start_error', p['tasks'][0], p['tasks'][0].get('start_error'))
        return pid, p['tasks'][0]['session_id']

    def authorize(self, pid):
        manager = b.project_manager()
        p = manager.load(pid)
        p['jobs_started'] = 62
        manager.save(p)
        if not p.get('job_budget_policy'):
            b.call_tool('telos_project_budget', {
                'project_id': pid, 'request_id': 'upgrade-v1',
                'reason': 'User authorized unlimited job work',
                'user_authorization': 'Explicit user instruction 2026-10-29'}, {'threadId': OWNER})
        result = b.call_tool('telos_project_budget', {
            'project_id': pid, 'request_id': 'upgrade-1',
            'reason': 'User authorized unlimited job work',
            'user_authorization': 'Explicit user instruction 2026-10-29',
            'include_work_turns': True}, {'threadId': OWNER})
        self.assertTrue(result['job_budget']['effective_max_jobs'] is None)
        self.assertIsNone(result['work_turn_budget']['effective_max_turns'])

    def child(self, sid, turns):
        root = b.run_dir(sid)
        state = b.read_json(root / 'state.json')
        state.update(turns=turns, phase='responded', delivery=None, last_result=None)
        state.pop('last_event_id', None)
        state['events'] = {}
        state.pop('auto_check', None)
        b.write_json(root / 'state.json', state)
        return state

    def test_explicit_send_20_to_21_without_reset(self):
        pid, sid = self.create()
        self.authorize(pid)
        manager = b.project_manager()
        self.child(sid, 20)
        before = manager.load(pid)
        self.assertEqual(before['jobs_started'], 62)
        original = b._start_job
        def start(*args, **kw):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kw)
        with patch.object(b, '_start_job', start):
            result = manager.start_child(sid, 'send', 'Continue remaining work')
        self.assertFalse(result.get('duplicate', False))
        after = manager.load(pid)
        self.assertEqual(after['jobs_started'], 63)
        child = b.read_json(b.run_dir(sid) / 'state.json')
        self.assertEqual(child['turns'], 21)
        self.assertEqual(child.get('project_id'), pid)
        self.assertEqual(after['tasks'][0]['session_id'], sid)

    def test_automatic_launch_20_to_21_without_reset(self):
        pid, sid = self.create()
        self.authorize(pid)
        manager = b.project_manager()
        root = b.run_dir(sid)
        with b.locked(sid):
            state, contract = b.load_session(root)
            state.update(turns=20, phase='checkpoint', last_result=None)
            state['delivery'] = {'delivery_id': 'd1', 'next_step': 'Continue',
                                 'thread_id': state.get('thread_id')}
            state['auto_continuations'] = {}
            state.pop('auto_continue_pending', None)
            continuation.prepare(state, contract)
            b.write_json(root / 'state.json', state)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            manager.continue_pending(sid)
        child = b.read_json(b.run_dir(sid) / 'state.json')
        self.assertEqual(child['turns'], 21)
        self.assertEqual(child['auto_continuations'][state['job_id']]['kind'], 'checkpoint')
        current = manager.load(pid)
        self.assertEqual(current['jobs_started'], 63)
        self.assertEqual(current['tasks'][0]['session_id'], sid)

    def test_legacy_cap_and_paused_or_foreign_never_exempt(self):
        pid, sid = self.create()
        manager = b.project_manager()
        # Without the sibling policy the v1 ceiling alone is legacy: max_turns still blocks.
        b.call_tool('telos_project_budget', {
            'project_id': pid, 'request_id': 'legacy-v1',
            'reason': 'User authorized unlimited jobs',
            'user_authorization': 'Explicit user instruction'}, {'threadId': OWNER})
        self.child(sid, 20)
        with self.assertRaises(b.GateError) as ctx:
            manager.start_child(sid, 'send', 'Continue')
        self.assertIn('Maximum conversation turns', str(ctx.exception))
        child = b.read_json(b.run_dir(sid) / 'state.json')
        self.assertEqual(child['turns'], 20)
        # Upgrade, then pause: no exemption and no launch.
        self.authorize(pid)
        p = manager.load(pid); p['continuation'] = 'paused'; manager.save(p)
        self.child(sid, 20)
        with self.assertRaises(b.GateError):
            manager.start_child(sid, 'send', 'Continue')
        self.assertEqual(b.read_json(b.run_dir(sid) / 'state.json')['turns'], 20)
        # Foreign session is not a bound child.
        p = manager.load(pid); p['continuation'] = 'active'; manager.save(p)
        self.assertIsNone(project_budget.work_turn_decision(p, session_id='foreign-session'))

    def test_damaged_policy_and_tool_opt_in_validation(self):
        pid, _ = self.create()
        self.authorize(pid)
        manager = b.project_manager()
        current = manager.load(pid)
        v1_before = json.dumps(current['job_budget_policy'], sort_keys=True)
        bad = copy.deepcopy(current)
        bad['work_turn_budget_policy']['history'][0]['arguments'] = '0' * 64
        b.write_json(manager.path(pid), bad)
        with self.assertRaises(b.GateError):
            manager.load(pid)
        b.write_json(manager.path(pid), current)
        with self.assertRaises(b.GateError):
            b.call_tool('telos_project_budget', {
                'project_id': pid, 'request_id': 'bad-flag', 'reason': 'x',
                'user_authorization': 'y', 'include_work_turns': 'yes'}, {'threadId': OWNER})
        self.assertEqual(json.dumps(manager.load(pid)['job_budget_policy'], sort_keys=True),
                         v1_before)

    def test_concurrent_api_upgrade_creates_one_record(self):
        pid, _ = self.create()
        manager = b.project_manager()
        p = manager.load(pid); p['jobs_started'] = 62; manager.save(p)
        b.call_tool('telos_project_budget', {
            'project_id': pid, 'request_id': 'race-v1',
            'reason': 'User authorized unlimited jobs',
            'user_authorization': 'Explicit user instruction'}, {'threadId': OWNER})
        args = {'project_id': pid, 'request_id': 'race-work',
                'reason': 'User authorized unlimited work turns',
                'user_authorization': 'Explicit user instruction',
                'include_work_turns': True}
        def invoke(_):
            return b.call_tool('telos_project_budget', dict(args), {'threadId': OWNER})
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(invoke, range(6)))
        self.assertEqual(sum(not r['replayed'] for r in results), 1)
        history = manager.load(pid)['work_turn_budget_policy']['history']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['request_id'], 'race-work')

    def test_opt_in_still_blocks_progress_budget_and_exam_gates(self):
        pid, sid = self.create()
        self.authorize(pid)
        manager = b.project_manager()
        # no_progress still blocks explicit work and consumes no turn.
        self.child(sid, 20)
        with patch.object(progress_guard, 'check',
                          side_effect=b.GateError('No substantive progress', code='no_progress')):
            with self.assertRaises(b.GateError):
                manager.start_child(sid, 'send', 'Continue')
        self.assertEqual(b.read_json(b.run_dir(sid) / 'state.json')['turns'], 20)
        # Exhausted request budget still blocks with no launch/turn.
        self.child(sid, 20)
        with patch.object(usage.UsageLedger, 'ensure_available',
                          side_effect=usage.BudgetExceeded('Task budget exhausted', None)):
            with self.assertRaises(usage.BudgetExceeded):
                manager.start_child(sid, 'send', 'Continue')
        self.assertEqual(b.read_json(b.run_dir(sid) / 'state.json')['turns'], 20)
        # Exam rounds remain capped: the work exemption never covers evaluate.
        root = b.run_dir(sid)
        state = b.read_json(root / 'state.json')
        state.update(phase='responded', turns=20, rounds=5)
        b.write_json(root / 'state.json', state)
        with self.assertRaises(b.GateError) as ctx:
            manager.start_child(sid, 'evaluate')
        self.assertIn('exam rounds', str(ctx.exception))
        self.assertEqual(b.read_json(root / 'state.json')['rounds'], 5)
        # Wrong owner cannot record the sibling policy.
        with self.assertRaises(b.GateError):
            b.call_tool('telos_project_budget', {
                'project_id': pid, 'request_id': 'other-owner', 'reason': 'x',
                'user_authorization': 'y', 'include_work_turns': True},
                {'threadId': 'other'})


if __name__ == '__main__':
    unittest.main()

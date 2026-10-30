"""Real temporary state and Git; only model process launch is mocked."""
import copy
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import task_budget
import test_telos
import progress_guard
import usage

b = test_telos.b
OWNER = '00000000-0000-4000-8000-000000000001'


class StandaloneBudgetTests(unittest.TestCase):
    setUp = test_telos.BridgeTests.setUp
    tearDown = test_telos.BridgeTests.tearDown

    def create(self):
        sid = test_telos.BridgeTests.create(self)
        path = b.run_dir(sid) / 'state.json'
        state = b.read_json(path)
        state.update(owner={'thread_id': OWNER}, turns=20, phase='checkpoint')
        b.write_json(path, state)
        return sid

    def request(self, sid, **changes):
        args = {'session_id': sid, 'request_id': 'authorize-one',
                'reason': 'Resume the same task without resetting consumed work',
                'user_authorization': 'User explicitly removed this task work-turn limit'}
        args.update(changes)
        return b.call_tool('telos_task_budget', args, {'threadId': OWNER})

    def test_policy_only_change_and_replay_without_write(self):
        sid = self.create(); root = b.run_dir(sid)
        original = (root / 'contract.json').read_bytes()
        before = b.read_json(root / 'state.json')
        first = self.request(sid)
        after = b.read_json(root / 'state.json')
        self.assertEqual({k:v for k,v in after.items() if k != task_budget.KEY}, before)
        self.assertEqual(original, (root / 'contract.json').read_bytes())
        self.assertEqual(first['work_turn_budget']['initial_max_turns'], 20)
        self.assertIsNone(first['work_turn_budget']['effective_max_turns'])
        self.assertEqual(first['work_turn_budget']['used_turns'], 20)
        saved = (root / 'state.json').read_bytes()
        stamp = (root / 'state.json').stat().st_mtime_ns
        self.assertTrue(self.request(sid)['replayed'])
        self.assertEqual(saved, (root / 'state.json').read_bytes())
        self.assertEqual(stamp, (root / 'state.json').stat().st_mtime_ns)
        for changes in ({'reason':'different'}, {'request_id':'new'}, {'request_id':' spaced '},
                        {'strategy':'limited'}, {'user_authorization':'different'}):
            with self.subTest(changes=changes), self.assertRaises(b.GateError):
                self.request(sid, **changes)

    def test_legacy_stops_at_20_and_authorized_same_task_admits_21(self):
        sid = self.create()
        with self.assertRaisesRegex(b.GateError, 'Maximum conversation turns'):
            b.start_job(sid, 'send', 'Continue')
        self.request(sid)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())), patch.object(b, 'spawn_dispatch'):
            out = b.start_job(sid, 'send', 'Continue')
        state, _ = b.load_session(b.run_dir(sid))
        self.assertEqual(out['phase'], 'running')
        self.assertEqual(state['turns'], 21)
        self.assertEqual(state['session_id'], sid)
        self.assertEqual(state[task_budget.KEY]['turns_at_authorization'], 20)
        self.assertEqual(b.config()['max_turns'], 20)

    def test_pause_retained_and_owner_terminal_and_project_rejected(self):
        sid = self.create(); path = b.run_dir(sid) / 'state.json'
        state = b.read_json(path); state['continuation'] = 'paused'; b.write_json(path,state)
        self.assertEqual(self.request(sid)['continuation'], 'paused')
        with self.assertRaises(b.GateError): b.start_job(sid,'send','Continue')
        with self.assertRaises(b.GateError):
            b.call_tool('telos_task_budget', {'session_id':sid,'request_id':'x','reason':'r','user_authorization':'u'}, {'threadId':'other'})
        for change in ({'continuation':'cancelled'}, {'archived':{'at':1}}, {'project_id':'p'*32}):
            sid2=self.create(); p=b.run_dir(sid2)/'state.json'; s=b.read_json(p); s.update(change); b.write_json(p,s)
            with self.subTest(change=change), self.assertRaises(b.GateError): self.request(sid2)

    def test_damaged_policy_fails_closed_on_load(self):
        sid=self.create(); self.request(sid); root=b.run_dir(sid)
        saved=b.read_json(root/'state.json')
        mutations=[lambda s:s.update({task_budget.KEY:None}),
                   lambda s:s[task_budget.KEY].update(owner_thread_id='wrong'),
                   lambda s:s[task_budget.KEY].update(effective_max_turns=100),
                   lambda s:s[task_budget.KEY].update(turns_at_authorization=21),
                   lambda s:s[task_budget.KEY]['history'][0]['arguments'].update(reason='changed')]
        for mutate in mutations:
            state=copy.deepcopy(saved); mutate(state); b.write_json(root/'state.json',state)
            with self.assertRaises(b.GateError): b.load_session(root)
        b.write_json(root/'state.json',saved)
        b.load_session(root)

    def test_concurrent_replay_records_one_authorization(self):
        sid=self.create()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.request(sid),range(4)))
        self.assertEqual(sum(not r['replayed'] for r in results),1)
        state,_=b.load_session(b.run_dir(sid))
        self.assertEqual(len(state[task_budget.KEY]['history']),1)
        self.assertEqual(state['turns'],20)

    def test_progress_request_budget_and_exam_limits_remain(self):
        sid=self.create(); self.request(sid); root=b.run_dir(sid)
        with patch.object(progress_guard,'check',side_effect=b.GateError('no_progress')):
            with self.assertRaises(b.GateError): b.start_job(sid,'send','Continue')
        with patch.object(usage.UsageLedger,'ensure_available',side_effect=usage.BudgetExceeded('budget',None)):
            with self.assertRaises(usage.BudgetExceeded): b.start_job(sid,'send','Continue')
        state,_=b.load_session(root);self.assertEqual(state['turns'],20)
        state.update(phase='responded',rounds=5);b.write_json(root/'state.json',state)
        with self.assertRaisesRegex(b.GateError,'exam rounds'): b.start_job(sid,'evaluate')
        self.assertEqual(b.read_json(root/'state.json')['rounds'],5)

    def test_compact_status_exposes_actual_turns_and_authorization(self):
        sid=self.create(); self.request(sid)
        state,_=b.load_session(b.run_dir(sid))
        import compact
        value=compact.status({'work_turn_budget':task_budget.summary(state,20)})
        self.assertEqual(value['work_turn_budget']['used_turns'],20)
        self.assertEqual(value['work_turn_budget']['strategy'],'unlimited')
        self.assertEqual(len(value['work_turn_budget']['authorization_history']),1)

if __name__ == '__main__': unittest.main()

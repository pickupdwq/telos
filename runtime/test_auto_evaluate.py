"""Automatic evaluation after passing automatic checks: one supervisor event instead of two."""
import unittest
from unittest.mock import patch

from test_telos import b, AutoChecksTests, synthetic_exam
from test_callbacks import FakeRPC, OWNER
from codex_rpc import RPCError


class AutoEvaluateTests(unittest.TestCase):
    # Reuse the automatic-check fixtures without re-running their tests here.
    setUp = AutoChecksTests.setUp
    tearDown = AutoChecksTests.tearDown
    scores = AutoChecksTests.scores
    state = AutoChecksTests.state
    offline_jobs = AutoChecksTests.offline_jobs
    deliver = AutoChecksTests.deliver
    evaluate = AutoChecksTests.evaluate

    def create(self, **kw):
        kw.setdefault('auto_evaluate', True)
        return AutoChecksTests.create(self, **kw)

    def complete_checks(self, sid):
        # Finish the checks job only; the automatic evaluation it queues is
        # started explicitly by run_pending_evaluation.
        state = self.state(sid)
        with self.offline_jobs(), patch.object(b, 'peer_turn', side_effect=AssertionError('No model in checks')), \
                patch.object(b.project_manager().__class__, 'continue_pending'):
            b.background_job(sid, state['job_id'])
        return self.state(sid)

    def run_pending_evaluation(self, sid):
        with self.offline_jobs():
            b.project_manager().continue_pending(sid)
        state = self.state(sid)
        if state['phase'] == 'running' and state.get('job_kind') == 'evaluate':
            with self.offline_jobs(), patch.object(b, 'closed_book_exam', side_effect=synthetic_exam), \
                    patch.object(b, 'executable_checks', side_effect=AssertionError('Passing evidence must be reused')):
                b.background_job(sid, state['job_id'])
        return self.state(sid)

    def test_contract_freezes_policy_and_requires_auto_checks(self):
        sid = self.create()
        contract = b.read_json(b.run_dir(sid) / 'contract.json')
        self.assertIs(contract['auto_evaluate'], True)
        off = self.create(auto_run_checks=False)
        self.assertIs(b.read_json(b.run_dir(off) / 'contract.json')['auto_evaluate'], False)
        default = self.create(auto_evaluate=None)
        self.assertIs(b.read_json(b.run_dir(default) / 'contract.json')['auto_evaluate'], False)
        b.write_json(b.CONFIG, {'auto_evaluate': True, 'auto_run_checks': True})
        configured = b.create_session(repo=str(self.repo), task='Addition', questions=self.q,
                                      checks=self.checks, notify=False)['session_id']
        self.assertIs(b.read_json(b.run_dir(configured) / 'contract.json')['auto_evaluate'], True)
        with self.assertRaises(b.GateError):
            self.create(auto_evaluate='yes')

    def test_passing_checks_go_straight_to_exam_then_supervisor_grades(self):
        sid = self.create()
        self.deliver(sid)
        checked = self.complete_checks(sid)
        self.assertEqual(checked['phase'], 'responded')
        self.assertEqual(checked['auto_check']['auto_evaluate']['status'], 'pending')
        self.assertEqual(b.status(sid)['next_action'], 'automatic_evaluation_pending')
        reviewed = self.run_pending_evaluation(sid)
        self.assertEqual(reviewed['phase'], 'awaiting_review', reviewed.get('last_error'))
        self.assertEqual(reviewed['rounds'], 1)  # the checks round is reused, not a second round
        self.assertEqual(reviewed['auto_check']['auto_evaluate']['status'], 'started')
        packet = b.review_packet(sid)
        self.assertTrue(packet['evidence']['check_evidence']['reused'])
        self.assertNotIn('accepted', reviewed.get('acceptance') or {})
        self.assertFalse(b.status(sid)['accepted'])
        self.assertTrue(b.grade(sid, packet['packet_id'], self.scores(), [])['accepted'])

    def test_failed_checks_stop_before_exam(self):
        sid = self.create(checks=[{'id': 'failure', 'argv': ['{python}', '-c', 'assert False'],
                                   'freshness': {'mode': 'snapshot'}}])
        self.deliver(sid)
        state = self.complete_checks(sid)
        self.assertEqual(state['phase'], 'not_accepted')
        self.assertNotIn('auto_evaluate', state['auto_check'])
        self.assertEqual(self.run_pending_evaluation(sid)['phase'], 'not_accepted')

    def test_disabled_policy_keeps_the_supervisor_step(self):
        sid = self.create(auto_evaluate=False)
        self.deliver(sid)
        state = self.complete_checks(sid)
        self.assertNotIn('auto_evaluate', state['auto_check'])
        self.assertEqual(b.status(sid)['next_action'], 'telos_evaluate')
        self.assertEqual(self.run_pending_evaluation(sid)['phase'], 'responded')

    def test_pause_stops_the_automatic_evaluation(self):
        sid = self.create()
        self.deliver(sid)
        self.complete_checks(sid)
        b.return_control(sid, 'pause')
        self.assertEqual(self.run_pending_evaluation(sid)['phase'], 'responded')
        b.return_control(sid, 'resume')
        state = self.run_pending_evaluation(sid)
        self.assertEqual(state['phase'], 'awaiting_review', state.get('last_error'))

    def test_explicit_supervisor_evaluate_supersedes_the_automatic_one(self):
        sid = self.create()
        self.deliver(sid)
        self.complete_checks(sid)
        with patch.object(b, 'executable_checks', side_effect=AssertionError('reuse evidence')):
            reviewed = self.evaluate(sid)
        self.assertEqual(reviewed['phase'], 'awaiting_review', reviewed.get('last_error'))
        self.assertEqual(reviewed['auto_check']['auto_evaluate']['status'], 'stopped')
        self.assertEqual(reviewed['rounds'], 1)
        with self.offline_jobs():
            b.project_manager().continue_pending(sid)
        self.assertEqual(self.state(sid)['job_id'], reviewed['job_id'])

    def test_stale_identity_is_refused(self):
        sid = self.create()
        self.deliver(sid)
        self.complete_checks(sid)
        state = self.state(sid)
        source = state['auto_check']['auto_evaluate']['source_job_id']
        with self.assertRaises(b.GateError):
            b._start_job(sid, 'evaluate', _auto_evaluate_id='0' * 32)
        with self.assertRaises(b.GateError):
            b._start_job(sid, 'send', 'work', _auto_evaluate_id=source)


class AutoEvaluateCallbackTests(unittest.TestCase):
    """With an owner, only the exam result reaches the supervisor."""
    tearDown_fixture = AutoChecksTests.tearDown
    scores = AutoChecksTests.scores
    state = AutoChecksTests.state
    offline_jobs = AutoChecksTests.offline_jobs
    deliver = AutoChecksTests.deliver
    complete_checks = AutoEvaluateTests.complete_checks
    run_pending_evaluation = AutoEvaluateTests.run_pending_evaluation

    def setUp(self):
        AutoChecksTests.setUp(self)
        self.db = {'thread': {'id': OWNER, 'status': {'type': 'idle'}, 'cwd': str(self.repo)}, 'queue': [], 'calls': []}
        self.rpc = patch.object(b, 'CodexRPC', side_effect=lambda *_: FakeRPC(self.db))
        self.desktop = patch.object(b, 'DesktopRPC', side_effect=RPCError('No test Desktop owner'))
        self.rpc.start(); self.desktop.start()

    def tearDown(self):
        self.rpc.stop(); self.desktop.stop()
        AutoChecksTests.tearDown(self)

    def test_one_event_for_work_checks_and_exam(self):
        sid = b.create_session(repo=str(self.repo), task='Implement addition', questions=self.q,
                               checks=[dict(c, freshness={'mode': 'snapshot'}) for c in self.checks],
                               notify=True, owner_thread_id=OWNER, auto_run_checks=True, auto_evaluate=True)['session_id']
        self.deliver(sid)
        checked = self.complete_checks(sid)
        # Delivery waits while the automatic follow-up is pending.
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual([p for m, p in self.db['calls'] if m == 'thread/queue/add'], [])
        reviewed = self.run_pending_evaluation(sid)
        self.assertEqual(reviewed['phase'], 'awaiting_review', reviewed.get('last_error'))
        live = [e for e in reviewed['events'].values() if e['status'] in b.WATCH_EVENTS]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]['phase'], 'awaiting_review')
        b.deliver_once(sid)
        added = [p for m, p in self.db['calls'] if m == 'thread/queue/add']
        self.assertEqual(len(added), 1)  # the only wake-up the supervisor receives
        self.assertEqual(added[0]['clientUserMessageId'], live[0]['id'])


if __name__ == '__main__':
    unittest.main()

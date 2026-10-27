"""Real worktrees/process pipes; deterministic executors, no provider calls."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from test_telos import b, BridgeTests, synthetic_exam
from test_meetings import MeetingTests, opinion
from test_projects import ProjectTests
from test_callbacks import OWNER, OTHER
import progress_guard as pg
from processes import terminate_and_drain, process_running


class ProgressTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    create = BridgeTests.create

    @contextlib.contextmanager
    def no_worker(self, failure=None):
        original = subprocess.Popen
        def launch(argv, **kw):
            if '_job' in argv:
                if failure:
                    raise failure
                return Mock(pid=os.getpid())
            return original(argv, **kw)
        with patch.object(b.subprocess, 'Popen', side_effect=launch), patch.object(b, 'reap_background'):
            yield

    def state(self, sid):
        return b.read_json(b.run_dir(sid) / 'state.json')

    def work(self, sid, change=None, summary='Done', error=None):
        with self.no_worker():
            job = b.start_job(sid, 'send', 'Complete the public task.')['job_id']
        def peer(state, contract, message):
            if change is not None:
                (Path(state['worktree']) / 'calc.py').write_text(change)
            if error:
                raise error
            if contract.get('work_reply_version') == 2 and not summary.lstrip().startswith('{'):
                summary_text = json.dumps({'status':'completed','summary':summary,'changed_files':[],
                    'checks_performed':[],'remaining_issues':[],'clarification_questions':[]})
            else:
                summary_text = summary
            return {'thread_id': OTHER, 'summary': summary_text}
        with patch.object(b, 'peer_turn', side_effect=peer):
            b.background_job(sid, job)
        return self.state(sid)

    def replan(self, sid, ident='p1', **kw):
        args = dict(session_id=sid, request_id=ident, blocker='Working files stayed unchanged.',
                    approach='Inspect the arithmetic edge case ' + ident,
                    evidence='The local comparison showed unchanged implementation.',
                    success_criterion='A focused regression passes with the changed implementation.')
        args.update(kw)
        return b.call_tool('telos_replan', args)

    def test_three_unchanged_jobs_stop_even_with_different_summaries_and_ids(self):
        sid = self.create(_workflow_version=3)
        for i in range(3):
            s = self.work(sid, summary='Fresh summary ' + str(i))
        self.assertTrue(pg.blocked(s))
        self.assertEqual(s['progress_guard']['streams']['work']['streak'], 3)
        with self.no_worker(), self.assertRaises(b.GateError) as caught:
            b.start_job(sid, 'send', 'Different wording; try harder.')
        self.assertEqual(caught.exception.code, 'no_progress')
        full = b.status(sid); compact = b.status(sid, 'summary')
        self.assertEqual(compact['progress_guard'], full['progress_guard'])
        self.assertEqual(compact['next_action'], 'telos_replan')
        self.assertIsNone(b.read_json(b.run_dir(sid) / 'contract.json')['budget'])

    def test_replans_are_idempotent_single_trials_and_cannot_be_an_endless_loop(self):
        sid = self.create()
        for _ in range(3): self.work(sid)
        self.replan(sid)
        self.assertTrue(self.replan(sid)['duplicate'])
        with self.assertRaises(b.GateError): self.replan(sid, 'another')
        s = self.work(sid)
        request = b.read_json(b.run_dir(sid) / f"job-{s['job_id']}.json")
        self.assertIn('Supervisor adjusted plan', request['message'])
        self.assertTrue(pg.blocked(s))
        with self.assertRaises(b.GateError):
            self.replan(sid, 'same-plan-new-id', approach='Inspect the arithmetic edge case p1')
        self.replan(sid, 'p2'); self.work(sid)
        with self.assertRaises(b.GateError) as caught: self.replan(sid, 'p3')
        self.assertEqual(caught.exception.next_action, 'report_blocker_to_user')
        self.assertEqual(b.status(sid)['next_action'], 'report_blocker_to_user')

    def test_actual_change_resets_streak_but_reverting_to_seen_content_does_not(self):
        sid = self.create()
        self.work(sid); self.work(sid)
        s = self.work(sid, change='def add(a, b):\n    return a + b\n')
        self.assertEqual(s['progress_guard']['streams']['work']['streak'], 0)
        s = self.work(sid, change='def add(a, b): return a + b\n')
        self.assertEqual(s['progress_guard']['streams']['work']['streak'], 1)

    def test_replan_cannot_bypass_owner_pause_or_acceptance_contract(self):
        sid = self.create()
        for _ in range(3): self.work(sid)
        path = b.run_dir(sid) / 'state.json'; s = self.state(sid)
        s['owner'] = {'thread_id': OWNER}; b.write_json(path, s)
        with self.assertRaises(b.GateError) as caught: self.replan(sid)
        self.assertEqual(caught.exception.code, 'owner_mismatch')
        s['owner'] = None; s['continuation'] = 'paused'; b.write_json(path, s)
        with self.assertRaises(b.GateError): self.replan(sid)
        self.assertEqual(self.state(sid)['continuation'], 'paused')
        self.assertFalse(b.status(sid)['accepted'])

    def test_failures_count_without_clearing_progress_and_receipts_do_not_double_count(self):
        sid = self.create()
        for _ in range(3):
            s = self.work(sid, error=b.providers.ExecutionTimeout('fixture timeout'))
        self.assertTrue(pg.blocked(s))
        before = json.loads(json.dumps(s['progress_guard']))
        pg.completed(b.bridge_services(), s, b.read_json(b.run_dir(sid) / 'contract.json'), {'kind': 'send'})
        self.assertEqual(before, s['progress_guard'])
        self.assertEqual(s['last_failure_kind'], 'execution_timeout')

    def test_waiting_for_clarification_is_not_stagnation(self):
        sid = self.create()
        summary = json.dumps({'summary': 'Need business input', 'status': 'needs_clarification',
                              'clarification_questions': ['Should strings be supported?'],
                              'changed_files': [], 'checks_performed': [], 'remaining_issues': []})
        for _ in range(3): s = self.work(sid, summary=summary)
        self.assertFalse(pg.blocked(s)); self.assertEqual(s['phase'], 'needs_clarification')

    def test_adjusted_trial_can_wait_for_and_receive_a_clarification_answer(self):
        sid = self.create()
        for _ in range(3): self.work(sid)
        self.replan(sid)
        summary = json.dumps({'summary': 'Need business input', 'status': 'needs_clarification',
                              'clarification_questions': ['Should strings be supported?'],
                              'changed_files': [], 'checks_performed': [], 'remaining_issues': []})
        self.work(sid, summary=summary)
        self.assertEqual(b.status(sid)['next_action'], 'answer_clarification_then_send')
        with self.no_worker(): b.start_job(sid, 'send', 'Only signed integers are required.')
        self.assertEqual(self.state(sid)['phase'], 'running')

    def test_startup_failure_has_backoff_and_three_failures_stop_without_spend(self):
        sid = self.create()
        for i in range(3):
            with self.no_worker(OSError('fixture spawn failure')), patch('time.time', return_value=time.time() + 100 * i):
                with self.assertRaises(b.GateError): b.start_job(sid, 'send', 'Run task')
        s = self.state(sid)
        self.assertEqual(s['turns'], 0); self.assertEqual(s['phase'], 'ready')
        self.assertTrue(pg.blocked(s))
        self.assertEqual(s['progress_guard']['streams']['startup']['streak'], 3)

    def test_new_checks_count_but_changed_logs_and_alternating_failures_do_not(self):
        s = {}
        pg.observe(s, 'checks', '1', ['passed-a'])
        pg.observe(s, 'checks', '2', ['passed-a'])
        pg.observe(s, 'checks', '3', [])
        pg.observe(s, 'checks', '4', ['passed-a'])
        self.assertTrue(pg.blocked(s))
        pg.observe(s, 'checks', '5', ['passed-a', 'passed-b'])
        self.assertFalse(pg.blocked(s))

    def test_adjusted_work_can_still_pass_real_checks_and_same_thread_acceptance(self):
        sid = self.create(_workflow_version=3)
        for _ in range(3): self.work(sid)
        contract_before = b.read_json(b.run_dir(sid) / 'contract.json')
        self.replan(sid)
        self.work(sid, change='def add(a, b):\n    return a + b\n')
        with self.no_worker(): job = b.start_job(sid, 'evaluate')['job_id']
        with patch.object(b, 'closed_book_exam', side_effect=synthetic_exam): b.background_job(sid, job)
        s = self.state(sid)
        self.assertTrue(all(x['status'] == 'passed' for x in b.review_packet(sid)['evidence']['checks']))
        self.assertTrue(b.grade(sid, s['packet_id'], BridgeTests.scores(self), [])['accepted'])
        self.assertEqual(b.read_json(b.run_dir(sid) / 'contract.json'), contract_before)
        self.assertEqual(self.state(sid)['thread_id'], OTHER)

    def test_unreadable_evidence_and_lost_background_process_do_not_stay_running(self):
        sid = self.create()
        with self.no_worker(): b.start_job(sid, 'send', 'Complete work')
        with patch.object(b, 'process_running', return_value=False), \
                patch.object(pg, 'source_evidence', side_effect=OSError('fixture unreadable')):
            result = b.status(sid)
        self.assertEqual(result['phase'], 'error')
        self.assertEqual(result['progress_guard']['streams']['work']['streak'], 1)

    def test_blocked_callback_receipt_routes_to_replan(self):
        sid = self.create()
        for _ in range(3): s = self.work(sid)
        s['owner'] = {'thread_id': OWNER}; b.record_event(s)
        b.write_json(b.run_dir(sid) / 'state.json', s)
        self.assertIn('Do not repeat', b.event_message(sid, s['events'][s['last_event_id']]))
        receipt = b.call_tool('telos_receive', {'session_id': sid, 'event_id': s['last_event_id']}, {'threadId': OWNER})
        self.assertTrue(receipt['proceed']); self.assertEqual(receipt['next_action'], 'telos_replan')


class MeetingProgressTests(unittest.TestCase):
    setUp = MeetingTests.setUp
    tearDown = MeetingTests.tearDown
    no_workers = MeetingTests.no_workers
    open = MeetingTests.open
    project = MeetingTests.project
    child = MeetingTests.child
    start = MeetingTests.start
    finish = MeetingTests.finish
    conclude = MeetingTests.conclude

    def test_repeated_opinions_block_followups_but_keep_conclusion_available(self):
        mid = self.open(); self.start(mid); self.finish(mid, 'p0'); self.finish(mid, 'p1')
        for i in range(3):
            with self.no_workers():
                b.call_tool('telos_meeting_round', {'meeting_id': mid, 'request_id': str(i),
                    'messages': [{'participant_id': 'p0', 'message': 'Review the same issue.'}],
                    'decision_reason': 'Still debating.'})
            text = json.loads(opinion()); text['summary'] += str(i)
            self.finish(mid, 'p0', json.dumps(text))
        before = self.project(mid)['jobs_started']
        with self.no_workers(), self.assertRaises(b.GateError) as caught:
            b.call_tool('telos_meeting_round', {'meeting_id': mid, 'request_id': 'another',
                'messages': [{'participant_id': 'p0', 'message': 'Try harder.'}], 'decision_reason': 'New reason.'})
        self.assertEqual(caught.exception.code, 'no_progress')
        view = b.project_manager().status(mid, 'summary')
        self.assertTrue(view['discussion']['progress_guard']['blocked'])
        for _ in range(3): b.project_manager().recover()
        self.assertEqual(self.project(mid)['jobs_started'], before)
        self.assertEqual(self.conclude(mid)['phase'], 'discussed')

    def test_rotating_participants_does_not_reset_streak_and_replan_allows_one_round(self):
        mid = self.open(); self.start(mid); self.finish(mid, 'p0'); self.finish(mid, 'p1')
        for i in range(3):
            ident = 'new' + str(i)
            with self.no_workers():
                b.call_tool('telos_meeting_round', {'meeting_id': mid, 'request_id': ident,
                    'messages': [], 'add_participants': [{'id': ident, 'role': 'Additional reviewer'}],
                    'decision_reason': 'Check the same design.'})
            self.finish(mid, ident)
        self.assertTrue(b.project_manager().status(mid)['discussion']['progress_guard']['blocked'])
        result = b.call_tool('telos_replan', {'session_id': mid, 'request_id': 'new-experiment',
            'blocker': 'Same opinions repeated.', 'approach': 'Run a signed-input experiment.',
            'evidence': 'The original implementation is available for the experiment.',
            'success_criterion': 'Supply a new observed result.'})
        self.assertTrue(result['progress_guard']['attempt_allowed'])
        with self.no_workers():
            b.call_tool('telos_meeting_round', {'meeting_id': mid, 'request_id': 'experiment',
                'messages': [{'participant_id': 'p0', 'message': 'Run the adjusted experiment.'}],
                'decision_reason': 'Verify new evidence.'})
        text = json.loads(opinion()); text['evidence'].append('add(-2, 5) returned 3 in the experiment.')
        self.finish(mid, 'p0', json.dumps(text))
        self.assertFalse(b.project_manager().status(mid)['discussion']['progress_guard']['blocked'])


class ProjectProgressTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    state = ProjectTests.state
    child = ProjectTests.child

    def test_scheduler_stops_repeated_start_failures_across_recovery(self):
        pid = self.create([self.task('a')])
        with patch.object(b, '_start_job', side_effect=b.GateError('fixture backend unavailable')) as start:
            now = time.time()
            for i in range(3):
                with patch('time.time', return_value=now + i * 100): b.project_manager().tick(pid)
            self.assertEqual(start.call_count, 3)
            for i in range(5):
                with patch('time.time', return_value=now + 1000 + i * 100): b.project_manager().recover()
            self.assertEqual(start.call_count, 3)
        view = b.project_manager().status(pid, 'summary')
        self.assertEqual(view['jobs_started'], 0)
        self.assertTrue(view['tasks'][0]['progress_guard']['blocked'])
        self.assertEqual(view['tasks'][0]['next_action'], 'telos_replan')


class TimeoutCleanupTests(unittest.TestCase):
    def stop(self, pid):
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass

    def test_orphan_pipe_cannot_hold_timeout_cleanup_and_partial_output_is_preserved(self):
        script = ('import subprocess,sys\n'
                  'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],start_new_session=True)\n'
                  'print(p.pid,flush=True)\n'
                  'print("partial",file=sys.stderr,flush=True)\n')
        p = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
        try: p.communicate(timeout=.2)
        except subprocess.TimeoutExpired as error:
            pid = int(error.output.strip()); self.addCleanup(self.stop, pid)
            started = time.monotonic()
            out, err, incomplete = terminate_and_drain(p, error)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(incomplete); self.assertIn(b'partial', err); self.assertIn(str(pid).encode(), out)
        self.assertIsNotNone(p.returncode)

    def test_browser_timeout_uses_bounded_teardown_with_detached_pipe_holder(self):
        import audit_probe
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve(); pidfile = folder / 'child.pid'
            module = folder / 'module'; module.touch()
            executable = folder / 'browser'; executable.touch()
            real = subprocess.Popen
            script = ('import subprocess,sys,time,pathlib\n'
                      'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],start_new_session=True)\n'
                      'pathlib.Path(sys.argv[1]).write_text(str(p.pid))\n'
                      'time.sleep(30)\n')
            def launch(argv, **kw):
                return real([sys.executable, '-c', script, str(pidfile)] if argv[0] == '/usr/bin/sandbox-exec' else argv, **kw)
            policy = {'browser': {'module_path': str(module), 'executable_path': str(executable)},
                      'limits': {'probe_timeout_seconds': 1}}
            started = time.monotonic()
            try:
                with patch.object(audit_probe.subprocess, 'Popen', side_effect=launch), \
                        patch.object(audit_probe.shutil, 'which', return_value=sys.executable):
                    with self.assertRaisesRegex(b.GateError, 'timed out'):
                        audit_probe.browser_probe(b, policy, {}, folder)
                self.assertLess(time.monotonic() - started, 14)
                self.assertFalse(json.loads((folder / 'timeout.json').read_text())['accepted'])
            finally:
                if pidfile.exists(): self.stop(int(pidfile.read_text()))


if __name__ == '__main__':
    unittest.main()

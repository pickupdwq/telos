"""Timing coverage is independent of budget and acceptance decisions."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import timing
from usage import UsageLedger
from test_callbacks import CallbackTests, OWNER, OTHER
from test_telos import b
from test_projects import ProjectTests, Projects


class TimingTests(unittest.TestCase):
    def test_latency_includes_failures_and_separates_unknown_from_in_flight(self):
        entries = [
            {'phase': 'work', 'status': 'completed', 'latency_ms': 1500},
            {'phase': 'repair', 'status': 'failed', 'latency_ms': 2500},
            {'phase': 'exam', 'status': 'completed'},
            {'phase': 'exam', 'status': 'reserved', 'latency_ms': 8000},
        ]
        summary = timing.request_latency(entries)
        self.assertEqual(summary['observed_seconds'], 4)
        self.assertEqual(summary['requests'], 4)
        self.assertEqual(summary['measured_requests'], 2)
        self.assertEqual(summary['missing_requests'], 1)
        self.assertEqual(summary['in_flight_requests'], 1)
        self.assertTrue(summary['phases']['work']['complete'])
        self.assertFalse(summary['phases']['exam']['complete'])
        self.assertFalse(summary['complete'])

    def test_invalid_latency_never_changes_usage_or_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = UsageLedger(Path(directory) / 'usage.json', {'max_requests': 8})
            for value in (float('nan'), float('inf'), -1, True, '7', 10 ** 500):
                ledger.finish(ledger.begin('work'), {'input_tokens': 3, 'output_tokens': 2}, latency_ms=value)
            summary = ledger.snapshot()
            self.assertEqual(summary['totals']['total_tokens'], 30)
            self.assertEqual(summary['budget_state']['remaining']['max_requests'], 2)
            self.assertEqual(summary['request_latency']['missing_requests'], 6)
            self.assertEqual(summary['request_latency']['observed_seconds'], 0)
            raw = json.loads(ledger.path.read_text())
            self.assertTrue(all('latency_ms' not in row for row in raw['requests'].values()))

    def test_project_latency_preserves_explicit_incomplete_and_unavailable_sources(self):
        known = timing.request_latency([{'status': 'failed', 'phase': 'work', 'latency_ms': 2000}])
        incomplete = timing.request_latency([])
        incomplete['complete'] = False
        self.assertFalse(timing.merge_latency([known, incomplete])['complete'])
        result = timing.merge_latency([known, None])
        self.assertEqual(result['observed_seconds'], 2)
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['unavailable_sources'], 1)
        self.assertFalse(result['complete'])
        self.assertFalse(result['phases']['exam']['complete'])

    def test_lifecycle_intervals_and_duplicate_transitions_do_not_double_count(self):
        state = {'created_at': 10, 'phase': 'ready'}
        timing.initialize(state, 10)
        for stage, at in [('work', 12), ('callback_wait', 17), ('supervisor_wait', 20),
                          ('supervisor_wait', 22), ('checks', 25), ('exam', 30),
                          ('callback_wait', 40), ('supervisor_wait', 45), (None, 50)]:
            timing.transition(state, stage, at)
        state['phase'] = 'accepted'
        before = copy.deepcopy(state)
        result = timing.snapshot(state, 100)
        self.assertEqual(state, before)
        self.assertEqual(result['elapsed_wall_seconds'], 40)
        self.assertEqual(result['phase_seconds'], {
            'idle': 2, 'work': 5, 'checks': 5, 'exam': 10,
            'callback_wait': 8, 'supervisor_wait': 10, 'paused': 0})
        self.assertTrue(result['complete'])

    def test_legacy_corrupt_interrupted_and_regressing_clocks_stay_incomplete(self):
        legacy = {'created_at': 10, 'phase': 'accepted'}
        self.assertIsNone(timing.snapshot(legacy, 100)['elapsed_wall_seconds'])
        self.assertIsNone(timing.snapshot(legacy, 100)['phase_seconds'])
        for value in (None, {}, {'phase_seconds': 'bad'}):
            state = {'created_at': 10, 'phase': 'running', 'timing': value}
            timing.transition(state, 'work', 20)
            self.assertFalse(timing.snapshot(state, 25)['complete'])
        state = {'created_at': 10, 'phase': 'running'}
        timing.initialize(state, 10)
        timing.transition(state, 'work', 12)
        timing.transition(state, 'supervisor_wait', 30, interrupted=True)
        result = timing.snapshot(state, 35)
        self.assertFalse(result['complete'])
        self.assertEqual(result['phase_seconds']['work'], 0)
        timing.transition(state, 'checks', 20)
        self.assertFalse(timing.snapshot(state, 21)['complete'])

    def test_project_wall_time_is_not_sum_of_overlapping_children(self):
        project = {'created_at': 10, 'phase': 'running',
                   'check_timing': {'observed_seconds': 3, 'completed_runs': 1, 'complete': True}}
        children = []
        for name in ('a', 'b'):
            state = {'created_at': 10, 'phase': 'running'}
            timing.initialize(state, 10); timing.transition(state, 'work', 10)
            children.append({'id': name, 'phase': 'running', 'timing': timing.snapshot(state, 30)})
        result = timing.project_snapshot(project, children, 30)
        self.assertEqual(result['elapsed_wall_seconds'], 20)
        self.assertEqual(result['child_phase_seconds']['work'], 40)
        self.assertTrue(result['children_complete'])
        children.append({'id': 'corrupt', 'phase': 'quarantined'})
        project['phase'] = 'verifying'
        result = timing.project_snapshot(project, children, 30)
        self.assertFalse(result['children_complete'])
        self.assertEqual(result['incomplete_children'], ['corrupt'])
        self.assertFalse(result['integration_checks']['complete'])

    def test_damaged_integration_metrics_do_not_block_new_measurements(self):
        for value in (None, 'bad', {'observed_seconds': float('nan'), 'completed_runs': 2}):
            project = {'check_timing': value}
            timing.finish_checks(project, 1.5)
            self.assertEqual(project['check_timing']['observed_seconds'], 1.5)
            self.assertFalse(project['check_timing']['complete'])
        project = {'check_timing': {'observed_seconds': 2, 'completed_runs': 1, 'complete': True}}
        timing.finish_checks(project, 3)
        self.assertEqual(project['check_timing'], {'observed_seconds': 5, 'completed_runs': 2, 'complete': True})


class LifecycleIntegrationTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    create = CallbackTests.create

    def stage(self, sid):
        return b.read_json(b.run_dir(sid) / 'state.json')['timing']['stage']

    def start(self, sid, kind, event_id=None):
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            return b.start_job(sid, kind, 'Implement addition' if kind == 'send' else '', event_id)['job_id']

    def test_work_checks_exam_callback_review_pause_and_cancel_are_observed(self):
        sid = self.create(notify=True, owner_thread_id=OWNER)
        job = self.start(sid, 'send')
        self.assertEqual(self.stage(sid), 'work')
        with patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': 'Done'}):
            b.background_job(sid, job)
        self.assertEqual(self.stage(sid), 'callback_wait')
        receipt = b.receive_event(sid, job)
        self.assertTrue(receipt['proceed'])
        self.assertEqual(self.stage(sid), 'supervisor_wait')
        exam_job = self.start(sid, 'evaluate', job)
        self.assertEqual(self.stage(sid), 'checks')

        def exam(_):
            self.assertEqual(self.stage(sid), 'exam')
            return {'answers': [{'id': 'q1', 'answer': '5'}, {'id': 'q2', 'answer': '3'}]}

        with patch.object(b, 'answer_exam', side_effect=exam):
            b.background_job(sid, exam_job)
        self.assertEqual(self.stage(sid), 'callback_wait')
        self.assertTrue(b.receive_event(sid, exam_job)['proceed'])
        self.assertEqual(self.stage(sid), 'supervisor_wait')
        b.return_control(sid, 'pause')
        self.assertEqual(self.stage(sid), 'paused')
        b.return_control(sid, 'resume')
        self.assertEqual(self.stage(sid), 'callback_wait')
        b.return_control(sid, 'cancel')
        self.assertIsNone(self.stage(sid))
        status = b.call_tool('telos_status', {'session_id': sid}, {'threadId': OWNER})
        self.assertTrue(status['timing']['complete'])
        self.assertIsNotNone(status['timing']['elapsed_wall_seconds'])


class ProjectTimingIntegrationTests(unittest.TestCase):
    setUp = ProjectTests.setUp
    tearDown = ProjectTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child

    def test_reassignment_keeps_old_duration_and_marks_missing_history(self):
        pid = self.create([self.task('a')]); self.start(pid)
        old = self.child(pid, 'a')
        old.update(phase='error', continuation='cancelled')
        old['created_at'] -= 20
        timing.initialize(old, old['created_at'])
        timing.transition(old, 'work', old['created_at'])
        timing.transition(old, None, old['created_at'] + 12)
        old_path = b.run_dir(old['session_id']) / 'state.json'
        b.write_json(old_path, old)
        _, contract = b.load_session(old_path.parent)
        ledger = b.usage_ledger(old, contract)
        ledger.finish(ledger.begin('work'), {'input_tokens': 7, 'output_tokens': 3}, latency_ms=1250)
        with patch.object(Projects, 'tick', return_value={}):
            b.project_manager().reassign(pid, 'a', 'Retry with the configured executor')
        queued = b.project_manager().status(pid, detail='summary')
        self.assertEqual(queued['tasks'][0]['phase'], 'pending')
        self.assertEqual(queued['usage']['total_tokens'], 10)
        self.assertEqual(queued['usage']['request_latency']['observed_seconds'], 1.25)
        self.start(pid)
        full = b.project_manager().status(pid)
        summary = b.project_manager().status(pid, detail='summary')
        self.assertGreaterEqual(full['timing']['child_phase_seconds']['work'], 12)
        self.assertGreaterEqual(summary['timing']['child_phase_seconds']['work'], 12)
        self.assertTrue(summary['timing']['children_complete'])
        self.assertIn('timing', summary['tasks'][0])
        old_path.unlink()
        damaged = b.project_manager().status(pid, detail='summary')['timing']
        self.assertFalse(damaged['children_complete'])
        self.assertEqual(damaged['incomplete_children'], ['a@' + old['session_id'][:8]])

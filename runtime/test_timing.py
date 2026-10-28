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
            'callback_wait': 8, 'supervisor_wait': 10, 'paused': 0, 'scheduler_wait': 0})
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

class SchedulerTimingIntegrationTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    create = CallbackTests.create

    def setup_clock(self):
        import time
        self.now = time.time()
        clock = patch.object(timing.time, 'time', side_effect=lambda:self.now)
        clock.start(); self.addCleanup(clock.stop)

    def checkpoint(self, output=None):
        from test_clarification import CheckpointTests, phase1_reply
        fixture=CheckpointTests()
        sid=self.create(_workflow_version=3,auto_continue_checkpoints=True,
                        notify=True,owner_thread_id=OWNER)
        fixture.launch(sid)
        self.now += 10
        with patch.object(Projects,'capacity',return_value=False):
            state,launches,_=fixture.finish(sid,output or phase1_reply())
        self.assertEqual(launches,[])
        return sid,state

    def load(self,sid):
        return b.load_session(b.run_dir(sid))[0]

    def test_checkpoint_capacity_wait_is_durable_and_not_work_or_supervisor_time(self):
        self.setup_clock()
        sid,state=self.checkpoint()
        frozen=(b.run_dir(sid)/'contract.json').read_bytes()
        before=timing.snapshot(state)
        self.now += 60
        with patch.object(Projects,'capacity',return_value=False), patch.object(b.subprocess,'Popen') as spawn:
            for _ in range(2):
                b.project_manager().continue_pending(sid)
                b.recover_progress(sid)
            spawn.assert_not_called()
        after=b.status(sid,'summary')['timing']
        self.assertEqual(after['phase_seconds']['work'],before['phase_seconds']['work'])
        self.assertEqual(after['current_stage'],'scheduler_wait')
        self.assertEqual(after['phase_seconds']['scheduler_wait'],60)
        self.assertEqual(after['phase_seconds']['supervisor_wait'],0)
        self.assertEqual(after['phase_seconds']['callback_wait'],0)
        self.assertEqual(self.load(sid)['turns'],1)
        self.assertFalse(self.load(sid).get('last_event_id'))
        self.assertEqual((b.run_dir(sid)/'contract.json').read_bytes(),frozen)

    def test_pause_resume_cancel_preserve_wait_totals_without_launching(self):
        self.setup_clock()
        sid,_=self.checkpoint()
        self.now += 60
        b.return_control(sid,'pause')
        self.now += 20
        self.assertEqual(timing.snapshot(self.load(sid))['current_stage'],'paused')
        b.return_control(sid,'resume')
        self.now += 15
        with patch.object(Projects,'capacity',return_value=False), patch.object(b.subprocess,'Popen') as spawn:
            b.project_manager().continue_pending(sid)
            spawn.assert_not_called()
        b.return_control(sid,'cancel')
        self.now += 30
        result=timing.snapshot(self.load(sid))
        self.assertIsNone(result['current_stage'])
        self.assertEqual(result['phase_seconds']['work'],10)
        self.assertEqual(result['phase_seconds']['scheduler_wait'],75)
        self.assertEqual(result['phase_seconds']['paused'],20)
        self.assertEqual(self.load(sid)['turns'],1)

    def test_successful_start_ends_wait_once_and_completion_still_waits_for_review(self):
        from test_clarification import CheckpointTests,phase1_reply
        self.setup_clock()
        sid,_=self.checkpoint()
        self.now += 60
        real=b.subprocess.Popen
        with patch.object(b.subprocess,'Popen',side_effect=lambda argv,**kw:
                          Mock(pid=os.getpid()) if '_job' in argv else real(argv,**kw)) as spawn:
            b.project_manager().continue_pending(sid)
            b.project_manager().continue_pending(sid)
        self.assertEqual(sum('_job' in call.args[0] for call in spawn.call_args_list),1)
        self.now += 5
        state=self.load(sid)
        result=timing.snapshot(state)
        self.assertEqual(result['current_stage'],'work')
        self.assertEqual(result['phase_seconds']['work'],15)
        self.assertEqual(result['phase_seconds']['scheduler_wait'],60)
        CheckpointTests().finish(sid,phase1_reply('completed'))
        self.assertEqual(timing.snapshot(self.load(sid))['current_stage'],'callback_wait')
        self.assertFalse(b.status(sid)['accepted'])

    def test_failed_spawn_stops_scheduler_clock_and_notifies_supervisor(self):
        self.setup_clock()
        sid,_=self.checkpoint()
        self.now += 60
        real=b.subprocess.Popen
        def fail(argv,**kwargs):
            if '_job' in argv: raise OSError('fixture launch failed')
            return real(argv,**kwargs)
        with patch.object(b.subprocess,'Popen',side_effect=fail):
            b.project_manager().continue_pending(sid)
        self.now += 20
        result=timing.snapshot(self.load(sid))
        self.assertEqual(result['current_stage'],'callback_wait')
        self.assertEqual(result['phase_seconds']['work'],10)
        self.assertEqual(result['phase_seconds']['scheduler_wait'],60)
        self.assertEqual(result['phase_seconds']['callback_wait'],20)

    def test_blocked_or_decision_stages_do_not_accrue_scheduler_wait(self):
        from test_clarification import phase1_reply
        self.setup_clock()
        for status in ('needs_clarification','blocked_environment','completed'):
            with self.subTest(status=status):
                sid,_=self.checkpoint(phase1_reply(status))
                self.now += 60
                result=timing.snapshot(self.load(sid))
                self.assertEqual(result['current_stage'],'callback_wait')
                self.assertEqual(result['phase_seconds']['scheduler_wait'],0)
                self.assertEqual(result['phase_seconds']['work'],10)

    def test_saved_legacy_pending_task_starts_new_wait_measurement_without_rewriting_history(self):
        self.setup_clock()
        sid,state=self.checkpoint()
        root=b.run_dir(sid)
        frozen=(root/'contract.json').read_bytes()
        state['timing']['version']=1
        state['timing'].pop('scheduler_wait_tracked_since')
        state['timing']['phase_seconds'].pop('scheduler_wait')
        state['timing'].update(stage='work',since=self.now)
        b.write_json(root/'state.json',state)
        self.now += 60
        with patch.object(Projects,'capacity',return_value=False):
            b.project_manager().continue_pending(sid)
        boundary=self.now
        self.now += 15
        observed=timing.snapshot(self.load(sid))
        self.assertEqual(observed['current_stage'],'scheduler_wait')
        self.assertEqual(observed['phase_seconds']['work'],70)
        self.assertEqual(observed['phase_seconds']['scheduler_wait'],15)
        self.assertEqual(observed['scheduler_wait_tracked_since'],boundary)
        self.assertEqual((root/'contract.json').read_bytes(),frozen)

    def test_protocol_correction_wait_is_also_scheduler_time(self):
        self.setup_clock()
        sid,_=self.checkpoint('invalid protocol text')
        self.now += 60
        result=timing.snapshot(self.load(sid))
        self.assertEqual(result['current_stage'],'scheduler_wait')
        self.assertEqual(result['phase_seconds']['work'],10)
        self.assertEqual(result['phase_seconds']['scheduler_wait'],60)


class SchedulerTimingCompatibilityTests(unittest.TestCase):
    def legacy(self):
        stages=('idle','work','checks','exam','callback_wait','supervisor_wait','paused')
        return {'created_at':10,'phase':'responded','timing':{
            'version':1,'started_at':10,'since':30,'stage':'supervisor_wait',
            'phase_seconds':{k:(20 if k=='work' else 0) for k in stages},'complete':True}}

    def test_old_timing_counters_survive_read_and_new_transition(self):
        state=self.legacy(); before=copy.deepcopy(state)
        observed=timing.snapshot(state,40)
        self.assertEqual(state,before)
        self.assertEqual(observed['phase_seconds']['work'],20)
        self.assertEqual(observed['phase_seconds']['supervisor_wait'],10)
        self.assertIsNone(observed.get('scheduler_wait_tracked_since'))
        timing.transition(state,'scheduler_wait',40)
        observed=timing.snapshot(state,100)
        self.assertEqual(observed['phase_seconds']['work'],20)
        self.assertEqual(observed['phase_seconds']['supervisor_wait'],10)
        self.assertEqual(observed['phase_seconds']['scheduler_wait'],60)
        self.assertEqual(observed['scheduler_wait_tracked_since'],40)

    def test_corrupt_new_counter_is_not_treated_as_valid_legacy_data(self):
        state=self.legacy()
        state['timing'].update(version=2,scheduler_wait_tracked_since=30)
        state['timing']['phase_seconds']['scheduler_wait']='bad'
        self.assertFalse(timing.snapshot(state,40)['complete'])
        self.assertIsNone(timing.snapshot(state,40)['phase_seconds'])

class ProjectSchedulerTimingTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child
    setup_clock = SchedulerTimingIntegrationTests.setup_clock

    def test_project_child_wait_pause_resume_and_aggregate_preserve_job_count(self):
        from test_clarification import phase1_reply
        self.setup_clock()
        pid=self.create([self.task('a')],auto_continue_checkpoints=True)
        self.start(pid);child=self.child(pid,'a')
        self.now += 10
        with patch.object(Projects,'capacity',return_value=False), \
             patch.object(b,'peer_turn',return_value={'thread_id':OTHER,'summary':phase1_reply()}):
            b.background_job(child['session_id'],child['job_id'])
        self.now += 60
        before=b.project_manager().status(pid,'summary')
        self.assertEqual(before['timing']['child_phase_seconds']['work'],10)
        self.assertEqual(before['timing']['child_phase_seconds']['scheduler_wait'],60)
        self.assertEqual(before['tasks'][0]['timing']['current_stage'],'scheduler_wait')
        b.project_manager().control(pid,'pause')
        self.now += 20
        with patch.object(Projects,'capacity',return_value=False):
            b.project_manager().control(pid,'resume')
            self.now += 15
            b.project_manager().tick(pid)
        final=b.project_manager().status(pid,'summary')
        self.assertEqual(final['jobs_started'],1)
        self.assertEqual(final['timing']['child_phase_seconds']['work'],10)
        self.assertEqual(final['timing']['child_phase_seconds']['scheduler_wait'],75)
        self.assertEqual(final['timing']['child_phase_seconds']['paused'],20)


class ParallelStageTimingTests(unittest.TestCase):
    def test_overlapping_stages_have_union_and_overlap_separate_from_lifecycle(self):
        state = {'created_at': 10, 'phase': 'running'}
        timing.initialize(state, 10)
        timing.begin(state, 'local_checks', 'check', at=12)
        timing.begin(state, 'supervisor_processing', 'review', at=14)
        timing.end(state, 'check', at=20)
        timing.end(state, 'review', at=22)
        timing.mark(state, 'review_completed', 'review', at=22)
        result = timing.snapshot(state, 25)['stages']
        self.assertEqual(result['cumulative_seconds']['local_checks'], 8)
        self.assertEqual(result['cumulative_seconds']['supervisor_processing'], 8)
        self.assertEqual(result['union_seconds'], 10)
        self.assertEqual(result['overlap_seconds'], 6)
        self.assertEqual(result['milestones'][0]['at'], 22)

    def test_missing_legacy_and_interrupted_spans_are_not_invented(self):
        state = {'created_at': 1, 'phase': 'running'}
        self.assertIsNone(timing.snapshot(state, 10)['stages']['tracked_since'])
        timing.begin(state, 'environment_preparation', 'env', at=10)
        timing.end(state, 'env', at=20, outcome='interrupted')
        timing.begin(state, 'local_checks', 'checks', at=20)
        value = timing.snapshot(state, 25)['stages']
        self.assertFalse(value['complete'])
        self.assertEqual(value['in_progress'], ['checks'])
        self.assertEqual(value['intervals'][0]['outcome'], 'interrupted')
        self.assertEqual(value['cumulative_seconds']['model_work'], 0)

    def test_duplicate_marks_and_spans_do_not_double_count(self):
        state = {'created_at': 1, 'phase': 'running'}
        timing.initialize(state, 1)
        timing.begin(state, 'scheduler_wait', 'q', at=2)
        timing.begin(state, 'scheduler_wait', 'q', at=3)
        timing.mark(state, 'checks_queued', 'q', at=2)
        timing.mark(state, 'checks_queued', 'q', at=3)
        timing.end(state, 'q', at=5); timing.end(state, 'q', at=7)
        value = timing.snapshot(state, 8)['stages']
        self.assertEqual(value['cumulative_seconds']['scheduler_wait'], 3)
        self.assertEqual(len(value['milestones']), 1)


class TimingOriginTests(unittest.TestCase):
    def test_project_origin_does_not_load_its_unpersisted_self(self):
        bridge = Mock()
        bridge.project_manager.side_effect = AssertionError('Unpersisted project lookup')
        project = {'project_id': 'a'*32, 'created_at': 10, 'phase': 'running'}
        timing.attach_origin(bridge, project)
        self.assertEqual(project['requirement_started_at'], 10)
        self.assertEqual(timing.requirement_elapsed(project, 25), 15)

    def test_linked_meeting_origin_and_unknown_legacy_remain_distinct(self):
        bridge = Mock()
        bridge.project_manager.return_value.load.return_value = {'created_at': 10}
        state = {'session_id':'a'*32, 'created_at':20, 'phase':'running'}
        timing.attach_origin(bridge, state, {'meeting_id':'b'*32})
        self.assertEqual(timing.requirement_elapsed(state, 30), 20)
        self.assertEqual(state['requirement_time_source'], 'meeting_created')
        self.assertIsNone(timing.requirement_elapsed({'created_at':10}, 30))


class RequestIntervalScopeTests(unittest.TestCase):
    def test_reassignment_does_not_count_prior_or_future_model_requests_twice(self):
        state = {'created_at':20,'phase':'accepted'}
        timing.initialize(state,20);timing.transition(state,None,40)
        rows = [{'id':'old','category':'model_work','started_at':10,'finished_at':15,'outcome':'completed'},
                {'id':'own','category':'model_work','started_at':25,'finished_at':30,'outcome':'completed'},
                {'id':'next','category':'model_work','started_at':45,'finished_at':50,'outcome':'completed'}]
        result = timing.snapshot(state,60,requests=rows)['stages']
        self.assertEqual(result['cumulative_seconds']['model_work'],5)
        self.assertEqual([r['id'] for r in result['intervals']],['own'])

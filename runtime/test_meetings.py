"""Meeting coordination, read-only evidence and implementation handoff."""
import contextlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
import urllib.request
from urllib.parse import urlsplit
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from test_telos import b, BridgeTests
from test_callbacks import CallbackTests, OWNER, OTHER


def opinion(questions=()):
    return json.dumps({'summary': 'Addition has a small existing implementation.',
                      'proposal': 'Preserve its public interface; verify signed inputs.',
                      'risks': ['Decide invalid-input behavior.'], 'questions': list(questions),
                      'evidence': ['calc.py defines add(a, b).']})


PLAN = {'requirements': ['Support addition of signed integers.'],
        'implementation': ['Preserve the existing add function.'],
        'workstreams': ['Verify arithmetic', 'Review input behavior'],
        'verification': ['Run existing addition regression checks.']}


class MeetingTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown

    @contextlib.contextmanager
    def no_workers(self):
        original = b._start_job
        def start(*args, **kwargs):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kwargs)
        with patch.object(b, '_start_job', side_effect=start):
            yield

    def open(self, count=2, caller=None, **kw):
        result = b.call_tool('telos_meeting_open', {'repo': str(self.repo), 'requirement': 'Verify addition requirements.',
            'participants': [{'id': 'p' + str(i), 'role': 'Independent domain ' + str(i)} for i in range(count)],
            'decision_reason': 'Different implementation and review perspectives.', 'notify': False, **kw}, caller)
        return result['project_id']

    def project(self, mid):
        return b.project_manager().load(mid)

    def child(self, mid, ident):
        t = next(t for t in self.project(mid)['tasks'] if t['id'] == ident)
        return b.read_json(b.run_dir(t['session_id']) / 'state.json')

    def finish(self, mid, ident, text=None):
        state = self.child(mid, ident)
        with self.no_workers(), patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': text or opinion()}):
            b.background_job(state['session_id'], state['job_id'])
        return self.child(mid, ident)

    def start(self, mid, caller=None):
        with self.no_workers():
            return b.call_tool('telos_project_start', {'project_id': mid}, caller)

    def conclude(self, mid, answers=None, caller=None):
        return b.call_tool('telos_meeting_conclude', {'meeting_id': mid, 'plan': PLAN,
                           'answers': answers or [], 'rationale': 'Independent reports support the plan; verify it during implementation.'}, caller)

    def test_adaptive_participants_use_three_slots_and_no_default_token_round_job_cap(self):
        mid = self.open(5)
        p = self.project(mid)
        self.assertIsNone(p['max_jobs'])
        self.start(mid)
        self.assertEqual([t['phase'] for t in self.project(mid)['tasks']], ['running'] * 3 + ['pending'] * 2)
        self.finish(mid, 'p0')
        self.assertEqual(self.child(mid, 'p3')['phase'], 'running')
        contract = b.read_json(b.run_dir(self.child(mid, 'p0')['session_id']) / 'contract.json')
        self.assertIsNone(contract['budget'])
        self.assertEqual(contract['questions'], [])
        self.assertEqual(contract['checks'], [])
        self.assertEqual(contract['task_type'], 'discussion')
        with self.assertRaises(b.GateError): self.conclude(mid)

    def test_default_mcp_requires_meeting_and_explicit_direct_exception_is_recorded(self):
        args = {'repo': str(self.repo), 'task': 'Implement addition', 'questions': self.q, 'checks': self.checks, 'notify': False}
        with self.assertRaises(b.GateError) as caught:
            b.call_tool('telos_open', args)
        self.assertEqual(caught.exception.code, 'meeting_required')
        result = b.call_tool('telos_open', {**args, 'skip_meeting_reason': 'User explicitly requested a direct trivial edit.'})
        contract = b.read_json(b.run_dir(result['session_id']) / 'contract.json')
        self.assertEqual(contract['planning']['mode'], 'direct')
        with self.assertRaises(b.GateError): self.open(1)
        self.open(1, single_executor_reason='A tiny isolated review explicitly needs one participant.')

    def test_targeted_round_retains_thread_and_retry_does_not_duplicate_work(self):
        mid = self.open(); self.start(mid)
        first = self.finish(mid, 'p0'); self.finish(mid, 'p1')
        root = b.run_dir(first['session_id'])
        state = b.read_json(root / 'state.json'); state['turns'] = 100
        b.write_json(root / 'state.json', state)
        args = {'meeting_id': mid, 'request_id': 'review-1', 'decision_reason': 'Resolve only the input-policy disagreement.',
                'messages': [{'participant_id': 'p0', 'message': 'Negative integers are supported. Verify the proposed behavior.'}]}
        with self.no_workers():
            b.call_tool('telos_meeting_round', args)
            self.assertTrue(b.call_tool('telos_meeting_round', args)['duplicate'])
        current = self.child(mid, 'p0')
        self.assertEqual(current['thread_id'], first['thread_id'])
        self.assertEqual(current['turns'], 101)
        self.assertEqual(self.project(mid)['jobs_started'], 3)
        self.assertEqual(self.child(mid, 'p1')['phase'], 'discussion_result')
        with self.assertRaises(b.GateError):
            b.call_tool('telos_meeting_round', {**args, 'decision_reason': 'Changed retry'})
        # Recover after spawn succeeded but task-queue cleanup was not saved.
        p = self.project(mid); p['tasks'][0]['discussion_pending'] = {'request_id': 'review-1', 'message': args['messages'][0]['message']}
        b.project_manager().save(p); self.start(mid)
        self.assertEqual(self.project(mid)['jobs_started'], 3)

    def test_add_retire_pause_cancel_and_invalid_round_are_atomic(self):
        mid = self.open(); self.start(mid); self.finish(mid, 'p0'); self.finish(mid, 'p1')
        before = self.project(mid)
        args = {'meeting_id': mid, 'request_id': 'experts', 'messages': [], 'decision_reason': 'Need a database specialist.',
                'add_participants': [{'id': 'database', 'role': 'Check migration and persistence.'}], 'retire_participants': ['p1']}
        with self.assertRaises(b.GateError):
            b.call_tool('telos_meeting_round', {**args, 'retire_participants': ['missing']})
        self.assertEqual(before, self.project(mid))
        with self.no_workers(): b.call_tool('telos_meeting_round', args)
        self.assertEqual(self.child(mid, 'database')['phase'], 'running')
        self.assertFalse(self.project(mid)['tasks'][1]['active'])
        b.project_manager().control(mid, 'pause')
        with self.assertRaises(b.GateError):
            b.call_tool('telos_meeting_round', {**args, 'request_id': 'paused'})
        with self.no_workers(): b.project_manager().control(mid, 'resume')
        b.project_manager().control(mid, 'cancel')
        with self.assertRaises(b.GateError): self.conclude(mid)

    def test_questions_block_conclusion_and_plan_enters_new_frozen_contract(self):
        mid = self.open(); self.start(mid)
        self.finish(mid, 'p0', opinion(['Must negative inputs be supported?'])); self.finish(mid, 'p1')
        with self.assertRaises(b.GateError): self.conclude(mid)
        view = b.project_manager().status(mid, 'summary')
        qid = view['discussion']['questions'][0]['id']
        answers = [{'question_id': qid, 'answer': 'The requirement confirms signed integers are supported.'}]
        result = self.conclude(mid, answers)
        self.assertTrue(result['discussion']['discussion_complete'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['phase'], 'discussed')
        self.assertEqual(self.conclude(mid, answers)['discussion']['conclusion'], result['discussion']['conclusion'])
        args = {'repo': str(self.repo), 'task': 'Implement approved addition plan', 'questions': self.q,
                'checks': self.checks, 'notify': False, 'meeting_id': mid}
        session = b.call_tool('telos_open', args)
        contract = b.read_json(b.run_dir(session['session_id']) / 'contract.json')
        self.assertEqual(contract['planning']['meeting_id'], mid)
        self.assertIn('Support addition of signed integers.', contract['context'])
        self.assertEqual(contract['checks'], self.checks)
        with self.assertRaises(b.GateError):
            b.start_job(self.child(mid, 'p0')['session_id'], 'evaluate')

    def test_opinions_are_not_acceptance_and_corrupted_evidence_blocks_handoff(self):
        mid = self.open(); self.start(mid); self.finish(mid, 'p0'); self.finish(mid, 'p1')
        for ident in ('p0', 'p1'):
            with self.assertRaises(b.GateError): b.start_job(self.child(mid, ident)['session_id'], 'evaluate')
        with self.assertRaises(b.GateError): b.project_manager().integrate(mid)
        self.conclude(mid)
        path = next(b.run_dir(self.child(mid, 'p0')['session_id']).glob('discussion-*.json'))
        record = b.read_json(path); record['proposal'] = 'tampered'; b.write_json(path, record)
        with self.assertRaises(b.GateError):
            b.call_tool('telos_open', {'repo': str(self.repo), 'task': 'Addition', 'questions': self.q,
                'checks': self.checks, 'notify': False, 'meeting_id': mid})

    def test_owned_meeting_callback_and_foreign_supervisor_rejection(self):
        result = b.call_tool('telos_meeting_open', {'repo': str(self.repo), 'requirement': 'Review addition',
            'participants': [{'id': 'p0', 'role': 'Design'}, {'id': 'p1', 'role': 'Review'}],
            'decision_reason': 'Independent design and review.'}, {'threadId': OWNER})
        mid = result['project_id']
        with self.no_workers():
            b.call_tool('telos_project_start', {'project_id': mid}, {'threadId': OWNER})
        self.finish(mid, 'p0'); self.finish(mid, 'p1')
        courier = b.read_json(b.run_dir(mid) / 'state.json')
        receipt = b.call_tool('telos_project_receive', {'project_id': mid, 'event_id': courier['last_event_id']}, {'threadId': OWNER})
        self.assertTrue(receipt['proceed'])
        self.assertEqual(len(receipt['project']['discussion']['reports']), 2)
        self.assertTrue(all(x['next_action'] == 'review_opinion_then_meeting_round_or_conclude' for x in receipt['events']))
        with self.assertRaises(b.GateError):
            b.call_tool('telos_meeting_conclude', {'meeting_id': mid, 'plan': PLAN, 'answers': [], 'rationale': 'reviewed'}, {'threadId': OTHER})

    def test_concluded_meeting_has_no_actionable_callbacks_or_reminders(self):
        caller = {'threadId': OWNER}
        mid = self.open(caller=caller, notify=True)
        self.start(mid, caller); self.finish(mid, 'p0'); self.finish(mid, 'p1'); self.conclude(mid, caller=caller)
        for sid in [mid] + [t['session_id'] for t in self.project(mid)['tasks']]:
            before = b.read_json(b.run_dir(sid) / 'state.json')
            b.recover_progress(sid)
            after = b.read_json(b.run_dir(sid) / 'state.json')
            self.assertTrue(after['return_complete'])
            self.assertEqual(before['events'], after['events'])
        self.assertFalse(b.project_manager().receive(mid)['proceed'])
        path = b.project_manager().path(mid).parent / 'discussion-conclusion.json'
        record = b.read_json(path); record['rationale'] = 'tampered'; b.write_json(path, record)
        with self.assertRaisesRegex(b.GateError, 'conclusion changed'): self.conclude(mid, caller=caller)
        with self.assertRaisesRegex(b.GateError, 'conclusion changed'):
            b.storage_manager().manage('archive', mid, caller=OWNER)

    def test_project_handoff_rejects_changed_baseline_and_preserves_public_plan(self):
        mid = self.open(); self.start(mid); self.finish(mid, 'p0'); self.finish(mid, 'p1'); self.conclude(mid)
        args = {'repo': str(self.repo), 'task': 'Approved addition project', 'meeting_id': mid, 'notify': False,
                'questions': self.q, 'checks': self.checks, 'decision_reason': 'One implementation unit after design review.',
                'tasks': [{'id': 'arithmetic', 'task': 'Verify addition', 'message': 'Implement the approved plan.',
                           'allowed_paths': ['calc.py'], 'questions': self.q, 'checks': self.checks}]}
        result = b.call_tool('telos_project_open', args)
        p = self.project(result['project_id'])
        self.assertEqual(p['contract']['planning']['meeting_id'], mid)
        self.assertIn('Support addition of signed integers.', p['tasks'][0]['context'])
        b.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '--allow-empty', '-qm', 'New baseline')
        with self.assertRaisesRegex(b.GateError, 'source differs'):
            b.call_tool('telos_project_open', args)

    def test_failed_followup_start_retains_diagnostic_and_can_resume_without_loss(self):
        mid = self.open(); self.start(mid); self.finish(mid, 'p0'); self.finish(mid, 'p1')
        args = {'meeting_id': mid, 'request_id': 'spawn-retry', 'decision_reason': 'Resolve ambiguity.',
                'messages': [{'participant_id': 'p0', 'message': 'Check signed inputs.'}]}
        with patch.object(b, '_start_job', side_effect=b.GateError('Fixture spawn unavailable')):
            b.call_tool('telos_meeting_round', args)
        p = b.project_manager().refresh(self.project(mid))
        self.assertEqual(p['tasks'][0]['error'], 'Fixture spawn unavailable')
        self.assertIn('discussion_pending', p['tasks'][0])
        with self.no_workers(): b.project_manager().tick(mid)
        self.assertEqual(self.child(mid, 'p0')['phase'], 'running')
        self.assertEqual(self.project(mid)['jobs_started'], 3)

    def test_queued_followup_cannot_be_overwritten_and_retirement_clears_it(self):
        mid = self.open(); self.start(mid); self.finish(mid, 'p0'); self.finish(mid, 'p1')
        args = {'meeting_id': mid, 'request_id': 'queued', 'decision_reason': 'Resolve input question.',
                'messages': [{'participant_id': 'p0', 'message': 'Verify signed behavior.'}]}
        with patch('projects.Projects.capacity', return_value=False):
            b.call_tool('telos_meeting_round', args)
            with self.assertRaises(b.GateError):
                b.call_tool('telos_meeting_round', {**args, 'request_id': 'overwrite'})
            b.call_tool('telos_meeting_round', {**args, 'request_id': 'retire', 'messages': [], 'retire_participants': ['p0']})
        self.assertNotIn('discussion_pending', self.project(mid)['tasks'][0])
        self.conclude(mid)


class NativeMeetingTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    no_workers = MeetingTests.no_workers
    child = MeetingTests.child
    project = MeetingTests.project

    @contextlib.contextmanager
    def fixture(self):
        external = os.environ.get('TELOS_MEETING_SIMULATION_URL')
        if external:
            url = urlsplit(external)
            if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.username or url.password:
                raise ValueError('Only a local simulation is allowed')
            yield external.rstrip('/')
            return
        fixture = Path(__file__).resolve().parents[1] / 'postman/mocks/meetings/default.js'
        process = subprocess.Popen(['node', str(fixture)], env={**os.environ, 'PORT': '0'},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            if not select.select([process.stdout], [], [], 10)[0]:
                self.fail('Local model fixture did not start')
            port = json.loads(process.stdout.readline())['listening']
            yield f'http://127.0.0.1:{port}'
        finally:
            process.terminate()
            try: process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()

    def test_real_parallel_cli_readonly_followup_and_accepted_implementation(self):
        import isolation
        run_id = uuid.uuid4().hex
        context = f'MEETING_FIXTURE_ID: {run_id}\nMEETING_FIXTURE_PYTHON: {sys.executable}\n'
        b.write_json(b.CONFIG, {'job_timeout': 60, 'task_budget': {'max_requests': 1}})
        mid = b.call_tool('telos_meeting_open', {'repo': str(self.repo), 'requirement': 'Verify signed addition.',
            'context': context, 'participants': [{'id': f'p{i}', 'role': f'Native perspective p{i}', 'provider': 'bailian'} for i in range(2)],
            'decision_reason': 'Independent design and verification perspectives.', 'notify': False})['project_id']
        with self.no_workers(): b.call_tool('telos_project_start', {'project_id': mid})
        original_relay = isolation.model_relay
        with self.fixture() as base_url:
            def relay(endpoint, key, timeout, **kwargs):
                return original_relay(base_url + '/compatible-mode/v1', key, timeout, **kwargs)
            with patch.object(b, 'provider_credential', return_value=('SYNTHETIC_LOCAL_ONLY', 'fixture')), \
                    patch.object(isolation, 'model_relay', side_effect=relay):
                initial = [self.child(mid, ident) for ident in ('p0', 'p1')]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(b.background_job, s['session_id'], s['job_id']) for s in initial]
                    for future in futures: future.result(timeout=90)
                returned = [self.child(mid, ident) for ident in ('p0', 'p1')]
                for state in returned:
                    self.assertEqual(state['phase'], 'discussion_result', state.get('last_error'))
                    self.assertIsNone(b.read_json(b.run_dir(state['session_id']) / 'contract.json')['budget'])
                    self.assertEqual(b.git(state['worktree'], 'status', '--porcelain'), '')
                self.assertNotEqual(returned[0]['thread_id'], returned[1]['thread_id'])
                with self.no_workers():
                    b.call_tool('telos_meeting_round', {'meeting_id': mid, 'request_id': 'native-followup',
                        'messages': [{'participant_id': 'p0', 'message': 'FOCUSED_FOLLOWUP: resolve signed-input behavior.'}],
                        'decision_reason': 'Resolve the one outstanding technical disagreement.'})
                state = self.child(mid, 'p0'); b.background_job(state['session_id'], state['job_id'])
                self.assertEqual(self.child(mid, 'p0')['phase'], 'discussion_result')
                self.assertEqual(self.child(mid, 'p0')['thread_id'], returned[0]['thread_id'])
                concluded = b.call_tool('telos_meeting_conclude', {'meeting_id': mid, 'plan': PLAN,
                    'answers': [], 'rationale': 'Source inspection and sandbox experiments support this plan.'})
                self.assertFalse(concluded['accepted'])
                sid = b.call_tool('telos_open', {'repo': str(self.repo), 'task': 'Verify approved addition behavior.',
                    'context': context, 'questions': self.q, 'checks': self.checks, 'provider': 'bailian',
                    'budget': {'max_requests': 3}, 'notify': False, 'meeting_id': mid})['session_id']
                for kind, message in [('send', 'Complete the approved addition task.'), ('evaluate', '')]:
                    with self.no_workers(): job = b.start_job(sid, kind, message)
                    b.background_job(sid, job['job_id'])
                state = b.read_json(b.run_dir(sid) / 'state.json')
                self.assertEqual(state['phase'], 'awaiting_review', state.get('last_error'))
                review = b.review_packet(sid)
                self.assertTrue(all(c['status'] == 'passed' for c in review['evidence']['checks']))
                accepted = b.grade(sid, state['packet_id'], BridgeTests.scores(self), [])
                self.assertTrue(accepted['accepted'])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(base_url + '/fixture/evidence/' + run_id, timeout=5) as response:
                evidence = json.load(response)
            self.assertTrue(evidence['parallelFirstRound'])
            self.assertEqual(len(evidence['requests']), 7)
            self.assertEqual(sum(r['followup'] for r in evidence['requests']), 1)
            self.assertTrue(evidence['requests'][-1]['toolsDisabled'])
            self.assertNotIn(self.q[0]['reference'], json.dumps(evidence))
            if os.environ.get('TELOS_MEETING_EVIDENCE'):
                b.write_json(Path(os.environ['TELOS_MEETING_EVIDENCE']), {
                    'passed': True, 'boundary': 'real CLI and macOS sandbox against local Responses fixture',
                    'parallel_first_round': True, 'independent_threads': [s['thread_id'] for s in returned],
                    'readonly_source_and_git': True, 'scratch_experiment': True, 'same_thread_followup': True,
                    'meeting_accepted': False, 'implementation_accepted': accepted['accepted'], 'http_requests': len(evidence['requests'])})


if __name__ == '__main__':
    unittest.main()

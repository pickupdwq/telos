"""Requirement questions must return to the owner before checks or exams."""
import json
import os
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from test_telos import b, BridgeTests
from test_callbacks import CallbackTests, OWNER, OTHER
from test_projects import ProjectTests


def reply(status='needs_clarification', questions=None):
    return json.dumps({'status': status, 'summary': 'Need the input policy.' if status == 'needs_clarification' else 'Addition verified.',
        'clarification_questions': (['Should negative inputs be supported?'] if status == 'needs_clarification' else [])
        if questions is None else questions,
        'changed_files': [], 'checks_performed': [], 'remaining_issues': []})


class ClarificationTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    create = BridgeTests.create
    state = CallbackTests.state

    def send(self, sid, text, event_id=None, answer='Inspect requirements.'):
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            job = b.start_job(sid, 'send', answer, event_id)
        with patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': text}) as peer:
            b.background_job(sid, job['job_id'])
        return self.state(sid), peer.call_args.args[0]

    def test_question_receipt_gate_answer_and_explicit_completion(self):
        sid = self.create(notify=True, owner_thread_id=OWNER)
        state, _ = self.send(sid, reply())
        eid = state['last_event_id']
        self.assertEqual(state['phase'], 'needs_clarification')
        self.assertIn('clarification', b.event_message(sid, state['events'][eid]))
        receipt = b.call_tool('telos_receive', {'session_id': sid, 'event_id': eid}, {'threadId': OWNER})
        self.assertTrue(receipt['proceed'])
        self.assertEqual(receipt['next_action'], 'answer_clarification_then_send')
        self.assertEqual(receipt['result']['clarification']['questions'], ['Should negative inputs be supported?'])
        with patch.object(b.subprocess, 'Popen') as spawn:
            with self.assertRaises(b.GateError) as caught:
                b.start_job(sid, 'evaluate', event_id=eid)
            self.assertEqual(caught.exception.code, 'clarification_required')
            spawn.assert_not_called()
        self.assertEqual(self.state(sid)['rounds'], 0)
        state, resumed = self.send(sid, reply('completed'), eid, 'Yes, support negative inputs. Complete and verify the work.')
        self.assertEqual(resumed['thread_id'], OTHER)
        self.assertEqual(state['phase'], 'responded')
        self.assertIsNone(state['clarification'])
        self.assertTrue((b.run_dir(sid) / f'delivery-{eid}.json').is_file())
        with patch.object(b.subprocess, 'Popen') as spawn:
            duplicate = b.start_job(sid, 'send', 'Yes, support negative inputs. Complete and verify the work.', eid)
            self.assertTrue(duplicate['duplicate'])
            spawn.assert_not_called()
        new_id = state['last_event_id']
        b.receive_event(sid, new_id)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            first = b.start_job(sid, 'evaluate', event_id=new_id)
            self.assertTrue(b.start_job(sid, 'evaluate', event_id=new_id)['duplicate'])
        self.assertEqual(first['phase'], 'running')
        self.assertEqual(self.state(sid)['rounds'], 1)

    def test_malformed_conflicting_and_missing_resolution_keep_gate_closed(self):
        for output in (reply(questions=[]), reply(questions=['']), reply(questions='question'),
                       reply('completed', ['Unresolved business choice'])):
            with self.subTest(output=output):
                sid = self.create(notify=False)
                state, _ = self.send(sid, output)
                self.assertEqual(state['phase'], 'needs_clarification')
                self.assertIsNotNone(state['clarification']['error'])
                state, _ = self.send(sid, 'Plain text cannot clear an outstanding question.')
                self.assertEqual(state['phase'], 'needs_clarification')
                with self.assertRaises(b.GateError): b.start_job(sid, 'evaluate')

    def test_followup_question_pause_resume_and_cancellation(self):
        sid = self.create(notify=True, owner_thread_id=OWNER)
        state, _ = self.send(sid, reply())
        eid = state['last_event_id']
        b.receive_event(sid, eid)
        state, _ = self.send(sid, reply(questions=['What should happen on invalid input?']), eid)
        eid = state['last_event_id']
        b.return_control(sid, 'pause')
        self.assertFalse(b.receive_event(sid, eid)['proceed'])
        with self.assertRaises(b.GateError): b.start_job(sid, 'send', 'answer', eid)
        b.return_control(sid, 'resume')
        self.assertTrue(b.receive_event(sid, eid)['proceed'])
        self.assertEqual(self.state(sid)['clarification']['questions'], ['What should happen on invalid input?'])
        b.recover_progress(sid)
        self.assertIn('progress', self.state(sid)['events'][eid])
        with patch.object(b.time, 'time', return_value=b.time.time() + b.config()['progress_timeout_seconds'] + 1):
            b.recover_progress(sid)
        reminder = self.state(sid)['events'][eid]['reminder']
        self.assertIn('clarification', b.event_message(sid, reminder))
        b.return_control(sid, 'cancel')
        with self.assertRaises(b.GateError): b.start_job(sid, 'send', 'answer', eid)


class ProjectClarificationTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child

    def test_questions_visible_in_project_and_block_only_dependent_tasks(self):
        pid = self.create(tasks=[self.task('a'), self.task('b', deps=['a']), self.task('c')])
        self.start(pid)
        child = self.child(pid, 'a'); sid = child['session_id']
        with patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': reply()}):
            b.background_job(sid, child['job_id'])
        for detail in ('summary', 'full'):
            view = b.project_manager().status(pid, detail)
            tasks = {t['id']: t for t in view['tasks']}
            self.assertEqual(tasks['a']['phase'], 'needs_clarification')
            self.assertEqual(tasks['a']['next_action'], 'answer_clarification_then_send')
            self.assertEqual(tasks['a']['clarification']['questions'], ['Should negative inputs be supported?'])
            self.assertEqual(tasks['b']['phase'], 'pending')
            self.assertEqual(tasks['c']['phase'], 'running')
        before = self.state(pid)['jobs_started']
        with self.assertRaises(b.GateError): b.start_job(sid, 'evaluate')
        self.assertEqual(self.state(pid)['jobs_started'], before)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.start_job(sid, 'send', 'Yes, support negative inputs.')
        self.assertEqual(self.state(pid)['jobs_started'], before + 1)

    def test_project_callback_receives_child_questions_with_child_action_id(self):
        pid = self.create(tasks=[self.task('a')], notify=True, caller=OWNER)
        self.start(pid)
        child = self.child(pid, 'a')
        with patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': reply()}):
            b.background_job(child['session_id'], child['job_id'])
        courier = b.read_json(b.run_dir(pid) / 'state.json')
        result = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': courier['last_event_id']},
                             {'threadId': OWNER})
        self.assertTrue(result['proceed'])
        event = next(x for x in result['events'] if x['task_id'] == 'a')
        self.assertEqual(event['event_id'], child['job_id'])
        self.assertEqual(event['next_action'], 'answer_clarification_then_send')
        self.assertEqual(event['result']['clarification']['questions'], ['Should negative inputs be supported?'])
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            sent = b.call_tool('telos_send', {'session_id': child['session_id'], 'event_id': event['event_id'],
                                            'message': 'Yes, support negative inputs.'}, {'threadId': OWNER})
        self.assertEqual(sent['phase'], 'running')


class NativeClarificationTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def test_real_cli_http_question_answer_same_thread_then_exam(self):
        import isolation
        from chat_adapter import encode
        captured = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append(req)
                text = (json.dumps({'answers': [{'id': 'q1', 'answer': '5'}, {'id': 'q2', 'answer': '3'}]})
                        if req.get('tool_choice') == 'none' else reply() if len(captured) == 1 else reply('completed'))
                response = {'id': 'resp_' + str(len(captured)), 'object': 'response', 'status': 'completed',
                    'model': req['model'], 'output': [{'type': 'message', 'id': 'msg_' + str(len(captured)),
                        'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
                    'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}
                data, content_type = encode(response, req.get('stream', False))
                self.send_response(200); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        sid = b.create_session(str(self.repo), 'Verify addition; clarify ambiguous input policy first.',
                               self.q, self.checks, provider='bailian', notify=False,
                               budget={'max_requests': 3})['session_id']
        def relay(endpoint, key, timeout, **kwargs):
            return isolation_original(f'http://127.0.0.1:{server.server_port}/compatible-mode/v1', key, timeout, **kwargs)
        isolation_original = isolation.model_relay
        def run(kind, message):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                job = b.start_job(sid, kind, message)
            b.background_job(sid, job['job_id'])
            return b.read_json(b.run_dir(sid) / 'state.json')
        try:
            with patch.object(b, 'provider_credential', return_value=('SYNTHETIC_LOCAL', 'fixture')), \
                    patch.object(isolation, 'model_relay', side_effect=relay):
                first = run('send', 'Inspect the requirement and ask if a business choice is missing.')
                self.assertEqual(first['phase'], 'needs_clarification', first.get('last_error'))
                with self.assertRaises(b.GateError): b.start_job(sid, 'evaluate')
                second = run('send', 'NEGATIVE_INPUT_POLICY: support negative inputs. Verify addition.')
                self.assertEqual(second['phase'], 'responded', second.get('last_error'))
                self.assertEqual(first['thread_id'], second['thread_id'])
                final = run('evaluate', '')
                self.assertEqual(final['phase'], 'awaiting_review', final.get('last_error'))
            self.assertEqual(len(captured), 3)
            self.assertIn('Should negative inputs be supported?', json.dumps(captured[1]['input']))
            self.assertIn('NEGATIVE_INPUT_POLICY', json.dumps(captured[1]['input']))
            self.assertIn('clarification_questions', json.dumps(captured[0]['input']))
            self.assertNotIn(self.q[0]['reference'], json.dumps(captured))
            self.assertEqual(captured[-1]['tools'], [])
            self.assertEqual(b.status(sid)['usage']['totals']['requests'], 3)
        finally:
            server.shutdown(); server.server_close(); worker.join()


if __name__ == '__main__':
    unittest.main()


def phase1_reply(status='checkpoint', **extra):
    value = {'protocol_version': 2, 'status': status, 'summary': 'Saved current work.',
             'changed_files': [], 'checks_performed': ['Inspected the current source.'],
             'remaining_issues': [], 'clarification_questions': []}
    if status == 'checkpoint':
        value.update(completed_work=['Inspected source'], remaining_work=['Implement next change'],
                     next_step='Implement the remaining change and run authorized checks.',
                     evidence_paths=['calc.py'])
    elif status == 'needs_clarification':
        value['clarification_questions'] = ['Should negative inputs be supported?']
    elif status == 'blocked_environment':
        value.update(blockers=['Declared test service is unavailable'],
                     repair_actions=['Restore the declared test service'], evidence_paths=['service-check.json'])
    value.update(extra)
    return json.dumps(value)


class CheckpointTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    create = BridgeTests.create
    state = CallbackTests.state
    scores = BridgeTests.scores

    def session(self, **kw):
        return self.create(_workflow_version=3, auto_continue_checkpoints=True,
                           notify=True, owner_thread_id=OWNER, **kw)

    def launch(self, sid):
        real = b.subprocess.Popen
        def spawn(argv, *args, **kw):
            return Mock(pid=os.getpid()) if '_job' in argv else real(argv, *args, **kw)
        with patch.object(b.subprocess, 'Popen', side_effect=spawn):
            return b.start_job(sid, 'send', 'Implement the authorized work.')['job_id']

    def finish(self, sid, output, *, change=False):
        s = self.state(sid)
        if change:
            with (Path(s['worktree']) / 'calc.py').open('a') as f:
                f.write('# substantive source fixture change\n')
        real = b.subprocess.Popen
        def spawn(argv, *args, **kw):
            return Mock(pid=os.getpid()) if '_job' in argv else real(argv, *args, **kw)
        with patch.object(b.subprocess, 'Popen', side_effect=spawn) as process, \
                patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': output}) as peer:
            b.background_job(sid, s['job_id'])
        launches = [c for c in process.call_args_list if '_job' in c.args[0]]
        return self.state(sid), launches, peer

    def test_checkpoint_same_session_then_completed_requires_original_acceptance(self):
        sid = self.session()
        first = self.launch(sid)
        before = (b.run_dir(sid) / 'contract.json').read_bytes()
        s, launches, _ = self.finish(sid, phase1_reply(), change=True)
        self.assertEqual(len(launches), 1)
        self.assertEqual(s['phase'], 'running')
        self.assertEqual(s['thread_id'], OTHER)
        self.assertNotEqual(s['job_id'], first)
        self.assertEqual(s['rounds'], 0)
        action = s['auto_continuations'][first]
        self.assertEqual(action['status'], 'started')
        self.assertEqual(action['source_job_id'], first)
        self.assertEqual(action['job_id'], s['job_id'])
        self.assertNotIn('received_at', s.get('events', {}).get(first, {}))
        s, launches, peer = self.finish(sid, phase1_reply('completed'), change=True)
        self.assertEqual(s['phase'], 'responded')
        self.assertEqual(len(launches), 0)
        self.assertEqual(peer.call_args.args[0]['thread_id'], OTHER)
        self.assertFalse(b.status(sid)['accepted'])
        self.assertEqual((b.run_dir(sid) / 'contract.json').read_bytes(), before)
        with self.assertRaises(b.GateError):
            b.start_job(sid, 'evaluate', event_id=s['last_event_id'])
        self.assertTrue(b.receive_event(sid, s['last_event_id'])['proceed'])
        real = b.subprocess.Popen
        with patch.object(b.subprocess, 'Popen', side_effect=lambda argv, **kw:
                          Mock(pid=os.getpid()) if '_job' in argv else real(argv, **kw)):
            self.assertEqual(b.start_job(sid, 'evaluate', event_id=s['last_event_id'])['phase'], 'running')

    def test_duplicate_and_concurrent_recovery_start_only_once(self):
        import concurrent.futures
        sid = self.session(); self.launch(sid)
        with patch.object(b.project_manager().__class__, 'continue_pending', return_value=None):
            s, launches, _ = self.finish(sid, phase1_reply(), change=True)
        first = s['job_id']
        real = b.subprocess.Popen
        def spawn(argv, **kw):
            return Mock(pid=os.getpid()) if '_job' in argv else real(argv, **kw)
        with patch.object(b.subprocess, 'Popen', side_effect=spawn) as process:
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                list(pool.map(lambda _: b.project_manager().continue_pending(sid), range(8)))
            b.background_job(sid, first)
        self.assertEqual(sum('_job' in c.args[0] for c in process.call_args_list), 1)
        self.assertEqual(self.state(sid)['turns'], 2)

    def test_pause_and_cancel_stop_pending_checkpoint(self):
        for control in ('pause', 'cancel'):
            with self.subTest(control=control):
                sid = self.session(); self.launch(sid)
                b.return_control(sid, control)
                s, launches, _ = self.finish(sid, phase1_reply(), change=True)
                self.assertEqual(s['phase'], 'checkpoint')
                self.assertEqual(len(launches), 0)
                b.project_manager().continue_pending(sid)
                self.assertEqual(self.state(sid)['turns'], 1)

    def test_repeated_checkpoint_text_does_not_reset_real_progress_guard(self):
        sid = self.session(); self.launch(sid)
        for i in range(3):
            s, launches, _ = self.finish(sid, phase1_reply(summary='Claimed progress ' + str(i)))
        self.assertEqual(s['phase'], 'checkpoint')
        self.assertEqual(s['progress_guard']['streams']['work']['streak'], 3)
        self.assertTrue(b.status(sid)['progress_guard']['blocked'])
        self.assertEqual(len(launches), 0)
        self.assertEqual(s['turns'], 3)

    def test_real_questions_and_environment_block_do_not_continue(self):
        for status in ('needs_clarification', 'blocked_environment'):
            with self.subTest(status=status):
                sid = self.session(); self.launch(sid)
                s, launches, _ = self.finish(sid, phase1_reply(status))
                self.assertEqual(s['phase'], status)
                self.assertEqual(len(launches), 0)
                self.assertIsNotNone(s.get('last_event_id'))
                self.assertEqual(s['turns'], 1)

    def test_protocol_error_has_one_bounded_correction_and_retains_raw_reply(self):
        sid = self.session(); self.launch(sid)
        raw = '{"protocol_version":2,"status":"completed"'
        s, launches, _ = self.finish(sid, raw)
        self.assertEqual(len(launches), 1)
        first = next(iter(s['auto_continuations']))
        receipt = b.read_json(b.run_dir(sid) / ('delivery-' + first + '.json'))
        self.assertEqual(receipt['status'], 'protocol_error')
        self.assertEqual(receipt['raw_text'], raw)
        self.assertEqual(receipt['protocol_error']['code'], 'invalid_json')
        self.assertIsNone(s.get('clarification'))
        s, launches, _ = self.finish(sid, raw)
        self.assertEqual(s['phase'], 'protocol_error')
        self.assertEqual(len(launches), 0)
        self.assertEqual(s['turns'], 2)
        self.assertEqual(s['rounds'], 0)

    def test_legacy_contract_and_completed_reply_remain_compatible(self):
        sid = self.create(notify=False)
        self.launch(sid)
        before = (b.run_dir(sid) / 'contract.json').read_bytes()
        s, launches, _ = self.finish(sid, reply('completed'))
        self.assertEqual(s['phase'], 'responded')
        self.assertEqual(len(launches), 0)
        self.assertNotIn('auto_continue_checkpoints', b.read_json(b.run_dir(sid) / 'contract.json'))
        self.assertEqual(before, (b.run_dir(sid) / 'contract.json').read_bytes())

    def test_valid_long_delivery_is_not_truncated_to_invalid_json(self):
        import delivery
        sid = self.session()
        s = self.state(sid); s.update(job_id='a' * 32, turns=1)
        text = phase1_reply('completed', summary='details ' * 3000)
        receipt = delivery.capture(b.bridge_services(), s, {'thread_id': OTHER, 'summary': text},
                                   b.read_json(b.run_dir(sid) / 'contract.json'))
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['raw_text'], text)


class ProjectCheckpointTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child

    def test_added_tasks_inherit_project_environment_and_frozen_default_policy(self):
        pid=self.create(tasks=[self.task('a')],auto_continue_checkpoints=True,
                        environment_requirements={'version':1,'local_ipc':True})
        before=self.state(pid)
        with patch.object(b.project_manager().__class__, 'tick', return_value={}):
            b.project_manager().update(pid,'Add scoped work',add_tasks=[self.task('b',deps=['a'])])
        current=self.state(pid)
        self.assertEqual(current['tasks'][0],before['tasks'][0])
        self.assertEqual(current['tasks'][1]['environment_requirements'],{'version':1,'local_ipc':True})
        self.assertTrue(current['tasks'][1]['auto_continue_checkpoints'])

    def test_project_job_budget_stops_continuation_without_reset(self):
        pid=self.create(tasks=[self.task('a')],auto_continue_checkpoints=True,max_jobs=1)
        self.start(pid); child=self.child(pid,'a')
        with patch.object(b,'peer_turn',return_value={'thread_id':OTHER,'summary':phase1_reply()}):
            b.background_job(child['session_id'],child['job_id'])
        current=self.child(pid,'a')
        self.assertEqual(current['phase'],'checkpoint')
        self.assertEqual(current['turns'],1)
        self.assertEqual(self.state(pid)['jobs_started'],1)
        self.assertEqual(current['auto_continuations'][child['job_id']]['status'],'stopped')
        self.assertIn('budget',current['last_error'])
        view=b.project_manager().status(pid,'summary')
        self.assertEqual(view['tasks'][0]['next_action'],'resume_authorized_work')

    def test_delayed_child_policy_is_frozen_before_configuration_changes(self):
        b.write_json(b.CONFIG,{'auto_continue_checkpoints':True})
        pid=self.create(tasks=[self.task('a'),self.task('b',deps=['a'])])
        b.write_json(b.CONFIG,{'auto_continue_checkpoints':False})
        self.start(pid)
        child=self.child(pid,'a')
        contract=b.read_json(b.run_dir(child['session_id'])/'contract.json')
        self.assertTrue(contract['auto_continue_checkpoints'])
        self.assertTrue(self.state(pid)['tasks'][1]['auto_continue_checkpoints'])

    def test_project_continuation_uses_same_child_and_increments_jobs_once(self):
        pid=self.create(tasks=[self.task('a')],auto_continue_checkpoints=True)
        self.start(pid); child=self.child(pid,'a')
        real=b.subprocess.Popen
        with patch.object(b.subprocess,'Popen',side_effect=lambda argv,**kw:
                          Mock(pid=os.getpid()) if '_job' in argv else real(argv,**kw)), \
             patch.object(b,'peer_turn',return_value={'thread_id':OTHER,'summary':phase1_reply()}):
            b.background_job(child['session_id'],child['job_id'])
            b.project_manager().tick(pid)
            b.project_manager().continue_pending(child['session_id'])
        current=self.child(pid,'a')
        self.assertEqual(current['session_id'],child['session_id'])
        self.assertEqual(current['thread_id'],OTHER)
        self.assertEqual(current['turns'],2)
        self.assertEqual(self.state(pid)['jobs_started'],2)


class CheckpointRecoveryTests(CheckpointTests):
    def test_pause_wins_race_before_internal_spawn(self):
        sid=self.session(); self.launch(sid)
        with patch.object(b.project_manager().__class__,'continue_pending',return_value=None):
            self.finish(sid,phase1_reply())
        original=b._start_job
        def race(*args,**kw):
            b.return_control(sid,'pause')
            return original(*args,**kw)
        with patch.object(b,'_start_job',side_effect=race), patch.object(b.subprocess,'Popen') as spawn:
            b.project_manager().continue_pending(sid)
            spawn.assert_not_called()
        self.assertEqual(self.state(sid)['turns'],1)

    def test_spawn_failure_is_durable_and_not_retried_by_recovery(self):
        sid=self.session(); self.launch(sid)
        with patch.object(b.project_manager().__class__,'continue_pending',return_value=None):
            s,_,_=self.finish(sid,phase1_reply())
        real=b.subprocess.Popen
        def spawn(argv,**kw):
            if '_job' in argv: raise OSError('fixture spawn refused')
            return real(argv,**kw)
        with patch.object(b.subprocess,'Popen',side_effect=spawn) as process:
            b.project_manager().continue_pending(sid)
            b.project_manager().continue_pending(sid)
        current=self.state(sid)
        self.assertEqual(sum('_job' in c.args[0] for c in process.call_args_list),1)
        self.assertEqual(current['turns'],1)
        self.assertEqual(current['auto_continuations'][s['job_id']]['status'],'stopped')
        self.assertIn('could not start',current['last_error'])

    def test_lost_continuation_process_is_uncertain_and_not_resent(self):
        sid=self.session(); self.launch(sid)
        state,_,_=self.finish(sid,phase1_reply())
        old_job=next(iter(state['auto_continuations']))
        with patch.object(b,'process_running',return_value=False):
            view=b.status(sid)
        self.assertEqual(view['phase'],'error')
        self.assertEqual(view['auto_continuations'][old_job]['status'],'uncertain')
        with patch.object(b.subprocess,'Popen') as spawn:
            b.project_manager().continue_pending(sid)
            spawn.assert_not_called()
        self.assertEqual(self.state(sid)['turns'],2)

    def test_forced_timeout_never_becomes_checkpoint(self):
        sid=self.session(); first=self.launch(sid)
        with patch.object(b,'peer_turn',side_effect=b.providers.ExecutionTimeout('fixture timeout')), \
             patch.object(b,'provider_credential',return_value=('', 'fixture')):
            b.background_job(sid,first)
        s=self.state(sid)
        self.assertEqual(s['phase'],'error')
        self.assertEqual(s['last_failure_kind'],'execution_timeout')
        self.assertFalse(s.get('auto_continuations'))

    def test_checkpoint_waiting_for_capacity_does_not_request_supervisor_receipt(self):
        sid=self.session(); self.launch(sid)
        with patch.object(b.project_manager().__class__,'capacity',return_value=False):
            s,_,_=self.finish(sid,phase1_reply())
        b.recover_progress(sid)
        current=self.state(sid)
        self.assertEqual(current['phase'],'checkpoint')
        self.assertFalse(current.get('last_event_id'))
        self.assertEqual(b.status(sid,'summary')['next_action'],'automatic_continuation_pending')

    def test_duplicate_worker_entry_does_not_issue_second_model_call(self):
        sid=self.session(); first=self.launch(sid)
        entered=threading.Event(); release=threading.Event()
        def peer(*args,**kw):
            entered.set(); self.assertTrue(release.wait(5))
            return {'thread_id':OTHER,'summary':phase1_reply('completed')}
        with patch.object(b,'peer_turn',side_effect=peer) as model:
            worker=threading.Thread(target=b.background_job,args=(sid,first)); worker.start()
            self.assertTrue(entered.wait(5))
            b.background_job(sid,first)
            release.set(); worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(model.call_count,1)
        self.assertEqual(self.state(sid)['phase'],'responded')

class NativeCheckpointTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    create = BridgeTests.create
    scores = BridgeTests.scores

    def test_real_cli_checkpoint_resume_completed_then_original_receipt_and_grade(self):
        from test_closed_book import WorkflowTests, message
        from test_providers import configured
        b.write_json(b.CONFIG, {**configured(), 'job_timeout':35})
        sid=self.create(_workflow_version=3,auto_continue_checkpoints=True,
                        notify=True,owner_thread_id=OWNER)
        def response(request, number):
            if request.get('tool_choice') == 'none':
                return message(json.dumps({'answers':[{'id':'q1','answer':'5'},{'id':'q2','answer':'3'}]}))
            return message(phase1_reply('checkpoint' if number==1 else 'completed'))
        real=b.subprocess.Popen
        def spawn(argv,*args,**kwargs):
            return Mock(pid=os.getpid()) if '_job' in argv else real(argv,*args,**kwargs)
        with WorkflowTests.model(self,response) as requests, patch.object(b.subprocess,'Popen',side_effect=spawn):
            first=b.start_job(sid,'send','Implement the authorized work.')
            b.background_job(sid,first['job_id'])
            current,contract=b.load_session(b.run_dir(sid))
            thread=current['thread_id']
            self.assertEqual(current['phase'],'running',current.get('last_error'))
            self.assertNotIn('received_at',current.get('events',{}).get(first['job_id'],{}))
            b.background_job(sid,current['job_id'])
            current,_=b.load_session(b.run_dir(sid))
            self.assertEqual(current['phase'],'responded',current.get('last_error'))
            self.assertEqual(current['thread_id'],thread)
            self.assertEqual(current['rounds'],0)
            self.assertEqual(len(requests),2)
            self.assertFalse(b.status(sid)['accepted'])
            eid=current['last_event_id']
            with self.assertRaises(b.GateError): b.start_job(sid,'evaluate',event_id=eid)
            self.assertTrue(b.receive_event(sid,eid)['proceed'])
            exam=b.start_job(sid,'evaluate',event_id=eid)
            b.background_job(sid,exam['job_id'])
            final,_=b.load_session(b.run_dir(sid))
            self.assertEqual(final['phase'],'awaiting_review',final.get('last_error'))
            self.assertEqual(final['thread_id'],thread)
            eid=final['last_event_id']
            self.assertTrue(b.receive_event(sid,eid)['proceed'])
            packet=b.review_packet(sid)
            self.assertTrue(b.grade(sid,packet['packet_id'],self.scores(),[],event_id=eid)['accepted'])
        self.assertEqual(len(requests),3)
        self.assertIn('checkpoint',json.dumps(requests[1]['input']))
        self.assertEqual(requests[-1]['tools'],[])

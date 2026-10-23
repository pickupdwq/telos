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

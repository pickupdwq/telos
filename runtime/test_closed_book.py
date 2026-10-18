"""Real CLI/resume, relay and grading boundaries for the two-role workflow."""
import contextlib
import copy
import json
import os
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from test_telos import b, BridgeTests
from test_providers import configured
from closed_book import ExamPolicy, validate_evidence
import isolation
import providers
import supervisor


def sse(request, item):
    response = {'id': 'resp', 'object': 'response', 'status': 'completed', 'model': request['model'],
                'output': [item], 'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}
    events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
              {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
              {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
              {'type': 'response.completed', 'response': response}]
    return ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()


def message(text):
    return {'type': 'message', 'id': 'msg', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


class WorkflowTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    @contextlib.contextmanager
    def model(self, reply):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append(request)
                data = sse(request, reply(request, len(captured)))
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        relay = isolation.model_relay
        def local_relay(endpoint, key, timeout=30, policy=None):
            return relay(f'http://127.0.0.1:{server.server_port}', key, timeout, policy=policy)
        try:
            with patch.object(b, 'provider_credential', return_value=('FIXTURE_ONLY', 'test')), \
                    patch.object(isolation, 'model_relay', side_effect=local_relay):
                yield captured
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def create(self, **kw):
        cfg = configured(); cfg['job_timeout'] = 35
        b.write_json(b.CONFIG, cfg)
        sid = b.create_session(str(self.repo), 'Remember the calculation implementation.', self.q, self.checks, **kw)['session_id']
        return b.load_session(b.run_dir(sid))

    def test_real_cli_preserves_work_history_disables_tools_and_restores_work(self):
        state, contract = self.create()
        tree = Path(state['worktree'])
        (tree / 'calc.py').write_text('def add(a, b): return a + b\n# WORK_MEMORY_742\n')
        answers = json.dumps({'answers': [{'id': q['id'], 'answer': '5' if i == 0 else '3'} for i, q in enumerate(self.q)]})
        def reply(request, n):
            if n == 1:
                name = 'shell_command' if any(t.get('name') == 'shell_command' for t in request['tools']) else 'exec_command'
                args = {'command': 'cat calc.py'} if name == 'shell_command' else {'cmd': 'cat calc.py'}
                return {'type': 'function_call', 'id': 'tool_1', 'call_id': 'call_1', 'name': name,
                        'arguments': json.dumps(args)}
            return message(answers if request.get('tool_choice') == 'none' else 'Work complete.')
        with self.model(reply) as captured:
            result = b.peer_turn(state, contract, 'Read calc.py and remember its implementation.')
            state['thread_id'] = result['thread_id']
            commit, _ = b.snapshot(state, 'closed-book-probe')
            # Source and instructions appearing only after work must not be read.
            (tree / 'AGENTS.md').write_text('NEW_SOURCE_MUST_NOT_APPEAR_991')
            exam = b.closed_book_exam(state, contract, commit)
            self.assertEqual(exam['executor_thread_id'], result['thread_id'])
            req = captured[-1]
            self.assertEqual(req['tools'], [])
            self.assertEqual(req['tool_choice'], 'none')
            self.assertEqual(req['reasoning']['effort'], 'max')
            self.assertIn('WORK_MEMORY_742', json.dumps(req['input']))
            self.assertNotIn('NEW_SOURCE_MUST_NOT_APPEAR_991', json.dumps(req))
            for secret in [self.q[0]['reference'], self.checks[0]['argv'][2]]:
                self.assertNotIn(secret, json.dumps(captured))
            self.assertEqual(exam['tool_call_count'], 0)
            self.assertEqual(exam['answers'][0]['answer'], '5')
            self.assertEqual(exam['usage']['input_tokens'], 10)
            (tree / 'AGENTS.md').unlink()
            repaired = b.peer_turn(state, contract, 'Continue work; report briefly.')
            self.assertEqual(repaired['thread_id'], result['thread_id'])
            self.assertTrue(captured[-1].get('tools'))
            self.assertIn('closed-book examination', json.dumps(captured[-1]['input']))
        self.assertEqual(len(captured), 4)

    def test_real_cli_rejects_exam_tool_before_it_can_execute(self):
        state, contract = self.create()
        marker = Path(state['worktree']) / 'exam_tool_executed'
        def reply(request, n):
            if request.get('tool_choice') == 'none':
                return {'type': 'function_call', 'id': 'forbidden', 'call_id': 'forbidden', 'name': 'shell_command',
                        'arguments': json.dumps({'command': 'touch exam_tool_executed', 'workdir': state['worktree']})}
            return message('Work complete.')
        with self.model(reply) as captured:
            state['thread_id'] = b.peer_turn(state, contract, 'Report briefly.')['thread_id']
            with self.assertRaisesRegex(b.GateError, 'tool call'):
                b.closed_book_exam(state, contract, b.git(state['worktree'], 'rev-parse', 'HEAD'))
        self.assertFalse(marker.exists())
        self.assertEqual(len(captured), 2)

    def test_format_retry_keeps_thread_and_usage_excludes_work(self):
        state, contract = self.create()
        answer = json.dumps({'answers': [{'id': q['id'], 'answer': 'Known'} for q in self.q]})
        def reply(request, n):
            return message('INVALID_JSON_FIRST_ATTEMPT' if n == 2 else answer if n >= 3 else 'Work complete.')
        with self.model(reply) as captured:
            state['thread_id'] = b.peer_turn(state, contract, 'Report briefly.')['thread_id']
            exam = b.closed_book_exam(state, contract, b.git(state['worktree'], 'rev-parse', 'HEAD'))
        self.assertEqual(len(exam['attempts']), 2)
        self.assertEqual(exam['executor_thread_id'], state['thread_id'])
        self.assertIn('INVALID_JSON_FIRST_ATTEMPT', json.dumps(captured[-1]['input']))
        self.assertEqual(exam['usage']['input_tokens'], 20)
        self.assertEqual(exam['usage']['output_tokens'], 10)
        self.assertTrue(all(r.get('tools') == [] and r.get('tool_choice') == 'none' for r in captured[1:]))

    def test_real_job_waits_for_supervisor_and_rejects_mismatched_evidence(self):
        state, contract = self.create()
        answer = json.dumps({'answers': [{'id': q['id'], 'answer': '5' if i == 0 else '3'} for i, q in enumerate(self.q)]})
        sid = state['session_id']; root = b.run_dir(sid)
        with self.model(lambda request, n: message(answer if n > 1 else 'Work complete.')):
            state.update(thread_id=b.peer_turn(state, contract, 'Report briefly.')['thread_id'], phase='responded')
            b.write_json(root / 'state.json', state)
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                jid = b.start_job(sid, 'evaluate')['job_id']
            b.background_job(sid, jid)
        state, contract = b.load_session(root)
        self.assertEqual(state['phase'], 'awaiting_review')
        self.assertFalse(b.status(sid)['accepted'])
        packet = b.review_packet(sid)['evidence']
        scores = [{'id': q['id'], 'points': q['points'], 'rationale': 'Verified against the pinned implementation.'} for q in self.q]
        for field, value in [('executor_thread_id', 'different-thread'), ('commit', 'different-commit'),
                             ('tool_call_count', 1), ('public_payload_hash', 'different-questions')]:
            wrong = copy.deepcopy(packet); wrong['exam'][field] = value
            b.write_json(root / 'attempt-1.json', wrong)
            state['packet_id'] = b.digest(wrong); b.write_json(root / 'state.json', state)
            with self.assertRaises(b.GateError): b.grade(sid, state['packet_id'], scores, [])
        b.write_json(root / 'attempt-1.json', packet)
        state['packet_id'] = b.digest(packet); b.write_json(root / 'state.json', state)
        (Path(state['worktree']) / 'calc.py').write_text('CHANGED AFTER EXAM')
        with self.assertRaisesRegex(b.GateError, 'Code changed'): b.grade(sid, state['packet_id'], scores, [])
        b.git(state['worktree'], 'checkout', '--', 'calc.py')
        self.assertTrue(b.grade(sid, state['packet_id'], scores, [])['accepted'])

    def test_failed_checks_never_start_an_exam_request(self):
        state, contract = self.create()
        root = b.run_dir(state['session_id'])
        (Path(state['worktree']) / 'calc.py').write_text('def add(a,b): return 0\n')
        state.update(phase='responded', thread_id='00000000-0000-4000-8000-000000000001')
        b.write_json(root / 'state.json', state)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            jid = b.start_job(state['session_id'], 'evaluate')['job_id']
        with patch.object(b, 'peer_turn', side_effect=AssertionError('Failed checks must not invoke a model')):
            b.background_job(state['session_id'], jid)
        state, _ = b.load_session(root)
        self.assertEqual(state['phase'], 'not_accepted')
        self.assertEqual(b.review_packet(state['session_id'])['evidence']['exam']['skipped'], 'executable_checks_failed')

    def test_relay_rejects_custom_tools_and_incomplete_streams(self):
        binding = providers.resolve_executor(configured())
        for item in [
                {'type': 'custom_tool_call', 'name': 'apply_patch', 'input': 'forbidden'},
                {'type': 'web_search_call', 'id': 'search'},
                {'type': 'mcp_call', 'id': 'mcp'}]:
            policy = ExamPolicy(binding)
            with self.assertRaises(b.GateError):
                policy.validate_response(sse({'model': binding['model']}, item), 'text/event-stream')
            self.assertEqual(policy.responses, [])
        policy = ExamPolicy(binding)
        with self.assertRaises(b.GateError):
            policy.validate_response(b'data: {"type":"response.created","response":{"output":[]}}\n\n', 'text/event-stream')

    def test_single_frozen_executor_and_public_schema(self):
        state, contract = self.create()
        self.assertEqual(contract['workflow_version'], 3)
        self.assertEqual(contract['executor']['provider'], 'deepseek')
        self.assertNotIn('backends', contract)
        self.assertNotIn('exam_provider', contract)
        cfg = configured(); cfg['profiles']['deepseek_official']['model'] = 'deepseek-v4-pro'
        b.write_json(b.CONFIG, cfg)
        self.assertEqual(b.backend_bindings(contract)['executor']['model'], 'deepseek-flash')
        for schema in [b.TOOLS[0]['inputSchema'], b.PROJECT_TASK,
                       next(x for x in b.TOOLS if x['name'] == 'telos_project_reassign')['inputSchema']]:
            self.assertIn('executor_profile', schema['properties'])
            self.assertFalse(any(x.startswith('exam_') for x in schema['properties']))
        with self.assertRaises(b.GateError):
            b.create_session(str(self.repo), 'Task', self.q, self.checks, exam_provider='ark')
        with self.assertRaises(b.GateError): b.answer_exam(contract)
        with self.assertRaises(b.GateError): b.closed_book_exam(state, contract, 'any')

    def test_custom_responses_provider_is_frozen_and_host_bound(self):
        cfg = configured()
        cfg['providers'] = {'other_vendor': {'base_url': 'https://models.example.test/v1', 'env': 'OTHER_MODEL_KEY',
                            'default_model': 'vendor-flagship', 'models': ['vendor-flagship']}}
        binding = providers.resolve_executor(cfg, provider='other_vendor')
        self.assertEqual(binding['reasoning_effort'], 'max')
        cfg['providers']['other_vendor']['base_url'] = 'https://changed.example.test/v1'
        self.assertEqual(providers.validate_binding(binding)['base_url'], 'https://models.example.test/v1')
        with self.assertRaises(b.GateError):
            providers.credential(binding, self.base, {'DEEPSEEK_API_KEY': 'WRONG_PROVIDER'})
        self.assertEqual(providers.credential(binding, self.base, {'OTHER_MODEL_KEY': 'RIGHT_PROVIDER'})[0], 'RIGHT_PROVIDER')
        binding['provider_spec']['base_url'] = 'http://unsafe.example.test'
        with self.assertRaises(b.GateError): providers.validate_binding(binding)

    def test_supervisor_policy_rejects_executor_models_and_unapproved_gpt(self):
        cfg = {'supervisor': {'provider': 'openai', 'models': ['gpt-6-astra']}}
        supervisor.verify_thread({'modelProvider': 'openai', 'model': 'gpt-6-astra'}, cfg)
        for provider, model in [('deepseek', 'deepseek-flash'), ('openai', 'gpt-mini'), ('custom', 'gpt-6-astra')]:
            with self.assertRaises(b.GateError):
                supervisor.verify_thread({'modelProvider': provider, 'model': model}, cfg)


if __name__ == '__main__': unittest.main()

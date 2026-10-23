"""Provider migration tests at config, credential, project and actual CLI boundaries."""
import copy
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from types import SimpleNamespace
from test_telos import b, BridgeTests
from test_projects import ProjectTests
import providers as p
from isolation import model_relay


def configured():
    return {**b.DEFAULT, 'http_timeout': 300, 'exam_max_tokens': 32768,
            'default_executor_profile': 'deepseek_official', 'default_exam_profile': 'ark_coding_plan',
            'profiles': {'deepseek_official': {'provider': 'deepseek', 'model': 'deepseek-flash', 'reasoning_effort': 'max'},
                         'ark_coding_plan': {'provider': 'ark', 'model': 'deepseek-v4-1-flash-260910', 'reasoning_effort': 'max'}}}


class BindingTests(unittest.TestCase):
    def test_bailian_snapshot_profile_and_frozen_destination(self):
        cfg = configured()
        cfg['profiles']['bailian_deepseek'] = {'provider': 'bailian', 'model': 'deepseek-v4.1-flash-1017', 'reasoning_effort': 'max'}
        binding = p.resolve_executor(cfg, executor_profile='bailian_deepseek')
        self.assertEqual(binding['provider'], 'bailian')
        self.assertEqual(binding['model'], 'deepseek-v4.1-flash-1017')
        self.assertEqual(binding['base_url'], 'https://dashscope.aliyuncs.com/compatible-mode/v1')
        self.assertEqual(binding['wire_api'], 'responses')
        self.assertEqual(binding['exam_wire_api'], 'responses')
        self.assertEqual(binding['credential_ref'], {'env': 'DASHSCOPE_API_KEY', 'opencode_provider': None})
        self.assertEqual(binding['reasoning_effort'], 'max')
        self.assertEqual(p.resolve_executor(cfg)['provider'], 'deepseek')
        for effort in p.EFFORTS:
            self.assertEqual(p.resolve_executor(cfg, provider='bailian', reasoning_effort=effort)['reasoning_effort'], effort)
        cfg['profiles']['bailian_deepseek']['reasoning_effort'] = 'low'
        self.assertEqual(p.contract_bindings({'workflow_version': 3, 'executor': binding}, cfg)['executor'], binding)
        with self.assertRaises(p.ProviderError):
            p.validate_binding({**binding, 'base_url': 'https://elsewhere.example/v1'})
        with self.assertRaises(p.ProviderError):
            p.resolve_executor(cfg, provider='bailian', model='deepseek-flash')

    def test_default_roles_max_and_explicit_legacy_arguments(self):
        pair = p.resolve_pair(configured())
        self.assertEqual(pair['executor']['provider'], 'deepseek')
        self.assertEqual(pair['exam']['provider'], 'ark')
        self.assertTrue(all(x['reasoning_effort'] == 'max' for x in pair.values()))
        self.assertEqual(pair['exam']['exam_max_tokens'], 32768)
        pair = p.resolve_pair(configured(), provider='ark', model='glm-5.3', exam_provider='deepseek')
        self.assertEqual(pair['executor']['model'], 'glm-5.3')
        self.assertEqual(pair['exam']['model'], 'deepseek-flash')
        pair = p.resolve_pair(configured(), exam_profile='inherit_executor')
        self.assertEqual(pair['executor'], pair['exam'])

    def test_legacy_contract_ignores_new_provider_defaults(self):
        legacy = {'model': 'glm-5.3'}
        before = b.digest(legacy)
        pair = p.contract_bindings(legacy, configured())
        self.assertEqual(pair['executor']['provider'], 'ark')
        self.assertEqual(pair['executor']['reasoning_effort'], 'medium')
        self.assertIsNone(pair['exam']['reasoning_effort'])
        self.assertEqual(b.digest(legacy), before)

    def test_new_bindings_do_not_change_with_live_config(self):
        pair = p.resolve_pair(configured())
        cfg = configured(); cfg['profiles']['deepseek_official']['model'] = 'deepseek-v4-pro'
        cfg.update(http_timeout=1, base_url='https://wrong.example', exam_max_tokens=1)
        actual = p.contract_bindings({'backends': pair}, cfg)
        self.assertEqual(actual, pair)
        self.assertEqual(actual['executor']['http_timeout'], 300)

    def test_conflicts_and_credential_destinations_are_rejected(self):
        with self.assertRaises(b.GateError):
            p.resolve_pair(configured(), executor_profile='deepseek_official', provider='ark')
        with self.assertRaises(b.GateError):
            p.resolve_pair(configured(), executor_profile='missing')
        for field, value in [('base_url', 'https://evil.example'), ('credential_ref', {'env': 'ARK_API_KEY', 'opencode_provider': None})]:
            pair = p.resolve_pair(configured()); pair['executor'][field] = value
            with self.assertRaises(b.GateError): p.contract_bindings({'backends': pair}, configured())
        cfg = configured(); cfg['profiles']['deepseek_official']['api_key'] = 'SHOULD_NOT_BE_IN_CONFIG'
        with self.assertRaises(b.GateError): p.resolve_pair(cfg)

    def test_mcp_schemas_expose_profiles_and_max_consistently(self):
        task = b.PROJECT_TASK
        for schema in [task, b.TOOLS[0]['inputSchema'], next(x for x in b.TOOLS if x['name'] == 'telos_project_reassign')['inputSchema']]:
            self.assertEqual(schema['properties']['provider']['type'], 'string')
            self.assertIn('executor_profile', schema['properties'])
            self.assertIn('max', schema['properties']['reasoning_effort']['enum'])
            self.assertNotIn('exam_profile', schema['properties'])


class CredentialAndExamTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def test_bailian_private_credentials_do_not_reuse_other_providers(self):
        binding = p.resolve_binding({}, provider='bailian')
        with self.assertRaises(p.ProviderError):
            p.credential(binding, self.base, {'DEEPSEEK_API_KEY': 'OTHER_PROVIDER'})
        with patch.object(b, 'HOME', self.base), patch.object(sys.stdin, 'isatty', return_value=True), \
                patch('getpass.getpass', return_value='BAILIAN_FIXTURE_ONLY'):
            result = b.configure_credential('bailian')
        path = Path(result['path'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('BAILIAN_FIXTURE_ONLY', json.dumps(result))
        self.assertEqual(p.credential(binding, self.base, {})[0], 'BAILIAN_FIXTURE_ONLY')
        b.write_json(path, {'bailian': {'base_url': 'https://wrong.example/v1', 'api_key': 'WRONG_HOST'}})
        with self.assertRaises(p.ProviderError):
            p.credential(binding, self.base, {})


    def test_credentials_never_cross_provider_hosts(self):
        binding = p.resolve_pair(configured())['executor']
        path = self.base / '.config/opencode/opencode.json'; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'provider': {'deepseek': {'options': {'baseURL': p.PROVIDERS['ark']['base_url'], 'apiKey': 'WRONG_HOST_SECRET'}}}}))
        with self.assertRaises(b.GateError): p.credential(binding, self.base, {'ARK_API_KEY': 'WRONG_ENV_SECRET'})
        path.write_text(json.dumps({'provider': {'deepseek': {'options': {'baseURL': binding['base_url'], 'apiKey': '{env:LOCAL_DEEPSEEK_KEY}'}}}}))
        key, _ = p.credential(binding, self.base, {'LOCAL_DEEPSEEK_KEY': 'CORRECT_KEY'})
        self.assertEqual(key, 'CORRECT_KEY')
        self.assertEqual(p.credential(binding, self.base, {'DEEPSEEK_API_KEY': 'ROTATED_KEY'})[0], 'ROTATED_KEY')

    def test_local_auth_is_private_and_readable_without_server_restart(self):
        with patch.object(b, 'HOME', self.base), patch.object(sys.stdin, 'isatty', return_value=True), patch('getpass.getpass', return_value='LOCAL_FIXTURE_KEY'):
            result = b.configure_credential('deepseek')
        path = Path(result['path'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('LOCAL_FIXTURE_KEY', json.dumps(result))
        binding = p.resolve_pair(configured())['executor']
        self.assertEqual(p.credential(binding, self.base, {})[0], 'LOCAL_FIXTURE_KEY')

    def test_opencode_auth_store_requires_matching_configured_host(self):
        binding = p.resolve_pair(configured())['executor']
        auth = self.base / '.local/share/opencode/auth.json'; auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({'deepseek': {'type': 'api', 'key': 'AUTH_STORE_FIXTURE'}}))
        with self.assertRaises(b.GateError): p.credential(binding, self.base, {})
        config = self.base / '.config/opencode/opencode.json'; config.parent.mkdir(parents=True)
        for url in ('https://wrong.example', binding['base_url']):
            config.write_text(json.dumps({'provider': {'deepseek': {'options': {'baseURL': url}}}}))
            if url == binding['base_url']:
                self.assertEqual(p.credential(binding, self.base, {})[0], 'AUTH_STORE_FIXTURE')
            else:
                with self.assertRaises(b.GateError): p.credential(binding, self.base, {})

    def test_deepseek_exam_max_json_and_private_separation(self):
        binding = p.resolve_pair(configured(), exam_profile='inherit_executor')
        contract = {'model': 'deepseek-flash', 'task': 'Implement addition', 'context': '',
                    'questions': self.q, 'checks': self.checks, 'backends': binding}
        captured = []
        def respond(request, timeout):
            captured.append((request.full_url, json.loads(request.data), timeout))
            return io.BytesIO(json.dumps({'model': 'deepseek-flash', 'usage': {'prompt_tokens': 9, 'completion_tokens': 4},
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': [{'id': q['id'], 'answer': 'Reviewed'} for q in self.q]})}}]}).encode())
        with patch.object(b, 'model_request', side_effect=respond), patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'FIXTURE_KEY'}):
            result = b.answer_exam(contract)
        url, data, timeout = captured[0]
        self.assertEqual(url, 'https://api.deepseek.com/chat/completions')
        self.assertEqual(data['reasoning_effort'], 'max')
        self.assertEqual(data['thinking'], {'type': 'enabled'})
        self.assertEqual(data['response_format'], {'type': 'json_object'})
        self.assertEqual(timeout, 300)
        self.assertEqual(data['max_tokens'], 32768)
        self.assertEqual(result['backend']['provider'], 'deepseek')
        self.assertNotIn(self.q[0]['reference'], json.dumps(data))
        self.assertNotIn(self.checks[0]['argv'][2], json.dumps(data))
        self.assertNotIn('tools', data)

    def test_provider_errors_keep_infrastructure_classification(self):
        binding = p.resolve_pair(configured())['executor']
        cfg = {**binding, 'exam_provider': 'deepseek'}
        for status in (401, 429, 503):
            error = urllib.error.HTTPError(binding['base_url'], status, 'FIXTURE_KEY', {}, io.BytesIO(b'{"error":{"code":"busy"}}'))
            expected = b.InfrastructureError if status in (429, 503) else b.GateError
            with patch.object(b, 'model_request', side_effect=error), self.assertRaises(expected) as caught:
                b.request_exam({'model': binding['model']}, 'FIXTURE_KEY', cfg)
            self.assertNotIn('FIXTURE_KEY', str(caught.exception))

    def test_exam_redirect_does_not_forward_authorization(self):
        seen = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(302); self.send_header('Location', '/stolen'); self.end_headers()
            def do_GET(self):
                seen.append(self.headers.get('Authorization')); self.send_response(200); self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/responses', data=b'{}', headers={'Authorization': 'Bearer SYNTHETIC_ONLY'})
            with self.assertRaises(urllib.error.HTTPError): b.model_request(request, timeout=3)
            self.assertFalse(seen)
        finally:
            server.shutdown(); server.server_close(); thread.join()


class FrozenProjectTests(ProjectTests):
    def setUp(self):
        super().setUp()
        b.write_json(b.CONFIG, configured())

    def test_queued_tasks_keep_profiles_when_defaults_change(self):
        pid = self.create([self.task('a'), self.task('b', ['a'])])
        original = self.state(pid)['tasks'][1]['executor']
        cfg = configured(); cfg['profiles']['deepseek_official']['model'] = 'deepseek-v4-pro'
        cfg['profiles']['ark_coding_plan']['model'] = 'glm-5.3'; b.write_json(b.CONFIG, cfg)
        self.start(pid); self.submit(pid, 'a'); self.start(pid)
        child = self.child(pid, 'b')
        self.assertEqual(b.read_json(b.run_dir(child['session_id']) / 'contract.json')['executor'], original)
        self.assertEqual(child['model'], 'deepseek-flash')

    def test_reassign_switches_executor_only_with_fresh_thread(self):
        pid = self.create([self.task('a')]); self.start(pid)
        old = self.child(pid, 'a'); old['phase'] = 'error'; b.write_json(b.run_dir(old['session_id']) / 'state.json', old)
        old_contract = b.read_json(b.run_dir(old['session_id']) / 'contract.json')
        with patch.object(b, '_start_job', return_value={}):
            b.project_manager().reassign(pid, 'a', 'Use Coding Plan for retry', executor_profile='ark_coding_plan')
        new = self.child(pid, 'a'); contract = b.read_json(b.run_dir(new['session_id']) / 'contract.json')
        self.assertNotEqual(new['session_id'], old['session_id'])
        self.assertIsNone(new['thread_id'])
        self.assertEqual(contract['provider'], 'ark')
        self.assertEqual(contract['executor']['provider'], 'ark')
        self.assertNotIn('backends', contract)
        self.assertEqual(contract['questions'], old_contract['questions'])
        self.assertTrue(Path(old['worktree']).exists())
        self.assertEqual(b.read_json(b.run_dir(old['session_id']) / 'contract.json'), old_contract)

    def test_provider_only_reassign_uses_destination_model_default(self):
        pid = self.create([self.task('a', executor_profile='ark_coding_plan')])
        with patch.object(b, '_start_job', return_value={}):
            b.project_manager().reassign(pid, 'a', 'Use official DeepSeek', provider='deepseek')
        self.assertEqual(self.child(pid, 'a')['model'], 'deepseek-flash')

    def test_legacy_session_resume_does_not_rewrite_contract(self):
        sid = self.create_legacy_session()
        root = b.run_dir(sid); before = (root / 'contract.json').read_bytes()
        status = b.status(sid)
        self.assertEqual(status['backends']['executor']['provider'], 'ark')
        self.assertEqual(status['backends']['executor']['reasoning_effort'], 'medium')
        self.assertEqual((root / 'contract.json').read_bytes(), before)

    def create_legacy_session(self):
        sid = b.create_session(str(self.repo), 'Add', self.q, self.checks, provider='ark', _workflow_version=2)['session_id']
        root = b.run_dir(sid); state, contract = b.load_session(root)
        contract.pop('backends'); state['contract_hash'] = b.digest(contract)
        b.write_json(root / 'contract.json', contract); b.write_json(root / 'state.json', state)
        return sid


class BailianRelayTests(unittest.TestCase):
    def setUp(self):
        self.binding = p.resolve_binding({}, provider='bailian')
        self.captured = []
        captured = self.captured
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                data = b'{"status":"completed","usage":{"input_tokens":100,"output_tokens":20,"total_tokens":120}}'
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True); self.worker.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.call = {'type': 'function_call', 'call_id': 'a', 'name': 'shell_command', 'arguments': '{"command":"pwd"}'}
        self.result = {'type': 'function_call_output', 'call_id': 'a', 'output': 'fixture'}
        self.payload = {'model': self.binding['model'], 'parallel_tool_calls': True,
                        'input': [self.call, self.result], 'reasoning': {'effort': 'max'}}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join()

    def send(self, url, token, payload):
        request = urllib.request.Request(url + '/responses', data=json.dumps(payload).encode(),
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            return json.loads(response.read())

    def test_parallel_history_is_paired_without_losing_items_or_usage(self):
        from usage import UsageLedger
        call2 = {'type': 'custom_tool_call', 'call_id': 'b', 'name': 'apply_patch', 'input': 'synthetic patch'}
        result2 = {'type': 'custom_tool_call_output', 'call_id': 'b', 'output': 'patched'}
        before = {'type': 'reasoning', 'id': 'rs1', 'summary': []}
        after = {'role': 'user', 'content': 'continue'}
        payload = {**self.payload, 'input': [before, self.call, call2, result2, self.result, after]}
        with tempfile.TemporaryDirectory() as folder:
            ledger = UsageLedger(Path(folder) / 'usage.json', {'max_requests': 2, 'max_total_tokens': 1000})
            with model_relay(self.endpoint, '', binding=self.binding, usage_ledger=ledger) as (url, token, _):
                self.send(url, token, payload)
                from closed_book import ExamPolicy, fingerprint
                policy = ExamPolicy(self.binding)
            with model_relay(self.endpoint, '', binding=self.binding, policy=policy, usage_ledger=ledger) as (url, token, _):
                # This upstream fixture omits exam output; policy rejects its reply,
                # but the prepared request must still fingerprint the actual wire input.
                with self.assertRaises(urllib.error.HTTPError): self.send(url, token, payload)
            self.assertEqual(ledger.snapshot()['totals']['total_tokens'], 240)
        expected = [before, self.call, self.result, call2, result2, after]
        self.assertEqual(self.captured[0]['input'], expected)
        self.assertIs(self.captured[0]['parallel_tool_calls'], False)
        self.assertEqual(self.captured[0]['reasoning'], payload['reasoning'])
        self.assertEqual(policy.requests[0]['input_hash'], fingerprint(self.captured[1]['input']))
        self.assertEqual(self.captured[1]['tools'], [])
        self.assertEqual(self.captured[1]['tool_choice'], 'none')
        self.assertEqual(payload['input'], [before, self.call, call2, result2, self.result, after])

    def test_ambiguous_history_and_model_changes_never_reach_upstream(self):
        from usage import UsageLedger
        histories = [[self.result], [self.call], [self.call, self.call, self.result],
                     [self.call, self.result, self.result],
                     [self.call, {**self.result, 'call_id': 'other'}],
                     [self.call, {**self.result, 'type': 'custom_tool_call_output'}],
                     [self.call, {'role': 'user', 'content': 'interruption'}, self.result],
                     [{**self.call, 'call_id': ''}, self.result]]
        with tempfile.TemporaryDirectory() as folder:
            ledger = UsageLedger(Path(folder) / 'usage.json', {'max_requests': 1})
            with model_relay(self.endpoint, '', binding=self.binding, usage_ledger=ledger) as (url, token, _):
                for history in histories:
                    with self.subTest(history=history), self.assertRaises(urllib.error.HTTPError):
                        self.send(url, token, {**self.payload, 'input': history})
                with self.assertRaises(urllib.error.HTTPError):
                    self.send(url, token, {**self.payload, 'model': 'other-model'})
            self.assertEqual(ledger.snapshot()['totals']['requests'], 0)
        self.assertEqual(self.captured, [])

    def test_work_relay_decodes_and_encodes_history_once_with_fresh_budget(self):
        from usage import UsageLedger
        from isolation import WORK_BUDGET_PREFIX
        counts = {'loads': 0, 'dumps': 0}
        def loads(raw, *args, **kwargs):
            value = json.loads(raw, *args, **kwargs)
            if isinstance(value, dict) and 'input' in value:
                counts['loads'] += 1
            return value
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and 'input' in value:
                counts['dumps'] += 1
            return json.dumps(value, *args, **kwargs)
        payload = {**self.payload, 'input': [
            {'role': 'user', 'content': 'history ' * 131072}, self.call, self.result]}
        with tempfile.TemporaryDirectory() as folder:
            ledger = UsageLedger(Path(folder) / 'usage.json', {'max_requests': 3})
            with patch('isolation.json', SimpleNamespace(loads=loads, dumps=dumps)):
                with model_relay(self.endpoint, '', binding=self.binding, usage_ledger=ledger,
                                 usage_context={'phase': 'work'}) as (url, token, _):
                    self.send(url, token, payload)
            self.assertEqual(ledger.snapshot()['totals']['requests'], 1)
        self.assertEqual(counts, {'loads': 1, 'dumps': 1})
        forwarded = self.captured[0]
        self.assertEqual(forwarded['input'][:-1], payload['input'])
        advisory = forwarded['input'][-1]
        self.assertEqual(advisory['role'], 'developer')
        budget = json.loads(advisory['content'].removeprefix(WORK_BUDGET_PREFIX))
        self.assertEqual(budget['further_work_requests_remaining'], 2)
        self.assertIs(forwarded['parallel_tool_calls'], False)
        self.assertEqual(forwarded['reasoning'], payload['reasoning'])

    def test_other_cloud_history_is_not_rewritten(self):
        with model_relay(self.endpoint, '', binding={'provider': 'ark'}) as (url, token, _):
            self.send(url, token, self.payload)
        self.assertEqual(self.captured, [self.payload])


class ExecutorIntegrationTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def test_real_codex_bailian_tools_and_same_thread_exam(self):
        import isolation
        from chat_adapter import encode
        captured = []
        def call(name, args, number):
            return {'type': 'function_call', 'id': f'fc_{number}', 'call_id': f'call_{number}',
                    'name': name, 'arguments': json.dumps(args)}
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append((self.path, self.headers.get('Authorization'), req))
                history = req.get('input', [])
                for index, item in enumerate(history):
                    if item.get('type') in ('function_call', 'custom_tool_call') and (
                            index + 1 == len(history) or history[index + 1].get('type') != item['type'] + '_output'
                            or history[index + 1].get('call_id') != item['call_id']):
                        self.send_error(400, 'Bailian requires adjacent tool results'); return
                number = len(captured)
                if number == 1:
                    item = call('shell_command', {'command': 'cat calc.py'}, number)
                elif number == 2:
                    item = {'type': 'custom_tool_call', 'id': 'ctc_patch', 'call_id': 'call_patch', 'name': 'apply_patch',
                            'input': '*** Begin Patch\n*** Update File: calc.py\n@@\n def add(a, b): return a + b\n+# BAILIAN_MEMORY_742\n*** End Patch'}
                elif number == 3:
                    code = "import os; from calc import add; assert add(-2,5)==3; assert not os.environ.get('DASHSCOPE_API_KEY'); assert not os.environ.get('TELOS_RELAY_TOKEN'); print('Bailian tool check passed')"
                    item = call('shell_command', {'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)}, number)
                else:
                    text = json.dumps({'answers': [{'id': 'q1', 'answer': '5'}, {'id': 'q2', 'answer': '3'}]}) if req.get('tool_choice') == 'none' else 'Work complete.'
                    item = {'type': 'message', 'id': f'msg_{number}', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
                output = [item]
                if number == 3:
                    # Exercise a historical parallel batch even if the server ignores
                    # parallel_tool_calls=false (or the CLI resumes older history).
                    output.append(call('shell_command', {'command': 'git diff -- calc.py'}, 'parallel'))
                response = {'id': f'resp_{number}', 'object': 'response', 'status': 'completed', 'model': req['model'],
                            'output': output, 'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                                                      'output_tokens_details': {'reasoning_tokens': 12}}}
                data, content_type = encode(response, req.get('stream', False))
                self.send_response(200); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        b.write_json(b.CONFIG, {**configured(), 'job_timeout': 45})
        sid = b.create_session(str(self.repo), 'Inspect calc.py, add the assigned comment and verify addition.',
                               self.q, self.checks, provider='bailian', budget={'max_requests': 8})['session_id']
        state, contract = b.load_session(b.run_dir(sid))
        def relay(endpoint, key, timeout, **kwargs):
            self.assertEqual(endpoint, p.PROVIDERS['bailian']['base_url'])
            return model_relay(f'http://127.0.0.1:{server.server_port}/compatible-mode/v1', key, timeout, **kwargs)
        try:
            with patch.object(b, 'provider_credential', return_value=('SYNTHETIC_BAILIAN', 'test')), \
                    patch.object(isolation, 'model_relay', side_effect=relay), \
                    patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'PARENT_ONLY_SECRET'}):
                result = b.peer_turn(state, contract, 'Perform the task using shell_command and apply_patch.')
                state['thread_id'] = result['thread_id']
                commit, _ = b.snapshot(state, 'bailian-probe')
                exam = b.closed_book_exam(state, contract, commit)
            self.assertEqual(exam['executor_thread_id'], result['thread_id'])
            self.assertEqual(exam['tool_call_count'], 0)
            self.assertEqual(exam['answers'][0]['answer'], '5')
            self.assertIn('BAILIAN_MEMORY_742', (Path(state['worktree']) / 'calc.py').read_text())
            self.assertEqual(len(captured), 5)
            for path, auth, req in captured:
                self.assertEqual(path, '/compatible-mode/v1/responses')
                self.assertEqual(auth, 'Bearer SYNTHETIC_BAILIAN')
                self.assertEqual(req['model'], 'deepseek-v4.1-flash-1017')
                self.assertEqual(req['reasoning']['effort'], 'max')
                self.assertFalse(req.get('store', False))
                if req.get('tool_choice') != 'none':
                    self.assertIs(req['parallel_tool_calls'], False)
                self.assertTrue(all(t['type'] in ('function', 'custom') for t in req.get('tools', [])))
                self.assertNotIn('PARENT_ONLY_SECRET', json.dumps(req))
                self.assertNotIn(self.q[0]['reference'], json.dumps(req))
            self.assertEqual(captured[-1][2]['tools'], [])
            self.assertEqual(captured[-1][2]['tool_choice'], 'none')
            self.assertIn('Bailian tool check passed', json.dumps(captured[-1][2]['input']))
            usage = b.usage_snapshot(state, contract)
            self.assertEqual(usage['totals']['requests'], 5)
            self.assertEqual(usage['totals']['failed_requests'], 0)
            self.assertEqual(usage['totals']['total_tokens'], 600)
            self.assertEqual(usage['phases']['exam']['requests'], 1)
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_real_codex_ark_max_and_legacy_provider_id(self):
        captured = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append(req)
                item = {'type': 'message', 'id': 'msg', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'Ark probe completed.', 'annotations': []}]}
                response = {'id': 'resp', 'object': 'response', 'status': 'completed', 'model': req['model'],
                            'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                events = [{'type': 'response.created', 'response': {**response, 'output': [], 'status': 'in_progress'}},
                          {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                          {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                          {'type': 'response.completed', 'response': response}]
                data = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        cfg = configured(); cfg['job_timeout'] = 30; b.write_json(b.CONFIG, cfg)
        try:
            for legacy in (False, True):
                with self.subTest(legacy=legacy):
                    sid = b.create_session(str(self.repo), 'Probe', self.q, self.checks, provider='ark', _workflow_version=2)['session_id']
                    state, contract = b.load_session(b.run_dir(sid))
                    if legacy:
                        contract.pop('backends')
                    with model_relay(f'http://127.0.0.1:{server.server_port}', 'SYNTHETIC_ARK') as (url, token, port):
                        result = b.isolated_peer_turn(state, contract, 'Respond briefly.', url, token, port)
                    self.assertTrue(result['thread_id'])
                    req = captured[-1]
                    self.assertEqual(req['model'], 'deepseek-v4-1-flash-260910')
                    if not legacy:
                        self.assertEqual(req.get('reasoning', {}).get('effort'), 'max')
                    rollouts = list((b.CACHE / 'homes' / sid / 'sessions').rglob('*.jsonl'))
                    metadata = json.loads(rollouts[0].read_text().splitlines()[0])['payload']
                    self.assertEqual(metadata['model_provider'], 'ark_exam_loop' if legacy else 'exam_loop_ark')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_real_codex_deepseek_max_shell_patch_and_resume(self):
        captured = []
        code = ("import os\nfrom pathlib import Path\n"
                "assert not any(os.environ.get(k) for k in ['DEEPSEEK_API_KEY','ARK_API_KEY','TELOS_RELAY_TOKEN'])\n"
                "assert Path('patch_probe.txt').read_text() == 'PATCH_OK\\n'\n"
                "Path('shell_probe.txt').write_text('SHELL_OK')\nprint('SHELL_OK')")
        command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append((self.path, self.headers.get('Authorization'), req))
                tools = {t.get('name'): t for t in req.get('tools', [])}
                history = req.get('input', [])
                patched = any(x.get('type') == 'custom_tool_call_output' for x in history)
                shelled = any(x.get('type') == 'function_call_output' for x in history)
                if not patched:
                    item = {'type': 'custom_tool_call', 'id': 'ctc_probe', 'call_id': 'call_patch', 'name': 'apply_patch',
                            'input': '*** Begin Patch\n*** Add File: patch_probe.txt\n+PATCH_OK\n*** End Patch'}
                elif not shelled:
                    name = 'shell_command' if 'shell_command' in tools else 'exec_command'
                    args = {'command': command, 'timeout_ms': 10000} if name == 'shell_command' else {'cmd': command}
                    item = {'type': 'function_call', 'id': 'fc_probe', 'call_id': 'call_shell', 'name': name, 'arguments': json.dumps(args)}
                else:
                    item = {'type': 'message', 'id': 'msg_probe', 'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': 'Probe complete.', 'annotations': []}]}
                response = {'id': 'resp_probe', 'object': 'response', 'status': 'completed', 'model': 'deepseek-flash',
                            'output': [item], 'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}
                events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                          {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                          {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                          {'type': 'response.completed', 'response': response}]
                data = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        cfg = configured(); cfg['job_timeout'] = 45
        b.write_json(b.CONFIG, cfg)
        try:
            sid = b.create_session(str(self.repo), 'Local integration probe', self.q, self.checks)['session_id']
            state, contract = b.load_session(b.run_dir(sid))
            with model_relay(f'http://127.0.0.1:{server.server_port}', 'SYNTHETIC_UPSTREAM') as (url, token, port):
                with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'SYNTHETIC_PARENT_ONLY'}):
                    first = b.isolated_peer_turn(state, contract, 'Use apply_patch and shell for the probe.', url, token, port)
                    state['thread_id'] = first['thread_id']
                    second = b.isolated_peer_turn(state, contract, 'Confirm previous probe result.', url, token, port)
            self.assertEqual(first['thread_id'], second['thread_id'])
            self.assertEqual((Path(state['worktree']) / 'shell_probe.txt').read_text(), 'SHELL_OK')
            self.assertGreaterEqual(len(captured), 4)
            for path, auth, req in captured:
                self.assertEqual(path, '/responses')
                self.assertEqual(auth, 'Bearer SYNTHETIC_UPSTREAM')
                self.assertEqual(req['model'], 'deepseek-flash')
                self.assertEqual(req['reasoning']['effort'], 'max')
                self.assertFalse(req.get('store', False))
                self.assertFalse(req.get('previous_response_id'))
                self.assertNotIn('SYNTHETIC_PARENT_ONLY', json.dumps(req))
                self.assertNotIn(self.q[0]['reference'], json.dumps(req))
            last = captured[-1][2]
            self.assertTrue(any(x.get('type') == 'function_call_output' for x in last['input']))
            self.assertIn('SHELL_OK', json.dumps(last['input']))
            self.assertEqual(second['backend']['reasoning_effort'], 'max')
        finally:
            server.shutdown(); server.server_close(); thread.join()


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (BindingTests, CredentialAndExamTests, FrozenProjectTests, BailianRelayTests, ExecutorIntegrationTests):
        for name in cls.__dict__:
            if name.startswith('test_'): suite.addTest(cls(name))
    return suite


if __name__ == '__main__': unittest.main(verbosity=2)

"""Local endpoint selection, credentials and real CLI protocol regressions."""
import contextlib
import copy
import json
import os
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import chat_adapter as chat
import local_executor as local
import providers as p
from closed_book import ExamPolicy
from isolation import model_relay
from test_telos import b, BridgeTests
from test_providers import configured
from test_closed_book import sse, message
from test_projects import ProjectTests


def spec(port=14000, protocol='chat', **kw):
    return dict(base_url=f'http://127.0.0.1:{port}/v1', model='local-model:latest', wire_api=protocol, **kw)


def config():
    cfg = configured()
    cfg['profiles']['local'] = {'provider': 'local'}
    cfg['local_executor'] = {'active': 'one', 'endpoints': {'one': spec(), 'two': spec(14001, 'responses')}}
    return cfg


@contextlib.contextmanager
def server(reply):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': request})
            status, headers, data = reply(request, len(requests))
            self.send_response(status)
            for name, value in headers.items(): self.send_header(name, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield http.server_port, requests
    finally:
        http.shutdown(); http.server_close(); worker.join()


def completion(text='Done', calls=None, reason=None):
    return {'model': 'local-model:latest', 'choices': [{'message': {'role': 'assistant', 'content': text,
            'tool_calls': calls or []}, 'finish_reason': reason or ('tool_calls' if calls else 'stop')}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}}


class ConfigurationTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def test_one_active_and_cloud_profiles_preserved(self):
        cfg = configured(); b.write_json(b.CONFIG, cfg)
        local.manage(b, 'add', 'one', spec())
        with self.assertRaisesRegex(p.ProviderError, 'No active'):
            p.resolve_executor(b.config(), executor_profile='local')
        local.manage(b, 'add', 'two', spec(14001))
        local.manage(b, 'select', 'one')
        self.assertEqual(p.resolve_executor(b.config())['provider'], 'deepseek')
        local.manage(b, 'select', 'two', make_default=True)
        current = b.config()
        self.assertEqual(p.resolve_executor(current)['local_endpoint'], 'two')
        self.assertEqual(p.resolve_executor(current, executor_profile='ark_coding_plan')['provider'], 'ark')
        self.assertEqual(p.resolve_executor(current, executor_profile='deepseek_official')['provider'], 'deepseek')
        for name, profile in cfg['profiles'].items(): self.assertEqual(current['profiles'][name], profile)
        self.assertEqual(len(local.manage(b, 'list')['endpoints']), 2)
        with self.assertRaises(p.ProviderError): local.manage(b, 'add', 'one', spec())
        with self.assertRaises(p.ProviderError): local.manage(b, 'select', 'missing')

    def test_invalid_and_ambiguous_endpoints_rejected(self):
        for address in ['http://example.com:14000/v1', 'http://192.168.1.1:14000/v1',
                        'http://127.0.0.1/v1', 'http://u:p@127.0.0.1:14000/v1',
                        'http://127.0.0.1:14000/v1?key=secret', 'http://127.0.0.1:14000/v1#part',
                        'http://127.0.0.1:14000/../v1', 'http://127.0.0.1:14000/%76%31',
                        'ftp://127.0.0.1:14000/v1', None]:
            with self.subTest(address=address), self.assertRaises(p.ProviderError):
                local.endpoint({**spec(), 'base_url': address})
        self.assertEqual(local.endpoint({**spec(), 'base_url': 'http://localhost:14000/v1/'})['base_url'], spec()['base_url'])
        self.assertEqual(local.endpoint({**spec(), 'base_url': 'http://[::1]:14000/v1'})['base_url'], 'http://[::1]:14000/v1')
        for value in [['one', 'two'], 'missing', True]:
            cfg = config(); cfg['local_executor']['active'] = value
            with self.assertRaises(p.ProviderError): local.configuration(cfg)
        for env in ['ARK_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'HOME', 'TELOS_RELAY_TOKEN']:
            with self.assertRaises(p.ProviderError): local.endpoint(spec(env=env))
        with self.assertRaises(p.ProviderError):
            p.validate_custom_spec({'base_url': spec()['base_url'], 'env': 'LOCAL_TEST_KEY'})

    def test_frozen_binding_and_resume_selection(self):
        cfg = config(); b.write_json(b.CONFIG, cfg)
        binding = p.resolve_executor(cfg, executor_profile='local')
        self.assertEqual(p.validate_binding(binding), binding)
        self.assertEqual(binding['version'], 3)
        self.assertEqual(binding['wire_api'], 'chat')
        self.assertEqual(binding['reasoning_effort'], 'none')
        local.manage(b, 'select', 'two')
        self.assertEqual(p.validate_binding(binding)['base_url'], spec()['base_url'])
        with self.assertRaisesRegex(p.ProviderError, 'pinned'): local.assert_active(b, binding)
        local.manage(b, 'select', 'one')
        local.assert_active(b, binding)
        for field in ('provider_spec', 'local_endpoint', 'credential_ref'):
            wrong = copy.deepcopy(binding); del wrong[field]
            with self.assertRaises(p.ProviderError): p.validate_binding(wrong)
        wrong = copy.deepcopy(binding); wrong['credential_ref']['opencode_provider'] = 'deepseek'
        with self.assertRaises(p.ProviderError): p.validate_binding(wrong)
        with self.assertRaises(p.ProviderError): p.resolve_executor(cfg, provider='local', model='another')

    def test_endpoint_credentials_and_no_auth_do_not_fall_back(self):
        cfg = config()
        cfg['local_executor']['endpoints']['one']['env'] = 'LOCAL_TEST_KEY'
        cfg['local_executor']['endpoints']['two']['env'] = 'LOCAL_TEST_KEY'
        first = p.resolve_executor(cfg, provider='local')
        path = p.credential_path(self.base)
        b.write_json(path, {'deepseek': {'base_url': p.PROVIDERS['deepseek']['base_url'], 'api_key': 'cloud'},
                           'local:one': {'base_url': first['base_url'], 'api_key': 'first'}})
        self.assertEqual(p.credential(first, self.base, {})[0], 'first')
        self.assertEqual(p.credential(first, self.base, {'LOCAL_TEST_KEY': 'rotated'})[0], 'rotated')
        cfg['local_executor']['active'] = 'two'
        second = p.resolve_executor(cfg, provider='local')
        with self.assertRaises(p.ProviderError): p.credential(second, self.base, {'DEEPSEEK_API_KEY': 'cloud'})
        data = b.read_json(path); data['local:two'] = {'base_url': first['base_url'], 'api_key': 'wrong-port'}
        b.write_json(path, data)
        with self.assertRaises(p.ProviderError): p.credential(second, self.base, {})
        data['local:two']['base_url'] = second['base_url']; data['local:two']['api_key'] = 'second'
        b.write_json(path, data)
        self.assertEqual(p.credential(second, self.base, {})[0], 'second')
        cfg['local_executor']['endpoints']['two']['env'] = None
        self.assertEqual(p.credential(p.resolve_executor(cfg, provider='local'), self.base, {'LOCAL_TEST_KEY': 'ignored'})[0], '')

    def test_selection_blocked_during_lease_and_scheduled_job(self):
        b.write_json(b.CONFIG, config())
        binding = p.resolve_executor(b.config(), provider='local')
        with local.lease(b, binding), local.lease(b, binding):
            with self.assertRaisesRegex(p.ProviderError, 'running'):
                local.manage(b, 'select', 'two')
        sid = b.create_session(str(self.repo), 'Read calc.py', self.q, self.checks, executor_profile='local')['session_id']
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.start_job(sid, 'send', 'Read calc.py')
        with self.assertRaisesRegex(p.ProviderError, 'scheduled/running'): local.manage(b, 'select', 'two')
        state = b.read_json(b.run_dir(sid) / 'state.json'); state['phase'] = 'responded'
        b.write_json(b.run_dir(sid) / 'state.json', state)
        local.manage(b, 'select', 'two')
        with self.assertRaisesRegex(p.ProviderError, 'pinned'): b.start_job(sid, 'send', 'Continue')


class AdapterTests(unittest.TestCase):
    def test_nullable_reasoning_history_preserves_text_and_effort(self):
        for summary, content in [([{'type': 'summary_text', 'text': 'Remembered reasoning'}], None),
                                 (None, [{'type': 'reasoning_text', 'text': 'Remembered reasoning'}]),
                                 (None, None)]:
            with self.subTest(summary=summary, content=content):
                request = {'model': 'local-model:latest', 'reasoning': {'effort': 'high'}, 'input': [
                    {'type': 'reasoning', 'summary': summary, 'content': content, 'encrypted_content': None},
                    {'type': 'function_call', 'name': 'exec_command', 'call_id': 'c1', 'arguments': '{}'},
                    {'type': 'function_call_output', 'call_id': 'c1', 'output': 'Done'}]}
                outgoing, _ = chat.prepare(request)
                self.assertEqual(outgoing['reasoning_effort'], 'high')
                self.assertEqual(outgoing['messages'][0].get('reasoning_content'),
                                 'Remembered reasoning' if summary or content else None)

    def test_namespaced_additional_tools_and_closed_book_removal(self):
        declaration = {'type': 'additional_tools', 'role': 'developer', 'tools': [
            {'type': 'namespace', 'name': 'functions', 'tools': [{'type': 'custom', 'name': 'exec'}]}]}
        request = {'model': 'local-model:latest', 'input': [declaration, {'role': 'user', 'content': 'Hi'}]}
        outgoing, types = chat.prepare(request)
        self.assertEqual(outgoing['tools'][0]['function']['name'], 'functions__exec')
        calls = [{'id': 'c1', 'type': 'function', 'function': {'name': 'functions__exec', 'arguments': '{"input":"text(1)"}'}}]
        item = chat.convert(completion(None, calls), request['model'], types)['output'][0]
        self.assertEqual((item['namespace'], item['name'], item['input']), ('functions', 'exec', 'text(1)'))
        request['input'] += [item, {'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': '1'}]
        outgoing, _ = chat.prepare(request)
        self.assertEqual(outgoing['messages'][-2]['tool_calls'][0]['function']['name'], 'functions__exec')
        policy = ExamPolicy(p.resolve_executor(config(), provider='local'))
        exam = json.loads(policy.prepare(json.dumps(request).encode()))
        self.assertFalse(any(x.get('type') == 'additional_tools' for x in exam['input']))
        outgoing, _ = chat.prepare(exam)
        self.assertNotIn('tools', outgoing)
        request['tool_choice'] = 'none'
        self.assertNotIn('tools', chat.prepare(request)[0])

    def test_history_custom_tools_and_output(self):
        request = {'model': 'local-model:latest', 'instructions': 'Work carefully', 'input': [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Edit'}]},
            {'type': 'custom_tool_call', 'call_id': 'p1', 'name': 'apply_patch', 'input': 'PATCH'},
            {'type': 'custom_tool_call_output', 'call_id': 'p1', 'output': 'Done'}],
            'tools': [{'type': 'custom', 'name': 'apply_patch', 'description': 'Edit files'}],
            'tool_choice': 'auto', 'reasoning': {'effort': 'none'}}
        converted, types = chat.prepare(request)
        self.assertEqual(converted['messages'][0]['role'], 'system')
        self.assertEqual(converted['messages'][2]['tool_calls'][0]['id'], 'p1')
        self.assertEqual(json.loads(converted['messages'][2]['tool_calls'][0]['function']['arguments']), {'input': 'PATCH'})
        self.assertEqual(converted['messages'][3]['tool_call_id'], 'p1')
        self.assertNotIn('reasoning_effort', converted)
        call = {'id': 'p2', 'type': 'function', 'function': {'name': 'apply_patch', 'arguments': '{"input":"NEXT"}'}}
        result = chat.convert(completion(None, [call]), request['model'], types)
        self.assertEqual(result['output'][0]['type'], 'custom_tool_call')
        self.assertEqual(result['output'][0]['input'], 'NEXT')
        self.assertEqual(result['output'][0]['call_id'], 'p2')
        self.assertEqual(result['usage']['input_tokens'], 11)

    def test_reject_unsupported_history_and_malformed_calls(self):
        for item in [{'type': 'compaction'}, {'type': 'reasoning', 'encrypted_content': 'secret'}]:
            with self.assertRaises(p.ProviderError): chat.prepare({'model': 'local', 'input': [item]})
        with self.assertRaises(p.ProviderError): chat.prepare({'model': 'local', 'previous_response_id': 'old'})
        for calls in [[{'id': 'bad', 'type': 'function', 'function': {'name': 'unknown', 'arguments': '{}'}}],
                      [{'id': 'bad', 'type': 'function', 'function': {'name': 'apply_patch', 'arguments': '{"wrong":1}'}}]]:
            with self.assertRaises(p.ProviderError):
                chat.convert(completion(None, calls), 'local', {'apply_patch': {'type': 'custom', 'name': 'apply_patch', 'namespace': None}})

    def test_truncation_never_passes_closed_book(self):
        binding = p.resolve_executor(config(), provider='local')
        for reason in ('length', 'content_filter'):
            result = chat.convert(completion('partial', reason=reason), binding['model'], {})
            self.assertEqual(result['status'], 'incomplete')
            for stream in (False, True):
                data, kind = chat.encode(result, stream)
                with self.assertRaises(p.ProviderError): ExamPolicy(binding).validate_response(data, kind)


class RelayTests(unittest.TestCase):
    def test_rejects_incomplete_tools_and_reports_private_failure(self):
        calls = [{'id': 'c1', 'type': 'function', 'function': {'name': 'exec_command', 'arguments': '{"cmd":"touch unsafe"}'}}]
        with server(lambda request, n: (200, {'Content-Type': 'application/json'}, json.dumps(completion(None, calls, 'length')).encode())) as (port, seen):
            cfg = config(); cfg['local_executor']['endpoints']['one'] = spec(port)
            binding = p.resolve_executor(cfg, provider='local'); diagnostics = {}
            with model_relay(binding['base_url'], '', binding=binding, diagnostics=diagnostics) as (url, token, _):
                req = urllib.request.Request(url + '/responses', data=json.dumps({'model': binding['model'], 'input': 'Hi',
                    'tools': [{'type': 'function', 'name': 'exec_command'}]}).encode(), headers={'Authorization': 'Bearer ' + token})
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
                self.assertNotIn(b'touch unsafe', error.exception.read())
                self.assertIn('no partial tool call', diagnostics['failure'])

    def test_selection_validation_rechecked_before_request(self):
        with server(lambda request, n: (200, {}, b'{}')) as (port, seen):
            cfg = config(); cfg['local_executor']['endpoints']['one'] = spec(port)
            binding = p.resolve_executor(cfg, provider='local')
            with model_relay(binding['base_url'], '', binding=binding,
                             validate=Mock(side_effect=p.ProviderError('Endpoint changed'))) as (url, token, _):
                req = urllib.request.Request(url + '/responses', data=b'{}', headers={'Authorization': 'Bearer ' + token})
                with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(req)
            self.assertEqual(seen, [])

    def test_protocols_authorization_proxy_and_frozen_model(self):
        for protocol in ('chat', 'responses'):
            def reply(request, n):
                return (200, {'Content-Type': 'application/json' if protocol == 'chat' else 'text/event-stream'},
                        json.dumps(completion()).encode() if protocol == 'chat' else sse(request, message('Done')))
            with server(reply) as (port, seen):
                cfg = config(); cfg['local_executor']['endpoints']['one'] = spec(port, protocol)
                binding = p.resolve_executor(cfg, provider='local')
                with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': '', 'NO_PROXY': ''}), \
                        model_relay(binding['base_url'], '', binding=binding) as (url, token, _):
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    def send(model, path='/responses'):
                        req = urllib.request.Request(url + path,
                            data=json.dumps({'model': model, 'input': 'Hi', 'stream': True, 'reasoning': {'effort': 'none'}}).encode(),
                            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                        return opener.open(req).read()
                    self.assertIn(b'response.completed', send(binding['model']))
                    with self.assertRaises(urllib.error.HTTPError): send('other')
                    with self.assertRaises(urllib.error.HTTPError): send(binding['model'], '/responses/compact')
                self.assertEqual(len(seen), 1)
                self.assertIsNone(seen[0]['authorization'])
                self.assertEqual(seen[0]['path'], '/v1/chat/completions' if protocol == 'chat' else '/v1/responses')
                self.assertNotIn('reasoning', seen[0]['body'])

    def test_redirect_does_not_forward_key(self):
        with server(lambda request, n: (200, {}, b'{}')) as (other, stolen):
            with server(lambda request, n: (307, {'Location': f'http://127.0.0.1:{other}/stolen'}, b'')) as (port, seen):
                cfg = config(); cfg['local_executor']['endpoints']['one'] = spec(port)
                binding = p.resolve_executor(cfg, provider='local')
                with model_relay(binding['base_url'], 'fixture-key', binding=binding) as (url, token, _):
                    req = urllib.request.Request(url + '/responses', data=json.dumps({'model': binding['model'], 'input': 'Hi'}).encode(),
                        headers={'Authorization': 'Bearer ' + token})
                    with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(req)
                self.assertEqual(seen[0]['authorization'], 'Bearer fixture-key')
                self.assertEqual(stolen, [])


class LocalWorkflowTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def workflow(self, protocol, model='local-model:latest', effort='none'):
        answers = json.dumps({'answers': [{'id': q['id'], 'answer': '5' if i == 0 else '3'} for i, q in enumerate(self.q)]})
        def reply(request, n):
            if n == 1:
                if model == 'gpt-6.1-sol':
                    item = {'type': 'custom_tool_call', 'id': 'tool_1', 'call_id': 'call_1', 'namespace': 'functions',
                            'name': 'exec', 'input': 'text(await tools.exec_command({cmd: "cat calc.py"}));'}
                else:
                    names = [t.get('function', t)['name'] for t in request['tools']]
                    name = 'shell_command' if 'shell_command' in names else 'exec_command'
                    args = {'command': 'cat calc.py'} if name == 'shell_command' else {'cmd': 'cat calc.py'}
                    item = {'type': 'function_call', 'id': 'tool_1', 'call_id': 'call_1', 'name': name, 'arguments': json.dumps(args)}
            else:
                item = message(answers if request.get('tool_choice') == 'none' else 'Work complete.')
            if protocol == 'responses': return 200, {'Content-Type': 'text/event-stream'}, sse(request, item)
            calls = [{'id': item['call_id'], 'type': 'function', 'function': {
                'name': chat.tool_name(item['name'], item.get('namespace')),
                'arguments': json.dumps({'input': item['input']}) if item['type'] == 'custom_tool_call' else item['arguments']}}] if n == 1 else None
            value = completion(None if calls else item['content'][0]['text'], calls)
            if effort != 'none':
                value['choices'][0]['message']['reasoning_content'] = 'Reasoning retained across work and exam.'
            return 200, {'Content-Type': 'application/json'}, json.dumps(value).encode()
        with server(reply) as (port, seen):
            cfg = config(); cfg['local_executor']['endpoints']['one'] = spec(port, protocol)
            cfg['local_executor']['endpoints']['one']['model'] = model
            cfg['local_executor']['endpoints']['one']['reasoning_effort'] = effort
            cfg.update(job_timeout=40, experience_enabled=False)
            b.write_json(b.CONFIG, cfg)
            sid = b.create_session(str(self.repo), 'Read calc.py and remember it.', self.q, self.checks,
                                   executor_profile='local')['session_id']
            root = b.run_dir(sid); state, contract = b.load_session(root)
            tree = Path(state['worktree']); (tree / 'calc.py').write_text('def add(a, b): return a + b\n# LOCAL_MEMORY_742\n')
            state.update(thread_id=b.peer_turn(state, contract, 'Read calc.py')['thread_id'], phase='responded')
            b.write_json(root / 'state.json', state)
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                jid = b.start_job(sid, 'evaluate')['job_id']
            b.background_job(sid, jid)
            current, _ = b.load_session(root)
            self.assertEqual(current['phase'], 'awaiting_review', current.get('last_error'))
            packet = b.review_packet(sid)
            exam = packet['evidence']['exam']
            self.assertEqual(exam['executor_thread_id'], state['thread_id'])
            self.assertEqual(exam['tool_call_count'], 0)
            self.assertIn('LOCAL_MEMORY_742', json.dumps(seen[-1]['body']))
            self.assertFalse(seen[-1]['body'].get('tools'))
            self.assertEqual(seen[-1]['body']['tool_choice'], 'none')
            if protocol == 'chat' and effort != 'none':
                self.assertTrue(all(r['body']['reasoning_effort'] == effort for r in seen))
                self.assertTrue(any(m.get('reasoning_content') for m in seen[-1]['body']['messages']))
            for secret in [self.q[0]['reference'], self.checks[0]['argv'][2]]: self.assertNotIn(secret, json.dumps(seen))
            self.assertFalse(b.status(sid)['accepted'])
            scores = [{'id': q['id'], 'points': q['points'], 'rationale': 'Verified against the pinned implementation.'} for q in self.q]
            self.assertTrue(b.grade(sid, current['packet_id'], scores, [])['accepted'])
            self.assertEqual(len(seen), 3)

    def test_real_cli_chat_work_and_same_thread_exam(self): self.workflow('chat')

    def test_real_cli_responses_work_and_same_thread_exam(self): self.workflow('responses')

    def test_real_cli_namespaced_chat_tools_and_same_thread_exam(self): self.workflow('chat', 'gpt-6.1-sol')

    def test_real_cli_namespaced_responses_tools_and_same_thread_exam(self): self.workflow('responses', 'gpt-6.1-sol')

    def test_real_cli_high_chat_reasoning_survives_tools_and_exam(self): self.workflow('chat', 'gpt-6.1-sol', 'high')


class LocalProjectTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child

    def test_project_local_tasks_share_endpoint_with_cloud_task(self):
        b.write_json(b.CONFIG, config())
        pid = self.create([self.task('a', executor_profile='local'), self.task('b', executor_profile='local'),
                           self.task('c', executor_profile='deepseek_official')])
        result = self.start(pid)
        self.assertTrue(all(t['phase'] == 'running' for t in result['tasks']))
        bindings = [t['executor'] for t in self.state(pid)['tasks']]
        self.assertEqual([x['provider'] for x in bindings], ['local', 'local', 'deepseek'])
        self.assertEqual(bindings[0], bindings[1])
        with self.assertRaisesRegex(p.ProviderError, 'scheduled/running'): local.manage(b, 'select', 'two')

    def test_reassign_resolves_active_endpoint_without_changing_original_contract(self):
        b.write_json(b.CONFIG, config())
        pid = self.create([self.task('a', executor_profile='local')])
        before = copy.deepcopy(self.state(pid)['tasks'][0])
        local.manage(b, 'select', 'two')
        b.project_manager().reassign(pid, 'a', 'Choose the selected replacement endpoint', executor_profile='local')
        after = self.state(pid)['tasks'][0]
        self.assertNotEqual(after['session_id'], before['session_id'])
        self.assertEqual(after['executor']['local_endpoint'], 'two')
        self.assertEqual(before['executor']['local_endpoint'], 'one')


if __name__ == '__main__': unittest.main()

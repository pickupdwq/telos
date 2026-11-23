"""Executor via this Mac's own Codex ChatGPT login, and profile fallback to 14000."""
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import codex_login
import providers as p
from closed_book import ExamPolicy
from isolation import model_relay

ACCESS = 'SYNTHETIC-ACCESS-TOKEN-NEVER-REAL'


def jwt(exp):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return part({'alg': 'none'}) + '.' + part({'exp': exp}) + '.signature'


def write_login(home, *, exp=None, mode='chatgpt', token=None, account='acct-fixture'):
    path = Path(home) / '.codex' / 'auth.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    exp = time.time() + 86400 if exp is None else exp
    path.write_text(json.dumps({'auth_mode': mode, 'OPENAI_API_KEY': None, 'last_refresh': 'fixture',
                                'tokens': {'access_token': token or jwt(exp) + ACCESS, 'refresh_token': 'rt-fixture',
                                           'id_token': 'id-fixture', 'account_id': account}}))
    return path


def config():
    return {'default_executor_profile': 'gpt_codex',
            'local_executor': {'active': 'asteam14000', 'endpoints': {'asteam14000': {
                'base_url': 'http://127.0.0.1:14000/v1', 'model': 'gpt-6.1-sol', 'wire_api': 'responses',
                'env': None, 'reasoning_effort': 'high'}}},
            'profiles': {'gpt_codex': {'provider': 'codex_login', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high',
                                       'fallback_profile': 'gpt_14000'},
                         'gpt_14000': {'provider': 'local'}}}


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = {}

    def tearDown(self):
        self.home.cleanup()

    def test_reads_login_and_account_without_writing(self):
        path = write_login(self.home.name)
        before = path.read_bytes()
        headers, secret = codex_login.headers(self.home.name, self.env)
        self.assertEqual(headers['Authorization'], 'Bearer ' + secret)
        self.assertEqual(headers['chatgpt-account-id'], 'acct-fixture')
        self.assertEqual(path.read_bytes(), before)

    def test_codex_home_environment_is_respected(self):
        other = Path(self.home.name) / 'custom'
        write_login(other)
        os.rename(other / '.codex' / 'auth.json', other / 'auth.json')
        _, secret = codex_login.headers(self.home.name, {'CODEX_HOME': str(other)})
        self.assertIn(ACCESS, secret)

    def test_missing_api_key_mode_and_expired_logins_are_unavailable(self):
        with self.assertRaises(p.ProviderError):
            codex_login.login(self.home.name, self.env)
        write_login(self.home.name, mode='apikey')
        with self.assertRaises(p.ProviderError):
            codex_login.login(self.home.name, self.env)
        write_login(self.home.name, exp=time.time() - 10)
        with self.assertRaises(p.ProviderError):
            codex_login.login(self.home.name, self.env)
        write_login(self.home.name, token='not-a-jwt')
        with self.assertRaises(p.ProviderError):
            codex_login.login(self.home.name, self.env)

    def test_selection_needs_margin_but_requests_only_validity(self):
        write_login(self.home.name, exp=time.time() + 600)
        self.assertFalse(codex_login.available(self.home.name, self.env))
        self.assertTrue(codex_login.login(self.home.name, self.env)[0])
        write_login(self.home.name)
        self.assertTrue(codex_login.available(self.home.name, self.env))

    def test_usage_limit_cooldown_prefers_fallback_for_new_tasks(self):
        write_login(self.home.name)
        state = Path(self.home.name) / 'state'
        self.assertTrue(codex_login.available(self.home.name, self.env, state_dir=state))
        codex_login.Upstream(self.home.name, state, self.env).limited('120')
        self.assertFalse(codex_login.available(self.home.name, self.env, state_dir=state))
        self.assertTrue(codex_login.available(self.home.name, self.env, now=time.time() + 200, state_dir=state))
        codex_login.mark_limited(state, None)
        self.assertFalse(codex_login.available(self.home.name, self.env, now=time.time() + 200, state_dir=state))

    def test_model_must_be_in_the_login_catalog(self):
        write_login(self.home.name)
        state = Path(self.home.name) / 'state'
        calls = []

        def offered(*models):
            def fetch(token, account):
                calls.append((token, account))
                return set(models)
            return fetch
        self.assertTrue(codex_login.available(self.home.name, self.env, state_dir=state, model='gpt-6.2-sol',
                                              fetch=offered('gpt-6.2-sol', 'gpt-6.1-sol')))
        self.assertIn(ACCESS, calls[0][0])
        self.assertEqual(calls[0][1], 'acct-fixture')
        # Cached briefly: a second check does not ask again.
        self.assertFalse(codex_login.available(self.home.name, self.env, state_dir=state, model='gpt-6.3-sol',
                                               fetch=offered()))
        self.assertEqual(len(calls), 1)
        # After the cache expires the catalog is fetched again.
        later = time.time() + codex_login.MODELS_TTL_SECONDS + 1
        self.assertFalse(codex_login.available(self.home.name, self.env, now=later, state_dir=state,
                                               model='gpt-6.2-sol', fetch=offered('gpt-6.1-sol')))
        self.assertEqual(len(calls), 2)
        self.assertNotIn(ACCESS, (state / codex_login.MODELS_FILE).read_text())

    def test_unknown_catalog_falls_back(self):
        write_login(self.home.name)

        def broken(token, account):
            raise OSError('network down')
        self.assertFalse(codex_login.available(self.home.name, self.env, model='gpt-6.2-sol', fetch=broken))
        # Without a model, the catalog is not consulted.
        self.assertTrue(codex_login.available(self.home.name, self.env, fetch=broken))

    def test_catalog_cache_is_per_account(self):
        write_login(self.home.name)
        state = Path(self.home.name) / 'state'
        self.assertTrue(codex_login.available(self.home.name, self.env, state_dir=state, model='gpt-6.2-sol',
                                              fetch=lambda *_: {'gpt-6.2-sol'}))
        write_login(self.home.name, account='acct-other')
        self.assertFalse(codex_login.available(self.home.name, self.env, state_dir=state, model='gpt-6.2-sol',
                                               fetch=lambda *_: {'gpt-6.1-sol'}))

    def test_prepare_fixes_backend_shape(self):
        request = codex_login.prepare({'model': 'gpt-6.1-sol', 'store': True, 'stream': False,
                                       'previous_response_id': 'resp_x', 'include': ['other']})
        self.assertEqual((request['store'], request['stream']), (False, True))
        self.assertNotIn('previous_response_id', request)
        self.assertEqual(request['include'], ['other', 'reasoning.encrypted_content'])


class BindingTests(unittest.TestCase):
    def test_binding_has_fixed_destination_and_no_key(self):
        binding = p.resolve_executor(config(), executor_profile='gpt_codex', available=lambda *_: True)
        self.assertEqual(binding['provider'], 'codex_login')
        self.assertEqual(binding['base_url'], 'https://chatgpt.com/backend-api/codex')
        self.assertEqual(binding['credential_ref'], {'env': None, 'opencode_provider': None})
        self.assertEqual(binding['reasoning_effort'], 'high')
        self.assertEqual(p.public_binding(binding)['billing'], 'chatgpt_plan')
        with self.assertRaises(p.ProviderError):
            p.validate_binding({**binding, 'base_url': 'https://elsewhere.example/backend-api/codex'})

    def test_fallback_follows_availability_only_at_creation(self):
        cfg = config()
        self.assertEqual(p.resolve_executor(cfg, available=lambda *_: True)['provider'], 'codex_login')
        fallback = p.resolve_executor(cfg, available=lambda _, provider, model: provider != 'codex_login')
        self.assertEqual((fallback['provider'], fallback['local_endpoint']), ('local', 'asteam14000'))
        self.assertEqual(fallback['reasoning_effort'], 'high')
        # An explicit provider or model is an exact request, not a profile chain.
        explicit = p.resolve_executor(cfg, provider='codex_login', model='gpt-6.1-sol',
                                      available=lambda *_: False)
        self.assertEqual(explicit['provider'], 'codex_login')

    def test_selection_passes_the_profile_model(self):
        cfg = config()
        cfg['profiles']['gpt_codex']['model'] = 'gpt-6.2-sol'
        seen = []

        def available(_, provider, model):
            seen.append((provider, model))
            return model == 'gpt-6.2-sol'
        self.assertEqual(p.resolve_executor(cfg, available=available)['model'], 'gpt-6.2-sol')
        self.assertEqual(seen, [('codex_login', 'gpt-6.2-sol')])
        missing = p.resolve_executor(cfg, available=lambda _, provider, model: provider != 'codex_login')
        self.assertEqual(missing['provider'], 'local')

    def test_gpt62_dynamic_efforts(self):
        import effort_policy
        cfg = config()
        cfg['profiles']['gpt_codex']['model'] = 'gpt-6.2-sol'
        binding = p.resolve_executor(cfg, available=lambda *_: True)
        policy = effort_policy.freeze(binding, {'dynamic_reasoning_enabled': True})
        self.assertEqual(policy['supported_efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])
        self.assertEqual(policy['initial_effort'], 'high')

    def test_fallback_chain_errors_are_explicit(self):
        cfg = config()
        cfg['profiles']['gpt_14000']['fallback_profile'] = 'gpt_codex'
        cfg['profiles']['gpt_14000']['provider'] = 'codex_login'
        with self.assertRaises(p.ProviderError):
            p.resolve_executor(cfg, available=lambda *_: False)
        cfg = config()
        cfg['profiles']['gpt_codex']['fallback_profile'] = 'missing'
        with self.assertRaises(p.ProviderError):
            p.resolve_executor(cfg, available=lambda *_: False)

    def test_credential_lookup_reads_login_at_request_time(self):
        with tempfile.TemporaryDirectory() as home:
            binding = p.resolve_executor(config(), executor_profile='gpt_codex', available=lambda *_: True)
            with self.assertRaises(p.ProviderError):
                p.credential(binding, home, {})
            write_login(home)
            secret, source = p.credential(binding, home, {})
            self.assertIn(ACCESS, secret)
            self.assertIn('Codex', source)


class RelayTests(unittest.TestCase):
    """The real relay against a local fixture shaped like the ChatGPT Codex backend."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        write_login(self.home.name)
        cfg = config()
        cfg['profiles']['gpt_codex']['model'] = 'gpt-6.2-sol'
        self.binding = p.resolve_executor(cfg, executor_profile='gpt_codex', available=lambda *_: True)
        self.captured = []
        self.status = [200]
        captured, status = self.captured, self.status

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
                if status[0] != 200:
                    self.send_response(status[0]); self.send_header('Retry-After', '300')
                    self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
                    return
                events = [{'type': 'response.created', 'response': {'id': 'r', 'status': 'in_progress'}},
                          {'type': 'response.metadata', 'metadata': {'fixture': True}},
                          {'type': 'response.output_item.done', 'item': {
                              'type': 'message', 'role': 'assistant',
                              'content': [{'type': 'output_text', 'text': '{"answers":[{"id":"q1","answer":"4"}]}'}]}},
                          {'type': 'response.completed', 'response': {
                              'id': 'r', 'status': 'completed', 'model': body.get('model'),
                              'output': [{'type': 'message', 'role': 'assistant', 'content': [
                                  {'type': 'output_text', 'text': '{"answers":[{"id":"q1","answer":"4"}]}'}]}],
                              'usage': {'input_tokens': 30, 'output_tokens': 7, 'total_tokens': 37}}}]
                data = b''.join(b'event: ' + e['type'].encode() + b'\ndata: ' + json.dumps(e).encode() + b'\n\n'
                                for e in events)
                # Deliberately no Content-Type, like the real backend.
                self.send_response(200); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'
        self.state = Path(self.home.name) / 'state'
        self.upstream = codex_login.Upstream(self.home.name, self.state, {})

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join()
        self.home.cleanup()

    def send(self, url, token, payload):
        request = urllib.request.Request(url + '/responses', data=json.dumps(payload).encode(),
                                         headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            return response.headers.get('Content-Type'), response.read()

    def payload(self):
        return {'model': self.binding['model'], 'service_tier': 'priority', 'store': True, 'stream': False, 'previous_response_id': 'resp_old',
                'reasoning': {'effort': 'high'}, 'input': [{'type': 'message', 'role': 'user',
                                                            'content': [{'type': 'input_text', 'text': 'hi'}]}]}

    def test_work_request_uses_fresh_login_and_backend_shape(self):
        with model_relay(self.endpoint, '', binding=self.binding, upstream_headers=self.upstream) as (url, token, _):
            ctype, raw = self.send(url, token, self.payload())
        sent = self.captured[0]
        self.assertEqual(sent['path'], '/responses')
        self.assertEqual(sent['body'].get('service_tier'), 'priority')
        self.assertIn(ACCESS, sent['headers']['authorization'])
        self.assertEqual(sent['headers']['chatgpt-account-id'], 'acct-fixture')
        self.assertNotIn(token, json.dumps(sent))
        self.assertEqual((sent['body']['store'], sent['body']['stream']), (False, True))
        self.assertNotIn('previous_response_id', sent['body'])
        self.assertIn('reasoning.encrypted_content', sent['body']['include'])
        self.assertEqual(ctype, 'text/event-stream')
        self.assertIn(b'response.completed', raw)

    def test_compact_endpoint_is_not_forwarded(self):
        with model_relay(self.endpoint, '', binding=self.binding, upstream_headers=self.upstream) as (url, token, _):
            request = urllib.request.Request(url + '/responses/compact', data=b'{}',
                                             headers={'Authorization': 'Bearer ' + token})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.captured, [])

    def test_closed_book_exam_accepts_metadata_event_and_disables_tools(self):
        policy = ExamPolicy(self.binding)
        payload = {**self.payload(), 'tools': [{'type': 'function', 'name': 'shell'}], 'tool_choice': 'auto'}
        with model_relay(self.endpoint, '', binding=self.binding, policy=policy,
                         upstream_headers=self.upstream) as (url, token, _):
            _, raw = self.send(url, token, payload)
        sent = self.captured[0]['body']
        self.assertEqual(sent.get('service_tier'), 'priority')
        self.assertEqual((sent['tools'], sent['tool_choice'], sent['store'], sent['stream']), ([], 'none', False, True))
        self.assertIsNone(policy.failure)
        self.assertEqual(len(policy.responses), 1)
        self.assertIn(b'answers', raw)

    def test_metadata_event_only_for_codex_backends(self):
        raw = (b'data: ' + json.dumps({'type': 'response.metadata'}).encode() + b'\n\n')
        with self.assertRaises(p.ProviderError) as caught:
            ExamPolicy(p.resolve_binding({}, provider='bailian')).validate_response(raw, 'text/event-stream')
        self.assertIn('disallowed stream event', str(caught.exception))
        relayed = p.resolve_executor(config(), executor_profile='gpt_14000')
        with self.assertRaises(p.ProviderError) as caught:
            ExamPolicy(relayed).validate_response(raw, 'text/event-stream')
        # Accepted as an event; the fixture then fails only for having no completion.
        self.assertIn('complete exactly once', str(caught.exception))
        other = config()
        other['local_executor']['endpoints']['asteam14000']['model'] = 'deepseek-flash'
        with self.assertRaises(p.ProviderError) as caught:
            ExamPolicy(p.resolve_executor(other, executor_profile='gpt_14000')).validate_response(raw, 'text/event-stream')
        self.assertIn('disallowed stream event', str(caught.exception))

    def test_usage_limit_marks_cooldown_and_never_leaks_login(self):
        self.status[0] = 429
        diagnostics = {}
        with model_relay(self.endpoint, '', binding=self.binding, upstream_headers=self.upstream,
                         diagnostics=diagnostics) as (url, token, _):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.send(url, token, self.payload())
        self.assertEqual(caught.exception.code, 429)
        self.assertGreater(codex_login.limited_until(self.state), time.time() + 200)
        self.assertNotIn(ACCESS, json.dumps(diagnostics))

    def test_login_headers_are_required_exactly_for_codex_login(self):
        with self.assertRaises(ValueError):
            with model_relay(self.endpoint, '', binding=self.binding):
                pass
        with self.assertRaises(ValueError):
            with model_relay(self.endpoint, '', binding=p.resolve_binding({}, provider='bailian'),
                             upstream_headers=self.upstream):
                pass


if __name__ == '__main__':
    unittest.main()

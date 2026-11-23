"""Exercise the installed CLI, Telos's real launch settings and ChatGPT relay offline."""
import unittest
import uuid

import codex_login
import executor
import providers
from closed_book import ExamPolicy
from isolation import model_relay
from test_closed_book import message, sse
from test_codex_login import write_login
from test_local_executor import server
from test_telos import b
import test_telos


class ServiceTierWireTests(unittest.TestCase):
    setUp = test_telos.BridgeTests.setUp
    tearDown = test_telos.BridgeTests.tearDown

    def check_requests(self, model):
        binding = providers.resolve_binding({'job_timeout': 35}, provider='codex_login', model=model)
        contract = {'workflow_version': 3, 'task': 'Reply OK.', 'context': '', 'executor': binding}
        state = {'session_id': uuid.uuid4().hex, 'worktree': str(self.repo)}
        write_login(self.base)
        login = codex_login.Upstream(self.base, b.STATE, {})
        policy = ExamPolicy(binding)

        def reply(body, _):
            item = message('{"answers":[{"id":"q1","answer":"OK"}]}')
            return 200, {'Content-Type': 'text/event-stream'}, sse(body, item)

        with server(reply) as (port, requests):
            for phase in ('work', 'exam'):
                with model_relay(f'http://127.0.0.1:{port}/v1', '', binding=binding,
                                 upstream_headers=login, policy=policy if phase == 'exam' else None) as relay:
                    result = executor.run(b.bridge_services(), state, contract, 'Reply OK.', *relay, phase=phase)
                    state['thread_id'] = result['thread_id']
        self.assertEqual([r['path'] for r in requests], ['/v1/responses', '/v1/responses'])
        for request in requests:
            body = request['body']
            self.assertEqual(body['model'], model)
            if model == 'gpt-6.2-sol':
                self.assertEqual(body.get('service_tier'), 'priority')
            else:
                self.assertNotIn('service_tier', body)
        self.assertEqual(requests[-1]['body']['tool_choice'], 'none')

    def test_gpt_62_sol_sends_priority(self):
        self.check_requests('gpt-6.2-sol')

    def test_gpt_61_sol_omits_tier(self):
        self.check_requests('gpt-6.1-sol')

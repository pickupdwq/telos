"""Direct behavioral tests for usage normalization, durable budgets and the relay."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import isolation
import usage
import closed_book
import chat_adapter
import telos


def responses_usage(input_tokens=100, cached=40, output=20, reasoning=5, total=120):
    return {'input_tokens': input_tokens, 'output_tokens': output,
            'total_tokens': total,
            'input_tokens_details': {'cached_tokens': cached},
            'output_tokens_details': {'reasoning_tokens': reasoning}}


def chat_usage(prompt=11, completion=7, cached=0, reasoning=0, total=18):
    return {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': total,
            'prompt_tokens_details': {'cached_tokens': cached},
            'completion_tokens_details': {'reasoning_tokens': reasoning}}


class NormalizationTests(unittest.TestCase):
    def test_responses_and_chat_are_normalized_without_double_counting_subsets(self):
        normalized = usage.normalize_usage(responses_usage())
        self.assertEqual(normalized, {'input_tokens': 100, 'cached_input_tokens': 40,
                                      'uncached_input_tokens': 60, 'output_tokens': 20,
                                      'reasoning_output_tokens': 5, 'total_tokens': 120})
        normalized = usage.normalize_usage(chat_usage())
        self.assertEqual(normalized['input_tokens'], 11)
        self.assertEqual(normalized['cached_input_tokens'], 0)
        self.assertEqual(normalized['uncached_input_tokens'], 11)
        self.assertEqual(normalized['output_tokens'], 7)
        self.assertEqual(normalized['reasoning_output_tokens'], 0)
        self.assertEqual(normalized['total_tokens'], 18)

    def test_missing_and_invalid_usage_is_unknown_not_zero(self):
        for value in (None, {}, {'input_tokens': True, 'output_tokens': -3},
                      {'input_tokens': '12', 'output_tokens': None},
                      {'input_tokens': 5}, {'output_tokens': 2},
                      {'prompt_tokens': 5}, {'completion_tokens': 2}):
            self.assertIsNone(usage.normalize_usage(value))
        normalized = usage.normalize_usage({'input_tokens': 10, 'output_tokens': 4})
        self.assertIsNone(normalized['cached_input_tokens'])
        self.assertIsNone(normalized['reasoning_output_tokens'])
        self.assertIsNone(normalized['uncached_input_tokens'])
        self.assertEqual(normalized['total_tokens'], 14)
        invalid_total = usage.normalize_usage({'input_tokens': 10, 'output_tokens': 4, 'total_tokens': -1})
        self.assertEqual(invalid_total['total_tokens'], 14)
        contradictory = usage.normalize_usage({'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 5})
        self.assertEqual(contradictory['total_tokens'], 14)
        normalized = usage.normalize_usage({'input_tokens': 5, 'output_tokens': 2,
                                            'input_tokens_details': {'cached_tokens': 9},
                                            'output_tokens_details': {'reasoning_tokens': 7}})
        self.assertIsNone(normalized['cached_input_tokens'])
        self.assertIsNone(normalized['reasoning_output_tokens'])

    def test_budget_validation_rejects_bool_unknown_and_nonpositive_limits(self):
        self.assertIsNone(usage.validate_budget(None))
        self.assertEqual(usage.validate_budget({'max_requests': 2}), {'max_requests': 2})
        for value in ({}, {'unknown': 1}, {'max_requests': True}, {'max_requests': 0},
                      {'max_output_tokens': -1}, {'max_input_tokens': '3'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                usage.validate_budget(value)

    def test_payload_usage_extraction_handles_json_sse_malformed_and_duplicates(self):
        normalized = usage.normalize_usage(responses_usage())
        self.assertEqual(isolation.usage_from_payload(
            json.dumps({'response': {'usage': responses_usage()}}).encode(), 'application/json'), responses_usage())
        event = {'type': 'response.completed', 'response': {'usage': responses_usage()}}
        stream = (('event: response.completed\ndata: ' + json.dumps(event) + '\n\n') * 2).encode()
        self.assertEqual(isolation.usage_from_payload(stream, 'text/event-stream'), responses_usage())
        self.assertIsNone(isolation.usage_from_payload(b'not-json', 'application/json'))
        self.assertIsNone(isolation.usage_from_payload(b'{"output":[]}', 'application/json'))
        self.assertEqual(isolation.usage_from_payload(
            json.dumps({'usage': chat_usage()}).encode(), 'application/json'), chat_usage())
        self.assertEqual(normalized['uncached_input_tokens'], 60)

    def test_terminal_response_outcomes_are_classified_without_provider_bodies(self):
        self.assertEqual(isolation.response_outcome(
            json.dumps({'status': 'incomplete'}).encode(), 'application/json'), 'incomplete')
        self.assertEqual(isolation.response_outcome(
            json.dumps({'response': {'status': 'failed'}}).encode(), 'application/json'), 'failed')
        self.assertEqual(isolation.response_outcome(b'not-json', 'application/json'), 'malformed')
        self.assertEqual(isolation.response_outcome(b'', 'application/json'), 'malformed')
        self.assertEqual(isolation.response_outcome(
            b'event: response.created\ndata: {"type":"response.created"}\n\n', 'text/event-stream'),
            'missing_terminal')
        stream = (
            'event: response.incomplete\ndata: ' + json.dumps({'type': 'response.incomplete'}) + '\n\n'
            'event: response.completed\ndata: ' + json.dumps({'type': 'response.completed'}) + '\n\n'
        ).encode()
        self.assertEqual(isolation.response_outcome(stream, 'text/event-stream'), 'completed')

    def test_chat_conversion_does_not_invent_missing_usage_details(self):
        response = {'model': 'm', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'},
                                                'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}
        converted = chat_adapter.convert(response, 'm', {})
        self.assertEqual(converted['usage'], {'input_tokens': 10, 'output_tokens': 2})
        normalized = usage.normalize_usage(converted['usage'])
        self.assertEqual(normalized['total_tokens'], 12)
        self.assertIsNone(normalized['cached_input_tokens'])
        self.assertIsNone(normalized['reasoning_output_tokens'])

    def test_closed_book_partial_usage_keeps_known_totals_and_unknown_count(self):
        attempts = [{'responses': [{'usage': responses_usage()}, {'usage': None},
                                   {'usage': {'input_tokens': 500}}]}]
        totals = closed_book.response_usage(attempts)
        self.assertEqual(totals['input_tokens'], 100)
        self.assertEqual(totals['output_tokens'], 20)
        self.assertEqual(totals['unknown_responses'], 2)
        self.assertIsNone(closed_book.response_usage([{'responses': []}]))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'private' / 'usage.json'

    def tearDown(self):
        self.temp.cleanup()

    def test_completion_replay_and_failed_unknown_attempts_are_counted_once(self):
        ledger = usage.UsageLedger(self.path)
        first = ledger.begin('work', provider='deepseek', model='deepseek-flash', job='job-1')
        snapshot = ledger.finish(first, responses_usage())
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['input_tokens'], 100)
        self.assertEqual(snapshot['phases']['work']['requests'], 1)
        self.assertEqual(snapshot['phases']['exam']['requests'], 0)
        self.assertEqual(ledger.finish(first, responses_usage(1000, 0, 1000))['totals']['input_tokens'], 100)
        second = ledger.begin('exam')
        snapshot = ledger.finish(second, {'input_tokens': False}, status='failed')
        self.assertEqual(snapshot['totals']['requests'], 2)
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 1)
        self.assertEqual(snapshot['phases']['exam']['failed_requests'], 1)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['requests'][first]['provider'], 'deepseek')
        self.assertEqual(stored['requests'][first]['model'], 'deepseek-flash')
        self.assertEqual(stored['requests'][first]['job'], 'job-1')
        self.assertNotIn('prompt', json.dumps(stored))

    def test_reopening_preserves_usage_and_legacy_budget_cannot_be_raised(self):
        ledger = usage.UsageLedger(self.path, {'max_requests': 2})
        ledger.finish(ledger.begin('work'), responses_usage())
        reopened = usage.UsageLedger(self.path, {'max_requests': 2})
        self.assertEqual(reopened.snapshot()['totals']['requests'], 1)
        reopened.finish(reopened.begin('work'), responses_usage())
        with self.assertRaises(usage.BudgetExceeded) as caught:
            reopened.begin('work')
        self.assertIn('max_requests', str(caught.exception))
        # Reopening without repeating the budget still enforces the persisted one.
        with self.assertRaises(usage.BudgetExceeded):
            usage.UsageLedger(self.path).begin('work')
        with self.assertRaises(ValueError):
            usage.UsageLedger(self.path, {'max_requests': 3})

    def test_token_limits_stop_the_next_request_after_observed_spend(self):
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10, 'max_total_tokens': 15})
        ledger.finish(ledger.begin('work'), {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            ledger.begin('exam')
        self.assertIn('max_input_tokens', str(caught.exception))
        self.assertEqual(caught.exception.snapshot['budget_state']['state'], 'exceeded')

    def test_abandoned_reservation_blocks_token_budget_after_reopen(self):
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        ledger.begin('work')
        reopened = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        snapshot = reopened.snapshot()
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['unresolved_requests'], 1)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 1)
        self.assertEqual(snapshot['budget_state']['state'], 'unknown')
        with self.assertRaises(usage.BudgetExceeded) as caught:
            reopened.begin('work')
        self.assertEqual(caught.exception.snapshot['budget_state']['state'], 'unknown')
        # A request-count-only budget still counts the reservation exactly once.
        request_path = Path(self.temp.name) / 'requests-only.json'
        requests = usage.UsageLedger(request_path, {'max_requests': 2})
        requests.begin('work')
        usage.UsageLedger(request_path, {'max_requests': 2}).begin('exam')
        self.assertEqual(usage.UsageLedger(request_path).snapshot()['totals']['requests'], 2)

    def test_unknown_usage_blocks_only_configured_token_limits(self):
        unknown = usage.UsageLedger(self.path, {'max_input_tokens': 100})
        unknown.finish(unknown.begin('work'), None)
        with self.assertRaises(usage.BudgetExceeded) as caught:
            unknown.begin('work')
        self.assertEqual(caught.exception.snapshot['budget_state']['state'], 'unknown')
        requests_only = usage.UsageLedger(Path(self.temp.name) / 'requests.json', {'max_requests': 3})
        requests_only.finish(requests_only.begin('work'), None)
        requests_only.finish(requests_only.begin('exam'), {'input_tokens': 1, 'output_tokens': 1})
        snapshot = requests_only.snapshot()
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 1)
        self.assertEqual(snapshot['budget_state']['state'], 'ok')

    def test_partial_known_aggregates_and_unknown_counts_are_preserved(self):
        ledger = usage.UsageLedger(self.path)
        ledger.finish(ledger.begin('work'), {'input_tokens': 5, 'output_tokens': 2, 'total_tokens': 7})
        ledger.finish(ledger.begin('work'), {'input_tokens': False})
        ledger.finish(ledger.begin('work'), {'input_tokens': 100})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['input_tokens'], 5)
        self.assertEqual(snapshot['totals']['output_tokens'], 2)
        self.assertEqual(snapshot['totals']['total_tokens'], 7)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 2)
        self.assertEqual(snapshot['unknown_fields']['input_tokens'], 2)

    def test_concurrent_reservations_cannot_exceed_the_request_budget(self):
        ledger = usage.UsageLedger(self.path, {'max_requests': 5})
        accepted, rejected = [], []
        barrier = threading.Barrier(8)

        def reserve():
            barrier.wait()
            try:
                accepted.append(usage.UsageLedger(self.path).begin('work'))
            except usage.BudgetExceeded as error:
                rejected.append(error)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(accepted), 5)
        self.assertEqual(len(rejected), 3)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['requests'], 5)
        self.assertEqual(snapshot['budget_state']['remaining']['max_requests'], 0)

    def test_repair_alias_counts_as_work(self):
        ledger = usage.UsageLedger(self.path)
        ledger.finish(ledger.begin('repair'), responses_usage())
        self.assertEqual(ledger.snapshot()['phases']['work']['requests'], 1)

    def test_work_exam_and_format_attempts_share_one_ledger(self):
        ledger = usage.UsageLedger(self.path, {'max_requests': 4})
        first = ledger.finish(ledger.begin('work'), responses_usage())
        second = ledger.finish(ledger.begin('work'), responses_usage())
        third = ledger.finish(ledger.begin('exam'), responses_usage())
        fourth = ledger.finish(ledger.begin('exam'), responses_usage())
        self.assertEqual((first['totals']['requests'], second['totals']['requests'],
                          third['totals']['requests'], fourth['totals']['requests']), (1, 2, 3, 4))
        self.assertEqual(ledger.snapshot()['phases']['work']['requests'], 2)
        self.assertEqual(ledger.snapshot()['phases']['exam']['requests'], 2)
        with self.assertRaises(usage.BudgetExceeded):
            ledger.begin('work')


class PeerBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'usage.json'
        self.state = {'session_id': 'a' * 32, 'job_id': 'job-1'}
        self.contract = {'executor': {'provider': 'ark', 'model': 'm', 'reasoning_effort': 'max',
                                      'base_url': 'http://127.0.0.1:9/v1', 'http_timeout': 1}}
        self.binding = self.contract['executor']

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def fake_relay(self, *args, **kwargs):
        yield 'http://127.0.0.1:9/v1', 'token', 9

    def run_peer(self, ledger, finish_usage):
        def isolated(*args, **kwargs):
            request = ledger.begin('work', provider='ark', model='m', job='job-1')
            ledger.finish(request, finish_usage)
            return {'thread_id': 't', 'summary': 'ok', 'usage': None}
        with patch.object(telos, 'backend_bindings', return_value={'executor': self.binding}), \
                patch.object(telos, 'provider_credential', return_value=('KEY', 'env')), \
                patch.object(telos, 'usage_ledger', return_value=ledger), \
                patch.object(telos, 'isolated_peer_turn', side_effect=isolated), \
                patch.object(isolation, 'model_relay', self.fake_relay):
            return telos.peer_turn(self.state, self.contract, 'message')

    def test_unknown_usage_fails_the_turn_instead_of_silent_success(self):
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(ledger, None)
        self.assertIn('unknown', str(caught.exception))
        self.assertEqual(caught.exception.snapshot['budget_state']['state'], 'unknown')

    def test_observed_overspend_allows_the_turn_but_blocks_the_next_request(self):
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        result = self.run_peer(ledger, {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11})
        self.assertEqual(result['summary'], 'ok')
        with self.assertRaises(usage.BudgetExceeded):
            ledger.begin('exam')

    def test_empty_credential_does_not_shred_error_diagnostics(self):
        binding = {'provider': 'local'}
        with patch.object(telos, 'provider_credential', return_value=('', 'local')):
            self.assertEqual(telos.redact_credentials('actionable failure', [binding]), 'actionable failure')
        with patch.object(telos, 'provider_credential', return_value=('secret-key', 'env')):
            self.assertEqual(telos.redact_credentials('failed secret-key', [binding]), 'failed [REDACTED]')

    def test_ledger_read_failure_is_not_ignored_after_a_turn(self):
        ledger = usage.UsageLedger(self.path)
        with patch.object(ledger, 'ensure_known', side_effect=OSError('ledger unreadable')):
            with self.assertRaises(telos.GateError) as caught:
                self.run_peer(ledger, {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})
        self.assertIn('Usage accounting unavailable', str(caught.exception))


@contextlib.contextmanager
def upstream(reply):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            requests.append(request)
            status, content_type, data = reply(request, len(requests))
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown(); server.server_close(); worker.join()


def post(relay, path='/responses', payload=None):
    url, token, _ = relay
    request = urllib.request.Request(url + path, data=json.dumps(payload or {}).encode(),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + token})
    return urllib.request.urlopen(request, timeout=10).read()


class RelayAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'usage.json'

    def tearDown(self):
        self.temp.cleanup()

    def test_responses_json_usage_is_recorded_once(self):
        def reply(request, n):
            return 200, 'application/json', json.dumps({'usage': responses_usage()}).encode()
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work', 'provider': 'deepseek',
                                                      'model': 'deepseek-flash', 'job': 'j1'}) as relay:
                post(relay, payload={'model': 'deepseek-flash'})
        snapshot = ledger.snapshot()
        self.assertEqual(len(seen), 1)
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['input_tokens'], 100)
        self.assertEqual(snapshot['totals']['reasoning_output_tokens'], 5)

    def test_duplicate_terminal_sse_usage_events_are_not_summed(self):
        event = {'type': 'response.completed', 'response': {'usage': responses_usage()}}
        data = (('event: response.completed\ndata: ' + json.dumps(event) + '\n\n') * 2).encode()

        def reply(request, n):
            return 200, 'text/event-stream', data
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'exam'}) as relay:
                post(relay, payload={'model': 'deepseek-flash'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['input_tokens'], 100)
        self.assertEqual(snapshot['totals']['output_tokens'], 20)
        self.assertEqual(snapshot['totals']['total_tokens'], 120)

    def test_local_chat_usage_is_recorded_before_conversion_and_failure_is_bounded(self):
        binding = {'provider': 'local', 'model': 'local-model', 'wire_api': 'chat',
                   'reasoning_effort': 'none'}
        good = {'model': 'local-model', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'},
                                                      'finish_reason': 'stop'}], 'usage': chat_usage()}
        def reply(request, n):
            return 200, 'application/json', json.dumps(good).encode()
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', binding=binding,
                                       usage_ledger=ledger, usage_context={'phase': 'work'}) as relay:
                post(relay, payload={'model': 'local-model', 'input': 'hello'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['input_tokens'], 11)
        self.assertEqual(snapshot['totals']['cached_input_tokens'], 0)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 0)
        self.assertEqual(seen[0].get('messages', [])[0]['content'], 'hello')

        bad = {'model': 'local-model', 'choices': [], 'usage': chat_usage()}
        def broken(request, n):
            return 200, 'application/json', json.dumps(bad).encode()
        bad_path = Path(self.temp.name) / 'bad.json'
        with upstream(broken) as (port, seen):
            ledger = usage.UsageLedger(bad_path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', binding=binding,
                                       usage_ledger=ledger, usage_context={'phase': 'exam'}) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay, payload={'model': 'local-model', 'input': 'hello'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['input_tokens'], 11)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 0)

    def test_local_chat_exam_policy_validates_once_and_keeps_transport_evidence(self):
        binding = {'provider': 'local', 'model': 'local-model', 'wire_api': 'chat',
                   'reasoning_effort': 'none', 'exam_max_tokens': 64}
        policy = closed_book.ExamPolicy(binding)
        good = {'model': 'local-model', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'},
                                                      'finish_reason': 'stop'}], 'usage': chat_usage()}
        def reply(request, n):
            return 200, 'application/json', json.dumps(good).encode()
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', binding=binding,
                                       policy=policy, usage_ledger=ledger,
                                       usage_context={'phase': 'exam'}) as relay:
                post(relay, payload={'model': 'local-model', 'input': 'hello'})
        self.assertEqual(len(policy.requests), 1)
        self.assertEqual(len(policy.responses), 1)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['failed_requests'], 0)
        self.assertEqual(snapshot['totals']['input_tokens'], 11)

    def test_local_chat_policy_rejection_records_failure_and_known_usage(self):
        class ToolRejectingPolicy:
            MAX_RESPONSE_BYTES = 16 * 1024 * 1024
            def __init__(self):
                self.calls = 0
            def prepare(self, raw):
                return raw
            def validate_response(self, raw, content_type):
                self.calls += 1
                if b'function_call' in raw:
                    raise ValueError('closed-book tool call rejected')

        binding = {'provider': 'local', 'model': 'local-model', 'wire_api': 'chat',
                   'reasoning_effort': 'none'}
        policy = ToolRejectingPolicy()
        response = {'model': 'local-model', 'choices': [{'message': {
            'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call-1', 'type': 'function',
                            'function': {'name': 'shell', 'arguments': '{}'}}]},
            'finish_reason': 'tool_calls'}], 'usage': chat_usage()}
        def reply(request, n):
            return 200, 'application/json', json.dumps(response).encode()
        tools = [{'type': 'function', 'name': 'shell', 'description': 'run',
                  'parameters': {'type': 'object', 'properties': {}}}]
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', binding=binding,
                                       policy=policy, usage_ledger=ledger,
                                       usage_context={'phase': 'exam'}) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay, payload={'model': 'local-model', 'input': 'hello', 'tools': tools})
        self.assertEqual(policy.calls, 1)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['input_tokens'], 11)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 0)

    def test_exhausted_budget_blocks_upstream_and_http_failure_is_recorded(self):
        def reply(request, n):
            return 200, 'application/json', json.dumps({'usage': responses_usage()}).encode()
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path, {'max_requests': 1})
            ledger.finish(ledger.begin('work'), responses_usage())
            failure = {}
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}, budget_failure=failure) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay, payload={'model': 'deepseek-flash'})
            self.assertEqual(seen, [])
            self.assertIn('Task budget exhausted', failure['message'])
        self.assertEqual(ledger.snapshot()['totals']['requests'], 1)

        failed_path = Path(self.temp.name) / 'failed.json'
        def error(request, n):
            return 503, 'application/json', b'{"error":"provider detail should not be stored"}'
        with upstream(error) as (port, seen):
            ledger = usage.UsageLedger(failed_path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'exam'}) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay, payload={'model': 'deepseek-flash'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 1)
        self.assertEqual(snapshot['phases']['exam']['failed_requests'], 1)
        self.assertNotIn('provider detail', ledger.path.read_text())

    def test_http_error_response_usage_is_retained_without_provider_body(self):
        def error(request, n):
            body = json.dumps({'error': 'provider detail must not persist', 'usage': responses_usage()})
            return 503, 'application/json', body.encode()
        with upstream(error) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay, payload={'model': 'deepseek-flash'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 0)
        self.assertEqual(snapshot['totals']['input_tokens'], 100)
        self.assertEqual(snapshot['totals']['reasoning_output_tokens'], 5)
        self.assertNotIn('provider detail', ledger.path.read_text())

    def test_incomplete_and_failed_responses_are_classified_as_failed(self):
        payloads = [
            ({'status': 'incomplete', 'usage': responses_usage()}, 'application/json', 'response_incomplete'),
            ({'status': 'failed', 'usage': responses_usage()}, 'application/json', 'response_failed'),
        ]
        for index, (payload, content_type, error_kind) in enumerate(payloads):
            with self.subTest(error_kind=error_kind):
                def reply(request, n, payload=payload, content_type=content_type):
                    return 200, content_type, json.dumps(payload).encode()
                path = Path(self.temp.name) / f'terminal-{index}.json'
                with upstream(reply) as (port, seen):
                    ledger = usage.UsageLedger(path)
                    with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                               usage_context={'phase': 'work'}) as relay:
                        post(relay, payload={'model': 'deepseek-flash'})
                snapshot = ledger.snapshot()
                self.assertEqual(snapshot['totals']['failed_requests'], 1)
                self.assertEqual(snapshot['totals']['input_tokens'], 100)
                stored = json.loads(path.read_text())['requests']
                self.assertEqual(next(iter(stored.values()))['error_kind'], error_kind)

        event = {'type': 'response.failed', 'response': {'status': 'failed', 'usage': responses_usage()}}
        stream = ('event: response.failed\ndata: ' + json.dumps(event) + '\n\n').encode()
        def streamed(request, n):
            return 200, 'text/event-stream', stream
        stream_path = Path(self.temp.name) / 'terminal-stream.json'
        with upstream(streamed) as (port, seen):
            ledger = usage.UsageLedger(stream_path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'exam'}) as relay:
                post(relay, payload={'model': 'deepseek-flash'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['input_tokens'], 100)

    def test_malformed_json_and_truncated_sse_are_failed_unknown(self):
        def malformed(request, n):
            return 200, 'application/json', b'{not valid json'
        malformed_path = Path(self.temp.name) / 'malformed.json'
        with upstream(malformed) as (port, seen):
            ledger = usage.UsageLedger(malformed_path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}) as relay:
                post(relay, payload={'model': 'deepseek-flash'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 1)
        stored = json.loads(malformed_path.read_text())['requests']
        self.assertEqual(next(iter(stored.values()))['error_kind'], 'response_malformed')

        truncated = b'event: response.created\ndata: {"type":"response.created"}\n\n'
        def incomplete_stream(request, n):
            return 200, 'text/event-stream', truncated
        stream_path = Path(self.temp.name) / 'truncated-stream.json'
        with upstream(incomplete_stream) as (port, seen):
            ledger = usage.UsageLedger(stream_path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'exam'}) as relay:
                post(relay, payload={'model': 'deepseek-flash'})
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['failed_requests'], 1)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 1)
        stored = json.loads(stream_path.read_text())['requests']
        self.assertEqual(next(iter(stored.values()))['error_kind'], 'response_missing_terminal')

    def test_accounting_completion_failure_is_surfaced(self):
        class BrokenLedger:
            def begin(self, phase, **kwargs):
                return 'request-1'
            def finish(self, *args, **kwargs):
                raise OSError('ledger write failed')

        def reply(request, n):
            return 200, 'application/json', json.dumps({'usage': responses_usage()}).encode()
        failure = {}
        with upstream(reply) as (port, seen):
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=BrokenLedger(),
                                       usage_context={'phase': 'work'}, budget_failure=failure) as relay:
                post(relay, payload={'model': 'deepseek-flash'})
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay, payload={'model': 'deepseek-flash'})
        self.assertEqual(len(seen), 1)
        self.assertTrue(failure.get('accounting'))
        self.assertIn('Usage accounting failed', failure['message'])

    def test_unknown_usage_stops_a_configured_token_budget(self):
        def reply(request, n):
            return 200, 'application/json', json.dumps({'output': []}).encode()
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}) as relay:
                post(relay, payload={'model': 'deepseek-flash'})
            self.assertEqual(len(seen), 1)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay, payload={'model': 'deepseek-flash'})
            self.assertEqual(len(seen), 1)
        self.assertEqual(ledger.snapshot()['budget_state']['state'], 'unknown')


if __name__ == '__main__':
    unittest.main(verbosity=2)

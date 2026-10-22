"""Direct behavioral tests for usage normalization, durable budgets and the relay."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
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

    def test_streaming_sse_metadata_handles_crlf_multiline_and_arbitrary_splits(self):
        event = {'type': 'response.completed', 'response': {'usage': responses_usage()}}
        lines = json.dumps(event, indent=2).splitlines()
        stream = (b'event: response.completed\r\n'
                  + b'\r\n'.join(b'data: ' + line.encode() for line in lines)
                  + b'\r\n\r\n')
        tracker = isolation.SSEUsageTracker()
        for byte in stream:
            tracker.feed(bytes([byte]))
        self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))

    def test_streaming_sse_metadata_keeps_terminal_event_larger_than_two_mib(self):
        event = {'type': 'response.completed', 'response': {
            'usage': responses_usage(),
            'output': [{'type': 'output_text', 'text': 'x' * (2 * 1024 * 1024 + 8192)}]}}
        stream = (b'event: response.completed\r\ndata: ' + json.dumps(event).encode()
                  + b'\r\n\r\n')
        tracker = isolation.SSEUsageTracker()
        for start in range(0, len(stream), 4093):
            tracker.feed(stream[start:start + 4093])
        self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))

    def test_streaming_sse_metadata_does_not_invent_a_terminal_event(self):
        tracker = isolation.SSEUsageTracker()
        tracker.feed(b'event: response.created\r\ndata: {"type":"response.created"}\r\n\r\n')
        self.assertEqual(tracker.finish(), (None, None))
        self.assertEqual(isolation._terminal('completed', 'missing_terminal'),
                         ('failed', 'response_missing_terminal'))

    def test_streaming_sse_metadata_recovers_after_an_oversized_event(self):
        tracker = isolation.SSEUsageTracker(max_event_bytes=1024)
        oversized = (b'event: response.output_text.delta\r\ndata: '
                     + b'x' * 2048 + b'\r\n\r\n')
        terminal = {'type': 'response.completed', 'response': {'usage': responses_usage()}}
        stream = oversized + (b'event: response.completed\r\ndata: '
                              + json.dumps(terminal).encode() + b'\r\n\r\n')
        tracker.feed(stream)
        self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))

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


class SSEChunkScanTests(unittest.TestCase):
    @staticmethod
    def terminal_event(usage=None, kind='response.completed'):
        response = {'usage': usage} if usage is not None else {}
        return json.dumps({'type': kind, 'response': response}, separators=(',', ':')).encode()

    def test_crlf_and_lf_splits_expose_terminal_at_the_same_feed_boundary(self):
        payload = self.terminal_event(responses_usage())
        for ending in (b'\r\n', b'\n'):
            stream = b'event: response.completed' + ending + b'data: ' + payload + ending + ending
            for split in range(len(stream) + 1):
                tracker = isolation.SSEUsageTracker()
                tracker.feed(stream[:split])
                tracker.feed(stream[split:])
                self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))
        stream = b'data: ' + payload + b'\r\n\r\n'
        tracker = isolation.SSEUsageTracker()
        tracker.feed(stream[:-2])
        self.assertEqual((tracker.usage, tracker.outcome), (None, None))
        tracker.feed(stream[-2:-1])
        self.assertEqual((tracker.usage, tracker.outcome), (responses_usage(), 'completed'))
        tracker.feed(stream[-1:])
        self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))
        self.assertFalse(tracker.skip_lf)

    def test_supported_bytes_like_inputs_and_empty_chunks(self):
        payload = self.terminal_event(responses_usage())
        stream = b'event: response.completed\r\ndata: ' + payload + b'\r\n\r\n'
        for factory in (bytes, bytearray, memoryview):
            tracker = isolation.SSEUsageTracker()
            for start in range(0, len(stream), 7):
                tracker.feed(factory(b''))
                tracker.feed(factory(stream[start:start + 7]))
            self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))

    def test_duplicate_terminal_replacement_and_missing_usage(self):
        first, second = responses_usage(), responses_usage(200, 10, 50, 5, 250)
        tracker = isolation.SSEUsageTracker()
        for event in (self.terminal_event(first),
                      self.terminal_event(second, 'response.incomplete')):
            tracker.feed(b'data: ' + event + b'\n\n')
        self.assertEqual((tracker.usage, tracker.outcome), (second, 'incomplete'))
        tracker.feed(b'data: ' + self.terminal_event() + b'\n\n')
        self.assertEqual((tracker.usage, tracker.outcome), (second, 'completed'))
        bare = isolation.SSEUsageTracker()
        bare.feed(b'data: ' + self.terminal_event() + b'\n\n')
        self.assertEqual(bare.finish(), (None, 'completed'))

    def test_event_cap_boundaries_accept_exactly_at_cap_and_discard_one_over(self):
        cap = 512
        event = self.terminal_event(responses_usage())
        # The retained line, including the "data: " prefix, may fill the
        # per-line cap exactly; one more byte discards the event mid-line.
        pad = cap - 6 - len(event)
        tracker = isolation.SSEUsageTracker(max_event_bytes=cap)
        tracker.feed(b'data: ' + event + b' ' * pad + b'\r\n\r\n')
        self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))
        tracker = isolation.SSEUsageTracker(max_event_bytes=cap)
        tracker.feed(b'data: ' + event + b' ' * (pad + 1) + b'\r\n\r\n')
        self.assertEqual(tracker.finish(), (None, None))
        # Later lines are bounded by the remaining event budget, including
        # their own prefix. A line that exactly fills the remainder is kept;
        # one more byte discards the whole event and recovery stays possible.
        first_pad = 400 - 6 - len(event)
        tracker = isolation.SSEUsageTracker(max_event_bytes=cap)
        tracker.feed(b'data: ' + event + b' ' * first_pad + b'\n')
        remaining_line = cap - tracker.event_bytes - 6
        tracker.feed(b'data: ' + b' ' * remaining_line + b'\n')
        self.assertEqual(tracker.event_bytes, cap - 5)
        tracker.feed(b'\n')
        self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))
        tracker = isolation.SSEUsageTracker(max_event_bytes=cap)
        tracker.feed(b'data: ' + event + b' ' * first_pad + b'\n')
        tracker.feed(b'data: ' + b' ' * (remaining_line + 1) + b'\n')
        self.assertTrue(tracker.overflow)
        tracker.feed(b'\n')
        self.assertEqual(tracker.finish(), (None, None))

    def test_oversized_event_recovery_across_crlf_chunk_splits(self):
        cap = 512
        oversized = b'data: ' + b'x' * 1024 + b'\r\n\r\n'
        terminal = b'data: ' + self.terminal_event(responses_usage()) + b'\r\n\r\n'
        boundary = oversized.index(b'\r\n')
        for split in (boundary, boundary + 1, boundary + 2, boundary + 3):
            tracker = isolation.SSEUsageTracker(max_event_bytes=cap)
            stream = oversized + terminal
            tracker.feed(stream[:split])
            tracker.feed(stream[split:])
            self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))

    def test_overflow_retains_bounded_bytes_and_recovers_on_blank_line(self):
        cap = 512
        tracker = isolation.SSEUsageTracker(max_event_bytes=cap)
        tracker.feed(b'data: ' + b'x' * 10000)
        self.assertTrue(tracker.overflow)
        self.assertLessEqual(len(tracker.line), cap)
        self.assertEqual(tracker.data_lines, [])
        tracker.feed(b'\r\n\r\n')
        self.assertFalse(tracker.overflow)
        terminal = b'data: ' + self.terminal_event(responses_usage()) + b'\n\n'
        tracker.feed(terminal)
        self.assertLessEqual(sum(len(value) + 1 for value in tracker.data_lines), cap)
        self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))

    def test_bulk_scan_keeps_multiline_ignored_and_done_semantics(self):
        usage = responses_usage()
        event = json.dumps({'type': 'response.completed', 'response': {'usage': usage}},
                           indent=2).splitlines()
        stream = (b': keep-alive\r\n'
                  b'event: response.completed\r\n'
                  b'id: 7\r\n'
                  + b'\r\n'.join(b'data: ' + line.encode() for line in event)
                  + b'\r\n\r\n'
                  b'data: [DONE]\r\n\r\n'
                  b'data without colon\r\n\r\n')
        tracker = isolation.SSEUsageTracker()
        for start in range(0, len(stream), 3):
            tracker.feed(stream[start:start + 3])
        self.assertEqual(tracker.finish(), (usage, 'completed'))

    def test_dense_delimiter_and_comment_runs_keep_terminal_visible(self):
        terminal = b'data: ' + self.terminal_event(responses_usage()) + b'\r\n\r\n'
        fillers = (b'\n' * 65536, b'\r' * 65536, b'\r\n' * 32768,
                   b': comment\n' * 6553, b': comment\r\n' * 5957)
        for filler in fillers:
            stream = filler + terminal
            tracker = isolation.SSEUsageTracker()
            for start in range(0, len(stream), 65536):
                tracker.feed(stream[start:start + 65536])
            self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))
            self.assertEqual(tracker.data_lines, [])

    def test_dense_mixed_delimiters_and_split_crlf_recover_after_overflow(self):
        cap = 4096
        oversized = b'data: ' + b'x' * (cap + 1) + b'\r\n\r\n'
        dense = (b': c\n' * 3000) + (b'\r\n' * 2000) + b'\r\n'
        terminal = b'data: ' + self.terminal_event(responses_usage()) + b'\r\n\r\n'
        stream = oversized + dense + terminal
        split = stream.index(b'\r\n') + 1
        for factory in (bytes, bytearray, memoryview):
            tracker = isolation.SSEUsageTracker(max_event_bytes=cap)
            tracker.feed(factory(stream[:split]))
            tracker.feed(factory(b''))
            tracker.feed(factory(stream[split:]))
            self.assertEqual(tracker.finish(), (responses_usage(), 'completed'))


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


class FailureMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'usage.json'

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, ledger):
        return next(iter(json.loads(ledger.path.read_text())['requests'].values()))

    def test_failed_request_persists_only_complete_allowlisted_metadata(self):
        ledger = usage.UsageLedger(self.path)
        request = ledger.begin('work')
        ledger.finish(request, responses_usage(), status='failed', latency_ms=12.5,
                      error_kind='transport', upstream_request_id='req-1',
                      failure_stage='upstream_read', failure_type='timeout')
        entry = self.entry(ledger)
        self.assertEqual((entry['status'], entry['error_kind'], entry['latency_ms'],
                          entry['upstream_request_id'], entry['failure_stage'],
                          entry['failure_type']),
                         ('failed', 'transport', 12.5, 'req-1', 'upstream_read', 'timeout'))
        self.assertEqual(entry['usage']['input_tokens'], 100)

        invalid = (
            {'failure_stage': 'upstream_read'},
            {'failure_type': 'timeout'},
            {'failure_stage': 'other', 'failure_type': 'timeout'},
            {'failure_stage': 'relay', 'failure_type': 'network'},
            {'failure_stage': 1, 'failure_type': 'timeout'},
            {'failure_stage': 'relay', 'failure_type': 1},
        )
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                request = ledger.begin('work')
                ledger.finish(request, None, status='failed', **metadata)
                stored = json.loads(ledger.path.read_text())['requests'][request]
                self.assertNotIn('failure_stage', stored)
                self.assertNotIn('failure_type', stored)

        completed = ledger.begin('work')
        ledger.finish(completed, responses_usage(), failure_stage='relay', failure_type='other')
        stored = json.loads(ledger.path.read_text())['requests'][completed]
        self.assertNotIn('failure_stage', stored)
        self.assertNotIn('failure_type', stored)

    def test_first_finish_wins_and_legacy_calls_are_unchanged(self):
        ledger = usage.UsageLedger(self.path)
        request = ledger.begin('work')
        ledger.finish(request, None, status='failed', failure_stage='relay',
                      failure_type='other')
        before = self.entry(ledger)
        ledger.finish(request, responses_usage(), status='completed',
                      failure_stage='response_validation', failure_type='value_error')
        self.assertEqual(self.entry(ledger), before)

        legacy = ledger.begin('work')
        ledger.finish(legacy, responses_usage())
        self.assertNotIn('failure_stage', json.loads(self.path.read_text())['requests'][legacy])


class PeerBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'usage.json'
        self.peer_paths = 0
        self.state = {'session_id': 'a' * 32, 'job_id': 'job-1'}
        self.contract = {'executor': {'provider': 'ark', 'model': 'm', 'reasoning_effort': 'max',
                                      'base_url': 'http://127.0.0.1:9/v1', 'http_timeout': 1}}
        self.binding = self.contract['executor']

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def fake_relay(self, *args, **kwargs):
        yield 'http://127.0.0.1:9/v1', 'token', 9

    def run_peer(self, ledger, finish_usage, isolated_fn=None, relay_factory=None,
                 credential=('KEY', 'env')):
        def isolated(*args, **kwargs):
            request = ledger.begin('work', provider='ark', model='m', job='job-1')
            ledger.finish(request, finish_usage)
            return {'thread_id': 't', 'summary': 'ok', 'usage': None}
        with patch.object(telos, 'backend_bindings', return_value={'executor': self.binding}), \
                patch.object(telos, 'provider_credential', return_value=credential), \
                patch.object(telos, 'usage_ledger', return_value=ledger), \
                patch.object(telos, 'isolated_peer_turn', side_effect=isolated_fn or isolated), \
                patch.object(isolation, 'model_relay', relay_factory or self.fake_relay):
            return telos.peer_turn(self.state, self.contract, 'message')

    def relay_with_diagnostic(self, diagnostic, token='relay-token'):
        @contextlib.contextmanager
        def relay(*args, **kwargs):
            kwargs['diagnostics']['failure'] = diagnostic
            yield 'http://127.0.0.1:9/v1', token, 9
        return relay

    def unknown_with_failure(self, ledger, message='synthetic executor failure'):
        def isolated(*args, **kwargs):
            request = ledger.begin('work', provider='ark', model='m', job='job-1')
            ledger.finish(request, None)
            raise RuntimeError(message)
        return isolated

    def relay_detail(self, message):
        marker = 'Relay diagnostic: '
        start = message.index(marker) + len(marker)
        end = message.index('; Task budget cannot be evaluated', start)
        return message[start:end]

    def run_peer_failure(self, error, *, key='KEY', token='token', budget=None,
                         finish_usage=None, state=None):
        self.peer_paths += 1
        path = self.path.with_name(f'usage-{self.peer_paths}.json')
        ledger = usage.UsageLedger(path, budget)

        def isolated(*args, **kwargs):
            request = ledger.begin('work', provider='ark', model='m', job='job-1')
            ledger.finish(request, finish_usage)
            raise error

        @contextlib.contextmanager
        def relay(*args, **kwargs):
            yield 'http://127.0.0.1:9/v1', token, 9

        with patch.object(telos, 'backend_bindings', return_value={'executor': self.binding}), \
                patch.object(telos, 'provider_credential', return_value=(key, 'env')), \
                patch.object(telos, 'usage_ledger', return_value=ledger), \
                patch.object(telos, 'isolated_peer_turn', side_effect=isolated), \
                patch.object(isolation, 'model_relay', relay):
            try:
                telos.peer_turn(state or self.state, self.contract, 'message')
            except Exception as caught:
                return caught
        self.fail('peer_turn unexpectedly succeeded')

    def test_unknown_usage_fails_the_turn_instead_of_silent_success(self):
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(ledger, None)
        self.assertIn('unknown', str(caught.exception))
        self.assertEqual(caught.exception.snapshot['budget_state']['state'], 'unknown')

    def test_relay_diagnostic_survives_unknown_usage_budget_stop(self):
        self.binding = {**self.binding, 'provider': 'bailian'}
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(ledger, None,
                          isolated_fn=self.unknown_with_failure(ledger),
                          relay_factory=self.relay_with_diagnostic('synthetic relay failure'))
        message = str(caught.exception)
        self.assertIn('synthetic executor failure', message)
        self.assertIn('synthetic relay failure', message)
        self.assertIn('usage is unknown', message)
        self.assertEqual(caught.exception.snapshot['budget_state']['state'], 'unknown')
        self.assertEqual(caught.exception.snapshot['totals']['in_flight_requests'], 0)

    def test_relay_diagnostic_redacts_upstream_and_relay_secrets(self):
        self.binding = {**self.binding, 'provider': 'bailian'}
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(
                ledger, None,
                isolated_fn=self.unknown_with_failure(ledger, 'executor saw UPSTREAM_SECRET'),
                relay_factory=self.relay_with_diagnostic(
                    'relay saw UPSTREAM_SECRET and RELAY_SECRET', token='RELAY_SECRET'),
                credential=('UPSTREAM_SECRET', 'env'))
        message = str(caught.exception)
        self.assertNotIn('UPSTREAM_SECRET', message)
        self.assertNotIn('RELAY_SECRET', message)
        self.assertIn('executor saw [REDACTED]', message)
        self.assertIn('relay saw [REDACTED] and [REDACTED]', message)

    def test_relay_diagnostic_redacts_when_upstream_credential_is_empty(self):
        self.binding = {**self.binding, 'provider': 'bailian'}
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(ledger, None,
                          isolated_fn=self.unknown_with_failure(ledger),
                          relay_factory=self.relay_with_diagnostic(
                              'relay saw RELAY_SECRET', token='RELAY_SECRET'),
                          credential=('', 'local'))
        message = str(caught.exception)
        self.assertNotIn('RELAY_SECRET', message)
        self.assertIn('relay saw [REDACTED]', message)
        self.assertIn('synthetic executor failure', message)

    def test_relay_diagnostic_redacts_before_capping_a_cutoff_secret(self):
        self.binding = {**self.binding, 'provider': 'bailian'}
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(
                ledger, None,
                isolated_fn=self.unknown_with_failure(ledger),
                relay_factory=self.relay_with_diagnostic('A' * 598 + 'TOPSECRET'),
                credential=('TOPSECRET', 'env'))
        detail = self.relay_detail(str(caught.exception))
        self.assertEqual(len(detail), 600)
        self.assertEqual(detail[:598], 'A' * 598)
        self.assertNotIn('TO', detail[598:])

    def test_relay_diagnostic_cap_applies_after_secret_expansion(self):
        self.binding = {**self.binding, 'provider': 'bailian'}
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(ledger, None,
                          isolated_fn=self.unknown_with_failure(ledger),
                          relay_factory=self.relay_with_diagnostic('K' * 2000),
                          credential=('K', 'env'))
        detail = self.relay_detail(str(caught.exception))
        self.assertEqual(detail, '[REDACTED]' * 60)

    def test_relay_diagnostic_survives_accounting_errors(self):
        self.binding = {**self.binding, 'provider': 'bailian'}
        for error_type in (OSError, ValueError):
            with self.subTest(error_type=error_type.__name__):
                ledger = usage.UsageLedger(self.path.with_name(error_type.__name__ + '.json'))
                with patch.object(ledger, 'ensure_known',
                                  side_effect=error_type('ledger unreadable')):
                    with self.assertRaises(telos.GateError) as caught:
                        self.run_peer(
                            ledger, responses_usage(),
                            relay_factory=self.relay_with_diagnostic('synthetic relay failure'))
                message = str(caught.exception)
                self.assertIn('synthetic relay failure', message)
                self.assertIn('Usage accounting unavailable', message)
                self.assertIn('ledger unreadable', message)

    def test_unknown_usage_without_relay_diagnostic_keeps_legacy_message(self):
        ledger = usage.UsageLedger(self.path, {'max_input_tokens': 10})
        with self.assertRaises(usage.BudgetExceeded) as caught:
            self.run_peer(ledger, None)
        message = str(caught.exception)
        self.assertTrue(message.startswith(
            'Task budget cannot be evaluated because usage is unknown'))
        self.assertNotIn('Relay diagnostic', message)

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

    def test_failure_origin_redacts_upstream_and_relay_credentials_before_cutoff(self):
        upstream = 'UPSTREAM_CREDENTIAL_12345'
        relay = 'RELAY_CREDENTIAL_98765'
        for label, credential, key, token in (
                ('upstream', upstream, upstream, 'relay-token'),
                ('relay', relay, 'UPSTREAM_KEY', relay)):
            with self.subTest(label=label):
                caught = self.run_peer_failure(
                    RuntimeError('a' * 1190 + credential + ' trailing'), key=key, token=token,
                    budget={'max_total_tokens': 100})
                message = str(caught)
                prefix = message[:1202]
                self.assertEqual(prefix[:1190], 'a' * 1190)
                self.assertEqual(prefix[1190:], '[REDACTED]; ')
                self.assertNotIn(credential[:12], prefix)
                self.assertIn('Task budget cannot be evaluated', message[1202:])

    def test_failure_origin_caps_replacement_expansion_after_redaction(self):
        caught = self.run_peer_failure(
            RuntimeError('x' * 1300), key='x', budget={'max_total_tokens': 100})
        message = str(caught)
        self.assertEqual(message[:1202], '[REDACTED]' * 120 + '; ')
        self.assertNotIn('x', message[:1202])
        self.assertIn('Task budget cannot be evaluated', message[1202:])

    def test_failure_origin_skips_empty_credentials_and_keeps_short_text(self):
        caught = self.run_peer_failure(RuntimeError('actionable failure RELAY_TOKEN'),
                                       key='', token='RELAY_TOKEN',
                                       budget={'max_total_tokens': 100})
        self.assertTrue(str(caught).startswith('actionable failure [REDACTED]; '))
        caught = self.run_peer_failure(RuntimeError('actionable failure'), key='', token='',
                                       budget={'max_total_tokens': 100})
        self.assertTrue(str(caught).startswith('actionable failure; '))
        caught = self.run_peer_failure(RuntimeError('actionable failure'),
                                       budget={'max_total_tokens': 100})
        self.assertTrue(str(caught).startswith('actionable failure; '))

    def test_unknown_budget_preserves_timeout_origin_thread_and_snapshot(self):
        failure = telos.providers.ExecutionTimeout('Execution timed out; UPSTREAM_SECRET RELAY_TOKEN')
        failure.thread_id = 'thread-42'
        caught = self.run_peer_failure(
            failure, key='UPSTREAM_SECRET', token='RELAY_TOKEN',
            budget={'max_total_tokens': 100},
            state={'session_id': 'b' * 32, 'job_id': 'job-2'})
        self.assertIsInstance(caught, usage.BudgetExceeded)
        self.assertIn('Execution timed out', str(caught))
        self.assertNotIn('UPSTREAM_SECRET', str(caught))
        self.assertNotIn('RELAY_TOKEN', str(caught))
        self.assertEqual(caught.origin_kind, 'execution_timeout')
        self.assertEqual(caught.thread_id, 'thread-42')
        self.assertEqual(caught.snapshot['budget_state']['state'], 'unknown')
        self.assertEqual(caught.snapshot['totals']['in_flight_requests'], 0)

    def test_ledger_read_failure_is_not_ignored_after_a_turn(self):
        ledger = usage.UsageLedger(self.path)
        with patch.object(ledger, 'ensure_known', side_effect=OSError('ledger unreadable')):
            with self.assertRaises(telos.GateError) as caught:
                self.run_peer(ledger, {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})
        self.assertIn('Usage accounting unavailable', str(caught.exception))

    def test_relay_settles_before_usage_check_and_keeps_original_timeout(self):
        for settle in (True, False):
            with self.subTest(settle=settle):
                path = self.path.with_name(str(settle) + '.json')
                ledger = usage.UsageLedger(path, {'max_total_tokens': 1000})
                request_id = None

                def interrupted(*args, **kwargs):
                    nonlocal request_id
                    request_id = ledger.begin('work')
                    raise telos.providers.ExecutionTimeout('Execution model timed out; KEY')

                @contextlib.contextmanager
                def draining(*args, **kwargs):
                    try:
                        yield 'http://127.0.0.1:9/v1', 'relay-token', 9
                    finally:
                        ledger.finish(request_id, responses_usage() if settle else None,
                                      status='failed', error_kind='transport')

                with patch.object(telos, 'backend_bindings', return_value={'executor': self.binding}), \
                        patch.object(telos, 'provider_credential', return_value=('KEY', 'env')), \
                        patch.object(telos, 'usage_ledger', return_value=ledger), \
                        patch.object(telos, 'isolated_peer_turn', side_effect=interrupted), \
                        patch.object(isolation, 'model_relay', draining):
                    expected = telos.providers.ExecutionTimeout if settle else usage.BudgetExceeded
                    with self.assertRaises(expected) as caught:
                        telos.peer_turn(self.state, self.contract, 'message')
                self.assertIn('timed out', str(caught.exception))
                if not settle:
                    self.assertNotIn('KEY', str(caught.exception))
                    self.assertEqual(caught.exception.origin_kind, 'execution_timeout')
                    self.assertEqual(caught.exception.snapshot['totals']['in_flight_requests'], 0)
                else:
                    self.assertEqual(ledger.snapshot()['totals']['total_tokens'], 120)


class ExamReserveTests(unittest.TestCase):
    def test_reserve_is_frozen_shared_by_repairs_and_available_to_exams(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'usage.json'
            budget = {'max_requests': 4, 'exam_reserve_requests': 2,
                      'max_total_tokens': 500, 'exam_reserve_tokens': 200}
            ledger = usage.UsageLedger(path, budget)
            for phase in ('work', 'repair'):
                ledger.finish(ledger.begin(phase), responses_usage())
            with self.assertRaisesRegex(usage.BudgetExceeded, 'reserved for the exam'):
                ledger.ensure_available('work')
            # A reopened ledger cannot reset either the work allowance or reserve.
            reopened = usage.UsageLedger(path, budget)
            with self.assertRaises(usage.BudgetExceeded):
                reopened.begin('repair')
            for _ in range(2):
                reopened.finish(reopened.begin('exam'), responses_usage())
            self.assertEqual(reopened.snapshot()['totals']['total_tokens'], 480)
            with self.assertRaises(usage.BudgetExceeded):
                reopened.begin('exam')
            with self.assertRaises(ValueError):
                usage.UsageLedger(path, {**budget, 'exam_reserve_requests': 1})

    def test_token_reserve_blocks_more_work_without_inventing_a_hard_token_cap(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = usage.UsageLedger(Path(root) / 'usage.json',
                                      {'max_total_tokens': 200, 'exam_reserve_tokens': 100})
            ledger.finish(ledger.begin('work'), responses_usage())  # observed overshoot: 120 > 100
            with self.assertRaises(usage.BudgetExceeded):
                ledger.begin('work')
            ledger.finish(ledger.begin('exam'), responses_usage(30, 0, 10, 0, 40))
            self.assertEqual(ledger.snapshot()['totals']['total_tokens'], 160)

    def test_invalid_reserves_and_unknown_usage_cannot_bypass_total_caps(self):
        for budget in ({'exam_reserve_requests': 1}, {'max_requests': 2, 'exam_reserve_requests': 2},
                       {'max_total_tokens': 5, 'exam_reserve_tokens': 6},
                       {'max_requests': 2, 'exam_reserve_requests': False}):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                usage.validate_budget(budget)
        with tempfile.TemporaryDirectory() as root:
            ledger = usage.UsageLedger(Path(root) / 'usage.json',
                                      {'max_requests': 3, 'exam_reserve_requests': 1, 'max_total_tokens': 10})
            first = ledger.begin('work')
            ledger.finish(first, None, status='failed')
            # Unknown data must also be reported when a different known limit is exhausted.
            with self.assertRaises(usage.BudgetExceeded):
                ledger.begin('exam')
            with self.assertRaises(usage.BudgetExceeded):
                ledger.ensure_known()
            exhausted = usage.UsageLedger(Path(root) / 'exhausted.json',
                                          {'max_requests': 1, 'max_total_tokens': 10})
            exhausted.finish(exhausted.begin('work'), None, status='failed')
            self.assertEqual(exhausted.snapshot()['budget_state']['state'], 'exceeded')
            with self.assertRaises(usage.BudgetExceeded):
                exhausted.ensure_known()


@contextlib.contextmanager
def streaming_upstream(emit):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('x-request-id', 'fixture-upstream-12345')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                emit(self.wfile, len(seen))
            except (OSError, ValueError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=2)


class RelayCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = usage.UsageLedger(Path(self.temp.name) / 'usage.json', {'max_total_tokens': 1000})

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def terminal():
        event = {'type': 'response.completed', 'response': {'status': 'completed', 'usage': responses_usage()}}
        return b'data: ' + json.dumps(event).encode() + b'\n\n'

    @staticmethod
    def disconnect(relay):
        _, token, port = relay
        sock = socket.create_connection(('127.0.0.1', port), timeout=3)
        payload = b'{"model":"m"}'
        sock.sendall((f'POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n'
                      f'Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n').encode() + payload)
        sock.recv(4096)  # Real HTTP headers have arrived; the upstream call is in flight.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        sock.close()

    def test_admission_deadline_does_not_reserve_or_send_a_request(self):
        def reply(request, n):
            return 200, 'application/json', b'{}'
        with upstream(reply) as (port, seen):
            closeout = isolation.RelayCloseout(duration=0, grace=0.05)
            with isolation.model_relay(f'http://127.0.0.1:{port}', '', usage_ledger=self.ledger,
                                       closeout=closeout) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay)
            self.assertEqual(seen, [])
        self.assertEqual(self.ledger.snapshot()['totals']['requests'], 0)

    def test_deadline_cannot_be_reset_or_reopen_closeout(self):
        closeout = isolation.RelayCloseout()
        closeout.start(10)
        with self.assertRaises(RuntimeError):
            closeout.start(20)
        closed = isolation.RelayCloseout()
        closed.stop('finished')
        with self.assertRaises(RuntimeError):
            closed.start(10)

    def test_http_failure_keeps_safe_provider_request_id_and_reported_usage(self):
        def reply(request, n):
            return 429, 'application/json', json.dumps({'usage': responses_usage()}).encode()

        with upstream(reply, request_id='failed-upstream-12345') as (port, seen):
            with isolation.model_relay(f'http://127.0.0.1:{port}', '', usage_ledger=self.ledger) as relay:
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay)
        entry = next(iter(json.loads(self.ledger.path.read_text())['requests'].values()))
        self.assertEqual(entry['upstream_request_id'], 'failed-upstream-12345')
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(entry['usage']['total_tokens'], 120)

    def test_disconnect_drains_terminal_usage_and_blocks_new_paid_requests(self):
        release = threading.Event()
        done = threading.Event()
        terminal = self.terminal()

        def emit(stream, n):
            stream.write(b': first\n\n'); stream.flush()
            release.wait(2)
            stream.write(b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'); stream.flush()
            time.sleep(0.05)
            stream.write(terminal); stream.flush()
            done.set()

        with streaming_upstream(emit) as (port, seen):
            closeout = isolation.RelayCloseout(grace=0.5)
            with isolation.model_relay(f'http://127.0.0.1:{port}', '', timeout=2,
                                       usage_ledger=self.ledger, closeout=closeout) as relay:
                self.disconnect(relay)
                release.set()
                self.assertTrue(done.wait(2))
                # drain() also waits for the handler to commit the receipt.
                closeout.drain()
                with self.assertRaises(urllib.error.HTTPError):
                    post(relay)
            self.assertEqual(len(seen), 1)
        totals = self.ledger.snapshot()['totals']
        self.assertEqual(totals['total_tokens'], 120)
        self.assertEqual(totals['unknown_usage_requests'], 0)
        self.assertEqual(totals['in_flight_requests'], 0)
        entry = next(iter(json.loads(self.ledger.path.read_text())['requests'].values()))
        self.assertEqual(entry['upstream_request_id'], 'fixture-upstream-12345')

    def test_hanging_stream_has_bounded_closeout_and_remains_unknown(self):
        release = threading.Event()

        def emit(stream, n):
            stream.write(b': no terminal yet\n\n'); stream.flush()
            release.wait(3)

        try:
            with streaming_upstream(emit) as (port, seen):
                closeout = isolation.RelayCloseout(grace=0.05)
                started = time.monotonic()
                with isolation.model_relay(f'http://127.0.0.1:{port}', '', timeout=2,
                                           usage_ledger=self.ledger, closeout=closeout) as relay:
                    self.disconnect(relay)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(len(seen), 1)
                totals = self.ledger.snapshot()['totals']
                self.assertEqual(totals['in_flight_requests'], 0)
                self.assertEqual(totals['unknown_usage_requests'], 1)
                self.assertEqual(totals['failed_requests'], 1)
                with self.assertRaises(usage.BudgetExceeded):
                    self.ledger.ensure_available()
        finally:
            release.set()


class _RelayPolicySpy:
    MAX_RESPONSE_BYTES = 16 * 1024 * 1024

    def __init__(self):
        self.prepared = []
        self.failure = None
        self.transport_error = None

    def prepare(self, raw):
        self.prepared.append(raw)
        return raw

    def validate_response(self, raw, content_type):
        raise AssertionError('response validation must not run before admission')


class _RelayOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError('rejected admission must not reach upstream transport')
        return self.response


class _JsonResponse:
    status = 200

    def __init__(self, payload=None):
        self.headers = Message()
        self.headers['Content-Type'] = 'application/json'
        self.payload = json.dumps(payload or {
            'status': 'completed', 'usage': responses_usage()}).encode()

    def read(self, amount=-1):
        return self.payload if amount < 0 else self.payload[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _RaisingCloseout:
    def __init__(self, error_type=TimeoutError, message='admission rejected'):
        self.error_type = error_type
        self.message = message
        self.begin_calls = 0
        self.drained = False

    def begin(self, interrupt):
        self.begin_calls += 1
        raise self.error_type(self.message)

    def end(self, lease):
        pass

    def drain(self):
        self.drained = True


class RelayAdmissionDiagnosticTests(unittest.TestCase):
    """Socket-free diagnostics for the real model_relay Handler.do_POST."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'usage.json'
        self.ledger = usage.UsageLedger(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def relay(self, *, ledger=None, opener=None, key='UPSTREAM_SECRET', **kwargs):
        captured = {}

        class FakeServer:
            server_port = 9

            def __init__(self, address, handler):
                captured['handler'] = handler

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        opener = opener or _RelayOpener()
        arguments = {'usage_ledger': ledger or self.ledger,
                     'usage_context': {'phase': 'exam'}}
        arguments.update(kwargs)
        with patch.object(isolation, 'ThreadingHTTPServer', FakeServer), \
                patch.object(isolation.urllib.request, 'build_opener',
                             lambda *args, **options: opener):
            with isolation.model_relay('http://127.0.0.1:9', key, **arguments) as relay:
                yield captured['handler'], relay, opener

    def invoke(self, handler_class, relay, *, path='/v1/responses', payload=None,
               authorization=None):
        raw = json.dumps(payload if payload is not None else {}).encode()
        handler = handler_class.__new__(handler_class)
        handler.headers = Message()
        handler.headers['Authorization'] = authorization or 'Bearer ' + relay[1]
        handler.headers['Content-Length'] = str(len(raw))
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        handler.path = path
        handler.request_version = 'HTTP/1.1'
        handler.command = 'POST'
        handler.close_connection = False
        handler._headers_buffer = []
        handler.requestline = f'POST {path} HTTP/1.1'
        handler.client_address = ('127.0.0.1', 0)
        handler.do_POST()
        response = handler.wfile.getvalue()
        head, _, body = response.partition(b'\r\n\r\n')
        return handler, int(head.split(b' ', 2)[1]), body

    def test_expired_admission_returns_fixed_message_without_reservation_or_transport(self):
        policy = _RelayPolicySpy()
        diagnostics = {}
        closeout = isolation.RelayCloseout(duration=0, grace=0.05)
        with self.relay(policy=policy, diagnostics=diagnostics,
                        closeout=closeout) as (handler_class, relay, opener):
            handler, status, body = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        self.assertIn(b'Execution admission closed; no upstream request was sent', body)
        self.assertNotIn(b'Model protocol or frozen endpoint validation failed', body)
        self.assertTrue(handler.close_connection)
        self.assertEqual(opener.calls, [])
        totals = self.ledger.snapshot()['totals']
        self.assertEqual((totals['requests'], totals['in_flight_requests']), (0, 0))
        self.assertEqual(closeout.reason, 'execution_deadline')
        self.assertEqual(closeout.active, {})
        self.assertEqual(len(policy.prepared), 1)
        self.assertIn('Execution admission deadline reached', policy.failure)
        self.assertEqual(policy.transport_error, 'Exam endpoint transport failed')
        self.assertIn('Execution admission deadline reached', diagnostics['failure'])

    def test_closing_and_repeated_admission_are_rejected_without_accounting(self):
        closeout = isolation.RelayCloseout()
        closeout.stop('execution_finished')
        with self.relay(closeout=closeout) as (handler_class, relay, opener):
            first = self.invoke(handler_class, relay)
            second = self.invoke(handler_class, relay)
        for handler, status, body in (first, second):
            self.assertEqual(status, 400)
            self.assertIn(b'Execution admission closed; no upstream request was sent', body)
            self.assertNotIn(b'Model protocol or frozen endpoint validation failed', body)
            self.assertTrue(handler.close_connection)
        self.assertEqual(opener.calls, [])
        totals = self.ledger.snapshot()['totals']
        self.assertEqual((totals['requests'], totals['in_flight_requests']), (0, 0))
        self.assertEqual(closeout.reason, 'execution_finished')
        self.assertEqual(closeout.active, {})

    def test_admission_rejection_never_copies_exception_text_or_credentials_publicly(self):
        closeout = _RaisingCloseout()
        diagnostics = {}
        with self.relay(closeout=closeout,
                        diagnostics=diagnostics) as (handler_class, relay, opener):
            closeout.message = (relay[1] + ' UPSTREAM_SECRET '
                                + 'credential-detail ' * 200)
            _, status, body = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        self.assertIn(b'Execution admission closed; no upstream request was sent', body)
        for value in (relay[1].encode(), b'UPSTREAM_SECRET', b'credential-detail'):
            self.assertNotIn(value, body)
        self.assertLessEqual(len(diagnostics['failure']), 600)
        self.assertNotIn(relay[1], diagnostics['failure'])
        self.assertNotIn('UPSTREAM_SECRET', diagnostics['failure'])
        self.assertEqual(closeout.begin_calls, 1)
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.ledger.snapshot()['totals']['requests'], 0)

    def test_validation_timeout_before_admission_keeps_generic_protocol_error(self):
        diagnostics = {}
        closeout = isolation.RelayCloseout(duration=60, request_window=5)

        def validate():
            raise TimeoutError('validation timed out')

        with self.relay(validate=validate, closeout=closeout,
                        diagnostics=diagnostics) as (handler_class, relay, opener):
            handler, status, body = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        self.assertIn(b'Model protocol or frozen endpoint validation failed', body)
        self.assertNotIn(b'Execution admission closed', body)
        self.assertTrue(handler.close_connection)
        self.assertEqual(diagnostics['failure'], 'validation timed out')
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.ledger.snapshot()['totals']['requests'], 0)
        self.assertEqual(closeout.active, {})

    def test_policy_preparation_timeout_before_admission_keeps_generic_protocol_error(self):
        class TimeoutPreparingPolicy(_RelayPolicySpy):
            def prepare(self, raw):
                raise TimeoutError('policy preparation timed out')

        policy = TimeoutPreparingPolicy()
        diagnostics = {}
        with self.relay(policy=policy,
                        diagnostics=diagnostics) as (handler_class, relay, opener):
            handler, status, body = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        self.assertIn(b'Model protocol or frozen endpoint validation failed', body)
        self.assertNotIn(b'Execution admission closed', body)
        self.assertEqual(policy.failure, 'policy preparation timed out')
        self.assertEqual(policy.transport_error, 'Exam endpoint transport failed')
        self.assertEqual(diagnostics['failure'], 'policy preparation timed out')
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.ledger.snapshot()['totals']['requests'], 0)
        self.assertTrue(handler.close_connection)

    def test_non_timeout_from_admission_keeps_generic_protocol_error(self):
        closeout = _RaisingCloseout(RuntimeError, 'closeout setup failed')
        policy = _RelayPolicySpy()
        diagnostics = {}
        with self.relay(closeout=closeout, policy=policy,
                        diagnostics=diagnostics) as (handler_class, relay, opener):
            handler, status, body = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        self.assertIn(b'Model protocol or frozen endpoint validation failed', body)
        self.assertNotIn(b'Execution admission closed', body)
        self.assertEqual(policy.failure, 'closeout setup failed')
        self.assertIsNone(policy.transport_error)
        self.assertEqual(diagnostics['failure'], 'closeout setup failed')
        self.assertEqual(closeout.begin_calls, 1)
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.ledger.snapshot()['totals']['requests'], 0)
        self.assertTrue(handler.close_connection)

    def test_timeout_after_reservation_keeps_protocol_failure_and_accounts_request(self):
        closeout = isolation.RelayCloseout(duration=60, request_window=5)
        policy = _RelayPolicySpy()
        diagnostics = {}
        with self.relay(closeout=closeout, policy=policy,
                        diagnostics=diagnostics) as (handler_class, relay, opener):
            with patch.object(closeout, 'timeout',
                              side_effect=TimeoutError('Execution closeout deadline reached')):
                handler, status, body = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        self.assertIn(b'Model protocol or frozen endpoint validation failed', body)
        self.assertNotIn(b'Execution admission closed', body)
        totals = self.ledger.snapshot()['totals']
        self.assertEqual(totals['requests'], 1)
        self.assertEqual(totals['failed_requests'], 1)
        self.assertEqual(totals['unknown_usage_requests'], 1)
        self.assertEqual(totals['in_flight_requests'], 0)
        self.assertEqual(totals['unresolved_requests'], 0)
        self.assertEqual(closeout.active, {})
        self.assertEqual(policy.transport_error, 'Exam endpoint transport failed')
        self.assertIn('Execution closeout deadline reached', policy.failure)
        self.assertEqual(opener.calls, [])
        self.assertTrue(handler.close_connection)

    def test_auth_path_and_budget_short_circuits_never_reach_admission(self):
        closeout = _RaisingCloseout(AssertionError, 'admission must not run')
        budget_failure = {'message': 'Task budget exhausted; no upstream request was sent'}
        with self.relay(closeout=closeout,
                        budget_failure=budget_failure) as (handler_class, relay, opener):
            _, auth_status, _ = self.invoke(handler_class, relay,
                                            authorization='Bearer wrong-token')
            _, path_status, _ = self.invoke(handler_class, relay, path='/v1/not-allowed')
            _, budget_status, budget_body = self.invoke(handler_class, relay)
        self.assertEqual((auth_status, path_status, budget_status), (403, 404, 400))
        self.assertIn(b'Task budget or usage accounting stop; no upstream request was sent',
                      budget_body)
        self.assertEqual(closeout.begin_calls, 0)
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.ledger.snapshot()['totals']['requests'], 0)

    def test_completed_response_keeps_success_on_responses_and_compact_routes(self):
        for index, path in enumerate(('/v1/responses', '/v1/responses/compact')):
            with self.subTest(path=path):
                opener = _RelayOpener(_JsonResponse())
                ledger = usage.UsageLedger(Path(self.temp.name) / f'success-{index}.json')
                with self.relay(ledger=ledger, opener=opener) as (handler_class, relay, unused):
                    handler, status, body = self.invoke(handler_class, relay, path=path,
                                                        payload={'model': 'm'})
                self.assertEqual(status, 200)
                self.assertIn(b'"status": "completed"', body)
                self.assertEqual(len(opener.calls), 1)
                totals = ledger.snapshot()['totals']
                self.assertEqual((totals['requests'], totals['failed_requests']), (1, 0))
                self.assertEqual(totals['total_tokens'], 120)

    def test_provider_http_failure_keeps_existing_status_and_public_error(self):
        headers = Message()
        headers['Content-Type'] = 'application/json'
        error = urllib.error.HTTPError('http://127.0.0.1:9/v1/responses', 503,
                                       'provider failure', headers, io.BytesIO(b'{}'))
        policy = _RelayPolicySpy()
        with self.relay(opener=_RelayOpener(error=error),
                        policy=policy) as (handler_class, relay, opener):
            handler, status, body = self.invoke(handler_class, relay)
        self.assertEqual(status, 503)
        self.assertIn(b'Model endpoint rejected request', body)
        self.assertNotIn(b'Execution admission closed', body)
        self.assertEqual(policy.transport_error, 'Exam endpoint HTTP 503')
        self.assertEqual(self.ledger.snapshot()['totals']['failed_requests'], 1)
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(handler.close_connection)



    def test_upstream_open_exceptions_record_safe_stage_and_type(self):
        cases = (
            (TimeoutError('secret-message'), 'timeout'),
            (urllib.error.URLError(TimeoutError('secret-message')), 'timeout'),
            (ssl.SSLError('secret-message'), 'tls'),
            (ConnectionRefusedError('secret-message'), 'connection'),
            (OSError('secret-message'), 'os_error'),
            (ValueError('secret-message'), 'value_error'),
            (RuntimeError('secret-message'), 'other'),
        )
        for index, (error, expected) in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                path = Path(self.temp.name) / f'open-{index}.json'
                ledger = usage.UsageLedger(path)
                opener = _RelayOpener(error=error)
                with self.relay(ledger=ledger, opener=opener) as (handler_class, relay, unused):
                    _, status, _ = self.invoke(handler_class, relay)
                self.assertEqual(status, 400)
                entry = next(iter(json.loads(path.read_text())['requests'].values()))
                self.assertEqual((entry['status'], entry['failure_stage'], entry['failure_type']),
                                 ('failed', 'upstream_open', expected))
                self.assertNotIn('secret-message', json.dumps(entry))

    def test_upstream_read_exception_records_read_stage(self):
        class BrokenRead:
            status = 200
            headers = Message()
            headers['Content-Type'] = 'application/json'

            def read(self, amount=-1):
                raise ValueError('secret-message')

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        opener = _RelayOpener(BrokenRead())
        with self.relay(opener=opener) as (handler_class, relay, unused):
            _, status, _ = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        entry = next(iter(json.loads(self.ledger.path.read_text())['requests'].values()))
        self.assertEqual((entry['failure_stage'], entry['failure_type']),
                         ('upstream_read', 'value_error'))
        self.assertNotIn('secret-message', json.dumps(entry))

    def test_policy_validation_exception_records_validation_stage_before_outer_catch(self):
        class InvalidPolicy(_RelayPolicySpy):
            def validate_response(self, raw, content_type):
                raise ValueError('secret-message')

        opener = _RelayOpener(_JsonResponse())
        with self.relay(policy=InvalidPolicy(), opener=opener) as (handler_class, relay, unused):
            _, status, _ = self.invoke(handler_class, relay)
        self.assertEqual(status, 400)
        entry = next(iter(json.loads(self.ledger.path.read_text())['requests'].values()))
        self.assertEqual((entry['status'], entry['error_kind'], entry['failure_stage'],
                          entry['failure_type']),
                         ('failed', 'protocol', 'response_validation', 'value_error'))
        self.assertEqual(entry['usage']['input_tokens'], 100)
        self.assertNotIn('secret-message', json.dumps(entry))




@contextlib.contextmanager
def upstream(reply, request_id=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            requests.append(request)
            status, content_type, data = reply(request, len(requests))
            self.send_response(status)
            if request_id:
                self.send_header('x-request-id', request_id)
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


def work_guidance_text(item):
    content = item.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(part['text'] for part in content
                       if isinstance(part, dict) and part.get('type') == 'input_text')
    return ''


def work_guidance_budget(item):
    text = work_guidance_text(item)
    if not text.startswith(isolation.WORK_BUDGET_PREFIX):
        raise AssertionError(text)
    return text, json.loads(text[len(isolation.WORK_BUDGET_PREFIX):])


class WorkGuidanceTests(unittest.TestCase):
    def test_list_input_preserves_payload_and_appends_one_developer_message(self):
        payload = {
            'model': 'frozen-model',
            'input': [
                {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': 'original one'}]},
                {'type': 'function_call', 'call_id': 'call-1', 'name': 'shell', 'arguments': '{}'},
                {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'ok'},
            ],
            'tools': [{'type': 'function', 'name': 'shell',
                       'parameters': {'type': 'object', 'properties': {}}}],
            'tool_choice': 'auto', 'stream': True, 'metadata': {'key': 'value'},
        }
        raw = json.dumps(payload, indent=2).encode()
        prepared = isolation.prepare_work_request(
            raw, seconds_remaining=12.75, requests_remaining=4)
        self.assertIsInstance(prepared, bytes)
        updated = json.loads(prepared)
        self.assertEqual({key: value for key, value in updated.items() if key != 'input'},
                         {key: value for key, value in payload.items() if key != 'input'})
        self.assertEqual(updated['input'][:-1], payload['input'])
        self.assertEqual(len(updated['input']), len(payload['input']) + 1)
        message = updated['input'][-1]
        self.assertEqual(message['role'], 'developer')
        self.assertEqual(set(message), {'role', 'content'})
        text, budget = work_guidance_budget(message)
        self.assertEqual(budget['admission_seconds_remaining'], 12)
        self.assertEqual(budget['further_work_requests_remaining'], 4)
        self.assertLessEqual(len(text), 1000)
        self.assertIn('advisory', budget['instruction'].lower())
        self.assertIn('not acceptance', budget['instruction'].lower())

    def test_string_input_becomes_one_user_message_with_original_text(self):
        raw = json.dumps({'model': 'm', 'input': 'keep this text unchanged'}).encode()
        updated = json.loads(isolation.prepare_work_request(raw, seconds_remaining=3))
        self.assertEqual(len(updated['input']), 2)
        self.assertEqual(updated['input'][0],
                         {'role': 'user', 'content': 'keep this text unchanged'})
        text, budget = work_guidance_budget(updated['input'][-1])
        self.assertEqual(set(budget), {'admission_seconds_remaining', 'instruction'})
        self.assertNotIn('further_work_requests_remaining', budget)
        self.assertLessEqual(len(text), 1000)

    def test_zero_is_known_and_unknown_values_are_omitted(self):
        raw = json.dumps({'model': 'm', 'input': []}).encode()
        updated = json.loads(isolation.prepare_work_request(
            raw, seconds_remaining=0, requests_remaining=0))
        _, budget = work_guidance_budget(updated['input'][-1])
        self.assertEqual(budget['admission_seconds_remaining'], 0)
        self.assertEqual(budget['further_work_requests_remaining'], 0)

        updated = json.loads(isolation.prepare_work_request(
            raw, seconds_remaining=0.9, requests_remaining=None))
        _, budget = work_guidance_budget(updated['input'][-1])
        self.assertEqual(budget['admission_seconds_remaining'], 0)
        self.assertNotIn('further_work_requests_remaining', budget)

        updated = json.loads(isolation.prepare_work_request(
            raw, seconds_remaining=None, requests_remaining=2))
        _, budget = work_guidance_budget(updated['input'][-1])
        self.assertNotIn('admission_seconds_remaining', budget)
        self.assertEqual(budget['further_work_requests_remaining'], 2)

        for seconds, requests in ((None, None), (True, None), (-1, None),
                                  (float('nan'), None), (float('inf'), None), ('4', None),
                                  (None, True), (None, -1), (None, 2.5), (None, '2')):
            with self.subTest(seconds=seconds, requests=requests):
                self.assertIs(isolation.prepare_work_request(
                    raw, seconds_remaining=seconds, requests_remaining=requests), raw)

    def test_unsupported_bodies_are_returned_unchanged(self):
        for raw in (b'not-json', b'[]', b'{}', b'{"input":3}', b'{"input":{"a":1}}', b'"text"'):
            with self.subTest(raw=raw):
                self.assertIs(isolation.prepare_work_request(
                    raw, seconds_remaining=5, requests_remaining=1), raw)

    def test_ineligible_payload_never_reads_optional_hint_metadata(self):
        class Sentinel:
            def snapshot(self):
                raise AssertionError('optional snapshot accessed')

            def admission_remaining(self):
                raise AssertionError('optional time accessed')

        sentinel = Sentinel()
        for raw in (b'{"model":"m"}', b'not-json', b'{"model":"m","input":3}'):
            with self.subTest(raw=raw):
                self.assertIs(isolation._apply_work_guidance(
                    raw, usage_ledger=sentinel, request_id='request-1', closeout=sentinel), raw)

    def test_admission_remaining_uses_monotonic_deadline_and_window(self):
        with patch.object(isolation.time, 'monotonic') as clock:
            clock.return_value = 100.0
            unarmed = isolation.RelayCloseout()
            self.assertIsNone(unarmed.admission_remaining())
            armed = isolation.RelayCloseout(duration=30, request_window=5)
            self.assertEqual(armed.admission_remaining(), 25.0)
            clock.return_value = 110.0
            self.assertEqual(armed.admission_remaining(), 15.0)
            clock.return_value = 124.5
            self.assertEqual(armed.admission_remaining(), 0.5)
            clock.return_value = 125.0
            self.assertEqual(armed.admission_remaining(), 0)
            clock.return_value = 1000.0
            self.assertEqual(armed.admission_remaining(), 0)

            clock.return_value = 50.0
            started = isolation.RelayCloseout()
            started.start(10)
            self.assertEqual(started.admission_remaining(), 10.0)
            started.stop('finished')
            clock.return_value = 51.0
            self.assertEqual(started.admission_remaining(), 0)


class WorkGuidanceRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'usage.json'

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def completed_reply(request, n):
        return 200, 'application/json', json.dumps(
            {'status': 'completed', 'usage': responses_usage()}).encode()

    def test_relay_injects_hint_for_direct_work_responses(self):
        with upstream(self.completed_reply) as (port, seen):
            ledger = usage.UsageLedger(self.path, {'max_requests': 5})
            closeout = isolation.RelayCloseout(duration=60, request_window=5)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}, closeout=closeout) as relay:
                post(relay, payload={'model': 'm', 'input': [
                    {'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': 'hello'}]}]})
        self.assertEqual(len(seen), 1)
        forwarded = seen[0]
        self.assertEqual(forwarded['input'][0]['content'][0]['text'], 'hello')
        self.assertEqual(len(forwarded['input']), 2)
        text, budget = work_guidance_budget(forwarded['input'][-1])
        self.assertLessEqual(len(text), 1000)
        self.assertEqual(budget['further_work_requests_remaining'], 4)
        self.assertIsInstance(budget['admission_seconds_remaining'], int)
        self.assertGreaterEqual(budget['admission_seconds_remaining'], 0)
        self.assertLessEqual(budget['admission_seconds_remaining'], 55)

    def test_each_request_refreshes_decreasing_allowances(self):
        allowances, seconds = [], []
        with upstream(self.completed_reply) as (port, seen):
            ledger = usage.UsageLedger(self.path, {'max_requests': 5})
            closeout = isolation.RelayCloseout(duration=60, request_window=5)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}, closeout=closeout) as relay:
                for _ in range(3):
                    post(relay, payload={'model': 'm', 'input': []})
        self.assertEqual(len(seen), 3)
        for forwarded in seen:
            _, budget = work_guidance_budget(forwarded['input'][-1])
            allowances.append(budget['further_work_requests_remaining'])
            seconds.append(budget['admission_seconds_remaining'])
        self.assertEqual(allowances, [4, 3, 2])
        self.assertEqual(seconds, sorted(seconds, reverse=True))

    def test_exam_reserve_is_visible_exhausted_and_not_used_for_work(self):
        with upstream(self.completed_reply) as (port, seen):
            ledger = usage.UsageLedger(
                self.path, {'max_requests': 3, 'exam_reserve_requests': 1})
            closeout = isolation.RelayCloseout(duration=60, request_window=5)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}, closeout=closeout) as relay:
                post(relay, payload={'model': 'm', 'input': []})
                post(relay, payload={'model': 'm', 'input': []})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post(relay, payload={'model': 'm', 'input': []})
                self.assertEqual(error.exception.code, 400)
            self.assertEqual(len(seen), 2)
            allowances = []
            for forwarded in seen:
                _, budget = work_guidance_budget(forwarded['input'][-1])
                allowances.append(budget['further_work_requests_remaining'])
            self.assertEqual(allowances, [1, 0])

            with isolation.model_relay(f'http://127.0.0.1:{port}', 'KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'exam'}) as relay:
                post(relay, payload={'model': 'm', 'input': []})
        self.assertEqual(len(seen), 3)
        self.assertNotIn(isolation.WORK_BUDGET_PREFIX, json.dumps(seen[2]))

    def test_work_hint_is_absent_for_exams_compaction_and_unspecified_phase(self):
        cases = [
            ('exam', {'phase': 'exam'}, '/responses'),
            ('compaction', {'phase': 'work'}, '/responses/compact'),
            ('unspecified', {}, '/responses'),
        ]
        for name, context, path in cases:
            with self.subTest(case=name):
                with upstream(self.completed_reply) as (port, seen):
                    ledger = usage.UsageLedger(Path(self.temp.name) / (name + '.json'))
                    with isolation.model_relay(f'http://127.0.0.1:{port}', 'KEY', usage_ledger=ledger,
                                               usage_context=context) as relay:
                        post(relay, path=path, payload={'model': 'm', 'input': []})
                self.assertEqual(len(seen), 1)
                self.assertNotIn(isolation.WORK_BUDGET_PREFIX, json.dumps(seen[0]))

    def test_work_hint_is_absent_for_local_chat_adaptation(self):
        binding = {'provider': 'local', 'model': 'local-model', 'wire_api': 'chat',
                   'reasoning_effort': 'none'}
        response = {'model': 'local-model',
                    'choices': [{'message': {'role': 'assistant', 'content': 'ok'},
                                 'finish_reason': 'stop'}], 'usage': chat_usage()}

        def reply(request, n):
            return 200, 'application/json', json.dumps(response).encode()

        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'KEY', binding=binding,
                                       usage_ledger=ledger,
                                       usage_context={'phase': 'work'}) as relay:
                post(relay, payload={'model': 'local-model', 'input': 'hello'})
        self.assertEqual(len(seen), 1)
        self.assertNotIn(isolation.WORK_BUDGET_PREFIX, json.dumps(seen[0]))
        self.assertNotIn('developer', [message.get('role') for message in seen[0]['messages']])


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

    def test_terminal_sse_event_larger_than_two_mib_is_forwarded_and_accounted(self):
        event = {'type': 'response.completed', 'response': {
            'usage': responses_usage(),
            'output': [{'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'x' * (2 * 1024 * 1024 + 8192)}]}]}}
        data = (b'event: response.completed\r\ndata: '
                + json.dumps(event).encode() + b'\r\n\r\n')

        def reply(request, n):
            return 200, 'text/event-stream', data
        with upstream(reply) as (port, seen):
            ledger = usage.UsageLedger(self.path)
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'UPSTREAM_KEY', usage_ledger=ledger,
                                       usage_context={'phase': 'work'}) as relay:
                forwarded = post(relay, payload={'model': 'deepseek-flash'})
        self.assertEqual(forwarded, data)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['totals']['requests'], 1)
        self.assertEqual(snapshot['totals']['failed_requests'], 0)
        self.assertEqual(snapshot['totals']['unknown_usage_requests'], 0)
        self.assertEqual(snapshot['totals']['input_tokens'], 100)
        self.assertEqual(snapshot['totals']['output_tokens'], 20)
        stored = json.loads(self.path.read_text())['requests']
        self.assertEqual(next(iter(stored.values()))['status'], 'completed')

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

"""Work reasoning display over real HTTP, with no paid provider requests."""
import concurrent.futures
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

import activity
import isolation
import reasoning
import test_usage as fixtures
import usage
import watch


def sse(event, newline='\n'):
    return ('event: ' + event['type'] + newline + 'data: ' +
            json.dumps(event, ensure_ascii=False) + newline * 2).encode()


def delta(text, **fields):
    return {'type': 'response.reasoning_text.delta', 'item_id': 'reason-1',
            'output_index': 0, 'content_index': 0, 'delta': text, **fields}


def done(text):
    return {**delta(''), 'type': 'response.reasoning_text.done', 'text': text}


def wait_for(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Observer did not update before deadline')


class ReasoningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recorder = activity.Recorder(self.root, 'a' * 32, 'job', secrets=('EXACT_PRIVATE_KEY',))

    def events(self):
        return list(activity.Reader(self.root / activity.FILENAME).read())

    def test_fragmented_utf8_crlf_and_final_snapshot_coalesce(self):
        decoder = reasoning.Decoder(self.recorder, 'request-1')
        raw = sse(delta('先检查边界。 '), '\r\n') + sse(delta('再运行测试。 '), '\r\n')
        for byte in raw:
            decoder.feed(bytes([byte]))
        decoder.flush()
        self.assertEqual(self.events()[0]['text'], '先检查边界。 再运行测试。 ')
        decoder.feed(sse(done('先检查边界。 再运行测试。 ')))
        decoder.feed(sse(done('DUPLICATE_SHOULD_NOT_APPEAR')))
        merged = list(watch.EventBuffer(self.events()))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['text'], '先检查边界。 再运行测试。 ')
        self.assertEqual(merged[0]['status'], 'completed')

    def test_streamed_credentials_are_redacted_across_every_character_boundary(self):
        decoder = reasoning.Decoder(self.recorder, 'request', secrets=self.recorder.secrets)
        text = ('Safe EXACT_PRIVATE_KEY sk-abcdefghijklmno Authorization: Bearer abcdefghijk '
                'password: one-two-three\nSAFE_END\n')
        for char in text:
            decoder.feed(sse(delta(char)))
            decoder.flush()
        decoder.feed(sse(done(text)))
        events = self.events()
        raw = json.dumps(events)
        for forbidden in ('PRIVATE_KEY', 'EXACT_', 'abcdefgh', 'one-two-three'):
            self.assertNotIn(forbidden, raw)
        displayed = list(watch.EventBuffer(events))[0]['text']
        self.assertIn('Safe', displayed)
        self.assertIn('SAFE_END', displayed)
        self.assertIn('[REDACTED]', displayed)

    def test_incomplete_secrets_and_terminal_controls_never_escape_on_disconnect(self):
        decoder = reasoning.Decoder(self.recorder, 'request', secrets=self.recorder.secrets)
        decoder.feed(sse(delta('SAFE\n\x1b]52;c;bad\x07 EXACT_PRIVATE_')))
        decoder.flush('interrupted')
        raw = (self.root / activity.FILENAME).read_text()
        self.assertNotIn('EXACT_PRIVATE_', raw)
        self.assertNotIn('\\u001b', raw)
        self.assertNotIn('\\u0007', raw)
        self.assertEqual(self.events()[-1]['status'], 'interrupted')

    def test_complete_visibility_normalizes_before_redaction(self):
        decoder = reasoning.Decoder(self.recorder, 'request', secrets=self.recorder.secrets)
        text = ('SAFE Bearer abc\u0000def api_\u200bkey: SYNTHETIC_VALUE '
                'EXACT\u200b_PRIVATE_KEY\nSAFE_END\n')
        decoder.feed(sse(delta(text)))
        decoder.feed(sse(done(text)))
        events = self.events()
        raw = json.dumps(events)
        for forbidden in ('abcdef', 'SYNTHETIC_VALUE', 'EXACT', '\\u0000', '\\u200b'):
            self.assertNotIn(forbidden, raw)
        displayed = list(watch.EventBuffer(events))[0]['text']
        self.assertIn('SAFE Bearer [REDACTED]', displayed)
        self.assertIn('api_key: [REDACTED]', displayed)
        self.assertIn('SAFE_END\n', displayed)

        controlled = activity.Recorder(self.root, 'b' * 32,
                                       secrets=('EXACT\u0000PRIVATE_KEY',))
        decoder = reasoning.Decoder(controlled, 'request', secrets=controlled.secrets)
        decoder.feed(sse(delta('SAFE EXACT\u200bPRIVATE_KEY END')))
        decoder.flush('completed')
        raw = json.dumps(self.events())
        self.assertNotIn('PRIVATE_KEY', raw)
        self.assertIn('SAFE [REDACTED] END', json.loads(raw)[-1]['text'])

    def test_duplicate_sequence_parts_summaries_and_request_ids_stay_separate(self):
        for request in ('first', 'second'):
            decoder = reasoning.Decoder(self.recorder, request)
            decoder.feed(sse(delta('one ', sequence_number=1)))
            decoder.feed(sse(delta('one ', sequence_number=1)))
            decoder.feed(sse(delta('two ', content_index=1, sequence_number=2)))
            decoder.feed(sse({**delta('summary '), 'type': 'response.reasoning_summary_text.delta'}))
            decoder.flush('completed')
        merged = list(watch.EventBuffer(self.events()))
        self.assertEqual(len(merged), 6)
        self.assertEqual([x['text'] for x in merged], ['one ', 'two ', 'summary '] * 2)

    def test_final_item_without_deltas_is_shown_once_and_nonreasoning_ignored(self):
        decoder = reasoning.Decoder(self.recorder, 'request')
        item = {'type': 'reasoning', 'id': 'reason-1', 'content': [{'type': 'reasoning_text', 'text': 'fallback'}]}
        decoder.feed(sse({'type': 'response.output_item.done', 'output_index': 0, 'item': item}))
        decoder.feed(sse({'type': 'response.completed', 'response': {'output': [item]}}))
        decoder.feed(sse({'type': 'response.output_text.delta', 'delta': 'NOT_REASONING'}))
        self.assertEqual([x['text'] for x in self.events()], ['fallback'])

    def test_oversized_and_malformed_events_recover_and_text_is_bounded(self):
        decoder = reasoning.Decoder(self.recorder, 'request')
        decoder.feed(b'data: ' + b'x' * (reasoning.MAX_EVENT_BYTES + 1) + b'\n\n')
        decoder.feed(b'data: bad-json\n\ndata: []\n\n')
        decoder.feed(sse(delta('界' * (activity.MAX_TEXT + 100))))
        decoder.feed(sse(delta('IGNORED_AFTER_TRUNCATION')))
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['status'], 'truncated')
        self.assertLessEqual(len(events[0]['text']), activity.MAX_TEXT + 30)

    def test_tail_gap_is_visible_and_final_snapshot_recovers_without_duplicate_output(self):
        self.recorder.emit('reasoning', 'tail ', item_id='x', offset=10, event_type='reasoning.delta')
        self.recorder.emit('reasoning', 'tail ', item_id='x', offset=10, event_type='reasoning.delta')
        view = watch.EventBuffer(self.events())
        self.assertIn('earlier reasoning omitted', list(view)[0]['text'])
        self.recorder.emit('reasoning', 'full text tail ', item_id='x', offset=0,
                           event_type='reasoning.snapshot', status='completed')
        self.assertEqual(list(watch.EventBuffer(self.events()))[0]['text'], 'full text tail ')
        bridge = SimpleNamespace(run_dir=lambda sid: self.root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            watch.stream(bridge, 'a' * 32, follow=False, tail=100)
        self.assertEqual(output.getvalue().count('full text tail '), 1)
        self.assertNotIn('earlier reasoning omitted', output.getvalue())

    def test_full_history_does_not_drop_old_events_or_repeat_completed_reasoning(self):
        for index in range(2010):
            self.recorder.emit('message', f'HISTORY_{index:04d}')
        decoder = reasoning.Decoder(self.recorder, 'request')
        decoder.feed(sse(delta('ONE_REASONING_BLOCK\n')))
        decoder.flush()
        decoder.feed(sse(done('ONE_REASONING_BLOCK\n')))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            watch.stream(SimpleNamespace(run_dir=lambda sid: self.root), 'a' * 32,
                         follow=False, history=True)
        text = output.getvalue()
        self.assertEqual(text.count('HISTORY_'), 2010)
        self.assertEqual(text.count('ONE_REASONING_BLOCK'), 1)

    def test_slow_recorder_and_queue_overflow_do_not_block_producer(self):
        entered, release = threading.Event(), threading.Event()
        class SlowRecorder:
            phase, secrets = 'work', ()
            def emit(self, *args, **kwargs):
                entered.set()
                release.wait(5)
        observer = reasoning.start(SlowRecorder())
        try:
            observer.feed('first', sse(done('first')))
            self.assertTrue(entered.wait(3))
            started = time.monotonic()
            for _ in range(reasoning.QUEUE_CHUNKS * 2):
                observer.feed('first', b'x' * 65536)
            self.assertLess(time.monotonic() - started, .2)
            self.assertTrue(observer.failed.is_set())
        finally:
            release.set()
            observer.close()
        self.assertFalse(observer.worker.is_alive())

    def test_exam_and_observer_start_failure_are_inert(self):
        exam = activity.Recorder(self.root, 'a' * 32, phase='exam')
        self.assertIsNone(reasoning.start(exam))
        with patch.object(reasoning.threading.Thread, 'start', side_effect=RuntimeError('unavailable')):
            self.assertIsNone(reasoning.start(self.recorder))
        self.assertEqual(self.events(), [])

    def test_real_http_exposes_reasoning_before_response_finishes_and_preserves_wire_usage(self):
        release = threading.Event()
        first = sse(delta('VISIBLE_BEFORE_FINISH\n'))
        terminal = sse({'type': 'response.completed', 'response': {
            'status': 'completed', 'usage': fixtures.responses_usage()}})
        last = sse(done('VISIBLE_BEFORE_FINISH\n')) + terminal
        def emit(stream, count):
            stream.write(first); stream.flush()
            release.wait(5)
            stream.write(last); stream.flush()
        ledger = usage.UsageLedger(self.root / 'usage.json', {'max_requests': 1})
        with fixtures.streaming_upstream(emit) as (port, seen):
            with isolation.model_relay(f'http://127.0.0.1:{port}', 'SYNTHETIC_KEY',
                    usage_ledger=ledger, usage_context={'phase': 'work'},
                    activity_recorder=self.recorder) as (endpoint, token, _):
                request = urllib.request.Request(endpoint + '/responses', data=b'{"model":"m"}',
                    headers={'Authorization': 'Bearer ' + token})
                def receive():
                    with urllib.request.urlopen(request, timeout=8) as response:
                        return response.read()
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(receive)
                    try:
                        wait_for(lambda: any('VISIBLE_BEFORE_FINISH' in e['text'] for e in self.events()))
                        self.assertFalse(future.done(), 'display was buffered until response end')
                    finally:
                        release.set()
                    self.assertEqual(future.result(), first + last)
            self.assertEqual(seen, [{'model': 'm'}])
        self.assertEqual(ledger.snapshot()['totals']['total_tokens'], 120)
        self.assertEqual(len(list(watch.EventBuffer(self.events()))), 1)

    def test_exam_or_policy_never_enables_stream_observation(self):
        def emit(stream, count):
            stream.write(sse(delta('PRIVATE_EXAM'))); stream.flush()
        for phase, policy in (('exam', None), ('work', SimpleNamespace())):
            with self.subTest(phase=phase), patch.object(reasoning, 'start') as start:
                with isolation.model_relay('http://127.0.0.1:9', '', usage_context={'phase': phase},
                                           policy=policy, activity_recorder=self.recorder):
                    pass
                start.assert_not_called()

    def test_observer_exceptions_cannot_change_http_response_or_usage(self):
        terminal = sse({'type': 'response.completed', 'response': {
            'status': 'completed', 'usage': fixtures.responses_usage()}})
        class Broken:
            def feed(self, *args): raise RuntimeError('display only')
            end = close = feed
        ledger = usage.UsageLedger(self.root / 'usage.json', {'max_requests': 1})
        with patch.object(reasoning, 'start', return_value=Broken()):
            with fixtures.streaming_upstream(lambda stream, count: stream.write(terminal)) as (port, _):
                with isolation.model_relay(f'http://127.0.0.1:{port}', '', usage_ledger=ledger,
                        usage_context={'phase': 'work'}, activity_recorder=self.recorder) as (endpoint, token, _):
                    request = urllib.request.Request(endpoint + '/responses', data=b'{}',
                        headers={'Authorization': 'Bearer ' + token})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.read(), terminal)
        self.assertEqual(ledger.snapshot()['totals']['total_tokens'], 120)


if __name__ == '__main__':
    unittest.main()

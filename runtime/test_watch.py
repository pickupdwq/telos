"""Observer tests: real pipes, real tmux, and real isolated CLI with a model fixture."""
import concurrent.futures
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import activity
import watch
from test_telos import b, BridgeTests
from test_closed_book import WorkflowTests, message


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Condition did not become true before deadline')


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / activity.FILENAME
        self.recorder = activity.Recorder(self.root, 'a' * 32, 'job', secrets=('EXACT_SECRET',))

    def events(self):
        return list(activity.Reader(self.log).read())

    def launch(self, code):
        p = subprocess.Popen([sys.executable, '-u', '-c', code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_categories_redaction_control_sequences_and_private_exam_boundary(self):
        for kind, item in [
            ('item.completed', {'type': 'agent_message', 'text': 'working EXACT_SECRET \x1b]52;c;payload\x07'}),
            ('item.started', {'type': 'command_execution', 'command': 'echo ready'}),
            ('item.completed', {'type': 'command_execution', 'command': 'echo ready',
                                'aggregated_output': 'ready', 'exit_code': 0}),
            ('item.completed', {'type': 'file_change', 'changes': [{'kind': 'update', 'path': 'calc.py'}]}),
            ('item.completed', {'type': 'reasoning', 'text': 'HIDDEN_REASONING'}),
        ]:
            self.recorder.event({'type': kind, 'item': item})
        self.recorder.event({'type': 'error', 'message': 'Authorization: Bearer FAKE_KEY_123'})
        exam = activity.Recorder(self.root, 'a' * 32, 'exam', phase='exam')
        exam.event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'PRIVATE_ANSWER'}})
        exam.event({'type': 'error', 'message': 'PRIVATE_REFERENCE'})
        exam.event({'type': 'turn.completed'})
        raw = self.log.read_text()
        for forbidden in ('EXACT_SECRET', 'FAKE_KEY_123', 'HIDDEN_REASONING', 'PRIVATE_ANSWER',
                          'PRIVATE_REFERENCE', '\\u001b', '\\u0007'):
            self.assertNotIn(forbidden, raw)
        self.assertTrue({'message', 'tool', 'command', 'files', 'error', 'status'} <=
                        {e['kind'] for e in self.events()})
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_controls_are_removed_before_secret_and_pattern_redaction(self):
        cases = [
            'known EXACT\u200b_SECRET tail',
            'Bearer abc\u0000def',
            'api_\u200bkey: SYNTHETIC_VALUE',
            's\u0000k-abcdefghijklmno',
            'ordinary\ttext\nsecond line',
        ]
        for case in cases:
            self.recorder.emit('message', case)
        self.recorder.emit('message', 'fields', item_id='Bearer abc\u0000def',
                           status='secret=SYNTHETIC_FIELD', event_type='api_\u200bkey')
        events = self.events()
        texts = [event['text'] for event in events]
        raw = json.dumps(events)
        for forbidden in ('EXACT_SECRET', 'abc\u0000def', 'SYNTHETIC_VALUE',
                          'abcdefghijklmno', 'SYNTHETIC_FIELD', '\u200b', '\u0000'):
            self.assertNotIn(forbidden, raw)
        self.assertIn('[REDACTED]', texts[0])
        self.assertIn('Bearer [REDACTED]', texts[1])
        self.assertIn('api_key: [REDACTED]', texts[2])
        self.assertIn('[REDACTED]', texts[3])
        self.assertEqual(texts[4], 'ordinary\ttext\nsecond line')
        self.assertIn('fields', texts[5])

        controlled = activity.Recorder(self.root, 'b' * 32, secrets=('CANARY\u0000SECRET',))
        controlled.emit('message', 'CANARY\u200bSECRET')
        controlled.emit('message', 'CANARY\u0000SECRET')
        raw = json.dumps(self.events())
        self.assertNotIn('CANARY', raw)
        self.assertNotIn('SECRET', raw)

    def test_clean_sanitizes_before_bound_and_keeps_newline_tab(self):
        text = activity.clean('a' * (activity.MAX_TEXT + 10) + '\u0000', ('x',))
        self.assertTrue(text.startswith('a' * activity.MAX_TEXT))
        self.assertTrue(text.endswith('\n[truncated]'))
        self.assertEqual(activity.clean('tab\there\nline\x00'), 'tab\there\nline')

    def test_concurrent_append_partial_line_and_independent_readers(self):
        self.log.write_bytes(b'{"interrupted":')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: activity.Recorder(self.root, 'a' * 32).emit('message', str(i)), range(80)))
        first, second = activity.Reader(self.log), activity.Reader(self.log)
        self.assertEqual(len(list(first.read())), 80)
        self.assertEqual(len(list(second.read())), 80)
        with self.log.open('ab') as stream:
            stream.write(b'{"version":1,"kind":"message","text":"last"')
        self.assertEqual(list(first.read()), [])
        with self.log.open('ab') as stream:
            stream.write(b'}\n')
        self.assertEqual([e['text'] for e in first.read()], ['last'])
        self.assertEqual([e['text'] for e in second.read()], ['last'])
        self.log.write_text('{"version":1,"text":"replaced"}\n')
        self.assertEqual([e['text'] for e in first.read()], ['replaced'])

    def test_tail_and_partial_follow_do_not_skip_later_completion(self):
        for i in range(20):
            self.recorder.emit('message', str(i))
        reader = activity.Reader(self.log)
        reader.seek_tail(3)
        self.assertGreater(reader.offset, 0)
        self.assertEqual([e['text'] for e in reader.read()], ['17', '18', '19'])
        with self.log.open('ab') as stream:
            stream.write(b'{"version":1,"text":"next"')
        reader.seek_tail(0)
        self.assertEqual(list(reader.read()), [])
        with self.log.open('ab') as stream:
            stream.write(b'}\n')
        self.assertEqual([e['text'] for e in reader.read()], ['next'])

    def test_real_output_is_visible_before_process_finishes_and_stderr_is_drained(self):
        gate = self.root / 'continue'
        code = (
            'import json,sys,time\nfrom pathlib import Path\n'
            'sys.stdin.read()\n'
            'print(json.dumps({"type":"thread.started","thread_id":"native-thread"}),flush=True)\n'
            'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"LIVE_NOW"}}),flush=True)\n'
            'sys.stderr.write("diagnostic\\n"*20000);sys.stderr.flush()\n'
            f'while not Path({str(gate)!r}).exists(): time.sleep(.02)\n'
            'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1}}),flush=True)\n')
        p = self.launch(code)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(activity.communicate, p, b'prompt', 8, self.recorder)
            wait_for(lambda: self.log.exists() and 'LIVE_NOW' in self.log.read_text())
            self.assertIsNone(p.poll(), 'message appeared only after process completion')
            # Two observers can read without consuming each other's events.
            self.assertTrue(any(e['text'] == 'LIVE_NOW' for e in self.events()))
            gate.touch()
            stdout, stderr = future.result(timeout=10)
        self.assertIn(b'native-thread', stdout)
        self.assertIn(b'turn.completed', stdout)
        self.assertLessEqual(len(stderr), 16000)
        self.assertEqual(p.returncode, 0)

    def test_timeout_still_enforced_when_stdin_is_not_read(self):
        p = self.launch('import time,json\nprint(json.dumps({"type":"thread.started","thread_id":"retain"}),flush=True)\ntime.sleep(30)')
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            activity.communicate(p, b'x' * 1000000, .3, self.recorder)
        self.assertIsNotNone(p.poll())
        self.assertIn(b'retain', caught.exception.output)

    def test_io_failure_does_not_fail_executor_and_symlinks_are_not_followed(self):
        target = self.root / 'keep'
        target.write_text('original')
        self.log.symlink_to(target)
        self.assertFalse(self.recorder.emit('message', 'new'))
        self.assertEqual(target.read_text(), 'original')
        self.assertTrue((self.root / 'activity-unavailable').exists())
        p = self.launch('import json\nprint(json.dumps({"type":"thread.started","thread_id":"ok"}))')
        stdout, _ = activity.communicate(p, b'', 3, self.recorder)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b'ok', stdout)

    def test_oversized_events_are_bounded_and_next_event_is_preserved(self):
        with patch.object(activity, 'MAX_LINE', 4096):
            p = self.launch('import json\nprint("x"*12000)\nprint(json.dumps({"type":"turn.completed"}))')
            out, _ = activity.communicate(p, b'', 3, self.recorder)
        self.assertIn(b'turn.completed', out)
        self.assertLess(self.log.stat().st_size, 3000)

    def test_reader_drops_oversized_line_across_eof_polls(self):
        with patch.object(activity, 'MAX_LINE', 128):
            self.log.write_bytes(b'x' * 129)
            reader = activity.Reader(self.log)
            self.assertEqual(list(reader.read()), [])
            self.assertEqual(list(reader.read()), [])
            with self.log.open('ab') as stream:
                stream.write(b'x' * 17)
            self.assertEqual(list(reader.read()), [])
            with self.log.open('ab') as stream:
                stream.write(b'{"version":1,"text":"false tail"}\n')
                stream.write(b'{"version":1,"text":"real record"}\n')
            self.assertEqual([e['text'] for e in reader.read()], ['real record'])

    def test_reader_resets_drop_state_on_rotation_truncation_and_seek(self):
        for change in ('rotate', 'truncate', 'seek'):
            with self.subTest(change=change), patch.object(activity, 'MAX_LINE', 128):
                self.log.write_bytes(b'x' * 129)
                reader = activity.Reader(self.log)
                self.assertEqual(list(reader.read()), [])
                if change == 'rotate':
                    self.log.rename(self.root / 'old.jsonl')
                self.log.write_bytes(b'{"version":1,"text":"new"}\n')
                if change == 'seek':
                    reader.seek_tail(1)
                self.assertEqual([e['text'] for e in reader.read()], ['new'])

    def test_missing_git_leaves_a_diagnostic_without_failing_work(self):
        bridge = SimpleNamespace(STATE=self.root, run_dir=lambda sid: self.root)
        with patch('preflight.git_executable', return_value=None):
            activity.files(bridge, {'session_id': 'a' * 32})
        self.assertEqual(self.events()[-1]['kind'], 'diagnostic')


class TimestampRobustnessTests(unittest.TestCase):
    """Pure timestamp-boundary tests for rendering, ordering, and streaming."""
    MISSING = object()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state'
        (self.state / 'runs').mkdir(parents=True)
        self.bridge = SimpleNamespace(
            STATE=self.state, CACHE=Path(self.temp.name) / 'cache',
            run_dir=lambda sid: self.state / 'runs' / sid)

    def task(self, sid, created_at=MISSING, **fields):
        directory = self.state / 'runs' / sid
        directory.mkdir(parents=True)
        value = {
            'session_id': sid, 'model': 'fixture-model', 'repo': 'fixture-repo',
            'phase': 'running', 'continuation': 'active',
        }
        if created_at is not self.MISSING:
            value['created_at'] = created_at
        value.update(fields)
        (directory / 'state.json').write_text(json.dumps(value))
        return sid

    def event(self, sid, timestamp=MISSING, text='event', **fields):
        value = {'version': 1, 'kind': 'message', 'phase': 'work', 'text': text}
        if timestamp is not self.MISSING:
            value['time'] = timestamp
        value.update(fields)
        with (self.state / 'runs' / sid / activity.FILENAME).open('a') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')

    def snapshot(self):
        return {path.relative_to(self.state): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in self.state.rglob('*') if path.is_file()}

    def test_missing_null_boolean_nonnumeric_and_nonfinite_times_are_unknown(self):
        for value in (self.MISSING, None, False, True, '123', [], float('nan'),
                      float('inf'), -float('inf'), 10 ** 100):
            with self.subTest(value=value):
                event = {'kind': 'message', 'phase': 'work', 'text': 'CONTENT'}
                if value is not self.MISSING:
                    event['time'] = value
                rendered = watch.render(event)
                self.assertEqual(rendered, '[??:??:??] message (work) \nCONTENT')

    def test_valid_times_keep_local_display_and_sanitized_content(self):
        for value in (0, 0.75, -1.25):
            with self.subTest(value=value):
                expected = time.strftime('%H:%M:%S', time.localtime(value))
                rendered = watch.render({'time': value, 'kind': 'message',
                                         'phase': 'work', 'text': 'VISIBLE'})
                self.assertEqual(rendered, f'[{expected}] message (work) \nVISIBLE')
        rendered = watch.render({
            'time': 'malformed', 'kind': 'message', 'phase': 'work',
            'text': 'VISIBLE Authorization: Bearer TOKEN_123\x1b[31mTAIL'})
        self.assertIn('VISIBLE', rendered)
        self.assertNotIn('TOKEN_123', rendered)
        self.assertNotIn('\x1b', rendered)
        with patch.object(watch.time, 'localtime', side_effect=RuntimeError('internal bug')):
            with self.assertRaisesRegex(RuntimeError, 'internal bug'):
                watch.render({'time': 1, 'text': 'programming errors stay visible'})

    def test_mixed_created_at_values_sort_safely_and_preserve_filters(self):
        values = {
            1: 2.5, 2: None, 3: '1', 4: float('nan'), 5: float('inf'),
            6: False, 7: -1, 8: 0, 9: 10 ** 30, 10: 1e20,
        }
        ids = {number: f'{number:032x}' for number in values}
        for number, created_at in values.items():
            fields = {}
            if number == 1:
                fields = {'watch_origin': {'group': 'a' * 24}, 'project_id': 'p' * 32}
            elif number == 2:
                fields = {'watch_origin': {'group': 'a' * 24}}
            self.task(ids[number], created_at, **fields)
        self.task('b' * 32, [], notification_only=True)
        invalid_path = self.task('c' * 32, None)
        path = self.state / 'runs' / invalid_path / 'state.json'
        state = json.loads(path.read_text())
        state['session_id'] = 'd' * 32
        path.write_text(json.dumps(state))

        expected = [ids[n] for n in (7, 2, 3, 4, 5, 6, 8, 9, 10, 1)]
        self.assertEqual([row['session_id'] for row in watch.tasks(self.state)], expected)
        self.assertEqual([row['session_id'] for row in watch.tasks(self.state, 'a' * 24)],
                         [ids[2], ids[1]])
        self.assertEqual([row['session_id'] for row in watch.tasks(self.state, project='p' * 32)],
                         [ids[1]])

    def test_platform_unrepresentable_created_at_uses_unknown_order(self):
        missing = self.task('1' * 32)
        float_unrepresentable = self.task('2' * 32, 1e20)
        huge_integer = self.task('3' * 32, 10 ** 30)
        valid = self.task('4' * 32, 2.5)
        self.assertEqual([row['session_id'] for row in watch.tasks(self.state)],
                         [missing, float_unrepresentable, huge_integer, valid])

    def test_unexpected_created_at_conversion_errors_remain_visible(self):
        self.task('1' * 32, 2.5)
        with patch.object(watch.time, 'localtime', side_effect=RuntimeError('internal bug')):
            with self.assertRaisesRegex(RuntimeError, 'internal bug'):
                watch.tasks(self.state)

    def test_history_streams_damaged_timestamps_with_healthy_neighbors(self):
        sid = self.task('1' * 32, 1)
        self.event(sid, self.MISSING, 'BAD_MISSING')
        self.event(sid, '123', 'BAD_STRING')
        self.event(sid, 0.5, 'HEALTHY_AFTER')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            watch.stream(self.bridge, sid, follow=False, history=True)
        rendered = output.getvalue()
        self.assertEqual(rendered.count('[??:??:??]'), 2)
        self.assertLess(rendered.index('BAD_MISSING'), rendered.index('BAD_STRING'))
        self.assertLess(rendered.index('BAD_STRING'), rendered.index('HEALTHY_AFTER'))

    def test_tail_stream_keeps_damaged_event_and_following_healthy_event(self):
        sid = self.task('1' * 32, 1)
        self.event(sid, 0, 'HEALTHY_BEFORE_TAIL')
        self.event(sid, None, 'TAIL_UNKNOWN')
        self.event(sid, 1.5, 'TAIL_HEALTHY')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            watch.stream(self.bridge, sid, follow=False, tail=2)
        rendered = output.getvalue()
        self.assertNotIn('HEALTHY_BEFORE_TAIL', rendered)
        self.assertIn('[??:??:??] message (work)', rendered)
        self.assertLess(rendered.index('TAIL_UNKNOWN'), rendered.index('TAIL_HEALTHY'))

    def test_follow_stream_continues_after_unknown_time_to_later_event(self):
        sid = self.task('1' * 32, 1)
        self.event(sid, self.MISSING, 'FOLLOW_UNKNOWN')

        output = io.StringIO()
        with patch.object(watch.time, 'sleep',
                          side_effect=lambda _: self.event(sid, 2, 'FOLLOW_HEALTHY')), \
                patch.object(watch, 'close_finished_panel', return_value=True), \
                contextlib.redirect_stdout(output):
            watch.stream(self.bridge, sid, follow=True, tail=10,
                         auto_close_group='a' * 24)
        rendered = output.getvalue()
        self.assertIn('[??:??:??] message (work)', rendered)
        self.assertLess(rendered.index('FOLLOW_UNKNOWN'), rendered.index('FOLLOW_HEALTHY'))

    def test_streaming_is_read_only_and_json_events_are_unchanged(self):
        sid = self.task('1' * 32, 1)
        self.event(sid, self.MISSING, 'RAW_MISSING')
        self.event(sid, '123', 'RAW_STRING')
        self.event(sid, 0, 'RAW_VALID')
        source = [json.loads(line) for line in
                  (self.state / 'runs' / sid / activity.FILENAME).read_text().splitlines()]
        before = self.snapshot()
        human = io.StringIO()
        with contextlib.redirect_stdout(human):
            watch.main(self.bridge, [sid, '--history', '--no-follow'])
        self.assertEqual(human.getvalue().count('[??:??:??]'), 2)
        raw = io.StringIO()
        with contextlib.redirect_stdout(raw):
            watch.main(self.bridge, [sid, '--history', '--no-follow', '--json'])
        self.assertEqual([json.loads(line) for line in raw.getvalue().splitlines()], source)
        self.assertEqual(self.snapshot(), before)

    def test_curses_frame_renders_unknown_time_and_later_healthy_event(self):
        sid = self.task('1' * 32, 1)
        self.event(sid, float('nan'), 'CURSES_UNKNOWN')
        self.event(sid, 1.5, 'CURSES_HEALTHY')

        class Screen:
            def __init__(self):
                self.lines = []

            def timeout(self, _):
                pass

            def getmaxyx(self):
                return 14, 100

            def erase(self):
                pass

            def refresh(self):
                pass

            def getch(self):
                return ord('q')

            def addnstr(self, y, x, text, n, style=0):
                self.lines.append(text)

        screen = Screen()
        with patch.object(watch.curses, 'curs_set', return_value=None), \
                patch.object(watch.curses, 'wrapper',
                             side_effect=lambda display: display(screen)):
            watch.ui(self.bridge, sid, tail=20)
        rendered = '\n'.join(screen.lines)
        self.assertIn('[??:??:??] message (work)', rendered)
        self.assertLess(rendered.index('CURSES_UNKNOWN'), rendered.index('CURSES_HEALTHY'))


class TailReaderTests(unittest.TestCase):
    """Pure byte-cursor tests for Reader.seek_tail (no subprocesses)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / activity.FILENAME

    @staticmethod
    def record(text):
        return (json.dumps({'version': 1, 'kind': 'message', 'text': text},
                           ensure_ascii=False) + '\n').encode('utf-8')

    @staticmethod
    def offset_after_tail_newline(data, count):
        newlines = [i for i, byte in enumerate(data) if byte == 10]
        if len(newlines) <= count:
            return 0
        return newlines[-count - 1] + 1

    def reader_for(self, data):
        self.log.write_bytes(data)
        return activity.Reader(self.log)

    def sized_record(self, size, text):
        prefix = (b'{"version":1,"kind":"message","text":' +
                  json.dumps(text).encode('ascii') + b'}')
        self.assertGreaterEqual(size, len(prefix) + 1)
        return prefix + b' ' * (size - len(prefix) - 1) + b'\n'

    def test_tail_counts_have_exact_byte_offsets_and_record_suffixes(self):
        records = [self.record(f'record-{i}') for i in range(6)]
        data = b''.join(records)
        for count in (0, 1, 2, 5, 6, 7):
            reader = self.reader_for(data)
            reader.seek_tail(count)
            self.assertEqual(reader.offset, self.offset_after_tail_newline(data, count),
                             f'tail {count}')
            expected = [f'record-{i}'
                        for i in range(max(0, len(records) - count), len(records))]
            self.assertEqual([e['text'] for e in reader.read()], expected, f'tail {count}')

    def test_incomplete_final_record_is_excluded_and_retried(self):
        complete = [self.record(f'complete-{i}') for i in range(3)]
        partial = b'{"version":1,"kind":"message","text":"finished"'
        data = b''.join(complete) + partial
        for count in (0, 1, 2, 3, 4):
            reader = self.reader_for(data)
            reader.seek_tail(count)
            self.assertEqual(reader.offset, self.offset_after_tail_newline(data, count),
                             f'tail {count}')
            expected = [f'complete-{i}' for i in range(max(0, 3 - count), 3)]
            self.assertEqual([e['text'] for e in reader.read()], expected, f'tail {count}')
        reader = self.reader_for(data)
        reader.seek_tail(1)
        self.assertEqual([e['text'] for e in reader.read()], ['complete-2'])
        with self.log.open('ab') as stream:
            stream.write(b'}\n')
        self.assertEqual([e['text'] for e in reader.read()], ['finished'])

    def test_chunk_boundaries_and_lines_longer_than_a_chunk(self):
        records = [
            self.sized_record(65536, 'chunk-end'),    # newline is byte 65535
            self.sized_record(65537, 'chunk-start'),  # newline is byte 131072
            self.sized_record(70000, 'long-line'),    # record crosses chunks
            self.sized_record(20000, 'short-line'),
        ]
        data = b''.join(records)
        self.assertEqual(data.index(b'\n'), 65535)
        self.assertEqual(data.index(b'\n', 65536), 131072)
        for count in (0, 1, 2, 3, 4, 5):
            reader = self.reader_for(data)
            reader.seek_tail(count)
            self.assertEqual(reader.offset, self.offset_after_tail_newline(data, count),
                             f'tail {count}')
            expected = ['chunk-end', 'chunk-start', 'long-line',
                        'short-line'][max(0, 4 - count):]
            self.assertEqual([e['text'] for e in reader.read()], expected, f'tail {count}')

    def test_utf8_offsets_are_byte_offsets(self):
        text = 'héllo-λ-😀'
        records = [self.record(text) for _ in range(3)]
        data = b''.join(records)
        reader = self.reader_for(data)
        reader.seek_tail(2)
        self.assertGreater(len(text.encode('utf-8')), len(text))
        self.assertEqual(reader.offset, len(records[0]))
        self.assertEqual([e['text'] for e in reader.read()], [text, text])

    def test_missing_and_empty_files_leave_cursor_at_zero(self):
        missing = activity.Reader(self.log)
        missing.seek_tail(3)
        self.assertEqual(missing.offset, 0)
        self.assertEqual(list(missing.read()), [])
        empty = self.reader_for(b'')
        empty.seek_tail(0)
        self.assertEqual(empty.offset, 0)
        self.assertEqual(list(empty.read()), [])

    def test_independent_cursors_seek_tail_separately(self):
        data = b''.join(self.record(f'event-{i}') for i in range(4))
        self.log.write_bytes(data)
        first, second = activity.Reader(self.log), activity.Reader(self.log)
        first.seek_tail(1)
        second.seek_tail(2)
        self.assertEqual([e['text'] for e in first.read()], ['event-3'])
        self.assertEqual([e['text'] for e in second.read()], ['event-2', 'event-3'])

    def test_truncation_and_replacement_reset_the_read_cursor(self):
        data = b''.join(self.record(f'old-{i}') for i in range(3))
        reader = self.reader_for(data)
        reader.seek_tail(1)
        self.assertEqual([e['text'] for e in reader.read()], ['old-2'])
        self.log.write_bytes(self.record('shorter'))
        self.assertEqual([e['text'] for e in reader.read()], ['shorter'])
        replacement = self.root / 'replacement'
        replacement.write_bytes(b''.join(self.record(f'new-{i}') for i in range(4)))
        os.replace(replacement, self.log)
        self.assertEqual([e['text'] for e in reader.read()],
                         ['new-0', 'new-1', 'new-2', 'new-3'])


class ViewerTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    create = BridgeTests.create

    def test_cli_history_filtering_prefix_and_read_only_state(self):
        first, second = self.create(), self.create()
        state = b.read_json(b.run_dir(first) / 'state.json')
        state.update(watch_origin={'group': 'a' * 24}, project_id='p' * 32)
        b.write_json(b.run_dir(first) / 'state.json', state)
        rec = activity.for_state(b, state)
        rec.emit('message', 'HISTORY_ONE')
        rec.emit('message', 'HISTORY_TWO')
        originals = {p: p.read_bytes() for p in b.STATE.rglob('*.json')}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            watch.main(b, [first[:8], '--history', '--no-follow', '--json'])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([e['text'] for e in events][-2:], ['HISTORY_ONE', 'HISTORY_TWO'])
        self.assertEqual([r['session_id'] for r in watch.tasks(b.STATE, 'a' * 24)], [first])
        self.assertEqual([r['session_id'] for r in watch.tasks(b.STATE, project='p' * 32)], [first])
        for p, raw in originals.items():
            self.assertEqual(p.read_bytes(), raw)
        self.assertTrue(any(r['session_id'] == second for r in watch.tasks(b.STATE)))
        with self.assertRaises(ValueError):
            watch.resolve_task(watch.tasks(b.STATE), '../../private')

    def test_non_tmux_and_missing_tmux_never_interrupt_tasks(self):
        with patch.dict(os.environ, {'TMUX': '', 'TMUX_PANE': ''}), \
                patch.object(watch, 'tmux', side_effect=AssertionError('tmux should not be queried')):
            self.assertIsNone(watch.origin(b.STATE))
            watch.after_start(b, {'session_id': 'a' * 32})
        with patch.dict(os.environ, {'TMUX': '/missing,1,0', 'TMUX_PANE': '%1'}), \
                patch.object(watch.shutil, 'which', return_value=None):
            self.assertIsNone(watch.origin(b.STATE))

    def test_viewer_interrupt_does_not_call_task_control(self):
        sid = self.create()
        with patch.object(watch.time, 'sleep', side_effect=KeyboardInterrupt), \
                patch.object(b, 'return_control', side_effect=AssertionError('viewer must not control tasks')), \
                contextlib.redirect_stdout(io.StringIO()):
            watch.main(b, [sid, '--json'])
        self.assertEqual(b.read_json(b.run_dir(sid) / 'state.json')['continuation'], 'active')

    def test_failed_work_attempt_retains_shell_edits_error_and_terminal_state(self):
        sid = self.create()
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())), \
                patch.object(watch, 'origin', return_value=None):
            job = b._start_job(sid, 'send', 'Update the implementation.')['job_id']
        def fail_after_edit(state, contract, text):
            (Path(state['worktree']) / 'calc.py').write_text('# partial work\n')
            raise RuntimeError('EXECUTOR_FAILURE_FIXTURE')
        with patch.object(b, 'peer_turn', side_effect=fail_after_edit):
            b.background_job(sid, job)
        state = b.read_json(b.run_dir(sid) / 'state.json')
        self.assertEqual(state['phase'], 'error')
        events = list(activity.Reader(b.run_dir(sid) / activity.FILENAME).read())
        self.assertTrue(any(e['kind'] == 'files' and 'calc.py' in e['text'] for e in events))
        self.assertTrue(any(e['kind'] == 'error' and 'EXECUTOR_FAILURE_FIXTURE' in e['text']
                            for e in events))
        self.assertTrue(any(e['kind'] == 'status' and e.get('status') == 'error' for e in events))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telos-origin-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.cwd = str(self.root / 'repo')

    def row(self, pane='%4', pid='100', cwd=None, attached='1', dead='0', group=''):
        return '\t'.join(('/tmp/test-tmux/default', pane, pid, attached, dead, group,
                          self.cwd if cwd is None else cwd))

    def discover(self, rows, ancestors=(100, 200)):
        def query(context, *args):
            return '\n'.join(rows) if args[0] == 'list-panes' else '@2'
        with patch.object(watch, 'tmux', side_effect=query), \
                patch.object(watch, 'codex_ancestors', return_value=set(ancestors)):
            return watch.origin(self.state, {}, cwd=self.cwd)

    def test_missing_environment_finds_unique_attached_codex_directory(self):
        result = self.discover([self.row(), self.row('%5', '200', cwd=str(self.root / 'elsewhere'))])
        self.assertEqual(result['pane'], '%4')
        self.assertEqual(result['window'], '@2')
        self.assertEqual(result['socket'], '/tmp/test-tmux/default')
        self.assertRegex(result['group'], r'^[0-9a-f]{24}$')
        # Linked windows can report the same pane more than once.
        self.assertEqual(self.discover([self.row(), self.row()]), result)

    def test_ambiguous_or_ineligible_panes_do_not_open(self):
        cases = [
            [self.row(), self.row('%5', '200')],
            [self.row(pid='999')],
            [self.row(attached='0')],
            [self.row(dead='1')],
            [self.row(group='a' * 24)],
            [self.row(cwd=self.cwd + '/subdir')],
            [self.row(cwd='relative')],
            [self.row(pane='invalid')],
            ['malformed'],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.assertIsNone(self.discover(rows))

    def test_explicit_environment_precedes_discovery_even_if_invalid(self):
        with patch.object(watch, 'discover_origin', side_effect=AssertionError('No fallback')), \
                patch.object(watch, 'tmux', return_value='@3'):
            result = watch.origin(self.state, {'TMUX': '/tmp/original,9,0', 'TMUX_PANE': '%8'},
                                  cwd=self.cwd)
            self.assertEqual(result['pane'], '%8')
            self.assertIsNone(watch.origin(self.state, {'TMUX': '/tmp/original,9,0'}, cwd=self.cwd))
        with patch.object(watch, 'discover_origin', side_effect=AssertionError('No fallback')), \
                patch.object(watch, 'tmux', side_effect=OSError('stale socket')):
            self.assertIsNone(watch.origin(self.state, {'TMUX': '/stale,9,0', 'TMUX_PANE': '%8'},
                                           cwd=self.cwd))

    def test_discovery_failure_is_optional_and_does_not_mutate_environment(self):
        environ = {}
        with patch.object(watch, 'tmux', side_effect=OSError('not installed')):
            self.assertIsNone(watch.origin(self.state, environ, cwd=self.cwd))
        self.assertEqual(environ, {})
        with patch.object(watch, 'tmux', return_value=self.row()), \
                patch.object(watch, 'codex_ancestors', side_effect=subprocess.TimeoutExpired('ps', 3)):
            self.assertIsNone(watch.origin(self.state, environ, cwd=self.cwd))

    def test_prepare_only_uses_trusted_cli_owner_directory(self):
        bridge = SimpleNamespace(STATE=self.state)
        state = {'repo': '/unrelated-executor', 'owner': {'transport': 'cli_queue', 'cwd': self.cwd}}
        with patch.object(watch, 'origin', return_value={'pane': '%4'}) as probe:
            watch.prepare(bridge, state)
        probe.assert_called_once_with(self.state, cwd=self.cwd)
        self.assertEqual(state['watch_origin'], {'pane': '%4'})
        for owner in (None, {'transport': 'desktop_ipc', 'cwd': self.cwd}):
            with patch.object(watch, 'origin', return_value=None) as probe:
                watch.prepare(bridge, {'repo': self.cwd, 'owner': owner})
            probe.assert_called_once_with(self.state, cwd=None)

    def test_codex_ancestry_uses_executable_names_without_process_arguments(self):
        output = ('100 1 /bin/zsh\n101 100 node\n102 101 /opt/codex/bin/codex\n'
                  '200 1 /bin/zsh\n201 200 /usr/bin/node\n300 300 /opt/bin/codex\n'
                  'bad process row\n')
        with patch.object(watch.subprocess, 'run', return_value=SimpleNamespace(stdout=output)) as probe:
            self.assertEqual(watch.codex_ancestors(), {100, 101, 102, 300})
        self.assertEqual(probe.call_args.args[0], ['/bin/ps', '-axo', 'pid=,ppid=,comm='])


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telos-watch-finish-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.group = 'a' * 24
        self.bridge = SimpleNamespace(STATE=self.root)
        (self.root / 'watch').mkdir()

    def record(self, sid='1' * 32, **fields):
        value = {'session_id': sid, 'phase': 'accepted', 'continuation': 'active',
                 'watch_origin': {'group': self.group}, **fields}
        b.write_json(self.root / 'runs' / sid / 'state.json', value)

    def test_work_review_repair_pause_and_other_window_states(self):
        self.assertFalse(watch.group_finished(self.root, self.group))
        for phase in ('ready', 'running', 'responded', 'awaiting_review', 'not_accepted', 'error'):
            with self.subTest(phase=phase):
                self.record(phase=phase)
                self.assertFalse(watch.group_finished(self.root, self.group))
        self.record(phase='responded', continuation='paused')
        self.assertFalse(watch.group_finished(self.root, self.group))
        self.record()
        self.record('2' * 32, phase='running', watch_origin={'group': 'b' * 24})
        self.assertTrue(watch.group_finished(self.root, self.group))
        self.record(phase='error', continuation='cancelled')
        self.assertTrue(watch.group_finished(self.root, self.group))

    def test_project_waits_for_final_acceptance_and_cancelled_workers(self):
        project = 'c' * 32
        self.record(project_id=project)
        self.assertFalse(watch.group_finished(self.root, self.group))
        self.record(project, notification_only=True, project_id=project,
                    phase='project_update', return_complete=False)
        self.assertFalse(watch.group_finished(self.root, self.group))
        self.record(project, notification_only=True, project_id=project,
                    phase='project_update', return_complete=True)
        self.assertTrue(watch.group_finished(self.root, self.group))
        self.record('2' * 32, project_id=project, phase='running',
                    watch_origin={'group': 'b' * 24})
        self.assertFalse(watch.group_finished(self.root, self.group))
        self.record(project, notification_only=True, project_id=project,
                    phase='project_update', continuation='cancelled')
        self.assertFalse(watch.group_finished(self.root, self.group))
        self.record('2' * 32, project_id=project, phase='error', continuation='cancelled',
                    watch_origin={'group': 'b' * 24})
        self.assertTrue(watch.group_finished(self.root, self.group))

    def test_cancellation_waits_for_real_worker_exit(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        self.addCleanup(lambda: process.poll() is None and process.terminate())
        self.record(phase='running', continuation='cancelled', job_pid=process.pid)
        self.assertFalse(watch.group_finished(self.root, self.group))
        process.terminate()
        process.wait(timeout=3)
        self.assertTrue(watch.group_finished(self.root, self.group))

    def test_unreadable_or_invalid_state_keeps_viewer_open(self):
        self.record()
        path = self.root / 'runs' / ('2' * 32) / 'state.json'
        path.parent.mkdir()
        for text in ('{', 'null', '{"session_id":"../other"}'):
            path.write_text(text)
            self.assertFalse(watch.group_finished(self.root, self.group))

    def test_close_requires_own_pane_process_and_matching_group(self):
        self.record()
        context = {'socket': '/tmp/fixture.sock', 'pane': '%7', 'window': '@0', 'group': self.group}
        for ownership in (f'wrong\t{os.getpid()}', f'{self.group}\t{os.getpid() + 1}'):
            with patch.object(watch, 'origin', return_value=context), \
                    patch.object(watch, 'tmux', return_value=ownership) as command:
                self.assertFalse(watch.close_finished_panel(self.bridge, self.group))
                self.assertFalse(any(call.args[1] == 'kill-pane' for call in command.call_args_list))
        with patch.object(watch, 'origin', return_value=context), \
                patch.object(watch, 'tmux', return_value=f'{self.group}\t{os.getpid()}') as command:
            self.assertTrue(watch.close_finished_panel(self.bridge, self.group))
            self.assertEqual(command.call_args.args, (context, 'kill-pane', '-t', '%7'))
        with patch.object(watch, 'origin', return_value=None):
            self.assertFalse(watch.close_finished_panel(self.bridge, self.group))

    def test_dispatch_or_panel_lock_prevents_closing_during_new_launch(self):
        self.record()
        for path in (self.root / 'coordinator.lock', self.root / 'watch' / (self.group + '.lock')):
            with self.subTest(lock=path.name), path.open('a+') as stream, \
                    patch.object(watch, 'tmux') as command:
                fcntl.flock(stream, fcntl.LOCK_EX)
                self.assertFalse(watch.close_finished_panel(self.bridge, self.group))
                command.assert_not_called()


@unittest.skipUnless(shutil.which('tmux'), 'tmux optional; not installed')
class TmuxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telos-watch-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.socket = str(self.root / 'tmux.sock')
        self.context = {'socket': self.socket}
        watch.tmux(self.context, '-f', '/dev/null', 'new-session', '-d', '-s', 'observer-test',
                   '-x', '160', '-y', '40', '/bin/sleep', '120')
        self.addCleanup(lambda: subprocess.run([shutil.which('tmux'), '-S', self.socket, 'kill-server'],
                                              capture_output=True))
        self.pane = watch.tmux(self.context, 'display-message', '-p', '#{pane_id}')
        self.bridge = SimpleNamespace(STATE=self.root/'state', CACHE=self.root/'cache',
                                      INSTALL=Path(__file__).parent.resolve())
        self.env = {'TMUX': self.socket + ',1,0', 'TMUX_PANE': self.pane}
        self.context = watch.origin(self.bridge.STATE, self.env)
        self.assertIsNotNone(self.context)

    def create(self, n):
        sid = str(n) * 32
        path = self.bridge.STATE / 'runs' / sid
        path.mkdir(parents=True)
        (path / 'state.json').write_text(json.dumps({
            'session_id': sid, 'model': 'fixture-model', 'phase': 'running',
            'continuation': 'active', 'created_at': n, 'watch_origin': self.context}))
        activity.Recorder(path, sid).emit('message', f'TASK_{n}_LIVE')
        return sid

    def capture(self, pane):
        return watch.tmux(self.context, 'capture-pane', '-p', '-t', pane)

    def update(self, sid, **fields):
        path = self.bridge.STATE / 'runs' / sid / 'state.json'
        value = json.loads(path.read_text())
        b.write_json(path, {**value, **fields})

    def test_auto_close_waits_for_every_executor_and_new_work_reopens(self):
        first, second = self.create(1), self.create(2)
        panel = watch.panel(self.bridge, self.context)['pane_id']
        wait_for(lambda: 'TASK_2_LIVE' in self.capture(panel))
        self.update(first, phase='accepted')
        self.update(second, phase='awaiting_review')
        time.sleep(1.3)
        self.assertEqual(len(watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines()), 2)
        # Even a user's inherited remain-on-exit setting must not leave a dead split.
        watch.tmux(self.context, 'set-option', '-p', '-t', panel, 'remain-on-exit', 'on')
        self.update(second, phase='accepted')
        wait_for(lambda: watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}') == self.pane)
        self.assertEqual(watch.tmux(self.context, 'display-message', '-p', '#{pane_id}'), self.pane)
        third = self.create(3)
        reopened = watch.panel(self.bridge, self.context)['pane_id']
        wait_for(lambda: 'TASK_3_LIVE' in self.capture(reopened))
        self.update(third, phase='accepted')
        wait_for(lambda: watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}') == self.pane)

    def test_project_final_review_and_manual_history_keep_pane_open(self):
        first, second = self.create(1), self.create(2)
        project = 'c' * 32
        courier = self.bridge.STATE / 'runs' / project / 'state.json'
        b.write_json(courier, {'session_id': project, 'project_id': project,
                              'notification_only': True, 'phase': 'project_update',
                              'continuation': 'active'})
        for sid in (first, second):
            self.update(sid, project_id=project)
        panel = watch.panel(self.bridge, self.context)['pane_id']
        wait_for(lambda: 'TASK_2_LIVE' in self.capture(panel))
        for sid in (first, second):
            self.update(sid, phase='accepted')
        time.sleep(1.3)
        self.assertEqual(len(watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines()), 2)
        self.update(project, return_complete=True)
        wait_for(lambda: watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}') == self.pane)
        manual = watch.panel(self.bridge, self.context, keep_open=True)['pane_id']
        wait_for(lambda: 'TASK_2_LIVE' in self.capture(manual))
        time.sleep(1.3)
        self.assertIn(manual, watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines())
        watch.tmux(self.context, 'send-keys', '-t', manual, 'q')
        wait_for(lambda: watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}') == self.pane)

    def test_cancelled_task_closes_only_after_its_process_exits(self):
        sid = self.create(1)
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        self.addCleanup(lambda: process.poll() is None and process.terminate())
        self.update(sid, continuation='cancelled', job_pid=process.pid)
        panel = watch.panel(self.bridge, self.context)['pane_id']
        wait_for(lambda: 'TASK_1_LIVE' in self.capture(panel))
        time.sleep(1.3)
        self.assertIn(panel, watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines())
        process.terminate()
        process.wait(timeout=3)
        wait_for(lambda: watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}') == self.pane)

    def test_one_panel_focus_switch_close_and_reopen(self):
        self.create(1); self.create(2)
        # Independent concurrent task launches still create only one panel.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            opened = list(pool.map(lambda _: watch.panel(self.bridge, self.context), range(2)))
        panes = {r['pane_id'] for r in opened}
        self.assertEqual(len(panes), 1)
        panel = panes.pop()
        self.assertEqual(watch.tmux(self.context, 'display-message', '-p', '#{pane_id}'), self.pane)
        self.assertEqual(len(watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines()), 2)
        wait_for(lambda: 'TASK_2_LIVE' in self.capture(panel))
        watch.tmux(self.context, 'send-keys', '-t', panel, 'n')
        wait_for(lambda: 'TASK_1_LIVE' in self.capture(panel))
        watch.tmux(self.context, 'send-keys', '-t', panel, 'q')
        wait_for(lambda: len(watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines()) == 1)
        # The executor state and recorder are independent of the vanished viewer.
        run = self.bridge.STATE / 'runs' / ('2' * 32)
        self.assertEqual(json.loads((run/'state.json').read_text())['phase'], 'running')
        activity.Recorder(run, '2' * 32).emit('message', 'AFTER_VIEWER_CLOSED')
        reopened = watch.panel(self.bridge, self.context)['pane_id']
        wait_for(lambda: 'AFTER_VIEWER_CLOSED' in self.capture(reopened))
        self.assertEqual(watch.tmux(self.context, 'display-message', '-p', '#{pane_id}'), self.pane)

    def test_reasoning_grows_in_real_pane_and_task_switch_does_not_mix_streams(self):
        first, second = self.create(1), self.create(2)
        panel = watch.panel(self.bridge, self.context)['pane_id']
        recorders = {sid: activity.Recorder(self.bridge.STATE / 'runs' / sid, sid) for sid in (first, second)}
        for sid, text in ((first, 'FIRST_REASONING '), (second, 'SECOND_REASONING ')):
            recorders[sid].emit('reasoning', text, item_id='same-id', offset=0,
                                event_type='reasoning.delta', status='streaming')
        wait_for(lambda: 'SECOND_REASONING' in self.capture(panel))
        self.assertNotIn('FIRST_REASONING', self.capture(panel))
        recorders[second].emit('reasoning', 'INCREMENT', item_id='same-id', offset=len('SECOND_REASONING '),
                               event_type='reasoning.delta', status='streaming')
        wait_for(lambda: 'SECOND_REASONING INCREMENT' in self.capture(panel))
        self.assertEqual(self.capture(panel).count('SECOND_REASONING'), 1)
        watch.tmux(self.context, 'send-keys', '-t', panel, 'n')
        wait_for(lambda: 'FIRST_REASONING' in self.capture(panel))
        self.assertNotIn('SECOND_REASONING', self.capture(panel))
        watch.tmux(self.context, 'send-keys', '-t', panel, 'q')
        wait_for(lambda: len(watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines()) == 1)
        self.assertEqual(json.loads((self.bridge.STATE / 'runs' / first / 'state.json').read_text())['phase'], 'running')

    def test_dead_pane_reused_and_invalid_context_does_not_target_another_window(self):
        self.create(1)
        pane = watch.panel(self.bridge, self.context)['pane_id']
        wait_for(lambda: 'TASK_1_LIVE' in self.capture(pane))
        watch.tmux(self.context, 'set-option', '-p', '-t', pane, 'remain-on-exit', 'on')
        watch.tmux(self.context, 'send-keys', '-t', pane, 'q')
        wait_for(lambda: watch.tmux(self.context, 'display-message', '-p', '-t', pane, '#{pane_dead}') == '1')
        self.assertEqual(watch.panel(self.bridge, self.context)['pane_id'], pane)
        wait_for(lambda: 'TASK_1_LIVE' in self.capture(pane))
        self.assertIsNone(watch.origin(self.bridge.STATE, {**self.env, 'TMUX_PANE': '%99999999'}))

    def test_start_hook_opens_panel_and_closing_it_leaves_job_alive(self):
        repo = self.root / 'repo'
        repo.mkdir()
        b.git(repo, 'init', '-q')
        (repo / 'seed.txt').write_text('fixture\n')
        b.git(repo, 'add', 'seed.txt')
        b.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'seed')
        real_popen = subprocess.Popen
        jobs = []
        def launch(argv, **kwargs):
            # Exercise the actual dispatch hook with an independent child, but
            # never invoke a paid executor from this deterministic test.
            if len(argv) > 2 and argv[2] == '_job':
                process = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
                jobs.append(process)
                self.addCleanup(lambda: process.poll() is None and process.terminate())
                return process
            return real_popen(argv, **kwargs)
        questions = [{'id': 'q', 'prompt': 'Describe the seed.', 'reference': 'fixture', 'points': 100}]
        checks = [{'id': 'seed', 'argv': ['{python}', '-c', 'assert True']}]
        with patch.object(b, 'STATE', self.bridge.STATE), patch.object(b, 'CACHE', self.bridge.CACHE), \
                patch.object(b, 'CONFIG', self.root / 'config.json'), patch.dict(os.environ, self.env), \
                patch.object(b.subprocess, 'Popen', side_effect=launch):
            sid = b.create_session(str(repo), 'Inspect the seed.', questions, checks)['session_id']
            started = b._start_job(sid, 'send', 'Begin inspection.')
            self.assertEqual(started['phase'], 'running')
            stored = b.read_json(b.run_dir(sid) / 'state.json')
            self.assertEqual(stored['watch_origin']['group'], self.context['group'])
            panes = watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}').splitlines()
            self.assertEqual(len(panes), 2)
            panel = next(p for p in panes if p != self.pane)
            wait_for(lambda: sid in self.capture(panel))
            self.assertEqual(watch.tmux(self.context, 'display-message', '-p', '#{pane_id}'), self.pane)
            watch.tmux(self.context, 'kill-pane', '-t', panel)
            self.assertIsNone(jobs[0].poll())
            self.assertEqual(b.read_json(b.run_dir(sid) / 'state.json')['phase'], 'running')

    def test_panel_tag_failure_cleans_only_its_own_split(self):
        self.create(1)
        actual_tmux = watch.tmux
        def fail_tag(context, *args):
            if args[0] == 'set-option' and '@telos_watch_group' in args:
                raise OSError('tag fixture failure')
            return actual_tmux(context, *args)
        with patch.object(watch, 'tmux', side_effect=fail_tag), self.assertRaises(OSError):
            watch.panel(self.bridge, self.context)
        self.assertEqual(watch.tmux(self.context, 'list-panes', '-F', '#{pane_id}'), self.pane)


class NativeObserverTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    model = WorkflowTests.model
    create = WorkflowTests.create

    def test_real_codex_tool_message_work_history_and_private_exam(self):
        state, contract = self.create()
        answers = json.dumps({'answers': [{'id': q['id'], 'answer': '5' if i == 0 else '3'}
                                          for i, q in enumerate(self.q)]})
        def reply(request, n):
            if n == 1:
                name = 'shell_command' if any(t.get('name') == 'shell_command' for t in request['tools']) else 'exec_command'
                log_path = shlex.quote(str(b.run_dir(state['session_id']) / activity.FILENAME))
                command = ('echo OBSERVER_NATIVE_COMMAND; cat calc.py; '
                           f'if cat {log_path} >/dev/null 2>&1; then echo OBSERVER_PRIVATE_READ; '
                           'else echo OBSERVER_PRIVATE_DENIED; fi')
                args = {'command' if name == 'shell_command' else 'cmd': command}
                return {'type': 'function_call', 'id': 'observer_tool', 'call_id': 'observer_call',
                        'name': name, 'arguments': json.dumps(args)}
            if n == 2:
                # Actual Codex CLI emitted the tool result while the provider
                # is still waiting to send its final assistant response.
                wait_for(lambda: any(e['kind'] == 'command' and 'OBSERVER_NATIVE_COMMAND' in e['text']
                                     for e in activity.Reader(b.run_dir(state['session_id']) /
                                                              activity.FILENAME).read()))
            return message(answers if request.get('tool_choice') == 'none' else 'OBSERVER_NATIVE_DONE')
        with self.model(reply):
            result = b.peer_turn(state, contract, 'Read calc.py and report.')
            state['thread_id'] = result['thread_id']
            commit, _ = b.snapshot(state, 'observer-probe')
            exam = b.closed_book_exam(state, contract, commit)
        self.assertEqual(exam['executor_thread_id'], result['thread_id'])
        events = list(activity.Reader(b.run_dir(state['session_id']) / activity.FILENAME).read())
        self.assertTrue(any(e['kind'] == 'tool' and 'OBSERVER_NATIVE_COMMAND' in e['text'] for e in events))
        self.assertTrue(any(e['kind'] == 'command' and 'def add' in e['text'] for e in events))
        self.assertTrue(any(e['kind'] == 'command' and '\nOBSERVER_PRIVATE_DENIED\n' in e['text']
                            for e in events))
        self.assertFalse(any(e['kind'] == 'command' and '\nOBSERVER_PRIVATE_READ\n' in e['text']
                             for e in events))
        self.assertTrue(any(e['kind'] == 'message' and 'OBSERVER_NATIVE_DONE' in e['text'] for e in events))
        log = json.dumps(events)
        self.assertNotIn(answers, log)
        for question in self.q:
            self.assertNotIn(question['reference'], log)
        self.assertNotIn(self.checks[0]['argv'][2], log)
        self.assertFalse(any(e['phase'] == 'exam' and e['kind'] != 'status' for e in events))


if __name__ == '__main__':
    unittest.main()

"""Observer tests: real pipes, real tmux, and real isolated CLI with a model fixture."""
import concurrent.futures
import contextlib
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

    def test_missing_git_leaves_a_diagnostic_without_failing_work(self):
        bridge = SimpleNamespace(STATE=self.root, run_dir=lambda sid: self.root)
        with patch('preflight.git_executable', return_value=None):
            activity.files(bridge, {'session_id': 'a' * 32})
        self.assertEqual(self.events()[-1]['kind'], 'diagnostic')


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

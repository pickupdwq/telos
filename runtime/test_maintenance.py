"""Regression coverage for snapshot fidelity, process recovery and scheduling."""
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from test_telos import b, BridgeTests
from test_projects import ProjectTests
from processes import process_running, process_start, reap_background, kill_process_tree


@contextlib.contextmanager
def bounded_graph():
    def expired(*_):
        raise AssertionError('A 30-task dependency graph exceeded five seconds')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(5)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class SnapshotTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def session(self):
        sid = b.create_session(str(self.repo), 'Review snapshot fidelity', self.q, self.checks)['session_id']
        return b.read_json(b.run_dir(sid) / 'state.json')

    def test_export_ignore_cannot_hide_a_failing_test(self):
        state = self.session()
        tree = Path(state['worktree'])
        (tree / '.gitattributes').write_text('test_bad.py export-ignore\n')
        (tree / 'test_bad.py').write_text(
            'import unittest\nclass Bad(unittest.TestCase):\n    def test_bad(self): self.assertEqual(5, 6)\n')
        (tree / 'test_good.py').write_text(
            'import unittest\nclass Good(unittest.TestCase):\n    def test_good(self): self.assertEqual(5, 5)\n')
        _, target = b.snapshot(state, 'fidelity')
        result = b.executable_checks({'checks': [{'id': 'tests', 'argv': ['{python}', '-c',
            'import subprocess,sys; subprocess.run([sys.executable,"-m","unittest","discover"],check=True)']}]}, target)[0]
        self.assertTrue((target / 'test_bad.py').is_file())
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Ran 2 tests', result['stderr'])

    def test_exact_bytes_names_and_executable_mode_survive_attributes(self):
        state = self.session()
        tree = Path(state['worktree'])
        (tree / '.gitattributes').write_text('release.txt export-subst\n*.bin export-ignore\n')
        contents = {'release.txt': b'commit=$Format:%H$\n',
                    'nested/文件\twith\nspaces.bin': b'\x00\xff\r\n' * 300000,
                    'run.sh': b'#!/bin/sh\necho ok\n'}
        for name, data in contents.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (tree / 'run.sh').chmod(0o755)
        commit, target = b.snapshot(state, 'bytes')
        for name, data in contents.items():
            self.assertEqual((target / name).read_bytes(), data)
        self.assertTrue((target / 'run.sh').stat().st_mode & 0o111)
        self.assertEqual(b.git(tree, 'rev-parse', 'HEAD'), commit)
        expected = set(contents) | {'.gitattributes', 'calc.py'}
        self.assertEqual({str(p.relative_to(target)) for p in target.rglob('*') if p.is_file()}, expected)

    def test_symlink_is_rejected_and_partial_snapshot_removed(self):
        state = self.session()
        (Path(state['worktree']) / 'link.py').symlink_to('calc.py')
        with self.assertRaisesRegex(b.GateError, 'path/link'):
            b.snapshot(state, 'unsafe')
        self.assertFalse((b.CACHE / 'snapshots' / state['session_id'] / 'unsafe').exists())

    def test_scope_preserves_leading_whitespace_in_untracked_and_tracked_paths(self):
        for tracked in (False, True):
            for name in (' a.py', '\ta.py', '\na.py'):
                with self.subTest(tracked=tracked, name=name):
                    if tracked:
                        (self.repo / name).write_text('old\n')
                        b.git(self.repo, 'add', '--', name)
                        b.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                              'commit', '-qm', 'fixture')
                    sid = b.create_session(str(self.repo), 'Implement a.py', self.q, self.checks,
                                           allowed_paths=['a.py'])['session_id']
                    state = b.read_json(b.run_dir(sid) / 'state.json')
                    (Path(state['worktree']) / name).write_text('changed\n')
                    with self.assertRaisesRegex(b.GateError, 'outside task scope'):
                        b.snapshot(state, 'scope')
                    self.assertEqual(b.git(state['worktree'], 'rev-parse', 'HEAD'), state['base_commit'])

    def test_explicit_whitespace_filename_is_allowed_without_normalization(self):
        for name in (' a.py', '\ta.py'):
            with self.subTest(name=name):
                sid = b.create_session(str(self.repo), 'Implement the named file', self.q, self.checks,
                                       allowed_paths=[name])['session_id']
                state = b.read_json(b.run_dir(sid) / 'state.json')
                (Path(state['worktree']) / name).write_text('VALUE = 1\n')
                _, target = b.snapshot(state, 'scope')
                self.assertEqual((target / name).read_text(), 'VALUE = 1\n')


class CheckTimeoutTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def test_cleanup_does_not_signal_unrelated_or_reused_pids(self):
        process = Mock(pid=101, returncode=None)
        initial = {101: (1, 'root'), 102: (101, 'old'), 103: (102, 'child'), 104: (1, 'unrelated')}
        current = {**initial, 102: (1, 'replacement')}
        with patch('processes._process_table', side_effect=[initial, current]), \
                patch('processes.os.kill') as kill, patch('processes.os.killpg') as killpg:
            kill_process_tree(process)
        kill.assert_called_once_with(103, signal.SIGKILL)
        killpg.assert_called_once_with(101, signal.SIGKILL)
        process.kill.assert_called_once_with()

    def detached_check(self, *, parent_exits=False):
        sid = b.create_session(str(self.repo), 'Check bounded timeout', self.q, self.checks)['session_id']
        state = b.read_json(b.run_dir(sid) / 'state.json')
        _, target = b.snapshot(state, 'timeout')
        code = ('import subprocess,sys,time\n'
                'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(5)"],start_new_session=True)\n'
                'print("child="+str(p.pid),flush=True)\n'
                'print("stderr marker",file=sys.stderr,flush=True)\n'
                + ('' if parent_exits else 'time.sleep(20)\n'))
        started = time.monotonic()
        result = b.executable_checks({'checks': [{'id': 'detached', 'argv': ['{python}', '-c', code],
                                                  'timeout_seconds': 1}]}, target)[0]
        elapsed = time.monotonic() - started
        pid = int(result['stdout'].split('child=')[1].splitlines()[0])
        self.addCleanup(self.stop_fixture, pid)
        return result, elapsed, pid

    @staticmethod
    def stop_fixture(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_timeout_terminates_detached_descendant_and_preserves_partial_output(self):
        result, elapsed, pid = self.detached_check()
        self.assertEqual(result['status'], 'timeout')
        self.assertIn('stderr marker', result['stderr'])
        self.assertLess(elapsed, 3.5)
        self.assertFalse(process_running(pid))

    def test_exited_parent_with_inherited_pipes_still_has_bounded_timeout(self):
        result, elapsed, _ = self.detached_check(parent_exits=True)
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(result['output_incomplete'])
        self.assertLess(elapsed, 3.5)


class StorageInventoryTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create

    def test_corrupt_record_does_not_block_healthy_project_inventory_or_allow_cleanup(self):
        pid = self.create([self.task('a')])
        broken = b.run_dir('f' * 32) / 'state.json'
        broken.parent.mkdir(parents=True)
        broken.write_text('{broken')
        store = b.storage_manager()
        result = store.inventory(pid)
        self.assertEqual(result['target_id'], pid)
        self.assertFalse(result['complete'])
        self.assertEqual(result['diagnostics'][0]['path'], str(broken))
        self.assertEqual(result['bytes'], sum(item['bytes'] for item in result['paths']))
        for action in ('archive', 'cleanup'):
            with self.subTest(action=action), self.assertRaisesRegex(b.GateError, 'scope'):
                store.manage(action, pid, dry_run=False)
        self.assertFalse((b.STATE / 'archives' / pid).exists())
        self.assertEqual(broken.read_text(), '{broken')
        self.assertFalse((b.STATE / 'quarantine').exists())

    def test_global_inventory_isolates_corrupt_projects_and_malformed_session_objects(self):
        pid = self.create([self.task('a')])
        broken_project = b.project_manager().path('e' * 32)
        broken_project.parent.mkdir(parents=True)
        broken_project.write_text('{broken')
        broken = b.run_dir('f' * 32) / 'state.json'
        broken.parent.mkdir(parents=True)
        for payload in ('{broken', 'null', '[]', '{}'):
            with self.subTest(payload=payload):
                broken.write_text(payload)
                result = b.storage_manager().inventory()
                self.assertIn(pid, [task['target_id'] for task in result['tasks']])
                self.assertFalse(result['complete'])
                self.assertEqual({item['path'] for item in result['diagnostics']},
                                 {str(broken), str(broken_project)})
                self.assertEqual(broken.read_text(), payload)


class SchedulingTests(ProjectTests):
    def returned(self, pid):
        state = self.child(pid, 'a')
        state['phase'] = 'responded'
        b.write_json(b.run_dir(state['session_id']) / 'state.json', state)
        return state

    def test_rejected_requests_do_not_consume_budget(self):
        pid = self.create([self.task('a')], max_jobs=2)
        self.start(pid)
        state = self.returned(pid)
        for _ in range(3):
            with self.assertRaisesRegex(b.GateError, 'nonempty'):
                b.start_job(state['session_id'], 'send', '')
        self.assertEqual(self.state(pid)['jobs_started'], 1)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.start_job(state['session_id'], 'send', 'Continue implementation')
        self.assertEqual(self.state(pid)['jobs_started'], 2)

    def test_spawn_failure_refunds_budget_and_allows_retry(self):
        pid = self.create([self.task('a')], max_jobs=2)
        self.start(pid)
        state = self.returned(pid)
        for _ in range(2):
            with patch.object(b.subprocess, 'Popen', side_effect=OSError('spawn unavailable')):
                with self.assertRaisesRegex(b.GateError, 'could not start'):
                    b.start_job(state['session_id'], 'send', 'Continue implementation')
        self.assertEqual(self.state(pid)['jobs_started'], 1)
        self.assertEqual(self.child(pid, 'a')['turns'], 1)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.start_job(state['session_id'], 'send', 'Continue implementation')
        self.assertEqual(self.state(pid)['jobs_started'], 2)

    def test_scheduler_spawn_failures_do_not_drain_budget(self):
        pid = self.create([self.task('a')], max_jobs=1)
        original = b._start_job
        def failed(*args, **kwargs):
            with patch.object(b.subprocess, 'Popen', side_effect=OSError('spawn unavailable')):
                return original(*args, **kwargs)
        with patch.object(b, '_start_job', side_effect=failed):
            for _ in range(3):
                b.project_manager().tick(pid)
        self.assertEqual(self.state(pid)['jobs_started'], 0)
        self.assertEqual(self.child(pid, 'a')['turns'], 0)
        self.start(pid)
        self.assertEqual(self.state(pid)['jobs_started'], 1)

    def test_uncertain_spawn_keeps_reservation(self):
        pid = self.create([self.task('a')], max_jobs=3)
        self.start(pid)
        state = self.returned(pid)
        def ambiguous(sid, *_):
            current = b.read_json(b.run_dir(sid) / 'state.json')
            current.update(job_id='f' * 32, phase='running')
            b.write_json(b.run_dir(sid) / 'state.json', current)
            raise OSError('response lost after launch')
        with patch.object(b, '_start_job', side_effect=ambiguous):
            with self.assertRaises(OSError):
                b.start_job(state['session_id'], 'send', 'Continue implementation')
        self.assertEqual(self.state(pid)['jobs_started'], 2)

    def test_backoff_rejection_does_not_consume_budget(self):
        pid = self.create([self.task('a')])
        self.start(pid)
        state = self.returned(pid)
        state.update(phase='error', job_kind='evaluate', next_retry_at=time.time() + 60)
        b.write_json(b.run_dir(state['session_id']) / 'state.json', state)
        with self.assertRaisesRegex(b.GateError, 'backoff'):
            b.start_job(state['session_id'], 'evaluate')
        self.assertEqual(self.state(pid)['jobs_started'], 1)

    def test_standalone_and_project_share_provider_capacity(self):
        b.write_json(b.CONFIG, {'provider_max_parallel': 1})
        pid = self.create([self.task('a')])
        standalone = [b.create_session(str(self.repo), 'Standalone', self.q, self.checks)['session_id']
                      for _ in range(2)]
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.start_job(standalone[0], 'send', 'Work')
            with self.assertRaisesRegex(b.GateError, 'concurrency'):
                b.start_job(standalone[1], 'send', 'Work')
        self.assertEqual(self.start(pid)['tasks'][0]['phase'], 'pending')
        state = b.read_json(b.run_dir(standalone[0]) / 'state.json')
        state['phase'] = 'responded'
        b.write_json(b.run_dir(standalone[0]) / 'state.json', state)
        self.assertEqual(self.start(pid)['tasks'][0]['phase'], 'running')
        with self.assertRaisesRegex(b.GateError, 'concurrency'):
            b.start_job(standalone[1], 'send', 'Work')

    def test_provider_capacity_is_independent_between_providers(self):
        b.write_json(b.CONFIG, {'provider_max_parallel': 1})
        ids = [b.create_session(str(self.repo), 'Standalone', self.q, self.checks, provider=provider)['session_id']
               for provider in ('ark', 'deepseek')]
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            for sid in ids:
                self.assertEqual(b.start_job(sid, 'send', 'Work')['phase'], 'running')

    def test_concurrent_standalone_starts_respect_limit(self):
        import threading
        b.write_json(b.CONFIG, {'provider_max_parallel': 2})
        ids = [b.create_session(str(self.repo), 'Standalone', self.q, self.checks)['session_id'] for _ in range(4)]
        results, errors = [], []
        def start(sid):
            try:
                results.append(b.start_job(sid, 'send', 'Work'))
            except b.GateError as error:
                errors.append(str(error))
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            threads = [threading.Thread(target=start, args=(sid,)) for sid in ids]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all('concurrency' in e for e in errors))

    def test_dense_dependency_graph_at_task_limit(self):
        tasks = [self.task('t' + str(i), ['t' + str(j) for j in range(i)]) for i in range(30)]
        with bounded_graph():
            validated = b.project_manager().validate_tasks(tasks)
        self.assertEqual(len(validated), 30)
        for task in validated:
            task.update(phase='accepted', accepted_commit=task['id'])
        with bounded_graph():
            pins = b.project_manager().dependency_pins({'tasks': validated}, validated[-1])
        self.assertEqual(set(pins), {'t' + str(i) for i in range(29)})
        validated[0]['phase'] = 'not_accepted'
        with self.assertRaisesRegex(b.GateError, 'Dependency not accepted'):
            b.project_manager().dependency_pins({'tasks': validated}, validated[-1])


class ProcessRecoveryTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def test_exited_child_without_result_recovers_once(self):
        sid = b.create_session(str(self.repo), 'Recovery', self.q, self.checks)['session_id']
        with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.05)']) as child:
            state = b.read_json(b.run_dir(sid) / 'state.json')
            state.update(phase='running', job_kind='evaluate', job_id='e' * 32,
                         job_pid=child.pid, job_started=time.time(), rounds=1)
            b.write_json(b.run_dir(sid) / 'job-{}.json'.format(state['job_id']), {'reserved_round': 1})
            b.write_json(b.run_dir(sid) / 'state.json', state)
            time.sleep(0.2)  # Do not poll/wait the child before exercising status.
            self.assertEqual(b.status(sid)['phase'], 'error')
            self.assertEqual(b.status(sid)['infra_failures'], 1)
            self.assertEqual(b.status(sid)['rounds'], 0)

    def test_zombie_owned_by_another_process_is_not_running(self):
        script = ('import os,sys\nchild=os.fork()\n'
                  'if child==0: os._exit(0)\n'
                  'print(child,flush=True)\nsys.stdin.read(1)\nos.waitpid(child,0)\n')
        with subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True) as parent:
            try:
                pid = int(parent.stdout.readline())
                deadline = time.monotonic() + 3
                while process_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(process_running(pid))
            finally:
                parent.communicate('x', timeout=5)

    def test_reaper_collects_child_and_live_identity_is_checked(self):
        with subprocess.Popen([sys.executable, '-c', 'pass']) as child:
            reap_background(child)
            deadline = time.monotonic() + 3
            while child.returncode is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(child.returncode)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
        start = process_start(os.getpid())
        self.assertIsNotNone(start)
        self.assertTrue(process_running(os.getpid(), start))
        self.assertFalse(process_running(os.getpid(), 'different process start'))


class ProjectRecoveryTests(ProjectTests):
    def test_dead_verification_child_recovers(self):
        pid = self.create([self.task('a')])
        with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.05)']) as child:
            project = self.state(pid)
            project.update(phase='verifying', verification_id='v', verification_pid=child.pid, integration={})
            b.project_manager().save(project)
            time.sleep(0.2)
            b.project_manager().recover()
        self.assertEqual(self.state(pid)['phase'], 'integration_failed')

    def test_interrupted_spawn_without_pid_recovers(self):
        pid = self.create([self.task('a')])
        project = self.state(pid)
        project.update(phase='verifying', verification_id='v', verification_started=time.time() - 60, integration={})
        b.project_manager().save(project)
        b.project_manager().recover()
        self.assertEqual(self.state(pid)['phase'], 'integration_failed')

    def test_recovery_does_not_overwrite_completed_verification(self):
        pid = self.create([self.task('a')])
        project = self.state(pid)
        project.update(phase='verifying', verification_id='v', verification_pid=12345, integration={})
        b.project_manager().save(project)
        def completed(*_):
            current = self.state(pid)
            current['phase'] = 'awaiting_final_review'
            b.project_manager().save(current)
            return False
        with patch('projects.process_running', side_effect=completed):
            b.project_manager().recover()
        self.assertEqual(self.state(pid)['phase'], 'awaiting_final_review')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name)
        for cls in (SnapshotTests, SchedulingTests, ProcessRecoveryTests, ProjectRecoveryTests)
        for name in cls.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()

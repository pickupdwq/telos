"""No-model preflight regressions and optional real-sandbox integration checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import executor
import isolation
import preflight
import providers
import telos


class FakeBridge:
    GateError = providers.ProviderError


class FakeRunner:
    def __init__(self, fail_at=None, error=None):
        self.calls = []
        self.fail_at = fail_at
        self.error = error

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            return SimpleNamespace(returncode=1, stdout=b'',
                                   stderr=b'topsecret xcrun_db /tmp/cache: Operation not permitted')
        return SimpleNamespace(returncode=0, stdout=b'ok', stderr=b'')


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_probe_order_is_bounded_and_never_invokes_a_model_or_repo_hooks(self):
        runner = FakeRunner()
        evidence = preflight.run(FakeBridge(), '/bin/sh', self.work, {'PATH': '/usr/bin:/bin'},
                                 'PROFILE', check_repo=True, secrets=('topsecret',), runner=runner)
        self.assertEqual(evidence['status'], 'passed')
        self.assertEqual([item['name'] for item in evidence['checks']],
                         ['dedicated CLI', 'Python', 'Git', 'Git worktree'])
        self.assertEqual(len(runner.calls), 4)
        for command, kwargs in runner.calls:
            self.assertEqual(command[:3], ['/usr/bin/sandbox-exec', '-p', 'PROFILE'])
            self.assertEqual(Path(kwargs['cwd']), self.work)
            self.assertEqual(kwargs['env']['PATH'], '/usr/bin:/bin')
            self.assertNotIn('exec', command)
            self.assertFalse(any('model' in str(value).lower() for value in command))
        worktree = runner.calls[-1][0]
        self.assertIn('core.fsmonitor=false', worktree)
        self.assertIn('core.hooksPath=/dev/null', worktree)
        self.assertIn('rev-parse', worktree)
        self.assertNotIn('hooks/', ' '.join(worktree))
        # One CLI version probe per invocation, not one per request/retry.
        self.assertEqual(sum(command.count('--version') for command, _ in runner.calls), 2)

    def test_repo_probe_is_absent_when_not_requested(self):
        runner = FakeRunner()
        preflight.run(FakeBridge(), '/bin/sh', self.work, {'PATH': '/usr/bin:/bin'}, 'PROFILE',
                      check_repo=False, runner=runner)
        self.assertEqual(len(runner.calls), 3)
        self.assertFalse(any('rev-parse' in command for command, _ in runner.calls))

    def test_failure_is_actionable_bounded_and_redacted(self):
        runner = FakeRunner(fail_at=1)
        with self.assertRaises(providers.ProviderError) as caught:
            preflight.run(FakeBridge(), '/bin/sh', self.work, {'PATH': '/usr/bin:/bin'}, 'PROFILE',
                          secrets=('topsecret',), runner=runner)
        message = str(caught.exception)
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn('topsecret', message)
        self.assertIn('dedicated CLI', message)
        self.assertIn('Apple Git', message)
        self.assertLess(len(message), 1200)

    def test_runner_os_error_is_reported_without_retry(self):
        runner = FakeRunner(error=OSError('missing sandbox helper'))
        with self.assertRaises(providers.ProviderError) as caught:
            preflight.run(FakeBridge(), '/bin/sh', self.work, {'PATH': '/usr/bin:/bin'}, 'PROFILE',
                          runner=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn('missing sandbox helper', str(caught.exception))

    def test_selected_git_is_not_the_apple_xcrun_shim_when_clt_exists(self):
        selected = preflight.git_executable()
        self.assertTrue(selected)
        self.assertTrue(Path(selected).is_file())
        if preflight.CLT_GIT.is_file():
            self.assertNotEqual(Path(selected), Path('/usr/bin/git'))
        runtime = [str(Path(path)) for path in preflight.git_runtime_paths()]
        self.assertIn(str(Path(selected)), runtime)
        self.assertNotIn(str(Path.home()), runtime)

    def test_runtime_allowlist_does_not_add_private_home_or_cache(self):
        install = self.root / 'install'
        home = self.root / 'home'
        paths = [str(path) for path in isolation.runtime_paths(install, home)]
        self.assertNotIn(str(home), paths)
        self.assertIn(str(Path(preflight.git_executable())), paths)
        self.assertIn(str(home / '.local/share/telos-cli'), paths)


def binding():
    return {'provider': 'ark', 'model': 'model', 'reasoning_effort': 'max',
            'wire_api': 'responses', 'exam_wire_api': 'chat', 'exam_max_tokens': 100,
            'base_url': 'http://127.0.0.1:9/v1', 'http_timeout': 1, 'job_timeout': 2}


class ExecutorPreflightOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'worktree'
        self.work.mkdir()
        self.state = {'session_id': 'a' * 32, 'worktree': str(self.work), 'thread_id': None,
                      'job_id': 'job'}
        self.contract = {'task': 'task', 'context': 'context', 'executor': binding()}
        self.bridge = SimpleNamespace(
            HOME=self.root / 'home', CACHE=self.root / 'cache', INSTALL=self.root / 'install',
            GateError=providers.ProviderError,
            backend_bindings=lambda contract: {'executor': binding()},
            minimal_env=lambda: {'PATH': '/usr/bin:/bin'},
            git=lambda repo, *args: '/tmp/common.git',
            write_json=lambda *args, **kwargs: None,
            read_json=lambda path: {},
            encoded=json.dumps,
            sandbox_profile=lambda **kwargs: 'PROFILE')

    def tearDown(self):
        self.temp.cleanup()

    def test_work_preflight_runs_once_before_any_cli_process(self):
        calls = []
        def fail(*args, **kwargs):
            calls.append((args, kwargs))
            raise providers.ProviderError('preflight failed before inference')
        with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                patch.object(preflight, 'run', side_effect=fail), \
                patch.object(executor.subprocess, 'Popen') as popen:
            with self.assertRaises(providers.ProviderError):
                executor._run(self.bridge, self.state, self.contract, 'message',
                              'http://127.0.0.1:9/v1', 'KEY', 1234)
            popen.assert_not_called()
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(Path(args[2]), self.work)
        self.assertEqual(args[4], 'PROFILE')
        self.assertEqual(kwargs['check_repo'], True)
        self.assertIn('KEY', kwargs['secrets'])

    def test_exam_never_runs_the_worktree_preflight_or_repo_probe(self):
        self.state['thread_id'] = 'thread'
        with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                patch.object(preflight, 'run') as probe, \
                patch.object(self.bridge, 'git', side_effect=AssertionError('worktree probed during exam')) as git, \
                patch.object(executor.subprocess, 'Popen', side_effect=RuntimeError('launched')):
            with self.assertRaisesRegex(RuntimeError, 'launched'):
                executor._run(self.bridge, self.state, self.contract, 'message',
                              'http://127.0.0.1:9/v1', 'KEY', 1234, phase='exam')
            probe.assert_not_called()
            git.assert_not_called()


@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(),
                     'requires macOS sandbox-exec')
class RealSandboxPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.cache = self.root / 'cache'
        self.install = self.root / 'install'

    def tearDown(self):
        self.temp.cleanup()

    def test_real_sandbox_can_run_python_and_git_and_still_denies_private_paths(self):
        probe = subprocess.run(['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)',
                                sys.executable, '-c', 'print(1)'], capture_output=True, timeout=20)
        if probe.returncode:
            self.skipTest('nested Seatbelt unavailable in this outer sandbox: '
                          + probe.stderr.decode(errors='replace')[-200:])
        with patch.object(telos, 'HOME', self.home), patch.object(telos, 'CACHE', self.cache), \
                patch.object(telos, 'INSTALL', self.install):
            profile = telos.sandbox_profile(writable=[self.work], readable_cache=[self.work],
                                            network=False)
            evidence = preflight.run(FakeBridge(), sys.executable, self.work,
                                     {'PATH': '/usr/bin:/bin'}, profile, check_repo=False)
            self.assertEqual([item['name'] for item in evidence['checks']], ['dedicated CLI', 'Python', 'Git'])
            private = self.home / '.codex' / 'secret'
            private.parent.mkdir(parents=True)
            private.write_text('private')
            denied = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c',
                                     'open(' + repr(str(private)) + ').read()'],
                                    cwd=self.work, env={'PATH': '/usr/bin:/bin'},
                                    capture_output=True, timeout=20)
            self.assertNotEqual(denied.returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)

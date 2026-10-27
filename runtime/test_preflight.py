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
        self.assertEqual(evidence['interpreter'], str(sys.executable))
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
        python_probe = next(command for command, _ in runner.calls if '-I' in command)
        isolated = python_probe.index('-I')
        self.assertEqual(python_probe[isolated + 1], '-c')
        # One CLI version probe per invocation, not one per request/retry.
        self.assertEqual(sum(command.count('--version') for command, _ in runner.calls), 2)

    def test_interpreter_evidence_preserves_the_exact_preflight_executable_path(self):
        interpreter = self.root / 'python-entrypoint'
        interpreter.symlink_to(sys.executable)
        runner = FakeRunner()
        with patch.object(preflight.sys, 'executable', str(interpreter)):
            evidence = preflight.run(FakeBridge(), '/bin/sh', self.work, {'PATH': '/usr/bin:/bin'},
                                     'PROFILE', runner=runner)
        self.assertEqual(evidence['interpreter'], str(interpreter))
        self.assertNotEqual(evidence['interpreter'], str(interpreter.resolve()))

    def test_python_probe_isolated_mode_ignores_repository_modules_and_startup_paths(self):
        marker = self.root / 'repo-import-ran'
        payload = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("ran")\n'
        (self.work / 'json.py').write_text(payload)
        (self.work / 'ssl.py').write_text(payload)
        (self.work / 'sitecustomize.py').write_text(payload)
        env = {**os.environ, 'PYTHONPATH': str(self.work), 'PYTHONSTARTUP': str(self.work / 'json.py')}
        probe = subprocess.run([sys.executable, '-I', '-c', 'import json, ssl, sys; print("ok")'],
                               cwd=self.work, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertFalse(marker.exists(), 'repository module or startup customization executed')

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
        self.assertIn(str(Path(selected).resolve()), runtime)
        self.assertNotIn(str(Path.home()), runtime)

    def test_runtime_allowlist_does_not_add_private_home_or_cache(self):
        install = self.root / 'install'
        home = self.root / 'home'
        paths = [str(path) for path in isolation.runtime_paths(install, home)]
        self.assertNotIn(str(home), paths)
        self.assertIn(str(Path(preflight.git_executable()).resolve()), paths)
        self.assertIn(str(home / '.local/share/telos-cli'), paths)

    def test_symlinked_git_keeps_a_narrow_canonical_runtime_allowlist(self):
        selected = Path(preflight.git_executable()).resolve()
        alias = self.root / 'bin' / 'git'
        alias.parent.mkdir()
        alias.symlink_to(selected)
        # Reproduce package-manager entrypoints even on hosts without Homebrew.
        preflight._git_runtime_dirs.cache_clear()
        self.addCleanup(preflight._git_runtime_dirs.cache_clear)
        with patch.object(preflight, 'git_executable', return_value=str(alias)):
            runtime = preflight.git_runtime_paths()
            paths = isolation.runtime_paths(self.root / 'install', self.root / 'home')
        self.assertIn(selected, runtime)
        self.assertIn(selected, paths)
        self.assertNotIn(alias, runtime)
        self.assertNotIn(alias.parent.resolve(), runtime)
        self.assertNotIn(self.root.resolve(), runtime)


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
                      'job_id': 'job', 'base_commit': 'a' * 40}
        self.contract = {'task': 'task', 'context': 'context', 'executor': binding()}
        self.bridge = SimpleNamespace(
            HOME=self.root / 'home', CACHE=self.root / 'cache', INSTALL=self.root / 'install',
            GateError=providers.ProviderError,
            backend_bindings=lambda contract: {'executor': binding()},
            execution_binding=lambda state, contract: binding(),
            minimal_env=lambda: {'PATH': '/usr/bin:/bin'},
            git=lambda repo, *args: '/tmp/common.git',
            write_json=lambda *args, **kwargs: None,
            read_json=lambda path: {},
            encoded=json.dumps,
            sandbox_profile=lambda **kwargs: 'PROFILE')
        # These tests mock process launch; provide their own CLI entrypoint
        # instead of silently relying on a developer's global Codex install.
        self.cli = self.bridge.HOME / '.local/share/telos-cli/node_modules/.bin/codex'
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text('#!/bin/sh\nexit 99\n')
        self.cli.chmod(0o700)
        lookup = patch.object(executor.shutil, 'which', return_value=None)
        self.addCleanup(lookup.stop)
        lookup.start()

    def tearDown(self):
        self.temp.cleanup()

    def _capture_prompt(self, *, thread_id=None, phase='work', contract=None, evidence=None):
        self.state['thread_id'] = thread_id
        prompts = []

        class CapturingProcess:
            returncode = 1

            def communicate(self, payload, timeout=None):
                prompts.append(payload.decode())
                return b'', b''

        with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                patch.object(preflight, 'run', return_value=evidence or {}) as probe, \
                patch.object(executor.subprocess, 'Popen', return_value=CapturingProcess()) as popen:
            with self.assertRaises(providers.ProviderError):
                executor._run(self.bridge, self.state, contract or self.contract, 'message',
                              'http://127.0.0.1:9/v1', 'KEY', 1234, phase=phase)
        if phase == 'work':
            self.assertEqual(probe.call_count, 1)
        else:
            probe.assert_not_called()
        self.assertEqual(len(prompts), 1)
        self.assertIn(str(self.cli), popen.call_args.args[0])
        return prompts[0]

    def test_initial_work_prompt_has_compact_verified_guidance_without_probe_output(self):
        evidence = {'status': 'passed', 'cwd': str(self.work), 'interpreter': '/verified/python',
                    'checks': [{'name': 'Python', 'output': 'RAW PROBE OUTPUT KEY'}]}
        prompt = self._capture_prompt(evidence=evidence)
        self.assertIn('Task:\ntask', prompt)
        self.assertIn('Verified work guidance', prompt)
        self.assertIn('/verified/python', prompt)
        self.assertIn(str(self.work), prompt)
        self.assertIn('Do not repeat bootstrap, version, interpreter, Git availability', prompt)
        self.assertIn('diagnose only that error narrowly', prompt)
        self.assertNotIn('RAW PROBE OUTPUT', prompt)
        self.assertNotIn('KEY', prompt)
        guidance = prompt.split('Verified work guidance', 1)[1].split('Codex message:', 1)[0]
        self.assertLessEqual(len(guidance), 2500)

    def test_resumed_work_prompt_includes_guidance_without_replaying_the_task(self):
        evidence = {'status': 'passed', 'cwd': str(self.work), 'interpreter': '/verified/python',
                    'checks': [{'name': 'Python', 'output': 'Python 3.12.1'}]}
        prompt = self._capture_prompt(thread_id='thread', evidence=evidence)
        self.assertIn('Verified work guidance', prompt)
        self.assertIn('/verified/python', prompt)
        self.assertNotIn('Task:\ntask', prompt)
        self.assertTrue(prompt.endswith('Codex message:\nmessage'))

    def test_audit_guidance_scopes_source_artifacts_and_frozen_identity(self):
        evidence = {'status': 'passed', 'cwd': str(self.root / 'audit_room'),
                    'interpreter': '/verified/python', 'checks': []}
        contract = {**self.contract, 'audit': {'targets': []}}
        artifacts = self.root / 'artifacts'
        text = executor._work_guidance(evidence, contract, self.state, artifact_root=artifacts)
        self.assertIn(str(self.work), text)
        self.assertIn(str(artifacts), text)
        self.assertIn(str(artifacts / 'report.json'), text)
        self.assertIn(str(artifacts / 'audit-report-help.json'), text)
        self.assertIn(self.state['base_commit'], text)
        self.assertIn('read-only', text)
        self.assertIn('offline source', text)
        self.assertNotIn('audit-probe-help.json', text)
        self.assertIn('Git metadata and whole-root listings are not granted', text)
        self.assertIn('scoped file bytes/hashes', text)
        self.assertIn('Do not run source Git or repository-root discovery', text)
        self.assertLessEqual(len(text), 2500)

    def test_online_audit_guidance_keeps_scoped_broker_without_probe_output(self):
        evidence = {'status': 'passed', 'cwd': str(self.root / 'audit_room'),
                    'interpreter': '/verified/python', 'checks': [{'output': 'RAW SECRET'}]}
        contract = {**self.contract, 'audit': {'targets': [{'origin': 'http://127.0.0.1:8000'}]}}
        artifacts = self.root / 'artifacts'
        text = executor._work_guidance(evidence, contract, self.state, artifact_root=artifacts)
        self.assertIn('online/mixed', text)
        self.assertIn('authenticated local broker', text)
        self.assertIn('declared targets', text)
        self.assertIn('never direct network', text)
        self.assertIn(str(artifacts / 'audit-report-help.json'), text)
        self.assertIn(str(artifacts / 'audit-probe-help.json'), text)
        self.assertNotIn('RAW SECRET', text)
        self.assertLessEqual(len(text), 2500)

    def test_guidance_rejects_incomplete_preflight_evidence(self):
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            executor._work_guidance({'status': 'passed', 'interpreter': '/verified/python'},
                                    self.contract, self.state)

    def test_exam_prompt_remains_exactly_message_only(self):
        contract = {**self.contract, 'task_type': 'audit', 'audit': {'targets': [], 'scope_paths': []}}
        prompt = self._capture_prompt(thread_id='thread', phase='exam', contract=contract)
        self.assertEqual(prompt, 'Codex message:\nmessage')
        self.assertNotIn('Verified work guidance', prompt)
        self.assertNotIn(str(self.work), prompt)
        self.assertNotIn('audit-report-help', prompt)

    def test_work_guidance_reports_remaining_capacity_without_private_ledger_data(self):
        self.bridge.usage_snapshot = lambda *args: {
            'budget_state': {'remaining': {'max_requests': 8, 'max_total_tokens': 3000}},
            'work_budget_state': {'remaining': {'max_requests': 5, 'max_total_tokens': 2000}},
            'private': 'DO_NOT_FORWARD'}
        contract = {**self.contract, 'budget': {'max_requests': 10, 'exam_reserve_requests': 3}}
        evidence = {'status': 'passed', 'cwd': str(self.work), 'interpreter': '/verified/python'}
        prompt = self._capture_prompt(thread_id='thread', contract=contract, evidence=evidence)
        self.assertIn('"max_requests":5', prompt)
        self.assertIn('"max_total_tokens":2000', prompt)
        self.assertIn('reserve one work request for a final factual report', prompt)
        self.assertNotIn('DO_NOT_FORWARD', prompt)
        self.assertEqual(self._capture_prompt(thread_id='thread', phase='exam', contract=contract),
                         'Codex message:\nmessage')

    def test_timeout_keeps_cli_thread_identity_without_successful_submission(self):
        class TimedOut:
            pid = 1234
            returncode = None

            def kill(self):
                self.returncode = -9

            def communicate(self, payload=None, timeout=None):
                if payload is not None:
                    raise subprocess.TimeoutExpired('codex', timeout)
                return b'{"type":"thread.started","thread_id":"original-executor"}\n', b''

        evidence = {'status': 'passed', 'cwd': str(self.work), 'interpreter': sys.executable}
        with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                patch.object(preflight, 'run', return_value=evidence), \
                patch.object(executor.subprocess, 'Popen', return_value=TimedOut()), \
                patch('processes._process_table', return_value={}), \
                patch.object(executor.os, 'killpg') as kill:
            with self.assertRaises(providers.ExecutionTimeout) as caught:
                executor._run(self.bridge, self.state, self.contract, 'message',
                              'http://127.0.0.1:9/v1', 'KEY', 1234)
        self.assertEqual(caught.exception.thread_id, 'original-executor')
        kill.assert_called_once()
        other = providers.ExecutionTimeout('interrupted')
        executor._retain_thread(other, b'{"type":"thread.started","thread_id":"different"}\n',
                                {'thread_id': 'original-executor'})
        self.assertFalse(hasattr(other, 'thread_id'))

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
        self.assertEqual(args[1], str(self.cli))
        self.assertEqual(Path(args[2]), self.work)
        self.assertEqual(args[4], 'PROFILE')
        self.assertEqual(kwargs['check_repo'], True)
        self.assertIn('KEY', kwargs['secrets'])

    def test_missing_cli_stops_before_preflight_or_executor_launch(self):
        self.cli.unlink()
        with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                patch.object(preflight, 'run') as probe, \
                patch.object(executor.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(providers.ProviderError, 'dedicated Codex CLI not found'):
                executor._run(self.bridge, self.state, self.contract, 'message',
                              'http://127.0.0.1:9/v1', 'KEY', 1234)
            probe.assert_not_called()
            popen.assert_not_called()

    def test_execution_deadline_starts_at_launch_after_preflight(self):
        clock = [100]
        closeout = isolation.RelayCloseout(request_window=1)

        def probe(*args, **kwargs):
            self.assertIsNone(closeout.deadline)
            clock[0] += 80
            return {'status': 'passed', 'cwd': str(self.work), 'interpreter': sys.executable}

        def launch(*args, **kwargs):
            self.assertEqual(closeout.deadline, 182)
            raise RuntimeError('launch checked')

        with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                patch.object(preflight, 'run', side_effect=probe), \
                patch.object(isolation.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(executor.subprocess, 'Popen', side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, 'launch checked'):
                executor.run(self.bridge, self.state, self.contract, 'message',
                             'http://127.0.0.1:9/v1', 'KEY', 1234, closeout=closeout)

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


class PatchFormatGuidanceTests(ExecutorPreflightOrderTests):
    """Pure prompt-routing checks for implementation patch-format guidance."""

    def _passed_evidence(self):
        return {'status': 'passed', 'cwd': str(self.work), 'interpreter': '/verified/python'}

    def _assert_single_patch_block(self, prompt):
        self.assertEqual(prompt.count('*** Begin Patch'), 1)
        self.assertEqual(prompt.count('*** End Patch'), 1)
        self.assertEqual(prompt.count('*** Add File: path'), 1)
        self.assertEqual(prompt.count('*** Update File: path'), 1)
        self.assertEqual(prompt.count('Patch format: apply_patch takes raw patch text'), 1)
        self.assertLess(prompt.index('*** Begin Patch'), prompt.index('*** End Patch'))
        self.assertIn('without a JSON wrapper or Markdown fence', prompt)
        self.assertIn('only for new paths', prompt)
        self.assertIn('matching context for existing files', prompt)
        self.assertIn('one corrected attempt', prompt)
        self.assertIn('already-available scoped file-edit capability', prompt)
        self.assertIn('concrete blocker', prompt)
        self.assertIn('existing allowed paths and sandbox permissions', prompt)
        self.assertIn('does not authorize a new tool', prompt)
        self.assertIn('silent removal of assertions', prompt)

    def test_initial_legacy_implementation_prompt_has_one_patch_format_block(self):
        prompt = self._capture_prompt(evidence=self._passed_evidence())
        self.assertIn('Task:\ntask', prompt)
        self._assert_single_patch_block(prompt)

    def test_resumed_legacy_implementation_prompt_has_one_patch_format_block(self):
        prompt = self._capture_prompt(thread_id='thread', evidence=self._passed_evidence())
        self.assertNotIn('Task:\ntask', prompt)
        self.assertTrue(prompt.endswith('Codex message:\nmessage'))
        self._assert_single_patch_block(prompt)

    def test_initial_explicit_implementation_prompt_has_one_patch_format_block(self):
        contract = {**self.contract, 'task_type': 'implementation'}
        prompt = self._capture_prompt(contract=contract, evidence=self._passed_evidence())
        self.assertIn('Task:\ntask', prompt)
        self._assert_single_patch_block(prompt)

    def test_resumed_explicit_implementation_prompt_has_one_patch_format_block(self):
        contract = {**self.contract, 'task_type': 'implementation'}
        prompt = self._capture_prompt(thread_id='thread', contract=contract,
                                      evidence=self._passed_evidence())
        self.assertNotIn('Task:\ntask', prompt)
        self._assert_single_patch_block(prompt)

    def test_offline_audit_work_prompt_excludes_patch_format_block(self):
        contract = {**self.contract, 'task_type': 'audit', 'audit': {'targets': [], 'scope_paths': []}}
        prompt = self._capture_prompt(contract=contract, evidence=self._passed_evidence())
        self.assertNotIn('*** Begin Patch', prompt)
        self.assertNotIn('*** End Patch', prompt)
        self.assertNotIn('Patch format: apply_patch', prompt)
        evidence = {'status': 'passed', 'cwd': str(self.root / 'audit_room'),
                    'interpreter': '/verified/python', 'checks': []}
        text = executor._work_guidance(evidence, contract, self.state,
                                       artifact_root=self.root / 'artifacts')
        self.assertIn('offline source', text)
        self.assertIn('Source access is file-scoped', text)
        self.assertIn('read-only', text)
        self.assertNotIn('*** Begin Patch', text)
        self.assertNotIn('Patch format: apply_patch', text)

    def test_online_audit_work_prompt_excludes_patch_format_block(self):
        contract = {**self.contract, 'task_type': 'audit',
                    'audit': {'targets': [{'origin': 'http://127.0.0.1:8000'}]}}
        prompt = self._capture_prompt(contract=contract, evidence=self._passed_evidence())
        self.assertNotIn('*** Begin Patch', prompt)
        self.assertNotIn('Patch format: apply_patch', prompt)
        evidence = {'status': 'passed', 'cwd': str(self.root / 'audit_room'),
                    'interpreter': '/verified/python', 'checks': []}
        text = executor._work_guidance(evidence, contract, self.state,
                                       artifact_root=self.root / 'artifacts')
        self.assertIn('authenticated local broker', text)
        self.assertIn('declared targets', text)
        self.assertIn('never direct network', text)
        self.assertNotIn('*** Begin Patch', text)
        self.assertNotIn('Patch format: apply_patch', text)

    def test_exam_prompt_with_implementation_contract_remains_exactly_message_only(self):
        contract = {**self.contract, 'task_type': 'implementation'}
        prompt = self._capture_prompt(thread_id='thread', phase='exam', contract=contract)
        self.assertEqual(prompt, 'Codex message:\nmessage')
        self.assertNotIn('*** Begin Patch', prompt)
        self.assertNotIn('Patch format: apply_patch', prompt)

    def test_patch_block_is_bounded_and_private_material_is_excluded(self):
        self.bridge.usage_snapshot = lambda *args: {
            'budget_state': {'remaining': {'max_requests': 8, 'max_total_tokens': 3000}},
            'work_budget_state': {'remaining': {'max_requests': 5, 'max_total_tokens': 2000}},
            'private': 'DO_NOT_FORWARD'}
        contract = {**self.contract, 'task_type': 'implementation',
                    'budget': {'max_requests': 10, 'exam_reserve_requests': 3}}
        evidence = {'status': 'passed', 'cwd': str(self.work), 'interpreter': '/verified/python',
                    'checks': [{'name': 'Python', 'output': 'RAW PROBE OUTPUT'}]}
        prompt = self._capture_prompt(thread_id='thread', contract=contract, evidence=evidence)
        self._assert_single_patch_block(prompt)
        self.assertNotIn('DO_NOT_FORWARD', prompt)
        self.assertNotIn('RAW PROBE OUTPUT', prompt)
        self.assertNotIn('KEY', prompt)
        self.assertIn('"max_requests":5', prompt)
        guidance = prompt.split('Verified work guidance', 1)[1].split('Codex message:', 1)[0]
        self.assertLessEqual(len(guidance), 2500)
        block = prompt[prompt.index('Patch format: apply_patch'):prompt.index('Codex message:')]
        self.assertLessEqual(len(block), 1200)


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


class ProjectEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tl-env-', dir='/tmp')
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / 'source'; self.work.mkdir()
        (self.work / 'deps.lock').write_text('dependency-v1')
        (self.work / 'bootstrap.py').write_text(
            'import pathlib,os\n'
            'p=pathlib.Path(os.environ["TELOS_CHECK_OUTPUT"])\n'
            'p.mkdir(exist_ok=True)\n'
            '(p/"installed.txt").write_text("installed")\n')
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.bridge = SimpleNamespace(CACHE=self.cache, HOME=Path.home(), INSTALL=Path(__file__).parent,
            GateError=providers.ProviderError, write_json=telos.write_json,
            read_json=telos.read_json, sandbox_profile=telos.sandbox_profile)
        self.env_patches = [patch.object(telos, 'CACHE', self.cache),
                           patch.object(telos, 'STATE', self.root / 'private'),
                           patch.object(telos, 'INSTALL', self.root / 'install')]
        for p in self.env_patches: p.start()

    def tearDown(self):
        for p in reversed(self.env_patches): p.stop()
        self.temp.cleanup()

    def module(self):
        self.assertTrue(hasattr(preflight, 'project'), 'Declared project environment preflight is missing')
        import project_environment
        return project_environment

    def requirements(self, **kw):
        return {'version': 1, 'tools': [{'id': 'python', 'path': sys.executable, 'kind': 'python'}],
                'lockfiles': ['deps.lock'], **kw}

    def run_probe(self, requirements=None, *, scratch=None, phase='work'):
        module = self.module()
        scratch = scratch or self.root / 'scratch'
        scratch.mkdir(exist_ok=True, mode=0o700)
        contract = {'environment_requirements': requirements or self.requirements(), 'dependency_paths': [],
                    'command_network': False}
        env, protected = module.build_environment(self.bridge, contract, self.work, scratch,
                                                   {'PATH': '/usr/bin:/bin'}, phase=phase)
        profile = telos.sandbox_profile(writable=[scratch], readable=[self.work],
                                         protected=[self.work, *protected],
                                         service_ports=module.service_ports(contract,phase),
                                         ipc_paths=[env['TMPDIR']] if contract['environment_requirements'].get('local_ipc') else [])
        return preflight.project(self.bridge, contract, self.work, scratch, env, profile, phase=phase), env

    def test_declared_tools_are_probed_inside_real_sandbox_and_cache_reused(self):
        one, env = self.run_probe()
        two, _ = self.run_probe()
        self.assertEqual(one['status'], 'passed')
        self.assertFalse(one['cache_hit'])
        self.assertTrue(two['cache_hit'])
        self.assertEqual(one['cache_key'], two['cache_key'])
        self.assertEqual(one['tools'][0]['path'], sys.executable)
        self.assertIn('abi', one['tools'][0])
        self.assertEqual(env['HOME'], str(self.root/'scratch/home'))
        self.assertEqual(env['TMPDIR'], str(self.root/'scratch/tmp'))

    def test_missing_runtime_is_structured_environment_blocker(self):
        module = self.module()
        with self.assertRaises(module.EnvironmentBlocked) as caught:
            self.run_probe(self.requirements(tools=[{'id':'missing','path':'/not-installed/python'}]))
        self.assertEqual(caught.exception.code, 'blocked_environment')
        self.assertEqual(caught.exception.evidence['status'], 'blocked_environment')
        self.assertIn('tool:missing', caught.exception.evidence['failed_check'])

    def test_lock_runtime_configuration_change_invalidates_cache(self):
        one, _ = self.run_probe()
        (self.work/'deps.lock').write_text('dependency-v2')
        two, _ = self.run_probe()
        self.assertNotEqual(one['cache_key'], two['cache_key'])
        three, _ = self.run_probe(self.requirements(variables={'CI':'true'}))
        self.assertNotEqual(two['cache_key'], three['cache_key'])
        four, _ = self.run_probe(self.requirements(tools=[{'id':'python','path':sys.executable,
                                                           'kind':'python','version_args':['-VV']}]))
        self.assertNotEqual(two['cache_key'], four['cache_key'])

    def test_read_only_sources_and_independent_writable_directories(self):
        _, a = self.run_probe(scratch=self.root/'a', phase='checks')
        _, z = self.run_probe(scratch=self.root/'z', phase='checks')
        self.assertNotEqual(a['HOME'], z['HOME'])
        self.assertNotEqual(a['TMPDIR'], z['TMPDIR'])
        self.assertNotEqual(a['TELOS_CHECK_OUTPUT'], z['TELOS_CHECK_OUTPUT'])
        Path(a['TELOS_CHECK_OUTPUT'],'data').write_text('first workspace')
        self.assertFalse(Path(z['TELOS_CHECK_OUTPUT'],'data').exists())

    def test_bootstrap_reused_only_with_intact_outputs_and_current_source(self):
        req = self.requirements(bootstrap=[{'id':'prepare','argv':['{python}','-I','{repo}/bootstrap.py'],
                                           'outputs':['output/installed.txt']}])
        one, env = self.run_probe(req)
        self.assertEqual(one['bootstrap'][0]['status'], 'executed')
        two, _ = self.run_probe(req)
        self.assertEqual(two['bootstrap'][0]['status'], 'reused')
        Path(env['TELOS_CHECK_OUTPUT'],'installed.txt').write_text('corrupt')
        three, _ = self.run_probe(req)
        self.assertEqual(three['bootstrap'][0]['status'], 'executed')
        (self.work/'new-source.py').write_text('VALUE=2')
        four, _ = self.run_probe(req)
        self.assertEqual(four['bootstrap'][0]['status'], 'executed')

    def test_failed_bootstrap_and_corrupt_fact_cache_are_never_reused_as_success(self):
        module = self.module()
        req = self.requirements(bootstrap=[{'id':'bad','argv':['{python}','-I','-c','raise SystemExit(9)'],
                                           'outputs':['output/missing']}])
        for _ in range(2):
            with self.assertRaises(module.EnvironmentBlocked): self.run_probe(req)
        one, _ = self.run_probe()
        cache = Path(one['cache_path'])
        cache.write_text('{broken')
        two, _ = self.run_probe()
        self.assertFalse(two['cache_hit'])
        self.assertEqual(two['status'], 'passed')

    def test_work_and_formal_checks_have_separate_verified_cache_entries(self):
        one, _ = self.run_probe()
        two, _ = self.run_probe(phase='checks')
        self.assertNotEqual(one['cache_key'], two['cache_key'])
        self.assertFalse(two['cache_hit'])

    def test_no_declared_capabilities_performs_no_extra_probe(self):
        self.module()
        with patch('project_environment.subprocess.Popen') as process:
            result = preflight.project(self.bridge, {}, self.work, self.root/'scratch',
                                       {'PATH':'/usr/bin:/bin'}, 'PROFILE')
        self.assertIsNone(result)
        process.assert_not_called()

    def test_concurrent_workspaces_share_facts_but_not_bootstrap_outputs(self):
        import concurrent.futures
        req = self.requirements(bootstrap=[{'id':'prepare','argv':['{python}','-I','{repo}/bootstrap.py'],
                                           'outputs':['output/installed.txt']}])
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda name:self.run_probe(req,scratch=self.root/name), ['one','two']))
        self.assertEqual(sorted(r[0]['cache_hit'] for r in results), [False,True])
        for evidence, env in results:
            self.assertEqual(evidence['bootstrap'][0]['status'], 'executed')
        self.assertNotEqual(results[0][1]['TELOS_CHECK_OUTPUT'],results[1][1]['TELOS_CHECK_OUTPUT'])


    def test_declared_local_ipc_uses_short_private_paths(self):
        evidence, env = self.run_probe(self.requirements(local_ipc=True))
        self.assertEqual(evidence['status'], 'passed')
        self.assertLess(len((env['TMPDIR']+'/s-12345678').encode()),104)

    def test_declared_local_service_is_checked_in_sandbox_without_broad_network(self):
        import socket
        server=socket.socket(); server.bind(('127.0.0.1',0)); server.listen(2)
        try:
            port=server.getsockname()[1]
            req=self.requirements(services=[{'id':'mail','host':'127.0.0.1','port':port}])
            self.assertEqual(self.run_probe(req)[0]['status'],'passed')
        finally:
            server.close()
        with self.assertRaises(self.module().EnvironmentBlocked):
            self.run_probe(req)

    def test_undeclared_dependency_read_is_not_added_to_sandbox(self):
        cache=self.root/'downloads'; cache.mkdir(); (cache/'package').write_text('cache')
        req=self.requirements(download_caches=[str(cache)])
        with self.assertRaises(self.module().EnvironmentBlocked) as caught:
            self.run_probe(req)
        self.assertEqual(caught.exception.evidence['failed_check'],'download_cache')

    def test_default_probe_does_not_execute_repository_bootstrap(self):
        (self.work/'bootstrap.py').write_text('raise RuntimeError("must not run")')
        evidence,_=self.run_probe()
        self.assertEqual(evidence['bootstrap'],[])


class EnvironmentAcceptanceTests(unittest.TestCase):
    def test_actual_clean_snapshot_checks_source_readonly_and_scratch_ipc(self):
        from test_telos import b, BridgeTests
        fixture=BridgeTests(); fixture.setUp()
        try:
            (fixture.repo/'.gitignore').write_text('leftover-build/\n')
            b.git(fixture.repo,'add','.gitignore')
            b.git(fixture.repo,'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','ignore build')
            sid=fixture.create(environment_requirements={'version':1,'local_ipc':True})
            state=b.read_json(b.run_dir(sid)/'state.json')
            (Path(state['worktree'])/'leftover-build').mkdir()
            (Path(state['worktree'])/'leftover-build/old').write_text('must not be required')
            _,tree=b.snapshot(state,'environment-clean')
            code = ('import os,pathlib,socket\n'
                    'assert not pathlib.Path("leftover-build").exists()\n'
                    'try: pathlib.Path("calc.py").write_text("bad")\n'
                    'except PermissionError: pass\n'
                    'else: raise AssertionError("source writable")\n'
                    '\nassert os.environ["HOME"]!=os.environ["TMPDIR"]\n'
                    'p=str(pathlib.Path(os.environ["TMPDIR"])/"test.sock"); s=socket.socket(socket.AF_UNIX); s.bind(p); s.close()\n'
                    'pathlib.Path(os.environ["TELOS_CHECK_OUTPUT"],"result").write_text("ok")')
            contract=b.read_json(b.run_dir(sid)/'contract.json')
            contract={**contract,'checks':[{'id':'clean','argv':['{python}','-c',code]}]}
            for _ in range(2):
                result=b.executable_checks(contract,tree)
                self.assertEqual(result[0]['status'],'passed',result)
                self.assertTrue(result[0]['source_read_only'])
                self.assertEqual(result[0]['environment']['phase'],'checks')
        finally: fixture.tearDown()


class EnvironmentExecutionOrderTests(ExecutorPreflightOrderTests):
    def test_project_environment_failure_prevents_executor_launch(self):
        import project_environment
        self.contract['environment_requirements']={'version':1}
        blocked=project_environment.EnvironmentBlocked('browser_launch','Declared browser cannot start')
        with patch.object(preflight,'git_executable',return_value=sys.executable), \
             patch.object(preflight,'run',return_value={'status':'passed','cwd':str(self.work),'interpreter':sys.executable}), \
             patch.object(preflight,'project',side_effect=blocked), \
             patch.object(executor.subprocess,'Popen') as process:
            with self.assertRaises(project_environment.EnvironmentBlocked):
                executor._run(self.bridge,self.state,self.contract,'message','http://127.0.0.1:9/v1','KEY',1234)
            process.assert_not_called()

    def test_base_preflight_failure_is_an_environment_error(self):
        import project_environment
        with patch.object(preflight,'git_executable',return_value=sys.executable), \
             patch.object(preflight,'run',side_effect=providers.ProviderError('Python unavailable')), \
             patch.object(executor.subprocess,'Popen') as process:
            with self.assertRaises(project_environment.EnvironmentBlocked):
                executor._run(self.bridge,self.state,self.contract,'message','http://127.0.0.1:9/v1','KEY',1234)
            process.assert_not_called()

class EnvironmentLaunchBoundaryTests(ExecutorPreflightOrderTests):
    def test_pause_or_cancel_during_preflight_prevents_cli_request(self):
        from test_telos import BridgeTests, b
        from unittest.mock import Mock
        fixture = BridgeTests(); fixture.setUp()
        try:
            for action in ('pause', 'cancel'):
                sid = fixture.create(_workflow_version=3)
                with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                    b.start_job(sid, 'send', 'Authorized work')
                state, contract = b.load_session(b.run_dir(sid))
                self.bridge.STATE = b.STATE
                self.bridge.locked = b.locked
                self.bridge.load_session = b.load_session
                self.bridge.run_dir = b.run_dir
                def stop(*args, **kwargs):
                    b.return_control(sid, action)
                    return {'status':'passed','cwd':state['worktree'],'interpreter':sys.executable}
                with patch.object(preflight, 'git_executable', return_value=sys.executable),                      patch.object(preflight, 'run', side_effect=stop),                      patch.object(executor.subprocess, 'Popen') as process:
                    with self.assertRaisesRegex(providers.ProviderError, 'paused|cancelled'):
                        executor._run(self.bridge, state, self.contract, 'message',
                                      'http://127.0.0.1:9/v1', 'KEY', 1234)
                    process.assert_not_called()
        finally:
            fixture.tearDown()

    def test_redirected_frozen_dependency_blocks_before_cli(self):
        from unittest.mock import Mock
        self.contract['dependency_paths'] = ['/declared/cache']
        self.bridge.validate_dependencies = Mock(return_value=['/redirected/private'])
        with patch.object(executor.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(providers.ProviderError, 'redirected'):
                executor._run(self.bridge,self.state,self.contract,'message',
                              'http://127.0.0.1:9/v1','KEY',1234)
            process.assert_not_called()


class EnvironmentIntegrityBoundaryTests(ProjectEnvironmentTests):
    def test_output_symlink_cannot_make_external_writable_artifact_reusable(self):
        module = self.module()
        outside = self.root/'external'; outside.mkdir()
        (outside/'artifact').write_text('external')
        (self.work/'bootstrap.py').write_text(
            'import os,pathlib\np=pathlib.Path(os.environ["TELOS_CHECK_OUTPUT"])\n'
            'p.mkdir(exist_ok=True)\n(p/"link").symlink_to(' + repr(str(outside/'artifact')) + ')\n')
        req = self.requirements(bootstrap=[{'id':'prepare','argv':['{python}','-I','{repo}/bootstrap.py'],
                                            'outputs':['output']}])
        with self.assertRaisesRegex(module.EnvironmentBlocked, 'outside'):
            self.run_probe(req)

    def test_shared_fact_expiry_and_probe_version_invalidate_only_facts(self):
        module = self.module()
        one,_ = self.run_probe()
        cache = Path(one['cache_path'])
        record = telos.read_json(cache); record.pop('proof'); record['at'] -= 301
        module._save_cache(self.bridge, cache, record)
        self.assertFalse(self.run_probe()[0]['cache_hit'])
        with patch.object(module, 'VERSION', 'future-probe'):
            changed,_ = self.run_probe()
        self.assertNotEqual(changed['cache_key'], one['cache_key'])

    def test_work_only_browser_is_not_probed_during_formal_checks(self):
        req = self.requirements(browser={'node':'/unavailable/node','module':'/unavailable/module',
                                         'executable':'/unavailable/browser','phases':['work']})
        self.assertEqual(self.run_probe(req,phase='checks')[0]['status'], 'passed')
        with self.assertRaises(self.module().EnvironmentBlocked):
            self.run_probe(req)

    def test_interrupted_bootstrap_marker_cannot_be_reused(self):
        module = self.module()
        req = self.requirements(bootstrap=[{'id':'prepare','argv':['{python}','-I','{repo}/bootstrap.py'],
                                            'outputs':['output/installed.txt']}])
        self.run_probe(req)
        marker = next((self.cache/'project-environments'/'prepared').glob('*.json'))
        record = telos.read_json(marker); record.pop('proof'); record['status']='preparing'
        module._save_cache(self.bridge, marker, record)
        evidence,_ = self.run_probe(req)
        self.assertEqual(evidence['bootstrap'][0]['status'], 'executed')

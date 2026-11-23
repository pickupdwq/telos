"""Fast (priority) serving tier is requested only for this Mac's Codex login on gpt-6.2-sol."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

import executor
import preflight
import project_environment
import providers

FAST = 'service_tier="priority"'


def binding(provider='codex_login', model='gpt-6.2-sol'):
    return {'provider': provider, 'model': model, 'reasoning_effort': 'high',
            'wire_api': 'responses', 'exam_wire_api': 'responses', 'exam_max_tokens': 100,
            'base_url': 'https://chatgpt.com/backend-api/codex', 'http_timeout': 1, 'job_timeout': 2}


class ServiceTierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'worktree'
        self.work.mkdir()
        self.state = {'session_id': 'a' * 32, 'worktree': str(self.work), 'thread_id': None,
                      'job_id': 'job', 'base_commit': 'a' * 40}
        self.bridge = SimpleNamespace(
            HOME=self.root / 'home', CACHE=self.root / 'cache', INSTALL=self.root / 'install',
            GateError=providers.ProviderError,
            backend_bindings=lambda contract: {'executor': contract['executor']},
            execution_binding=lambda state, contract: contract['executor'],
            minimal_env=lambda: {'PATH': '/usr/bin:/bin'},
            git=lambda repo, *args: '/tmp/common.git',
            write_json=lambda *args, **kwargs: None,
            read_json=lambda path: {},
            encoded=json.dumps,
            sandbox_profile=lambda **kwargs: 'PROFILE')
        self.cli = self.bridge.HOME / '.local/share/telos-cli/node_modules/.bin/codex'
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text('#!/bin/sh\nexit 99\n')
        self.cli.chmod(0o700)
        lookup = patch.object(executor.shutil, 'which', return_value=None)
        self.addCleanup(lookup.stop)
        lookup.start()

    def tearDown(self):
        self.temp.cleanup()

    def _argv(self, provider, model):
        contract = {'task': 'task', 'context': 'context', 'executor': binding(provider, model)}

        class CapturingProcess:
            returncode = 1

            def communicate(self, payload, timeout=None):
                return b'', b''

        evidence = {'status': 'passed', 'cwd': str(self.work), 'interpreter': '/verified/python',
                    'checks': [{'name': 'Python', 'output': 'ok'}]}
        with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                patch.object(preflight, 'run', return_value=evidence), \
                patch.object(executor.subprocess, 'Popen', return_value=CapturingProcess()) as popen:
            with self.assertRaises(providers.ProviderError):
                executor._run(self.bridge, self.state, contract, 'message',
                              'http://127.0.0.1:9/v1', 'KEY', 1234, phase='work')
        return popen.call_args.args[0]

    def test_fast_tier_is_requested_for_the_local_codex_login_on_gpt_62_sol(self):
        self.assertIn(FAST, self._argv('codex_login', 'gpt-6.2-sol'))

    def test_other_login_models_keep_the_default_tier(self):
        self.assertNotIn(FAST, self._argv('codex_login', 'gpt-6.1-sol'))
        self.assertNotIn(FAST, self._argv('codex_login', 'gpt-6-astra'))

    def test_other_providers_keep_the_default_tier(self):
        self.assertNotIn(FAST, self._argv('local', 'gpt-6.2-sol'))
        self.assertNotIn(FAST, self._argv('ark', 'gpt-6.2-sol'))
        self.assertNotIn(FAST, self._argv('bailian', 'gpt-6.2-sol'))

    def test_old_or_unidentified_cli_blocks_before_work_resume_or_exam(self):
        contract = {'task': 'task', 'context': '', 'executor': binding()}
        for version in ('codex-cli 0.169.0', 'codex-cli 0.169.1-alpha.1', 'unrecognized'):
            for phase, thread in [('work', None), ('work', 'existing-thread'), ('exam', 'existing-thread')]:
                with self.subTest(version=version, phase=phase, thread=thread):
                    result = SimpleNamespace(returncode=0, stdout=version.encode(), stderr=b'')
                    with patch.object(preflight, 'git_executable', return_value=sys.executable), \
                            patch.object(preflight.subprocess, 'run', return_value=result) as probe, \
                            patch.object(executor.subprocess, 'Popen') as launch:
                        with self.assertRaises(project_environment.EnvironmentBlocked) as caught:
                            executor.run(self.bridge, {**self.state, 'thread_id': thread}, contract, 'message',
                                         'http://127.0.0.1:9/v1', 'KEY', 1234, phase=phase)
                    self.assertIn('npm install --prefix "$HOME/.local/share/telos-cli" @openai/codex@0.171.0',
                                  str(caught.exception))
                    self.assertIn(version, str(caught.exception))
                    self.assertEqual(caught.exception.evidence['phase'], phase)
                    self.assertEqual(probe.call_args.args[0][-2:], [str(self.cli), '--version'])
                    probe.assert_called_once()
                    launch.assert_not_called()

    def test_first_fast_capable_cli_is_allowed(self):
        for version in ('0.169.1', '0.170.0', '0.171.0'):
            def probe(command, **kwargs):
                self.assertEqual(command[-2:], [str(self.cli), '--version'])
                return SimpleNamespace(returncode=0, stdout=f'codex-cli {version}'.encode(), stderr=b'')
            with self.subTest(version=version):
                evidence = preflight.run(self.bridge, self.cli, self.work, {}, 'PROFILE',
                                         cli_only=True, require_fast=True, runner=probe)
                self.assertEqual(len(evidence['checks']), 1)

    def test_tier_is_frozen_into_every_launch_once_only(self):
        argv = self._argv('codex_login', 'gpt-6.2-sol')
        self.assertEqual(argv.count(FAST), 1)


if __name__ == '__main__':
    unittest.main()

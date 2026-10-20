"""Pure regressions for verify_all selection, reporting and import safety."""
import contextlib
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
import uuid

import verify_all


EXPECTED_MODULES = [
    'test_telos',
    'test_callbacks',
    'test_repairs',
    'test_projects',
    'test_experience',
    'test_rsi',
    'test_providers',
    'test_closed_book',
    'test_maintenance',
    'test_hardening',
    'test_identity',
    'test_audit',
    'test_local_executor',
    'test_usage',
    'test_preflight',
    'test_compact',
    'test_mcp_contract',
    'test_comparison',
    'test_watch',
    'test_verify_all',
]


class _FixtureModules:
    def __init__(self, sources):
        self.sources = sources
        self.path = None
        self.names = []
        self._temporary = None

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix='verify-all-tests-')
        self.path = Path(self._temporary.name)
        for name, source in self.sources.items():
            (self.path / f'{name}.py').write_text(
                textwrap.dedent(source), encoding='utf-8')
        sys.path.insert(0, str(self.path))
        self.names = list(self.sources)
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            sys.path.remove(str(self.path))
        except ValueError:
            pass
        for name in self.names:
            sys.modules.pop(name, None)
        self._temporary.cleanup()


def _prefix():
    return '_verify_all_fixture_' + uuid.uuid4().hex[:12]


def _run_main(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = verify_all.main(argv)
    return status, stdout.getvalue(), stderr.getvalue()


class VerifyAllTests(unittest.TestCase):
    def test_modules_keep_existing_order_and_add_self_last(self):
        self.assertEqual(verify_all.MODULES, EXPECTED_MODULES)

    def test_import_is_side_effect_free(self):
        source = Path(verify_all.__file__).read_text(encoding='utf-8')
        namespace = {'__name__': 'verify_all_import_probe',
                     '__file__': verify_all.__file__}
        with mock.patch('importlib.import_module',
                        side_effect=AssertionError('module imported at import time')), \
                mock.patch.object(unittest.TextTestRunner, 'run',
                                  side_effect=AssertionError('tests ran at import time')):
            exec(compile(source, verify_all.__file__, 'exec'), namespace)
        self.assertEqual(namespace['MODULES'], EXPECTED_MODULES)

    def test_collect_suite_selects_locally_declared_tests_once(self):
        prefix = _prefix()
        base_name = f'{prefix}_base'
        child_name = f'{prefix}_child'
        sources = {
            base_name: '''
                from unittest import TestCase

                class ImportedBase(TestCase):
                    def test_imported_base(self):
                        raise AssertionError('imported test must not run')
            ''',
            child_name: f'''
                from unittest import TestCase
                from {base_name} import ImportedBase

                class LocalCase(TestCase):
                    def test_first(self):
                        pass

                    def test_second(self):
                        pass

                class InheritingCase(ImportedBase):
                    def test_child(self):
                        pass

                AliasedCase = LocalCase
                imported_alias = ImportedBase
            ''',
        }
        with _FixtureModules(sources):
            suite = verify_all.collect_suite([child_name, child_name, base_name])
            test_ids = [test.id() for test in verify_all._iter_test_cases(suite)]
            self.assertEqual(suite.countTestCases(), 4)
            self.assertEqual(test_ids, [
                f'{child_name}.LocalCase.test_first',
                f'{child_name}.LocalCase.test_second',
                f'{child_name}.InheritingCase.test_child',
                f'{base_name}.ImportedBase.test_imported_base',
            ])
            reversed_ids = [
                test.id() for test in verify_all._iter_test_cases(
                    verify_all.collect_suite([base_name, child_name]))
            ]
            self.assertEqual(reversed_ids[0],
                             f'{base_name}.ImportedBase.test_imported_base')

    def test_list_is_clean_json_and_runs_no_test_bodies(self):
        prefix = _prefix()
        marker = Path(tempfile.gettempdir()) / f'{prefix}-listed.txt'
        report = Path(tempfile.gettempdir()) / f'{prefix}-list-report.json'
        first_name = f'{prefix}_first'
        second_name = f'{prefix}_second'
        sources = {
            first_name: f'''
                from pathlib import Path
                from unittest import TestCase

                print('import noise that must not reach stdout')
                MARKER = Path({str(marker)!r})

                class FirstCase(TestCase):
                    def test_alpha(self):
                        MARKER.write_text('alpha ran', encoding='utf-8')
            ''',
            second_name: f'''
                from unittest import TestCase

                class SecondCase(TestCase):
                    def test_beta(self):
                        pass
            ''',
        }
        try:
            with _FixtureModules(sources), \
                    mock.patch.object(verify_all, 'MODULES', (first_name, second_name)):
                status, stdout, stderr = _run_main([
                    str(report), '--list', '--module', second_name,
                    '--module', first_name, '--module', second_name,
                ])
                self.assertEqual(status, 0, stderr)
                self.assertFalse(report.exists())
                self.assertFalse(marker.exists())
                self.assertEqual(stdout, json.dumps({
                    'modules': [second_name, first_name],
                    'test_ids': [
                        f'{second_name}.SecondCase.test_beta',
                        f'{first_name}.FirstCase.test_alpha',
                    ],
                    'count': 2,
                }) + '\n')
                payload = json.loads(stdout)
                self.assertEqual(set(payload), {'modules', 'test_ids', 'count'})
                self.assertEqual(payload['count'], len(payload['test_ids']))

                status, stdout, stderr = _run_main(['--list'])
                self.assertEqual(status, 0, stderr)
                payload = json.loads(stdout)
                self.assertEqual(payload['modules'], [first_name, second_name])
                self.assertEqual(payload['count'], 2)
                self.assertEqual(stdout,
                                 json.dumps(payload) + '\n')
                self.assertFalse(marker.exists())
        finally:
            marker.unlink(missing_ok=True)
            report.unlink(missing_ok=True)

    def test_unknown_module_is_rejected_before_import_or_report_write(self):
        prefix = _prefix()
        module_name = f'{prefix}_known'
        import_marker = Path(tempfile.gettempdir()) / f'{prefix}-imported.txt'
        report = Path(tempfile.gettempdir()) / f'{prefix}-unknown-report.json'
        sentinel = '{"keep": true}\n'
        report.write_text(sentinel, encoding='utf-8')
        sources = {
            module_name: f'''
                from pathlib import Path
                from unittest import TestCase

                Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')

                class KnownCase(TestCase):
                    def test_body_must_not_run(self):
                        raise AssertionError('test body must not run')
            ''',
        }
        try:
            with _FixtureModules(sources), \
                    mock.patch.object(verify_all, 'MODULES', (module_name,)):
                status, stdout, stderr = _run_main([
                    str(report), '--module', module_name, '--module', 'not_a_module',
                ])
                self.assertNotEqual(status, 0)
                self.assertEqual(stdout, '')
                self.assertIn('not_a_module', stderr)
                self.assertFalse(import_marker.exists())
                self.assertEqual(report.read_text(encoding='utf-8'), sentinel)

                status, stdout, _ = _run_main(['--list', '--module', 'not_a_module'])
                self.assertNotEqual(status, 0)
                self.assertEqual(stdout, '')
        finally:
            import_marker.unlink(missing_ok=True)
            report.unlink(missing_ok=True)

    def test_report_records_outcomes_subtests_and_timing(self):
        prefix = _prefix()
        module_name = f'{prefix}_outcomes'
        report_path = Path(tempfile.gettempdir()) / f'{prefix}-outcomes.json'
        sources = {
            module_name: '''
                from unittest import TestCase, expectedFailure, skip

                class OutcomeCase(TestCase):
                    def test_pass(self):
                        pass

                    def test_fail(self):
                        self.assertEqual(1, 2)

                    def test_error(self):
                        raise RuntimeError('boom')

                    @skip('intentional')
                    def test_skip(self):
                        pass

                    @expectedFailure
                    def test_expected_failure(self):
                        self.fail('expected')

                    @expectedFailure
                    def test_unexpected_success(self):
                        pass

                    def test_subtest_failure(self):
                        with self.subTest(value=1):
                            self.assertEqual(1, 1)
                        with self.subTest(value=2):
                            self.assertEqual(2, 1)
            ''',
        }
        try:
            with _FixtureModules(sources), \
                    mock.patch.object(verify_all, 'MODULES', (module_name,)):
                status, _, _ = _run_main([
                    str(report_path), '--module', module_name,
                ])
                self.assertEqual(status, 1)
                report = json.loads(report_path.read_text(encoding='utf-8'))
        finally:
            report_path.unlink(missing_ok=True)

        self.assertEqual(report['selection'],
                         {'mode': 'selected', 'modules': [module_name]})
        self.assertEqual(report['tests'], 7)
        self.assertFalse(report['passed'])
        self.assertEqual(report['failures'], 2)
        self.assertEqual(report['errors'], 1)
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(report['real_git_and_sandbox'], True)
        self.assertIn('model_boundary', report)
        self.assertEqual(len(report['test_results']), 7)
        self.assertTrue(math.isfinite(report['seconds']))
        self.assertGreaterEqual(report['seconds'], 0)
        outcomes = {}
        for row in report['test_results']:
            self.assertEqual(set(row), {'id', 'outcome', 'seconds'})
            self.assertTrue(row['id'].startswith(f'{module_name}.OutcomeCase.'))
            self.assertTrue(math.isfinite(row['seconds']))
            self.assertGreaterEqual(row['seconds'], 0)
            outcomes[row['id'].rsplit('.', 1)[-1]] = row['outcome']
        self.assertEqual(outcomes, {
            'test_pass': 'passed',
            'test_fail': 'failed',
            'test_error': 'error',
            'test_skip': 'skipped',
            'test_expected_failure': 'expected_failure',
            'test_unexpected_success': 'unexpected_success',
            'test_subtest_failure': 'failed',
        })
        self.assertLessEqual(
            sum(row['seconds'] for row in report['test_results']),
            report['seconds'] + 1e-6)

    def test_unexpected_success_is_reported_and_unsuccessful(self):
        prefix = _prefix()
        module_name = f'{prefix}_unexpected'
        report_path = Path(tempfile.gettempdir()) / f'{prefix}-unexpected.json'
        sources = {
            module_name: '''
                from unittest import TestCase, expectedFailure

                class UnexpectedCase(TestCase):
                    @expectedFailure
                    def test_unexpected_success(self):
                        pass
            ''',
        }
        try:
            with _FixtureModules(sources), \
                    mock.patch.object(verify_all, 'MODULES', (module_name,)):
                status, _, stderr = _run_main([
                    str(report_path), '--module', module_name,
                ])
                self.assertEqual(status, 1, stderr)
                report = json.loads(report_path.read_text(encoding='utf-8'))
        finally:
            report_path.unlink(missing_ok=True)

        self.assertEqual(report['tests'], 1)
        self.assertFalse(report['passed'])
        self.assertEqual((report['failures'], report['errors'], report['skipped']),
                         (0, 0, 0))
        self.assertEqual([row['outcome'] for row in report['test_results']],
                         ['unexpected_success'])

    def test_default_full_report_and_backward_compatible_argv(self):
        prefix = _prefix()
        first_name = f'{prefix}_passing_first'
        second_name = f'{prefix}_passing_second'
        report_path = Path(tempfile.gettempdir()) / f'{prefix}-full.json'
        sources = {
            first_name: '''
                from unittest import TestCase

                class FirstCase(TestCase):
                    def test_one(self):
                        pass

                    def test_two(self):
                        pass
            ''',
            second_name: '''
                from unittest import TestCase

                class SecondCase(TestCase):
                    def test_three(self):
                        pass
            ''',
        }
        try:
            with _FixtureModules(sources), \
                    mock.patch.object(verify_all, 'MODULES', (first_name, second_name)), \
                    mock.patch.object(sys, 'argv', ['verify_all', str(report_path)]):
                status, _, stderr = _run_main(None)
                self.assertEqual(status, 0, stderr)
                report = json.loads(report_path.read_text(encoding='utf-8'))
                self.assertEqual(report['selection'],
                                 {'mode': 'full', 'modules': [first_name, second_name]})
                self.assertEqual(report['tests'], 3)
                self.assertTrue(report['passed'])
                self.assertEqual((report['failures'], report['errors'], report['skipped']),
                                 (0, 0, 0))
                self.assertEqual([row['outcome'] for row in report['test_results']],
                                 ['passed', 'passed', 'passed'])
                self.assertTrue(math.isfinite(report['seconds']))
                self.assertGreaterEqual(report['seconds'], 0)
                self.assertEqual(
                    [test.id() for test in verify_all._iter_test_cases(
                        verify_all.collect_suite())],
                    [f'{first_name}.FirstCase.test_one',
                     f'{first_name}.FirstCase.test_two',
                     f'{second_name}.SecondCase.test_three'])

                status, _, stderr = _run_main([])
                self.assertEqual(status, 0, stderr)
        finally:
            report_path.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)

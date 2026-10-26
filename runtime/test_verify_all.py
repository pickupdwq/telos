"""Pure regressions for verify_all selection, reporting and import safety."""
import contextlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
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
    'test_timing',
    'test_preflight',
    'test_compact',
    'test_mcp_contract',
    'test_integrity',
    'test_clarification',
    'test_meetings',
    'test_comparison',
    'test_watch',
    'test_reasoning',
    'test_effort_policy',
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


def _suite_ids(suite):
    return [test.id() for test in verify_all._iter_test_cases(suite)]


class _CopiedRunner:
    """Write a disposable runner whose allowlist is just the fixtures."""

    def __init__(self, sources, corrupt_child_reports=False):
        self.sources = sources
        self.corrupt_child_reports = corrupt_child_reports
        self._temporary = None
        self.path = None

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(
            prefix='verify-all-copied-runner-')
        self.path = Path(self._temporary.name)
        for name, source in self.sources.items():
            (self.path / f'{name}.py').write_text(
                textwrap.dedent(source), encoding='utf-8')

        source = Path(verify_all.__file__).read_text(encoding='utf-8')
        marker = "\nif __name__ == '__main__':"
        modules = f'\nMODULES = {list(self.sources)!r}\n'
        self.assert_replaced = source.count(marker) == 1
        source = source.replace(marker, modules + marker, 1)
        if self.corrupt_child_reports:
            needle = '    if args.report is not None:\n'
            injection = (
                "    if ('--jobs' not in sys.argv and\n"
                "            __import__('os').environ.get("
                "'VERIFY_ALL_CORRUPT_CHILD')):\n"
                "        report['test_results'].append({\n"
                "            'id': '__foreign__.Foreign.test_foreign',\n"
                "            'outcome': 'passed',\n"
                "            'seconds': 0.0,\n"
                "        })\n"
                "        report['tests'] += 1\n"
            )
            self.assert_replaced = (
                self.assert_replaced and source.count(needle) == 1)
            source = source.replace(needle, injection + needle, 1)
        (self.path / 'verify_all.py').write_text(source, encoding='utf-8')
        return self.path

    def __exit__(self, exc_type, exc, traceback):
        self._temporary.cleanup()


def _run_copied_runner(root, argv, env=None, timeout=30):
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, '-B', str(root / 'verify_all.py'), *argv],
        cwd=root, env=child_env, text=True, capture_output=True,
        timeout=timeout)


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


class TestSelectionTests(unittest.TestCase):
    def test_collect_suite_matches_exact_classes_methods_and_overlap_order(self):
        prefix = _prefix()
        module_name = f'{prefix}_selection'
        sources = {
            module_name: '''
                from unittest import TestCase

                class Case(TestCase):
                    def test_two(self):
                        pass

                    def test_one(self):
                        pass

                class CaseExtra(TestCase):
                    def test_extra(self):
                        pass
            ''',
        }
        with _FixtureModules(sources):
            self.assertEqual(_suite_ids(verify_all.collect_suite(
                [module_name], [f'{module_name}.Case'])), [
                    f'{module_name}.Case.test_two',
                    f'{module_name}.Case.test_one',
                ])
            self.assertEqual(_suite_ids(verify_all.collect_suite(
                [module_name], [f'{module_name}.Case.test_one'])), [
                    f'{module_name}.Case.test_one',
                ])
            self.assertEqual(_suite_ids(verify_all.collect_suite(
                [module_name], [
                    f'{module_name}.Case.test_one',
                    f'{module_name}.Case',
                    f'{module_name}.Case.test_one',
                    f'{module_name}.CaseExtra',
                ])), [
                    f'{module_name}.Case.test_two',
                    f'{module_name}.Case.test_one',
                    f'{module_name}.CaseExtra.test_extra',
                ])

            with self.assertRaises(ValueError) as raised:
                verify_all.collect_suite(
                    [module_name],
                    [f'{module_name}.Missing',
                     f'{module_name}.Case.test_missing'])
            message = str(raised.exception)
            self.assertIn(f'{module_name}.Missing', message)
            self.assertIn(f'{module_name}.Case.test_missing', message)

    def test_selected_cli_run_is_scoped_ordered_and_reported(self):
        prefix = _prefix()
        first_name = f'{prefix}_first'
        second_name = f'{prefix}_second'
        with tempfile.TemporaryDirectory(prefix='verify-all-selected-') as directory:
            root = Path(directory)
            report_path = root / 'report.json'
            first_marker = root / 'first-ran'
            extra_marker = root / 'extra-ran'
            second_marker = root / 'second-ran'
            sources = {
                first_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    marker = Path({str(first_marker)!r})
                    extra_marker = Path({str(extra_marker)!r})

                    class FirstCase(TestCase):
                        def test_alpha(self):
                            marker.write_text('alpha', encoding='utf-8')

                        def test_beta(self):
                            marker.write_text('beta', encoding='utf-8')

                    class FirstExtra(TestCase):
                        def test_extra(self):
                            extra_marker.write_text('extra', encoding='utf-8')
                ''',
                second_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    marker = Path({str(second_marker)!r})

                    class SecondCase(TestCase):
                        def test_gamma(self):
                            marker.write_text('gamma', encoding='utf-8')
                ''',
            }
            selectors = [
                f'{second_name}.SecondCase',
                f'{first_name}.FirstCase.test_alpha',
                f'{first_name}.FirstCase',
                f'{first_name}.FirstCase.test_alpha',
            ]
            expected_ids = [
                f'{second_name}.SecondCase.test_gamma',
                f'{first_name}.FirstCase.test_alpha',
                f'{first_name}.FirstCase.test_beta',
            ]
            with _FixtureModules(sources), \
                    mock.patch.object(
                        verify_all, 'MODULES', (first_name, second_name)):
                argv = [str(report_path)]
                for selector in selectors:
                    argv.extend(['--test', selector])
                status, _, stderr = _run_main(argv)
                self.assertEqual(status, 0, stderr)
                report = json.loads(report_path.read_text(encoding='utf-8'))
                self.assertEqual(report['selection'], {
                    'mode': 'selected',
                    'modules': [second_name, first_name],
                    'selectors': [
                        f'{second_name}.SecondCase',
                        f'{first_name}.FirstCase.test_alpha',
                        f'{first_name}.FirstCase',
                    ],
                    'test_ids': expected_ids,
                })
                self.assertEqual(report['tests'], 3)
                self.assertTrue(report['passed'])
                self.assertEqual(
                    [row['id'] for row in report['test_results']],
                    expected_ids)
                self.assertTrue(first_marker.exists())
                self.assertTrue(second_marker.exists())
                self.assertFalse(extra_marker.exists())

                first_marker.unlink()
                second_marker.unlink()
                report_path.unlink()
                status, stdout, stderr = _run_main(
                    ['--list', '--test', f'{second_name}.SecondCase',
                     '--test', f'{first_name}.FirstCase.test_beta',
                     '--test', f'{second_name}.SecondCase'])
                self.assertEqual(status, 0, stderr)
                self.assertFalse(report_path.exists())
                self.assertFalse(first_marker.exists())
                self.assertFalse(second_marker.exists())
                self.assertFalse(extra_marker.exists())
                payload = json.loads(stdout)
                self.assertEqual(payload, {
                    'modules': [second_name, first_name],
                    'test_ids': [
                        f'{second_name}.SecondCase.test_gamma',
                        f'{first_name}.FirstCase.test_beta',
                    ],
                    'count': 2,
                    'selectors': [
                        f'{second_name}.SecondCase',
                        f'{first_name}.FirstCase.test_beta',
                    ],
                })
                self.assertEqual(
                    set(payload),
                    {'modules', 'test_ids', 'count', 'selectors'})

    def test_malformed_and_unknown_module_selectors_fail_before_import(self):
        prefix = _prefix()
        module_name = f'{prefix}_known'
        with tempfile.TemporaryDirectory(prefix='verify-all-selector-') as directory:
            root = Path(directory)
            report_path = root / 'report.json'
            import_marker = root / 'imported'
            test_marker = root / 'ran'
            sentinel = '{"keep": true}\n'
            report_path.write_text(sentinel, encoding='utf-8')
            sources = {
                module_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    Path({str(import_marker)!r}).write_text(
                        'imported', encoding='utf-8')

                    class KnownCase(TestCase):
                        def test_one(self):
                            Path({str(test_marker)!r}).write_text(
                                'ran', encoding='utf-8')
                ''',
            }
            with _FixtureModules(sources), \
                    mock.patch.object(verify_all, 'MODULES', (module_name,)):
                status, stdout, stderr = _run_main([
                    str(report_path), '--test', f'{module_name}.KnownCase',
                    '--test', 'not a selector',
                ])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, '')
                self.assertIn('not a selector', stderr)
                self.assertFalse(import_marker.exists())
                self.assertFalse(test_marker.exists())
                self.assertEqual(
                    report_path.read_text(encoding='utf-8'), sentinel)

                for selector in (
                        'one_component',
                        f'{module_name}.KnownCase.test_one.extra',
                        f'{module_name}/KnownCase',
                        f'{module_name}.KnownCase.*',
                        ''):
                    status, stdout, stderr = _run_main(['--test', selector])
                    self.assertEqual(status, 2, selector)
                    self.assertEqual(stdout, '')
                    self.assertIn('malformed test selector', stderr)
                    self.assertFalse(import_marker.exists())
                    self.assertEqual(
                        report_path.read_text(encoding='utf-8'), sentinel)

                status, stdout, stderr = _run_main([
                    str(report_path), '--test', 'not_a_module.SomeCase',
                ])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, '')
                self.assertIn('not_a_module', stderr)
                self.assertFalse(import_marker.exists())
                self.assertEqual(
                    report_path.read_text(encoding='utf-8'), sentinel)

    def test_out_of_scope_selector_fails_before_import(self):
        prefix = _prefix()
        first_name = f'{prefix}_first'
        second_name = f'{prefix}_second'
        with tempfile.TemporaryDirectory(prefix='verify-all-scope-') as directory:
            root = Path(directory)
            report_path = root / 'report.json'
            first_import = root / 'first-imported'
            second_import = root / 'second-imported'
            sentinel = '{"keep": true}\n'
            report_path.write_text(sentinel, encoding='utf-8')
            sources = {
                first_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    Path({str(first_import)!r}).write_text(
                        'imported', encoding='utf-8')

                    class FirstCase(TestCase):
                        def test_one(self):
                            raise AssertionError('must not run')
                ''',
                second_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    Path({str(second_import)!r}).write_text(
                        'imported', encoding='utf-8')

                    class SecondCase(TestCase):
                        def test_two(self):
                            pass
                ''',
            }
            with _FixtureModules(sources), \
                    mock.patch.object(
                        verify_all, 'MODULES', (first_name, second_name)):
                status, stdout, stderr = _run_main([
                    str(report_path), '--module', second_name,
                    '--test', f'{first_name}.FirstCase',
                ])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, '')
                self.assertIn(f'{first_name}.FirstCase', stderr)
                self.assertIn('outside the selected module scope', stderr)
                self.assertFalse(first_import.exists())
                self.assertFalse(second_import.exists())
                self.assertEqual(
                    report_path.read_text(encoding='utf-8'), sentinel)

    def test_unknown_class_or_method_fails_before_running_or_reporting(self):
        prefix = _prefix()
        module_name = f'{prefix}_unknown_test'
        with tempfile.TemporaryDirectory(prefix='verify-all-unknown-') as directory:
            root = Path(directory)
            report_path = root / 'report.json'
            test_marker = root / 'ran'
            sentinel = '{"keep": true}\n'
            report_path.write_text(sentinel, encoding='utf-8')
            sources = {
                module_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    class KnownCase(TestCase):
                        def test_one(self):
                            Path({str(test_marker)!r}).write_text(
                                'ran', encoding='utf-8')
                ''',
            }
            with _FixtureModules(sources), \
                    mock.patch.object(verify_all, 'MODULES', (module_name,)):
                status, stdout, stderr = _run_main([
                    str(report_path),
                    '--test', f'{module_name}.KnownCase',
                    '--test', f'{module_name}.MissingCase',
                    '--test', f'{module_name}.KnownCase.test_missing',
                ])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, '')
                self.assertIn(f'{module_name}.MissingCase', stderr)
                self.assertIn(
                    f'{module_name}.KnownCase.test_missing', stderr)
                self.assertFalse(test_marker.exists())
                self.assertEqual(
                    report_path.read_text(encoding='utf-8'), sentinel)

                status, stdout, stderr = _run_main([
                    '--list', '--test', f'{module_name}.KnownCase',
                    '--test', f'{module_name}.MissingCase',
                ])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, '')
                self.assertFalse(test_marker.exists())

    def test_unselected_collection_list_and_report_remain_backward_compatible(self):
        prefix = _prefix()
        first_name = f'{prefix}_plain_first'
        second_name = f'{prefix}_plain_second'
        with tempfile.TemporaryDirectory(prefix='verify-all-plain-') as directory:
            root = Path(directory)
            report_path = root / 'report.json'
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
            expected_ids = [
                f'{first_name}.FirstCase.test_one',
                f'{first_name}.FirstCase.test_two',
                f'{second_name}.SecondCase.test_three',
            ]
            with _FixtureModules(sources), \
                    mock.patch.object(
                        verify_all, 'MODULES', (first_name, second_name)):
                self.assertEqual(
                    _suite_ids(verify_all.collect_suite()),
                    expected_ids)
                self.assertEqual(
                    _suite_ids(verify_all.collect_suite(
                        [first_name, second_name], None)),
                    expected_ids)
                self.assertEqual(
                    _suite_ids(verify_all.collect_suite(
                        [first_name, second_name], [])),
                    expected_ids)
                self.assertEqual(
                    _suite_ids(verify_all.collect_suite(
                        [first_name, second_name], ())),
                    expected_ids)

                status, _, stderr = _run_main([
                    str(report_path), '--module', second_name,
                    '--module', first_name, '--module', second_name,
                ])
                self.assertEqual(status, 0, stderr)
                report = json.loads(report_path.read_text(encoding='utf-8'))
                self.assertEqual(report['selection'], {
                    'mode': 'selected',
                    'modules': [second_name, first_name],
                })
                self.assertEqual(
                    [row['id'] for row in report['test_results']],
                    [expected_ids[2], expected_ids[0], expected_ids[1]])

                status, stdout, stderr = _run_main([
                    '--list', '--module', second_name,
                    '--module', first_name, '--module', second_name,
                ])
                self.assertEqual(status, 0, stderr)
                payload = json.loads(stdout)
                self.assertEqual(payload, {
                    'modules': [second_name, first_name],
                    'test_ids': [
                        expected_ids[2], expected_ids[0], expected_ids[1]],
                    'count': 3,
                })
                self.assertEqual(
                    set(payload), {'modules', 'test_ids', 'count'})


class ParallelRunnerTests(unittest.TestCase):
    def test_parallel_is_bounded_isolated_and_ordered(self):
        prefix = _prefix()
        module_names = [f'{prefix}_slow_a', f'{prefix}_slow_b',
                        f'{prefix}_slow_c']
        with tempfile.TemporaryDirectory(
                prefix='verify-all-parallel-events-') as event_directory:
            events_path = Path(event_directory) / 'events.jsonl'
            sources = {
                name: f'''
                    import json
                    import os
                    import time
                    from pathlib import Path
                    from unittest import TestCase

                    EVENTS = Path({str(events_path)!r})

                    def record(kind):
                        with EVENTS.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps({{
                                'module': __name__,
                                'kind': kind,
                                'pid': os.getpid(),
                                'at': time.time(),
                            }}) + '\\n')

                    class SlowCase(TestCase):
                        def test_slow(self):
                            record('start')
                            time.sleep(0.3)
                            record('end')
                '''
                for name in module_names
            }
            with _CopiedRunner(sources) as root:
                report_path = root / 'report.json'
                completed = _run_copied_runner(
                    root, [str(report_path), '--jobs', '2'])
                self.assertEqual(completed.returncode, 0, completed.stderr)
                report = json.loads(report_path.read_text(encoding='utf-8'))

            records = [
                json.loads(line)
                for line in events_path.read_text(encoding='utf-8').splitlines()
            ]

        self.assertEqual(report['execution'], {
            'mode': 'parallel',
            'jobs': 2,
            'worker_errors': [],
        })
        self.assertEqual(report['selection'], {
            'mode': 'full',
            'modules': module_names,
        })
        self.assertEqual(
            [row['id'] for row in report['test_results']],
            [f'{name}.SlowCase.test_slow' for name in module_names])
        self.assertEqual(report['tests'], 3)
        self.assertTrue(report['passed'])

        by_module = {}
        for record in records:
            by_module.setdefault(record['module'], []).append(record)
        self.assertEqual(set(by_module), set(module_names))
        pids = {records_for_module[0]['pid']
                for records_for_module in by_module.values()}
        self.assertEqual(len(pids), 3)
        intervals = []
        for records_for_module in by_module.values():
            self.assertEqual(
                [record['kind'] for record in records_for_module],
                ['start', 'end'])
            intervals.append((records_for_module[0]['at'],
                              records_for_module[1]['at']))
        peak = max(
            sum(start <= candidate_start < end
                for start, end in intervals)
            for candidate_start, _ in intervals)
        self.assertEqual(peak, 2)

    def test_default_and_explicit_jobs_one_remain_serially_compatible(self):
        prefix = _prefix()
        first_name = f'{prefix}_serial_first'
        second_name = f'{prefix}_serial_second'
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
        with _CopiedRunner(sources) as root:
            default_report = root / 'default.json'
            jobs_one_report = root / 'jobs-one.json'
            default = _run_copied_runner(root, [str(default_report)])
            explicit = _run_copied_runner(
                root, [str(jobs_one_report), '--jobs', '1'])
            listed = _run_copied_runner(
                root, ['--list', '--jobs', '4'])
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            serial = json.loads(default_report.read_text(encoding='utf-8'))
            explicit_serial = json.loads(
                jobs_one_report.read_text(encoding='utf-8'))
            list_payload = json.loads(listed.stdout)

        self.assertNotIn('execution', serial)
        self.assertNotIn('execution', explicit_serial)
        self.assertEqual(set(serial), set(explicit_serial))
        self.assertEqual(serial['selection'], explicit_serial['selection'])
        self.assertEqual(serial['tests'], 3)
        self.assertEqual(
            [(row['id'], row['outcome'])
             for row in serial['test_results']],
            [(row['id'], row['outcome'])
             for row in explicit_serial['test_results']])
        self.assertEqual(
            list_payload,
            {
                'modules': [first_name, second_name],
                'test_ids': [
                    f'{first_name}.FirstCase.test_one',
                    f'{first_name}.FirstCase.test_two',
                    f'{second_name}.SecondCase.test_three',
                ],
                'count': 3,
            })

    def test_parallel_preserves_exact_selection_and_first_appearance_order(self):
        prefix = _prefix()
        first_name = f'{prefix}_selected_first'
        second_name = f'{prefix}_selected_second'
        with tempfile.TemporaryDirectory(
                prefix='verify-all-parallel-selection-') as marker_directory:
            markers = {
                key: Path(marker_directory) / key
                for key in ('alpha', 'beta', 'extra', 'gamma')
            }
            sources = {
                first_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    class FirstCase(TestCase):
                        def test_alpha(self):
                            Path({str(markers['alpha'])!r}).write_text(
                                'ran', encoding='utf-8')

                        def test_beta(self):
                            Path({str(markers['beta'])!r}).write_text(
                                'ran', encoding='utf-8')

                    class ExtraCase(TestCase):
                        def test_extra(self):
                            Path({str(markers['extra'])!r}).write_text(
                                'ran', encoding='utf-8')
                ''',
                second_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    class SecondCase(TestCase):
                        def test_gamma(self):
                            Path({str(markers['gamma'])!r}).write_text(
                                'ran', encoding='utf-8')
                ''',
            }
            selectors = [
                f'{second_name}.SecondCase',
                f'{first_name}.FirstCase.test_alpha',
                f'{first_name}.FirstCase',
                f'{second_name}.SecondCase',
            ]
            expected_ids = [
                f'{second_name}.SecondCase.test_gamma',
                f'{first_name}.FirstCase.test_alpha',
                f'{first_name}.FirstCase.test_beta',
            ]
            with _CopiedRunner(sources) as root:
                report_path = root / 'report.json'
                argv = [str(report_path), '--jobs', '4']
                for selector in selectors:
                    argv.extend(['--test', selector])
                completed = _run_copied_runner(root, argv)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                report = json.loads(report_path.read_text(encoding='utf-8'))
            marker_observations = {
                key: marker.exists() for key, marker in markers.items()
            }

        self.assertEqual(report['selection'], {
            'mode': 'selected',
            'modules': [second_name, first_name],
            'selectors': [
                f'{second_name}.SecondCase',
                f'{first_name}.FirstCase.test_alpha',
                f'{first_name}.FirstCase',
            ],
            'test_ids': expected_ids,
        })
        self.assertEqual(
            [row['id'] for row in report['test_results']],
            expected_ids)
        self.assertTrue(report['passed'])
        self.assertTrue(marker_observations['alpha'])
        self.assertTrue(marker_observations['beta'])
        self.assertTrue(marker_observations['gamma'])
        self.assertFalse(marker_observations['extra'])

    def test_parallel_aggregates_all_unittest_outcomes(self):
        prefix = _prefix()
        module_name = f'{prefix}_parallel_outcomes'
        sources = {
            module_name: '''
                from unittest import TestCase, expectedFailure, skip

                class OutcomeCase(TestCase):
                    def test_pass(self):
                        pass

                    def test_fail(self):
                        print('failure stdout context')
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
                            self.assertEqual(2, 1)
            ''',
        }
        with _CopiedRunner(sources) as root:
            report_path = root / 'report.json'
            completed = _run_copied_runner(
                root, [str(report_path), '--jobs', '2'])
            self.assertEqual(completed.returncode, 1)
            report = json.loads(report_path.read_text(encoding='utf-8'))

        self.assertEqual(report['execution']['worker_errors'], [])
        self.assertEqual(report['tests'], 7)
        detail, = report['execution']['failure_details']
        self.assertEqual(detail['module'], module_name)
        self.assertEqual(detail['stdout'], 'failure stdout context\n')
        for message in ('Traceback (most recent call last)', 'RuntimeError: boom',
                        'AssertionError: 1 != 2', 'AssertionError: 2 != 1',
                        'UNEXPECTED SUCCESS: test_unexpected_success'):
            self.assertIn(message, detail['stderr'])
            self.assertIn(message, completed.stderr)
        self.assertIn('failure stdout context', completed.stderr)
        self.assertFalse(report['passed'])
        self.assertEqual(
            (report['failures'], report['errors'], report['skipped']),
            (2, 1, 1))
        outcomes = {
            row['id'].rsplit('.', 1)[-1]: row['outcome']
            for row in report['test_results']
        }
        self.assertEqual(outcomes, {
            'test_pass': 'passed',
            'test_fail': 'failed',
            'test_error': 'error',
            'test_skip': 'skipped',
            'test_expected_failure': 'expected_failure',
            'test_unexpected_success': 'unexpected_success',
            'test_subtest_failure': 'failed',
        })

    def test_parallel_fixture_failures_keep_counts_without_fake_rows(self):
        prefix = _prefix()
        setup_name = f'{prefix}_setup_failure'
        teardown_name = f'{prefix}_teardown_failure'
        sources = {
            setup_name: '''
                from unittest import TestCase

                class SetupCase(TestCase):
                    @classmethod
                    def setUpClass(cls):
                        raise RuntimeError('class setup failed')

                    def test_one(self):
                        pass

                    def test_two(self):
                        pass
            ''',
            teardown_name: '''
                from unittest import TestCase

                class TeardownCase(TestCase):
                    @classmethod
                    def tearDownClass(cls):
                        raise RuntimeError('class teardown failed')

                    def test_three(self):
                        pass

                    def test_four(self):
                        pass
            ''',
        }
        with _CopiedRunner(sources) as root:
            report_path = root / 'report.json'
            completed = _run_copied_runner(
                root, [str(report_path), '--jobs', '2'])
            self.assertEqual(completed.returncode, 1)
            report = json.loads(report_path.read_text(encoding='utf-8'))

        self.assertEqual(report['execution']['worker_errors'], [])
        self.assertEqual(report['tests'], 2)
        self.assertFalse(report['passed'])
        self.assertEqual(
            (report['failures'], report['errors'], report['skipped']),
            (0, 2, 0))
        self.assertEqual(
            [row['id'] for row in report['test_results']],
            [f'{teardown_name}.TeardownCase.test_three',
             f'{teardown_name}.TeardownCase.test_four'])
        self.assertEqual(
            [row['outcome'] for row in report['test_results']],
            ['passed', 'passed'])
        details = report['execution']['failure_details']
        self.assertEqual([detail['module'] for detail in details],
                         [setup_name, teardown_name])
        for detail, message in zip(details, ('class setup failed',
                                             'class teardown failed')):
            self.assertIn('RuntimeError: ' + message, detail['stderr'])
            self.assertIn('RuntimeError: ' + message, completed.stderr)

    def test_failure_output_preserves_logs_with_non_utf8_bytes(self):
        module_name = f'{_prefix()}_binary_output'
        sources = {
            module_name: r'''
                import os
                from unittest import TestCase

                class OutputCase(TestCase):
                    def test_failure(self):
                        os.write(1, b'stdout context: \xff\n')
                        os.write(2, b'stderr context: \xff\n')
                        self.fail('failure after binary output')
            ''',
        }
        with _CopiedRunner(sources) as root:
            report_path = root / 'report.json'
            completed = _run_copied_runner(
                root, [str(report_path), '--jobs', '2'])
            self.assertEqual(completed.returncode, 1)
            report = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(report['execution']['worker_errors'], [])
        self.assertEqual((report['tests'], report['failures'], report['errors']),
                         (1, 1, 0))
        detail, = report['execution']['failure_details']
        self.assertEqual(detail['stdout'], 'stdout context: \ufffd\n')
        self.assertIn('stderr context: \ufffd', detail['stderr'])
        self.assertIn('AssertionError: failure after binary output', detail['stderr'])
        self.assertIn('AssertionError: failure after binary output', completed.stderr)

    def test_invalid_jobs_are_rejected_before_import_test_or_report(self):
        prefix = _prefix()
        module_name = f'{prefix}_invalid_jobs'
        with tempfile.TemporaryDirectory(
                prefix='verify-all-invalid-jobs-') as directory:
            root_path = Path(directory)
            import_marker = root_path / 'imported'
            test_marker = root_path / 'ran'
            report_path = root_path / 'report.json'
            sentinel = '{"keep": true}\n'
            report_path.write_text(sentinel, encoding='utf-8')
            sources = {
                module_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    Path({str(import_marker)!r}).write_text(
                        'imported', encoding='utf-8')

                    class InvalidJobsCase(TestCase):
                        def test_must_not_run(self):
                            Path({str(test_marker)!r}).write_text(
                                'ran', encoding='utf-8')
                ''',
            }
            with _CopiedRunner(sources) as root:
                for jobs in ('0', '5', '-1', 'abc', '1.5'):
                    completed = _run_copied_runner(
                        root, [str(report_path), '--module', module_name,
                               '--jobs', jobs])
                    self.assertEqual(completed.returncode, 2, jobs)
                    self.assertEqual(completed.stdout, '', jobs)
                    self.assertFalse(import_marker.exists(), jobs)
                    self.assertFalse(test_marker.exists(), jobs)
                    self.assertEqual(
                        report_path.read_text(encoding='utf-8'),
                        sentinel, jobs)

    def test_invalid_parallel_selection_returns_two_without_a_report(self):
        prefix = _prefix()
        module_name = f'{prefix}_invalid_selection'
        with tempfile.TemporaryDirectory(
                prefix='verify-all-invalid-selection-') as directory:
            report_path = Path(directory) / 'report.json'
            import_marker = Path(directory) / 'imported'
            test_marker = Path(directory) / 'ran'
            sentinel = '{"keep": true}\n'
            report_path.write_text(sentinel, encoding='utf-8')
            sources = {
                module_name: f'''
                    from pathlib import Path
                    from unittest import TestCase

                    Path({str(import_marker)!r}).write_text(
                        'imported', encoding='utf-8')

                    class KnownCase(TestCase):
                        def test_one(self):
                            Path({str(test_marker)!r}).write_text(
                                'ran', encoding='utf-8')
                ''',
            }
            with _CopiedRunner(sources) as root:
                malformed = _run_copied_runner(
                    root, [str(report_path), '--jobs', '4',
                           '--test', 'not a selector'])
                unknown_module = _run_copied_runner(
                    root, [str(report_path), '--jobs', '4',
                           '--module', 'not_a_module'])
                unmatched = _run_copied_runner(
                    root, [str(report_path), '--jobs', '4',
                           '--module', module_name,
                           '--test', f'{module_name}.MissingCase'])
                for completed in (malformed, unknown_module, unmatched):
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stdout, '')
                    self.assertEqual(
                        report_path.read_text(encoding='utf-8'), sentinel)
                self.assertFalse(test_marker.exists())
                self.assertTrue(import_marker.exists())

    def test_unexpected_child_exit_is_a_worker_error(self):
        prefix = _prefix()
        module_name = f'{prefix}_child_exit'
        sources = {
            module_name: '''
                import os
                from unittest import TestCase

                class ExitCase(TestCase):
                    def test_exits_without_report(self):
                        os._exit(7)
            ''',
        }
        with _CopiedRunner(sources) as root:
            report_path = root / 'report.json'
            completed = _run_copied_runner(
                root, [str(report_path), '--jobs', '2'])
            self.assertEqual(completed.returncode, 1)
            report = json.loads(report_path.read_text(encoding='utf-8'))

        self.assertFalse(report['passed'])
        self.assertEqual(report['tests'], 0)
        self.assertEqual(report['test_results'], [])
        self.assertEqual(report['execution']['mode'], 'parallel')
        self.assertTrue(report['execution']['worker_errors'])
        self.assertTrue(any(
            'report is unavailable' in error
            for error in report['execution']['worker_errors']))

    def test_foreign_child_report_ids_are_not_propagated(self):
        prefix = _prefix()
        module_name = f'{prefix}_foreign_report'
        sources = {
            module_name: '''
                from unittest import TestCase

                class TunneledCase(TestCase):
                    def test_passes(self):
                        pass
            ''',
        }
        with _CopiedRunner(
                sources, corrupt_child_reports=True) as root:
            report_path = root / 'report.json'
            completed = _run_copied_runner(
                root, [str(report_path), '--jobs', '2'],
                env={'VERIFY_ALL_CORRUPT_CHILD': '1'})
            self.assertEqual(completed.returncode, 1)
            report = json.loads(report_path.read_text(encoding='utf-8'))

        self.assertFalse(report['passed'])
        self.assertEqual(report['test_results'], [])
        self.assertEqual(report['tests'], 0)
        self.assertTrue(report['execution']['worker_errors'])
        self.assertTrue(any(
            'unknown test ID' in error
            for error in report['execution']['worker_errors']))
        self.assertNotIn(
            '__foreign__.Foreign.test_foreign',
            json.dumps(report['test_results']))

    def test_hostile_child_report_types_do_not_escape_validation(self):
        module_name = 'fixture_hostile_report'
        expected_ids = [f'{module_name}.Case.test_one']
        payload = verify_all._build_report(
            1, True, 0, 0, 0, 'selected', [module_name], 0.01,
            expected_ids, expected_ids,
            [{'id': expected_ids[0], 'outcome': 'passed', 'seconds': 0.01}])

        bad_outcome = json.loads(json.dumps(payload))
        bad_outcome['test_results'][0]['outcome'] = []
        errors, trusted = verify_all._validate_child_report(
            bad_outcome, module_name, expected_ids, 0)
        self.assertTrue(any('unittest outcome' in error for error in errors))
        self.assertIsNone(trusted)

        huge_duration = json.loads(json.dumps(payload))
        huge_duration['seconds'] = 10 ** 1000
        errors, trusted = verify_all._validate_child_report(
            huge_duration, module_name, expected_ids, 0)
        self.assertTrue(any('seconds must be' in error for error in errors))
        self.assertIsNone(trusted)

        huge_row_duration = json.loads(json.dumps(payload))
        huge_row_duration['test_results'][0]['seconds'] = 10 ** 1000
        errors, trusted = verify_all._validate_child_report(
            huge_row_duration, module_name, expected_ids, 0)
        self.assertTrue(any('seconds must be' in error for error in errors))
        self.assertIsNone(trusted)

    def test_undecodable_child_report_becomes_a_worker_error(self):
        module_name = 'fixture_undecodable_report'
        expected_ids = [f'{module_name}.Case.test_one']
        with tempfile.TemporaryDirectory(
                prefix='verify-all-undecodable-report-') as directory:
            root = Path(directory)
            report_path = root / 'report.json'
            stderr_path = root / 'stderr.log'
            report_path.write_bytes(b'{"tests": "\xff"}')
            stderr_path.write_bytes(b'\xff')
            report = verify_all._combine_parallel(
                [{
                    'module': module_name,
                    'expected_ids': expected_ids,
                    'report_path': str(report_path),
                    'stderr_path': str(stderr_path),
                    'returncode': 0,
                }],
                'selected', [module_name], 0.01, None, expected_ids, 2)

        self.assertFalse(report['passed'])
        self.assertEqual(report['tests'], 0)
        self.assertEqual(report['test_results'], [])
        self.assertTrue(report['execution']['worker_errors'])
        self.assertTrue(any(
            'unavailable or undecodable' in error
            for error in report['execution']['worker_errors']))

    def test_child_report_validator_rejects_integrity_violations(self):
        module_name = 'fixture_report_module'
        expected_ids = [
            f'{module_name}.Case.test_alpha',
            f'{module_name}.Case.test_beta',
        ]
        rows = [
            {'id': expected_ids[0], 'outcome': 'passed', 'seconds': 0.01},
            {'id': expected_ids[1], 'outcome': 'skipped', 'seconds': 0.01},
        ]
        payload = verify_all._build_report(
            2, True, 0, 0, 1, 'selected', [module_name], 0.02,
            expected_ids, expected_ids, rows)
        errors, trusted = verify_all._validate_child_report(
            payload, module_name, expected_ids, 0)
        self.assertEqual(errors, [])
        self.assertIsNotNone(trusted)

        missing = json.loads(json.dumps(payload))
        missing['tests'] = 1
        missing['test_results'] = missing['test_results'][:1]
        missing['skipped'] = 0
        errors, trusted = verify_all._validate_child_report(
            missing, module_name, expected_ids, 0)
        self.assertTrue(any('missing expected test ID' in error
                            for error in errors))
        self.assertIsNone(trusted)

        duplicate = json.loads(json.dumps(payload))
        duplicate['test_results'][1] = dict(duplicate['test_results'][0])
        duplicate['skipped'] = 0
        errors, trusted = verify_all._validate_child_report(
            duplicate, module_name, expected_ids, 0)
        self.assertTrue(any('duplicate test ID' in error for error in errors))
        self.assertIsNone(trusted)

        foreign = json.loads(json.dumps(payload))
        foreign['test_results'][1]['id'] = 'foreign.Case.test_foreign'
        errors, trusted = verify_all._validate_child_report(
            foreign, module_name, expected_ids, 0)
        self.assertTrue(any('unknown test ID' in error for error in errors))
        self.assertIsNone(trusted)

        bad_outcome = json.loads(json.dumps(payload))
        bad_outcome['test_results'][0]['outcome'] = 'flaky'
        errors, trusted = verify_all._validate_child_report(
            bad_outcome, module_name, expected_ids, 0)
        self.assertTrue(any('unittest outcome' in error for error in errors))
        self.assertIsNone(trusted)

        contradictory = json.loads(json.dumps(payload))
        contradictory['passed'] = False
        errors, trusted = verify_all._validate_child_report(
            contradictory, module_name, expected_ids, 0)
        self.assertTrue(any('passed contradicts' in error
                            for error in errors))
        self.assertIsNone(trusted)

        orphan_failure_count = json.loads(json.dumps(payload))
        orphan_failure_count['failures'] = 1
        orphan_failure_count['passed'] = False
        errors, trusted = verify_all._validate_child_report(
            orphan_failure_count, module_name, expected_ids, 1)
        self.assertTrue(any('no corresponding test result row' in error
                            for error in errors))
        self.assertIsNone(trusted)

        errors, trusted = verify_all._validate_child_report(
            payload, module_name, expected_ids, 9)
        self.assertTrue(any('unexpected status 9' in error
                            for error in errors))
        self.assertIsNone(trusted)


if __name__ == '__main__':
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Run every locally declared regression once, with real Git/macOS sandboxes.

The module is import-safe: tests run only through ``main``.  ``--list`` and the
JSON report make selection and timing evidence reusable by later RSI rounds.
"""
import argparse
import contextlib
import importlib
import io
import json
import math
from pathlib import Path
import sys
import time
import unittest


MODULES = [
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

MODEL_BOUNDARY = 'deterministic mocks; separate live report required'


class RecordingTextTestResult(unittest.TextTestResult):
    """Text result that records one timed row per executed test case."""

    _OUTCOME_PRIORITY = {
        'passed': 0,
        'skipped': 1,
        'expected_failure': 2,
        'unexpected_success': 3,
        'failed': 4,
        'error': 5,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_results = []
        self._records = {}
        self._starts = {}

    def startTest(self, test):
        super().startTest(test)
        self._starts[id(test)] = time.perf_counter()
        record = {'id': test.id(), 'outcome': None, 'seconds': 0.0}
        self._records[id(test)] = record
        self.test_results.append(record)

    def stopTest(self, test):
        started = self._starts.pop(id(test), None)
        record = self._records.pop(id(test), None)
        if record is not None:
            record['seconds'] = self._elapsed(started)
            if record['outcome'] is None:
                record['outcome'] = 'passed'
        super().stopTest(test)

    @staticmethod
    def _elapsed(started):
        if started is None:
            return 0.0
        elapsed = time.perf_counter() - started
        if not math.isfinite(elapsed) or elapsed < 0:
            return 0.0
        return elapsed

    def _record_for(self, test):
        record = self._records.get(id(test))
        if record is None and hasattr(test, 'test_case'):
            record = self._records.get(id(test.test_case))
        return record

    def _set_outcome(self, test, outcome):
        record = self._record_for(test)
        if record is None:
            return
        current = record['outcome']
        if current is None or self._OUTCOME_PRIORITY[outcome] >= self._OUTCOME_PRIORITY[current]:
            record['outcome'] = outcome

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            outcome = 'failed' if issubclass(err[0], test.failureException) else 'error'
            self._set_outcome(test, outcome)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set_outcome(test, 'passed')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set_outcome(test, 'failed')

    def addError(self, test, err):
        super().addError(test, err)
        self._set_outcome(test, 'error')

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set_outcome(test, 'skipped')

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set_outcome(test, 'expected_failure')

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set_outcome(test, 'unexpected_success')


def _iter_test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _iter_test_cases(test)
        else:
            yield test


def _collect_tests(module_names=None):
    """Collect each locally declared test once, in declaration/order of names."""
    if module_names is None:
        module_names = MODULES
    elif isinstance(module_names, str):
        module_names = [module_names]

    tests = []
    seen_modules = set()
    seen_classes = set()
    seen_test_ids = set()
    for module_name in module_names:
        if module_name in seen_modules:
            continue
        seen_modules.add(module_name)
        module = importlib.import_module(module_name)
        for value in vars(module).values():
            if not isinstance(value, type):
                continue
            if not issubclass(value, unittest.TestCase):
                continue
            if value.__module__ != module_name:
                continue
            if id(value) in seen_classes:
                continue
            seen_classes.add(id(value))
            for name, attribute in value.__dict__.items():
                if not name.startswith('test_') or not callable(attribute):
                    continue
                test = value(name)
                test_id = test.id()
                if test_id in seen_test_ids:
                    continue
                seen_test_ids.add(test_id)
                tests.append(test)
    return tests


def _selector_matches(test_id, selector):
    """Match an exact class or method selector against a collected test ID."""
    parts = selector.split('.')
    if len(parts) == 2:
        class_id, _, _ = test_id.rpartition('.')
        return class_id == selector
    if len(parts) == 3:
        return test_id == selector
    return False


def _filter_tests(tests, selectors):
    """Return tests matching any selector and the selectors matching nothing."""
    unique_selectors = []
    seen_selectors = set()
    for selector in selectors:
        if selector not in seen_selectors:
            seen_selectors.add(selector)
            unique_selectors.append(selector)

    matched = {selector: False for selector in unique_selectors}
    selected = []
    seen_test_ids = set()
    for test in tests:
        test_id = test.id()
        selected_test = False
        for selector in unique_selectors:
            if not _selector_matches(test_id, selector):
                continue
            matched[selector] = True
            selected_test = True
        if selected_test:
            if test_id not in seen_test_ids:
                seen_test_ids.add(test_id)
                selected.append(test)
    unmatched = [selector for selector in unique_selectors if not matched[selector]]
    return selected, unmatched


def collect_suite(module_names=None, test_selectors=None):
    """Collect each locally declared test once, in declaration/order of names.

    ``test_selectors`` optionally narrows the collected tests to exact
    ``module.Class`` or ``module.Class.test_method`` selectors.  ``None`` and
    empty sequences preserve the unselected collection behavior.  Every
    selector must match at least one collected test.
    """
    tests = _collect_tests(module_names)
    if test_selectors is None:
        selectors = []
    elif isinstance(test_selectors, str):
        selectors = [test_selectors]
    else:
        selectors = list(test_selectors)
    if selectors:
        tests, unmatched = _filter_tests(tests, selectors)
        if unmatched:
            raise ValueError(
                'test selector(s) matched no collected test: '
                + ', '.join(unmatched))
    suite = unittest.TestSuite()
    suite.addTests(tests)
    return suite


def _selection(requested):
    if not requested:
        return 'full', list(MODULES)
    return 'selected', _dedupe(requested)


def _dedupe(values):
    selected = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            selected.append(value)
    return selected


def _selector_module(selector):
    """Return a selector's module name, or ``None`` when it is malformed."""
    parts = selector.split('.')
    if len(parts) not in (2, 3):
        return None
    if not all(part.isidentifier() for part in parts):
        return None
    return parts[0]


def _report(result, mode, modules, seconds, selectors=None, test_ids=None):
    selection = {'mode': mode, 'modules': list(modules)}
    if selectors is not None:
        selection['selectors'] = list(selectors)
        selection['test_ids'] = list(test_ids or [])
    return {
        'tests': result.testsRun,
        'passed': result.wasSuccessful(),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'real_git_and_sandbox': True,
        'model_boundary': MODEL_BOUNDARY,
        'selection': selection,
        'seconds': seconds,
        'test_results': result.test_results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', nargs='?', help='optional JSON report path')
    parser.add_argument('--module', action='append', default=[], metavar='NAME',
                        help='run only this allowlisted module; repeatable')
    parser.add_argument('--test', action='append', default=[], metavar='SELECTOR',
                        dest='test_selectors',
                        help='run only an exact module.Class or '
                             'module.Class.test_method; repeatable')
    parser.add_argument('--list', action='store_true', dest='list_only',
                        help='print selected modules and test IDs without running tests')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    selectors = _dedupe(args.test_selectors)
    allowed = set(MODULES)
    unknown = [name for name in args.module if name not in allowed]
    if unknown:
        print('verify_all: unknown module(s): ' + ', '.join(unknown), file=sys.stderr)
        print('verify_all: allowed modules: ' + ', '.join(MODULES), file=sys.stderr)
        return 2

    selector_errors = []
    selector_modules = []
    seen_selector_modules = set()
    for selector in selectors:
        module_name = _selector_module(selector)
        if module_name is None:
            selector_errors.append(
                f"malformed test selector {selector!r}: expected "
                'module.Class or module.Class.test_method')
            continue
        if module_name not in allowed:
            selector_errors.append(
                f"test selector {selector!r} names unknown module "
                f'{module_name!r}')
            continue
        if module_name not in seen_selector_modules:
            seen_selector_modules.add(module_name)
            selector_modules.append(module_name)

    if args.module:
        mode, modules = _selection(args.module)
        in_scope = set(modules)
        for selector in selectors:
            module_name = _selector_module(selector)
            if module_name is not None and module_name in allowed and \
                    module_name not in in_scope:
                selector_errors.append(
                    f"test selector {selector!r} names module "
                    f'{module_name!r} outside the selected module scope')
    elif selectors:
        mode, modules = 'selected', selector_modules
    else:
        mode, modules = _selection(args.module)

    if selector_errors:
        for error in selector_errors:
            print('verify_all: ' + error, file=sys.stderr)
        return 2

    if args.list_only:
        # Keep stdout machine-readable even if an imported module prints.
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                suite = collect_suite(modules, selectors)
                test_ids = [test.id() for test in _iter_test_cases(suite)]
        except ValueError as exc:
            print(f'verify_all: {exc}', file=sys.stderr)
            return 2
        payload = {'modules': modules, 'test_ids': test_ids, 'count': len(test_ids)}
        if selectors:
            payload['selectors'] = selectors
        sys.stdout.write(json.dumps(payload) + '\n')
        return 0

    try:
        suite = collect_suite(modules, selectors)
    except ValueError as exc:
        print(f'verify_all: {exc}', file=sys.stderr)
        return 2
    test_ids = [test.id() for test in _iter_test_cases(suite)]
    started = time.perf_counter()
    result = unittest.TextTestRunner(
        verbosity=2, resultclass=RecordingTextTestResult).run(suite)
    seconds = time.perf_counter() - started
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    report = _report(
        result, mode, modules, seconds, selectors or None, test_ids)
    if args.report is not None:
        try:
            Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
        except OSError as exc:
            print(f'verify_all: cannot write report {args.report}: {exc}', file=sys.stderr)
            return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())

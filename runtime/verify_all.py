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


def collect_suite(module_names=None):
    """Collect each locally declared test once, in declaration/order of names."""
    if module_names is None:
        module_names = MODULES
    elif isinstance(module_names, str):
        module_names = [module_names]

    suite = unittest.TestSuite()
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
                suite.addTest(test)
    return suite


def _selection(requested):
    if not requested:
        return 'full', list(MODULES)
    selected = []
    seen = set()
    for module_name in requested:
        if module_name not in seen:
            seen.add(module_name)
            selected.append(module_name)
    return 'selected', selected


def _report(result, mode, modules, seconds):
    return {
        'tests': result.testsRun,
        'passed': result.wasSuccessful(),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'real_git_and_sandbox': True,
        'model_boundary': MODEL_BOUNDARY,
        'selection': {'mode': mode, 'modules': list(modules)},
        'seconds': seconds,
        'test_results': result.test_results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', nargs='?', help='optional JSON report path')
    parser.add_argument('--module', action='append', default=[], metavar='NAME',
                        help='run only this allowlisted module; repeatable')
    parser.add_argument('--list', action='store_true', dest='list_only',
                        help='print selected modules and test IDs without running tests')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    allowed = set(MODULES)
    unknown = [name for name in args.module if name not in allowed]
    if unknown:
        print('verify_all: unknown module(s): ' + ', '.join(unknown), file=sys.stderr)
        print('verify_all: allowed modules: ' + ', '.join(MODULES), file=sys.stderr)
        return 2

    mode, modules = _selection(args.module)
    if args.list_only:
        # Keep stdout machine-readable even if an imported module prints.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            suite = collect_suite(modules)
            test_ids = [test.id() for test in _iter_test_cases(suite)]
        payload = {'modules': modules, 'test_ids': test_ids, 'count': len(test_ids)}
        sys.stdout.write(json.dumps(payload) + '\n')
        return 0

    suite = collect_suite(modules)
    started = time.perf_counter()
    result = unittest.TextTestRunner(
        verbosity=2, resultclass=RecordingTextTestResult).run(suite)
    seconds = time.perf_counter() - started
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    report = _report(result, mode, modules, seconds)
    if args.report is not None:
        try:
            Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
        except OSError as exc:
            print(f'verify_all: cannot write report {args.report}: {exc}', file=sys.stderr)
            return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())

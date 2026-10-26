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
import subprocess
import sys
import tempfile
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
    'test_progress_guard',
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

MODEL_BOUNDARY = 'deterministic mocks; separate live report required'
_VALID_OUTCOMES = frozenset({
    'passed',
    'failed',
    'error',
    'skipped',
    'expected_failure',
    'unexpected_success',
})
_REPORT_FIELDS = frozenset({
    'tests',
    'passed',
    'failures',
    'errors',
    'skipped',
    'real_git_and_sandbox',
    'model_boundary',
    'selection',
    'seconds',
    'test_results',
})
_ROW_FIELDS = frozenset({'id', 'outcome', 'seconds'})


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


def _build_report(tests, passed, failures, errors, skipped, mode, modules,
                  seconds, selectors=None, test_ids=None, test_results=None,
                  execution=None):
    selection = {'mode': mode, 'modules': list(modules)}
    if selectors is not None:
        selection['selectors'] = list(selectors)
        selection['test_ids'] = list(test_ids or [])
    report = {
        'tests': tests,
        'passed': passed,
        'failures': failures,
        'errors': errors,
        'skipped': skipped,
        'real_git_and_sandbox': True,
        'model_boundary': MODEL_BOUNDARY,
        'selection': selection,
        'seconds': seconds,
        'test_results': list(test_results or []),
    }
    if execution is not None:
        report['execution'] = execution
    return report


def _report(result, mode, modules, seconds, selectors=None, test_ids=None):
    return _build_report(
        result.testsRun, result.wasSuccessful(), len(result.failures),
        len(result.errors), len(result.skipped), mode, modules, seconds,
        selectors, test_ids, result.test_results)


def _is_nonnegative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_seconds(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(numeric) and numeric >= 0


def _validate_child_report(payload, module, expected_ids, returncode):
    """Return integrity errors and a trusted child report, when one exists."""
    errors = []
    if not isinstance(payload, dict):
        return ['report is not a JSON object'], None

    fields = set(payload)
    if fields != _REPORT_FIELDS:
        if _REPORT_FIELDS - fields:
            errors.append(
                'report is missing field(s): '
                + ', '.join(sorted(_REPORT_FIELDS - fields)))
        if fields - _REPORT_FIELDS:
            errors.append(
                'report has unexpected field(s): '
                + ', '.join(sorted(fields - _REPORT_FIELDS)))

    tests = payload.get('tests')
    passed = payload.get('passed')
    failures = payload.get('failures')
    errors_count = payload.get('errors')
    skipped = payload.get('skipped')
    seconds = payload.get('seconds')
    rows = payload.get('test_results')
    for name, value in (
            ('tests', tests), ('failures', failures), ('errors', errors_count),
            ('skipped', skipped)):
        if not _is_nonnegative_int(value):
            errors.append(f'{name} must be a nonnegative integer')
    if not isinstance(passed, bool):
        errors.append('passed must be a boolean')
    if not _is_seconds(seconds):
        errors.append('seconds must be a finite nonnegative number')
    if payload.get('real_git_and_sandbox') is not True:
        errors.append('real_git_and_sandbox must be true')
    if payload.get('model_boundary') != MODEL_BOUNDARY:
        errors.append('model_boundary does not match this runner')

    expected_selection = {
        'mode': 'selected',
        'modules': [module],
        'selectors': list(expected_ids),
        'test_ids': list(expected_ids),
    }
    if payload.get('selection') != expected_selection:
        errors.append('selection identity does not match the precollected scope')

    if not isinstance(rows, list):
        errors.append('test_results must be a list')
        rows = []

    row_outcomes = []
    seen_ids = set()
    expected_id_set = set(expected_ids)
    expected_positions = {test_id: index
                          for index, test_id in enumerate(expected_ids)}
    positions = []
    for index, row in enumerate(rows):
        label = f'test_results[{index}]'
        if not isinstance(row, dict):
            errors.append(f'{label} must be an object')
            continue
        if set(row) != _ROW_FIELDS:
            errors.append(f'{label} has unexpected shape')
            continue
        test_id = row.get('id')
        outcome = row.get('outcome')
        row_seconds = row.get('seconds')
        if not isinstance(test_id, str) or not test_id:
            errors.append(f'{label}.id must be a nonempty string')
            continue
        if not isinstance(outcome, str) or outcome not in _VALID_OUTCOMES:
            errors.append(f'{label}.outcome is not a unittest outcome')
            continue
        if not _is_seconds(row_seconds):
            errors.append(f'{label}.seconds must be finite and nonnegative')
            continue
        if test_id in seen_ids:
            errors.append(f'duplicate test ID {test_id!r}')
            continue
        seen_ids.add(test_id)
        if test_id not in expected_id_set:
            errors.append(f'unknown test ID {test_id!r}')
            continue
        positions.append(expected_positions[test_id])
        row_outcomes.append(outcome)

    if isinstance(tests, int) and not isinstance(tests, bool) \
            and tests != len(rows):
        errors.append(
            f'tests={tests} contradicts {len(rows)} test result row(s)')
    if positions != sorted(positions):
        errors.append('test result rows are not in precollected order')

    missing = [test_id for test_id in expected_ids if test_id not in seen_ids]
    if missing:
        # Class/module setup errors are represented by unittest as error
        # events rather than test rows.  They are ordinary failures, not
        # report corruption, but may never become a successful run.
        if not _is_nonnegative_int(errors_count) or errors_count == 0:
            errors.append(
                'missing expected test ID(s): ' + ', '.join(missing))

    failed_rows = row_outcomes.count('failed')
    error_rows = row_outcomes.count('error')
    skipped_rows = row_outcomes.count('skipped')
    unexpected_rows = row_outcomes.count('unexpected_success')
    if _is_nonnegative_int(failures) and failures < failed_rows:
        errors.append('failure count is smaller than failed rows')
    if _is_nonnegative_int(failures) and failures > 0 \
            and failed_rows + error_rows == 0:
        errors.append('failure count has no corresponding test result row')
    if _is_nonnegative_int(errors_count) and errors_count < error_rows:
        errors.append('error count is smaller than error rows')
    if _is_nonnegative_int(skipped) and skipped < skipped_rows:
        errors.append('skip count is smaller than skipped rows')

    if isinstance(passed, bool) and _is_nonnegative_int(
            failures) and _is_nonnegative_int(errors_count):
        expected_passed = (
            failures == 0 and errors_count == 0 and unexpected_rows == 0)
        if passed != expected_passed:
            errors.append('passed contradicts unittest outcomes and counts')

    if returncode not in (0, 1):
        errors.append(f'child exited with unexpected status {returncode}')
    elif isinstance(passed, bool):
        if returncode == 0 and not passed:
            errors.append('child exited 0 with an unsuccessful report')
        if returncode == 1 and passed:
            errors.append('child exited 1 with a successful report')

    return errors, (None if errors else payload)


def _child_output_tail(text, limit=2000):
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return '...' + text[-limit:]


def _diagnostic(message, child_output):
    tail = _child_output_tail(child_output)
    if tail:
        return message + '\n' + tail
    return message


def _read_text(path):
    try:
        return Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''


def _failure_output(path):
    # Keep complete failure logs after the temporary worker directory is gone.
    # A non-UTF-8 byte from a subprocess must not discard its traceback.
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        return f'[could not read worker output: {exc}]\n'


def _parallel_groups(tests):
    groups = {}
    order = []
    for test in tests:
        module = type(test).__module__
        if module not in groups:
            groups[module] = []
            order.append(module)
        groups[module].append(test.id())
    return [(module, groups[module]) for module in order]


def _terminate(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _combine_parallel(tasks, mode, modules, seconds, selectors, test_ids,
                      jobs):
    test_results = []
    tests = failures = errors = skipped = 0
    passed = True
    worker_errors = []
    failure_details = []

    for task in tasks:
        module = task['module']
        if task.get('launch_error'):
            worker_errors.append(
                f'{module}: could not launch child: {task["launch_error"]}')
            passed = False
            continue
        returncode = task.get('returncode')
        report_path = task['report_path']
        try:
            report_text = Path(report_path).read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            worker_errors.append(
                _diagnostic(
                    f'{module}: child report is unavailable or undecodable: '
                    f'{exc}',
                    _read_text(task['stderr_path'])))
            passed = False
            continue
        try:
            payload = json.loads(report_text)
        except (TypeError, ValueError) as exc:
            worker_errors.append(
                _diagnostic(
                    f'{module}: child report is not valid JSON: {exc}',
                    _read_text(task['stderr_path'])))
            passed = False
            continue

        integrity_errors, trusted = _validate_child_report(
            payload, module, task['expected_ids'], returncode)
        if integrity_errors:
            child_output = _read_text(task['stderr_path'])
            for error in integrity_errors:
                worker_errors.append(
                    _diagnostic(f'{module}: {error}', child_output))
            passed = False
            continue

        test_results.extend(trusted['test_results'])
        tests += trusted['tests']
        failures += trusted['failures']
        errors += trusted['errors']
        skipped += trusted['skipped']
        if not trusted['passed']:
            passed = False
            failure_details.append({
                'module': module,
                'stdout': _failure_output(task['stdout_path']),
                'stderr': _failure_output(task['stderr_path']),
            })

    if worker_errors:
        passed = False
    execution = {
        'mode': 'parallel',
        'jobs': jobs,
        'worker_errors': worker_errors,
    }
    if failure_details:
        execution['failure_details'] = failure_details
    return _build_report(
        tests, passed, failures, errors, skipped, mode, modules, seconds,
        selectors, test_ids, test_results, execution)


def _run_parallel(tests, mode, modules, selectors, test_ids, jobs):
    groups = _parallel_groups(tests)
    started = time.perf_counter()
    runner_path = str(Path(__file__).resolve())

    with tempfile.TemporaryDirectory(prefix='verify-all-parallel-') as directory:
        root = Path(directory)
        tasks = []
        for index, (module, expected_ids) in enumerate(groups):
            tasks.append({
                'module': module,
                'expected_ids': expected_ids,
                'report_path': str(root / f'report-{index}.json'),
                'stdout_path': str(root / f'stdout-{index}.log'),
                'stderr_path': str(root / f'stderr-{index}.log'),
            })

        pending = list(tasks)
        active = []
        try:
            while pending or active:
                while pending and len(active) < jobs:
                    task = pending.pop(0)
                    command = [
                        sys.executable, '-B', runner_path,
                        task['report_path'], '--module', task['module'],
                    ]
                    for test_id in task['expected_ids']:
                        command.extend(['--test', test_id])
                    try:
                        with open(task['stdout_path'], 'w',
                                  encoding='utf-8') as stdout_file, \
                                open(task['stderr_path'], 'w',
                                     encoding='utf-8') as stderr_file:
                            task['process'] = subprocess.Popen(
                                command, stdout=stdout_file,
                                stderr=stderr_file)
                        active.append(task)
                    except OSError as exc:
                        task['launch_error'] = str(exc)
                if active:
                    for task in list(active):
                        returncode = task['process'].poll()
                        if returncode is not None:
                            task['returncode'] = returncode
                            active.remove(task)
                    if active:
                        time.sleep(0.02)
        finally:
            for task in active:
                _terminate(task['process'])
                task['returncode'] = task['process'].poll()

        seconds = time.perf_counter() - started
        if not math.isfinite(seconds) or seconds < 0:
            seconds = 0.0
        return _combine_parallel(
            tasks, mode, modules, seconds, selectors, test_ids, jobs)


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
    parser.add_argument('--jobs', type=int, default=1, metavar='N',
                        help='run selected nonempty modules in 1..4 isolated '
                             'child processes (default: 1, serial)')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    if not 1 <= args.jobs <= 4:
        print('verify_all: --jobs must be an integer from 1 to 4',
              file=sys.stderr)
        return 2

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
    tests = list(_iter_test_cases(suite))
    test_ids = [test.id() for test in tests]
    if args.jobs == 1:
        started = time.perf_counter()
        result = unittest.TextTestRunner(
            verbosity=2, resultclass=RecordingTextTestResult).run(suite)
        seconds = time.perf_counter() - started
        if not math.isfinite(seconds) or seconds < 0:
            seconds = 0.0
        report = _report(
            result, mode, modules, seconds, selectors or None, test_ids)
    else:
        report = _run_parallel(
            tests, mode, modules, selectors or None, test_ids, args.jobs)
        for error in report['execution']['worker_errors']:
            print('verify_all: ' + error, file=sys.stderr)
        for detail in report['execution'].get('failure_details', []):
            for stream in ('stdout', 'stderr'):
                output = detail[stream]
                if output:
                    print(f'verify_all: {detail["module"]}: {stream}\n{output}',
                          end='' if output.endswith('\n') else '\n',
                          file=sys.stderr)
    if args.report is not None:
        try:
            Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
        except OSError as exc:
            print(f'verify_all: cannot write report {args.report}: {exc}', file=sys.stderr)
            return 1
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

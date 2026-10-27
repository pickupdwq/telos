"""Comparison fairness, isolation, reports and real CLI/manual-gate regressions."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import comparison as c
import evaluation_suite
import telos as b
from test_local_executor import config, spec, server, completion


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telos-comparison-test-')
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'experiment'
        self.cfg = self.base / 'config.json'
        c.write(self.cfg, config())
        self.spec = {'baseline': str(c.SOURCE), 'candidate': str(c.SOURCE),
                     'config': str(self.cfg), 'executor_profile': 'local',
                     'case_ids': ['clamp-bounds'], 'repeats': 1,
                     'budget': {'max_requests': 10, 'max_total_tokens': 10000}}
        self.patches = [patch.object(c, 'normal_tasks_idle'),
                        patch.object(c, 'scheduler_root', return_value=self.base / 'scheduler')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def initialize(self):
        return c.initialize(self.root, self.spec)

    def test_plan_freezes_sources_versions_budget_and_alternating_order_without_model_calls(self):
        self.spec.update(case_ids=['clamp-bounds', 'median-even'], repeats=2)
        self.assertEqual(self.initialize()['model_requests_sent'], 0)
        manifest = c.read(self.root / 'manifest.json')
        self.assertEqual(len(manifest['trials']), 8)
        self.assertEqual(manifest['request_ceiling'], 80)
        self.assertEqual([t['side'] for t in manifest['trials']],
                         ['baseline', 'candidate', 'candidate', 'baseline', 'candidate', 'baseline', 'baseline', 'candidate'])
        self.assertEqual(manifest['versions']['baseline']['sha256'], manifest['versions']['candidate']['sha256'])
        self.assertFalse(c.read(self.root / 'versions/baseline/config.json')['experience_enabled'])
        self.assertEqual(c.next_trial(self.root)['completed'], 0)
        out = c.report(self.root)
        self.assertFalse(out['complete'])
        self.assertFalse(out['comparable'])
        self.assertIsNone(out['candidate_minus_baseline_tokens'])
        self.assertEqual(out['sides']['candidate']['pending'], 4)

    def test_live_opt_in_and_start_replay_do_not_send_work(self):
        self.initialize()
        with self.assertRaisesRegex(c.ComparisonError, '--live'):
            c.start(self.root)
        first = c.start(self.root, live=True)
        again = c.start(self.root, live=True)
        self.assertEqual(first['session_id'], again['session_id'])
        status = c.call(self.root, 'telos_status', {})['result']
        self.assertEqual(status['phase'], 'ready')
        self.assertEqual(status['usage']['totals']['requests'], 0)
        self.assertFalse(status['accepted'])
        with self.assertRaisesRegex(c.ComparisonError, 'failure reason'):
            c.finish(self.root)
        out = c.finish(self.root, reason='Explicitly stop this no-model test')
        self.assertEqual(out['outcome'], 'failed')
        self.assertFalse((self.base / 'scheduler/active.json').exists())

    def test_initial_instructions_session_selection_and_tool_scope_are_protected(self):
        self.initialize(); c.start(self.root, live=True)
        for tool, args in [('telos_send', {'message': 'Extra help for this variant'}),
                           ('telos_status', {'session_id': 'x'}),
                           ('telos_grade', {'_override': True}), ('telos_open', {})]:
            with self.subTest(tool=tool), self.assertRaises(c.ComparisonError):
                c.call(self.root, tool, args)
        c.finish(self.root, reason='No calls should have been forwarded')

    def test_no_global_configuration_or_experience_changes(self):
        original = self.cfg.read_bytes()
        self.initialize(); opened = c.start(self.root, live=True)
        trial_id = opened['trial']['id']
        trial_root = self.root / 'trials' / trial_id
        frozen = c.read(trial_root / 'state/runs' / opened['session_id'] / 'contract.json')
        self.assertEqual(frozen['experience_ids'], [])
        self.assertEqual(frozen['experience_context'], '')
        self.assertEqual(frozen['budget'], self.spec['budget'])
        self.assertEqual(frozen['threshold'], 90)
        self.assertEqual(frozen['max_rounds'], 2)
        self.assertEqual(self.cfg.read_bytes(), original)
        self.assertTrue(Path(opened['worktree']).is_relative_to(trial_root / 'cache'))
        self.assertFalse((trial_root / 'state/experience').exists())
        c.finish(self.root, reason='Isolated no-model test')

    def test_disabled_token_limits_freeze_the_same_request_only_comparison_budget(self):
        cfg = c.read(self.cfg)
        cfg['task_token_limits_enabled'] = False
        c.write(self.cfg, cfg)
        self.initialize()
        self.assertEqual(c.read(self.root / 'manifest.json')['budget'], {'max_requests': 10})
        opened = c.start(self.root, live=True)
        trial_root = self.root / 'trials' / opened['trial']['id']
        frozen = c.read(trial_root / 'state/runs' / opened['session_id'] / 'contract.json')
        self.assertEqual(frozen['budget'], {'max_requests': 10})
        c.finish(self.root, reason='Isolated token-policy test; no model requests')

    def test_pause_resume_and_permanent_cancel(self):
        self.initialize(); c.start(self.root, live=True)
        self.assertEqual(c.control(self.root, 'pause')['continuation'], 'paused')
        with self.assertRaisesRegex(c.ComparisonError, 'paused'):
            c.call(self.root, 'telos_send', {})
        self.assertEqual(c.call(self.root, 'telos_status', {})['result']['phase'], 'ready')
        c.control(self.root, 'resume')
        c.control(self.root, 'cancel')
        with self.assertRaisesRegex(c.ComparisonError, 'permanent'):
            c.control(self.root, 'resume')
        self.assertEqual(c.finish(self.root, reason='Cancelled by test')['outcome'], 'cancelled')
        self.assertFalse(c.report(self.root)['comparable'])

    def test_two_experiments_cannot_occupy_the_same_slot(self):
        self.initialize(); c.start(self.root, live=True)
        other = self.base / 'second'
        c.initialize(other, self.spec)
        with self.assertRaisesRegex(c.ComparisonError, 'Another comparison'):
            c.start(other, live=True)
        self.assertFalse((other / 'trials').exists())
        c.finish(self.root, reason='Release the first slot')
        c.start(other, live=True)
        c.finish(other, reason='Release the second slot')

    def test_runtime_and_manifest_tampering_fail_closed(self):
        self.initialize()
        version = self.root / 'versions/baseline/telos.py'
        version.write_text(version.read_text() + '\n# changed\n')
        with self.assertRaisesRegex(c.ComparisonError, 'Frozen runtime'):
            c.start(self.root, live=True)
        with self.assertRaisesRegex(c.ComparisonError, 'Frozen runtime'):
            c.report(self.root)
        manifest = c.read(self.root / 'manifest.json'); manifest['budget']['max_requests'] = 999
        c.write(self.root / 'manifest.json', manifest)
        with self.assertRaisesRegex(c.ComparisonError, 'manifest changed'):
            c.next_trial(self.root)

    def test_fixture_tampering_is_detected_before_work(self):
        self.initialize()
        (self.root / 'sources/clamp-bounds/main.py').write_text('tampered')
        with self.assertRaisesRegex(c.ComparisonError, 'fixture changed'):
            c.start(self.root, live=True)

    def test_evidence_tampering_is_not_reported_as_success(self):
        self.initialize(); c.start(self.root, live=True)
        result = c.finish(self.root, reason='Fixture failed')
        path = self.root / 'results' / (result['trial_id'] + '.json')
        value = c.read(path); value['outcome'] = 'passed'; c.write(path, value)
        with self.assertRaisesRegex(c.ComparisonError, 'evidence changed'):
            c.report(self.root)

    def test_invalid_budget_case_selection_and_inline_keys_are_rejected(self):
        for update in ({'budget': None}, {'budget': {'max_requests': True}}, {'repeats': 0},
                       {'case_ids': ['missing']}, {'case_ids': ['clamp-bounds'] * 2}, {'extra': 1}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                c.initialize(self.root, {**self.spec, **update})
            self.assertFalse(self.root.exists())
        for cfg in ({'api_key': 'fixture-only'}, {'base_url': 'https://name:password@example.test'}):
            c.write(self.cfg, cfg)
            with self.assertRaisesRegex(c.ComparisonError, 'credential|embed'):
                self.initialize()

    def test_existing_directory_is_never_overwritten(self):
        self.root.mkdir(); sentinel = self.root / 'user.txt'; sentinel.write_text('keep')
        with self.assertRaisesRegex(c.ComparisonError, 'preserved'):
            self.initialize()
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_cancel_recovers_a_setup_lease_without_running_any_task(self):
        self.initialize()
        with c.scheduler(self.root):
            pass
        self.assertTrue((self.base / 'scheduler/active.json').exists())
        c.control(self.root, 'cancel')
        self.assertFalse((self.base / 'scheduler/active.json').exists())

    def test_failed_request_with_unresolved_usage_can_close_but_cannot_claim_exact_cost(self):
        self.initialize(); opened = c.start(self.root, live=True)
        trial = opened['trial']
        state_root = self.root / 'trials' / trial['id'] / 'state'
        from usage import UsageLedger
        ledger = UsageLedger(state_root / 'usage' / (opened['session_id'] + '.json'), self.spec['budget'])
        ledger.begin('work')  # Simulate a process dying after a request reservation.
        state_path = state_root / 'runs' / opened['session_id'] / 'state.json'
        state = c.read(state_path); state.update(phase='error', last_error='Worker interrupted')
        c.write(state_path, state)
        result = c.finish(self.root, reason='Worker ended; request usage remains unknown')
        self.assertEqual(result['outcome'], 'failed')
        out = c.report(self.root)
        self.assertIsNone(out['sides']['baseline']['total_tokens'])
        self.assertEqual(out['sides']['baseline']['requests'], 1)
        self.assertFalse((self.base / 'scheduler/active.json').exists())

    def test_normal_running_job_prevents_experiment_launch(self):
        self.patches[0].stop()
        normal = self.base / 'normal'
        c.write(normal / 'runs/fixture/state.json', {'phase': 'running'})
        with patch.dict(os.environ, {'TELOS_STATE_DIR': str(normal)}):
            with self.assertRaisesRegex(c.ComparisonError, 'normal Telos job'):
                c.normal_tasks_idle()
        self.patches[0].start()

    def test_oracle_executes_frozen_checks_instead_of_trusting_acceptance_text(self):
        self.initialize(); opened = c.start(self.root, live=True)
        measured = {'worktree': opened['worktree'], 'commit': c.git(opened['worktree'], 'rev-parse', 'HEAD')}
        case = c.read(self.root / 'manifest.json')['cases'][0]
        oracle = c.outcome_checks(self.root, case, measured)
        self.assertFalse(oracle['passed'])  # untouched clamp implementation is broken
        c.finish(self.root, reason='Independent outcome checks rejected it')


class SummaryTests(unittest.TestCase):
    def fixture(self):
        trials = [{'id': f'{n}-{s}', 'case_id': n, 'repeat': 1, 'side': s}
                  for n in ('a', 'b') for s in c.SIDES]
        results = {}
        for t in trials:
            results[t['id']] = {'outcome': 'passed', 'started_at': 1, 'finished_at': 3,
                'oracle': None, 'measurement': {'provider_seconds': 0.5, 'usage': {
                    'totals': {'requests': 2, 'total_tokens': 100}, 'unknown_fields': {'total_tokens': 0}}}}
        return {'trials': trials}, results

    def test_failed_attempts_remain_in_cost_and_paired_regressions(self):
        manifest, results = self.fixture()
        results['a-candidate']['outcome'] = 'failed'
        out = c.summarize(manifest, results)
        self.assertTrue(out['comparable'])
        self.assertEqual(out['sides']['candidate']['success_rate'], 0.5)
        self.assertEqual(out['sides']['candidate']['tokens_per_success'], 200)
        self.assertEqual(out['regressions'], [{'case_id': 'a', 'repeat': 1}])
        self.assertEqual(out['candidate_minus_baseline_tokens'], 0)
        self.assertFalse(out['automatic_promotion'])

    def test_unknown_usage_is_not_zero_or_a_saving(self):
        manifest, results = self.fixture()
        results['a-candidate']['measurement']['usage']['unknown_fields']['total_tokens'] = 1
        out = c.summarize(manifest, results)
        self.assertIsNone(out['sides']['candidate']['total_tokens'])
        self.assertEqual(out['sides']['candidate']['observed_total_tokens'], 200)
        self.assertIsNone(out['sides']['candidate']['tokens_per_success'])
        self.assertIsNone(out['candidate_minus_baseline_tokens'])

    def test_missing_or_cancelled_trials_cannot_win_a_comparison(self):
        manifest, results = self.fixture(); results.pop('a-candidate')
        self.assertFalse(c.summarize(manifest, results)['comparable'])
        self.assertEqual(c.summarize(manifest, results)['sides']['candidate']['success_rate'], 0.5)
        manifest, results = self.fixture(); results['a-candidate']['outcome'] = 'cancelled'
        self.assertFalse(c.summarize(manifest, results)['comparable'])

    def test_audit_oracle_counts_missed_and_invented_findings(self):
        measured = {'evidence': {'audit': {'report': {'findings': [
            {'id': 'f1', 'check_id': 'c2', 'category': 'defect'},
            {'id': 'f2', 'check_id': 'c3', 'category': 'suggestion'}],
            'results': []}, 'screenshots': []}}, 'audit_review': {
                'findings': [{'id': 'f1', 'disposition': 'confirmed', 'rationale': 'Confirmed'},
                             {'id': 'f2', 'disposition': 'suggestion', 'rationale': 'Optional'}],
                'limitations': [], 'visual_evidence_reviewed': [], 'coverage_rationale': 'All source inspected'}}
        out = c.outcome_checks(None, {'audit': {}, 'expected_findings': ['c1']}, measured)
        self.assertEqual(out, {'passed': False, 'true_positive': 0, 'false_positive': 1, 'false_negative': 1})

        measured['audit_review']['findings'][0]['disposition'] = 'dismissed'
        out = c.outcome_checks(None, {'audit': {}, 'expected_findings': ['c1']}, measured)
        self.assertEqual(out['false_positive'], 0)
        self.assertEqual(out['false_negative'], 1)
        measured.pop('audit_review')
        with self.assertRaisesRegex(c.ComparisonError, 'supervisor finding decisions'):
            c.outcome_checks(None, {'audit': {}, 'expected_findings': ['c1']}, measured)


class SuiteTests(unittest.TestCase):
    def test_twenty_distinct_public_cases_with_real_failing_seeds(self):
        cases = evaluation_suite.cases(); c.validate_cases(cases)
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({x['id'] for x in cases}), 20)
        self.assertTrue(all(x['split'] == 'development' for x in cases))
        with tempfile.TemporaryDirectory() as folder:
            for case in cases:
                if 'audit' in case:
                    continue
                root = Path(folder) / case['id']; root.mkdir()
                (root / 'main.py').write_text(case['files']['main.py'])
                result = subprocess.run([sys.executable, '-c', case['checks'][0]['argv'][2]],
                                        cwd=root, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, case['id'])

    def test_each_implementation_oracle_has_a_valid_reference_solution(self):
        # Validate the benchmark itself: a malformed/impossible oracle must not
        # make both runtime versions fail a supposedly solvable task.
        solutions = {
            'clamp-bounds': 'def clamp(v,lo,hi):\n if lo>hi: raise ValueError()\n return min(hi,max(lo,v))',
            'median-even': 'def median(v):\n if not v: raise ValueError()\n s=sorted(v); n=len(s); return s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2',
            'stable-deduplicate': 'def unique(v): return list(dict.fromkeys(v))',
            'chunk-remainder': 'def chunks(v,n):\n if n<=0: raise ValueError()\n return [v[i:i+n] for i in range(0,len(v),n)]',
            'boolean-parser': 'def parse_bool(s):\n s=s.strip().lower()\n if s in ("true","1","yes"): return True\n if s in ("false","0","no"): return False\n raise ValueError()',
            'merge-intervals': 'def merge_intervals(v):\n out=[]\n for a,b in sorted(v):\n  if out and a<=out[-1][1]: out[-1][1]=max(out[-1][1],b)\n  else: out.append([a,b])\n return out',
            'zero-based-pagination': 'def page(v,n,s):\n if n<0 or s<=0: raise ValueError()\n return v[n*s:(n+1)*s]',
            'quoted-csv': 'import csv\ndef parse_row(s): return next(csv.reader([s]))',
            'decimal-money': 'from decimal import Decimal,ROUND_HALF_UP\ndef discounted(p,r): return str((Decimal(p)*(1-Decimal(r)/100)).quantize(Decimal(".01"),rounding=ROUND_HALF_UP))',
            'nested-redaction': 'def redact(v):\n if isinstance(v,dict): return {k:"***" if isinstance(k,str) and k.lower() in ("password","token") else redact(x) for k,x in v.items()}\n if isinstance(v,list): return [redact(x) for x in v]\n return v',
            'sliding-window': 'def windows(v,n):\n if n<=0: raise ValueError()\n return [v[i:i+n] for i in range(len(v)-n+1)]',
            'frequency-ties': 'from collections import Counter\ndef most_common(v): return Counter(v).most_common(1)[0][0] if v else None',
            'bounded-backoff': 'def delays(n,b,c):\n if min(n,b,c)<0: raise ValueError()\n return [min(b*2**i,c) for i in range(n)]',
            'nested-lookup': 'def get_path(d,keys,default=None):\n for k in keys:\n  if not isinstance(d,dict) or k not in d: return default\n  d=d[k]\n return d',
            'idempotent-events': 'def total_once(events):\n seen=set(); total=0\n for e in events:\n  if e["id"] not in seen: total+=e["amount"]; seen.add(e["id"])\n return total',
            'form-required-fields': 'def missing_fields(f,r): return [k for k in r if f.get(k) is None or isinstance(f[k],str) and not f[k].strip()]',
        }
        with tempfile.TemporaryDirectory() as folder:
            for case in evaluation_suite.cases():
                if 'audit' in case:
                    continue
                root = Path(folder) / case['id']; root.mkdir()
                (root / 'main.py').write_text(solutions[case['id']] + '\n')
                result = subprocess.run([sys.executable, '-c', case['checks'][0]['argv'][2]],
                                        cwd=root, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, case['id'] + ': ' + result.stderr)


class ComparisonWorkflowTests(ComparisonTests):
    def wait(self, phase):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = c.call(self.root, 'telos_status', {'detail': 'full'})['result']
            if state['phase'] == phase:
                return state
            if state['phase'] not in ('running', 'stale'):
                self.fail(str(state))
            time.sleep(.3)
        self.fail('Comparison workflow timed out')

    def test_real_cli_work_exam_rejected_grade_repair_and_acceptance_for_both_sides(self):
        fixed = 'def clamp(value, low, high):\n    if low > high: raise ValueError("bounds")\n    return min(high, max(low, value))\n'
        command = 'python3 -c ' + shlex.quote('from pathlib import Path; Path("main.py").write_text(' + repr(fixed) + ')')
        answers = json.dumps({'answers': [{'id': 'behavior', 'answer': 'Clamp between low and high; reversed bounds raise ValueError.'},
                                          {'id': 'evidence', 'answer': 'Changed main.py; the work command wrote the function.'}]})
        def reply(request, _):
            calls = None
            if request.get('tool_choice') == 'none':
                text = answers
            elif request['messages'][-1]['role'] == 'tool':
                text = json.dumps({'status': 'completed', 'summary': 'Implementation complete.', 'changed_files': [], 'checks_performed': [], 'remaining_issues': [], 'clarification_questions': []})
            else:
                names = [t['function']['name'] for t in request['tools']]
                name = 'shell_command' if 'shell_command' in names else 'exec_command'
                arguments = {'command': command} if name == 'shell_command' else {'cmd': command}
                calls = [{'id': 'call_' + str(len(request['messages'])), 'type': 'function',
                          'function': {'name': name, 'arguments': json.dumps(arguments)}}]
                text = None
            value = completion(text, calls)
            value['usage'] = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
            return 200, {'Content-Type': 'application/json'}, json.dumps(value).encode()
        with server(reply) as (port, seen):
            cfg = config(); cfg['local_executor']['endpoints']['one'] = spec(port)
            cfg.update(job_timeout=50, http_timeout=30)
            c.write(self.cfg, cfg)
            self.initialize()
            for _ in c.SIDES:
                opened = c.start(self.root, live=True)
                c.call(self.root, 'telos_send', {})
                first = self.wait('responded')
                self.assertFalse(first['accepted'])
                c.call(self.root, 'telos_evaluate', {})
                state = self.wait('awaiting_review')
                review = c.call(self.root, 'telos_review', {'detail': 'full'})['result']
                exam = review['evidence']['exam']
                self.assertEqual(exam['executor_thread_id'], first['last_result']['thread_id'])
                self.assertEqual(exam['tool_call_count'], 0)
                self.assertTrue(all(x['status'] == 'passed' for x in review['evidence']['checks']))
                # Deterministic supervisor-boundary fixture: deliberately reject
                # the first questionnaire, then require a real repair/exam cycle.
                scores = [{'id': q['id'], 'points': q['points'], 'rationale': 'Checked against pinned fixture source.'}
                          for q in review['questions']]
                rejected = c.call(self.root, 'telos_grade', {'packet_id': state['packet_id'],
                    'scores': scores, 'blockers': ['Repeat the public behavior verification.']})['result']
                self.assertFalse(rejected['accepted'])
                c.call(self.root, 'telos_repair', {'feedback': {'symptoms': ['Verify the public clamp requirements again.']}})
                self.wait('responded')
                c.call(self.root, 'telos_evaluate', {})
                state = self.wait('awaiting_review')
                c.call(self.root, 'telos_review', {})
                accepted = c.call(self.root, 'telos_grade', {'packet_id': state['packet_id'],
                    'scores': scores, 'blockers': []})['result']
                self.assertTrue(accepted['accepted'])
                out = c.finish(self.root)
                self.assertEqual(out['outcome'], 'passed')
                self.assertTrue(out['oracle']['passed'])
            out = c.report(self.root)
            self.assertTrue(out['complete']); self.assertTrue(out['comparable'])
            self.assertEqual(out['sides']['baseline']['requests'], 6)
            self.assertEqual(out['sides']['candidate']['total_tokens'], 720)
            self.assertEqual(out['candidate_minus_baseline_tokens'], 0)
            self.assertEqual(len(seen), 12)
            for private in evaluation_suite.cases()[0]['questions']:
                self.assertNotIn(private['reference'], json.dumps(seen))
            self.assertNotIn(evaluation_suite.cases()[0]['checks'][0]['argv'][2], json.dumps(seen))

    def test_accepted_audit_preserves_supervisor_decisions_for_independent_oracle(self):
        answers = json.dumps({'answers': [
            {'id': 'findings', 'answer': 'c1 is defective: service.py:3 uses number instead of number * size. c2 validates inputs; c3 uses a nonmutating slice.'},
            {'id': 'boundary', 'answer': 'Source inspection only; no browser, HTTP or production checks.'}]})
        command = ''

        def reply(request, _):
            if request.get('tool_choice') == 'none':
                value = completion(answers)
            elif request['messages'][-1]['role'] == 'tool':
                value = completion('Source audit evidence written; source unchanged.')
            else:
                names = [t['function']['name'] for t in request['tools']]
                name = 'shell_command' if 'shell_command' in names else 'exec_command'
                args = {'command': command} if name == 'shell_command' else {'cmd': command}
                value = completion(None, [{'id': 'audit_write', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}])
            value['usage'] = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
            return 200, {'Content-Type': 'application/json'}, json.dumps(value).encode()

        with server(reply) as (port, seen):
            cfg = config(); cfg['local_executor']['endpoints']['one'] = spec(port)
            c.write(self.cfg, cfg)
            self.spec['case_ids'] = ['audit-pagination']
            self.initialize()
            for _ in c.SIDES:
                opened = c.start(self.root, live=True)
                script = '''import os, json
from pathlib import Path
p = Path(os.environ['TELOS_AUDIT_ARTIFACTS'])
r = json.loads((p / 'report.json').read_text())
(p / 'source.txt').write_text(Path(SOURCE_FILE).read_text())
r['results'] = [{'id': ident, 'status': status, 'actual': actual,
    'steps': ['Inspect service.py'], 'evidence': ['source.txt']}
    for ident, status, actual in [('c1', 'finding', 'Uses page number as offset'),
    ('c2', 'passed', 'Rejects invalid inputs'), ('c3', 'passed', 'Slice preserves input')]]
r['findings'] = [{'id': 'F1', 'check_id': 'c1', 'category': 'defect',
    'severity': 'medium', 'title': 'Incorrect page offset', 'location': 'service.py:3',
    'expected': 'Offset is number * size', 'actual': 'Offset is number',
    'steps': ['Inspect service.py:3'], 'evidence': ['source.txt']}]
(p / 'report.json').write_text(json.dumps(r))
'''.replace('SOURCE_FILE', repr(str(Path(opened['worktree']) / 'service.py')))
                command = 'python3 -c ' + shlex.quote(script)
                c.call(self.root, 'telos_send', {})
                self.wait('responded')
                c.call(self.root, 'telos_evaluate', {})
                state = self.wait('awaiting_review')
                review = c.call(self.root, 'telos_review', {'detail': 'full'})['result']
                self.assertTrue(review['evidence']['exam']['same_executor'])
                self.assertEqual(review['evidence']['exam']['tool_call_count'], 0)
                decisions = {'findings': [{'id': 'F1', 'disposition': 'confirmed',
                    'rationale': 'Fixture source has the seeded offset defect.'}],
                    'limitations': [], 'visual_evidence_reviewed': [],
                    'coverage_rationale': 'All three source checklist items have captured evidence.'}
                accepted = c.call(self.root, 'telos_grade', {'packet_id': state['packet_id'],
                    'scores': [{'id': q['id'], 'points': q['points'], 'rationale': 'Deterministic audit fixture review.'}
                               for q in review['questions']], 'blockers': [], 'audit_review': decisions})['result']
                self.assertTrue(accepted['accepted'])
                result = c.finish(self.root)
                self.assertEqual(result['outcome'], 'passed')
                self.assertEqual(result['oracle'], {'passed': True, 'true_positive': 1,
                    'false_positive': 0, 'false_negative': 0})
            out = c.report(self.root)
            self.assertTrue(out['comparable'])
            self.assertEqual(out['sides']['candidate']['audit_oracles_scored'], 1)
            self.assertEqual(out['sides']['candidate']['true_positive'], 1)
            self.assertEqual(len(seen), 6)


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for cls in (ComparisonTests, SummaryTests, SuiteTests, ComparisonWorkflowTests):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

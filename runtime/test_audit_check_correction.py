"""Fixed audit-status correction, using only temporary synthetic contracts."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import audit
import audit_check_correction as correction
import test_audit
from test_audit import boundary
from test_callbacks import FakeRPC, OWNER, OTHER
import test_projects
import test_telos
from test_telos import b, synthetic_exam


PROGRAM = ("import json,os\nfrom pathlib import Path\n"
           "report=json.loads(Path(os.environ['TELOS_AUDIT_ARTIFACTS'],'report.json').read_text())\n"
           "assert all(x['status'] in ('pass','finding') for x in report['results'])\n")
CHECKS = [{'id': 'quality', 'argv': ['{python}', '-c', PROGRAM]}]


class ASTTests(unittest.TestCase):
    def contract(self, program, **kwargs):
        return {'task_type': 'audit', 'checks': [{'id': 'quality', 'argv': ['{python}', '-c', program]}],
                'threshold': 80, 'questions': [{'reference': 'private reference'}], **kwargs}

    def test_changes_only_supported_literal_preserving_other_bytes(self):
        for literal in ("'pass'", '"pass"', "u'pass'", "'pa' 'ss'"):
            for container in ("(%s,'finding')", "[%s,'blocked']", "{%s,'not_tested'}"):
                program = "title='中文'; assert x['status'] in " + (container % literal) + "  # pass\n"
                original = self.contract(program)
                saved = copy.deepcopy(original)
                fixed, changes = correction.corrected_contract(original)
                expected = program.replace(literal, "'passed'")
                with self.subTest(program=program):
                    self.assertEqual(fixed['checks'][0]['argv'][2], expected)
                    self.assertEqual(original, saved)
                    fixed['checks'] = saved['checks']
                    self.assertEqual(fixed, saved)
                    self.assertEqual(changes, [{'check_id': 'quality', 'before_program': program,
                                               'after_program': expected, 'replacements': 1}])

    def test_multiple_checks_and_multiline_offsets(self):
        program = "# 中文\nassert x[\n 'status'\n] in (\n 'pass', 'finding',\n)\nassert y['status'] in ['pass']\n"
        original = self.contract(program)
        original['checks'].append({'id': 'other', 'argv': ['{python}', '-c', "assert r['status'] in {'pass'}"]})
        fixed, changes = correction.corrected_contract(original)
        self.assertEqual([r['replacements'] for r in changes], [2, 1])
        self.assertEqual(fixed['checks'][0]['argv'][2], program.replace("'pass'", "'passed'"))

    def test_unsupported_expressions_and_noop_are_rejected(self):
        for expr in ("x['status'] not in ('pass','finding')", "x['other'] in ['pass']",
                     "x.status in ['pass']", "x['status'] in statuses", "x['status'] in ['pass', extra]",
                     "x['status'] in ['pass','anything']", "x['status'] in ['pass','passed']",
                     "x['status'] in ['pass','pass']", "x['status'] in ['passed','finding']",
                     "x['status'] in ['pass', 1]", "x['status'] == 'pass'",
                     "x['status'] in ['pass'] == y", "'pass' in ['finding']"):
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                correction.corrected_contract(self.contract('assert ' + expr))
        with self.assertRaises(ValueError):
            correction.corrected_contract(self.contract("assert x['status'] in ['pass']", task_type='implementation'))
        with self.assertRaises(ValueError):
            correction.corrected_contract(self.contract('', checks=[{'id': 'shell', 'argv': ['sh', '-c', 'true']}]))


class CorrectionTests(unittest.TestCase):
    tearDown = test_telos.BridgeTests.tearDown
    scores = test_telos.BridgeTests.scores
    data = test_audit.AuditTests.data
    report = test_audit.AuditTests.report
    review = test_audit.AuditTests.review

    def setUp(self):
        test_telos.BridgeTests.setUp(self)
        self.db = {'thread': {'id': OWNER, 'status': {'type': 'idle'}, 'cwd': str(self.repo)},
                   'queue': [], 'calls': []}
        for item in (patch.object(b, 'CodexRPC', side_effect=lambda *_: FakeRPC(self.db)),
                     patch.object(b, 'spawn_dispatch')):
            item.start()
            self.patches.append(item)

    def create(self, **kwargs):
        args = dict(repo=str(self.repo), task='Audit integer addition', questions=self.q,
                    checks=CHECKS, task_type='audit', audit=boundary(), notify=True, owner_thread_id=OWNER)
        args.update(kwargs)
        sid = b.create_session(**args)['session_id']
        self.report(sid)
        state, _ = self.data(sid)
        state.update(phase='responded', thread_id=OTHER, job_id='a' * 32, job_kind='send',
                     turns=1, last_result={'summary': 'Evidence submitted'})
        b.record_event(state)
        b.write_json(b.run_dir(sid) / 'state.json', state)
        self.assertTrue(b.receive_event(sid, state['last_event_id'])['proceed'])
        return sid

    def arguments(self, sid, **changes):
        state, _ = self.data(sid)
        return dict(session_id=sid, request_id='fix-status-v1', reason='Correct a historical audit enum spelling',
                    user_authorization='Approved scoped runtime correction preserving original criteria',
                    event_id=state['last_event_id'], **changes)

    def correct(self, sid, args=None, caller=OWNER):
        return b.call_tool('telos_correct_audit_status', args or self.arguments(sid), {'threadId': caller})

    def evaluate(self, sid):
        state, _ = self.data(sid)
        eid = state['last_event_id']
        real_popen = subprocess.Popen
        def launch(argv, **kwargs):
            return Mock(pid=os.getpid()) if '_job' in argv else real_popen(argv, **kwargs)
        with patch.object(b.subprocess, 'Popen', side_effect=launch):
            job = b.start_job(sid, 'evaluate', event_id=eid)['job_id']
        with patch.object(b, 'closed_book_exam', side_effect=synthetic_exam):
            b.background_job(sid, job)
        state, _ = self.data(sid)
        self.assertTrue(b.receive_event(sid, state['last_event_id'])['proceed'])
        return self.data(sid)[0]

    def save(self, sid, state):
        b.write_json(b.run_dir(sid) / 'state.json', state)

    def test_failing_check_correction_fresh_exam_and_acceptance(self):
        sid = self.create()
        state = self.evaluate(sid)
        root = b.run_dir(sid)
        self.assertEqual(state['phase'], 'not_accepted')
        self.assertFalse(state['last_result']['exam_received'])
        old_contract = (root / 'contract.json').read_bytes()
        old_attempt = (root / 'attempt-1.json').read_bytes()
        old_report = (audit.artifacts(b, state) / 'report.json').read_bytes()
        old_packet = state['packet_id']
        old_hash = state['contract_hash']
        old_thread = state['thread_id']
        args = self.arguments(sid)
        reply = self.correct(sid, args)
        self.assertFalse(reply['accepted'])
        self.assertEqual(reply['next_action'], 'telos_evaluate')
        state, effective = self.data(sid)
        self.assertEqual(state['rounds'], 1)
        self.assertEqual(state['turns'], 1)
        self.assertNotIn('packet_id', state)
        self.assertNotEqual(old_hash, state['contract_hash'])
        self.assertEqual(state['contract_hash'], b.digest(effective))
        self.assertEqual((root / 'contract.json').read_bytes(), old_contract)
        self.assertEqual((root / 'attempt-1.json').read_bytes(), old_attempt)
        with self.assertRaises(b.GateError):
            b.grade(sid, old_packet, self.scores(), [], event_id=args['event_id'])
        with self.assertRaises(b.GateError):
            b.review_packet(sid)
        status = b.status(sid, detail='summary')
        self.assertEqual(status['next_action'], 'telos_evaluate')
        self.assertEqual(status['audit_check_correction']['original_contract_hash'], old_hash)
        self.assertNotIn(PROGRAM, json.dumps(status['audit_check_correction']))
        state = self.evaluate(sid)
        self.assertEqual(state['phase'], 'awaiting_review', state.get('last_error'))
        self.assertEqual(state['rounds'], 2)
        self.assertEqual(state['thread_id'], old_thread)
        packet = b.review_packet(sid, detail='summary')
        self.assertEqual(packet['contract_hash'], state['contract_hash'])
        self.assertEqual(packet['identity']['executor_thread_id'], old_thread)
        self.assertEqual(packet['audit_check_correction']['effective_contract_hash'], state['contract_hash'])
        result = b.grade(sid, state['packet_id'], self.scores(), [], audit_review=self.review(sid),
                         event_id=state['last_event_id'])
        self.assertTrue(result['accepted'], result)
        self.assertTrue(b.status(sid)['accepted'])
        self.assertEqual((root / 'contract.json').read_bytes(), old_contract)
        self.assertEqual((root / 'attempt-1.json').read_bytes(), old_attempt)
        self.assertEqual((audit.artifacts(b, state) / 'report.json').read_bytes(), old_report)
        with self.assertRaisesRegex(b.GateError, 'Accepted'):
            self.correct(sid, args)

    def test_correction_does_not_accept_valid_finding_without_review(self):
        sid = self.create()
        self.report(sid, finding=True)
        self.correct(sid)
        state = self.evaluate(sid)
        with self.assertRaises(b.GateError):
            b.grade(sid, state['packet_id'], self.scores(), [], audit_review=self.review(sid),
                    event_id=state['last_event_id'])
        decisions = [{'id': 'F1', 'disposition': 'confirmed', 'rationale': 'Checked isolated fixture evidence'}]
        result = b.grade(sid, state['packet_id'], self.scores(), [], audit_review=self.review(sid, decisions),
                         event_id=state['last_event_id'])
        self.assertTrue(result['accepted'])
        self.assertEqual(result['system_health'], 'not_asserted')

    def test_owner_and_schema_cannot_be_overridden(self):
        sid = self.create()
        args = self.arguments(sid)
        for caller in (None, OTHER):
            with self.subTest(caller=caller), self.assertRaises(b.GateError):
                self.correct(sid, args, caller)
        with self.assertRaises(b.GateError):
            self.correct(sid, {**args, 'caller': OWNER})
        state, _ = self.data(sid)
        state['owner'] = None
        self.save(sid, state)
        with self.assertRaises(b.GateError):
            self.correct(sid, args)
        tool = next(t for t in b.TOOLS if t['name'] == 'telos_correct_audit_status')
        self.assertEqual(set(tool['inputSchema']['properties']), set(args))

    def test_invalid_phase_control_event_and_auto_check_guards(self):
        sid = self.create()
        state, _ = self.data(sid)
        args = self.arguments(sid)
        changes = [{'phase': x} for x in ('running', 'created', 'awaiting_review', 'accepted', 'error')]
        changes += [{'continuation': x} for x in ('paused', 'cancelled')]
        changes += [{'archived': {'path': 'retained'}}, {'clarification': {'questions': ['why']}},
                    {'last_result': {'exam_received': True}}, {'acceptance': {'accepted': True}},
                    {'auto_check': {'status': 'queued'}}, {'auto_check': {'status': 'running'}}]
        for change in changes:
            modified = {**state, **change}
            self.save(sid, modified)
            before = (b.run_dir(sid) / 'state.json').read_bytes()
            with self.subTest(change=change), self.assertRaises(b.GateError):
                self.correct(sid, args)
            self.assertEqual((b.run_dir(sid) / 'state.json').read_bytes(), before)
        self.save(sid, state)
        unreceived = copy.deepcopy(state)
        unreceived['events'][args['event_id']]['status'] = 'pending'
        self.save(sid, unreceived)
        with self.assertRaisesRegex(b.GateError, 'not received'):
            self.correct(sid, args)
        self.save(sid, state)
        for change in ({'event_id': 'unknown'}, {'event_id': ' '}, {'request_id': ' spaced '},
                       {'reason': ''}, {'user_authorization': ''}, {'reason': self.q[0]['reference']}):
            with self.subTest(change=change), self.assertRaises(b.GateError):
                self.correct(sid, {**args, **change})

    def test_trusted_supervisor_policy_is_reverified_before_correction(self):
        sid = self.create()
        args = self.arguments(sid)
        path = b.run_dir(sid) / 'state.json'
        before = path.read_bytes()
        b.write_json(b.CONFIG, {'supervisor': {'provider': 'openai', 'models': ['gpt-fixture']}})
        with self.assertRaises(b.GateError):
            self.correct(sid, args)
        self.assertEqual(path.read_bytes(), before)
        self.db['thread'].update(model='gpt-fixture', modelProvider='openai')
        self.assertFalse(self.correct(sid, args)['accepted'])

    def test_idempotency_and_preserved_counters_usage_history_and_controls(self):
        sid = self.create()
        state, _ = self.data(sid)
        state.update(rounds=2, turns=7, infra_failures=1, auto_check={'status': 'failed', 'id': 'old'},
                     auto_check_history={'old': {'status': 'failed'}},
                     progress_guard={'version': 1, 'streams': {'work': {'streak': 3, 'seen': []}},
                                     'plans': [], 'observed': [], 'replans_without_progress': 0})
        self.save(sid, state)
        root = b.run_dir(sid)
        (root / 'usage.jsonl').write_text('{"fixture":"retained"}\n')
        usage = (root / 'usage.jsonl').read_bytes()
        args = self.arguments(sid)
        self.correct(sid, args)
        after, _ = self.data(sid)
        for key in ('rounds', 'turns', 'infra_failures', 'progress_guard', 'auto_check_history',
                    'thread_id', 'owner', 'model', 'continuation'):
            self.assertEqual(after.get(key), state.get(key), key)
        self.assertEqual((root / 'usage.jsonl').read_bytes(), usage)
        self.assertNotIn('auto_check', after)
        self.assertEqual(after[correction.KEY]['prior_evidence']['auto_check'], state['auto_check'])
        before = (root / 'state.json').read_bytes()
        mtime = (root / 'state.json').stat().st_mtime_ns
        self.assertTrue(self.correct(sid, args)['duplicate'])
        self.assertEqual((root / 'state.json').read_bytes(), before)
        self.assertEqual((root / 'state.json').stat().st_mtime_ns, mtime)
        for key in ('request_id', 'reason', 'user_authorization', 'event_id'):
            with self.subTest(key=key), self.assertRaises(b.GateError):
                self.correct(sid, {**args, key: 'changed'})
        with patch.object(b.subprocess, 'Popen') as spawn, self.assertRaises(b.GateError):
            b.start_job(sid, 'evaluate', event_id=args['event_id'])
        spawn.assert_not_called()

    def test_no_match_refuses_without_writing(self):
        checks = copy.deepcopy(CHECKS)
        checks[0]['argv'][2] = PROGRAM.replace("'pass'", "'passed'")
        sid = self.create(checks=checks)
        path = b.run_dir(sid) / 'state.json'
        before = path.read_bytes()
        with self.assertRaisesRegex(b.GateError, 'No supported'):
            self.correct(sid)
        self.assertEqual(path.read_bytes(), before)

    def test_round_limit_still_blocks_after_correction(self):
        sid = self.create(max_rounds=1)
        state = self.evaluate(sid)
        args = self.arguments(sid)
        self.correct(sid, args)
        self.assertEqual(self.data(sid)[0]['rounds'], 1)
        with patch.object(b.subprocess, 'Popen') as spawn, self.assertRaisesRegex(b.GateError, 'Maximum exam rounds'):
            b.start_job(sid, 'evaluate', event_id=args['event_id'])
        spawn.assert_not_called()

    def test_damaged_records_fail_closed_even_with_recomputed_record_digest(self):
        sid = self.create()
        self.correct(sid)
        state, contract = self.data(sid)
        row = state[correction.KEY]
        variants = [None, {**row, 'extra': True}, {k: v for k, v in row.items() if k != 'changes'}]
        for key, value in (('version', True), ('kind', 'arbitrary'), ('session_id', 'other'),
                           ('owner_thread_id', OTHER), ('original_contract_hash', '0' * 64),
                           ('effective_contract_hash', '0' * 64), ('record_hash', '0' * 64),
                           ('arguments_hash', '0' * 64), ('prior_evidence', []),
                           ('created_at', float('inf')), ('changes', [])):
            variants.append({**row, key: value})
        changed = copy.deepcopy(row)
        changed['changes'][0]['after_program'] += '\nassert True'
        variants.append(changed)
        changed = copy.deepcopy(row)
        changed['arguments']['event_id'] = ''
        changed['arguments_hash'] = b.digest(changed['arguments'])
        variants.append(changed)
        for variant in variants:
            for recalculate in (False, True):
                bad = copy.deepcopy(variant)
                if recalculate and isinstance(bad, dict) and 'record_hash' in bad:
                    bad['record_hash'] = b.digest({k: v for k, v in bad.items() if k != 'record_hash'})
                # A digest mismatch alone is intentional; recomputing that exact
                # one field produces the original valid record, not corruption.
                if bad == row:
                    continue
                self.save(sid, {**state, correction.KEY: bad})
                with self.subTest(record=variant, recalculate=recalculate), self.assertRaises(b.GateError):
                    self.data(sid)
        self.save(sid, state)
        self.assertEqual(self.data(sid)[1], contract)
        self.save(sid, {**state, 'contract_hash': row['original_contract_hash']})
        with self.assertRaises(b.GateError):
            self.data(sid)

    def test_legacy_contract_integrity_still_enforced(self):
        sid = self.create()
        state, contract = self.data(sid)
        self.assertNotIn(correction.KEY, state)
        contract['threshold'] = 1
        b.write_json(b.run_dir(sid) / 'contract.json', contract)
        with self.assertRaises(b.GateError):
            self.data(sid)


class ProjectCorrectionTests(unittest.TestCase):
    setUp = CorrectionTests.setUp
    tearDown = test_telos.BridgeTests.tearDown
    task = test_projects.ProjectTests.task
    start = test_projects.ProjectTests.start
    data = test_audit.AuditTests.data
    report = test_audit.AuditTests.report
    arguments = CorrectionTests.arguments
    correct = CorrectionTests.correct
    evaluate = CorrectionTests.evaluate
    review = test_audit.AuditTests.review
    scores = test_telos.BridgeTests.scores
    state = test_projects.ProjectTests.state
    check_sync = test_projects.ProjectTests.check_sync

    def create(self):
        task = self.task('audit', task_type='audit', audit=boundary(), checks=CHECKS)
        task['allowed_paths'] = []
        manager = b.project_manager()
        pid = manager.create(repo=str(self.repo), task='Audit fixture project', tasks=[task], questions=self.q,
                             checks=self.checks, decision_reason='Independent read-only audit',
                             strategy='single', notify=True, caller=OWNER)['project_id']
        manager.budget(pid, 'unlimited', 'Retain approved unlimited policy', 'Synthetic user approval', caller=OWNER)
        self.start(pid)
        project = manager.load(pid)
        sid = project['tasks'][0]['session_id']
        self.report(sid)
        state, _ = self.data(sid)
        state.update(phase='responded', thread_id=OTHER, last_result={'summary': 'Audit complete'})
        b.record_event(state)
        b.write_json(b.run_dir(sid) / 'state.json', state)
        self.assertTrue(b.receive_event(sid, state['last_event_id'])['proceed'])
        return pid, sid

    def test_original_project_and_child_metadata_and_budget_are_unchanged(self):
        pid, sid = self.create()
        manager = b.project_manager()
        before = manager.load(pid)
        before_bytes = manager.path(pid).read_bytes()
        self.correct(sid)
        self.assertEqual(manager.path(pid).read_bytes(), before_bytes)
        status = manager.status(pid, detail='summary')
        task = status['tasks'][0]
        self.assertEqual(task['audit_check_correction']['kind'], correction.KIND)
        after = manager.load(pid)
        for key in ('contract_hash', 'contract', 'jobs_started', 'max_jobs', 'job_budget_policy', 'history'):
            self.assertEqual(after[key], before[key], key)
        for key in ('id', 'session_id', 'checks', 'questions', 'budget', 'usage_id', 'executor'):
            self.assertEqual(after['tasks'][0][key], before['tasks'][0][key], key)

    def test_project_control_and_owner_remain_authoritative(self):
        pid, sid = self.create()
        manager = b.project_manager()
        before = manager.load(pid)
        for changes in ({'continuation': 'paused'}, {'continuation': 'cancelled'}, {'phase': 'accepted'},
                        {'owner': {**before['owner'], 'thread_id': OTHER}}):
            manager.save({**before, **changes})
            with self.subTest(changes=changes), self.assertRaises(b.GateError):
                self.correct(sid)
        manager.save(before)
        self.assertFalse(self.correct(sid)['accepted'])

    def test_project_final_checks_and_review_bind_effective_child_contract(self):
        pid, sid = self.create()
        self.evaluate(sid)
        old_hash = self.data(sid)[0]['contract_hash']
        self.correct(sid)
        state = self.evaluate(sid)
        self.assertNotEqual(state['contract_hash'], old_hash)
        self.assertTrue(b.grade(sid, state['packet_id'], self.scores(), [],
                               audit_review=self.review(sid), event_id=state['last_event_id'])['accepted'])
        manager = b.project_manager()
        manager.integrate(pid)
        review = self.check_sync(pid)
        packet = review['evidence']
        self.assertEqual(packet['dependency_contracts']['audit']['contract_hash'], state['contract_hash'])
        self.assertEqual(packet['deliveries']['audit']['packet_id'], state['packet_id'])
        self.assertEqual(packet['audit_check_corrections']['audit']['original_contract_hash'], old_hash)
        summary = manager.review(pid, detail='summary')['evidence']
        self.assertEqual(summary['audit_check_corrections'], packet['audit_check_corrections'])
        self.assertEqual(summary['dependency_contracts'], packet['dependency_contracts'])
        self.assertTrue(manager.finish(pid, review['packet_id'], self.scores(), [],
                                      'Inspected retained evidence and fresh corrected audit checks')['accepted'])


if __name__ == '__main__':
    unittest.main()

"""MCP-boundary detail defaults, frozen budgets and budget-stop regressions."""
import copy
import os
import unittest
from unittest.mock import Mock, patch

import test_telos
from usage import BudgetExceeded

b = test_telos.b


class MCPBoundaryTests(unittest.TestCase):
    setUp = test_telos.BridgeTests.setUp
    tearDown = test_telos.BridgeTests.tearDown
    create = test_telos.BridgeTests.create
    ready_review = test_telos.BridgeTests.ready_review
    scores = test_telos.BridgeTests.scores

    def open_with_budget(self, budget=None):
        args = {'repo': str(self.repo), 'task': 'Implement addition.', 'questions': self.q,
                'checks': self.checks, 'notify': False}
        if budget is not None:
            args['budget'] = budget
        return b.call_tool('telos_open', args, None)

    def test_status_and_review_default_to_summary_and_full_preserves_pinned_evidence(self):
        sid = self.create()
        self.ready_review(sid)
        root = b.run_dir(sid)
        state_before = (root / 'state.json').read_bytes()
        packet_before = (root / 'attempt-1.json').read_bytes()

        summary = b.call_tool('telos_status', {'session_id': sid})
        full = b.call_tool('telos_status', {'session_id': sid, 'detail': 'full'})
        self.assertEqual(summary['detail'], 'summary')
        self.assertIn('detail="full"', summary['detail_hint'])
        self.assertEqual(full['detail'], 'full')
        for key in ('session_id', 'phase', 'packet_id', 'contract_hash', 'worktree',
                    'submitted_commit', 'budget', 'usage', 'next_action'):
            self.assertEqual(summary[key], full[key])

        review = b.call_tool('telos_review', {'session_id': sid})
        review_full = b.call_tool('telos_review', {'session_id': sid, 'detail': 'full'})
        self.assertEqual(review['detail'], 'summary')
        self.assertEqual(review_full['detail'], 'full')
        self.assertEqual(review['questions'], self.q)
        self.assertEqual(review_full['questions'], self.q)
        self.assertEqual(review['evidence']['exam']['answers'], review_full['evidence']['exam']['answers'])
        self.assertEqual([(check['id'], check['status']) for check in review['evidence']['checks']],
                         [(check['id'], check['status']) for check in review_full['evidence']['checks']])
        self.assertEqual(review['identity'], review_full['identity'])
        self.assertEqual(review['blockers'], [])
        self.assertEqual((root / 'state.json').read_bytes(), state_before)
        self.assertEqual((root / 'attempt-1.json').read_bytes(), packet_before)

    def test_invalid_detail_is_rejected_at_the_mcp_boundary(self):
        sid = self.create()
        for tool in ('telos_status', 'telos_receive', 'telos_review'):
            with self.subTest(tool=tool), self.assertRaisesRegex(b.GateError, 'summary or full'):
                b.call_tool(tool, {'session_id': sid, 'event_id': 'event', 'detail': 'verbose'}, None)

    def test_project_status_defaults_to_summary_with_all_task_identity(self):
        def task(ident, deps=(), paths=None):
            return {'id': ident, 'task': 'Implement ' + ident, 'message': 'Implement ' + ident,
                    'depends_on': list(deps), 'allowed_paths': paths or [ident + '.py'],
                    'questions': copy.deepcopy(self.q), 'checks': copy.deepcopy(self.checks)}
        pid = b.project_manager().create(repo=str(self.repo), task='Complete project',
            tasks=[task('a'), task('b', ['a'])], questions=self.q, checks=self.checks,
            decision_reason='One dependent writer', strategy='single', notify=False)['project_id']
        summary = b.call_tool('telos_project_status', {'project_id': pid}, None)
        full = b.call_tool('telos_project_status', {'project_id': pid, 'detail': 'full'}, None)
        self.assertEqual(summary['detail'], 'summary')
        self.assertEqual(full['detail'], 'full')
        self.assertEqual([(t['id'], t['depends_on'], t['session_id']) for t in summary['tasks']],
                         [(t['id'], t['depends_on'], t['session_id']) for t in full['tasks']])
        self.assertEqual(summary['tasks'][1]['depends_on'], ['a'])
        with self.assertRaisesRegex(b.GateError, 'summary or full'):
            b.call_tool('telos_project_status', {'project_id': pid, 'detail': 'verbose'}, None)

    def test_open_freezes_positive_integer_budget_and_rejects_invalid_shapes(self):
        budget = {'max_requests': 2, 'max_input_tokens': 100, 'max_uncached_input_tokens': 80,
                  'max_output_tokens': 40, 'max_total_tokens': 150}
        result = self.open_with_budget(budget)
        sid = result['session_id']
        self.assertEqual(result['budget'], budget)
        _, contract = b.load_session(b.run_dir(sid))
        self.assertEqual(contract['budget'], budget)
        self.assertEqual(b.call_tool('telos_status', {'session_id': sid})['budget'], budget)
        self.assertEqual(self.open_with_budget()['budget'], None)
        for invalid in ({'max_requests': True}, {'max_requests': 0}, {'max_requests': -1},
                        {'max_requests': '2'}, {'unknown': 1}, {}):
            with self.subTest(invalid=invalid), self.assertRaises(b.GateError):
                self.open_with_budget(invalid)

    def test_configured_default_budget_applies_and_is_validated(self):
        config = b.config()
        config['task_budget'] = {'max_requests': 3}
        b.write_json(b.CONFIG, config)
        self.assertEqual(self.open_with_budget()['budget'], {'max_requests': 3})
        config['task_budget'] = {'max_requests': False}
        b.write_json(b.CONFIG, config)
        with self.assertRaises(b.GateError):
            self.open_with_budget()

    def test_exhausted_budget_error_is_not_retried_as_infrastructure(self):
        sid = self.create()
        root = b.run_dir(sid)
        state, contract = b.load_session(root)
        state.update(phase='responded', thread_id='thread')
        b.write_json(root / 'state.json', state)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            job = b.start_job(sid, 'evaluate')['job_id']
        snapshot = {'budget': {'max_input_tokens': 10}, 'totals': {'requests': 1},
                    'budget_state': {'state': 'unknown', 'exceeded': [], 'unknown': ['max_input_tokens'],
                                     'remaining': {'max_input_tokens': 10}}}
        with patch.object(b, 'executable_checks', return_value=[{'id': 'check', 'status': 'passed'}]), \
                patch.object(b, 'answer_exam', side_effect=BudgetExceeded(
                    'Task budget cannot be evaluated because usage is unknown; no further paid request may be sent',
                    snapshot)):
            b.background_job(sid, job)
        current = b.read_json(root / 'state.json')
        self.assertEqual(current['last_failure_kind'], 'budget')
        self.assertEqual(current['next_retry_at'], 0)
        self.assertEqual(current.get('infra_failures', 0), 0)
        self.assertEqual(current['rounds'], 0)
        self.assertIn('budget cannot be evaluated', current['last_error'])
        evidence = b.read_json(root / f'job-{job}-error.json')
        self.assertEqual(evidence['kind'], 'budget')
        self.assertFalse(evidence['retryable'])
        self.assertEqual(evidence['usage'], snapshot)

    def test_send_budget_failure_is_terminal_with_usage_evidence(self):
        sid = self.create()
        root = b.run_dir(sid)
        state, _ = b.load_session(root)
        state.update(phase='responded', thread_id='thread')
        b.write_json(root / 'state.json', state)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            job = b.start_job(sid, 'send', 'Continue the work')['job_id']
        snapshot = {'budget': {'max_requests': 1}, 'totals': {'requests': 1},
                    'budget_state': {'state': 'exceeded', 'exceeded': ['max_requests'],
                                     'unknown': [], 'remaining': {'max_requests': 0}}}
        with patch.object(b, 'peer_turn', side_effect=BudgetExceeded(
                'Task budget exhausted (max_requests); used={"requests": 1} '
                'remaining={"max_requests": 0}; no upstream request was sent', snapshot)):
            b.background_job(sid, job)
        current = b.read_json(root / 'state.json')
        self.assertEqual(current['last_failure_kind'], 'budget')
        self.assertEqual(current['next_retry_at'], 0)
        self.assertEqual(current.get('infra_failures', 0), 0)
        evidence = b.read_json(root / f'job-{job}-error.json')
        self.assertEqual(evidence['kind'], 'budget')
        self.assertFalse(evidence['retryable'])
        self.assertEqual(evidence['usage'], snapshot)


if __name__ == '__main__':
    import unittest
    unittest.main(verbosity=2)

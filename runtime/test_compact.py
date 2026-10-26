"""Compact MCP projections must retain grading and identity evidence."""
import copy
import unittest

import compact


def status_full():
    return {
        'session_id': 's' * 32, 'phase': 'awaiting_review', 'accepted': False, 'model': 'm',
        'worktree': '/tmp/worktree', 'base_commit': 'a' * 40, 'submitted_commit': 'b' * 40,
        'packet_id': 'p' * 64, 'contract_hash': 'c' * 64, 'turns': 2, 'rounds': 1,
        'job_id': 'j', 'job_kind': 'evaluate', 'infra_failures': 0, 'next_retry_at': 0,
        'last_failure_kind': None, 'project_id': None, 'task_type': 'implementation',
        'version': 'test', 'workflow': 'v3', 'executor': {'provider': 'ark'},
        'budget': {'max_requests': 2}, 'usage': {'totals': {'requests': 1}},
        'next_action': 'telos_review_then_grade', 'archived': False,
        'last_error': 'failure ' * 1000,
        'acceptance': {'accepted': False, 'score': 0, 'threshold': 90, 'blockers': ['b']},
        'last_result': {'round': 1, 'checks_passed': True, 'exam_received': True,
                        'next_action': 'review_then_grade', 'usage': {'total_tokens': 3}},
        'return_channel': {'enabled': True, 'health': {'status': 'waiting'},
                           'event': {'id': 'event', 'status': 'delivered', 'attempts': 1,
                                     'actions': {'old': 'action'}, 'progress': {'status': 'waiting'},
                                     'last_error': 'bad ' * 500}},
    }


def review_full():
    return {
        'packet_id': 'p' * 64, 'session_id': 's' * 32, 'worktree': '/tmp/worktree',
        'submitted_commit': 'b' * 40, 'contract_hash': 'c' * 64,
        'questions': [{'id': 'q1', 'prompt': 'p', 'reference': 'private reference', 'points': 100,
                       'minimum': 90}],
        'instruction': 'grade carefully',
        'blockers': ['not yet accepted'],
        'budget': {'max_requests': 2},
        'usage': {'totals': {'requests': 1}},
        'identity': {'session_id': 's' * 32, 'packet_id': 'p' * 64, 'commit': 'b' * 40,
                     'contract_hash': 'c' * 64, 'executor_thread_id': 'thread'},
        'evidence': {
            'session_id': 's' * 32, 'round': 1, 'commit': 'b' * 40,
            'contract_hash': 'c' * 64, 'assessment_input_hash': 'i' * 64,
            'checks': [{'id': 'passed', 'status': 'passed', 'returncode': 0, 'seconds': 0.1,
                        'stdout': 'success ' * 5000, 'stderr': ''},
                       {'id': 'failed', 'status': 'failed', 'returncode': 1, 'seconds': 0.2,
                        'stdout': 'failure ' * 5000, 'stderr': 'traceback ' * 5000}],
            'exam': {'mode': 'closed_book', 'executor_thread_id': 'thread', 'session_id': 's' * 32,
                     'commit': 'b' * 40, 'same_executor': True, 'tool_call_count': 0,
                     'public_payload_hash': 'h' * 64, 'backend': {'model': 'm'},
                     'answers': [{'id': 'q1', 'answer': 'five'}],
                     'summary_text': 'static prose that may be omitted',
                     'attempts': [{'attempt': 1,
                                   'requests': [{'request_hash': 'r' * 64, 'input_hash': 'i' * 64,
                                                 'model': 'm'}],
                                   'responses': [{'response_hash': 'z' * 64,
                                                  'tool_call_count': 0}],
                                   'cli_session_usage': {'total_tokens': 8}}]},
            'submission': {'commit': 'b' * 40, 'status': 'pinned', 'backend': 'ark',
                           'files': [{'source': 'private code'}]},
            'job_id': 'job',
        },
    }


class CompactViewTests(unittest.TestCase):
    def test_timeout_partial_output_marker_survives_review_summary(self):
        full = review_full()
        full['evidence']['checks'][1].update(status='timeout', returncode=None, output_incomplete=True)
        check = compact.review(full)['evidence']['checks'][1]
        self.assertEqual(check['status'], 'timeout')
        self.assertTrue(check['output_incomplete'])
        self.assertTrue(check['stderr'])

    def test_status_summary_keeps_identity_usage_failure_and_next_action(self):
        full = status_full()
        before = copy.deepcopy(full)
        summary = compact.status(full)
        self.assertEqual(full, before)
        self.assertEqual(summary['detail'], 'summary')
        self.assertIn('detail="full"', summary['detail_hint'])
        for key in ('session_id', 'phase', 'accepted', 'worktree', 'base_commit', 'submitted_commit',
                    'packet_id', 'contract_hash', 'budget', 'usage', 'next_action', 'last_result'):
            self.assertEqual(summary[key], full[key])
        self.assertLessEqual(len(summary['last_error']), 1200)
        self.assertEqual(summary['acceptance'], full['acceptance'])
        self.assertEqual(summary['return_channel']['event']['id'], 'event')
        self.assertNotIn('actions', summary['return_channel']['event'])

    def test_review_summary_keeps_every_grading_input_but_bounds_logs(self):
        full = review_full()
        before = copy.deepcopy(full)
        summary = compact.review(full)
        self.assertEqual(full, before)
        self.assertEqual(summary['questions'], full['questions'])
        self.assertEqual(summary['identity'], full['identity'])
        self.assertEqual(summary['blockers'], full['blockers'])
        checks = summary['evidence']['checks']
        self.assertEqual([check['id'] for check in checks], ['passed', 'failed'])
        self.assertNotIn('stdout', checks[0])
        self.assertNotIn('stderr', checks[0])
        self.assertLessEqual(len(checks[1]['stdout']), 1200)
        self.assertLessEqual(len(checks[1]['stderr']), 1200)
        exam = summary['evidence']['exam']
        self.assertEqual(exam['answers'], full['evidence']['exam']['answers'])
        self.assertEqual(exam['executor_thread_id'], 'thread')
        self.assertEqual(exam['attempts'][0]['request_hashes'], ['r' * 64])
        self.assertEqual(summary['detail'], 'summary')
        self.assertIn('detail="full"', summary['detail_hint'])

    def test_receive_summary_keeps_event_identity_even_when_not_proceeding(self):
        full = {'proceed': False, 'event_id': 'event', 'session_id': 's' * 32,
                'next_action': None, 'result': None}
        summary = compact.receive(full)
        self.assertEqual(summary['proceed'], False)
        self.assertEqual(summary['event_id'], 'event')
        self.assertEqual(summary['session_id'], 's' * 32)
        self.assertIsNone(summary['result'])

    def test_project_summary_keeps_every_task_state_dependency_and_action(self):
        full = {'project_id': 'p' * 32, 'phase': 'running', 'accepted': False,
                'continuation': 'active', 'execution_mode': 'auto', 'strategy': 'parallel',
                'effective_parallel': 2, 'max_parallel': 2, 'max_jobs': 40, 'jobs_started': 3,
                'base_commit': 'a' * 40, 'decision_reason': 'parallel', 'return_health': {},
                'usage': {'requests': 2},
                'tasks': [{'id': 'a', 'task': 'static prose', 'phase': 'accepted',
                           'depends_on': [], 'allowed_paths': ['a.py'], 'session_id': 's' * 32,
                           'event_id': 'e1', 'accepted_commit': 'c' * 40,
                           'budget': {'max_requests': 3}, 'usage': {'requests': 1}},
                          {'id': 'b', 'phase': 'pending', 'depends_on': ['a'],
                           'allowed_paths': ['b.py'], 'session_id': 't' * 32,
                           'event_id': None, 'accepted_commit': None}],
                'integration': {'worktree': '/tmp/integration', 'pins': {'a': 'c' * 40},
                                'commit': None, 'packet_id': None, 'conflict': None},
                'final_review': {'accepted': False, 'blockers': ['b']}}
        summary = compact.project(full)
        self.assertEqual(summary['project_id'], 'p' * 32)
        self.assertEqual([task['id'] for task in summary['tasks']], ['a', 'b'])
        self.assertEqual(summary['tasks'][1]['depends_on'], ['a'])
        self.assertEqual(summary['tasks'][0]['event_id'], 'e1')
        self.assertEqual(summary['integration']['pins'], {'a': 'c' * 40})
        self.assertEqual(summary['final_review']['blockers'], ['b'])
        self.assertNotIn('task', summary['tasks'][0])

    def test_project_summary_propagates_return_event_identity_without_payload(self):
        full = {'project_id': 'p' * 32, 'phase': 'running', 'accepted': False,
                'continuation': 'paused', 'return_health': {'status': 'paused', 'next_action': None},
                'return_event': {'id': 'e' * 32, 'status': 'processed',
                                 'actions': {'private': 'payload'},
                                 'last_error': 'private transport detail'},
                'tasks': []}
        before = copy.deepcopy(full)
        summary = compact.project(full)
        self.assertEqual(summary['return_event'], {'id': 'e' * 32, 'status': 'processed'})
        self.assertEqual(set(summary['return_event']), {'id', 'status'})
        self.assertEqual(summary['return_health'], {'status': 'paused', 'next_action': None})
        self.assertEqual(full, before)

        full['return_event'] = None
        self.assertIsNone(compact.project(full)['return_event'])
        self.assertIsNone(compact.project({'project_id': 'p' * 32, 'tasks': []})['return_event'])
        receive = compact.project_receive({'project_id': 'p' * 32, 'project': full})
        self.assertIsNone(receive['project']['return_event'])

    def test_project_receive_keeps_child_event_and_session_identifiers(self):
        full = {'proceed': True, 'project_id': 'p' * 32, 'instruction': 'review',
                'events': [{'task_id': 'a', 'session_id': 's' * 32, 'proceed': True,
                            'event_id': 'e1', 'next_action': 'telos_evaluate',
                            'result': {'session_id': 's' * 32, 'phase': 'responded'}}],
                'project': {'project_id': 'p' * 32, 'phase': 'running', 'tasks': [
                    {'id': 'a', 'phase': 'responded', 'depends_on': [], 'event_id': 'e1'}]}}
        summary = compact.project_receive(full)
        event = summary['events'][0]
        self.assertEqual(summary['project_id'], 'p' * 32)
        self.assertEqual((event['task_id'], event['session_id'], event['event_id'],
                          event['next_action']), ('a', 's' * 32, 'e1', 'telos_evaluate'))
        self.assertEqual(summary['project']['tasks'][0]['id'], 'a')
        self.assertEqual(summary['detail'], 'summary')

    def test_project_review_keeps_criteria_check_outcomes_identity_and_blockers(self):
        full = review_full()
        full.update(project_id='p' * 32, worktree='/tmp/integration', commit='b' * 40,
                    pins={'a': 'c' * 40}, final_review={'accepted': False, 'blockers': ['b']})
        summary = compact.project_review(full)
        self.assertEqual(summary['questions'], full['questions'])
        self.assertEqual(summary['identity'], full['identity'])
        self.assertEqual(summary['pins'], {'a': 'c' * 40})
        self.assertEqual([check['id'] for check in summary['evidence']['checks']], ['passed', 'failed'])
        self.assertEqual(summary['blockers'], ['not yet accepted'])
        self.assertEqual(summary['detail'], 'summary')


class FormatSummaryTests(unittest.TestCase):
    """Format evidence is summarized within its fixed, bounded vocabulary."""

    @staticmethod
    def _full(attempts):
        full = review_full()
        full['evidence']['exam']['usage'] = {'exam_requests': len(attempts)}
        full['evidence']['exam']['attempts'] = attempts
        return full

    def test_legacy_attempts_omit_format_evidence_exactly(self):
        full = review_full()
        full['evidence']['exam']['attempts'].append({
            'attempt': 2, 'requests': [{'request_hash': 'x' * 64, 'input_hash': 'y' * 64}],
            'responses': [], 'cli_session_usage': None})
        before = copy.deepcopy(full)
        summary = compact.review(full)
        self.assertEqual(full, before)
        for attempt in summary['evidence']['exam']['attempts']:
            self.assertEqual(set(attempt), {
                'attempt', 'request_count', 'response_count', 'request_hashes',
                'input_hashes', 'response_hashes', 'tool_call_count', 'cli_session_usage'})
            self.assertNotIn('format_valid', attempt)
            self.assertNotIn('format_error', attempt)
            self.assertNotIn('text_formats', attempt)
        self.assertEqual(compact.project_review(full)['evidence']['exam']['attempts'],
                         summary['evidence']['exam']['attempts'])

    def test_format_valid_keeps_booleans_and_none_and_normalizes_other_types(self):
        attempts = [
            {'attempt': 1, 'requests': [], 'responses': [], 'format_valid': True},
            {'attempt': 2, 'requests': [], 'responses': [], 'format_valid': False},
            {'attempt': 3, 'requests': [], 'responses': [], 'format_valid': 'true'},
            {'attempt': 4, 'requests': [], 'responses': [], 'format_valid': 1},
            {'attempt': 5, 'requests': [], 'responses': [], 'format_valid': None},
            {'attempt': 6, 'requests': [], 'responses': []},
        ]
        summary = compact.review(self._full(attempts))
        summarized = summary['evidence']['exam']['attempts']
        self.assertEqual([attempt.get('format_valid', 'absent') for attempt in summarized],
                         [True, False, None, None, None, 'absent'])
        self.assertIs(summarized[0]['format_valid'], True)
        self.assertIs(summarized[1]['format_valid'], False)
        self.assertNotIn('format_valid', summarized[5])

    def test_format_error_keeps_fixed_diagnostics_and_bounds_malformed_values(self):
        diagnostics = [None, 'invalid JSON', 'root must contain only an answers array',
                       'each answer needs exactly id and answer',
                       'id and answer must be nonempty strings',
                       'missing, duplicate, or unknown question ids', 'answer schema violation']
        malformed = ['Exam returned invalid JSON', 'model payload: secret', 42,
                     {'private': 'text'}, ['bad']]
        attempts = [{'attempt': i + 1, 'requests': [], 'responses': [], 'format_error': value}
                    for i, value in enumerate(diagnostics + malformed)]
        attempts.append({'attempt': len(attempts) + 1, 'requests': [], 'responses': []})
        full = self._full(attempts)
        before = copy.deepcopy(full)
        summary = compact.review(full)
        self.assertEqual(full, before)
        summarized = summary['evidence']['exam']['attempts']
        self.assertEqual([attempt.get('format_error', 'absent') for attempt in summarized],
                         diagnostics + ['answer schema violation'] * len(malformed) + ['absent'])
        self.assertNotIn('format_error', summarized[-1])

    def test_text_formats_align_with_request_hashes_for_mixed_modes(self):
        requests = [
            {'request_hash': 'a' * 64, 'input_hash': '1' * 64, 'text_format': 'json_object'},
            {'request_hash': 'b' * 64, 'input_hash': '2' * 64, 'text_format': 'json_schema'},
            {'request_hash': 'c' * 64, 'input_hash': '3' * 64, 'text_format': 'text'},
            {'request_hash': 'd' * 64, 'input_hash': '4' * 64, 'text_format': 'json'},
            {'request_hash': 'e' * 64, 'input_hash': '5' * 64, 'text_format': 7},
            {'request_hash': 'f' * 64, 'input_hash': '6' * 64},
        ]
        full = self._full([{'attempt': 1, 'requests': requests, 'responses': []}])
        before = copy.deepcopy(full)
        summary = compact.review(full)
        attempt = summary['evidence']['exam']['attempts'][0]
        self.assertEqual(full, before)
        self.assertEqual(attempt['request_hashes'], [request['request_hash'] for request in requests])
        self.assertEqual(attempt['text_formats'],
                         ['json_object', 'json_schema', 'text', None, None, None])
        self.assertEqual(len(attempt['text_formats']), len(attempt['request_hashes']))

    def test_both_review_routes_expose_identical_format_evidence_without_mutation(self):
        full = self._full([
            {'attempt': 1,
             'requests': [{'request_hash': 'r' * 64, 'input_hash': 'i' * 64,
                           'text_format': 'json_schema'},
                          {'request_hash': 's' * 64, 'input_hash': 'j' * 64,
                           'text_format': 'json_object'}],
             'responses': [{'response_hash': 'z' * 64, 'tool_call_count': 0}],
             'cli_session_usage': {'total_tokens': 8},
             'format_valid': False, 'format_error': 'invalid JSON'},
            {'attempt': 2, 'requests': [], 'responses': [],
             'format_valid': True, 'format_error': None},
        ])
        full.update(project_id='p' * 32, worktree='/tmp/integration', commit='b' * 40,
                    pins={'a': 'c' * 40}, final_review={'accepted': False, 'blockers': ['b']})
        before = copy.deepcopy(full)
        reviewed = compact.review(full)
        projected = compact.project_review(full)
        self.assertEqual(full, before)
        self.assertEqual(reviewed['evidence']['exam']['attempts'],
                         projected['evidence']['exam']['attempts'])
        attempts = reviewed['evidence']['exam']['attempts']
        self.assertEqual(attempts[0]['format_valid'], False)
        self.assertEqual(attempts[0]['format_error'], 'invalid JSON')
        self.assertEqual(attempts[0]['text_formats'], ['json_schema', 'json_object'])
        self.assertEqual(attempts[1]['format_valid'], True)
        self.assertIsNone(attempts[1]['format_error'])
        self.assertNotIn('text_formats', attempts[1])
        self.assertEqual(reviewed['evidence']['exam']['answers'], full['evidence']['exam']['answers'])
        self.assertEqual(reviewed['evidence']['exam']['usage'], full['evidence']['exam']['usage'])
        self.assertEqual(reviewed['identity'], full['identity'])
        self.assertEqual(reviewed['blockers'], full['blockers'])
        self.assertEqual(projected['pins'], {'a': 'c' * 40})


if __name__ == '__main__':
    unittest.main(verbosity=2)

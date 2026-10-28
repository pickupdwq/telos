"""MCP-boundary detail defaults, frozen budgets and budget-stop regressions."""
import copy
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

import test_telos
from usage import BudgetExceeded

b = test_telos.b


class MCPErrorTests(unittest.TestCase):
    def transport(self, request, error=None):
        stream = io.StringIO()
        with patch.object(b.sys, 'stdin', io.StringIO(json.dumps(request) + '\n')), \
                patch.object(b.sys, 'stdout', stream):
            if error is None:
                b.serve_messages()
            else:
                with patch.object(b, 'call_tool', side_effect=error):
                    b.serve_messages()
        return json.loads(stream.getvalue())['result']

    def test_budget_error_retains_machine_readable_usage_and_never_suggests_retry(self):
        snapshot = {'totals': {'requests': 2}, 'budget_state': {'state': 'unknown'},
                    'requests': {'private': 'MUST_NOT_EXPOSE_RAW_LEDGER'}}
        request = {'id': 1, 'method': 'tools/call', 'params': {'name': 'telos_send'}}
        response = self.transport(request, BudgetExceeded('Usage is unknown', snapshot))
        self.assertTrue(response['isError'])
        data = json.loads(response['content'][0]['text'])
        self.assertEqual(data, response['structuredContent'])
        self.assertEqual(data['error']['code'], 'budget_unknown')
        self.assertFalse(data['error']['retryable'])
        self.assertEqual(data['error']['usage']['totals']['requests'], 2)
        self.assertNotIn('MUST_NOT_EXPOSE', json.dumps(data))

    def test_backoff_has_explicit_delay_and_unknown_errors_do_not_leak(self):
        request = {'id': 1, 'method': 'tools/call', 'params': {'name': 'telos_evaluate'}}
        error = b.GateError('Backoff', code='retry_backoff', retryable=True,
                            retry_after_seconds=12, next_action='retry_after_backoff')
        data = self.transport(request, error)['structuredContent']['error']
        self.assertEqual((data['code'], data['retryable'], data['retry_after_seconds']),
                         ('retry_backoff', True, 12))
        response = self.transport(request, RuntimeError('PRIVATE_PROVIDER_BODY'))
        self.assertEqual(response['structuredContent']['error']['code'], 'internal_error')
        self.assertNotIn('PRIVATE_PROVIDER_BODY', json.dumps(response))

    def test_invalid_tool_arguments_return_stable_errors(self):
        for params, expected in [
                ({'name': 'telos_status', 'arguments': {}}, 'invalid_arguments'),
                ({'name': 'telos_status', 'arguments': []}, 'invalid_arguments'),
                ({'name': 'unknown', 'arguments': {}}, 'unknown_tool')]:
            data = self.transport({'id': 1, 'method': 'tools/call', 'params': params})
            self.assertEqual(data['structuredContent']['error']['code'], expected)

    def test_initialize_retains_compact_rules_and_full_tool_contracts(self):
        response = self.transport({'id': 1, 'method': 'initialize', 'params': {}})
        instructions = response['instructions']
        self.assertLess(len(instructions), 400)
        for rule in ('original supervisor', 'private', 'proceed', 'event IDs',
                     'pause/cancel', 'accepted=true', 'project acceptance', 'merge/deploy'):
            self.assertIn(rule, instructions)
        tools = self.transport({'id': 2, 'method': 'tools/list'})['tools']
        self.assertEqual(len(tools), 30)
        budget = next(tool for tool in tools if tool['name'] == 'telos_project_budget')
        self.assertEqual(sorted(budget['inputSchema']['required']),
                         ['project_id', 'reason', 'request_id', 'user_authorization'])
        self.assertEqual(budget['inputSchema']['properties']['strategy']['enum'], ['unlimited'])

    def test_invalid_initialize_keeps_request_loop_alive(self):
        for params in (None, [], 42, 'PRIVATE_INPUT', {'protocolVersion': None},
                       {'protocolVersion': []}, {'protocolVersion': 42}):
            with self.subTest(params=params):
                requests = [{'id': 1, 'method': 'initialize', 'params': params},
                            {'method': 'notifications/initialized'},
                            {'id': 2, 'method': 'ping'},
                            {'id': 3, 'method': 'tools/list'},
                            {'id': 4, 'method': 'initialize', 'params': {}}]
                output = io.StringIO()
                with patch.object(b.sys, 'stdin', io.StringIO(
                        ''.join(json.dumps(r) + '\n' for r in requests))), \
                        patch.object(b.sys, 'stdout', output):
                    b.serve_messages()
                replies = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4])
                self.assertEqual(replies[0]['error']['code'], -32602)
                self.assertEqual(replies[1]['result'], {})
                self.assertEqual(len(replies[2]['result']['tools']), 30)
                self.assertEqual(replies[3]['result']['serverInfo']['name'], 'telos')
                self.assertNotIn('PRIVATE_INPUT', output.getvalue())

    def test_missing_tool_call_parameters_are_argument_errors(self):
        for request in ({'id': 1, 'method': 'tools/call'},
                        {'id': 1, 'method': 'tools/call', 'params': None}):
            response = self.transport(request)
            self.assertEqual(response['structuredContent']['error']['code'], 'invalid_arguments')

    def test_shortcut_handlers_check_required_arguments(self):
        for name, arguments in (
                ('telos_experience_search', {}),
                ('telos_experience_forget', {}),
                ('telos_audit_probe', {}),
                ('telos_audit_probe', {'request': {}}),
                ('telos_project_budget', {'project_id': 'p'}),
                ('telos_project_budget', {'project_id': 'p', 'request_id': 'r',
                                          'reason': 'x'})):
            with self.subTest(name=name, arguments=arguments):
                response = self.transport({'id': 1, 'method': 'tools/call',
                    'params': {'name': name, 'arguments': arguments}})
                error = response['structuredContent']['error']
                self.assertEqual(error['code'], 'invalid_arguments')
                self.assertEqual(error['next_action'], 'correct_arguments')
                self.assertFalse(error['retryable'])

    def test_handler_type_error_is_not_misclassified_as_invalid_arguments(self):
        with patch('experience.Experience.lookup', autospec=True,
                   side_effect=TypeError('PRIVATE_HANDLER_FAILURE')) as lookup:
            response = self.transport({'id': 1, 'method': 'tools/call',
                'params': {'name': 'telos_experience_search', 'arguments': {'repo': '/fixture'}}})
        lookup.assert_called_once()
        self.assertEqual(response['structuredContent']['error']['code'], 'internal_error')
        self.assertNotIn('PRIVATE_HANDLER_FAILURE', json.dumps(response))

    def test_nested_input_errors_identify_fields_before_runtime_calls(self):
        cases = [
            ('telos_project_open', {'tasks': [42]}, 'arguments.tasks[0]'),
            ('telos_project_open', {'tasks': [{'id': 42}]}, 'arguments.tasks[0].id'),
            ('telos_project_open', {'tasks': None}, 'arguments.tasks'),
            ('telos_project_update', {'add_tasks': [None]}, 'arguments.add_tasks[0]'),
            ('telos_open', {'questions': [{'points': True}]}, 'arguments.questions[0].points'),
            ('telos_open', {'checks': [{'argv': [42]}]}, 'arguments.checks[0].argv[0]'),
            ('telos_open', {'budget': {'max_requests': 'PRIVATE_VALUE'}}, 'arguments.budget.max_requests'),
            ('telos_grade', {'scores': [42]}, 'arguments.scores[0]'),
            ('telos_send', {'message': {'secret': 'PRIVATE_VALUE'}}, 'arguments.message'),
            ('telos_project_open', {'execution_mode': 'PRIVATE_VALUE'}, 'arguments.execution_mode'),
            ('telos_audit_probe', {'request': {'headers': {'PRIVATE_KEY': 42}}}, 'arguments.request.headers.<key>'),
        ]
        for name, arguments, field in cases:
            with self.subTest(name=name, field=field), \
                    patch.object(b, 'project_manager') as manager, \
                    patch.object(b, 'load_session') as load, \
                    patch.object(b, 'create_session') as create:
                result = self.transport({'id': 1, 'method': 'tools/call',
                    'params': {'name': name, 'arguments': arguments}})
                error = result['structuredContent']['error']
                self.assertEqual(error['code'], 'invalid_arguments')
                self.assertEqual(error['next_action'], 'correct_arguments')
                self.assertFalse(error['retryable'])
                self.assertIn(field, error['message'])
                self.assertNotIn('PRIVATE_', json.dumps(result))
                manager.assert_not_called(); load.assert_not_called(); create.assert_not_called()


class MCPBoundaryTests(unittest.TestCase):
    setUp = test_telos.BridgeTests.setUp
    tearDown = test_telos.BridgeTests.tearDown
    create = test_telos.BridgeTests.create
    ready_review = test_telos.BridgeTests.ready_review
    scores = test_telos.BridgeTests.scores

    def open_with_budget(self, budget=None):
        args = {'repo': str(self.repo), 'task': 'Implement addition.', 'skip_meeting_reason': 'Direct API compatibility fixture.', 'questions': self.q,
                'checks': self.checks, 'notify': False}
        if budget is not None:
            args['budget'] = budget
        return b.call_tool('telos_open', args, None)

    def test_null_options_and_default_project_strategy_remain_compatible(self):
        self.assertEqual(b.call_tool('telos_cache_manage', {})['tasks'], [])
        result = b.call_tool('telos_open', {'repo': str(self.repo), 'task': 'Implement addition.', 'skip_meeting_reason': 'Direct API compatibility fixture.',
            'questions': self.q, 'checks': self.checks, 'notify': False,
            'budget': None, 'executor_profile': None, 'dependency_paths': None})
        _, contract = b.load_session(b.run_dir(result['session_id']))
        self.assertIsNone(contract.get('budget'))
        project = b.call_tool('telos_project_open', {'repo': str(self.repo), 'task': 'Example project', 'skip_meeting_reason': 'Direct project compatibility fixture.',
            'tasks': [{'id': 'a', 'task': 'Implement a', 'message': 'Complete the assigned change',
                       'allowed_paths': ['a.py'], 'questions': self.q, 'checks': self.checks, 'budget': None}],
            'questions': self.q, 'checks': self.checks, 'decision_reason': 'One independent task', 'notify': False})
        self.assertEqual(project['strategy'], 'single')
        self.assertEqual(project['jobs_started'], 0)
        self.assertIsNone(project['usage'])

    def test_real_budget_gate_reaches_structured_mcp_error_without_starting_job(self):
        sid = self.open_with_budget({'max_requests': 1})['session_id']
        state, contract = b.load_session(b.run_dir(sid))
        ledger = b.usage_ledger(state, contract)
        ledger.finish(ledger.begin('work'), {'input_tokens': 2, 'output_tokens': 1}, latency_ms=25)
        with patch.object(b.subprocess, 'Popen') as launch:
            response = MCPErrorTests().transport({'id': 1, 'method': 'tools/call',
                'params': {'name': 'telos_send', 'arguments': {'session_id': sid, 'message': 'Continue'}}})
            launch.assert_not_called()
        error = response['structuredContent']['error']
        self.assertEqual(error['code'], 'budget_exhausted')
        self.assertFalse(error['retryable'])
        self.assertEqual(error['usage']['totals']['requests'], 1)
        self.assertEqual(error['usage']['request_latency']['observed_seconds'], .025)

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

    def test_disabled_token_limits_keep_usage_request_reserves_and_time_limits(self):
        from usage import BudgetExceeded
        config = b.config()
        config['task_token_limits_enabled'] = False
        b.write_json(b.CONFIG, config)
        supplied = {'max_requests': 3, 'exam_reserve_requests': 1, 'closeout_reserve_seconds': 1,
                    'max_input_tokens': 1, 'max_uncached_input_tokens': 1, 'max_output_tokens': 1,
                    'max_total_tokens': 4, 'exam_reserve_tokens': 2}
        opened = self.open_with_budget(supplied)
        expected = {'max_requests': 3, 'exam_reserve_requests': 1, 'closeout_reserve_seconds': 1}
        self.assertEqual(opened['budget'], expected)
        state, contract = b.load_session(b.run_dir(opened['session_id']))
        self.assertEqual(contract['budget'], expected)
        ledger = b.usage_ledger(state, contract)
        ledger.finish(ledger.begin('work'), {'input_tokens': 100, 'output_tokens': 20})
        self.assertEqual(ledger.ensure_available('work')['totals']['total_tokens'], 120)
        ledger.finish(ledger.begin('work'), None)
        ledger.ensure_known()
        with self.assertRaises(BudgetExceeded):
            ledger.begin('work')
        ledger.finish(ledger.begin('exam'), {'input_tokens': 200, 'output_tokens': 50})
        with self.assertRaises(BudgetExceeded):
            ledger.begin('exam')
        self.assertEqual(ledger.snapshot()['totals']['total_tokens'], 370)
        self.assertEqual(supplied['max_total_tokens'], 4)

    def test_disabled_token_only_default_freezes_unlimited_budget(self):
        config = b.config()
        config.update(task_token_limits_enabled=False,
                      task_budget={'max_total_tokens': 10, 'exam_reserve_tokens': 2})
        b.write_json(b.CONFIG, config)
        opened = self.open_with_budget()
        self.assertIsNone(opened['budget'])
        config.update(task_token_limits_enabled=True, task_budget={'max_total_tokens': 1})
        b.write_json(b.CONFIG, config)
        state, contract = b.load_session(b.run_dir(opened['session_id']))
        self.assertIsNone(contract['budget'])
        ledger = b.usage_ledger(state, contract)
        ledger.finish(ledger.begin('work'), {'input_tokens': 100, 'output_tokens': 20})
        self.assertEqual(ledger.ensure_available()['budget_state']['state'], 'unlimited')

    def test_token_policy_does_not_rewrite_existing_frozen_budget(self):
        from usage import BudgetExceeded
        opened = self.open_with_budget({'max_total_tokens': 10})
        config = b.config()
        config['task_token_limits_enabled'] = False
        b.write_json(b.CONFIG, config)
        state, contract = b.load_session(b.run_dir(opened['session_id']))
        self.assertEqual(contract['budget'], {'max_total_tokens': 10})
        ledger = b.usage_ledger(state, contract)
        ledger.finish(ledger.begin('work'), {'input_tokens': 10, 'output_tokens': 10})
        with self.assertRaises(BudgetExceeded):
            ledger.begin('work')
        self.assertIsNone(self.open_with_budget({'max_total_tokens': 10})['budget'])

    def test_token_limit_policy_requires_a_boolean(self):
        for invalid in ('false', 0, None):
            with self.subTest(invalid=invalid):
                config = b.config()
                config['task_token_limits_enabled'] = invalid
                b.write_json(b.CONFIG, config)
                with self.assertRaises(b.GateError):
                    self.open_with_budget({'max_requests': 2})

    def test_exam_reserves_freeze_at_mcp_boundary_and_gate_work_only(self):
        budget = {'max_requests': 3, 'exam_reserve_requests': 1,
                  'max_total_tokens': 1000, 'exam_reserve_tokens': 200, 'closeout_reserve_seconds': 1}
        opened = self.open_with_budget(budget)
        sid = opened['session_id']
        root = b.run_dir(sid)
        state, contract = b.load_session(root)
        self.assertEqual(contract['budget'], budget)
        ledger = b.usage_ledger(state, contract)
        for _ in range(2):
            ledger.finish(ledger.begin('work'), {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12})
        state.update(phase='responded', thread_id='existing-executor')
        b.write_json(root / 'state.json', state)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())) as launch:
            with self.assertRaisesRegex(BudgetExceeded, 'reserved for the exam'):
                b.start_job(sid, 'send', 'More work')
            launch.assert_not_called()
            self.assertEqual(b.start_job(sid, 'evaluate')['phase'], 'running')
            launch.assert_called_once()
        self.assertIn('work_budget_state', b.call_tool('telos_status', {'session_id': sid})['usage'])
        for invalid in ({'exam_reserve_tokens': 10}, {'max_requests': 1, 'exam_reserve_requests': 1}):
            with self.subTest(invalid=invalid), self.assertRaises(b.GateError):
                self.open_with_budget(invalid)
        with self.assertRaisesRegex(b.GateError, 'job_timeout'):
            self.open_with_budget({'closeout_reserve_seconds': contract['executor']['job_timeout']})

    def test_execution_timeout_is_saved_as_a_local_failure_not_a_quota_diagnosis(self):
        sid = self.create()
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            job = b.start_job(sid, 'send', 'Do the bounded task')['job_id']
        error = b.providers.ExecutionTimeout('Execution model timed out')
        error.thread_id = 'interrupted-executor'
        with patch.object(b, 'peer_turn', side_effect=error):
            b.background_job(sid, job)
        state = b.read_json(b.run_dir(sid) / 'state.json')
        failure = b.read_json(b.run_dir(sid) / f'job-{job}-error.json')
        self.assertEqual(state['phase'], 'error')
        self.assertEqual(state['last_failure_kind'], 'execution_timeout')
        self.assertEqual(state['thread_id'], 'interrupted-executor')
        self.assertEqual(failure['kind'], 'execution_timeout')
        self.assertFalse(failure['retryable'])
        self.assertFalse(b.status(sid)['accepted'])
        # Preserving the thread must not make an incomplete work turn examinable.
        with self.assertRaisesRegex(b.GateError, 'completed execution-model turn'):
            b.start_job(sid, 'evaluate')

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

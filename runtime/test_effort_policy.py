"""Frozen capabilities, per-job choices, and real CLI/HTTP acceptance evidence."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import effort_policy as ep
import providers
from test_telos import b, BridgeTests
from test_closed_book import WorkflowTests, message
from test_projects import ProjectTests
from test_meetings import MeetingTests
from test_callbacks import OWNER, OTHER
from test_local_executor import server, completion, spec, config as local_config
from isolation import model_relay
from usage import UsageLedger


def config():
    return {'dynamic_reasoning_enabled': True, 'task_token_limits_enabled': False,
            'task_budget': {'max_total_tokens': 1}, 'default_executor_profile': 'bailian_test',
            'profiles': {'bailian_test': {'provider': 'bailian',
                'model': 'deepseek-v4.1-flash-1017', 'reasoning_effort': 'max'}},
            'job_timeout': 35}


class CapabilityTests(unittest.TestCase):
    def test_opt_in_and_exact_capability_boundary(self):
        binding = providers.resolve_executor(config())
        self.assertIsNone(ep.freeze(binding, {}))
        policy = ep.freeze(binding, config())
        self.assertEqual(policy['allowed_efforts'], ['low', 'high', 'max'])
        self.assertEqual(ep.validate(policy, binding), policy)
        self.assertEqual(ep.freeze(binding, {}, {'allowed_efforts': ['medium', 'max']})['allowed_efforts'], ['high', 'max'])
        for key in ('provider', 'model', 'wire_api'):
            unknown = {**binding, key: 'unverified'}
            self.assertIsNone(ep.freeze(unknown, config()))
            with self.assertRaisesRegex(providers.ProviderError, 'No verified'):
                ep.freeze(unknown, config(), {'allowed_efforts': ['max']})
        declared = {**config(), 'reasoning_capabilities': {
            ep.capability_key(unknown): {'supported_efforts': ['medium', 'max'],
                                        'aliases': {}, 'source': 'operator fixture verification'}}}
        self.assertEqual(ep.freeze(unknown, declared)['supported_efforts'], ['medium', 'max'])

    def test_malformed_capabilities_and_ranges_fail_as_policy_errors(self):
        binding = providers.resolve_executor(config())
        spec = copy.deepcopy(next(iter(ep.CAPABILITIES.values())))
        for raw in (None, [], {}, {**spec, 'supported_efforts': [[]]},
                    {**spec, 'supported_efforts': ['high', 'high']},
                    {**spec, 'aliases': {'medium': []}}, {**spec, 'source': ''}):
            with self.subTest(raw=raw), self.assertRaises(providers.ProviderError):
                ep.capability(raw)
        for requested in ({}, {'allowed_efforts': []}, {'allowed_efforts': [[]]},
                          {'allowed_efforts': ['low']}, {'allowed_efforts': ['ultra']},
                          {'allowed_efforts': ['max'], 'model': 'override'}):
            with self.subTest(requested=requested), self.assertRaises(providers.ProviderError):
                ep.freeze(binding, config(), requested)
        policy = ep.freeze(binding, config())
        for change in ({'version': True}, {'allowed_efforts': [{}]}, {'initial_effort': 'low'}):
            with self.assertRaises(providers.ProviderError):
                ep.validate({**policy, **change}, binding)
        with self.assertRaises(providers.ProviderError):
            ep.select({'executor': binding, 'reasoning_policy': policy}, {'reasoning_decision': []})


class JobTests(unittest.TestCase):
    tearDown = BridgeTests.tearDown
    scores = BridgeTests.scores

    def setUp(self):
        BridgeTests.setUp(self)
        b.write_json(b.CONFIG, config())

    def create(self, **kwargs):
        return b.create_session(str(self.repo), 'Verify addition.', self.q, self.checks,
                                notify=False, **kwargs)['session_id']

    def load(self, sid):
        return b.load_session(b.run_dir(sid))

    def start(self, sid, effort=None, reason=None, kind='send', event_id=None):
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            return b.start_job(sid, kind, 'Verify addition.' if kind == 'send' else '',
                               event_id, effort, reason)

    def finish(self, sid, job):
        with patch.object(b, 'spawn_dispatch'), patch.object(b, 'peer_turn', return_value={'thread_id': OTHER, 'summary': 'Verified addition.'}):
            b.background_job(sid, job)

    def test_round_selection_alias_inheritance_and_frozen_config(self):
        sid = self.create(); state, contract = self.load(sid)
        frozen = b.digest(contract)
        self.assertIsNone(contract['budget'])
        b.write_json(b.CONFIG, {**config(), 'dynamic_reasoning_enabled': False,
                               'reasoning_capabilities': {ep.capability_key(contract['executor']): {}}})
        for index, (effort, effective) in enumerate([('high', 'high'), ('max', 'max'), ('minimal', 'low'), (None, 'low')]):
            result = self.start(sid, effort, 'Scope and evidence justify this level.' if effort else None)
            state, current = self.load(sid)
            self.assertEqual(b.execution_binding(state, current)['reasoning_effort'], effective)
            (Path(state['worktree']) / 'test_signed.py').write_text(
                f'from calc import add\nassert add({index}, -{index}) == 0\n')
            self.finish(sid, result['job_id'])
            self.assertEqual(self.load(sid)[0]['thread_id'], OTHER)
        state, current = self.load(sid)
        self.assertEqual(b.digest(current), frozen)
        self.assertEqual([d['effective_effort'] for d in state['reasoning_history']], ['high', 'max', 'low', 'low'])
        self.assertEqual(state['reasoning_history'][-1]['source'], 'inherit')
        self.assertEqual(b.status(sid, detail='full')['reasoning_history'], state['reasoning_history'])
        self.assertEqual(b.status(sid, detail='summary')['reasoning_decision'], state['reasoning_decision'])

    def test_invalid_selection_and_running_job_never_spawn_or_mutate(self):
        sid = self.create(reasoning_policy={'allowed_efforts': ['high', 'max']})
        before = self.load(sid)[0]
        for effort, reason in [('low', 'Too low'), ('high', None), (None, 'No effort'),
                               ('ultra', 'Unsupported'), ('max', ' '), ('high', 'x' * 1001)]:
            with patch.object(b.subprocess, 'Popen') as spawn, self.assertRaises(providers.ProviderError):
                b.start_job(sid, 'send', 'Verify addition.', reasoning_effort=effort, reasoning_reason=reason)
            spawn.assert_not_called()
            self.assertEqual(self.load(sid)[0], before)
        job = self.start(sid, 'high', 'Scoped work')
        running = self.load(sid)[0]
        with self.assertRaisesRegex(b.GateError, 'already running'):
            self.start(sid, 'max', 'Cannot alter in flight')
        self.assertEqual(self.load(sid)[0], running)
        self.finish(sid, job['job_id'])

    def test_legacy_and_preexisting_tasks_remain_fixed(self):
        b.write_json(b.CONFIG, {**config(), 'dynamic_reasoning_enabled': False})
        for version in (2, 3):
            sid = self.create(_workflow_version=version)
            state, contract = self.load(sid)
            self.assertNotIn('reasoning_policy', contract)
            b.write_json(b.CONFIG, config())
            with self.assertRaisesRegex(providers.ProviderError, 'fixed reasoning'):
                self.start(sid, 'high', 'New defaults do not rewrite old contracts')
            self.assertEqual(b.execution_binding(state, contract)['reasoning_effort'], 'max')
            b.write_json(b.CONFIG, {**config(), 'dynamic_reasoning_enabled': False})

    def test_spawn_rollback_and_persisted_job_tampering(self):
        sid = self.create(); job = self.start(sid, 'high', 'Scoped work'); self.finish(sid, job['job_id'])
        before = self.load(sid)[0]
        with patch.object(b.subprocess, 'Popen', side_effect=OSError('fixture')), self.assertRaises(b.GateError):
            b.start_job(sid, 'send', 'Try again.', reasoning_effort='max', reasoning_reason='Complex issue')
        state, contract = self.load(sid)
        self.assertEqual(state['reasoning_history'], before['reasoning_history'])
        self.assertEqual(state['reasoning_decision'], before['reasoning_decision'])
        self.assertEqual(len(list(b.run_dir(sid).glob('job-*.json'))), 1)
        path = b.run_dir(sid) / ('job-' + job['job_id'] + '.json')
        request = b.read_json(path); request['reasoning_decision']['effective_effort'] = 'low'
        b.write_json(path, request)
        with self.assertRaises(providers.ProviderError):
            b.execution_binding(state, contract)

    def test_idempotency_control_and_owner_guards_include_choice(self):
        sid = self.create(); job = self.start(sid); self.finish(sid, job['job_id'])
        root = b.run_dir(sid); state, _ = self.load(sid)
        event_id = 'choice-event'
        state.update(owner={'thread_id': OWNER, 'transport': 'desktop_ipc'}, last_event_id=event_id,
                     events={event_id: {'id': event_id, 'status': 'received', 'actions': {}}})
        b.write_json(root / 'state.json', state)
        first = self.start(sid, 'high', 'Scoped repair', event_id=event_id)
        self.assertEqual(self.start(sid, 'high', 'Scoped repair', event_id=event_id)['previous_result'], first)
        with self.assertRaises(b.GateError):
            self.start(sid, 'max', 'Scoped repair', event_id=event_id)
        self.finish(sid, first['job_id'])
        for continuation in ('paused', 'cancelled'):
            state, _ = self.load(sid); state['continuation'] = continuation
            b.write_json(root / 'state.json', state)
            with self.assertRaises(b.GateError):
                self.start(sid, 'low', 'Must honor user control')
        state['continuation'] = 'active'
        state['owner'] = {'thread_id': OWNER, 'transport': 'desktop_ipc'}
        b.write_json(root / 'state.json', state)
        with self.assertRaises(b.GateError):
            b.call_tool('telos_send', {'session_id': sid, 'message': 'Verify.',
                         'reasoning_effort': 'high', 'reasoning_reason': 'Wrong supervisor'}, OTHER)

    model = WorkflowTests.model

    def test_real_cli_resume_http_and_grade_use_selected_effort(self):
        sid = self.create()
        answers = json.dumps({'answers': [{'id': 'q1', 'answer': '5'}, {'id': 'q2', 'answer': '3'}]})
        def reply(request, number):
            return message(answers if request.get('tool_choice') == 'none' else 'Addition verified.')
        with self.model(reply) as requests:
            for effort in ('high', 'max'):
                job = self.start(sid, effort, 'Verify the selected execution level')
                b.background_job(sid, job['job_id'])
                self.assertEqual(self.load(sid)[0]['phase'], 'responded', self.load(sid)[0].get('last_error'))
            thread = self.load(sid)[0]['thread_id']
            job = self.start(sid, 'low', 'Small arithmetic recall with executable checks', kind='evaluate')
            b.background_job(sid, job['job_id'])
        state, contract = self.load(sid)
        self.assertEqual(state['phase'], 'awaiting_review', state.get('last_error'))
        self.assertEqual(state['thread_id'], thread)
        self.assertEqual([r['reasoning']['effort'] for r in requests], ['high', 'max', 'low'])
        self.assertEqual(requests[-1]['tools'], [])
        self.assertEqual(requests[-1]['tool_choice'], 'none')
        packet = b.read_json(b.run_dir(sid) / 'attempt-1.json')
        self.assertEqual(packet['exam']['backend']['reasoning_effort'], 'low')
        self.assertEqual(b.compact_views._evidence(packet)['reasoning_decision'], packet['reasoning_decision'])
        import closed_book
        for key, value in [('job_id', 'a' * 32), ('reasoning_decision', {})]:
            with self.assertRaises((b.GateError, OSError, providers.ProviderError)):
                closed_book.validate_evidence(b, state, contract, {**packet, key: value})
        changed = copy.deepcopy(packet); changed['exam']['backend']['reasoning_effort'] = 'max'
        with self.assertRaises(b.GateError):
            closed_book.validate_evidence(b, state, contract, changed)
        result = b.grade(sid, state['packet_id'], self.scores(), [])
        self.assertTrue(result['accepted'], result)
        self.assertTrue(b.status(sid)['accepted'])
        ledger = b.read_json(b.usage_path(sid))
        self.assertEqual([r['reasoning_effort'] for r in sorted(ledger['requests'].values(), key=lambda r: r['started_at'])], ['high', 'max', 'low'])


class ProjectPolicyTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = BridgeTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child

    def test_queued_policy_survives_config_change_and_reassignment(self):
        pid = self.create([self.task('a', reasoning_effort='high'), self.task('b')], strategy='single')
        frozen = copy.deepcopy(self.state(pid)['tasks'][0]['reasoning_policy'])
        b.write_json(b.CONFIG, {**config(), 'dynamic_reasoning_enabled': False})
        self.start(pid)
        state = self.child(pid, 'a')
        contract = b.read_json(b.run_dir(state['session_id']) / 'contract.json')
        self.assertEqual(contract['reasoning_policy'], frozen)
        self.assertEqual(state['reasoning_decision']['effective_effort'], 'high')
        state.update(phase='error'); b.write_json(b.run_dir(state['session_id']) / 'state.json', state)
        b.project_manager().reassign(pid, 'a', 'Recreate the failed executor with a higher initial effort', reasoning_effort='max')
        task = self.state(pid)['tasks'][0]
        self.assertEqual(task['reasoning_policy']['allowed_efforts'], frozen['allowed_efforts'])
        self.assertEqual(task['reasoning_policy']['initial_effort'], 'max')
        self.assertEqual(b.read_json(b.run_dir(state['session_id']) / 'contract.json'), contract)

    def test_queued_fixed_task_does_not_acquire_new_default_policy(self):
        b.write_json(b.CONFIG, {**config(), 'dynamic_reasoning_enabled': False})
        pid = self.create([self.task('a')])
        b.write_json(b.CONFIG, config()); self.start(pid)
        state = self.child(pid, 'a')
        self.assertNotIn('reasoning_policy', b.read_json(b.run_dir(state['session_id']) / 'contract.json'))

    def test_project_child_choice_and_failed_validation_refund_slot(self):
        pid = self.create([self.task('a')]); self.start(pid)
        state = self.child(pid, 'a'); state.update(phase='responded', thread_id=OTHER)
        b.write_json(b.run_dir(state['session_id']) / 'state.json', state)
        before = self.state(pid)['jobs_started']
        with self.assertRaises(providers.ProviderError):
            b.start_job(state['session_id'], 'send', 'Verify.', reasoning_effort='high')
        self.assertEqual(self.state(pid)['jobs_started'], before)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            result = b.start_job(state['session_id'], 'send', 'Verify.', reasoning_effort='high', reasoning_reason='Scoped child repair')
        self.assertEqual(result['reasoning_decision']['effective_effort'], 'high')
        self.assertEqual(self.state(pid)['jobs_started'], before + 1)


class RelayTests(unittest.TestCase):
    def test_selected_effort_overrides_worker_and_survives_adapters(self):
        for protocol in ('chat', 'responses', 'generic'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                def reply(request, number):
                    value = completion() if protocol == 'chat' else {
                        'model': request['model'], 'status': 'completed', 'output': [],
                        'usage': {'input_tokens': 11, 'output_tokens': 7, 'total_tokens': 18}}
                    return 200, {'Content-Type': 'application/json'}, json.dumps(value).encode()
                with server(reply) as (port, seen):
                    cfg = local_config()
                    cfg['local_executor']['endpoints']['one'] = spec(port, 'chat' if protocol == 'chat' else 'responses')
                    binding = providers.resolve_executor(cfg, provider='local', reasoning_effort='high')
                    if protocol == 'generic':
                        binding = {**binding, 'provider': 'custom'}
                    ledger = UsageLedger(Path(directory) / 'usage.json', None)
                    with model_relay(binding['base_url'], '', binding=binding, reasoning_effort='high',
                                     usage_ledger=ledger) as (url, token, _):
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        def send(payload, suffix='/responses'):
                            request = urllib.request.Request(url + suffix, data=json.dumps(payload).encode(),
                                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                            with opener.open(request, timeout=5) as response:
                                return response.read()
                        payload = {'model': binding['model'], 'reasoning': {'effort': 'low'},
                                   'input': [{'role': 'user', 'content': 'Verify.'}], 'stream': False}
                        send(payload)
                        for bad in ({**payload, 'reasoning': []}, {**payload, 'model': 'wrong-model'}):
                            with self.assertRaises(urllib.error.HTTPError) as caught:
                                send(bad)
                            self.assertEqual(caught.exception.code, 400)
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            send(payload, '/responses/compact')
                        self.assertEqual(caught.exception.code, 404)
                    self.assertEqual(len(seen), 1)
                    wire = seen[0]['body']
                    self.assertEqual(wire['reasoning_effort'] if protocol == 'chat' else wire['reasoning']['effort'], 'high')
                    self.assertEqual(seen[0]['path'], '/v1/chat/completions' if protocol == 'chat' else '/v1/responses')
                    data = json.loads((Path(directory) / 'usage.json').read_text())
                    self.assertEqual([r['reasoning_effort'] for r in data['requests'].values()], ['high'])
                    ledger.ensure_known()


class MeetingPolicyTests(unittest.TestCase):
    tearDown = MeetingTests.tearDown
    open = MeetingTests.open
    start = MeetingTests.start
    finish = MeetingTests.finish
    project = MeetingTests.project
    child = MeetingTests.child
    no_workers = MeetingTests.no_workers

    def setUp(self):
        MeetingTests.setUp(self)
        b.write_json(b.CONFIG, config())

    def test_targeted_followup_records_choice_and_replay_is_stable(self):
        mid = self.open(); self.start(mid); old = self.finish(mid, 'p0'); self.finish(mid, 'p1')
        args = {'meeting_id': mid, 'request_id': 'effort-review', 'decision_reason': 'Resolve one small question',
                'messages': [{'participant_id': 'p0', 'message': 'Verify signed inputs.',
                              'reasoning_effort': 'medium', 'reasoning_reason': 'Scoped review'}]}
        with self.no_workers():
            b.call_tool('telos_meeting_round', args)
            self.assertTrue(b.call_tool('telos_meeting_round', args)['duplicate'])
        current = self.child(mid, 'p0')
        self.assertEqual(current['thread_id'], old['thread_id'])
        self.assertEqual(current['reasoning_decision']['effective_effort'], 'high')
        self.assertEqual(current['reasoning_decision']['requested_effort'], 'medium')
        changed = copy.deepcopy(args); changed['messages'][0]['reasoning_effort'] = 'max'
        with self.assertRaises(b.GateError):
            b.call_tool('telos_meeting_round', changed)

    def test_queued_followup_keeps_selection_until_capacity_returns(self):
        mid = self.open(max_parallel=1); self.start(mid); old = self.finish(mid, 'p0')
        args = {'meeting_id': mid, 'request_id': 'queued-effort', 'decision_reason': 'Focused follow-up',
                'messages': [{'participant_id': 'p0', 'message': 'Verify signed inputs.',
                              'reasoning_effort': 'low', 'reasoning_reason': 'One concrete check'}]}
        with self.no_workers():
            b.call_tool('telos_meeting_round', args)
            self.assertTrue(b.call_tool('telos_meeting_round', args)['duplicate'])
        pending = self.project(mid)['tasks'][0]['discussion_pending']
        self.assertEqual(pending['reasoning_effort'], 'low')
        self.assertEqual(self.child(mid, 'p0')['reasoning_decision'], old['reasoning_decision'])
        self.finish(mid, 'p1')
        self.assertEqual(self.child(mid, 'p0')['reasoning_decision']['effective_effort'], 'low')


if __name__ == '__main__':
    unittest.main()

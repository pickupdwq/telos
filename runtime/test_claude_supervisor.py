"""Claude Code as the original supervisor: identity, policy, hooks and wake-up."""
import io
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

import claude_supervisor as cs
from test_telos import b, BridgeTests

SESSION = '2313d2dd-4220-4a36-bdb0-60f2292b077f'
OTHER = '3313d2dd-4220-4a36-bdb0-60f2292b077f'
TOOL = 'toolu_0187xzGoApvu9quyhdgfgW3K'


def hook(event, **fields):
    return io.StringIO(json.dumps({'hook_event_name': event, 'session_id': SESSION, **fields}))


class ClaudeSupervisorTests(unittest.TestCase):
    create = BridgeTests.create

    def setUp(self):
        BridgeTests.setUp(self)
        self.env = {'CLAUDE_CODE_SESSION_ID': SESSION, 'CLAUDE_PROJECT_DIR': str(self.repo)}
        self.spawn = patch.object(b, 'spawn_dispatch')
        self.spawn.start()

    def tearDown(self):
        self.spawn.stop()
        cs.CURRENT.clear()
        BridgeTests.tearDown(self)

    def attest(self, tool=TOOL, effort='xhigh', model='claude-opus-5-5', agent=None):
        out = io.StringIO()
        if model:
            cs.hook_main(b, hook('PostModelSwitch', to_model=model), self.env, out=out)
        fields = {'tool_use_id': tool, 'tool_name': 'mcp__plugin_telos_telos__telos_open',
                  'effort': {'level': effort}}
        if agent:
            fields['agent_id'] = agent
        cs.hook_main(b, hook('PreToolUse', **fields), self.env, out=out)
        return out.getvalue()

    def call(self, name, args, tool=TOOL):
        with patch.dict(os.environ, self.env):
            return b.call_tool(name, args, {'claudecode/toolUseId': tool, 'progressToken': 1})

    def open_args(self):
        return {'repo': str(self.repo), 'task': 'Implement addition.', 'questions': self.q, 'checks': self.checks,
                'skip_meeting_reason': 'Claude supervisor fixture.'}

    def open(self):
        self.attest()
        return self.call('telos_open', self.open_args())

    def test_opus_xhigh_session_becomes_owner(self):
        out = self.open()
        state = b.read_json(b.run_dir(out['session_id']) / 'state.json')
        owner = state['owner']
        self.assertEqual(owner['transport'], 'claude_hook')
        self.assertEqual(owner['thread_id'], SESSION)
        self.assertEqual((owner['model'], owner['effort']), ('claude-opus-5-5', 'xhigh'))
        self.assertEqual(owner['claude_pid'], os.getppid())
        self.assertEqual(out['return_channel']['owner_thread_id'], SESSION)

    def test_context_suffix_is_the_same_model(self):
        self.attest(model='claude-opus-5-5[1m]')
        out = self.call('telos_open', self.open_args())
        self.assertEqual(b.read_json(b.run_dir(out['session_id']) / 'state.json')['owner']['model'], 'claude-opus-5-5')

    def test_wrong_model_or_effort_is_rejected(self):
        for model, effort in (('claude-sonnet-5-5', 'xhigh'), ('claude-opus-5-5', 'high')):
            with self.subTest(model=model, effort=effort):
                cs.CURRENT.clear()
                self.attest(model=model, effort=effort)
                with self.assertRaises(b.GateError) as caught:
                    self.call('telos_open', self.open_args())
                self.assertEqual(caught.exception.code, 'supervisor_model_not_allowed')

    def test_configured_policy_and_disable(self):
        b.write_json(b.CONFIG, {**b.DEFAULT, 'claude_supervisor': {'models': ['claude-sonnet-5-5'], 'efforts': ['high']}})
        self.attest(model='claude-sonnet-5-5', effort='high')
        self.assertTrue(self.call('telos_open', self.open_args())['session_id'])
        b.write_json(b.CONFIG, {**b.DEFAULT, 'claude_supervisor': None})
        self.attest(tool='toolu_disabled0000000001')
        with self.assertRaises(b.GateError) as caught:
            self.call('telos_open', self.open_args(), tool='toolu_disabled0000000001')
        self.assertEqual(caught.exception.code, 'claude_supervisor_disabled')
        with self.assertRaises(b.GateError):
            cs.policy({'claude_supervisor': {'models': ['gpt-6'], 'efforts': ['xhigh']}})

    def test_missing_hooks_fail_closed_and_manual_mode_still_works(self):
        with patch.object(cs, 'ATTESTATION_WAIT', 0.05):
            with self.assertRaises(b.GateError) as caught:
                self.call('telos_open', self.open_args(), tool='toolu_nohook000000000001')
        self.assertEqual(caught.exception.code, 'claude_hooks_missing')
        manual = self.call('telos_open', {**self.open_args(), 'notify': False}, tool='toolu_nohook000000000002')
        self.assertFalse(manual['return_channel']['enabled'])

    def test_attestation_is_single_use_and_bound_to_the_call(self):
        self.attest()
        self.assertTrue(self.call('telos_open', self.open_args())['session_id'])
        with patch.object(cs, 'ATTESTATION_WAIT', 0.05), self.assertRaises(b.GateError) as caught:
            self.call('telos_open', self.open_args())
        self.assertEqual(caught.exception.code, 'claude_hooks_missing')
        self.attest(tool='toolu_other00000000000001')
        with patch.object(cs, 'ATTESTATION_WAIT', 0.05), self.assertRaises(b.GateError):
            self.call('telos_open', self.open_args(), tool='toolu_other00000000000002')

    def test_subagents_are_denied_in_hook_and_server(self):
        decision = json.loads(self.attest(agent='a124a7351c066d499'))
        self.assertEqual(decision['hookSpecificOutput']['permissionDecision'], 'deny')
        # Even if a hook were bypassed, a subagent attestation is refused.
        cs._write(cs.root(b) / 'calls' / (TOOL + '.json'),
                  {'tool_use_id': TOOL, 'claude_pid': os.getppid(), 'agent_id': 'a1', 'model': 'claude-opus-5-5',
                   'effort': 'xhigh', 'env_session': SESSION})
        with self.assertRaises(b.GateError) as caught:
            self.call('telos_open', self.open_args())
        self.assertEqual(caught.exception.code, 'subagent_not_supervisor')

    def test_other_session_cannot_act_on_the_task(self):
        sid = self.open()['session_id']
        self.env['CLAUDE_CODE_SESSION_ID'] = OTHER
        self.attest(tool='toolu_other00000000000003')
        with self.assertRaises(b.GateError) as caught:
            self.call('telos_status', {'session_id': sid}, tool='toolu_other00000000000003')
        self.assertEqual(caught.exception.code, 'owner_mismatch')
        with self.assertRaises(b.GateError):
            b.call_tool('telos_status', {'session_id': sid}, {'threadId': OTHER})

    def test_grade_rechecks_the_live_supervisor_model(self):
        sid = self.open()['session_id']
        self.attest(tool='toolu_grade0000000000001', model='claude-sonnet-5-5')
        with patch.dict(os.environ, self.env):
            cs.begin_call(b, {'claudecode/toolUseId': 'toolu_grade0000000000001'})
        state = b.read_json(b.run_dir(sid) / 'state.json')
        from supervisor import verify_owner
        with self.assertRaises(b.GateError) as caught:
            verify_owner(b.bridge_services(), state['owner'])
        self.assertEqual(caught.exception.code, 'supervisor_model_not_allowed')

    def test_model_falls_back_to_transcript_when_no_switch_recorded(self):
        transcript = self.base / 'session.jsonl'
        transcript.write_text(json.dumps({'type': 'assistant', 'message': {'model': 'claude-opus-5-5'}}) + '\n'
                              + json.dumps({'type': 'user', 'message': {'content': 'x'}}) + '\n')
        cs.hook_main(b, hook('PreToolUse', tool_use_id=TOOL, effort={'level': 'xhigh'},
                             transcript_path=str(transcript)), self.env, out=io.StringIO())
        self.assertEqual(cs._read(cs.root(b) / 'calls' / (TOOL + '.json'))['model'], 'claude-opus-5-5')

    def pending_event(self, sid):
        root = b.run_dir(sid)
        state = b.read_json(root / 'state.json')
        state.update(phase='responded', job_id='a' * 32, job_kind='send', thread_id=OTHER,
                     last_result={'summary': 'PRIVATE RESULT NEVER IN NOTIFICATION'})
        b.record_event(state)
        b.write_json(root / 'state.json', state)
        return state['last_event_id']

    def test_waiter_wakes_claude_once_with_the_event(self):
        sid = self.open()['session_id']
        eid = self.pending_event(sid)
        err = io.StringIO()
        code = cs.wait(b, {'hook_event_name': 'Stop', 'session_id': SESSION}, self.env, err, poll=0.01, limit=2)
        self.assertEqual(code, 2)
        text = err.getvalue()
        self.assertIn(eid, text)
        self.assertIn('telos_receive', text)
        self.assertNotIn('PRIVATE RESULT', text)
        event = b.read_json(b.run_dir(sid) / 'state.json')['events'][eid]
        self.assertEqual((event['status'], event['attempts']), ('delivered', 1))
        # SessionStart of a resumed session re-offers a delivered-but-unreceived event.
        self.assertEqual(cs.wait(b, {'hook_event_name': 'SessionStart', 'session_id': SESSION}, self.env,
                                 io.StringIO(), poll=0.01, limit=0.2), 2)
        # A plain re-arm without a turn boundary does not repeat it.
        state_path = b.run_dir(sid) / 'state.json'
        self.assertEqual(cs.scan(b, lambda o: cs.owns(o, os.getppid(), None, {SESSION}), {}, False), [])
        # After receive, nothing more is delivered.
        self.attest(tool='toolu_receive00000000001')
        self.call('telos_receive', {'session_id': sid, 'event_id': eid}, tool='toolu_receive00000000001')
        self.assertEqual(cs.wait(b, {'hook_event_name': 'Stop', 'session_id': SESSION}, self.env,
                                 io.StringIO(), poll=0.01, limit=0.2), 0)
        self.assertEqual(b.read_json(state_path)['events'][eid]['status'], 'received')

    def test_unreceived_event_stops_after_bounded_redelivery(self):
        sid = self.open()['session_id']
        eid = self.pending_event(sid)
        for _ in range(cs.MAX_DELIVERIES):
            self.assertEqual(cs.wait(b, {'hook_event_name': 'Stop', 'session_id': SESSION}, self.env,
                                     io.StringIO(), poll=0.01, limit=0.2), 2)
        self.assertEqual(cs.wait(b, {'hook_event_name': 'Stop', 'session_id': SESSION}, self.env,
                                 io.StringIO(), poll=0.01, limit=0.2), 0)
        event = b.read_json(b.run_dir(sid) / 'state.json')['events'][eid]
        self.assertEqual(event['status'], 'unacknowledged')
        self.assertEqual(b.return_health(b.read_json(b.run_dir(sid) / 'state.json'))['status'], 'needs_receipt')

    def test_waiter_ignores_other_supervisors_and_paused_tasks(self):
        sid = self.open()['session_id']
        self.pending_event(sid)
        other = lambda owner: cs.owns(owner, -1, 'never', {OTHER})
        self.assertEqual(cs.scan(b, other, {}, True), [])
        b.return_control(sid, 'pause')
        mine = lambda owner: cs.owns(owner, os.getppid(), b.read_json(b.run_dir(sid) / 'state.json')['owner']['claude_start'], set())
        self.assertEqual(cs.scan(b, mine, {}, True), [])

    def test_only_one_waiter_per_claude_and_listening_status(self):
        sid = self.open()['session_id']
        owner = b.read_json(b.run_dir(sid) / 'state.json')['owner']
        self.assertFalse(cs.listening(b, owner))
        result = {}
        worker = threading.Thread(target=lambda: result.update(code=cs.wait(
            b, {'hook_event_name': 'SessionStart', 'session_id': SESSION}, self.env, io.StringIO(), poll=0.01, limit=1.5)))
        worker.start()
        deadline = time.monotonic() + 1
        while not cs.listening(b, owner) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(cs.listening(b, owner))
        self.assertEqual(cs.wait(b, {'hook_event_name': 'Stop', 'session_id': SESSION}, self.env,
                                 io.StringIO(), poll=0.01, limit=0.1), 0)
        worker.join()
        self.assertEqual(result['code'], 0)
        self.assertFalse(cs.listening(b, owner))

    def test_delivery_without_listener_reports_waiting_for_owner(self):
        sid = self.open()['session_id']
        eid = self.pending_event(sid)
        self.assertFalse(b.deliver_once(sid))
        state = b.read_json(b.run_dir(sid) / 'state.json')
        self.assertEqual(state['events'][eid]['status'], 'pending')
        health = b.return_health(state)
        self.assertEqual((health['status'], health['next_action']), ('waiting_for_owner', 'resume_original_claude_session'))

    def test_project_children_inherit_the_claude_owner(self):
        self.attest()
        tasks = [{'id': 'one', 'task': 'Implement addition.', 'message': 'Implement the change and run checks.',
                  'questions': self.q, 'checks': self.checks, 'allowed_paths': ['calc.py'], 'depends_on': []}]
        original = b._start_job

        def start(*args, **kw):  # Real child creation; only the model subprocess is skipped.
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kw)
        with patch.object(b, '_start_job', start):
            out = self.call('telos_project_open', {
                'repo': str(self.repo), 'task': 'Implement addition.', 'questions': self.q, 'checks': self.checks,
                'tasks': tasks, 'decision_reason': 'Single task fixture.', 'skip_meeting_reason': 'Fixture.'})
            b.project_manager().tick(out['project_id'])
        project = b.project_manager().load(out['project_id'])
        self.assertEqual(project['owner']['transport'], 'claude_hook')
        child = b.project_manager().child(project['tasks'][0])
        self.assertIsNotNone(child)
        self.assertEqual(child['owner'], project['owner'])

    def test_plugin_contents(self):
        skill = self.base / 'skill'
        (skill / 'agents').mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: telos\n---\n')
        (skill / '.telos-skill.json').write_text('{}')
        launcher = self.base / 'telos'
        launcher.write_text('#!/bin/sh\n')
        with patch.object(b, 'HOME', self.base):
            base = cs.build_plugin(b.bridge_services(), str(launcher), skill)
        plugin = base / 'plugin'
        hooks = json.loads((plugin / 'hooks' / 'hooks.json').read_text())['hooks']
        self.assertEqual(set(hooks), {'SessionStart', 'Stop', 'PostModelSwitch', 'PreToolUse'})
        self.assertTrue(all(h[0]['hooks'][0]['args'] == ['claude-hook'] for h in hooks.values()))
        self.assertTrue(hooks['Stop'][0]['hooks'][0]['asyncRewake'])
        self.assertEqual(hooks['PreToolUse'][0]['matcher'], cs.TOOL_MATCHER)
        mcp = json.loads((plugin / '.mcp.json').read_text())['mcpServers']['telos']
        self.assertEqual(mcp['args'], ['serve'])
        self.assertTrue((plugin / 'skills' / 'telos' / 'SKILL.md').is_file())
        self.assertFalse((plugin / 'skills' / 'telos' / '.telos-skill.json').exists())
        market = json.loads((base / '.claude-plugin' / 'marketplace.json').read_text())
        self.assertEqual(market['plugins'][0]['source'], './plugin')
        import re
        self.assertTrue(re.fullmatch(cs.TOOL_MATCHER, 'mcp__plugin_telos_telos__telos_open'))
        self.assertTrue(re.fullmatch(cs.TOOL_MATCHER, 'mcp__telos__telos_receive'))
        self.assertFalse(re.fullmatch(cs.TOOL_MATCHER, 'mcp__other__telos_open'))

    def test_run_arguments_default_to_policy(self):
        self.assertEqual(cs.run_args({}, []), ['claude', '--model', 'opus', '--effort', 'xhigh'])
        self.assertEqual(cs.run_args({}, ['--resume', SESSION, '--effort', 'max']),
                         ['claude', '--model', 'opus', '--resume', SESSION, '--effort', 'max'])

    def test_hook_errors_never_fail_the_claude_turn(self):
        self.assertEqual(cs.hook_main(b, io.StringIO('not json'), self.env, out=io.StringIO()), 0)
        with patch.object(cs, 'record_model', side_effect=RuntimeError('boom')):
            self.assertEqual(cs.hook_main(b, hook('PostModelSwitch', to_model='x'), self.env, out=io.StringIO()), 0)

    def test_codex_owners_are_unchanged(self):
        self.assertIsNone(cs.begin_call(b, {'threadId': OTHER}))
        self.assertIsNone(cs.begin_call(b, None))
        self.assertFalse(cs.is_current(OTHER))


if __name__ == '__main__':
    unittest.main()

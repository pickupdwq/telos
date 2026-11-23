"""Project refactoring goes through an actual user confirmation before worker admission."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import claude_supervisor
from desktop_rpc import DesktopRPC
import refactor
import refactor_design
import test_meetings
import test_projects
from test_callbacks import FakeRPC, OWNER, OTHER
from test_telos import b


class RefactorTests(unittest.TestCase):
    setUp = test_meetings.MeetingTests.setUp
    tearDown = test_meetings.MeetingTests.tearDown
    no_workers = test_meetings.MeetingTests.no_workers
    finish = test_meetings.MeetingTests.finish
    child = test_meetings.MeetingTests.child
    project = test_meetings.MeetingTests.project
    state = test_meetings.MeetingTests.project
    scores = test_projects.ProjectTests.scores
    task = test_projects.ProjectTests.task

    def discovery(self):
        return {'source_commit': b.git(self.repo, 'rev-parse', 'HEAD'),
                'walkthrough': [{'paths': ['calc.py'], 'status': 'source_only', 'finding': 'Read add and its callers.'}],
                'current_prd': [{'flow': 'Addition', 'behavior': 'Signed operands produce their sum.', 'sources': ['calc.py']}]}

    def design(self):
        return {'detailed_prd': [{'flow': 'Addition', 'preserve': 'Keep signed sums.', 'internal_changes': 'Separate arithmetic.',
                                 'verify': 'Compare representative sums before and after.'}],
                'structure': [{'path': 'calc.py', 'responsibility': 'Public arithmetic entry point.'}],
                'technology': [{'choice': 'Keep Python', 'rationale': 'No new framework needed for arithmetic.'}],
                'index_path': 'docs/CODEMAP.md', 'code_index': [{'flow': 'Addition', 'paths': ['calc.py']}]}

    def meeting(self):
        caller = {'threadId': OWNER}
        mid = test_meetings.MeetingTests.open(self, caller=caller, notify=True, refactor_discovery=self.discovery())
        test_meetings.MeetingTests.start(self, mid, caller)
        for worker in ('p0', 'p1'):
            self.finish(mid, worker)
        b.call_tool('telos_meeting_conclude', {'meeting_id': mid, 'plan': test_meetings.PLAN,
                    'answers': [], 'rationale': 'Reviewed both independent reports.', 'refactor_design_plan': self.design()}, caller)
        return mid

    def open(self, **changes):
        self.plan = {'scope': ['Only the addition module; no API changes.'],
                     'preserve': ['Existing signed inputs and errors.'],
                     'improve': ['One place to change the addition rule.'],
                     'verify': ['Compare baseline and integrated results on the same inputs.']}
        if 'meeting_id' not in changes:
            changes['meeting_id'] = self.meeting()
        args = {'repo': str(self.repo), 'task': 'Refactor arithmetic', 'tasks': [
                self.task('add', paths=['calc.py', 'docs/CODEMAP.md'], refactor_flows=['Addition'])],
                'questions': self.q, 'checks': self.checks, 'decision_reason': 'One cohesive change',
                'refactor_plan': self.plan, **changes}
        self.owner = {'thread_id': OWNER, 'transport': 'cli_queue', 'socket': '/fixture', 'cwd': str(self.repo)}
        with patch.object(b, 'bind_owner', return_value=self.owner):
            return b.call_tool('telos_project_open', args, {'threadId': OWNER})

    def start(self, pid, **args):
        return b.call_tool('telos_project_start', {'project_id': pid, **args}, {'threadId': OWNER})

    def test_no_child_before_confirmation_even_on_resume(self):
        proposal = self.open()
        pid = proposal['project_id']
        for view in [proposal, self.start(pid), b.project_manager().control(pid, 'resume'),
                     b.project_manager().status(pid, detail='summary')]:
            self.assertFalse(view['proceed'])
            self.assertEqual(view['next_action'], 'ask_user_to_confirm_refactor')
            self.assertEqual(view['refactor']['plan'], self.plan)
            self.assertEqual(view['jobs_started'], 0)
        task = b.project_manager().load(pid)['tasks'][0]
        self.assertFalse(b.run_dir(task['session_id']).exists())
        with patch('refactor.latest_exchange', return_value=None), self.assertRaises(b.GateError):
            self.start(pid, confirm_refactor=proposal['refactor']['revision'])
        with self.assertRaises(b.GateError):
            b.call_tool('telos_project_start', {'project_id': pid, 'confirm_refactor': proposal['refactor']['revision']},
                        {'threadId': OTHER})
        for reply, prompt in [('不行', ''), ('确认，但先别动接口', proposal['refactor']['confirm_text']),
                              ('好', 'unrelated question')]:
            with patch('refactor.latest_exchange', return_value=('u1', reply, prompt)), self.assertRaises(b.GateError):
                self.start(pid, confirm_refactor=proposal['refactor']['revision'])
        self.assertEqual(b.project_manager().load(pid)['jobs_started'], 0)

    def test_reply_releases_only_reviewed_batches(self):
        view = self.open()
        pid, info = view['project_id'], view['refactor']
        turns = [{'id': 'second', 'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': '好'}]}]},
                 {'id': 'first', 'items': [{'type': 'agentMessage', 'text': '范围：算术。请回复：' + info['confirm_text']}]}]
        original = b._start_job
        def spawn(*args, **kwargs):
            with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                return original(*args, **kwargs)
        with patch.object(b, 'owner_rpc', return_value=FakeRPC({'calls': [], 'turns': turns})), patch.object(
                b, 'bind_owner', return_value=self.owner), patch.object(b, '_start_job', side_effect=spawn), patch.object(b, 'spawn_dispatch'):
            started = self.start(pid, confirm_refactor=info['revision'])
            self.assertTrue(started['refactor']['confirmed'])
            self.assertEqual(started['jobs_started'], 1)
            self.assertEqual(self.start(pid, confirm_refactor=info['revision'])['jobs_started'], 1)
        project = b.project_manager().load(pid)
        self.assertEqual(sum(item.get('action') == 'refactor_confirmed' for item in project['history']), 1)
        child = b.read_json(b.run_dir(project['tasks'][0]['session_id']) / 'contract.json')
        self.assertIn(self.plan['preserve'][0], child['context'])
        self.assertIn(refactor_design.index_block(self.design()), child['context'])
        updated = b.project_manager().update(pid, 'Include another maintained module',
                    add_tasks=[self.task('other', refactor_flows=['Addition'])])
        self.assertFalse(updated['refactor']['confirmed'])
        self.assertNotEqual(info['revision'], updated['refactor']['revision'])
        with self.assertRaises(b.GateError):
            self.start(pid, confirm_refactor=info['revision'])
        self.assertEqual(self.start(pid)['jobs_started'], 1)
        new_info = updated['refactor']
        with patch('refactor.latest_exchange', return_value=('new', new_info['confirm_text'], '')), patch(
                'projects.Projects.capacity', return_value=False):
            confirmed = self.start(pid, confirm_refactor=new_info['revision'])
        self.assertTrue(confirmed['refactor']['confirmed'])
        self.assertEqual(confirmed['refactor']['revision'], new_info['revision'])
        self.assertEqual(confirmed['refactor']['documents'], info['documents'])
        self.assertNotIn('design', confirmed['refactor'])

    def test_pause_during_conversation_read_prevents_dispatch(self):
        view = self.open()
        pid, info = view['project_id'], view['refactor']
        def read_and_pause(*_):
            paused = b.project_manager().control(pid, 'pause')
            self.assertNotEqual(paused.get('next_action'), 'ask_user_to_confirm_refactor')
            return ('u1', info['confirm_text'], '')
        with patch('refactor.latest_exchange', side_effect=read_and_pause), self.assertRaises(b.GateError):
            self.start(pid, confirm_refactor=info['revision'])
        project = b.project_manager().load(pid)
        self.assertNotIn('refactor_approval', project)
        self.assertEqual(project['jobs_started'], 0)

    def test_desktop_code_works_when_assistant_turn_is_not_visible(self):
        view = self.open()
        pid, info = view['project_id'], view['refactor']
        rpc = DesktopRPC.__new__(DesktopRPC)
        rpc.thread_id = OWNER
        rpc.sock = None
        rpc.state = {'turns': [{'turnId': 'host-turn', 'params': {
            'input': [{'type': 'text', 'text': info['confirm_text']}]}}]}
        with patch.object(b, 'owner_rpc', return_value=rpc), patch('projects.Projects.capacity', return_value=False):
            self.assertTrue(self.start(pid, confirm_refactor=info['revision'])['refactor']['confirmed'])
        self.assertEqual(b.project_manager().load(pid)['refactor_approval']['message_id'], 'host-turn:0')

    def test_incomplete_brief_and_ownerless_mode_are_rejected(self):
        with self.assertRaises(b.GateError):
            self.open(refactor_plan={'scope': ['all']})
        with self.assertRaises(b.GateError):
            self.open(notify=False)

    def test_no_direct_or_unrelated_meeting_bypass(self):
        with self.assertRaises(b.GateError) as error:
            self.open(meeting_id=None, skip_meeting_reason='small refactor')
        self.assertEqual(error.exception.code, 'refactor_meeting_required')
        mid = test_meetings.MeetingTests.open(self)
        test_meetings.MeetingTests.start(self, mid)
        for worker in ('p0', 'p1'):
            self.finish(mid, worker)
        test_meetings.MeetingTests.conclude(self, mid)
        with self.assertRaisesRegex(b.GateError, 'used together'):
            self.open(meeting_id=mid)
        mid = self.meeting()
        with self.assertRaisesRegex(b.GateError, 'used together'):
            b.call_tool('telos_open', {'meeting_id': mid, 'repo': str(self.repo), 'task': 'normal task',
                        'questions': self.q, 'checks': self.checks}, {'threadId': OWNER})

    def test_discovery_coverage_and_design_cannot_drop_a_flow(self):
        src = self.discovery()
        for mutation in ({'source_commit': 'old'}, {'current_prd': []}, {'walkthrough': [
                {'paths': ['calc.py'], 'status': 'unverified', 'finding': 'Unknown.'}]}):
            with self.assertRaises(b.GateError):
                refactor_design.validate_discovery(b, self.repo, src['source_commit'], {**src, **mutation})
        (self.repo / 'legacy.py').write_text('data = 10')
        b.git(self.repo, 'add', 'legacy.py')
        b.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'legacy')
        src = self.discovery()
        with self.assertRaisesRegex(b.GateError, 'legacy.py'):
            refactor_design.validate_discovery(b, self.repo, src['source_commit'], src)
        design = self.design()
        design['detailed_prd'][0]['flow'] = 'Different new feature'
        with self.assertRaisesRegex(b.GateError, 'same business flows'):
            refactor_design.validate_design(src, design)

    def test_artifacts_are_readable_and_integrated_index_cannot_point_nowhere(self):
        view = self.open()
        info = view['refactor']
        self.assertEqual(info['design'], self.design())
        self.assertIn('现状 PRD', Path(info['documents']['current_prd']).read_text())
        self.assertIn('Keep Python', Path(info['documents']['design']).read_text())
        design = self.design()
        index = self.repo / design['index_path']
        self.assertFalse(index.exists())
        self.assertEqual(refactor_design.check_index(self.repo, design)['status'], 'failed')
        index.parent.mkdir()
        index.write_text(refactor_design.index_block(design) * 2)
        self.assertEqual(refactor_design.check_index(self.repo, design)['status'], 'failed')
        index.write_text('# Custom introduction\n' + refactor_design.index_block(design))
        self.assertEqual(refactor_design.check_index(self.repo, design)['status'], 'passed')
        (self.repo / 'calc.py').unlink()
        self.assertIn('calc.py', refactor_design.check_index(self.repo, design)['stderr'])
        self.assertIn('calc.py', refactor_design.check_structure(self.repo, design)['stderr'])

    def test_editing_a_displayed_prd_invalidates_confirmation(self):
        view = self.open()
        info = view['refactor']
        Path(info['documents']['design']).write_text('Different architecture')
        with patch('refactor.latest_exchange', return_value=('u1', info['confirm_text'], '')), self.assertRaises(b.GateError) as error:
            self.start(view['project_id'], confirm_refactor=info['revision'])
        self.assertEqual(error.exception.code, 'refactor_document_changed')
        self.assertEqual(self.project(view['project_id'])['jobs_started'], 0)

    def test_full_product_flow_checks_the_shipped_code_map_at_integration(self):
        for correct in (False, True):
            with self.subTest(correct=correct):
                design = self.design()
                if not correct:
                    design['code_index'][0]['paths'].append('missing.py')
                with patch.object(self, 'design', return_value=design):
                    view = self.open()
                pid, info = view['project_id'], view['refactor']
                with self.no_workers(), patch('refactor.latest_exchange', return_value=('u1', info['confirm_text'], '')):
                    started = self.start(pid, confirm_refactor=info['revision'])
                self.assertEqual(started['jobs_started'], 1)
                contents = refactor_design.index_block(info['design'])
                test_projects.ProjectTests.submit(self, pid, 'add', {'docs/CODEMAP.md': contents})
                pm = b.project_manager()
                pm.integrate(pid)
                review = test_projects.ProjectTests.check_sync(self, pid)
                check = next(row for row in review['evidence']['checks'] if row['id'] == 'telos_refactor_code_index')
                self.assertEqual(check['status'], 'passed' if correct else 'failed')
                layout = next(row for row in review['evidence']['checks'] if row['id'] == 'telos_refactor_structure')
                self.assertEqual(layout['status'], 'passed')
                if correct:
                    result = pm.finish(pid, review['packet_id'], self.scores(), [], 'Reviewed integrated behavior and navigation.')
                    self.assertTrue(result['accepted'])
                else:
                    self.assertIn('missing.py', check['stderr'])
                    with self.assertRaises(b.GateError):
                        pm.finish(pid, review['packet_id'], self.scores(), [], 'Cannot accept a broken index.')

    def test_legacy_unconfirmed_plan_requires_design_but_existing_approval_survives(self):
        view = self.open()
        p = self.project(view['project_id'])
        del p['contract']['planning']
        del p['tasks'][0]['refactor_flows']
        p['contract_hash'] = b.digest(p['contract'])
        b.project_manager().save(p)
        pending = self.start(p['project_id'])
        self.assertEqual(pending['next_action'], 'telos_meeting_open')
        with self.assertRaisesRegex(b.GateError, 'predates'):
            self.start(p['project_id'], confirm_refactor=pending['refactor']['revision'])
        p['refactor_approval'] = {'signature': pending['refactor']['revision']}
        self.assertTrue(refactor.proposal(b, p)['confirmed'])

    def test_explicit_command_cannot_fall_back_to_unconfirmed_work(self):
        for name in ('telos_open', 'telos_project_open', 'telos_meeting_open'):
            field = 'requirement' if name == 'telos_meeting_open' else 'task'
            with self.subTest(name=name), self.assertRaises(b.GateError) as failed:
                b.call_tool(name, {field: '请使用telos对这个项目进行重构'}, {'threadId': OWNER})
            self.assertEqual(failed.exception.code, 'refactor_brief_required')
        self.assertFalse(refactor.explicit_request('Explain whether we should refactor this project.'))

    def test_claude_tool_result_is_not_user_consent(self):
        path = self.base / 'transcript.jsonl'
        def row(kind, content):
            return {'type': kind, 'sessionId': OWNER, 'uuid': 'message-' + kind,
                    'message': {'role': kind, 'content': content}}
        rows = [row('assistant', [{'type': 'text', 'text': '确认重构 123456789abc'}]),
                row('user', '不同意'), row('user', [{'type': 'tool_result', 'content': '确认重构 123456789abc'}])]
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        with patch.dict(claude_supervisor.CURRENT, {'session': OWNER, 'attestation': {'transcript_path': str(path)}}):
            self.assertEqual(refactor.latest_exchange(b, {'transport': 'claude_hook', 'thread_id': OWNER})[1], '不同意')

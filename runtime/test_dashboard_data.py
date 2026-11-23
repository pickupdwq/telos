import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import activity
from dashboard_data import DashboardData
import timing
from serialization import digest


class DashboardDataTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.board = DashboardData(self.root)
        self.pid, self.first, self.second, self.old = (c * 32 for c in 'abcd')
        self.owner = {'thread_id': 'thread-a', 'transport': 'claude_hook', 'model': 'model-a'}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def project(self, **updates):
        project = {'project_id': self.pid, 'phase': 'running', 'owner': self.owner, 'repo': '/src/foo',
                   'contract': {'task': '账户改版', 'questions': ['PRIVATE']}, 'tasks': [
                       {'session_id': self.first, 'id': 'api', 'task': '提供接口', 'phase': 'pending', 'model': 'foo'},
                       {'session_id': self.second, 'id': 'ui', 'task': '接好前端', 'phase': 'pending',
                        'depends_on': ['api'], 'model': 'bar'}], **updates}
        self.write(self.root / 'projects' / self.pid / 'project.json', project)
        return project

    def run_state(self, sid, **updates):
        state = {'session_id': sid, 'phase': 'accepted', 'owner': self.owner, 'repo': '/src/foo',
                 'model': 'foo', **updates}
        self.write(self.root / 'runs' / sid / 'state.json', state)
        return state

    def test_read_only_dependencies_owner_and_project_confirmation(self):
        self.project(history=[{'previous_session_id': self.old}])
        self.run_state(self.old, project_id=self.pid, continuation='cancelled')
        self.run_state(self.pid, notification_only=True, phase='project_update', project_id=self.pid)
        self.run_state(self.first, project_id=self.pid, phase='awaiting_review')
        initial = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*.json')}
        board = self.board.snapshot()
        self.assertEqual(board['warnings'], [])
        self.assertEqual(len(board['projects']), 1)
        project = board['projects'][0]
        self.assertEqual(project['owner'], {'id': 'thread-a', 'label': 'Claude Code', 'model': 'model-a'})
        self.assertIn('审核', project['wait'])
        self.assertEqual(project['tasks'][1]['wait'], '等前置任务通过：api')
        self.assertNotIn('PRIVATE', repr(board) + repr(self.board.cache))
        self.assertEqual(initial, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*.json')})
        self.run_state(self.first, project_id=self.pid)
        self.run_state(self.second, project_id=self.pid)
        self.assertEqual(self.board.snapshot()['projects'][0]['status'], '待项目确认')
        self.project(phase='integration_failed')
        self.assertEqual(self.board.snapshot()['projects'][0]['tone'], 'issue')
        self.project(phase='accepted')
        self.assertEqual(self.board.snapshot()['projects'][0]['tone'], 'done')
        self.assertIsNone(self.board.detail(self.pid))

    def test_live_activity_and_read_only_stale_detection(self):
        self.run_state(self.first, phase='running', job_pid=os.getpid(), job_started=100)
        log = activity.Recorder(self.root / 'runs' / self.first, self.first)
        log.emit('tool', 'pwd')
        self.assertEqual(self.board.snapshot()['projects'][0]['tasks'][0]['activity'], 'pwd')
        with patch('dashboard_data.process_running', return_value=False):
            self.assertEqual(self.board.snapshot()['projects'][0]['phase'], 'stale')
        self.assertEqual(json.loads((log.directory / 'state.json').read_text())['phase'], 'running')

    def test_cancelled_project_paused_children_and_discussion(self):
        self.project(continuation='cancelled')
        self.assertEqual(self.board.snapshot()['projects'][0]['phase'], 'cancelled')
        self.assertEqual(self.board.snapshot()['projects'][0]['tasks'][0]['phase'], 'cancelled')
        self.run_state(self.first, project_id=self.pid, phase='running', continuation='cancelled', job_pid=os.getpid())
        draining = self.board.snapshot()['projects'][0]
        self.assertEqual(draining['tone'], 'active')
        self.assertEqual(draining['tasks'][0]['status'], '收尾中')
        self.project(continuation='paused')
        self.assertEqual(self.board.snapshot()['projects'][0]['tasks'][1]['phase'], 'paused')
        self.project(phase='discussed', kind='discussion')
        project = self.board.snapshot()['projects'][0]
        self.assertEqual(project['kind'], 'discussion')
        self.assertEqual(project['status'], '讨论已结束')
        self.assertEqual(project['tasks'][0]['phase'], 'discussed')

    def test_legacy_and_new_names_unbound_and_timing(self):
        self.run_state(self.first, owner=None)
        self.run_state(self.second, task_title='新的独立任务', role_name='设计师')
        self.write(self.root / 'runs' / self.first / 'contract.json', {'task': 'MUST_NOT_BE_READ'})
        state = self.run_state(self.old)
        timing.initialize(state, at=1)
        timing.transition(state, 'work', at=2)
        timing.transition(state, 'checks', at=5)
        timing.transition(state, 'supervisor_wait', at=9)
        timing.transition(state, None, at=15)
        self.run_state(self.old, **{k: v for k, v in state.items() if k != 'session_id'})
        projects = {p['id']: p for p in self.board.snapshot()['projects']}
        self.assertEqual(projects[self.first]['title'], 'foo · ' + self.first[:8])
        self.assertEqual(projects[self.first]['owner']['id'], 'unbound')
        self.assertEqual(projects[self.second]['tasks'][0]['role'], '设计师')
        self.assertEqual(projects[self.second]['title'], '新的独立任务')
        self.assertIsNone(projects[self.first]['timing']['work'])
        self.assertEqual(self.board.detail(self.old)['timing'], {'work': 3, 'checks': 4, 'wait': 6})
        self.assertNotIn('MUST_NOT_BE_READ', repr(self.board.cache))

    def test_historical_title_uses_only_original_public_send(self):
        original = '做一个账单筛选。第二句话不需要作为标题'
        self.run_state(self.first, initial_message_hash=digest(original))
        root = self.root / 'runs' / self.first
        self.write(root / ('job-' + 'a'*32 + '-checks.json'), {'kind': 'send', 'message': 'PRIVATE'})
        self.write(root / ('job-' + 'b'*32 + '.json'), {'kind': 'evaluate', 'message': 'PRIVATE'})
        self.write(root / ('job-' + 'c'*32 + '.json'), {'kind': 'send', 'message': 'later handoff'})
        self.assertEqual(self.board.snapshot()['projects'][0]['title_source'], 'id')
        self.write(root / ('job-' + 'd'*32 + '.json'), {'kind': 'send', 'message': original})
        p = self.board.snapshot()['projects'][0]
        self.assertEqual(p['title'], '做一个账单筛选')
        self.assertEqual(p['title_source'], 'initial_message')
        with patch('dashboard_data.json.load', side_effect=AssertionError('uncached read')):
            self.assertEqual(self.board.snapshot()['projects'][0]['tasks'][0]['title'], p['title'])

    def test_sanitized_events_callback_and_partial_tail(self):
        self.run_state(self.first, auto_check={'status': 'passed', 'diagnostic': 'PRIVATE'},
                       events={'last': {'status': 'received', 'received_at': 1}}, last_event_id='last')
        log = activity.Recorder(self.root / 'runs' / self.first, self.first)
        log.emit('command', 'pwd\nPRIVATE stdout\nexit_code=0')
        log.emit('reasoning', 'PRIVATE reasoning')
        log.emit('error', 'PRIVATE Traceback')
        log.phase = 'exam'
        log.emit('message', 'PRIVATE answer')
        log.emit('status', 'PRIVATE exam prompt', event_type='turn.started')
        path = log.directory / activity.FILENAME
        with path.open('ab') as stream:
            stream.write(b'{"version":1')
        result = self.board.detail(self.first)
        self.assertEqual(result['checks'], '检查通过')
        self.assertEqual(result['callback'], '大总管已接收')
        self.assertNotIn('PRIVATE', repr(result))
        labels = [event['text'] for event in result['events']]
        self.assertIn('开始验证', labels)
        self.assertIn('pwd · exit_code=0', labels)
        self.assertIn('大总管已接收结果', labels)

    def test_polling_and_touch_are_not_activity_but_actual_events_are(self):
        project = self.project(created_at=100, updated_at=9000)
        state = self.run_state(self.first, project_id=self.pid, phase='error',
                               created_at=101, job_finished=200, last_event_id='last',
                               events={'last': {'received_at': 210, 'checked_at': 88888}})
        self.run_state(self.second, project_id=self.pid, created_at=100)
        self.assertEqual(self.board.snapshot()['projects'][0]['updated'], 210)
        self.write(self.root / 'projects' / self.pid / 'project.json', {**project, 'updated_at': 999999})
        path = self.root / 'runs' / self.first / 'state.json'
        os.utime(path, (999999, 999999))
        snapshot = self.board.snapshot()['projects'][0]
        self.assertEqual(snapshot['updated'], 210)
        self.assertEqual(snapshot['tasks'][0]['updated'], 210)
        self.write(path, {**state, 'auto_check': {'finished_at': 230}, 'last_control': {'at': 250}})
        self.assertEqual(self.board.snapshot()['projects'][0]['updated'], 250)
        log = path.with_name(activity.FILENAME)
        log.write_text(json.dumps({'version': 1, 'session_id': self.first, 'phase': 'work',
                                   'kind': 'tool', 'text': 'pwd', 'time': 260}) + '\n')
        os.utime(log, (999999, 999999))
        self.assertEqual(self.board.snapshot()['projects'][0]['updated'], 260)
        self.project(created_at=100, stage_timeline={'milestones': {'review': {'at': 280}}})
        self.assertEqual(self.board.snapshot()['projects'][0]['updated'], 280)
        self.run_state(self.pid, notification_only=True, project_id=self.pid, events={'n': {'processed_at': 300}})
        self.assertEqual(self.board.snapshot()['projects'][0]['updated'], 300)
        self.run_state(self.old)
        legacy = self.root / 'runs' / self.old / 'state.json'
        os.utime(legacy, (450, 450))
        lookup = {p['id']: p for p in self.board.snapshot()['projects']}
        self.assertEqual(lookup[self.old]['updated'], 450)

    def test_unblocks_only_when_one_unfinished_dependency_remains(self):
        project = self.project()
        project['tasks'].append({'id': 'ship', 'session_id': self.old, 'phase': 'pending',
                                 'task': '打包', 'depends_on': ['api', 'ui']})
        self.project(tasks=project['tasks'])
        tasks = self.board.snapshot()['projects'][0]['tasks']
        self.assertEqual(tasks[0]['unblocks'], ['ui'])
        self.assertNotIn('unblocks', tasks[1])
        self.run_state(self.first, project_id=self.pid, phase='accepted')
        tasks = self.board.snapshot()['projects'][0]['tasks']
        self.assertNotIn('unblocks', tasks[0])
        self.assertEqual(tasks[1]['unblocks'], ['ship'])

    def test_cache_is_invalidated_on_atomic_replacement_and_tolerates_bad_records(self):
        self.run_state(self.first, phase='ready')
        self.board.snapshot()
        with patch('dashboard_data.json.load', side_effect=AssertionError('uncached read')):
            self.assertEqual(self.board.snapshot()['projects'][0]['phase'], 'ready')
        path = self.root / 'runs' / self.first / 'state.json'
        changed = json.loads(path.read_bytes())
        changed['phase'] = 'error'
        replacement = path.with_suffix('.new')
        self.write(replacement, changed)
        os.replace(replacement, path)
        self.write(self.root / 'runs' / self.old / 'state.json', {'session_id': 'wrong identity'})
        output = self.board.snapshot()
        self.assertEqual(output['projects'][0]['phase'], 'error')
        self.assertTrue(output['warnings'])
        path.unlink()
        path.symlink_to(self.root / 'runs' / self.old / 'state.json')
        self.assertIsNone(self.board.detail(self.first))
        self.assertIsNone(self.board.detail('../' + self.first))
        self.assertEqual(self.board.snapshot()['projects'], [])


if __name__ == '__main__':
    unittest.main()

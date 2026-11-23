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
        self.assertEqual(project['tasks'][1]['wait'], '等前置任务通过：任务一')
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
        self.run_state(self.first, phase='running', job_pid=os.getpid(), job_started=100, execution_started_at=101)
        log = activity.Recorder(self.root / 'runs' / self.first, self.first)
        log.emit('tool', 'pwd')
        self.assertEqual(self.board.snapshot()['projects'][0]['tasks'][0]['activity'], 'pwd')
        with patch('dashboard_data.process_running', return_value=False):
            self.assertEqual(self.board.snapshot()['projects'][0]['phase'], 'stale')
        self.assertEqual(json.loads((log.directory / 'state.json').read_text())['phase'], 'running')

    def test_distinct_repeated_actions_are_stable_and_scoped_to_the_attempt(self):
        current, earlier = '1' * 32, '2' * 32
        state = dict(phase='running', job_pid=os.getpid(), job_started=99,
                     job_id=current, execution_claim=current, execution_started_at=100)
        self.run_state(self.first, **state)
        path = self.root / 'runs' / self.first / activity.FILENAME
        def record(job, kind='tool', text='pwd', phase='work', **extra):
            row = {'version': 1, 'time': 120, 'session_id': self.first, 'job_id': job,
                   'phase': phase, 'kind': kind, 'text': text, **extra}
            with path.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
        def task():
            return self.board.snapshot()['projects'][0]['tasks'][0]
        record(earlier)
        self.assertIsNone(task()['activity_id'])
        record(current)
        first = task()
        self.assertEqual((first['activity_kind'], first['activity_at'], first['run_id']), ('tool', 120, current))
        self.assertEqual(task()['activity_id'], first['activity_id'])
        record(current)  # same text, time, kind, and no native UUID
        second = task()['activity_id']
        self.assertNotEqual(first['activity_id'], second)
        record(earlier, text='old attempt appended late')
        record(current, 'reasoning', 'PRIVATE')
        record(current, 'message', 'PRIVATE', phase='exam')
        self.assertEqual(task()['activity_id'], second)
        record(current, 'files', 'M main.py', id='f' * 32)
        last = task()
        self.assertEqual((last['activity_id'], last['activity_kind'], last['activity']), ('f' * 32, 'files', 'M main.py'))
        self.assertNotIn('PRIVATE', repr(self.board.snapshot()))
        self.run_state(self.first, **{**state, 'phase': 'awaiting_review'})
        self.assertIsNone(task()['activity_id'])

    def test_starting_or_dead_worker_does_not_pose_as_running(self):
        import time
        self.run_state(self.first, phase='running', job_pid=os.getpid(), job_started=time.time(),
                       job_id='1' * 32, execution_claim='2' * 32)
        task = self.board.snapshot()['projects'][0]['tasks'][0]
        self.assertEqual((task['phase'], task['tone']), ('starting', 'wait'))
        self.project(phase='verifying', verification_started=time.time())
        project = next(p for p in self.board.snapshot()['projects'] if p['kind'] == 'project')
        self.assertEqual((project['phase'], project['tone']), ('verification_starting', 'wait'))
        self.project(phase='verifying', continuation='paused', verification_started=1, verification_pid=os.getpid())
        finishing = next(p for p in self.board.snapshot()['projects'] if p['kind'] == 'project')
        self.assertEqual((finishing['phase'], finishing['status']), ('verifying', '项目检查收尾中'))
        self.project(phase='verifying', verification_started=1, verification_pid=os.getpid())
        with patch('dashboard_data.process_running', return_value=False):
            projects = self.board.snapshot()['projects']
        self.assertTrue(all(p['phase'] == 'stale' and p['tone'] == 'issue' for p in projects))

    def test_cancelled_project_paused_children_and_discussion(self):
        self.project(continuation='cancelled')
        self.assertEqual(self.board.snapshot()['projects'][0]['phase'], 'cancelled')
        self.assertEqual(self.board.snapshot()['projects'][0]['tasks'][0]['phase'], 'cancelled')
        self.run_state(self.first, project_id=self.pid, phase='running', continuation='cancelled',
                       job_pid=os.getpid(), job_started=100, execution_started_at=101)
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

    def test_run_window_belongs_to_the_current_worker_attempt(self):
        current, previous = '1'*32, '2'*32
        fields = {'job_id': current, 'job_pid': os.getpid(), 'job_started': 200, 'created_at': 10}
        def window(**values):
            self.run_state(self.first, **{**fields, **values})
            task = self.board.snapshot()['projects'][0]['tasks'][0]
            detail = self.board.detail(self.first)
            pair = ('run_started_at', 'run_finished_at')
            self.assertEqual([task[k] for k in pair], [detail[k] for k in pair])
            return tuple(task[k] for k in pair)
        self.assertEqual(window(phase='running', execution_claim=previous, execution_started_at=100,
                                job_finished=180), (None, None))
        self.assertEqual(window(phase='running', execution_claim=current, execution_started_at=205,
                                job_finished=180), (205, None))
        self.assertEqual(window(phase='running', execution_claim=current, execution_started_at=205,
                                job_finished=320), (205, None))
        self.assertEqual(window(phase='error', execution_claim=current, execution_started_at=205,
                                job_finished=260), (205, 260))
        self.assertEqual(window(phase='error', execution_claim=current, execution_started_at=205,
                                job_finished=180), (205, None))
        self.assertEqual(window(phase='error', execution_claim=current, execution_started_at=205,
                                job_finished=260, last_error='Background process exited without a result; no acceptance'),
                         (205, None))
        self.assertEqual(window(phase='accepted', execution_started_at=210, job_finished=300), (210, 300))
        self.assertEqual(window(phase='ready', execution_started_at=110, job_finished=300), (None, None))
        self.assertEqual(window(phase='ready', finished_at=9000), (None, None))
        timeline = {'intervals': {current + ':environment': {'started_at': 208},
                                  previous + ':environment': {'started_at': 110}}}
        self.assertEqual(window(phase='responded', stage_timeline=timeline, job_finished=250), (208, 250))

    def test_project_verification_time_and_unstarted_assignment(self):
        self.project(phase='verifying', verification_started=70, verification_pid=os.getpid())
        project = self.board.snapshot()['projects'][0]
        self.assertEqual(project['verification_started_at'], 70)
        self.assertIsNone(project['tasks'][0]['run_started_at'])
        self.assertIsNone(project['tasks'][0]['run_finished_at'])
        self.project(phase='running', verification_started=70)
        self.assertIsNone(self.board.snapshot()['projects'][0]['verification_started_at'])

    def test_chinese_labels_survive_reassign_remove_append_and_keep_raw_keys(self):
        project = self.project()
        rows = project['tasks']
        rows[0]['task'] = 'Implement the API'
        rows[0]['role_snapshot'] = {'name': '资深程序员'}
        self.run_state(self.first, phase='awaiting_review', project_id=self.pid)
        self.project(tasks=rows)
        p = self.board.snapshot()['projects'][0]
        self.assertEqual([t['display_id'] for t in p['tasks']], ['任务一', '任务二'])
        self.assertEqual([t['display_title'] for t in p['tasks']], ['任务一', '接好前端'])
        self.assertEqual(p['tasks'][0]['title'], 'Implement the API')
        self.assertIn('任务一', p['wait'])
        self.assertNotIn('api', p['tasks'][1]['wait'])
        self.assertEqual(p['tasks'][0]['unblocks'], ['ui'])
        rows[0]['session_id'] = self.old
        self.project(tasks=rows)
        self.assertEqual(self.board.snapshot()['projects'][0]['tasks'][0]['display_id'], '任务一')
        # Native project updates replace in place and append without reordering;
        # a new observer reconstructs the same numbering from the manifest.
        fresh = DashboardData(self.root).snapshot()['projects'][0]
        self.assertEqual([t['display_id'] for t in fresh['tasks']], ['任务一', '任务二'])
        addition = {'id': 'tests', 'session_id': 'e'*32, 'task': 'Test newly added screen', 'phase': 'pending'}
        self.project(tasks=[rows[1], addition], history=[{'previous_session_id': self.first}])
        tasks = self.board.snapshot()['projects'][0]['tasks']
        self.assertEqual([t['display_id'] for t in tasks], ['任务二', '任务三'])
        self.assertEqual(tasks[0]['wait'], '等前置任务通过：未找到的前置任务')
        self.assertEqual(tasks[1]['key'], 'tests')
        addition['id'] = '用例回归'
        self.project(tasks=[addition])
        task = self.board.snapshot()['projects'][0]['tasks'][0]
        self.assertEqual((task['display_id'], task['display_title']), ('用例回归', '用例回归'))

    def test_standalone_localized_titles_are_separate_from_immutable_identity(self):
        self.run_state(self.first, created_at=10, task_title='Patch the API')
        self.run_state(self.second, created_at=20, task_title='新增登录页')
        def names():
            return {p['id']: (p['display_id'], p['display_title'], p['tasks'][0]['display_id'])
                    for p in self.board.snapshot()['projects']}
        self.assertEqual(names()[self.first], ('独立任务一', '独立任务一', '独立任务一'))
        self.assertEqual(names()[self.second], ('独立任务二', '新增登录页', '独立任务二'))
        fresh = DashboardData(self.root).snapshot()['projects']
        self.assertEqual({p['id']: p['display_id'] for p in fresh}, {self.first: '独立任务一', self.second: '独立任务二'})
        (self.root / 'runs' / self.first / 'state.json').unlink()
        self.run_state(self.old, created_at=30)
        self.assertEqual(names()[self.old], ('独立任务三', '独立任务三', '独立任务三'))
        self.assertEqual(names()[self.second][0], '独立任务二')
        self.assertEqual(self.board.detail(self.second)['id'], self.second)

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

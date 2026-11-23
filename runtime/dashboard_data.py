"""A read-only projection of the local Telos state for the browser observer.

Never call runtime status/recovery, load evidence or keep private contracts in
the cache. Project manifests mix public assignments with private criteria; only
their public fields survive parsing. Historical standalone records lack titles.
"""
import json
import os
import re
from pathlib import Path
import stat
import threading
import time

import activity
import continuation
from dashboard_presence import ClaudePresence
from processes import process_running
from serialization import digest
import progress_guard
import timing
from watch import SESSION


# Display, tone, immediate handoff. These describe the recorded lifecycle, not
# a new acceptance decision. Waiting for a supervisor is distinct from working.
STATES = {
    'ready': ('待派发', 'wait', '等大总管派发'),
    'planned': ('已规划', 'wait', '等大总管启动'),
    'pending': ('未开工', 'wait', '等待调度'),
    'starting': ('启动中', 'wait', '等待执行者接手'),
    'verification_starting': ('项目检查启动中', 'wait', '等待项目检查进程启动'),
    'running': ('执行中', 'active', ''),
    'responded': ('已提交', 'wait', '等大总管安排检查'),
    'awaiting_review': ('待审核', 'wait', '等大总管审核'),
    'discussion_result': ('已提交意见', 'wait', '等大总管汇总意见'),
    'needs_clarification': ('待答复', 'wait', '等大总管答复执行者的问题'),
    'checkpoint': ('已保存进度', 'wait', '等大总管安排继续'),
    'not_accepted': ('未通过', 'issue', '等大总管安排返工'),
    'blocked_environment': ('环境阻塞', 'issue', '执行环境需要处理'),
    'protocol_error': ('回复异常', 'issue', '等大总管处理回复格式'),
    'error': ('执行出错', 'issue', '等大总管查看错误'),
    'setup_error': ('启动失败', 'issue', '等大总管处理启动失败'),
    'quarantined': ('记录异常', 'issue', '任务记录需要核对'),
    'missing': ('状态不明', 'issue', '未找到有效的任务状态记录'),
    'stale': ('进程已退出', 'issue', '运行记录尚未更新，请大总管查看'),
    'changed_after_acceptance': ('通过后有变更', 'issue', '需要重新核对结果'),
    'integrated': ('已整合', 'wait', '等大总管安排项目检查'),
    'verifying': ('项目检查中', 'active', ''),
    'awaiting_final_review': ('待项目确认', 'wait', '等大总管确认整个项目'),
    'integration_failed': ('整体验证失败', 'issue', '等大总管处理项目验证结果'),
    'integration_conflict': ('整合冲突', 'issue', '等大总管解决整合冲突'),
    'accepted': ('已通过', 'done', ''),
    'discussed': ('讨论已结束', 'done', ''),
    'retired': ('已退出讨论', 'done', ''),
    'cancelled': ('已取消', 'done', ''),
    'paused': ('已暂停', 'idle', '等大总管恢复'),
}
CALLBACK = {
    'pending': '等待回传', 'sending': '正在回传', 'queued': '已排队，等大总管接收',
    'delivered': '已送达，等大总管确认收到', 'received': '大总管已接收',
    'processed': '大总管已处理', 'uncertain': '回传待确认', 'unacknowledged': '等待大总管确认收到',
    'cancelled': '回传已取消', 'superseded': '已被后续回传替代',
}
CHECKS = {'queued': '检查已排队', 'running': '检查进行中', 'passed': '检查通过',
          'failed': '检查未通过', 'blocked': '检查受阻', 'uncertain': '检查结果待核对'}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=140):
    if not isinstance(value, str):
        return ''
    text = ' '.join(activity.clean(value[:limit * 4]).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _number(value):
    return value if timing.number(value) else 0


HAN = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]')
DIGITS = '零一二三四五六七八九'


def _ordinal(number):
    if number >= 10000:
        return ''.join(DIGITS[int(digit)] for digit in str(number))
    result = ''
    for power, unit in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
        digit, number = divmod(number, power)
        if digit:
            if digit != 1 or power != 10 or result:
                result += DIGITS[digit]
            result += unit
        elif result and number and not result.endswith('零'):
            result += '零'
    return result


def _display_title(title, fallback):
    # A mixed-language business title can legitimately contain names like API
    # or iOS. Don't translate source text or show an English-only technical ID.
    for clause in re.split(r'[。！？\n]|(?<=[.!?])\s+', title or ''):
        text = clause.strip()
        heading, separator, rest = text.partition(':')
        if separator and not HAN.search(heading) and HAN.search(rest):
            return rest.strip()
        if HAN.search(text):
            return text
    return fallback


def _display_id(numbers, key, prefix='任务', stored=''):
    # Keep the manifest's original order, not the changing status/group order.
    # Reassignment keeps the same key. A retired/removed row never gives its
    # number to a new row during this observer's lifetime.
    number = numbers.setdefault(key, len(numbers) + 1)
    for candidate in (stored, key):
        if HAN.search(candidate):
            return candidate
    return prefix + _ordinal(number)


def _run_window(state):
    # job_started is dispatch time, not the moment the background job took
    # ownership. Between retries state can still contain the *previous* claim,
    # execution_started_at and job_finished.
    job = state.get('job_id')
    dispatched = _number(state.get('job_started'))
    started = _number(state.get('execution_started_at'))
    claimed = state.get('execution_claim')
    if claimed:
        if claimed != job:
            return None, None
    elif not dispatched or started < dispatched:
        started = 0  # Historical states have no token; require time ordering.
    if not started and isinstance(job, str) and job:
        intervals = _dict(_dict(state.get('stage_timeline')).get('intervals'))
        started = _latest(intervals.get(job + ':environment'), 'started_at')
    if not started:
        return None, None
    finished = _number(state.get('job_finished'))
    lost = state.get('last_error') == 'Background process exited without a result; no acceptance'
    if state.get('phase') == 'running' or lost or finished < started:
        return started, None
    return started, finished


def _owner(value):
    item = _dict(value)
    transport = item.get('transport')
    label = {'cli_queue': 'Codex CLI', 'desktop_ipc': 'Codex 桌面',
             'claude_hook': 'Claude Code'}.get(transport, '大总管')
    ident = _text(item.get('thread_id'))
    return {'id': ident or 'unbound', 'label': label if ident else '未绑定大总管',
            'model': _text(item.get('model'))}


def _callback(state):
    if not state.get('owner'):
        return '未启用自动回传'
    event = _dict(_dict(state.get('events')).get(state.get('last_event_id')))
    if _dict(event.get('progress')).get('status') == 'needs_manual_continuation':
        return '等原大总管恢复会话'
    if event.get('delivery_blocker') in ('owner_not_listening', 'owner_not_loaded'):
        return '等原大总管重新打开会话'
    return CALLBACK.get(event.get('status'), '尚无回传记录')


def _latest(row, *fields):
    return max((_number(_dict(row).get(key)) for key in fields), default=0)


def _lifecycle_time(data):
    # updated_at/mtime advance even on a query. A transport poll (checked_at)
    # or future retry_at isn't new work either. Keep actual observed transitions.
    latest = max(_latest(data, 'created_at', 'execution_started_at', 'job_started',
                         'job_finished', 'finished_at', 'verification_started'),
                 _latest(data.get('last_control'), 'at'),
                 _latest(data.get('timing'), 'started_at', 'since', 'finished_at'),
                 _latest(data.get('auto_check'), 'queued_at', 'started_at', 'finished_at'),
                 _latest(data.get('delivery'), 'recorded_at'), _latest(data.get('conclusion'), 'at'))
    timeline = _dict(data.get('stage_timeline'))
    for row in _dict(timeline.get('milestones')).values():
        latest = max(latest, _latest(row, 'at'))
    for row in _dict(timeline.get('intervals')).values():
        latest = max(latest, _latest(row, 'started_at', 'finished_at'))
    for event in _dict(data.get('events')).values():
        latest = max(latest, _latest(event, 'created_at', 'queued_at', 'delivered_at', 'received_at',
                                     'processed_at', 'resume_requested_at', 'owner_stalled_at'))
    for field in ('history', 'discussion_rounds'):
        for row in data.get(field, ()):
            latest = max(latest, _latest(row, 'at'))
    return latest


def _run(data, modified):
    check = _dict(data.get('auto_check')).get('status')
    checks_passed = _dict(data.get('acceptance')).get('checks_passed')
    checks = CHECKS.get(check, '暂无检查结果')
    if not check and type(checks_passed) is bool:
        checks = '检查通过' if checks_passed else '检查未通过'
    raw_timing = _dict(data.get('timing'))
    started, finished = _run_window(data)
    pid = data.get('project_id')
    if pid is not None and (not isinstance(pid, str) or not SESSION.fullmatch(pid)):
        raise ValueError('invalid project identity')
    event = _dict(_dict(data.get('events')).get(data.get('last_event_id')))
    milestones = []
    for key, label in [('queued_at', '回传已排队'), ('delivered_at', '结果已送达'), ('received_at', '大总管已接收结果'),
                       ('processed_at', '大总管已处理结果')]:
        if _number(event.get(key)):
            milestones.append({'at': event[key], 'kind': 'status', 'text': label})
    # Do not retain events, provider details, prompts, output, checks or scoring packets.
    return {
        'id': data['session_id'], 'pid': data.get('project_id'), 'courier': bool(data.get('notification_only')),
        'repo': _text(data.get('repo'), 350), 'owner': _owner(data.get('owner')),
        'phase': _text(data.get('phase')), 'control': _text(data.get('continuation')),
        'title': _text(data.get('task_title')), 'stored_label': _text(data.get('display_id')), 'initial_hash': _text(data.get('initial_message_hash')), 'role': _text(data.get('role_name')),
        'model': _text(data.get('model')), 'kind': _text(data.get('job_kind')),
        'worker': data.get('job_pid'), 'start': _text(data.get('job_process_start')),
        'run_id': _text(data.get('job_id'), 80) or None,
        'launched': _number(data.get('job_started')), 'created': _number(data.get('created_at')),
        'run_started_at': started, 'run_finished_at': finished, 'discussed': bool(data.get('discussion_complete')),
        'blocked': progress_guard.blocked(data), 'automatic': bool(continuation.pending(data)),
        'callback': _callback(data), 'milestones': milestones, 'checks': checks, 'check_status': check,
        'timing': {k: raw_timing[k] for k in ('version', 'phase_seconds', 'stage', 'since', 'started_at',
                                               'scheduler_wait_tracked_since') if k in raw_timing},
        'updated': _lifecycle_time(data), 'file_time': modified,
    }


def _assignment(row):
    return {'id': row['session_id'], 'key': row['id'], 'phase': _text(row.get('phase')),
            'title': _text(row.get('task')) or _text(row['id']), 'stored_label': _text(row.get('display_id')),
            'role': _text(row.get('role') or _dict(row.get('role_snapshot')).get('name')),
            'model': _text(row.get('model') or _dict(row.get('executor')).get('model')),
            'deps': [_text(dep) for dep in row.get('depends_on', []) if isinstance(dep, str)],
            'retired': row.get('active') is False, 'start_issue': bool(row.get('start_error'))}


def _project(data, modified):
    return {'id': data['project_id'], 'repo': _text(data.get('repo'), 350), 'owner': _owner(data.get('owner')),
            'kind': 'discussion' if data.get('kind') == 'discussion' else 'project',
            'title': _text(_dict(data.get('contract')).get('task')) or Path(data.get('repo') or '').name,
            'phase': _text(data.get('phase')), 'control': _text(data.get('continuation')),
            'updated': _lifecycle_time(data), 'file_time': modified, 'created': _number(data.get('created_at')),
            'tasks': [_assignment(t) for t in data['tasks'] if SESSION.fullmatch(str(t.get('session_id', '')))],
            'integration_seconds': _number(_dict(data.get('check_timing')).get('observed_seconds')),
            'retired_ids': [h['previous_session_id'] for h in data.get('history', []) if SESSION.fullmatch(str(h.get('previous_session_id', '')))],
            'worker': data.get('verification_pid'), 'start': data.get('verification_process_start'),
            'launched': _number(data.get('verification_started'))}


def _timing(run, now):
    # Use Telos' own phase accounting, without building large request/interval histories.
    view = timing.snapshot({'timing': run.get('timing')}, at=now)['phase_seconds']
    if view is None:
        return dict.fromkeys(('work', 'checks', 'wait'))
    return {'work': view['work'], 'checks': view['checks'] + view['exam'],
            'wait': view['callback_wait'] + view['supervisor_wait'] + view['scheduler_wait']}


def _alive(row, now):
    if row.get('worker'):
        return process_running(row['worker'], row.get('start') or None)
    return bool(row.get('launched') and now - row['launched'] <= 30)


def _effective(control, phase):
    return control if control in ('cancelled', 'paused') else phase


def _task(row, run, project, now, label):
    recorded = row.get('phase')
    phase = run.get('phase') or recorded
    if recorded in ('quarantined', 'changed_after_acceptance'):
        phase = recorded
    if not run and phase not in ('pending', 'starting', 'setup_error', 'quarantined'):
        phase = 'missing'
    if run.get('discussed') or project.get('phase') == 'discussed':
        phase = 'discussed'
    if row.get('retired'):
        phase = 'retired'
    if phase == 'running':
        if not _alive(run, now):
            phase = 'stale'
        elif not run.get('worker') or not run.get('run_started_at'):
            phase = 'starting'
    observer_now = min(now, run['updated'] or run['file_time']) if phase == 'stale' else now
    control = _effective(project.get('control'), run.get('control'))
    draining = phase == 'running' and control in ('cancelled', 'paused')
    if phase not in ('accepted', 'discussed', 'retired') and not draining:
        phase = _effective(control, phase)
    status, tone, wait = STATES.get(phase, ('未知状态', 'idle', ''))
    if phase == 'running':
        status = {'checks': '执行检查', 'evaluate': '验证结果'}.get(run.get('kind'), status)
        if draining:
            status = '收尾中'
            wait = STATES[control][0] + '后续派发，当前执行尚未结束'
    elif phase == 'responded' and run.get('check_status') in ('queued', 'passed', 'failed', 'blocked'):
        wait = {'queued': '等待自动检查', 'passed': '检查已通过，等大总管评审',
                'failed': '检查未通过，等修复', 'blocked': '自动检查受阻' }[run['check_status']]
    elif phase in ('checkpoint', 'protocol_error') and run.get('automatic'):
        status, tone, wait = '待自动继续', 'wait', '等待调度继续执行'
    elif phase in ('ready', 'pending') and row.get('start_issue'):
        status, tone, wait = '派发受阻', 'issue', '等大总管处理派发失败'
    if run.get('blocked') and tone not in ('done', 'idle') and phase != 'running':
        tone, wait = 'issue', '多次尝试没有进展，等大总管调整方案'
    title = row.get('title') or run.get('title')
    role = row.get('role') or run.get('role') or ''
    return {'id': row['id'], 'key': row.get('key', row['id'][:8]), 'title': title,
            'display_id': label, 'display_title': _display_title(title, label),
            'model': run.get('model') or row.get('model') or '', 'role': role,
            'run_id': run.get('run_id'), 'run_started_at': run.get('run_started_at'),
            'run_finished_at': run.get('run_finished_at'), 'activity_id': None,
            'activity_at': None, 'activity_kind': None,
            'phase': phase, 'status': status, 'tone': tone, 'wait': wait, 'updated': run.get('updated') or run.get('file_time') or project.get('created', 0),
            'timing': _timing(run, observer_now)}


def _project_view(project, runs, now, numbers):
    tasks = []
    for row in project['tasks']:
        run = runs.get(row['id'], {})
        if run.get('pid') not in (None, project['id']) or run.get('courier'):
            run = {}
        label = _display_id(numbers, row['key'], stored=row['stored_label'])
        tasks.append(_task(row, run, project, now, label))
    by_key = {t['key']: t for t in tasks}
    for row, task in zip(project['tasks'], tasks):
        if task['phase'] == 'pending':
            waiting = [k for k in row['deps'] if by_key.get(k, {}).get('phase') != 'accepted']
            if waiting:
                labels = [by_key[k]['display_id'] if k in by_key else '未找到的前置任务' for k in waiting]
                task['wait'] = '等前置任务通过：' + '、'.join(labels)
                if len(waiting) == 1 and waiting[0] in by_key:
                    by_key[waiting[0]].setdefault('unblocks', []).append(task['key'])
    phase = _effective(project['control'], project['phase'])
    finishing_check = (project['phase'] == 'verifying' and phase in ('cancelled', 'paused')
                       and project.get('worker') and _alive(project, now))
    if finishing_check:
        phase = 'verifying'
    elif phase == 'verifying':
        if not _alive(project, now):
            phase = 'stale'
        elif not project.get('worker'):
            phase = 'verification_starting'
    status, tone, wait = STATES.get(phase, ('未知状态', 'idle', ''))
    if finishing_check:
        status, wait = '项目检查收尾中', '后续调度已停止，当前项目检查尚未结束'
    if phase in ('cancelled', 'paused') and any(t['tone'] == 'active' for t in tasks):
        status += ' · 收尾中'
        tone, wait = 'active', '后续派发已停止，仍有执行者在收尾'
    if phase == 'running':
        blockers = [t for t in tasks if t['tone'] == 'issue']
        handoffs = [t for t in tasks if t['tone'] == 'wait' and t['phase'] not in ('pending', 'ready')]
        if tasks and all(t['phase'] == 'accepted' for t in tasks):
            status, tone, wait = '待项目确认', 'wait', '子任务已通过，等大总管整合并确认整个项目'
        elif blockers:
            status, tone, wait = '有任务待处理', 'issue', blockers[0]['display_id'] + '：' + blockers[0]['wait']
        elif any(t['tone'] == 'active' for t in tasks):
            if handoffs:
                wait = handoffs[0]['display_id'] + '：' + handoffs[0]['wait']
        else:
            status, tone = '等待推进', 'wait'
            wait = (handoffs[0]['display_id'] + '：' + handoffs[0]['wait']) if handoffs else '等待任务调度'
    durations = {}
    times = [t['timing'] for t in tasks if t['phase'] != 'pending']
    times.extend(_timing(runs[sid], now) for sid in project['retired_ids'] if sid in runs)
    for stage in ('work', 'checks', 'wait'):
        values = [record[stage] for record in times if record[stage] is not None]
        durations[stage] = sum(values) if values else None
    if project['integration_seconds']:
        durations['checks'] = (durations['checks'] or 0) + project['integration_seconds']
    related = [project['id'], *(t['id'] for t in tasks), *project['retired_ids']]
    updated = max([project['updated'], *(runs.get(sid, {}).get('updated', 0) for sid in related)])
    checked_phases = ('verifying', 'awaiting_final_review', 'accepted', 'integration_failed')
    verification = project['launched'] if project['phase'] in checked_phases else 0
    return {**{k: project[k] for k in ('id', 'title', 'repo', 'kind', 'owner')},
            'verification_started_at': verification or None, 'phase': phase, 'status': status, 'tone': tone, 'wait': wait, 'tasks': tasks,
            'updated': updated or project['file_time'], 'timing': durations}


def _tail_events(stream, sid, limit, reverse=False):
    size = stream.seek(0, os.SEEK_END)
    offset = max(0, size - limit)
    stream.seek(offset)
    raw = stream.read(size - offset)
    lines = raw.split(b'\n')
    trailing = lines.pop()  # Exclude a possibly incomplete record.
    if offset and lines:
        offset += len(lines.pop(0)) + 1
    position = size - len(trailing) if reverse else offset
    for line in reversed(lines) if reverse else lines:
        if reverse:
            position -= len(line) + 1
        line_offset = position
        if not reverse:
            position += len(line) + 1
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(event, dict) and event.get('version') == 1 and event.get('session_id') == sid:
            yield line_offset, event


ACTIONS = ('files', 'command', 'tool', 'message')


def _public_event(event, offset):
    kind = event.get('kind')
    if kind not in (*ACTIONS, 'status', 'error', 'plan'):
        return None
    if event.get('phase') == 'exam':
        if kind != 'status':
            return None
        message = {'turn.started': '开始验证', 'turn.completed': '验证回合结束',
                   'turn.failed': '验证回合失败', 'error': '验证出错'}.get(event.get('event_type'))
    elif event.get('phase') in ('work', 'lifecycle'):
        message = event.get('text')
        if not isinstance(message, str):
            return None
        if kind == 'error':
            message = '执行出错，请在原会话查看详情'
        elif kind in ('tool', 'command'):
            # Do not send arbitrary command output, responses or stack traces.
            first_line = message.partition('\n')[0]
            last_line = message.rpartition('\n')[2]
            message = first_line
            if kind == 'command' and re.fullmatch(r'exit_code=-?\d+', last_line):
                message += ' · ' + last_line
        if 'Traceback (most recent call last)' in message:
            message = '出现异常，请在原会话查看详情'
    else:
        return None
    if not message:
        return None
    cleaned = activity.clean(message[:6000])
    ident = event.get('id')
    if not isinstance(ident, str) or not SESSION.fullmatch(ident):
        # Old records without UUIDs: byte offsets are stable on append and
        # distinguish even repeated identical commands in the same instant.
        ident = digest([event['session_id'], offset, event.get('time'), kind])
    return {'at': _number(event.get('time')), 'kind': kind, 'id': ident,
            'text': cleaned if len(cleaned) <= 2500 else cleaned[:2499] + '…'}


def _activity_summary(stream, sid, run):
    # A compact per-run projection for all subscribers. Usually one short tail
    # supplies both the last public update and current action. Never tokenize a
    # whole transcript just to decide whether an animation should play.
    latest, action = 0, None
    active = run['phase'] == 'running' and run['run_started_at'] is not None
    for limit in (8192, 256 * 1024):
        for offset, event in _tail_events(stream, sid, limit, reverse=True):
            if run['run_id'] != event.get('job_id'):
                continue
            row = _public_event(event, offset)
            if row is None or not row['at']:
                continue
            latest = max(latest, row['at'])
            if not active:
                return latest, None
            if row['at'] < run['run_started_at']:
                return latest, None
            if row['kind'] in ACTIONS:
                action = {**row, 'text': _text(row['text'])}
                return latest, action
        if stream.tell() <= limit:
            break
    return latest, action


def _events(stream, sid):
    # The detail route alone projects multiple events; private records don't
    # displace real actions in the dashboard's bounded history.
    rows = []
    for offset, event in _tail_events(stream, sid, 1024 * 1024):
        row = _public_event(event, offset)
        if row is not None:
            rows.append(row)
    return rows[-80:]


class DashboardData:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.presence = ClaudePresence(self.root)
        self.cache = {}
        self.legacy_titles = {}
        self.activity_summaries = {}
        self.project_numbers = {}
        self.standalone_numbers = {}
        self.lock = threading.Lock()

    def _read(self, path, project=False, events=False, activity_run=None):
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ValueError('unexpected directory link')
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('not a regular record')
        signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if activity_run is not None:
            signature += (activity_run['run_id'], activity_run['run_started_at'], activity_run['phase'])
        cache = self.activity_summaries if activity_run is not None else self.cache
        cached = cache.get(path)
        if cached and cached[0] == signature:
            return cached[1]
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('not a regular record')
            if activity_run is not None:
                latest, action = _activity_summary(stream, path.parent.name, activity_run)
                if (cached and signature[:2] == cached[0][:2] and signature[2] > cached[0][2]
                        and signature[4:] == cached[0][4:]):
                    # Long batches of hidden reasoning or partial lines cannot
                    # erase an earlier observation from the same attempt.
                    latest = max(latest, cached[1][0])
                    action = action or cached[1][1]
                item = latest, action
            elif events:
                item = _events(stream, path.parent.name)
            else:
                data = json.load(stream)
                ident = 'project_id' if project else 'session_id'
                if not isinstance(data, dict) or data.get(ident) != path.parent.name:
                    raise ValueError('record identity mismatch')
                item = _project(data, st.st_mtime) if project else _run(data, st.st_mtime)
        cache[path] = signature, item
        return item

    def _legacy_title(self, sid, expected_hash):
        # The original dispatch is executor-visible. Never open *-checks.json,
        # *-error.json, contract.json or review packets to recover a label.
        root = self.root / 'runs' / sid
        try:
            directory = root.lstat()
            if not stat.S_ISDIR(directory.st_mode) or root.parent.is_symlink():
                return ''
            signature = (directory.st_dev, directory.st_ino, directory.st_mtime_ns, expected_hash)
            cached = self.legacy_titles.get(sid)
            if cached and cached[0] == signature:
                return cached[1]
            jobs = []
            for path in root.glob('job-*.json'):
                if re.fullmatch(r'job-[0-9a-f]{32}\.json', path.name):
                    record = path.lstat()
                    if stat.S_ISREG(record.st_mode):
                        jobs.append((record.st_mtime_ns, path))
            title = ''
            for _, path in sorted(jobs):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        continue
                    request = _dict(json.load(stream))
                message = request.get('message')
                if request.get('kind') != 'send' or not isinstance(message, str):
                    continue
                if expected_hash and digest(message) != expected_hash:
                    continue
                first = re.split(r'[\r\n。！？]|(?<=[.!?])\s+', message.strip(), maxsplit=1)[0]
                title = _text(first)
                if title:
                    break
            self.legacy_titles[sid] = signature, title
            return title
        except (OSError, ValueError, RecursionError):
            return ''

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        projects, runs, seen, unreadable = {}, {}, set(), 0
        for folder, filename, records in [('projects', 'project.json', projects), ('runs', 'state.json', runs)]:
            for path in (self.root / folder).glob('*/' + filename):
                if not SESSION.fullmatch(path.parent.name):
                    continue
                seen.add(path)
                try:
                    records[path.parent.name] = self._read(path, project=folder == 'projects')
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, KeyError, AttributeError, TypeError, RecursionError):
                    unreadable += 1
        for path in list(self.cache):
            if path not in seen and (path.name != activity.FILENAME or path.with_name('state.json') not in seen):
                del self.cache[path]
        for path in list(self.activity_summaries):
            if path.with_name('state.json') not in seen:
                del self.activity_summaries[path]
        for sid, run in runs.items():
            try:
                activity_at, action = self._read(self.root / 'runs' / sid / activity.FILENAME, activity_run=run)
            except (OSError, ValueError, TypeError, RecursionError):
                continue
            runs[sid] = {**run, 'updated': max(run['updated'], activity_at), 'action': action}
        for sid in self.legacy_titles.keys() - runs.keys():
            del self.legacy_titles[sid]
        now = time.time()
        result = [_project_view(p, runs, now, self.project_numbers.setdefault(p['id'], {}))
                  for p in projects.values()]
        claimed = {t['id'] for p in projects.values() for t in p['tasks']}
        claimed.update(sid for p in projects.values() for sid in p['retired_ids'])
        for sid, run in sorted(runs.items(), key=lambda item: (item[1]['created'], item[0])):
            if run['courier'] or sid in claimed:
                continue
            if run['pid']:  # An orphan is not an independent assignment.
                if run['pid'] not in projects:
                    unreadable += 1
                continue
            initial = '' if run['title'] else self._legacy_title(sid, run['initial_hash'])
            title = run['title'] or initial or (Path(run['repo']).name or '独立任务') + ' · ' + sid[:8]
            label = _display_id(self.standalone_numbers, sid, prefix='独立任务', stored=run['stored_label'])
            task = _task({'id': sid, 'title': title}, run, {}, now, label)
            if not run['title'] and not initial:  # Fallback title contains a raw short ID for search only.
                task['display_title'] = label
            result.append({**{k: task[k] for k in ('id', 'title', 'display_id', 'display_title', 'phase', 'status', 'tone',
                                                   'wait', 'updated', 'timing')},
                           'verification_started_at': None,
                           'repo': run['repo'], 'kind': 'standalone', 'owner': run['owner'], 'tasks': [task],
                           'title_source': 'task' if run['title'] else ('initial_message' if initial else 'id')})
        for project in result:
            for task in project['tasks']:
                if task['phase'] != 'running':
                    continue
                event = runs[task['id']].get('action')
                if event is not None:
                    task.update(activity=event['text'], activity_id=event['id'],
                                activity_at=event['at'], activity_kind=event['kind'])
        ranks = {'active': 0, 'issue': 1, 'wait': 2, 'idle': 3, 'done': 4}
        result.sort(key=lambda p: (ranks[p['tone']], -p['updated'], p['id']))
        return {'projects': result, 'supervisors': self.presence.snapshot(),
                'warnings': [f'{unreadable} 条记录缺失或暂时无法读取'] if unreadable else []}

    def _activity(self, sid):
        try:
            return self._read(self.root / 'runs' / sid / activity.FILENAME, events=True)
        except (OSError, ValueError, TypeError, RecursionError):
            return []

    def detail(self, sid):
        if not isinstance(sid, str) or not SESSION.fullmatch(sid):
            return None
        root = self.root / 'runs' / sid
        with self.lock:
            try:
                run = self._read(root / 'state.json')
                if run['courier']:
                    return None
            except (OSError, ValueError, KeyError, AttributeError, TypeError, RecursionError):
                return None
            events = self._activity(sid)
            now = time.time()
            if run['phase'] == 'running' and not _alive(run, now):
                now = min(now, max(run['updated'], *(e['at'] for e in events), 0) or run['file_time'])
            events = sorted([*events, *run['milestones']], key=lambda event: event['at'])[-80:]
            return {'id': sid, 'events': events, 'callback': run['callback'], 'checks': run['checks'],
                    'run_started_at': run['run_started_at'], 'run_finished_at': run['run_finished_at'],
                    'timing': _timing(run, now)}

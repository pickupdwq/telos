"""Project coordination around the existing independent-session acceptance gate.

The original caller chooses the plan. This module enforces it, never grades it.
All coordinator mutations use one cross-process lock; session locks are nested
inside it, never the reverse. No model is called while holding a session lock.
"""
from __future__ import annotations
import copy
import contextlib
import fcntl
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import uuid
from processes import process_start, process_running, reap_background
from identity import canonical_tool_name
from usage import TOKEN_FIELDS, validate_budget


class Projects:
    def __init__(self, bridge):
        self.b = bridge

    @contextlib.contextmanager
    def lock(self):
        self.b.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.b.STATE / 'coordinator.lock').open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def path(self, pid):
        self.b.run_dir(pid)  # validate ID
        return self.b.STATE / 'projects' / pid / 'project.json'

    def load(self, pid):
        p = self.b.read_json(self.path(pid))
        if self.b.digest(p['contract']) != p['contract_hash']:
            raise self.b.GateError('Project acceptance contract changed')
        return p

    def save(self, p):
        p['updated_at'] = time.time()
        self.b.write_json(self.path(p['project_id']), p)

    def authorize(self, p, caller):
        if p.get('owner') and p['owner']['thread_id'] != caller:
            raise self.b.GateError('Project belongs to another supervisor thread')

    def active(self, p):
        if p['continuation'] != 'active':
            raise self.b.GateError('Project is paused/cancelled')
        if p['phase'] == 'accepted':
            raise self.b.GateError('Accepted project is terminal; create a new project')

    def scope(self, paths):
        if not isinstance(paths, list) or not paths:
            raise self.b.GateError('Each task needs nonempty allowed_paths (files or directory prefixes)')
        for path in paths:
            if not isinstance(path, str) or not path or '\\' in path or any(c in path for c in '*?[\x00\n'):
                raise self.b.GateError('allowed_paths must be literal relative paths')
            if path != '.' and (PurePosixPath(path).is_absolute() or any(x in ('', '..', '.', '.git') for x in path.rstrip('/').split('/'))):
                raise self.b.GateError('Unsafe allowed_paths')
        return list(dict.fromkeys(x.rstrip('/') or '.' for x in paths))

    @staticmethod
    def overlap(left, right):
        return any(a == '.' or b == '.' or a == b or a.startswith(b + '/') or b.startswith(a + '/')
                   for a in left for b in right)

    def validate_tasks(self, tasks, existing=()):
        if not isinstance(tasks, list) or not tasks or len(tasks) + len(existing) > 30:
            raise self.b.GateError('Provide 1-30 project tasks in total')
        result = []
        ids = {t['id'] for t in existing}
        for raw in tasks:
            t = dict(raw)
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', t.get('id', '')) or t['id'] in ids:
                raise self.b.GateError('Task IDs must be unique simple identifiers')
            ids.add(t['id'])
            for k in ('task', 'message'):
                if not isinstance(t.get(k), str) or not t[k].strip():
                    raise self.b.GateError('Each task needs task and public message')
            if t.get('task_type', 'implementation') == 'audit':
                import audit
                if t.get('allowed_paths') not in (None, []) or t.get('command_network', False) or any(c.get('network', False) for c in t.get('checks', [])):
                    raise self.b.GateError('Audit children are read-only and use scoped probes, not unrestricted network')
                t['allowed_paths'] = []
                t['audit'] = audit.validate(t.get('audit'))
            else:
                if t.get('task_type', 'implementation') != 'implementation' or t.get('audit') is not None:
                    raise self.b.GateError('Invalid project task type/audit boundaries')
                t['allowed_paths'] = self.scope(t.get('allowed_paths'))
            t.setdefault('depends_on', [])
            if not isinstance(t['depends_on'], list) or not all(isinstance(x, str) for x in t['depends_on']):
                raise self.b.GateError('depends_on must contain task IDs')
            t['depends_on'] = list(dict.fromkeys(t['depends_on']))
            t.setdefault('context', '')
            # Stable accounting identity: unlike session_id this survives the
            # constrained reassignment flow, so task budgets stay cumulative.
            t['usage_id'] = uuid.uuid4().hex
            try:
                configured = t.get('budget') if t.get('budget') is not None else self.b.config().get('task_budget')
                t['budget'] = validate_budget(configured)
            except ValueError as error:
                raise self.b.GateError('Invalid task budget: ' + str(error)) from None
            if any(k in t for k in ('exam_profile', 'exam_provider', 'exam_model', 'exam_reasoning_effort',
                                     'backends', 'executor', 'workflow_version', '_workflow_version')):
                raise self.b.GateError('New project tasks configure one executor; internal/legacy bindings are not task inputs')
            t['executor'] = self.b.providers.resolve_executor(self.b.config(), **{k: t.get(k) for k in (
                'executor_profile', 'provider', 'model', 'reasoning_effort')})
            t['workflow_version'] = 3
            self.backend_fields(t)
            t.setdefault('threshold', self.b.config()['threshold'])
            t.setdefault('max_rounds', self.b.config()['max_rounds'])
            t.setdefault('assessment_mode', 'closed_book')
            if t['assessment_mode'] != 'closed_book' or type(t.get('command_network', False)) is not bool:
                raise self.b.GateError('Invalid task assessment mode or command_network')
            if t.get('review_paths') is not None:
                raise self.b.GateError('Source review belongs to the GPT supervisor; executor exams are closed-book')
            if t.get('dependency_paths') is not None:
                t['dependency_paths'] = self.b.validate_dependencies(t['dependency_paths'])
            self.b.validate_exam(t['questions'], t['checks'], t['threshold'], t['max_rounds'])
            self.b.deny_leak(t['message'], t)
            result.append(t)
        all_tasks = list(existing) + result
        by_id = {t['id']: t for t in all_tasks}
        ancestor_cache = {}
        visiting = set()
        def ancestors(tid):
            if tid in visiting:
                raise self.b.GateError('Task dependency cycle')
            if tid in ancestor_cache:
                return ancestor_cache[tid]
            if tid not in by_id:
                raise self.b.GateError('Unknown dependency: ' + tid)
            visiting.add(tid)
            out = set()
            for dep in by_id[tid]['depends_on']:
                out.add(dep); out.update(ancestors(dep))
            visiting.remove(tid)
            ancestor_cache[tid] = out
            return out
        for tid in by_id:
            ancestors(tid)
        # Unordered overlapping writers are rejected, not silently sequenced.
        for i, a in enumerate(all_tasks):
            for c in all_tasks[i + 1:]:
                if self.overlap(a['allowed_paths'], c['allowed_paths']) and a['id'] not in ancestors(c['id']) and c['id'] not in ancestors(a['id']):
                    raise self.b.GateError(f"Overlapping writers {a['id']} / {c['id']}; add an explicit dependency")
        return result

    def create(self, repo, task, tasks, questions, checks, decision_reason,
               execution_mode='auto', strategy='single', max_parallel=3,
               max_jobs=40, context='', notify=True, caller=None, use_experience=True, dependency_paths=None):
        if execution_mode not in ('auto', 'single', 'parallel') or strategy not in ('single', 'parallel', 'phased'):
            raise self.b.GateError('Invalid execution mode or supervisor strategy')
        if not isinstance(decision_reason, str) or not decision_reason.strip():
            raise self.b.GateError('Supervisor must explain its execution strategy')
        if type(max_parallel) is not int or not 1 <= max_parallel <= 3:
            raise self.b.GateError('max_parallel must be 1-3')
        if type(max_jobs) is not int or not 1 <= max_jobs <= 200:
            raise self.b.GateError('max_jobs must be 1-200')
        tasks = self.validate_tasks(tasks)
        b = self.b
        dependency_paths = b.validate_dependencies([] if dependency_paths is None else dependency_paths)
        b.validate_exam(questions, checks, 90, 5)
        repo = Path(b.git(repo, 'rev-parse', '--show-toplevel')).resolve()
        if b.git(repo, 'status', '--porcelain'):
            raise b.GateError('Source repository must be clean; commit/stash explicitly')
        if not task or not isinstance(task, str):
            raise b.GateError('Nonempty project task required')
        owner = b.bind_owner(caller) if notify else None
        pid = uuid.uuid4().hex
        contract = {'task': task, 'context': context, 'questions': questions, 'checks': checks,
                    'threshold': 90, 'max_rounds': 5, 'dependency_paths': dependency_paths}
        p = {'project_id': pid, 'repo': str(repo), 'base_commit': b.git(repo, 'rev-parse', 'HEAD'),
             'owner': owner, 'execution_mode': execution_mode, 'strategy': strategy,
             'decision_reason': decision_reason, 'max_parallel': max_parallel, 'max_jobs': max_jobs,
             'jobs_started': 0, 'phase': 'planned', 'continuation': 'active', 'tasks': tasks, 'use_experience': use_experience,
             'contract': contract, 'contract_hash': b.digest(contract), 'history': [], 'created_at': time.time()}
        for t in tasks:
            t.update(session_id=uuid.uuid4().hex, phase='pending', revision=1)
        with self.lock():
            # A project has one notification courier; child events stay durable in
            # their own sessions and are acknowledged by project_receive.
            courier = {'session_id': pid, 'project_id': pid, 'notification_only': True,
                       'contract_hash': b.digest(contract), 'phase': 'project_update', 'owner': owner,
                       'continuation': 'active', 'events': {}, 'model': 'project', 'turns': 0, 'rounds': 0}
            b.write_json(b.run_dir(pid) / 'contract.json', contract)
            b.write_json(b.run_dir(pid) / 'state.json', courier)
            self.save(p)
        return self.status(pid)

    def child(self, t):
        path = self.b.run_dir(t['session_id']) / 'state.json'
        return self.b.read_json(path) if path.exists() else None

    def refresh(self, p):
        for t in p['tasks']:
            s = self.child(t)
            if s:
                stat = self.b.status(t['session_id'])
                t['phase'] = stat['phase']
                t['error'] = stat.get('last_error')
                t['accepted_commit'] = stat.get('acceptance', {}).get('commit') if stat.get('accepted') else None
                t['event_id'] = stat.get('return_channel', {}).get('event', {}).get('id') if stat.get('return_channel', {}).get('event') else None
                if s.get('base_commit'):
                    t['base_commit'] = s['base_commit']
            elif t['phase'] == 'starting':
                t.update(phase='setup_error', error='Interrupted task setup; inspect workspace, then replace this unstarted task')
        return p

    def _usage(self, p):
        totals = {'requests': 0, 'failed_requests': 0, 'unknown_usage_requests': 0,
                  'in_flight_requests': 0, 'input_tokens': 0, 'cached_input_tokens': 0,
                  'uncached_input_tokens': 0, 'output_tokens': 0, 'reasoning_output_tokens': 0,
                  'total_tokens': 0}
        unknown_fields = {key: 0 for key in TOKEN_FIELDS}
        started = 0
        incomplete = False
        unavailable = []
        for t in p['tasks']:
            try:
                state = self.child(t)
            except Exception:
                # A corrupt child state after setup is a started but unavailable
                # ledger; never silently omit it from the aggregate.
                if t.get('phase') != 'pending':
                    started += 1
                    incomplete = True
                    unavailable.append(t['id'])
                continue
            if not state:
                continue
            started += 1
            try:
                _, contract = self.b.load_session(self.b.run_dir(t['session_id']))
                snapshot = self.b.usage_snapshot(state, contract)
            except Exception:
                incomplete = True
                unavailable.append(t['id'])
                continue
            snapshot_totals = snapshot.get('totals') if isinstance(snapshot, dict) else None
            if not isinstance(snapshot_totals, dict):
                incomplete = True
                unavailable.append(t['id'])
                continue
            for key in totals:
                value = snapshot_totals.get(key)
                if type(value) is int:
                    totals[key] += value
            fields = snapshot.get('unknown_fields')
            if isinstance(fields, dict):
                for key in unknown_fields:
                    value = fields.get(key)
                    if type(value) is int:
                        unknown_fields[key] += value
        if not started:
            return None
        totals['unknown_fields'] = unknown_fields
        totals['incomplete'] = incomplete
        totals['unavailable_tasks'] = unavailable
        return totals

    def view(self, p, detail='full'):
        result = {k: p[k] for k in ('project_id', 'phase', 'continuation', 'execution_mode', 'strategy',
                                   'decision_reason', 'max_parallel', 'max_jobs', 'jobs_started', 'base_commit')}
        result['contract_hash'] = p.get('contract_hash')
        result['effective_parallel'] = self.limit(p)
        tasks = []
        for t in p['tasks']:
            item = {k: t.get(k) for k in ('id', 'task', 'depends_on', 'allowed_paths', 'provider', 'model',
                                           'executor_profile', 'workflow_version', 'reasoning_effort',
                                           'session_id', 'phase', 'accepted_commit', 'event_id', 'error',
                                           'budget', 'revision')}
            child = self.child(t)
            if child:
                try:
                    _, contract = self.b.load_session(self.b.run_dir(t['session_id']))
                    item['budget'] = contract.get('budget')
                    item['usage'] = self.b.usage_snapshot(child, contract)
                except Exception:
                    pass
            tasks.append(item)
        result['tasks'] = tasks
        result['usage'] = self._usage(p)
        result['integration'] = p.get('integration')
        if 'final_review' in p:
            result['final_review'] = p['final_review']
        result['accepted'] = p['phase'] == 'accepted'
        courier = self.b.read_json(self.b.run_dir(p['project_id']) / 'state.json')
        events = courier.get('events')
        last_event_id = courier.get('last_event_id')
        event = events.get(last_event_id) if isinstance(events, dict) and last_event_id is not None else None
        result['return_event'] = ({'id': last_event_id, 'status': event.get('status')}
                                  if isinstance(event, dict) else None)
        result['return_health'] = ({'status': 'complete', 'next_action': None} if result['accepted']
                                   else self.b.return_health(courier))
        if detail == 'summary':
            from compact import project as project_view
            return project_view(result)
        result['detail'] = 'full'
        return result

    def status(self, pid, detail='full'):
        if detail not in ('summary', 'full'):
            raise self.b.GateError('detail must be summary or full')
        with self.lock():
            p = self.refresh(self.load(pid))
            if p['phase'] == 'accepted':
                integ = p['integration']
                code_ok = self.b.storage_manager().archived_commit(p, integ['worktree'], integ['commit']) or (self.b.git(integ['worktree'], 'rev-parse', 'HEAD') == integ['commit'] and not self.b.git(integ['worktree'], 'status', '--porcelain'))
                if self.pins(p) != integ['pins'] or not all(t['phase'] == 'accepted' for t in p['tasks']) or not code_ok:
                    p['phase'] = 'changed_after_acceptance'
                else:
                    packet = self.b.read_json(self.path(pid).parent / 'integration-review.json')
                    if self.b.digest(packet) != integ['packet_id']:
                        raise self.b.GateError('Project review evidence changed')
            self.save(p)
            return self.view(p, detail)

    def limit(self, p):
        if p['execution_mode'] == 'single' or (p['execution_mode'] == 'auto' and p['strategy'] == 'single'):
            return 1
        return p['max_parallel']

    def capacity(self, p, provider):
        # All launch paths hold the coordinator lock, including standalone tasks.
        running = []
        for path in (self.b.STATE / 'runs').glob('*/state.json'):
            s = self.b.read_json(path)
            if s.get('phase') == 'running':
                running.append(s)
        if p is not None and sum(s.get('project_id') == p['project_id'] for s in running) >= self.limit(p):
            return False
        if sum(s.get('active_provider', s.get('provider', 'ark')) == provider for s in running) >= self.b.config().get('provider_max_parallel', 3):
            return False
        if p is not None and p['jobs_started'] >= p['max_jobs']:
            raise self.b.GateError('Project job budget exhausted; no more model jobs')
        return True

    def launch(self, p, sid, kind, message='', event_id=None):
        """Reserve before spawning; refund only when no new job was persisted."""
        before = self.b.read_json(self.b.run_dir(sid) / 'state.json')
        p['jobs_started'] += 1
        self.save(p)
        try:
            result = self.b._start_job(sid, kind, message, event_id)
        except Exception:
            # Validation and definite spawn failure leave the old job identity.
            # A new identity (or unreadable state) is ambiguous: retain the slot.
            try:
                current = self.b.read_json(self.b.run_dir(sid) / 'state.json')
            except (OSError, ValueError):
                pass
            else:
                if current.get('job_id') == before.get('job_id'):
                    p['jobs_started'] -= 1
                    self.save(p)
            raise
        if result.get('duplicate'):
            p['jobs_started'] -= 1
            self.save(p)
        return result

    def pins(self, p):
        return {t['id']: t.get('accepted_commit') for t in p['tasks']}

    def merge_workspace(self, p, pins, label):
        b = self.b
        target = b.CACHE / 'projects' / p['project_id'] / (label + '-' + uuid.uuid4().hex[:12])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        b.git(p['repo'], 'worktree', 'add', '--detach', str(target), p['base_commit'])
        pending = [t for t in p['tasks'] if t['id'] in pins]
        done = set()
        while pending:
            t = next(t for t in pending if all(d not in pins or d in done for d in t['depends_on']))
            try:
                b.git(target, '-c', 'user.name=TELOS', '-c', 'user.email=telos@localhost',
                      '-c', 'commit.gpgsign=false', 'merge', '--no-ff', '--no-edit', pins[t['id']])
            except b.GateError:
                conflicts = b.git(target, 'diff', '--name-only', '--diff-filter=U').splitlines()
                if not conflicts:
                    raise
                return str(target), {'task_id': t['id'], 'files': conflicts}
            done.add(t['id']); pending.remove(t)
        return str(target), None

    def dependency_pins(self, p, t):
        by_id = {x['id']: x for x in p['tasks']}
        pins = {}
        visiting = set()
        def visit(tid):
            if tid in pins:
                return
            if tid in visiting:
                raise self.b.GateError('Task dependency cycle')
            dep = by_id[tid]
            if dep['phase'] != 'accepted' or not dep.get('accepted_commit'):
                raise self.b.GateError('Dependency not accepted: ' + tid)
            visiting.add(tid)
            for ancestor in dep['depends_on']:
                visit(ancestor)
            visiting.remove(tid)
            pins[tid] = dep['accepted_commit']
        for tid in t['depends_on']:
            visit(tid)
        return pins

    def tick(self, pid):
        b = self.b
        with self.lock():
            p = self.refresh(self.load(pid))
            if p['continuation'] != 'active' or p['phase'] not in ('running', 'planned'):
                self.save(p); return self.view(p)
            p['phase'] = 'running'
            for t in p['tasks']:
                if t['phase'] not in ('pending', 'ready'):
                    continue
                if any(next(x for x in p['tasks'] if x['id'] == dep)['phase'] != 'accepted' for dep in t['depends_on']):
                    continue
                try:
                    if not self.capacity(p, t['provider']):
                        continue
                    if t['phase'] == 'pending':
                        t['phase'] = 'starting'; self.save(p)
                        pins = self.dependency_pins(p, t)
                        base = p['base_commit']
                        if pins:
                            tree, conflict = self.merge_workspace(p, pins, 'dependency-' + t['id'])
                            if conflict:
                                t.update(phase='setup_error', error='Dependency integration conflict', conflict_worktree=tree)
                                self.save(p); continue
                            base = b.git(tree, 'rev-parse', 'HEAD')
                        b.create_session(repo=p['repo'], task=t['task'], questions=t['questions'], checks=t['checks'],
                                         context=t['context'], model=t['model'], threshold=t['threshold'], max_rounds=t['max_rounds'],
                                         notify=bool(p['owner']), owner_thread_id=p['owner']['thread_id'] if p['owner'] else None,
                                         provider=t['provider'], exam_provider=t.get('exam_provider'), exam_model=t.get('exam_model'),
                                         assessment_mode=t.get('assessment_mode', 'closed_book'), review_paths=t.get('review_paths'),
                                         dependency_paths=t.get('dependency_paths'), command_network=t.get('command_network', False),
                                         use_experience=p.get('use_experience', True),
                                         task_type=t.get('task_type', 'implementation'), audit=t.get('audit'), repair_of=t.get('repair_of'),
                                         _session_id=t['session_id'], _base_commit=base, _project_id=pid,
                                         _usage_id=t.get('usage_id'), _budget=t.get('budget'),
                                         _allowed_paths=t['allowed_paths'], _dependency_pins=pins,
                                         _workflow_version=t.get('workflow_version', 2),
                                         _executor=t.get('executor'),
                                         _backends=None if t.get('workflow_version') == 3 else self.b.backend_bindings(t))
                        t.update(phase='ready', base_commit=base)
                        self.save(p)
                    self.launch(p, t['session_id'], 'send', t['message'])
                    t['phase'] = 'running'
                except Exception as e:
                    t['error'] = str(e)[:500]
                    if t['phase'] == 'starting':
                        t['phase'] = 'setup_error'
                self.save(p)
            self.refresh(p)
            self.publish(p)
            self.save(p)
            return self.view(p)

    def start_child(self, sid, kind, message='', event_id=None):
        b = self.b
        with self.lock():
            s = b.read_json(b.run_dir(sid) / 'state.json')
            if s.get('notification_only'):
                raise b.GateError('Project courier cannot execute tasks')
            p = self.refresh(self.load(s['project_id'])) if s.get('project_id') else None
            if p is not None:
                self.active(p)
                s = b.read_json(b.run_dir(sid) / 'state.json')
            # Idempotency must be checked before reserving another budget slot.
            replay = b.event_action(s, event_id, kind, {'message': message})
            if replay:
                return replay
            provider = s.get('exam_provider', s.get('provider', 'ark')) if kind == 'evaluate' else s.get('provider', 'ark')
            if not self.capacity(p, provider):
                raise b.GateError('Project/provider concurrency limit reached; retry after completion')
            return (self.launch(p, sid, kind, message, event_id) if p is not None
                    else b._start_job(sid, kind, message, event_id))

    def publish(self, p):
        b = self.b
        if not p.get('owner'):
            return
        signature = b.digest({'phase': p['phase'], 'tasks': [(t['id'], t['phase'], t.get('event_id'), t.get('error')) for t in p['tasks']],
                              'integration': p.get('integration', {}).get('packet_id')})
        if signature == p.get('notice_signature'):
            return
        # A running-only transition needs no supervisor action. Keep any
        # outstanding batch (and its existing identity/reminders) alive, but do
        # not create a fresh courier when there is nothing to deliver.
        quiet = (p.get('phase') == 'running' and p.get('continuation') == 'active'
                 and any(t.get('phase') == 'running' for t in p['tasks'])
                 and all(t.get('phase') in ('pending', 'starting', 'running', 'accepted') for t in p['tasks'])
                 and not any(t.get('error') for t in p['tasks']))
        # Never overwrite an in-flight courier event: receive always gathers the
        # latest project state, so one outstanding wake-up covers all completions.
        with b.locked(p['project_id']) as root:
            state, _ = b.load_session(root)
            event = state['events'].get(state.get('last_event_id'), {})
            if event.get('status') not in (*b.WATCH_EVENTS, 'received'):
                if quiet:
                    return
                state.update(job_id=uuid.uuid4().hex, phase='project_update')
                b.record_event(state)
            b.write_json(root / 'state.json', state)
        p['notice_signature'] = signature
        b.spawn_dispatch(p['project_id'])

    def receive(self, pid, event_id=None, detail='full'):
        if detail not in ('summary', 'full'):
            raise self.b.GateError('detail must be summary or full')
        b = self.b
        with self.lock():
            p = self.refresh(self.load(pid))
            stored = (p.get('receipts') or {}).get(event_id) if event_id else None
            # detail=full is a historical read-only lookup. The default summary
            # path is the MCP callback continuation: it may still recover a
            # pending progress reminder for unhandled work.
            if isinstance(stored, dict) and detail == 'full':
                return self._receive_result(self._replay_receipt(stored, event_id, pid), detail)
            if p['continuation'] != 'active' or p['phase'] == 'accepted':
                return self._receive_result({'proceed': False, 'event_id': event_id, 'events': [], 'project': self.view(p), 'project_id': pid}, detail)
            if p.get('owner'):
                receipt = b.receive_event(pid, event_id)
                if not receipt['proceed']:
                    if isinstance(stored, dict):
                        return self._receive_result(self._replay_receipt(stored, event_id, pid), detail)
                    return self._receive_result({'proceed': False, 'event_id': event_id, 'events': [], 'project': self.view(p), 'project_id': pid}, detail)
            events = []
            for t in p['tasks']:
                s = self.child(t)
                if s and s.get('last_event_id'):
                    r = b.receive_event(t['session_id'], s['last_event_id'])
                    if r['proceed']:
                        events.append({'task_id': t['id'], **r})
            full = {'proceed': True, 'events': events, 'project': self.view(p), 'project_id': pid,
                    'instruction': 'Supervisor reviews every child via existing evaluate/review/grade tools; integrate then check/review/finish the project. Never treat child acceptance as project acceptance.'}
            if event_id and event_id not in (p.get('receipts') or {}):
                p.setdefault('receipts', {})[event_id] = copy.deepcopy({
                    'proceed': full['proceed'], 'event_id': event_id, 'project_id': pid,
                    'events': full['events'], 'project': full['project'],
                    'instruction': full['instruction']})
            self.save(p)
            if p.get('owner') and event_id:
                # The immutable receipt is durable before the courier event is
                # retired, so a later full-detail lookup cannot lose the batch.
                with b.locked(pid) as root:
                    state, _ = b.load_session(root)
                    state['events'][event_id]['status'] = 'processed'
                    b.write_json(root / 'state.json', state)
            return self._receive_result(full, detail)

    @staticmethod
    def _replay_receipt(stored, event_id, pid):
        replay = copy.deepcopy(stored)
        replay.update(proceed=False, event_id=event_id, project_id=pid, replayed=True, read_only=True,
                      continuation_hint='Use the default summary continuation to recover any pending reminder; '
                                         'this full lookup never authorizes work.')
        for event in replay.get('events') or []:
            if isinstance(event, dict):
                event['proceed'] = False
        return replay

    @staticmethod
    def _receive_result(full, detail):
        if detail == 'summary':
            from compact import project_receive
            return project_receive(full)
        full['detail'] = 'full'
        return full

    def update(self, pid, decision_reason, strategy=None, max_parallel=None, add_tasks=None):
        with self.lock():
            p = self.refresh(self.load(pid)); self.active(p)
            if p['phase'] == 'verifying':
                raise self.b.GateError('Wait for current integration checks before revising the plan')
            if not isinstance(decision_reason, str) or not decision_reason.strip():
                raise self.b.GateError('Explain the revised plan')
            if strategy is not None:
                if strategy not in ('single', 'parallel', 'phased'):
                    raise self.b.GateError('Invalid strategy')
                p['strategy'] = strategy
            if max_parallel is not None:
                if type(max_parallel) is not int or not 1 <= max_parallel <= 3:
                    raise self.b.GateError('max_parallel must be 1-3')
                p['max_parallel'] = max_parallel
            if add_tasks:
                tasks = self.validate_tasks(add_tasks, p['tasks'])
                for t in tasks:
                    t.update(session_id=uuid.uuid4().hex, phase='pending', revision=1)
                p['tasks'].extend(tasks)
                p.pop('integration', None)
                p['phase'] = 'running'
            p['history'].append({'at': time.time(), 'reason': decision_reason, 'strategy': p['strategy']})
            p['decision_reason'] = decision_reason
            self.save(p)
        return self.tick(pid)

    @staticmethod
    def backend_fields(task):
        binding = task['executor']
        for field in ('provider', 'model', 'reasoning_effort'):
            task[field] = binding[field]
        task['executor_profile'] = binding['profile']

    def reassign(self, pid, task_id, decision_reason, provider=None, model=None, message=None,
                 executor_profile=None, reasoning_effort=None):
        with self.lock():
            p = self.refresh(self.load(pid)); self.active(p)
            if not decision_reason.strip() or p['phase'] == 'verifying':
                raise self.b.GateError('Explain reassignment and wait for integration checks')
            t = next((x for x in p['tasks'] if x['id'] == task_id), None)
            if t is None or t['phase'] in ('running', 'stale', 'accepted', 'awaiting_review', 'changed_after_acceptance'):
                raise self.b.GateError('Only unstarted, failed or returned nonaccepted tasks can be reassigned; grade pending work first')
            if t['revision'] >= 3:
                raise self.b.GateError('Task reassignment limit reached')
            new = dict(t)
            binding = self.b.backend_bindings(t)['executor']
            if executor_profile is not None or (provider is not None and provider != binding['provider']):
                binding = self.b.providers.resolve_executor(self.b.config(), executor_profile=executor_profile,
                    provider=provider, model=model, reasoning_effort=reasoning_effort)
            else:
                if model is not None:
                    binding['model'] = model
                if reasoning_effort is not None:
                    binding['reasoning_effort'] = reasoning_effort
                binding = self.b.providers.validate_binding(binding)
            # A replacement is a new conversation and follows the current two-role
            # protocol. The previous immutable contract and evidence are retained.
            for key in ('backends', 'exam_provider', 'exam_model', 'exam_profile', 'exam_reasoning_effort', 'review_paths'):
                new.pop(key, None)
            new.update(executor=binding, workflow_version=3, assessment_mode='closed_book')
            self.backend_fields(new)
            if message is not None:
                if not isinstance(message, str) or not message.strip():
                    raise self.b.GateError('Public handoff message must be nonempty')
                self.b.deny_leak(message, t)
                new['message'] = message
            old = self.child(t)
            if old:
                self.b.return_control(t['session_id'], 'cancel')
            p['history'].append({'at': time.time(), 'task_id': task_id, 'previous_session_id': t['session_id'],
                                 'previous_worktree': old.get('worktree') if old else None, 'reason': decision_reason})
            if not new.get('usage_id'):
                # Legacy task: its prior ledger is keyed by the old session ID.
                new['usage_id'] = t['session_id']
            new.update(session_id=uuid.uuid4().hex, phase='pending', revision=t['revision'] + 1)
            for key in ('accepted_commit', 'event_id', 'error', 'base_commit'):
                new.pop(key, None)
            p['tasks'][p['tasks'].index(t)] = new
            p.pop('integration', None); p['phase'] = 'running'
            self.save(p)
        return self.tick(pid)

    def control(self, pid, action):
        if action not in ('pause', 'resume', 'cancel'):
            raise self.b.GateError('Use pause/resume/cancel')
        with self.lock():
            p = self.load(pid)
            if p['continuation'] == 'cancelled' and action != 'cancel':
                raise self.b.GateError('Cancelled project cannot be resumed')
            p['continuation'] = {'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled'}[action]
            self.save(p)  # persisted before touching children or callbacks
            receipts = []
            for sid in [pid] + [t['session_id'] for t in p['tasks'] if self.child(t)]:
                receipts.append(self.b.return_control(sid, action))
            if action == 'resume':
                p.pop('notice_signature', None)
                self.refresh(p); self.publish(p); self.save(p)
        result = self.tick(pid) if action == 'resume' else self.status(pid)
        result['control_receipts'] = receipts
        return result

    def integrate(self, pid):
        with self.lock():
            p = self.refresh(self.load(pid)); self.active(p)
            if not all(t['phase'] == 'accepted' for t in p['tasks']):
                raise self.b.GateError('All tasks must pass supervisor review before integration')
            pins = self.pins(p)
            if p.get('integration', {}).get('pins') == pins:
                return self.view(p)
            tree, conflict = self.merge_workspace(p, pins, 'integration')
            p['integration'] = {'worktree': tree, 'pins': pins, 'conflict': conflict}
            p['phase'] = 'integration_conflict' if conflict else 'integrated'
            self.save(p)
            return self.view(p)

    def check(self, pid):
        with self.lock():
            p = self.refresh(self.load(pid)); self.active(p)
            if p['phase'] not in ('integrated', 'integration_failed', 'integration_conflict', 'awaiting_final_review'):
                raise self.b.GateError('Integrate accepted tasks before checking')
            if p['integration']['pins'] != self.pins(p) or not all(t['phase'] == 'accepted' for t in p['tasks']):
                raise self.b.GateError('Accepted task evidence changed; integrate again')
            if p['integration'].get('conflict'):
                raise self.b.GateError('Resolve integration conflicts through project_resolve before checking')
            p['phase'] = 'verifying'; p['verification_id'] = uuid.uuid4().hex
            p['integration'].pop('packet_id', None)
            p['integration'].pop('error', None)
            p['verification_started'] = time.time()
            p.pop('verification_pid', None)
            p.pop('verification_process_start', None)
            self.save(p)
            try:
                worker = subprocess.Popen([sys.executable, str(Path(self.b.__file__)), '_project_check', pid, p['verification_id']],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          start_new_session=True, close_fds=True)
            except OSError:
                p['phase'] = 'integration_failed'; self.save(p); raise
            p['verification_pid'] = worker.pid
            p['verification_process_start'] = process_start(worker.pid)
            reap_background(worker)
            self.save(p)
            return self.view(p)

    def resolve(self, pid, rationale):
        # Supervisor resolves the reported files in this scratch workspace. Merge
        # remaining pins after completing each conflicted merge; never skip a task.
        with self.lock():
            p = self.refresh(self.load(pid)); self.active(p)
            if p['phase'] != 'integration_conflict' or not rationale.strip():
                raise self.b.GateError('No integration conflict or missing resolution rationale')
            b = self.b; tree = p['integration']['worktree']
            if p['integration']['pins'] != self.pins(p) or not all(t['phase'] == 'accepted' for t in p['tasks']):
                raise b.GateError('Task evidence changed')
            if b.git(tree, 'diff', '--name-only', '--diff-filter=U'):
                raise b.GateError('Resolve and stage conflicted files in the integration worktree first')
            try:
                b.git(tree, '-c', 'user.name=TELOS', '-c', 'user.email=telos@localhost', '-c', 'commit.gpgsign=false', 'commit', '--no-edit')
            except b.GateError:
                raise b.GateError('Could not finish the conflicted integration merge')
            # Re-merge all pinned heads; Git skips already incorporated ancestors.
            for t in p['tasks']:
                try:
                    b.git(tree, '-c', 'user.name=TELOS', '-c', 'user.email=telos@localhost', '-c', 'commit.gpgsign=false',
                          'merge', '--no-ff', '--no-edit', p['integration']['pins'][t['id']])
                except b.GateError:
                    files = b.git(tree, 'diff', '--name-only', '--diff-filter=U').splitlines()
                    if not files:
                        raise
                    p['integration']['conflict'] = {'task_id': t['id'], 'files': files}
                    self.save(p); return self.view(p)
            p['integration']['conflict'] = None
            p['history'].append({'resolution': rationale, 'at': time.time()})
            p['phase'] = 'integrated'; self.save(p)
            return self.view(p)

    def verification_contract(self, p):
        """Derive check permissions from immutable project/child contracts.

        Older project contracts need no migration: their accepted children already
        contain the authorized dependencies. Mutable planning metadata is not an
        authority for granting access to the final verification sandbox.
        """
        b = self.b
        paths = list(p['contract'].get('dependency_paths', []))
        sources = {}
        for task in p['tasks']:
            state, contract = b.load_session(b.run_dir(task['session_id']))
            if state.get('project_id') != p['project_id']:
                raise b.GateError('Verification dependency belongs to another project')
            paths.extend(contract.get('dependency_paths', []))
            sources[task['id']] = {'session_id': state['session_id'], 'contract_hash': state['contract_hash']}
        paths = sorted(set(paths))
        validated = b.validate_dependencies(paths)
        if validated != paths:
            raise b.GateError('A frozen dependency path was redirected; restore the declared directory')
        return {**p['contract'], 'dependency_paths': paths}, sources

    def verify_job(self, pid, ident):
        b = self.b
        with self.lock():
            p = self.load(pid)
            if p.get('verification_id') != ident or p['phase'] != 'verifying':
                return
        try:
            check_contract, dependency_contracts = self.verification_contract(p)
            state = {'session_id': pid, 'worktree': p['integration']['worktree']}
            commit, target = b.snapshot(state, ident)
            audit_reports = {}
            for task in p['tasks']:
                child_state, child_contract = b.load_session(b.run_dir(task['session_id']))
                if child_contract.get('task_type') == 'audit':
                    import audit
                    evidence = b.read_json(b.run_dir(task['session_id']) / f"attempt-{child_state['rounds']}.json")['audit']
                    audit.verify(b, child_state, child_contract, evidence)
                    audit_reports[task['id']] = {'evidence': evidence, 'review': child_state['acceptance']['audit_review']}
                    shutil.copytree(evidence['evidence_dir'], target / '.telos-audits' / task['id'])
            checks = b.executable_checks(check_contract, target)
            packet = {'project_id': pid, 'commit': commit, 'pins': p['integration']['pins'],
                      'contract_hash': p['contract_hash'], 'checks': checks,
                      'dependency_paths': check_contract['dependency_paths'],
                      'dependency_contracts': dependency_contracts}
            if audit_reports:
                packet['audits'] = audit_reports
            b.write_json(self.path(pid).parent / 'integration-review.json', packet)
            phase = 'awaiting_final_review' if all(x['status'] == 'passed' for x in checks) else 'integration_failed'
            update = {'commit': commit, 'packet_id': b.digest(packet)}
        except Exception as error:
            phase, update = 'integration_failed', {'error': str(error)[:1000]}
        finally:
            if 'target' in locals() and target.exists():
                shutil.rmtree(target)
        with self.lock():
            current = self.load(pid)
            if current.get('verification_id') != ident:
                return
            current['phase'] = phase
            current['integration'].update(update)
            if current['continuation'] == 'active':
                self.publish(current)
            self.save(current)

    def review(self, pid, detail='full'):
        if detail not in ('summary', 'full'):
            raise self.b.GateError('detail must be summary or full')
        with self.lock():
            p = self.load(pid)
            if p['phase'] not in ('awaiting_final_review', 'integration_failed') or not p['integration'].get('packet_id'):
                raise self.b.GateError('No completed integration checks')
            packet = self.b.read_json(self.path(pid).parent / 'integration-review.json')
            if self.b.digest(packet) != p['integration']['packet_id']:
                raise self.b.GateError('Integration evidence changed')
            full = {'packet_id': p['integration']['packet_id'], 'questions': p['contract']['questions'], 'evidence': packet,
                    'instruction': 'Supervisor must inspect the integrated code and answer each final acceptance criterion with evidence before finish. These private references must not be forwarded to workers.',
                    'project_id': pid, 'worktree': p['integration'].get('worktree'), 'commit': packet.get('commit'),
                    'pins': p['integration'].get('pins'), 'contract_hash': p.get('contract_hash'),
                    'blockers': (p.get('final_review') or {}).get('blockers', []),
                    'final_review': p.get('final_review'), 'usage': self._usage(p),
                    'identity': {'project_id': pid, 'packet_id': p['integration']['packet_id'],
                                 'commit': packet.get('commit'), 'pins': p['integration'].get('pins'),
                                 'contract_hash': p.get('contract_hash')}}
            if detail == 'summary':
                from compact import project_review
                return project_review(full)
            full['detail'] = 'full'
            return full

    def finish(self, pid, packet_id, scores, blockers, rationale):
        with self.lock():
            p = self.refresh(self.load(pid)); self.active(p)
            from supervisor import verify_owner
            verify_owner(self.b, p.get('owner'))
            if p['phase'] != 'awaiting_final_review' or packet_id != p['integration'].get('packet_id') or not rationale.strip():
                raise self.b.GateError('Current integration evidence and review rationale required')
            b = self.b; integ = p['integration']
            packet = b.read_json(self.path(pid).parent / 'integration-review.json')
            if b.digest(packet) != packet_id or self.pins(p) != integ['pins'] or not all(t['phase'] == 'accepted' for t in p['tasks']):
                raise b.GateError('Task or integration evidence changed')
            if 'dependency_contracts' in packet:
                check_contract, sources = self.verification_contract(p)
                if sources != packet['dependency_contracts'] or check_contract['dependency_paths'] != packet['dependency_paths']:
                    raise b.GateError('Verification dependency evidence changed; rerun project checks')
            if b.git(integ['worktree'], 'rev-parse', 'HEAD') != packet['commit'] or b.git(integ['worktree'], 'status', '--porcelain'):
                raise b.GateError('Integrated code changed after checks')
            if not isinstance(blockers, list) or not all(isinstance(x, str) and x for x in blockers):
                raise b.GateError('Invalid blockers')
            by_id = {x['id']: x for x in scores}
            questions = p['contract']['questions']
            if len(by_id) != len(scores) or set(by_id) != {q['id'] for q in questions}:
                raise b.GateError('Grade every final criterion exactly once')
            for q in questions:
                s = by_id[q['id']]
                if type(s.get('points')) is not int or not 0 <= s['points'] <= q['points'] or not isinstance(s.get('rationale'), str) or not s['rationale'].strip():
                    raise b.GateError('Invalid final criterion score')
            passed = not blockers and all(x['status'] == 'passed' for x in packet['checks']) and sum(x['points'] for x in scores) >= p['contract']['threshold'] and all(by_id[q['id']]['points'] >= q.get('minimum', 0) for q in questions)
            p['phase'] = 'accepted' if passed else 'integration_failed'
            p['final_review'] = {'packet_id': packet_id, 'scores': scores, 'blockers': blockers, 'rationale': rationale, 'accepted': passed}
            self.save(p)
            if passed:
                with b.locked(pid) as root:
                    courier, _ = b.load_session(root)
                    b.retire_reminders(courier, complete=True)
                    b.write_json(root / 'state.json', courier)
                try:
                    b.experience_manager().promote_project(p)
                except Exception as error:
                    b.recovery_error(self.path(pid).parent / 'experience-error.json', error)
            return self.view(p)

    def recover(self):
        for path in (self.b.STATE / 'projects').glob('*/project.json'):
            try:
                p = self.load(path.parent.name)
                if p['phase'] == 'verifying':
                    missing = (not process_running(p['verification_pid'], p.get('verification_process_start'))
                               if p.get('verification_pid') else
                               time.time() - p.get('verification_started', p.get('updated_at', time.time())) > 30)
                    if missing:
                        with self.lock():
                            current = self.load(path.parent.name)
                            if current['phase'] == 'verifying' and current.get('verification_id') == p.get('verification_id'):
                                current['phase'] = 'integration_failed'
                                current['integration']['error'] = 'Verification process exited without a result; rerun checks'
                                self.publish(current); self.save(current)
                            p = current
                if p['phase'] == 'running' and p['continuation'] == 'active':
                    self.tick(p['project_id'])
            except Exception as e:
                self.b.recovery_error(path.parent / 'recovery-error.json', e)

    def call(self, name, args, caller):
        name = canonical_tool_name(name)
        if name == 'telos_project_open':
            return self.create(**args, caller=caller)
        pid = args['project_id']
        self.authorize(self.load(pid), caller)
        rest = {k: v for k, v in args.items() if k != 'project_id'}
        mapping = {'start': self.tick, 'status': self.status, 'receive': self.receive, 'update': self.update,
                   'control': self.control, 'integrate': self.integrate, 'check': self.check, 'review': self.review,
                   'finish': self.finish, 'resolve': self.resolve, 'reassign': self.reassign}
        return mapping[name.removeprefix('telos_project_')](pid, **rest)

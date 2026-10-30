"""Project coordination around the existing independent-session acceptance gate.

The original caller chooses the plan. This module enforces it, never grades it.
All coordinator mutations use one cross-process lock; session locks are nested
inside it, never the reverse. No model is called while holding a session lock.
"""
from __future__ import annotations
import copy
import contextlib
import errors
import fcntl
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import uuid
from processes import process_start, process_running, reap_background
from identity import canonical_tool_name
from usage import TOKEN_FIELDS, resolve_task_budget
import effort_policy
import progress_guard
import continuation
import check_runtime
import project_budget
import roles


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
        # A stored policy that cannot be trusted must never silently fall back
        # to the legacy ceiling or to unlimited admission.
        try:
            project_budget.validate_project(p)
            project_budget.validate_work_project(p)
        except ValueError as error:
            raise self.b.GateError(str(error), code='invalid_budget_policy') from None
        return p

    def save(self, p):
        p['updated_at'] = time.time()
        self.b.write_json(self.path(p['project_id']), p)

    def authorize(self, p, caller):
        if p.get('owner') and p['owner']['thread_id'] != caller:
            raise self.b.GateError('Project belongs to another supervisor thread',
                                   code='owner_mismatch', next_action='use_original_supervisor')

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
                if t.get('environment_requirements') is not None:
                    raise self.b.GateError('Project environment preparation is for implementation tasks only')
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
                cfg = self.b.config()
                configured = t.get('budget') if t.get('budget') is not None else cfg.get('task_budget')
                t['budget'] = resolve_task_budget(configured, cfg)
            except ValueError as error:
                raise self.b.GateError('Invalid task budget: ' + str(error)) from None
            if any(k in t for k in ('exam_profile', 'exam_provider', 'exam_model', 'exam_reasoning_effort',
                                     'backends', 'executor', 'workflow_version', '_workflow_version', 'role_snapshot')):
                raise self.b.GateError('New project tasks configure one executor; internal/legacy bindings are not task inputs')
            role = roles.freeze(self.b.config(), t.get('role_profile'))
            if role:
                t['role_snapshot'] = role
            t['experience_tags'] = roles.tags(t.get('experience_tags'))
            t['executor'] = self.b.providers.resolve_executor(self.b.config(), **roles.executor_options(
                role, **{k: t.get(k) for k in ('executor_profile', 'provider', 'model', 'reasoning_effort')}))
            t['workflow_version'] = 3
            t['work_reply_version'] = 2
            t['auto_continue_checkpoints'] = continuation.freeze(t.get('auto_continue_checkpoints'), self.b.config())
            if t.get('task_type', 'implementation') == 'implementation':
                t['auto_run_checks'] = check_runtime.freeze(t.get('auto_run_checks'), self.b.config())
            t['reasoning_policy'] = effort_policy.freeze(t['executor'], self.b.config(), t.get('reasoning_policy'))
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
            if t.get('environment_requirements') is not None:
                import project_environment
                t['environment_requirements'] = project_environment.validate(t['environment_requirements'])
            self.b.validate_exam(t['questions'], t['checks'], t['threshold'], t['max_rounds'])
            self.validate_public_task(t, t)
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

    def validate_public_task(self, t, private_contract):
        fields = {key: t.get(key, '') for key in ('task', 'context', 'message')}
        if t.get('role_snapshot'):
            fields['role_guidance'] = roles.prompt(t['role_snapshot'])
            if not self.b.experience_manager().safe_public(fields['role_guidance'], [private_contract]):
                raise self.b.GateError('Role guidance must exclude private acceptance material and credentials')
        fields['allowed_paths'] = self.b.encoded(t.get('allowed_paths'))
        if t.get('environment_requirements') is not None:
            fields['environment_requirements'] = self.b.encoded(t['environment_requirements'])
        if t.get('audit'):
            fields['audit'] = self.b.encoded(t['audit'])
        self.b.validate_public_material(private_contract, **fields)

    def create(self, repo, task, tasks, questions, checks, decision_reason,
               execution_mode='auto', strategy='single', max_parallel=3,
               max_jobs=40, context='', notify=True, caller=None, use_experience=True, dependency_paths=None, environment_requirements=None, auto_continue_checkpoints=None, auto_run_checks=None, _planning=None):
        if execution_mode not in ('auto', 'single', 'parallel') or strategy not in ('single', 'parallel', 'phased'):
            raise self.b.GateError('Invalid execution mode or supervisor strategy')
        if not isinstance(decision_reason, str) or not decision_reason.strip():
            raise self.b.GateError('Supervisor must explain its execution strategy')
        if type(max_parallel) is not int or not 1 <= max_parallel <= 3:
            raise self.b.GateError('max_parallel must be 1-3')
        if type(max_jobs) is not int or not 1 <= max_jobs <= 200:
            raise self.b.GateError('max_jobs must be 1-200')
        import project_environment
        environment_requirements = project_environment.validate(environment_requirements, repo)
        tasks = [dict(t) for t in tasks]
        for t in tasks:
            if t.get('task_type', 'implementation') == 'implementation':
                if auto_run_checks is not None:
                    t.setdefault('auto_run_checks', auto_run_checks)
                if auto_continue_checkpoints is not None:
                    t.setdefault('auto_continue_checkpoints', auto_continue_checkpoints)
                if environment_requirements is not None:
                    t.setdefault('environment_requirements', environment_requirements)
        tasks = self.validate_tasks(tasks)
        if _planning and _planning.get('meeting_id'):
            for t in tasks:
                t['context'] = context + '\nTask context:\n' + t['context']
        b = self.b
        dependency_paths = b.validate_dependencies([] if dependency_paths is None else dependency_paths)
        b.validate_exam(questions, checks, 90, 5)
        final_contract = {'questions': questions, 'checks': checks}
        b.validate_public_material(final_contract, task=task, context=context)
        for t in tasks:
            self.validate_public_task(t, final_contract)
        repo = Path(b.git(repo, 'rev-parse', '--show-toplevel')).resolve()
        if b.git(repo, 'status', '--porcelain'):
            raise b.GateError('Source repository must be clean; commit/stash explicitly')
        if not task or not isinstance(task, str):
            raise b.GateError('Nonempty project task required')
        owner = b.bind_owner(caller) if notify else None
        pid = uuid.uuid4().hex
        contract = {'task': task, 'context': context, 'questions': questions, 'checks': checks,
                    'threshold': 90, 'max_rounds': 5, 'dependency_paths': dependency_paths}
        contract['auto_continue_checkpoints'] = continuation.freeze(auto_continue_checkpoints, b.config())
        contract['auto_run_checks'] = check_runtime.freeze(auto_run_checks, b.config())
        if environment_requirements is not None:
            contract['environment_requirements'] = environment_requirements
        if _planning is not None:
            contract['planning'] = _planning
        p = {'project_id': pid, 'repo': str(repo), 'base_commit': b.git(repo, 'rev-parse', 'HEAD'),
             'owner': owner, 'execution_mode': execution_mode, 'strategy': strategy,
             'decision_reason': decision_reason, 'max_parallel': max_parallel, 'max_jobs': max_jobs,
             'jobs_started': 0, 'phase': 'planned', 'continuation': 'active', 'tasks': tasks, 'use_experience': use_experience,
             'contract': contract, 'contract_hash': b.digest(contract), 'history': [], 'created_at': time.time(),
             'check_timing': {'observed_seconds': 0.0, 'completed_runs': 0, 'complete': True}}
        for t in tasks:
            t.update(session_id=uuid.uuid4().hex, phase='pending', revision=1)
        return self.initialize(p)

    def initialize(self, p, *, courier_model='project'):
        """Persist a coordinator and its notification courier under the same lock."""
        b = self.b
        pid = p['project_id']
        import timing
        timing.initialize(p, p['created_at'])
        timing.attach_origin(b, p, p['contract'].get('planning'))
        with self.lock():
            # A project has one notification courier; child events stay durable in
            # their own sessions and are acknowledged by project_receive.
            courier = {'session_id': pid, 'project_id': pid, 'notification_only': True,
                       'contract_hash': p['contract_hash'], 'phase': 'project_update', 'owner': p['owner'],
                       'continuation': 'active', 'events': {}, 'model': courier_model, 'coalesce_active': p['contract'].get('auto_run_checks', False), 'turns': 0, 'rounds': 0}
            b.write_json(b.run_dir(pid) / 'contract.json', p['contract'])
            b.write_json(b.run_dir(pid) / 'state.json', courier)
            self.save(p)
        return self.status(pid)

    def _child_session(self, t):
        path = self.b.run_dir(t['session_id']) / 'state.json'
        if not path.exists() and t.get('phase') in ('pending', 'starting', 'setup_error'):
            return None
        return self.b.load_session(path.parent)

    def child(self, t):
        session = self._child_session(t)
        return session[0] if session else None

    def isolate(self, t, error):
        t.pop('start_error', None)
        t.update(phase='quarantined', error=str(error)[:500], accepted_commit=None, event_id=None)

    def refresh(self, p, observations=None):
        for t in p['tasks']:
            try:
                unstarted = (t.get('phase') in ('pending', 'starting', 'setup_error')
                             and not (self.b.run_dir(t['session_id']) / 'state.json').exists())
                stat = None if unstarted else self.b.status(t['session_id'])
            except (self.b.GateError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                self.isolate(t, error)
                continue
            if stat:
                if observations is not None:
                    observations[t['session_id']] = stat
                t['phase'] = stat['phase']
                t['progress_guard'] = stat.get('progress_guard')
                t['auto_continue_pending'] = stat.get('auto_continue_pending')
                t['automatic_checks'] = stat.get('automatic_checks')
                t['audit_check_correction'] = stat.get('audit_check_correction')
                if t['phase'] != 'ready' and not t.get('discussion_pending'):
                    t.pop('start_error', None)
                # A rejected start never reaches the child's execution state.
                # Retain its diagnostic until that child actually advances.
                t['error'] = stat.get('last_error')
                if t['error'] is None and t.get('start_error'):
                    t['error'] = t['start_error']['message']
                t['accepted_commit'] = stat.get('acceptance', {}).get('commit') if stat.get('accepted') else None
                t['event_id'] = stat.get('return_channel', {}).get('event', {}).get('id') if stat.get('return_channel', {}).get('event') else None
                if stat.get('base_commit'):
                    t['base_commit'] = stat['base_commit']
            elif t['phase'] == 'starting':
                t.update(phase='setup_error', error='Interrupted task setup; inspect workspace, then replace this unstarted task')
        if p.get('kind') == 'discussion':
            from meetings import Meetings
            Meetings(self.b).observe_progress(p)
        return p

    def _usage(self, p, snapshots=None):
        import timing
        totals = {'requests': 0, 'failed_requests': 0, 'unknown_usage_requests': 0,
                  'in_flight_requests': 0, 'input_tokens': 0, 'cached_input_tokens': 0,
                  'uncached_input_tokens': 0, 'output_tokens': 0, 'reasoning_output_tokens': 0,
                  'total_tokens': 0}
        unknown_fields = {key: 0 for key in TOKEN_FIELDS}
        started = 0
        incomplete = False
        unavailable = []
        latencies = []
        for t in p['tasks']:
            if snapshots is not None and t['session_id'] in snapshots:
                started += 1
                snapshot = snapshots[t['session_id']]
            else:
                try:
                    session = self._child_session(t)
                except Exception:
                    # A corrupt child state after setup is a started but unavailable
                    # ledger; never silently omit it from the aggregate.
                    if t.get('phase') != 'pending':
                        started += 1
                        incomplete = True
                        unavailable.append(t['id'])
                        latencies.append(None)
                    continue
                prior_usage = not session and t.get('usage_id')
                if not session and not prior_usage:
                    continue
                if (prior_usage and t.get('phase') == 'pending' and t.get('revision') == 1
                        and not self.b.usage_path(prior_usage).exists()
                        and not self.b.run_dir(t['session_id']).exists()
                        and not any(h.get('task_id') == t['id'] for h in p.get('history', []))):
                    # A newly allocated accounting ID is not evidence of spend.
                    # Existing artifacts or prior assignments remain unknown if
                    # their ledger is missing; never hide that history as zero.
                    continue
                started += 1
                try:
                    if prior_usage:
                        # A replacement may still be queued. Its original ledger
                        # already contains spend and must not disappear until start.
                        if not self.b.usage_path(prior_usage).is_file():
                            raise ValueError('Prior assignment usage ledger unavailable')
                        state = {'session_id': t['session_id'], 'usage_id': prior_usage}
                        contract = {'budget': t.get('budget')}
                    else:
                        state, contract = session
                    snapshot = self.b.usage_snapshot(state, contract)
                except Exception:
                    incomplete = True
                    unavailable.append(t['id'])
                    latencies.append(None)
                    continue
            snapshot_totals = snapshot.get('totals') if isinstance(snapshot, dict) else None
            if not isinstance(snapshot_totals, dict):
                incomplete = True
                unavailable.append(t['id'])
                latencies.append(None)
                continue
            latency = snapshot.get('request_latency')
            if snapshot.get('incomplete') is True:
                incomplete = True
                if isinstance(latency, dict):
                    latency = {**latency, 'complete': False}
            latencies.append(latency)
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
        totals['request_latency'] = timing.merge_latency(latencies)
        return totals

    def view(self, p, detail='full', observations=None):
        import timing
        result = {k: p[k] for k in ('project_id', 'phase', 'continuation', 'execution_mode', 'strategy',
                                   'decision_reason', 'max_parallel', 'max_jobs', 'jobs_started', 'base_commit')}
        result['contract_hash'] = p.get('contract_hash')
        result['job_budget'] = project_budget.summary(p)
        result['work_turn_budget'] = project_budget.work_turn_summary(
            p, int(self.b.config()['max_turns']))
        if p.get('kind') == 'discussion':
            from meetings import Meetings
            result['discussion'] = Meetings(self.b).view(p)
            result['kind'] = 'discussion'
        result['effective_parallel'] = self.limit(p)
        result['local_checks_started'] = p.get('local_checks_started', 0)
        tasks = []
        # Reuse only this view's observations. New views and acceptance paths
        # still load and validate current records; no cache survives this call.
        snapshots = {}
        for t in p['tasks']:
            item = {k: t.get(k) for k in ('id', 'task', 'depends_on', 'allowed_paths', 'provider', 'model',
                                           'executor_profile', 'workflow_version', 'reasoning_effort',
                                           'session_id', 'phase', 'accepted_commit', 'event_id', 'error',
                                           'budget', 'revision', 'reasoning_policy', 'auto_run_checks', 'automatic_checks', 'audit_check_correction',
                                           'role_snapshot', 'experience_tags')}
            item['progress_guard'] = t.get('progress_guard')
            if p.get('kind') == 'discussion':
                item.update(role=t['role'], active=t.get('active', True), queued_followup=bool(t.get('discussion_pending')))
            try:
                observed = (observations or {}).get(t['session_id'])
                if observed is not None:
                    if observed.get('reasoning_decision'):
                        item['reasoning_decision'] = observed['reasoning_decision']
                    if observed.get('clarification'):
                        item['clarification'] = observed['clarification']
                    item['environment'] = observed.get('environment')
                    item['auto_continue_pending'] = observed.get('auto_continue_pending')
                    item['audit_check_correction'] = observed.get('audit_check_correction')
                    item['budget'] = observed['budget']
                    item['usage'] = observed['usage']
                    snapshots[t['session_id']] = item['usage']
                    item['timing'] = observed['timing']
                    if observed.get('delivery'):
                        from delivery import summary
                        item['delivery'] = summary(observed['delivery'])
                elif (session := self._child_session(t)):
                    child, contract = session
                    if child.get('reasoning_decision'):
                        item['reasoning_decision'] = child['reasoning_decision']
                    if child.get('clarification'):
                        item['clarification'] = child['clarification']
                    item['environment'] = child.get('environment')
                    item['auto_continue_pending'] = continuation.pending(child)
                    item['budget'] = contract.get('budget')
                    item['usage'] = self.b.usage_snapshot(child, contract)
                    snapshots[t['session_id']] = item['usage']
                    item['timing'] = timing.snapshot(child, requests=item['usage'].get('request_intervals'))
                    item['automatic_checks'] = check_runtime.public(child)
                    import audit_check_correction
                    item['audit_check_correction'] = audit_check_correction.public(child)
                    if child.get('delivery'):
                        from delivery import summary
                        item['delivery'] = summary(child['delivery'])
            except (self.b.GateError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                self.isolate(item, error)
            if item['phase'] in ('checkpoint', 'blocked_environment', 'protocol_error'):
                item['next_action'] = ('automatic_continuation_pending' if t.get('auto_continue_pending')
                                       else self.b.next_action_for(item['phase'])) if p['continuation'] == 'active' else None
            if item['phase'] == 'needs_clarification':
                item['next_action'] = ('answer_clarification_then_send' if p['continuation'] == 'active' else None)
            if t.get('start_error') and item['phase'] in ('pending', 'ready', 'starting', 'setup_error'):
                item['start_error'] = t['start_error']
                item['next_action'] = (t['start_error']['next_action']
                                       if p['continuation'] == 'active' else None)
            if (item.get('progress_guard') or {}).get('blocked'):
                item['next_action'] = item['progress_guard']['next_action'] if p['continuation'] == 'active' else None
            tasks.append(item)
        result['tasks'] = tasks
        result['usage'] = self._usage(p, snapshots)
        result['timing'] = self._timing(p, tasks)
        result['integration'] = p.get('integration')
        if 'final_review' in p:
            result['final_review'] = p['final_review']
        result['accepted'] = p['phase'] == 'accepted'
        try:
            courier, _ = self.b.load_session(self.b.run_dir(p['project_id']))
        except (self.b.GateError, OSError, ValueError, KeyError, TypeError) as error:
            courier = {}
            result['return_error'] = str(error)[:500]
        events = courier.get('events')
        last_event_id = courier.get('last_event_id')
        event = events.get(last_event_id) if isinstance(events, dict) and last_event_id is not None else None
        result['return_event'] = ({'id': last_event_id, 'status': event.get('status')}
                                  if isinstance(event, dict) else None)
        result['return_health'] = ({'status': 'complete', 'next_action': None} if result['accepted']
                                   else {'status': 'quarantined', 'next_action': 'restore_courier_state'}
                                   if 'return_error' in result else self.b.return_health(courier))
        if detail == 'summary':
            from compact import project as project_view
            return project_view(result)
        result['detail'] = 'full'
        return result

    def _timing(self, p, tasks=None):
        import timing
        # Reuse status's child summaries; reviews only read state, not another
        # complete status/usage/return-channel projection.
        items = list(tasks) if tasks is not None else []
        seen = {task['session_id'] for task in items}
        sources = [] if tasks is not None else [(t['id'], t['session_id'], t['phase']) for t in p['tasks']]
        sources.extend((h['task_id'] + '@' + h['previous_session_id'][:8], h['previous_session_id'], 'retired')
                       for h in p.get('history', []) if h.get('previous_session_id'))
        for ident, sid, phase in sources:
            if sid in seen:
                continue
            seen.add(sid)
            item = {'id': ident, 'phase': phase}
            if phase != 'pending':
                try:
                    state, contract = self.b.load_session(self.b.run_dir(sid))
                    item['timing'] = timing.snapshot(state, requests=self.b.usage_snapshot(state, contract).get('request_intervals'))
                except (self.b.GateError, OSError, ValueError, KeyError, TypeError, AttributeError):
                    pass  # Preserve unavailable assignments as incomplete.
            items.append(item)
        return timing.project_snapshot(p, items)

    def status(self, pid, detail='full'):
        if detail not in ('summary', 'full'):
            raise self.b.GateError('detail must be summary or full')
        with self.lock():
            observations = {}
            p = self.refresh(self.load(pid), observations)
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
            return self.view(p, detail, observations)

    def limit(self, p):
        if p['execution_mode'] == 'single' or (p['execution_mode'] == 'auto' and p['strategy'] == 'single'):
            return 1
        return p['max_parallel']

    def capacity(self, p, provider, *, local_checks=False):
        # All launch paths hold the coordinator lock, including standalone tasks.
        running = []
        roots = {path.parent for pattern in ('*/state.json', '*/job-lease.json')
                 for path in (self.b.STATE / 'runs').glob(pattern)}
        for root in roots:
            import state_store
            try:
                s = state_store.read(self.b, root)
            except self.b.GateError:
                slot = state_store.isolated_slot(self.b, root)
                if slot is not None:
                    running.append(slot)
                continue
            if s.get('phase') == 'running':
                running.append(s)
        child_ids = {t['session_id'] for t in p['tasks']} if p else set()
        if p is not None and sum(s.get('project_id') == p['project_id'] or s.get('session_id') in child_ids for s in running) >= self.limit(p):
            return False
        if sum(s.get('capacity_unknown') or s.get('active_provider', s.get('provider', 'ark')) == provider for s in running) >= self.b.config().get('provider_max_parallel', 3):
            return False
        if not local_checks and p is not None:
            try:
                ceiling = project_budget.effective_max_jobs(p)
            except ValueError as error:
                raise self.b.GateError(str(error), code='invalid_budget_policy') from None
            if ceiling is not None and p['jobs_started'] >= ceiling:
                raise self.b.GateError('Project job budget exhausted; no more model jobs')
        return True

    def budget(self, pid, request_id, reason, user_authorization, strategy='unlimited',
               include_work_turns=False, caller=None):
        """Original-supervisor-only, audited job ceiling policy change.

        Only the one-way ``unlimited`` strategy is supported.  The frozen
        contract, max_jobs, jobs_started, tasks and continuation stay untouched;
        a paused project remains paused and nothing is started or accepted.
        """
        with self.lock():
            if type(include_work_turns) is not bool:
                raise self.b.GateError('include_work_turns must be a boolean',
                                       code='invalid_arguments',
                                       next_action='correct_arguments')
            p = self.load(pid)
            if not p.get('owner'):
                raise self.b.GateError('Only the original owner can change the job budget policy',
                                       code='owner_required', next_action='use_original_supervisor')
            # Re-check the trusted metadata-derived caller under the same lock as
            # the mutation. There is no MCP argument that can supply this value.
            if not isinstance(caller, str) or caller != p['owner']['thread_id']:
                raise self.b.GateError('Project belongs to another supervisor thread',
                                       code='owner_mismatch', next_action='use_original_supervisor')
            if p.get('kind') == 'discussion':
                raise self.b.GateError('Discussion projects have no job budget policy',
                                       code='unsupported_project_kind')
            if p['phase'] == 'accepted' or p['continuation'] == 'cancelled' or p.get('phase') == 'cancelled':
                raise self.b.GateError('Cancelled or accepted projects cannot change budget policy',
                                       code='project_terminal')
            try:
                if include_work_turns is True:
                    # This call records only the sibling work-turn scope; the
                    # existing v1 object (and its request ids) stays byte-for-byte.
                    if project_budget.validate_project(p) is None:
                        raise ValueError('A valid unlimited job budget policy is required first')
                    snapshot = self._turns_snapshot(p)
                    updated, work_replayed, work_turns = project_budget.authorize_work(
                        p, owner_thread_id=caller, request_id=request_id,
                        reason=reason, user_authorization=user_authorization,
                        now=time.time(), original_max_turns=int(self.b.config()['max_turns']),
                        turns_snapshot=snapshot)
                    replayed, policy = work_replayed, project_budget.summary(updated)
                else:
                    updated, replayed, policy = project_budget.authorize(
                        p, owner_thread_id=caller, strategy=strategy,
                        request_id=request_id, reason=reason,
                        user_authorization=user_authorization, now=time.time())
                    work_turns = project_budget.work_turn_summary(
                        updated, int(self.b.config()['max_turns']))
            except ValueError as error:
                raise self.b.GateError(str(error), code='invalid_budget_policy') from None
            if not replayed:
                self.save(updated)
            return {'project_id': pid, 'replayed': replayed, 'strategy': strategy,
                    'continuation': p['continuation'], 'phase': p['phase'],
                    'max_jobs': p['max_jobs'], 'jobs_started': p['jobs_started'],
                    'contract_hash': p.get('contract_hash'),
                    'tasks_unchanged': True, 'job_budget': policy,
                    'work_turn_budget': work_turns}

    def _turns_snapshot(self, p):
        """Actual per-child turn counts; unreadable sessions remain explicitly null."""
        snapshot = {}
        for task in p.get('tasks', []):
            sid = task.get('session_id')
            if not sid:
                continue
            try:
                state = self.b.read_json(self.b.run_dir(sid) / 'state.json')
                turns = state.get('turns')
                snapshot[sid] = turns if type(turns) is int and turns >= 0 else None
            except (OSError, ValueError):
                snapshot[sid] = None
        return snapshot or {'unknown': None}

    def launch(self, p, sid, kind, message='', event_id=None, discussion_request_id=None,
               reasoning_effort=None, reasoning_reason=None, automatic=False, continuation_id=None):
        """Reserve before spawning; refund only when no new job was persisted."""
        before = self.b.read_json(self.b.run_dir(sid) / 'state.json')
        p['jobs_started'] += 1
        self.save(p)
        try:
            extra = {'_discussion_request_id': discussion_request_id} if discussion_request_id is not None else {}
            if continuation_id is not None:
                extra['_continuation_id'] = continuation_id
            if reasoning_effort is not None or reasoning_reason is not None:
                extra.update(reasoning_effort=reasoning_effort, reasoning_reason=reasoning_reason)
            try:
                decision = (project_budget.work_turn_decision(
                                p, session_id=sid,
                                child_contract_hash=before.get('contract_hash'),
                                child_owner_thread_id=(before.get('owner') or {}).get('thread_id'))
                            if kind == 'send' else None)
            except ValueError as error:
                raise self.b.GateError(str(error), code='invalid_budget_policy') from None
            if decision is not None:
                extra['_work_turns'] = decision
            result = self.b._start_job(sid, kind, message, event_id, **extra)
        except Exception as error:
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
                    if (automatic and current.get('progress_guard') == before.get('progress_guard') and
                            getattr(error, 'code', None) not in ('no_progress', 'retry_backoff', 'job_running', 'continuation_stopped')):
                        with self.b.locked(sid) as root:
                            current, _ = self.b.load_session(root)
                            progress_guard.consume(current)
                            progress_guard.startup_failure(current, 'startup:' + uuid.uuid4().hex,
                                                           errors.payload(error)['error']['message'])
                            self.b.write_json(root / 'state.json', current)
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
                convergence = t.get('progress_guard') or {}
                if (convergence.get('blocked') and not convergence.get('attempt_allowed') or
                        time.time() < convergence.get('retry_at', 0)):
                    continue
                if p.get('kind') == 'discussion':
                    if not t.get('active', True):
                        continue
                    if t.get('discussion_pending'):
                        try:
                            from meetings import Meetings
                            Meetings(b).launch_pending(p, t)
                            t.pop('start_error', None)
                        except Exception as error:
                            t['start_error'] = errors.payload(error)['error']
                        self.save(p)
                        continue
                if (t.get('automatic_checks') or {}).get('status') == 'queued':
                    self._continue_pending(t['session_id'], p)
                    continue
                if t['phase'] in ('checkpoint', 'protocol_error'):
                    self._continue_pending(t['session_id'], p)
                    continue
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
                                         _reasoning_policy=t.get('reasoning_policy'),
                                         _role_snapshot=t.get('role_snapshot'), experience_tags=t.get('experience_tags'),
                                          auto_continue_checkpoints=t.get('auto_continue_checkpoints', False),
                                          auto_run_checks=t.get('auto_run_checks', False),
                                          _work_reply_version=t.get('work_reply_version', 1),
                                          environment_requirements=t.get('environment_requirements'),
                                         _allowed_paths=t['allowed_paths'], _dependency_pins=pins,
                                         _workflow_version=t.get('workflow_version', 2),
                                         **({'_discussion': True} if p.get('kind') == 'discussion' else {}),
                                         _executor=t.get('executor'),
                                         _backends=None if t.get('workflow_version') == 3 else self.b.backend_bindings(t))
                        t.update(phase='ready', base_commit=base)
                        self.save(p)
                    self.launch(p, t['session_id'], 'send', t['message'], automatic=True)
                    t['phase'] = 'running'
                    t.pop('start_error', None)
                except Exception as e:
                    diagnostic = errors.payload(e)['error']
                    diagnostic.pop('usage', None)  # Current usage is already in the task view.
                    diagnostic['message'] = diagnostic['message'][:500]
                    t['start_error'] = diagnostic
                    t['error'] = diagnostic['message']
                    if t['phase'] == 'starting':
                        t['phase'] = 'setup_error'
                self.save(p)
            self.refresh(p)
            self.publish(p)
            self.save(p)
            return self.view(p)


    def continue_pending(self, sid):
        with self.lock():
            s, _ = self.b.load_session(self.b.run_dir(sid))
            if s.get('notification_only'):
                return
            p = self.refresh(self.load(s['project_id'])) if s.get('project_id') else None
            return self._continue_pending(sid, p)

    def _continue_pending(self, sid, p=None):
        """Coordinator lock -> session lock. Admission uses the existing launch path."""
        b = self.b
        with b.locked(sid) as root:
            state, contract = b.load_session(root)
            source = check_runtime.pending(state)
            checks = source is not None
            source = source or continuation.pending(state)
            if not source or state.get('continuation', 'active') != 'active':
                return
            if p is not None and (p['continuation'] != 'active' or p['phase'] not in ('planned', 'running')):
                return
            try:
                if checks:
                    check_runtime.authorize(b, state, contract, source)
                else:
                    continuation.authorize(state, contract, source)
                # Reconcile persisted pending work as well as fresh checkpoints.
                # Repeated recovery keeps the same interval; no launch or receipt.
                import timing
                timing.transition(state, timing.waiting_stage(state))
                timing.begin(state, 'scheduler_wait', ('checks:' if checks else 'continuation:') + source + ':control:' + str(state.get('control_revision', 0)))
                b.write_json(root / 'state.json', state)
                message = '' if checks else continuation.message(state)
            except b.GateError as error:
                if checks:
                    state['auto_check'].update(status='blocked', diagnostic=str(error)[:1000])
                else:
                    continuation.stop(state, source, error)
                b.record_event(state); b.write_json(root / 'state.json', state)
                return
        try:
            if not (self.capacity(p, state['provider'], local_checks=True) if checks else self.capacity(p, state['provider'])):
                return
            if checks:
                result = b._start_job(sid, 'checks', _auto_check_id=source)
                if p is not None:
                    p['local_checks_started'] = p.get('local_checks_started', 0) + 1
                    self.save(p)
            else:
                result = (self.launch(p, sid, 'send', message, continuation_id=source) if p is not None
                          else b._start_job(sid, 'send', message, _continuation_id=source))
            return result
        except Exception as error:
            with b.locked(sid) as root:
                current, _ = b.load_session(root)
                if checks and (current.get('auto_check') or {}).get('status') == 'queued':
                    current['auto_check'].update(status='blocked', diagnostic=str(error)[:1000])
                else:
                    continuation.stop(current, source, error)
                if current['phase'] != 'running':
                    b.record_event(current)
                b.write_json(root / 'state.json', current)
            return {'stopped': True, 'diagnostic': str(error)[:1000]}

    def start_child(self, sid, kind, message='', event_id=None, reasoning_effort=None, reasoning_reason=None):
        b = self.b
        with self.lock():
            s, contract = b.load_session(b.run_dir(sid))
            if s.get('notification_only'):
                raise b.GateError('Project courier cannot execute tasks')
            p = self.refresh(self.load(s['project_id'])) if s.get('project_id') else None
            if p is not None:
                self.active(p)
                if p.get('kind') == 'discussion':
                    raise b.GateError('Use telos_meeting_round for durable targeted follow-up; meetings cannot run implementation or exams')
                s = b.read_json(b.run_dir(sid) / 'state.json')
            # Idempotency must be checked before reserving another budget slot.
            replay = b.event_action(s, event_id, kind, effort_policy.arguments(message, reasoning_effort, reasoning_reason))
            if replay:
                return replay
            if kind == 'evaluate' and s.get('auto_check'):
                gate = check_runtime.evaluate_gate(b, s, contract)
                if gate:
                    return gate
            provider = s.get('exam_provider', s.get('provider', 'ark')) if kind == 'evaluate' else s.get('provider', 'ark')
            if not self.capacity(p, provider):
                raise b.GateError('Project/provider concurrency limit reached; retry after completion')
            extra = ({'reasoning_effort': reasoning_effort, 'reasoning_reason': reasoning_reason}
                     if reasoning_effort is not None or reasoning_reason is not None else {})
            return (self.launch(p, sid, kind, message, event_id, **extra) if p is not None
                    else b._start_job(sid, kind, message, event_id, **extra))

    def publish(self, p):
        b = self.b
        if not p.get('owner'):
            return
        signature = b.digest({'phase': p['phase'], 'tasks': [(t['id'], t['phase'], t.get('event_id'), t.get('error'),
                                                          (t.get('progress_guard') or {}).get('blocked')) for t in p['tasks']],
                              'integration': p.get('integration', {}).get('packet_id')})
        if signature == p.get('notice_signature'):
            return
        # A running-only transition needs no supervisor action. Keep any
        # outstanding batch (and its existing identity/reminders) alive, but do
        # not create a fresh courier when there is nothing to deliver.
        quiet = (p.get('phase') == 'running' and p.get('continuation') == 'active'
                 and any(t.get('phase') == 'running' or t.get('auto_continue_pending') for t in p['tasks'])
                 and all(t.get('phase') in ('pending', 'starting', 'running', 'accepted') or t.get('auto_continue_pending') for t in p['tasks'])
                 and not any(t.get('error') or (t.get('automatic_checks') and t.get('event_id')) for t in p['tasks']))
        # Never overwrite an in-flight courier event: receive always gathers the
        # latest project state, so one outstanding wake-up covers all completions.
        with b.locked(p['project_id']) as root:
            try:
                state, _ = b.load_session(root)
            except b.GateError as error:
                p['return_error'] = str(error)[:500]
                return
            p.pop('return_error', None)
            event = state['events'].get(state.get('last_event_id'), {})
            if event.get('status') not in (*b.WATCH_EVENTS, 'received'):
                if quiet:
                    return
                state.update(job_id=uuid.uuid4().hex, phase='project_update')
                b.record_event(state)
            b.write_json(root / 'state.json', state)
        p['notice_signature'] = signature
        b.spawn_dispatch(p['project_id'])

    def receive(self, pid, event_id=None, detail='summary'):
        if detail not in ('summary', 'full'):
            raise self.b.GateError('detail must be summary or full')
        b = self.b
        with self.lock():
            p = self.load(pid)
            stored = (p.get('receipts') or {}).get(event_id) if event_id else None
            # detail=full is a historical read-only lookup. The default summary
            # path is the MCP callback continuation: it may still recover a
            # pending progress reminder for unhandled work.
            if detail == 'full':
                if isinstance(stored, dict):
                    return self._receive_result(self._replay_receipt(stored, event_id, pid), detail)
                # Do not refresh children here: status recovery can itself write
                # state. Only the continuation path may acknowledge events.
                return self._receive_result({
                    'proceed': False, 'read_only': True, 'replayed': False,
                    'receipt_available': False, 'event_id': event_id,
                    'project_id': pid, 'events': [],
                    'next_action': 'telos_project_receive',
                    'instruction': 'No stored receipt. Receive with detail="summary" to acknowledge this event.'}, detail)
            p = self.refresh(p)
            if p['continuation'] != 'active' or p['phase'] in ('accepted', 'discussed'):
                return self._receive_result({'proceed': False, 'event_id': event_id, 'events': [], 'project': self.view(p), 'project_id': pid}, detail)
            if p.get('owner'):
                receipt = b.receive_event(pid, event_id)
                if not receipt['proceed']:
                    if isinstance(stored, dict):
                        return self._receive_result(self._replay_receipt(stored, event_id, pid), detail)
                    return self._receive_result({'proceed': False, 'event_id': event_id, 'events': [], 'project': self.view(p), 'project_id': pid}, detail)
            events = []
            for t in p['tasks']:
                if t['phase'] == 'quarantined':
                    continue
                s = self.child(t)
                if s and s.get('last_event_id'):
                    r = b.receive_event(t['session_id'], s['last_event_id'])
                    if r['proceed']:
                        events.append({'task_id': t['id'], **r})
            full = {'proceed': True, 'events': events, 'project': self.view(p), 'project_id': pid,
                    'instruction': 'Follow each child next_action. Answer needs_clarification through telos_send before evaluation; ask the user for unresolved business choices. Review submitted work via evaluate/review/grade; integrate then check/review/finish the project. Never treat child acceptance as project acceptance.'}
            if p.get('kind') == 'discussion':
                full['instruction'] = ('Review independent meeting reports and evidence. Resolve business questions with the user. '
                                       'Use telos_meeting_round for targeted follow-up, or telos_meeting_conclude when ready. '
                                       'Do not evaluate/grade discussion participants as implementation.')
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
            if p.get('kind') == 'discussion':
                raise self.b.GateError('Use telos_meeting_round to change meeting participants')
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
                additions = [dict(task) for task in add_tasks]
                for task in additions:
                    if task.get('task_type', 'implementation') == 'implementation':
                        for key in ('environment_requirements', 'auto_continue_checkpoints', 'auto_run_checks'):
                            if key in p['contract']:
                                task.setdefault(key, p['contract'][key])
                tasks = self.validate_tasks(additions, p['tasks'])
                for t in tasks:
                    self.validate_public_task(t, p['contract'])
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

    def unstarted_audit_normalization(self, p, t, old):
        if t.get('task_type') != 'audit' or t.get('environment_requirements') is None:
            return None
        reason = 'Project environment preparation is for implementation tasks only'
        prior = [h for h in p.get('history', []) if h.get('task_id') == t['id']]
        session_ids = {t['session_id']} | {h['previous_session_id'] for h in prior if h.get('previous_session_id')}
        usage_ids = session_ids | {t.get('usage_id') or t['session_id']}
        paths = ([self.b.run_dir(sid) for sid in session_ids]
                 + [self.b.usage_path(sid) for sid in usage_ids])
        # This legacy error happened before create_session wrote anything. Any
        # child, ledger or residual artifact makes "never executed" uncertain;
        # do not reinterpret an existing frozen contract as a setup-only error.
        if (t.get('phase') != 'setup_error' or old is not None or t.get('error') != reason
                or any(t.get(key) for key in ('base_commit', 'accepted_commit', 'event_id'))
                or any(h.get('previous_worktree') for h in prior)
                or any(path.exists() or path.is_symlink() for path in paths)):
            raise self.b.GateError('Cannot normalize audit environment requirements unless setup failed before any session or execution evidence')
        return {'field': 'environment_requirements', 'previous_value': copy.deepcopy(t['environment_requirements']),
                'reason': reason, 'previous_phase': t['phase'], 'previous_error': t.get('error'),
                'previous_start_error': copy.deepcopy(t.get('start_error'))}

    def reassign(self, pid, task_id, decision_reason, provider=None, model=None, message=None,
                 executor_profile=None, reasoning_effort=None):
        with self.lock():
            p = self.refresh(self.load(pid)); self.active(p)
            if p.get('kind') == 'discussion':
                raise self.b.GateError('Use telos_meeting_round to retire/add discussion participants')
            if not decision_reason.strip() or p['phase'] == 'verifying':
                raise self.b.GateError('Explain reassignment and wait for integration checks')
            t = next((x for x in p['tasks'] if x['id'] == task_id), None)
            if t is None or t['phase'] in ('running', 'stale', 'accepted', 'awaiting_review', 'changed_after_acceptance'):
                raise self.b.GateError('Only unstarted, failed or returned nonaccepted tasks can be reassigned; grade pending work first')
            if t['revision'] >= 3:
                raise self.b.GateError('Task reassignment limit reached')
            old = self.child(t)
            normalization = self.unstarted_audit_normalization(p, t, old)
            new = dict(t)
            if normalization:
                new.pop('environment_requirements')
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
            old_policy = t.get('reasoning_policy')
            if old_policy and old_policy['capability_key'] == effort_policy.capability_key(binding):
                new['reasoning_policy'] = effort_policy.validate({**old_policy,
                    'initial_effort': effort_policy.canonical(binding['reasoning_effort'], old_policy)}, binding)
            else:
                new['reasoning_policy'] = effort_policy.freeze(binding, self.b.config())
            self.backend_fields(new)
            if message is not None:
                if not isinstance(message, str) or not message.strip():
                    raise self.b.GateError('Public handoff message must be nonempty')
                self.b.deny_leak(message, t)
                new['message'] = message
            self.validate_public_task(new, new)
            self.validate_public_task(new, p['contract'])
            if old:
                self.b.return_control(t['session_id'], 'cancel')
            entry = {'at': time.time(), 'task_id': task_id, 'previous_session_id': t['session_id'],
                     'previous_worktree': old.get('worktree') if old else None, 'reason': decision_reason}
            if normalization:
                entry['normalizations'] = [normalization]
            p['history'].append(entry)
            if not new.get('usage_id'):
                # Legacy task: its prior ledger is keyed by the old session ID.
                new['usage_id'] = t['session_id']
            new.update(session_id=uuid.uuid4().hex, phase='pending', revision=t['revision'] + 1)
            for key in ('accepted_commit', 'event_id', 'error', 'start_error', 'base_commit'):
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
            if action == 'cancel':
                p.setdefault('finished_at', time.time())
            self.save(p)  # persisted before touching children or callbacks
            receipts = []
            for sid in [pid] + [t['session_id'] for t in p['tasks'] if self.b.run_dir(t['session_id']).exists()]:
                try:
                    receipts.append(self.b.return_control(sid, action))
                except (self.b.GateError, OSError, ValueError) as error:
                    receipts.append({'session_id': sid, 'action': action, 'applied': False,
                                     'error': str(error)[:500]})
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
        started = time.monotonic()
        try:
            check_contract, dependency_contracts = self.verification_contract(p)
            state = {'session_id': pid, 'worktree': p['integration']['worktree']}
            commit, target = b.snapshot(state, ident)
            audit_reports = {}
            audit_corrections = {}
            deliveries = {}
            for task in p['tasks']:
                child_state, child_contract = b.load_session(b.run_dir(task['session_id']))
                child_packet = b.read_json(b.run_dir(task['session_id']) / f"attempt-{child_state['rounds']}.json")
                if b.digest(child_packet) != child_state.get('packet_id') or child_packet.get('commit') != p['integration']['pins'][task['id']]:
                    raise b.GateError('Child delivery/evidence changed after acceptance')
                deliveries[task['id']] = {'session_id': task['session_id'],
                    'commit': child_packet['commit'], 'packet_id': child_state['packet_id'],
                    'delivery': child_packet.get('delivery')}
                if child_contract.get('task_type') == 'audit':
                    import audit
                    import audit_check_correction
                    if correction := audit_check_correction.public(child_state):
                        audit_corrections[task['id']] = correction
                    evidence = child_packet['audit']
                    audit.verify(b, child_state, child_contract, evidence)
                    audit_reports[task['id']] = {'evidence': evidence, 'review': child_state['acceptance']['audit_review']}
                    shutil.copytree(evidence['evidence_dir'], target / '.telos-audits' / task['id'])
            checks = b.executable_checks(check_contract, target)
            packet = {'project_id': pid, 'commit': commit, 'pins': p['integration']['pins'],
                      'contract_hash': p['contract_hash'], 'checks': checks,
                      'deliveries': deliveries,
                      'dependency_paths': check_contract['dependency_paths'],
                      'dependency_contracts': dependency_contracts}
            if audit_reports:
                packet['audits'] = audit_reports
            if audit_corrections:
                packet['audit_check_corrections'] = audit_corrections
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
            import timing
            timing.finish_checks(current, time.monotonic() - started)
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
                    'job_budget': project_budget.summary(p),
                    'work_turn_budget': project_budget.work_turn_summary(
                        p, int(self.b.config()['max_turns'])),
                    'blockers': (p.get('final_review') or {}).get('blockers', []),
                    'final_review': p.get('final_review'), 'usage': self._usage(p),
                    'timing': self._timing(p),
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
            import timing
            timing.mark(p, 'review_completed', packet_id)
            if passed:
                p['finished_at'] = time.time()
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
                                import timing
                                current['check_timing'] = timing.check_timing(current)
                                current['check_timing']['complete'] = False
                                current['integration']['error'] = 'Verification process exited without a result; rerun checks'
                                self.publish(current); self.save(current)
                            p = current
                if p['phase'] == 'running' and p['continuation'] == 'active':
                    self.tick(p['project_id'])
            except Exception as e:
                self.b.recovery_error(path.parent / 'recovery-error.json', e)

    def call(self, name, args, caller):
        import inspect
        name = canonical_tool_name(name)
        if name == 'telos_project_open':
            try:
                inspect.signature(self.create).bind(**args, caller=caller)
            except TypeError:
                raise self.b.GateError('Missing required or unsupported tool arguments; consult the tool schema',
                                       code='invalid_arguments', next_action='correct_arguments') from None
            return self.create(**args, caller=caller)
        if 'project_id' not in args:
            raise self.b.GateError('project_id is required', code='invalid_arguments', next_action='correct_arguments')
        pid = args['project_id']
        if name == 'telos_project_budget':
            rest = {k: v for k, v in args.items() if k != 'project_id'}
            try:
                inspect.signature(self.budget).bind(pid, caller=caller, **rest)
            except TypeError:
                raise self.b.GateError('Missing required or unsupported tool arguments; consult the tool schema',
                                       code='invalid_arguments', next_action='correct_arguments') from None
            return self.budget(pid, caller=caller, **rest)
        self.authorize(self.load(pid), caller)
        rest = {k: v for k, v in args.items() if k != 'project_id'}
        mapping = {'start': self.tick, 'status': self.status, 'receive': self.receive, 'update': self.update,
                   'control': self.control, 'integrate': self.integrate, 'check': self.check, 'review': self.review,
                   'finish': self.finish, 'resolve': self.resolve, 'reassign': self.reassign}
        method = mapping[name.removeprefix('telos_project_')]
        try:
            inspect.signature(method).bind(pid, **rest)
        except TypeError:
            raise self.b.GateError('Missing required or unsupported tool arguments; consult the tool schema',
                                   code='invalid_arguments', next_action='correct_arguments') from None
        return method(pid, **rest)

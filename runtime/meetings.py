"""Public, read-only design discussions before implementation contracts exist."""
import json
from pathlib import Path
import re
import time
import uuid
import effort_policy
import progress_guard
import roles


GUIDANCE = (
    'You are a design meeting participant, not an implementation worker. Read the scoped repository and '
    'verify technical facts. Source is read-only; use TELOS_DISCUSSION_SCRATCH for temporary experiments. '
    'Do not implement, commit, modify source, call MCP/subagents, or claim acceptance. '
    'Give your independent assessment before considering peer opinions. In follow-up rounds address only '
    'the supplied disagreements, use evidence or small experiments, and revise your position when warranted. '
    'End the turn with JSON: summary (string), proposal (string), risks, questions, evidence '
    '(arrays of strings). Questions are unresolved decisions needed before implementation; evidence should '
    'name inspected code or actual commands/results. Never invent measurements. The supervisor resolves '
    'technical disagreements and asks the user about business choices. Do not wait inside this process.\n'
)


def report(b, state, result):
    raw = result.get('summary', '').strip()
    if raw.startswith('```json\n') and raw.endswith('```'):
        raw = raw[8:-3].strip()
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = None
    if (result.get('summary_truncated') or not isinstance(value, dict)
            or any(not isinstance(value.get(k), str) or not value[k].strip() for k in ('summary', 'proposal'))
            or any(not isinstance(value.get(k), list) or any(not isinstance(v, str) or not v.strip() for v in value[k])
                   for k in ('risks', 'questions', 'evidence'))):
        raise b.GateError('Invalid meeting report; request summary, proposal, risks, questions and evidence JSON.',
                          code='discussion_format', next_action='telos_meeting_round')
    record = {k: value[k] for k in ('summary', 'proposal', 'risks', 'questions', 'evidence')}
    record.update(session_id=state['session_id'], job_id=state['job_id'],
                  executor_thread_id=result['thread_id'], trust='executor_claims', recorded_at=time.time())
    record['report_id'] = b.digest(record)
    return record


class Meetings:
    def __init__(self, b):
        self.b = b
        self.projects = b.project_manager()

    def load(self, meeting_id):
        p = self.projects.load(meeting_id)
        if p.get('kind') != 'discussion':
            raise self.b.GateError('Not a design meeting')
        return p

    def participants(self, values, existing=()):
        b = self.b
        ids = {t['id'] for t in existing}
        if not isinstance(values, list) or len(values) + len(existing) > 30:
            raise b.GateError('A meeting supports at most 30 participants; concurrency is separate')
        out = []
        for raw in values:
            if not isinstance(raw, dict) or set(raw) - {'id', 'role', 'executor_profile', 'provider', 'model', 'reasoning_effort', 'reasoning_policy', 'role_profile', 'experience_tags'}:
                raise b.GateError('Invalid meeting participant fields')
            ident, role = raw.get('id'), raw.get('role')
            if (not isinstance(ident, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', ident)
                    or ident in ids or not isinstance(role, str) or not role.strip()):
                raise b.GateError('Participants need unique IDs and concrete roles')
            ids.add(ident)
            snapshot = roles.freeze(b.config(), raw.get('role_profile'))
            binding = b.providers.resolve_executor(b.config(), **roles.executor_options(snapshot,
                **{k: raw.get(k) for k in ('executor_profile', 'provider', 'model', 'reasoning_effort')}))
            task = {'id': ident, 'role': role, 'task': 'Design assessment: ' + role,
                    'message': 'Independently assess the requirement from your role. Inspect relevant code and return your report.',
                    'depends_on': [], 'allowed_paths': [], 'questions': [], 'checks': [],
                    'task_type': 'discussion', 'executor': binding, 'workflow_version': 3,
                    'threshold': 90, 'max_rounds': 5, 'assessment_mode': 'closed_book',
                    'budget': None, 'usage_id': uuid.uuid4().hex,
                    'session_id': uuid.uuid4().hex, 'phase': 'pending', 'revision': 1, 'active': True}
            self.projects.backend_fields(task)
            if snapshot:
                task['role_snapshot'] = snapshot
            task['experience_tags'] = roles.tags(raw.get('experience_tags'))
            self.projects.validate_public_task(task, task)
            task['reasoning_policy'] = effort_policy.freeze(binding, b.config(), raw.get('reasoning_policy'))
            out.append(task)
        return out

    def create(self, repo, requirement, participants, decision_reason, context='', max_parallel=3,
               notify=True, caller=None, single_executor_reason=None, dependency_paths=None):
        b = self.b
        if not isinstance(requirement, str) or not requirement.strip() or not isinstance(context, str):
            raise b.GateError('Provide the requirement and public context')
        if not isinstance(decision_reason, str) or not decision_reason.strip():
            raise b.GateError('Explain participant count and roles based on the requirement')
        tasks = self.participants(participants)
        if not tasks or len(tasks) < 2 and not (isinstance(single_executor_reason, str) and single_executor_reason.strip()):
            raise b.GateError('Meetings default to multiple participants; explain any single-executor exception')
        if type(max_parallel) is not int or not 1 <= max_parallel <= 3:
            raise b.GateError('max_parallel must be 1-3; queue additional participants')
        repo = Path(b.git(repo, 'rev-parse', '--show-toplevel')).resolve()
        if b.git(repo, 'status', '--porcelain'):
            raise b.GateError('Prepare a clean source checkout for a pinned read-only meeting')
        dependencies = b.validate_dependencies(dependency_paths or [])
        owner = b.bind_owner(caller) if notify else None
        mid = uuid.uuid4().hex
        contract = {'task': requirement, 'context': context, 'questions': [], 'checks': [],
                    'dependency_paths': dependencies, 'task_type': 'discussion'}
        for t in tasks:
            t['context'] = requirement + '\n' + context + '\nYour role: ' + t['role']
            t['dependency_paths'] = dependencies
        p = {'project_id': mid, 'kind': 'discussion', 'repo': str(repo),
             'base_commit': b.git(repo, 'rev-parse', 'HEAD'), 'owner': owner,
             'execution_mode': 'parallel', 'strategy': 'parallel', 'max_parallel': max_parallel,
             'max_jobs': None, 'jobs_started': 0, 'phase': 'planned', 'continuation': 'active',
             'decision_reason': decision_reason, 'single_executor_reason': single_executor_reason,
             'tasks': tasks, 'use_experience': True, 'contract': contract, 'contract_hash': b.digest(contract),
             'history': [], 'created_at': time.time(), 'discussion_rounds': [],
             'check_timing': {'observed_seconds': 0.0, 'completed_runs': 0, 'complete': True}}
        return self.projects.initialize(p, courier_model='meeting')

    def observations(self, p):
        """Use immutable per-job reports, including prior rounds and retired experts."""
        reports, questions = [], []
        for t in p['tasks']:
            for path in sorted(self.b.run_dir(t['session_id']).glob('discussion-*.json')):
                r = self.b.read_json(path)
                expected = r.get('report_id')
                if self.b.digest({k: v for k, v in r.items() if k != 'report_id'}) != expected:
                    raise self.b.GateError('Meeting evidence changed')
                reports.append({'participant_id': t['id'], 'role': t['role'], **r})
                for index, question in enumerate(r['questions']):
                    questions.append({'id': r['job_id'] + ':' + str(index), 'participant_id': t['id'], 'question': question})
        return reports, questions

    def view(self, p):
        reports, questions = self.observations(p)
        blockers = [{'participant_id': t['id'], 'session_id': t['session_id'], 'progress_guard': t['progress_guard']}
                    for t in p['tasks'] if t.get('active', True) and (t.get('progress_guard') or {}).get('blocked')]
        return {'meeting_id': p['project_id'], 'rounds': p.get('discussion_rounds', []),
                'progress_guard': progress_guard.view(p),
                'blockers': blockers,
                'reports': reports, 'questions': questions, 'conclusion': p.get('conclusion'),
                'discussion_complete': p['phase'] == 'discussed',
                'next_action': ('create_implementation_contract' if p['phase'] == 'discussed' else
                                'telos_project_start' if p['phase'] == 'planned' else
                                'conclude_or_replan_meeting' if progress_guard.blocked(p) else
                                'conclude_or_replan_blocked_participants' if blockers else
                                'await_participant_callbacks' if any(t['phase'] in ('pending', 'ready', 'running') or t.get('discussion_pending') for t in p['tasks'] if t.get('active', True)) else
                                'review_opinions_then_round_or_conclude'),
                'note': 'Discussion is not code acceptance. No default token, round or job cap; repeated reports trigger the durable no-progress guard. Existing provider concurrency and per-call timeouts apply.'}

    def observe_progress(self, p):
        """Count settled rounds, not callbacks, participants or scheduler polls."""
        if 'progress_guard' not in p:
            return
        by_id = {t['id']: t for t in p['tasks']}
        for row in p['discussion_rounds']:
            observation = 'round:' + row['request_id']
            if observation in p['progress_guard']['observed']:
                continue
            targets = [by_id[m['participant_id']] for m in row['messages']]
            targets += [by_id[t['id']] for t in row['add_participants']]
            if not targets:
                continue  # Retirement is administrative, not a model attempt.
            if any(t.get('discussion_pending') or t['phase'] in ('pending', 'ready', 'starting', 'running') for t in targets):
                continue
            facts = []
            for t in targets:
                try:
                    s, _ = self.b.load_session(self.b.run_dir(t['session_id']))
                except (self.b.GateError, OSError, ValueError):
                    continue
                if s['phase'] == 'discussion_result':
                    facts.extend(progress_guard.discussion_facts(s.get('delivery') or {}))
            progress_guard.observe(p, 'discussion', observation, facts,
                                  'Compared round proposals, risks, questions and evidence across participants; opinions remain claims.')

    def validated_conclusion(self, p):
        conclusion = self.b.read_json(self.projects.path(p['project_id']).parent / 'discussion-conclusion.json')
        if (conclusion != p.get('conclusion') or
                self.b.digest({k: v for k, v in conclusion.items() if k != 'id'}) != conclusion.get('id')):
            raise self.b.GateError('Meeting conclusion changed')
        reports, _ = self.observations(p)
        if [r['report_id'] for r in reports] != conclusion['report_ids']:
            raise self.b.GateError('Meeting evidence changed after conclusion')
        return conclusion

    def round(self, meeting_id, request_id, messages, decision_reason, add_participants=None, retire_participants=None):
        b = self.b
        args = dict(messages=messages, decision_reason=decision_reason,
                    add_participants=add_participants or [], retire_participants=retire_participants or [])
        if not isinstance(request_id, str) or not request_id.strip() or not decision_reason.strip():
            raise b.GateError('Provide a stable request_id and explain the remaining disagreements')
        with self.projects.lock():
            p = self.projects.refresh(self.load(meeting_id)); self.projects.active(p)
            for row in p['discussion_rounds']:
                if row['request_id'] == request_id:
                    if row['arguments_hash'] != b.digest(args):
                        raise b.GateError('Round request_id reused with different arguments')
                    return {'duplicate': True, 'meeting_id': meeting_id, 'round': row['round'],
                            'next_action': 'telos_project_status'}
            if p['phase'] == 'discussed':
                raise b.GateError('Meeting concluded; create implementation from its plan')
            if messages or add_participants:
                progress_guard.check(p)
            new = self.participants(add_participants or [], p['tasks'])
            by_id = {t['id']: t for t in p['tasks']}
            retiring = retire_participants or []
            if not isinstance(messages, list) or not isinstance(retiring, list):
                raise b.GateError('Provide participant messages and retirement IDs as lists')
            selected = set()
            for msg in messages:
                if (not isinstance(msg, dict) or not {'participant_id', 'message'} <= set(msg)
                        or set(msg) - {'participant_id', 'message', 'reasoning_effort', 'reasoning_reason'}
                        or not isinstance(msg['message'], str) or not msg['message'].strip()):
                    raise b.GateError('Each follow-up needs participant_id and a focused public message')
                ident = msg['participant_id']
                t = by_id.get(ident)
                if (not t or ident in selected or not t.get('active', True) or t.get('discussion_pending')
                        or t['phase'] not in ('discussion_result', 'error')):
                    raise b.GateError('Follow up once per returned active participant; wait for running work')
                if ident in retiring:
                    raise b.GateError('Cannot follow up and retire the same participant')
                state, contract = b.load_session(b.run_dir(t['session_id']))
                progress_guard.check(state)
                effort_policy.select(contract, state, msg.get('reasoning_effort'), msg.get('reasoning_reason'))
                selected.add(ident)
            for ident in retiring:
                if ident not in by_id or by_id[ident]['phase'] in ('running', 'stale'):
                    raise b.GateError('Retire only known participants whose work has stopped')
            if not messages and not new and not retiring:
                raise b.GateError('Round must address a disagreement, add expertise or retire a participant')
            if 'progress_guard' not in p:
                reports, _ = self.observations(p)
                initial = set(f for report in reports for f in progress_guard.discussion_facts(report))
                progress_guard.guard(p)['streams']['discussion'] = {'seen': sorted(initial), 'streak': 0}
                # Old rounds are historical evidence, never retroactive failures.
                p['progress_guard']['observed'] = ['round:' + r['request_id'] for r in p['discussion_rounds']]
            trial = p['progress_guard'].get('trial')
            if trial:
                plan = next(x for x in p['progress_guard']['plans'] if x['request_id'] == trial)
                messages = [{**msg, 'message': msg['message'] + '\nSupervisor adjusted plan:\n' + b.encoded(plan['fields'])}
                            for msg in messages]
                for t in new:
                    t['message'] += '\nSupervisor adjusted plan:\n' + b.encoded(plan['fields'])
            if messages or new:
                progress_guard.consume(p)
            row = {'round': len(p['discussion_rounds']) + 1, 'request_id': request_id,
                   'arguments_hash': b.digest(args), **args, 'at': time.time()}
            p['discussion_rounds'].append(row)
            for msg in messages:
                t = by_id[msg['participant_id']]
                t['discussion_pending'] = {'request_id': request_id, **{k: v for k, v in msg.items() if k != 'participant_id'}}
            for ident in retiring:
                by_id[ident]['active'] = False
                by_id[ident].pop('discussion_pending', None)
            for t in new:
                t['context'] = p['contract']['task'] + '\n' + p['contract']['context'] + '\nYour role: ' + t['role']
                t['dependency_paths'] = p['contract']['dependency_paths']
            p['tasks'].extend(new)
            self.projects.save(p)  # queue is durable before any executor starts
        return self.projects.tick(meeting_id)

    def launch_pending(self, p, t):
        """Called under coordinator lock; deduplicate across spawn/save crashes."""
        pending = t.get('discussion_pending')
        if not pending or not t.get('active', True):
            return
        b = self.b
        s, _ = b.load_session(b.run_dir(t['session_id']))
        marker = b.digest([p['project_id'], t['id'], pending['request_id']])
        if s.get('discussion_request_id') == marker:
            t.pop('discussion_pending', None)
            return
        if s['phase'] not in ('discussion_result', 'error') or not self.projects.capacity(p, t['provider']):
            return
        eid = s.get('last_event_id')
        if eid:
            if not b.receive_event(t['session_id'], eid)['proceed']:
                raise b.GateError('Meeting participant receipt is not actionable')
        # Marker and running job are persisted together by _start_job.
        self.projects.launch(p, t['session_id'], 'send', pending['message'], eid, discussion_request_id=marker,
                             reasoning_effort=pending.get('reasoning_effort'), reasoning_reason=pending.get('reasoning_reason'),
                             automatic=True)
        t.pop('discussion_pending', None)

    def conclude(self, meeting_id, plan, answers, rationale):
        b = self.b
        if (not isinstance(plan, dict) or set(plan) != {'requirements', 'implementation', 'workstreams', 'verification'}
                or any(not isinstance(plan[k], list) or not plan[k] or any(not isinstance(v, str) or not v.strip() for v in plan[k]) for k in plan)
                or not isinstance(rationale, str) or not rationale.strip()):
            raise b.GateError('Provide requirements, implementation, workstreams and verification as nonempty lists, with a decision rationale')
        with self.projects.lock():
            p = self.projects.refresh(self.load(meeting_id)); self.projects.active(p)
            args = {'plan': plan, 'answers': answers, 'rationale': rationale}
            if p.get('conclusion'):
                self.validated_conclusion(p)
                if p['conclusion']['arguments_hash'] != b.digest(args):
                    raise b.GateError('Meeting conclusion is frozen')
            else:
                if any(t['phase'] in ('running', 'stale') or t.get('discussion_pending') or
                       t.get('active', True) and t['phase'] != 'discussion_result' for t in p['tasks']):
                    raise b.GateError('Wait for participant reports; resolve failed or queued rounds before concluding')
                reports, questions = self.observations(p)
                required = 1 if p.get('single_executor_reason') else 2
                if len({r['participant_id'] for r in reports}) < required:
                    raise b.GateError('Need reports from multiple independent participants before concluding')
                if not isinstance(answers, list) or any(not isinstance(a, dict) or set(a) != {'question_id', 'answer'}
                        or not isinstance(a['answer'], str) or not a['answer'].strip() for a in answers):
                    raise b.GateError('Resolve each question with question_id and a concrete answer')
                if len({a['question_id'] for a in answers}) != len(answers) or {a['question_id'] for a in answers} != {q['id'] for q in questions}:
                    raise b.GateError('Every recorded question must be resolved before implementation')
                conclusion = {**args, 'arguments_hash': b.digest(args), 'report_ids': [r['report_id'] for r in reports],
                              'source_commit': p['base_commit'], 'at': time.time(), 'accepted': False}
                conclusion['id'] = b.digest(conclusion)
                b.write_json(self.projects.path(meeting_id).parent / 'discussion-conclusion.json', conclusion)
                p.update(conclusion=conclusion, phase='discussed', finished_at=time.time())
                self.projects.save(p)
            # Idempotent terminal receipts; no code/grade acceptance is fabricated.
            for sid in [meeting_id] + [t['session_id'] for t in p['tasks'] if b.run_dir(t['session_id']).exists()]:
                with b.locked(sid) as root:
                    s, _ = b.load_session(root)
                    b.retire_reminders(s, complete=True)
                    s['discussion_complete'] = True
                    import timing
                    timing.transition(s, None)
                    b.write_json(root / 'state.json', s)
        return self.projects.status(meeting_id)

    def prepare_implementation(self, args, caller):
        """Only public conclusions cross into a new, separately frozen contract."""
        b = self.b
        mid = args.pop('meeting_id', None)
        reason = args.pop('skip_meeting_reason', None)
        if not mid:
            mode = b.config().get('planning_mode', 'multi_executor')
            if mode not in ('multi_executor', 'direct'):
                raise b.GateError('planning_mode must be multi_executor or direct')
            if mode == 'multi_executor' and not (isinstance(reason, str) and reason.strip()):
                raise b.GateError('Start a multi-executor design meeting before freezing implementation; provide skip_meeting_reason only for an explicit direct-work exception.',
                                  code='meeting_required', next_action='telos_meeting_open')
            args['_planning'] = {'mode': 'direct', 'reason': reason}
            return
        p = self.load(mid); self.projects.authorize(p, caller)
        if p['phase'] != 'discussed' or p.get('continuation') != 'active':
            raise b.GateError('Conclude the active meeting before implementation')
        conclusion = self.validated_conclusion(p)
        repo = str(Path(b.git(args['repo'], 'rev-parse', '--show-toplevel')).resolve())
        if repo != p['repo'] or b.git(repo, 'rev-parse', 'HEAD') != p['base_commit']:
            raise b.GateError('Implementation source differs from the discussed source; review it in a new meeting')
        args['context'] = args.get('context', '') + '\nConfirmed public meeting plan:\n' + b.encoded({k: conclusion[k] for k in ('plan', 'answers', 'rationale')})
        args['_planning'] = {'mode': 'multi_executor', 'meeting_id': mid, 'conclusion_id': conclusion['id']}

    def call(self, name, args, caller):
        if name == 'telos_meeting_open':
            return self.b.invoke_tool(self.create, args, caller=caller)
        if 'meeting_id' not in args:
            raise self.b.GateError('meeting_id is required', code='invalid_arguments', next_action='correct_arguments')
        p = self.load(args['meeting_id']); self.projects.authorize(p, caller)
        return self.b.invoke_tool(self.round if name == 'telos_meeting_round' else self.conclude, args)

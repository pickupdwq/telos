"""Durable convergence guard, independent of tokens, job IDs and model effort.

Evidence is deliberately conservative: file contents, newly passing checks, or
new discussion propositions. Novel text is not proof of correctness; acceptance
still belongs to the existing checks/exam/supervisor gates.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from serialization import digest
from providers import ProviderError as GateError

LIMIT = 3
REPLAN_LIMIT = 2


def guard(state):
    return state.setdefault('progress_guard', {'version': 1, 'streams': {}, 'observed': [],
                                               'plans': [], 'replans_without_progress': 0})


def normalize(value):
    return ' '.join(value.split()).casefold()


def source_evidence(b, state, contract):
    """Hash actual working files without touching the Git index or creating commits."""
    import audit
    root = Path(state['worktree'])
    try:
        if contract.get('task_type') == 'audit':
            root = audit.artifacts(b, state)
            # Report data is the read-only investigator's deliverable. Ignore
            # broker receipts/timestamps, which can grow on identical probes.
            report = audit.read_regular(root, 'report.json', 2_000_000)
            return [digest(json.loads(report))], None
        else:
            names = sorted(set(b.git(root, 'ls-files', '-z', '--cached', '--others',
                                     '--exclude-standard', raw=True).decode(errors='surrogateescape').split('\0')) - {''})
        rows, size, deadline = [], 0, time.monotonic() + 5
        for name in names:
            path = root / name
            # Never follow symlinks (including replaced parent directories).
            if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
                raise ValueError('redirected parent')
            try:
                info = path.lstat()
            except FileNotFoundError:
                rows.append([name, 'deleted']); continue
            if stat.S_ISLNK(info.st_mode):
                rows.append([name, 'link', os.readlink(path)]); continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('nonregular evidence')
            size += info.st_size
            if size > 256 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError('evidence scan limit')
            h = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    h.update(chunk)
                    if time.monotonic() > deadline:
                        raise ValueError('evidence scan deadline')
            rows.append([name, bool(info.st_mode & 0o111), h.hexdigest()])
        return [digest(rows)], None
    except (OSError, ValueError, GateError):
        return [], 'Source evidence unavailable or exceeds the bounded scan; inspect artifacts before replanning.'


def seed(b, state, contract):
    g = guard(state)
    if contract.get('task_type') != 'discussion' and 'work' not in g['streams']:
        facts, note = source_evidence(b, state, contract)
        g['streams']['work'] = {'seen': facts, 'streak': 0, 'note': note}


def blocked(state):
    if state.get('phase') in ('accepted', 'discussed') or state.get('discussion_complete') or state.get('clarification'):
        return False
    return any(v['streak'] >= LIMIT for v in state.get('progress_guard', {}).get('streams', {}).values())


def view(state):
    g = state.get('progress_guard')
    if not g:
        return None
    stopped = blocked(state)
    exhausted = g.get('replans_without_progress', 0) >= REPLAN_LIMIT and not g.get('trial')
    return {'blocked': stopped, 'attempt_allowed': not stopped or bool(g.get('trial')),
            'limit': LIMIT, 'replan_trials_remaining': max(0, REPLAN_LIMIT - g.get('replans_without_progress', 0)),
            'trial': g.get('trial'), 'retry_at': g.get('retry_at', 0),
            'streams': {k: {key: v.get(key) for key in ('streak', 'note', 'last_observation', 'new_evidence')}
                        for k, v in g['streams'].items()},
            'next_action': ('report_blocker_to_user' if exhausted else 'telos_replan') if stopped else None,
            'requirement': ('Report the blocker and retained artifacts. Supply a different approach, evidence supporting it, '
                            'and an observable success criterion. Do not repeat calls or merely raise reasoning effort.')
                           if stopped else None}


def check(state):
    if blocked(state) and not state['progress_guard'].get('trial'):
        v = view(state)
        raise GateError('No substantive progress in consecutive attempts; artifacts are retained. ' + v['requirement'],
                        code='no_progress', next_action=v['next_action'])


def consume(state):
    g = guard(state)
    if g.pop('trial', None):
        g['replans_without_progress'] += 1


def observe(state, stream, observation, facts=(), note=None):
    g = guard(state)
    if observation in g['observed']:
        return
    g['observed'].append(observation)
    row = g['streams'].setdefault(stream, {'seen': [], 'streak': 0})
    new = sorted(set(facts) - set(row['seen']))
    row['seen'].extend(new)
    row.update(streak=0 if new else row['streak'] + 1, last_observation=observation,
               new_evidence=len(new), note=note or ('New observable evidence recorded.' if new else
                                                   'No new observable evidence; previous results repeated.'))
    if new:
        g['replans_without_progress'] = 0
        # A changed implementation permits its failed checks to be retried.
        if stream == 'work' and 'checks' in g['streams']:
            g['streams']['checks']['streak'] = 0
        if stream == 'checks' and 'work' in g['streams']:
            g['streams']['work']['streak'] = 0
    if blocked(state):
        g.pop('trial', None)


def discussion_facts(receipt):
    return [digest([key, normalize(item)]) for key in ('proposal', 'risks', 'questions', 'evidence')
            for item in ([receipt[key]] if key == 'proposal' and key in receipt else receipt.get(key, []))]


def completed(b, state, contract, request):
    try:
        _completed(b, state, contract, request)
    except (OSError, ValueError, TypeError, KeyError):
        # Evidence collection must not strand a finished executor in running.
        observe(state, 'work' if request['kind'] == 'send' else 'checks', state['job_id'],
                note='Progress evidence could not be read; inspect the retained artifacts before retrying.')


def _completed(b, state, contract, request):
    if state.get('clarification'):
        # Waiting on a business decision is not an execution retry failure.
        return
    kind = request['kind']
    if kind == 'send':
        if contract.get('task_type') == 'discussion':
            return  # The meeting compares whole rounds, including replacement participants.
        else:
            facts, note = source_evidence(b, state, contract)
            observe(state, 'work', state['job_id'], facts, note)
    else:
        path = b.run_dir(state['session_id']) / f"job-{state['job_id']}-checks.json"
        evidence = b.read_json(path) if path.exists() else {}
        facts = [digest(x['id']) for x in evidence.get('checks', []) if x.get('status') == 'passed']
        observe(state, 'checks', state['job_id'], facts,
                'Compared newly passing check IDs; timestamps, logs, errors and job IDs do not count as progress.')


def startup_failure(state, observation, note):
    observe(state, 'startup', observation, note=note)
    g = guard(state)
    streak = g['streams']['startup']['streak']
    g['retry_at'] = time.time() + min(60, 10 * 2 ** min(streak - 1, 3))


def started(state):
    g = guard(state)
    if 'startup' in g['streams']:
        g['streams']['startup']['streak'] = 0
    g.pop('retry_at', None)


def replan(b, sid, request_id, blocker, approach, evidence, success_criterion):
    fields = dict(blocker=blocker, approach=approach, evidence=evidence, success_criterion=success_criterion)
    if any(not isinstance(x, str) or not x.strip() or len(x) > 4000 for x in [request_id, *fields.values()]):
        raise GateError('Replan requires bounded nonempty request_id, blocker, approach, evidence and success_criterion')
    # Coordinator -> child lock is the same ordering as dispatch/control.
    with b.project_manager().lock(), b.locked(sid) as root:
        state, contract = b.load_session(root)
        project = None
        if state.get('project_id'):
            project = b.project_manager().load(state['project_id'])
            if state.get('notification_only'):
                project = b.project_manager().refresh(project)
            b.project_manager().active(project)
        if (state.get('continuation', 'active') != 'active' or state.get('archived') or
                state['phase'] in ('accepted', 'running', 'awaiting_review') or state.get('discussion_complete')):
            raise GateError('Replan requires an active stopped task; honor user controls and pending review')
        for value in fields.values():
            b.deny_leak(value, contract)
        meeting = project if state.get('notification_only') and project and project.get('kind') == 'discussion' else None
        if meeting:
            if meeting['phase'] == 'discussed':
                raise GateError('Meeting is already concluded')
            state = meeting
        g = guard(state)
        plan_hash = digest([normalize(approach), normalize(evidence), normalize(success_criterion)])
        for plan in g['plans']:
            if plan['request_id'] == request_id:
                if plan['fields'] != fields:
                    raise GateError('Replan request_id reused with different arguments')
                return {'duplicate': True, 'session_id': sid, 'progress_guard': view(state)}
        if not blocked(state):
            raise GateError('Task is not blocked by the no-progress guard')
        if g.get('trial'):
            raise GateError('Use the already granted replan trial before submitting another plan')
        if g['replans_without_progress'] >= REPLAN_LIMIT:
            raise GateError('Two adjusted plans produced no new evidence. Report the blocker and seek a user decision; do not create another automatic retry.',
                            code='no_progress', next_action='report_blocker_to_user')
        if any(p['hash'] == plan_hash for p in g['plans']):
            raise GateError('This approach and evidence were already tried; provide a materially different plan',
                            code='repeated_plan', next_action='report_blocker_and_adjust_plan')
        g['plans'].append({'request_id': request_id, 'hash': plan_hash, 'fields': fields, 'at': time.time()})
        g['trial'] = request_id
        g.pop('retry_at', None)
        if meeting:
            b.project_manager().save(meeting)
        else:
            b.write_json(root / 'state.json', state)
        return {'session_id': sid, 'progress_guard': view(state),
                'next_action': 'retry_once_with_adjusted_plan', 'accepted': False}

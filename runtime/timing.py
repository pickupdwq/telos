"""Observational timing only: never used for budgets, scheduling or acceptance.

Request durations are cumulative (possibly overlapping), not wall time. Lifecycle
intervals use persisted wall timestamps so they survive process restarts; clock
regressions and missing historical intervals remain explicitly incomplete.
"""
import math
import time

LEGACY_STAGES = ('idle', 'work', 'checks', 'exam', 'callback_wait', 'supervisor_wait', 'paused')
STAGES = (*LEGACY_STAGES, 'scheduler_wait')
COUNTERS = ('requests', 'measured_requests', 'missing_requests', 'in_flight_requests')


def number(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _latency():
    return {**{key: 0 for key in COUNTERS}, 'observed_seconds': 0.0, 'complete': True}


def request_latency(entries):
    result = _latency()
    result['phases'] = {phase: _latency() for phase in ('work', 'exam')}
    for entry in entries:
        phase = 'exam' if entry.get('phase') == 'exam' else 'work'
        for summary in (result, result['phases'][phase]):
            summary['requests'] += 1
            if entry.get('status') == 'reserved':
                summary['in_flight_requests'] += 1
            elif number(entry.get('latency_ms')):
                summary['measured_requests'] += 1
                summary['observed_seconds'] += entry['latency_ms'] / 1000
            else:
                summary['missing_requests'] += 1
    for summary in (result, *result['phases'].values()):
        summary['complete'] = not (summary['missing_requests'] or summary['in_flight_requests'])
        summary['observed_seconds'] = round(summary['observed_seconds'], 6)
    return result


def merge_latency(summaries):
    result = request_latency([])
    result['unavailable_sources'] = 0
    for summary in summaries:
        if not isinstance(summary, dict):
            result['unavailable_sources'] += 1
            result['complete'] = False
            for phase in result['phases'].values():
                phase['complete'] = False
            continue
        phases = summary.get('phases') if isinstance(summary.get('phases'), dict) else {}
        for target, source in [(result, summary), *(
                (result['phases'][phase], phases.get(phase))
                for phase in result['phases'])]:
            if not isinstance(source, dict):
                target['complete'] = False
                continue
            target['complete'] = target['complete'] and source.get('complete') is True
            for key in (*COUNTERS, 'observed_seconds'):
                value = source.get(key)
                if number(value) and (key == 'observed_seconds' or type(value) is int):
                    target[key] += value
                else:
                    target['complete'] = False
    for summary in (result, *result['phases'].values()):
        summary['observed_seconds'] = round(summary['observed_seconds'], 6)
    result['complete'] = result['complete'] and all(p['complete'] for p in result['phases'].values())
    return result


def initialize(state, at=None):
    at = time.time() if at is None else at
    state.setdefault('stage_timeline', {'version': 1, 'tracked_since': at, 'complete': True, 'intervals': {}, 'milestones': {}})
    state['timing'] = {'version': 2, 'started_at': at, 'since': at, 'stage': 'idle',
                       'scheduler_wait_tracked_since': at,
                       'phase_seconds': {stage: 0.0 for stage in STAGES}, 'complete': True}


def _valid(value):
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] not in (1, 2):
        return False
    stages = LEGACY_STAGES if value['version'] == 1 else STAGES
    return (value.get('stage') in (*stages, None)
            and number(value.get('started_at')) and number(value.get('since'))
            and isinstance(value.get('phase_seconds'), dict)
            and all(number(value['phase_seconds'].get(stage)) for stage in stages)
            and (value['version'] == 1 or number(value.get('scheduler_wait_tracked_since'))))


def transition(state, stage, at=None, *, interrupted=False):
    """Update only a caller-owned state object, under its existing state lock."""
    if state.get('notification_only'):
        return
    if stage is not None and stage not in STAGES:
        raise ValueError('Unknown timing stage')
    at = time.time() if at is None else at
    if not _valid(state.get('timing')):
        initialize(state, at)
        state['timing']['complete'] = False  # Never backfill historical durations.
        state['stage_timeline']['complete'] = False
    timing = state['timing']
    if timing['version'] == 1:
        # Observational schema migration only: retain old counters and their
        # attribution. Historical scheduler waiting cannot be inferred as zero.
        timing['phase_seconds']['scheduler_wait'] = 0.0
        timing.update(version=2, scheduler_wait_tracked_since=at)
    previous, since = timing.get('stage'), timing.get('since')
    if previous == stage and not interrupted:
        if not number(at) or at < since:
            timing['complete'] = False
        return
    if previous in STAGES:
        if not interrupted and number(since) and number(at) and at >= since:
            timing['phase_seconds'][previous] += at - since
        else:
            timing['complete'] = False
    timing.update(stage=stage, since=at)
    if stage is None:
        timing['finished_at'] = at
    else:
        timing.pop('finished_at', None)


def waiting_stage(state):
    if state.get('continuation') == 'cancelled' or state.get('phase') == 'accepted':
        return None
    if state.get('continuation') == 'paused':
        return 'paused'
    if state.get('phase') == 'ready':
        return 'idle'
    from continuation import quiet
    if state.get('phase') in ('checkpoint', 'protocol_error') and quiet(state):
        return 'scheduler_wait'
    event = state.get('events', {}).get(state.get('last_event_id'), {})
    if state.get('owner') and event.get('status') in (
            'pending', 'sending', 'uncertain', 'queued', 'delivered', 'unacknowledged'):
        return 'callback_wait'
    return 'supervisor_wait'


def elapsed(state, at=None):
    at = time.time() if at is None else at
    terminal = state.get('phase') == 'accepted' or state.get('continuation') == 'cancelled'
    end = state.get('finished_at') if terminal else at
    start = state.get('created_at')
    return round(end - start, 6) if number(start) and number(end) and end >= start else None


def snapshot(state, at=None, requests=None):
    at = time.time() if at is None else at
    timing = state.get('timing')
    if not _valid(timing):
        return {'complete': False, 'elapsed_wall_seconds': elapsed(state, at),
                'phase_seconds': None, 'current_stage': None, 'tracked_since': None, 'stages': stage_snapshot(state, at, requests)}
    durations = {stage: timing['phase_seconds'].get(stage, 0.0) for stage in STAGES}
    stage, since = timing.get('stage'), timing.get('since')
    complete = timing.get('complete') is True
    if stage in STAGES:
        if number(since) and number(at) and at >= since:
            durations[stage] += at - since
        else:
            complete = False
    wall = elapsed({**state, 'finished_at': timing.get('finished_at')}, at)
    return {'complete': complete and wall is not None,
            'elapsed_wall_seconds': wall,
            'phase_seconds': {key: round(value, 6) for key, value in durations.items()},
            'current_stage': stage, 'tracked_since': timing.get('started_at'),
            'requirement_elapsed_wall_seconds': requirement_elapsed(state, at),
            'requirement_started_at': state.get('requirement_started_at'),
            'requirement_time_source': state.get('requirement_time_source'),
            'scheduler_wait_tracked_since': timing.get('scheduler_wait_tracked_since'), 'stages': stage_snapshot(state, at, requests)}


def check_timing(project):
    value = project.get('check_timing')
    if (isinstance(value, dict) and number(value.get('observed_seconds'))
            and type(value.get('completed_runs')) is int and value['completed_runs'] >= 0):
        return {'observed_seconds': value['observed_seconds'], 'completed_runs': value['completed_runs'],
                'complete': value.get('complete') is True}
    return {'observed_seconds': 0.0, 'completed_runs': 0, 'complete': False}


def finish_checks(project, seconds):
    value = check_timing(project)
    if number(seconds):
        value['observed_seconds'] = round(value['observed_seconds'] + seconds, 6)
        value['completed_runs'] += 1
    else:
        value['complete'] = False
    project['check_timing'] = value


def project_snapshot(project, children, at=None):
    """Child durations can overlap; report them separately from project wall time."""
    phases = {stage: 0.0 for stage in STAGES}
    missing = []
    for child in children:
        if child.get('phase') == 'pending':
            continue
        value = child.get('timing') or {}
        if value.get('complete') is not True:
            missing.append(child['id'])
        for stage, seconds in (value.get('phase_seconds') or {}).items():
            if stage in phases and number(seconds):
                phases[stage] += seconds
    stage_children = []
    for child in children:
        for interval in ((child.get('timing') or {}).get('stages') or {}).get('intervals', []):
            stage_children.append({**interval, 'id': child['id'] + ':' + interval['id']})
    stages = stage_snapshot(project, time.time() if at is None else at, stage_children)
    integration = check_timing(project)
    integration['in_flight_runs'] = int(project.get('phase') == 'verifying')
    if integration['in_flight_runs']:
        integration['complete'] = False
    return {'elapsed_wall_seconds': elapsed(project, at), 'stages': stages,
            'child_phase_seconds': {key: round(value, 6) for key, value in phases.items()},
            'children_complete': not missing, 'incomplete_children': missing,
            'integration_checks': integration,
            'requirement_elapsed_wall_seconds': requirement_elapsed(project, at),
            'requirement_started_at': project.get('requirement_started_at'),
            'scope': 'child durations are cumulative and may overlap; they are not project wall time'}


# Independent intervals can overlap (for example checks and supervisor reading).
# The legacy phase counters above remain available with their original meaning.
CATEGORIES = ('model_work', 'environment_preparation', 'scheduler_wait', 'local_checks',
              'closed_book', 'callback_delivery', 'supervisor_processing', 'paused')


def _timeline(state, at):
    value = state.get('stage_timeline')
    if not isinstance(value, dict) or value.get('version') != 1:
        value = {'version': 1, 'tracked_since': at, 'complete': False, 'intervals': {}, 'milestones': {}}
        state['stage_timeline'] = value
    return value


def begin(state, category, ident, at=None):
    if category not in CATEGORIES:
        raise ValueError('Unknown interval category')
    at = time.time() if at is None else at
    value = _timeline(state, at)
    value['intervals'].setdefault(ident, {'id': ident, 'category': category, 'started_at': at, 'outcome': 'in_progress'})


def end(state, ident, at=None, outcome='completed'):
    at = time.time() if at is None else at
    row = (state.get('stage_timeline') or {}).get('intervals', {}).get(ident)
    if row and 'finished_at' not in row:
        row.update(finished_at=at, outcome=outcome)


def mark(state, name, ident, at=None):
    at = time.time() if at is None else at
    _timeline(state, at)['milestones'].setdefault(name + ':' + ident, {'name': name, 'id': ident, 'at': at})


def stage_snapshot(state, at, requests=None):
    value = state.get('stage_timeline') or {}
    intervals = [dict(x) for x in value.get('intervals', {}).values()]
    start = state.get('created_at')
    finish = state.get('finished_at', (state.get('timing') or {}).get('finished_at'))
    intervals += [dict(row) for row in (requests or [])
                  if (not number(start) or not number(row.get('started_at')) or row['started_at'] >= start)
                  and (not number(finish) or not number(row.get('started_at')) or row['started_at'] <= finish)]
    totals = {name: 0.0 for name in CATEGORIES}
    points, running = [], []
    complete = value.get('complete') is True
    for row in intervals:
        category, start, stop = row.get('category'), row.get('started_at'), row.get('finished_at', at)
        if category not in totals or not number(start) or not number(stop) or stop < start:
            complete = False
            continue
        if row.get('outcome') == 'unknown' and 'finished_at' not in row:
            complete = False
            continue
        if 'finished_at' not in row:
            running.append(row['id'])
        if row.get('outcome') in ('unknown', 'interrupted'):
            complete = False
        totals[category] += stop - start
        points.extend(((start, 1), (stop, -1)))
    active, previous, union, overlap = 0, None, 0.0, 0.0
    for at_point, change in sorted(points):
        if previous is not None:
            if active: union += at_point - previous
            if active > 1: overlap += at_point - previous
        active += change
        previous = at_point
    return {'tracked_since': value.get('tracked_since'), 'complete': complete,
            'cumulative_seconds': {k: round(v, 6) for k, v in totals.items()},
            'union_seconds': round(union, 6), 'overlap_seconds': round(overlap, 6),
            'unattributed_wall_seconds': max(0, round(elapsed(state, at) - union, 6)) if elapsed(state, at) is not None else None,
            'overlap_excess_seconds': round(sum(totals.values()) - union, 6),
            'in_progress': running, 'intervals': intervals,
            'milestones': list(value.get('milestones', {}).values()),
            'model_requests_available': requests is not None,
            'scope': 'independent observed intervals; cumulative time and overlapping time are not lifecycle wall time; supervisor interval is receipt-to-action, not CPU time'}


def record(b, state, operation, *args, **kwargs):
    """Persist a worker's observation without overwriting concurrent receipts/control."""
    if not hasattr(b, 'locked') or not state.get('job_id'):
        return
    with b.locked(state['session_id']) as root:
        current, _ = b.load_session(root)
        if current.get('job_id') != state['job_id']:
            return
        globals()[operation](current, *args, **kwargs)
        b.write_json(root / 'state.json', current)


def request_intervals(entries):
    result = []
    for entry in entries:
        if not number(entry.get('started_at')):
            continue
        row = {'id': 'request:' + str(entry.get('request_id')), 'category': 'model_work',
               'phase': entry.get('phase'), 'started_at': entry['started_at'],
               'outcome': 'in_progress' if entry.get('status') == 'reserved' else entry.get('status', 'unknown')}
        if number(entry.get('finished_at')):
            row['finished_at'] = entry['finished_at']
        result.append(row)
    return result



def requirement_elapsed(state, at=None):
    start = state.get('requirement_started_at')
    if not number(start):
        return None
    return elapsed({**state, 'created_at': start,
                    'finished_at': state.get('finished_at', (state.get('timing') or {}).get('finished_at'))}, at)


def attach_origin(b, state, planning=None):
    """Earliest recorded runtime boundary, never an invented user-message time."""
    origin, label = state['created_at'], 'task_created'
    if planning and planning.get('meeting_id'):
        meeting = b.project_manager().load(planning['meeting_id'])
        origin, label = meeting['created_at'], 'meeting_created'
    elif state.get('session_id') and state.get('project_id') and state['session_id'] != state['project_id']:
        project = b.project_manager().load(state['project_id'])
        origin = project.get('requirement_started_at', project['created_at'])
        label = project.get('requirement_time_source', 'project_created')
    state.update(requirement_started_at=origin, requirement_time_source=label)

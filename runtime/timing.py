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


def snapshot(state, at=None):
    at = time.time() if at is None else at
    timing = state.get('timing')
    if not _valid(timing):
        return {'complete': False, 'elapsed_wall_seconds': elapsed(state, at),
                'phase_seconds': None, 'current_stage': None, 'tracked_since': None}
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
            'scheduler_wait_tracked_since': timing.get('scheduler_wait_tracked_since')}


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
    integration = check_timing(project)
    integration['in_flight_runs'] = int(project.get('phase') == 'verifying')
    if integration['in_flight_runs']:
        integration['complete'] = False
    return {'elapsed_wall_seconds': elapsed(project, at),
            'child_phase_seconds': {key: round(value, 6) for key, value in phases.items()},
            'children_complete': not missing, 'incomplete_children': missing,
            'integration_checks': integration,
            'scope': 'child durations are cumulative and may overlap; they are not project wall time'}

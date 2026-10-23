"""Validate durable session records and isolate unreadable records in place.

Quarantine records contain diagnostics, never a repaired/fabricated task state.
The original bytes stay available for recovery. Scheduling leases are separate
so corruption cannot make a live process disappear from concurrency accounting.
"""
import math
import re


PHASES = {'ready', 'running', 'responded', 'needs_clarification', 'discussion_result', 'awaiting_review', 'not_accepted',
          'accepted', 'error', 'project_update'}


def validate(state, sid):
    if not re.fullmatch(r'[a-f0-9]{32}', sid) or not isinstance(state, dict) or state.get('session_id') != sid:
        raise ValueError('invalid session identity/object')
    if not isinstance(state.get('phase'), str) or state['phase'] not in PHASES:
        raise ValueError('invalid phase')
    if not isinstance(state.get('contract_hash'), str) or not re.fullmatch(r'[a-f0-9]{64}', state['contract_hash']):
        raise ValueError('invalid contract identity')
    for key in ('turns', 'rounds'):
        if type(state.get(key)) is not int or state[key] < 0:
            raise ValueError('invalid counter: ' + key)
    for key in ('job_started', 'job_finished', 'next_retry_at'):
        if key in state and (type(state[key]) not in (int, float) or not math.isfinite(state[key])):
            raise ValueError('invalid timestamp: ' + key)
    for key in ('events', 'acceptance', 'delivery', 'clarification'):
        if key in state and state[key] is not None and not isinstance(state[key], dict):
            raise ValueError('invalid object: ' + key)
    if not isinstance(state.get('events', {}), dict) or any(not isinstance(v, dict) for v in state.get('events', {}).values()):
        raise ValueError('invalid event records')
    owner = state.get('owner')
    if owner is not None and (not isinstance(owner, dict) or not isinstance(owner.get('thread_id'), str) or not owner['thread_id']):
        raise ValueError('invalid owner')
    if 'job_pid' in state and (type(state['job_pid']) is not int or not 0 < state['job_pid'] <= 2**31 - 1):
        raise ValueError('invalid process identity')
    if state.get('job_process_start') is not None and not isinstance(state['job_process_start'], str):
        raise ValueError('invalid process start identity')
    if state.get('last_event_id') is not None and not isinstance(state['last_event_id'], str):
        raise ValueError('invalid event identity')
    if state['phase'] == 'accepted' and (not isinstance(state.get('acceptance'), dict)
            or not isinstance(state.get('packet_id'), str) or not isinstance(state.get('submitted_commit'), str)):
        raise ValueError('missing accepted evidence identity')
    return state


def quarantine(b, root, error):
    diagnostic = {'session_id': root.name, 'phase': 'quarantined',
                  'error': type(error).__name__, 'reason': str(error)[:300],
                  'state_path': str(root / 'state.json'), 'accepted': False}
    b.write_json(b.STATE / 'quarantine' / (root.name + '.json'), diagnostic)
    return b.GateError('Session state quarantined: ' + root.name + '; ' + str(error)[:300])


def read(b, root):
    try:
        return validate(b.read_json(root / 'state.json'), root.name)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        raise quarantine(b, root, error) from None


def lease(b, root, state):
    value = {key: state.get(key) for key in ('session_id', 'job_id', 'project_id', 'phase',
             'provider', 'active_provider', 'job_pid', 'job_process_start', 'job_started')}
    b.write_json(root / 'job-lease.json', value)


def isolated_slot(b, root):
    try:
        value = b.read_json(root / 'job-lease.json')
        if not isinstance(value, dict) or value.get('session_id') != root.name:
            raise ValueError('invalid lease')
        if not isinstance(value.get('job_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', value['job_id']):
            raise ValueError('invalid lease job')
        if not isinstance(value.get('active_provider'), str) or not value['active_provider']:
            raise ValueError('invalid lease provider')
        if value.get('phase') not in PHASES:
            raise ValueError('invalid lease phase')
        if value['phase'] != 'running':
            return None
        pid = value.get('job_pid')
        if pid is not None and (type(pid) is not int or not 0 < pid <= 2**31 - 1):
            raise ValueError('invalid lease process')
        if value.get('job_process_start') is not None and not isinstance(value['job_process_start'], str):
            raise ValueError('invalid lease process start')
        if pid and not b.process_running(pid, value.get('job_process_start')):
            return None
        return value
    except (OSError, ValueError, TypeError, OverflowError):
        # Historical/unreadable lease: reserve one unknown slot rather than
        # declaring potentially-running work idle. Other free slots still work.
        return {'session_id': root.name, 'capacity_unknown': True}

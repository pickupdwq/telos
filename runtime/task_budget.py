"""Audited work-turn policy for one existing standalone task, never a reset."""
import math
import time
from serialization import digest

KEY = 'task_work_turn_policy'
KIND = 'telos.standalone_work_turn_policy'
FIELDS = {'version', 'kind', 'session_id', 'owner_thread_id', 'original_contract_hash',
          'initial_max_turns', 'effective_max_turns', 'turns_at_authorization',
          'history', 'record_hash'}
RECORD_FIELDS = {'seq', 'arguments', 'arguments_hash', 'created_at'}


def arguments(session_id, request_id, reason, user_authorization, strategy='unlimited'):
    values = dict(session_id=session_id, request_id=request_id, reason=reason,
                  user_authorization=user_authorization, strategy=strategy)
    if (any(not isinstance(x, str) or not x.strip() or len(x) > 4000 for x in values.values())
            or request_id != request_id.strip() or strategy != 'unlimited'):
        raise ValueError('Bounded authorization, request_id and strategy=unlimited are required')
    return values


def validate(state, original):
    if KEY not in state:
        return None
    row = state[KEY]
    if not isinstance(row, dict) or set(row) != FIELDS:
        raise ValueError('Invalid standalone work-turn policy fields')
    if (type(row['version']) is not int or row['version'] != 1 or row['kind'] != KIND
            or state.get('project_id') is not None
            or row['session_id'] != state.get('session_id')
            or not row['owner_thread_id']
            or row['owner_thread_id'] != (state.get('owner') or {}).get('thread_id')
            or row['original_contract_hash'] != digest(original)
            or type(row['initial_max_turns']) is not int or row['initial_max_turns'] < 1
            or row['effective_max_turns'] is not None
            or type(row['turns_at_authorization']) is not int
            or not 0 <= row['turns_at_authorization'] <= state['turns']
            or row['record_hash'] != digest({k: v for k, v in row.items() if k != 'record_hash'})
            or not isinstance(row['history'], list) or len(row['history']) != 1):
        raise ValueError('Standalone work-turn policy identity or integrity mismatch')
    record = row['history'][0]
    if (not isinstance(record, dict) or set(record) != RECORD_FIELDS
            or type(record['seq']) is not int or record['seq'] != 1
            or type(record['created_at']) not in (int, float) or not math.isfinite(record['created_at'])
            or not isinstance(record['arguments'], dict)
            or record['arguments'] != arguments(**record['arguments'])
            or record['arguments']['session_id'] != state['session_id']
            or record['arguments_hash'] != digest(record['arguments'])):
        raise ValueError('Invalid standalone work-turn authorization history')
    return row


def summary(state, configured):
    row = state.get(KEY)
    return {'scope': 'work_turns', 'strategy': 'unlimited' if row else 'fixed',
            'initial_max_turns': row['initial_max_turns'] if row else configured,
            'effective_max_turns': None if row else configured,
            'used_turns': state['turns'],
            'turns_at_authorization': row['turns_at_authorization'] if row else None,
            'authorization_history': row['history'] if row else [],
            'record_hash': row['record_hash'] if row else None}


def authorize(b, sid, request_id, reason, user_authorization, caller, strategy='unlimited'):
    """One atomic policy write under the existing coordinator/session locks."""
    try:
        args = arguments(sid, request_id, reason, user_authorization, strategy)
        with b.project_manager().lock(), b.locked(sid) as root:
            state, contract = b.load_session(root)
            owner = (state.get('owner') or {}).get('thread_id')
            if not owner or caller != owner:
                raise b.GateError('Original trusted supervisor required', code='owner_mismatch')
            from supervisor import verify_owner
            verify_owner(b, state['owner'])
            if state.get('project_id') is not None:
                raise ValueError('Use telos_project_budget for project-owned tasks')
            if (state.get('archived') or state.get('continuation') == 'cancelled'
                    or state['phase'] == 'accepted' or contract.get('task_type') == 'discussion'):
                raise ValueError('Terminal or discussion tasks cannot change work-turn policy')
            for value in (reason, user_authorization):
                b.deny_leak(value, contract)
            original = b.read_json(root / 'contract.json')
            row = validate(state, original)
            replayed = row is not None
            if row:
                if row['history'][0]['arguments'] != args:
                    raise ValueError('An unlimited work-turn authorization already exists; different arguments rejected')
            else:
                initial = b.config()['max_turns']
                row = {'version': 1, 'kind': KIND, 'session_id': sid,
                       'owner_thread_id': owner, 'original_contract_hash': digest(original),
                       'initial_max_turns': initial, 'effective_max_turns': None,
                       'turns_at_authorization': state['turns'],
                       'history': [{'seq': 1, 'arguments': args, 'arguments_hash': digest(args),
                                    'created_at': time.time()}]}
                row['record_hash'] = digest(row)
                state[KEY] = row
                validate(state, original)
                b.write_json(root / 'state.json', state)
            return {'session_id': sid, 'replayed': replayed, 'accepted': False,
                    'phase': state['phase'], 'continuation': state.get('continuation'),
                    'work_turn_budget': summary(state, b.config()['max_turns']),
                    'next_action': 'telos_status'}
    except (ValueError, TypeError, KeyError) as error:
        raise b.GateError(str(error), code='invalid_budget_policy') from None

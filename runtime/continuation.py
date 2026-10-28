"""Explicitly authorized continuation jobs; never impersonate supervisor receipts."""
import time
import progress_guard
from providers import ProviderError
from serialization import digest


def freeze(value, config):
    value = config.get('auto_continue_checkpoints', False) if value is None else value
    if type(value) is not bool:
        raise ProviderError('auto_continue_checkpoints must be a boolean')
    return value


def prepare(state, contract):
    """Called under the session lock after durable delivery and progress observation."""
    if contract.get('work_reply_version', 1) != 2:
        return
    phase = state['phase']
    if phase == 'protocol_error':
        state['work_reply_failures'] = state.get('work_reply_failures', 0) + 1
    elif phase in ('checkpoint', 'responded', 'needs_clarification', 'blocked_environment'):
        state['work_reply_failures'] = 0
    kind = None
    if phase == 'checkpoint' and contract.get('auto_continue_checkpoints') is True:
        kind = 'checkpoint'
    if (phase == 'protocol_error' and state.get('thread_id') and
            state['work_reply_failures'] <= contract.get('work_reply_format_retries', 0)):
        kind = 'protocol_correction'
    if kind is None or progress_guard.blocked(state):
        return
    job = state['job_id']
    state.setdefault('auto_continuations', {}).setdefault(job, {
        'source_job_id': job, 'delivery_id': state['delivery']['delivery_id'],
        'contract_hash': state['contract_hash'], 'thread_id': state['thread_id'],
        'kind': kind, 'status': 'pending', 'created_at': time.time(),
        'evidence_file': 'delivery-' + job + '.json',
        'diagnostic': state['delivery'].get('protocol_error')})
    state['auto_continue_pending'] = job


def pending(state):
    key = state.get('auto_continue_pending')
    return key if state.get('auto_continuations', {}).get(key, {}).get('status') == 'pending' else None


def authorize(state, contract, key):
    row = state.get('auto_continuations', {}).get(key)
    if (not row or row['status'] != 'pending' or state.get('job_id') != key
            or state.get('continuation', 'active') != 'active' or state.get('archived')
            or row['contract_hash'] != state['contract_hash'] or row['thread_id'] != state.get('thread_id')
            or row['delivery_id'] != (state.get('delivery') or {}).get('delivery_id')
            or contract.get('work_reply_version') != 2 or state.get('clarification')):
        raise ProviderError('Continuation identity is stale, stopped or not authorized',
                            code='continuation_stopped', next_action='inspect_status_before_retry')
    permitted = (
        row['kind'] == 'checkpoint' and state['phase'] == 'checkpoint'
        and contract.get('auto_continue_checkpoints') is True
    ) or (
        row['kind'] == 'protocol_correction' and state['phase'] == 'protocol_error'
        and state.get('work_reply_failures', 0) <= contract.get('work_reply_format_retries', 0)
    )
    if not permitted:
        raise ProviderError('Frozen policy does not authorize this continuation')
    progress_guard.check(state)
    return row


def message(state):
    row = state['auto_continuations'][pending(state)]
    if row['kind'] == 'protocol_correction':
        error = (state['delivery'].get('protocol_error') or {}).get('message', 'Invalid reply')
        return ('Your previous work reply failed the structured protocol: ' + error +
                '. Correct the reply using the required JSON schema. Do not invent completion, '
                'repeat work, or ask whether to continue. Preserve the actual task status and evidence.')
    receipt = state['delivery']
    return ('Continue the same authorized task from your saved checkpoint. Complete remaining work, '
            'run already-authorized checks and repair in-scope defects without asking permission to continue. '
            'Keep the same scope and acceptance criteria. Next step: ' + receipt['next_step'])


def started(state, source, job):
    row = state['auto_continuations'][source]
    import timing
    for ident, interval in (state.get('stage_timeline') or {}).get('intervals', {}).items():
        if interval['category'] == 'scheduler_wait':
            timing.end(state, ident)
    row.update(status='launching', job_id=job, claimed_at=time.time(),
               action_id=digest([state['session_id'], source, row['delivery_id'], job]))
    state.pop('auto_continue_pending', None)
    # Retire only this internal stage's notification; never create a receive receipt.
    event = state.get('events', {}).get(source)
    if event:
        event.update(status='superseded', internal_action_id=row['action_id'])


def quiet(state):
    return (bool(pending(state)) and state.get('continuation', 'active') == 'active'
            and not progress_guard.blocked(state))


def stop(state, source, error):
    row = state.get('auto_continuations', {}).get(source)
    if row and row['status'] == 'pending':
        row.update(status='stopped', stopped_at=time.time(), diagnostic=str(error)[:1000])
        state.pop('auto_continue_pending', None)
        state['last_error'] = 'Automatic continuation stopped: ' + str(error)[:1000]

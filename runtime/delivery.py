"""Durable executor claims, kept distinct from independently verified checks."""
import json
import time


def capture(b, state, result):
    text = result.get('summary') or ''
    if not isinstance(text, str):
        text = str(text)
    body = text.strip()
    if body.startswith('```json\n') and body.endswith('```'):
        body = body[8:-3].strip()
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        value = None
    fields = ('changed_files', 'checks_performed', 'remaining_issues')
    structured = (isinstance(value, dict) and isinstance(value.get('summary'), str)
                  and all(isinstance(value.get(k), list) and
                          all(isinstance(x, str) for x in value[k]) for k in fields))
    record = {'session_id': state['session_id'], 'job_id': state.get('job_id'),
              'turn': state.get('turns'), 'executor_thread_id': result.get('thread_id'),
              'contract_hash': state['contract_hash'], 'recorded_at': time.time(),
              'trust': 'executor_claims', 'format': 'structured' if structured else ('text' if text else 'missing'),
              'summary': value['summary'] if structured else text,
              **{k: value[k] if structured else None for k in fields},
              'raw_text': text, 'truncated': bool(result.get('summary_truncated'))}
    record['delivery_id'] = b.digest(record)
    return record


def bind(b, state, commit):
    record = state.get('delivery')
    if record is None:
        # Older sessions retain whatever work response is still available.
        record = capture(b, state, state.get('last_result') or {})
    return {**record, 'commit': commit, 'base_commit': state.get('base_commit')}


def summary(record):
    if not isinstance(record, dict):
        return record
    # Already bounded at the executor boundary; never hide remaining issues.
    return {k: v for k, v in record.items() if k != 'raw_text'}

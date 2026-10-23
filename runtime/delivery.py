"""Durable executor claims, kept distinct from independently verified checks."""
import json
import time


WORK_REPLY_GUIDANCE = (
    'Check requirements before implementing. Resolve technical facts from scoped code/context; do not guess '
    'missing business decisions. If blocked, stop affected work and end this turn with JSON '
    'status="needs_clarification", clarification_questions=[concise questions with useful options]. '
    'Do not call user-input/MCP tools or wait inside the process. The supervisor answers in this same conversation; '
    'ask again if questions remain. Only when resolved and work is ready for verification, return '
    'status="completed", clarification_questions=[]. Every reply also includes summary (string), changed_files, '
    'checks_performed, remaining_issues (arrays of strings). Report failures honestly; never claim acceptance.\n'
)


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
    if isinstance(value, dict) and ('status' in value or 'clarification_questions' in value):
        questions = value.get('clarification_questions')
        valid = (isinstance(questions, list) and
                 all(isinstance(q, str) and q.strip() for q in questions))
        completed = (value.get('status') == 'completed' and valid and not questions
                     and structured and not record['truncated'])
        # Contradictory/malformed structured replies cannot silently become
        # completed work. Keep the raw reply for the supervisor to inspect.
        record['status'] = 'completed' if completed else 'needs_clarification'
        record['clarification_questions'] = questions if valid else []
        if not completed and (value.get('status') != 'needs_clarification' or not valid or not questions):
            record['clarification_error'] = 'Invalid work reply; resend the required status and clarification_questions fields.'
    record['delivery_id'] = b.digest(record)
    return record


def pending_clarification(record, previous=None):
    if record.get('status') == 'needs_clarification':
        return {'job_id': record['job_id'], 'summary': record['summary'],
                'questions': record['clarification_questions'],
                'error': record.get('clarification_error')}
    if previous and record.get('status') != 'completed':
        return {**previous, 'error': 'Clarification is still pending; executor must explicitly confirm resolution with status=completed.'}
    return None


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

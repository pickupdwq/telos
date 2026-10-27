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


def _legacy_capture(b, state, result):
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


WORK_REPLY_V2_GUIDANCE = (
    'Return one complete JSON object, protocol_version=2. Common required fields: status, summary (string), '
    'changed_files, checks_performed, remaining_issues, clarification_questions (arrays of strings). '
    'completed means implementation ready for the existing independent acceptance, never accepted. '
    'checkpoint means unfinished authorized work/time-slice closeout with no supervisor decision needed; '
    'also include completed_work and remaining_work (arrays), next_step (nonempty string), evidence_paths (array). '
    'blocked_environment means a concrete dependency/network/browser/test-service/sandbox blocker; include '
    'blockers, repair_actions and evidence_paths (arrays). needs_clarification is only for missing requirements, '
    'business/scope/permission decisions and must have nonempty clarification_questions. '
    'All other statuses require clarification_questions=[]. Routine continuation, already-authorized tests and '
    'in-scope defect repair require no new permission. Save detailed reports as evidence files and return their '
    'paths when useful. Do not invent fields or completion; report the actual checks and remaining work. '
    'End with a valid checkpoint before the execution deadline; a forcibly timed-out process is not a checkpoint.\n'
)


def guidance(contract):
    return WORK_REPLY_V2_GUIDANCE if contract.get('work_reply_version', 1) == 2 else WORK_REPLY_GUIDANCE


def capture(b, state, result, contract=None):
    if not contract or contract.get('work_reply_version', 1) != 2:
        return _legacy_capture(b, state, result)
    text = result.get('summary')
    record = {'session_id': state['session_id'], 'job_id': state.get('job_id'),
              'turn': state.get('turns'), 'executor_thread_id': result.get('thread_id'),
              'contract_hash': state['contract_hash'], 'recorded_at': time.time(),
              'trust': 'executor_claims', 'format': 'invalid', 'status': 'protocol_error',
              'raw_text': text, 'summary': '', 'truncated': bool(result.get('summary_truncated')),
              'protocol_version': 2}
    code, problem = 'invalid_json', 'Reply must be a complete JSON object'
    try:
        if not isinstance(text, str) or record['truncated']:
            code, problem = 'truncated_reply', 'Missing or truncated work reply; resend a complete object'
            raise ValueError(problem)
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError('Duplicate JSON key: ' + key)
                value[key] = item
            return value
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
        code = 'invalid_schema'
        if not isinstance(value, dict):
            raise ValueError('Reply root must be an object')
        def strings(name, nonempty=False):
            v = value.get(name)
            if (not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v)
                    or nonempty and not v):
                raise ValueError(name + ' must be an array of nonempty strings' + (' with at least one item' if nonempty else ''))
        for field in ('changed_files', 'checks_performed', 'remaining_issues', 'clarification_questions'):
            strings(field)
        if not isinstance(value.get('summary'), str) or not value['summary'].strip():
            raise ValueError('summary must be a nonempty string')
        status = value.get('status')
        # Explicit old completed/question objects remain valid on a new contract;
        # new semantics must identify protocol v2, never be inferred from prose.
        version = value.get('protocol_version', 1)
        if type(version) is not int or version not in (1, 2):
            raise ValueError('Unsupported work reply protocol_version')
        if status not in ('completed', 'needs_clarification', 'checkpoint', 'blocked_environment'):
            raise ValueError('Unknown work reply status')
        if status in ('checkpoint', 'blocked_environment') and version != 2:
            raise ValueError('New work statuses require protocol_version=2')
        if status == 'needs_clarification':
            strings('clarification_questions', True)
        elif value['clarification_questions']:
            raise ValueError('Only needs_clarification may contain clarification_questions')
        selected = ('summary', 'changed_files', 'checks_performed', 'remaining_issues', 'clarification_questions')
        if status == 'checkpoint':
            strings('completed_work'); strings('remaining_work', True); strings('evidence_paths')
            if not isinstance(value.get('next_step'), str) or not value['next_step'].strip():
                raise ValueError('checkpoint requires a nonempty next_step')
            selected += ('completed_work', 'remaining_work', 'next_step', 'evidence_paths')
        if status == 'blocked_environment':
            strings('blockers', True); strings('repair_actions', True); strings('evidence_paths')
            selected += ('blockers', 'repair_actions', 'evidence_paths')
        record.update({k: value[k] for k in selected})
        record.update(status=status, format='structured', reply_protocol_version=version)
    except (ValueError, TypeError) as error:
        record['protocol_error'] = {'code': code, 'message': str(error) or problem,
                                    'retryable': True, 'next_action': 'correct_work_reply'}
    record['delivery_id'] = b.digest(record)
    return record

"""Lossy MCP-facing views over unchanged full evidence payloads.

These functions never persist or mutate task/project state. Internal callers keep
using the full functions so gates and ownership checks never depend on a compact
projection.
"""
from __future__ import annotations


# Fixed closed-book diagnostic vocabulary. Compact views never copy arbitrary
# error text out of attempt receipts.
_SAFE_FORMAT_ERRORS = frozenset((
    'invalid JSON',
    'root must contain only an answers array',
    'each answer needs exactly id and answer',
    'id and answer must be nonempty strings',
    'missing, duplicate, or unknown question ids',
    'answer schema violation',
))
_FORMAT_MODES = frozenset(('json_object', 'json_schema', 'text'))


def _text(value, limit=1200):
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[-limit:]


# Per-request and per-stage records grow with every model request; a long task
# returned hundreds of intervals on each poll. Summaries keep totals and counts;
# detail="full" still returns every record.
_RUNNING_LIMIT = 20
_CONTINUATION_FIELDS = ('source_job_id', 'job_id', 'kind', 'status', 'created_at',
                        'finished_at', 'stopped_at', 'result_phase', 'diagnostic')


def _usage(value):
    if not isinstance(value, dict) or 'request_intervals' not in value:
        return value
    result = {key: item for key, item in value.items() if key != 'request_intervals'}
    if isinstance(value['request_intervals'], list):
        result['request_interval_count'] = len(value['request_intervals'])
    return result


def _stages(value):
    if not isinstance(value, dict):
        return value
    result = {key: item for key, item in value.items() if key not in ('intervals', 'milestones')}
    for key, count in (('intervals', 'interval_count'), ('milestones', 'milestone_count')):
        if isinstance(value.get(key), list):
            result[count] = len(value[key])
    running = value.get('in_progress')
    if isinstance(running, list) and len(running) > _RUNNING_LIMIT:
        result['in_progress'] = running[-_RUNNING_LIMIT:]
        result['in_progress_omitted'] = len(running) - _RUNNING_LIMIT
    return result


def _timing(value):
    if not isinstance(value, dict) or not isinstance(value.get('stages'), dict):
        return value
    return {**value, 'stages': _stages(value['stages'])}


def _continuations(value):
    if not isinstance(value, dict):
        return value
    rows = [row for row in value.values() if isinstance(row, dict)]
    counts = {}
    for row in rows:
        counts[str(row.get('status'))] = counts.get(str(row.get('status')), 0) + 1
    rows.sort(key=lambda row: row.get('created_at') if type(row.get('created_at')) in (int, float) else 0)
    # Finished continuations are history; anything else may need the supervisor.
    recent = [row for row in rows if row.get('status') != 'finished'][-5:] or rows[-1:]
    return {'count': len(rows), 'by_status': counts,
            'recent': [{key: _text(row.get(key), 500) for key in _CONTINUATION_FIELDS if key in row}
                       for row in recent]}


def _task_usage(value):
    # The project's own usage aggregates every task; per task, totals and the
    # budget state are enough to spot the expensive or exhausted one.
    if not isinstance(value, dict):
        return value
    result = {key: value.get(key) for key in ('totals', 'budget_state', 'work_budget_state', 'incomplete', 'error')
              if key in value}
    if isinstance(value.get('request_latency'), dict):
        result['request_latency'] = {key: value['request_latency'].get(key)
                                     for key in ('observed_seconds', 'complete') if key in value['request_latency']}
    return result


def _task_timing(value):
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ('complete', 'current_stage', 'elapsed_wall_seconds', 'phase_seconds')
            if key in value}


def _environment(value):
    # A passed environment is tool/file fingerprints; blocked ones stay whole
    # because their reason and repair actions are what the supervisor needs.
    if not isinstance(value, dict) or value.get('status') != 'passed':
        return value
    return {key: value.get(key) for key in ('status', 'phase', 'cache_hit', 'browser_version') if key in value}


_DELIVERY_FIELDS = ('job_id', 'session_id', 'status', 'format', 'trust', 'summary', 'commit',
                    'next_step', 'clarification_questions', 'protocol_error', 'report_id')


def _task_delivery(value, discussion):
    if not isinstance(value, dict):
        return value
    if discussion:
        # The full opinion is already in discussion.reports.
        return {key: value.get(key) for key in ('job_id', 'report_id', 'summary') if key in value}
    result = {key: value.get(key) for key in _DELIVERY_FIELDS if key in value}
    for key in ('changed_files', 'checks_performed', 'remaining_issues', 'blockers',
                'repair_actions', 'remaining_work', 'evidence_paths'):
        if isinstance(value.get(key), list):
            result[key + '_count'] = len(value[key])
    if isinstance(value.get('remaining_issues'), list):
        result['remaining_issues'] = value['remaining_issues'][:10]
    if isinstance(value.get('blockers'), list):
        result['blockers'] = value['blockers']
    return result


def _checks(checks):
    result = []
    for check in checks or []:
        item = {key: check.get(key) for key in ('id', 'status', 'returncode', 'seconds', 'source_read_only', 'output_incomplete') if key in check}
        # Successful logs are usually noise. Failures remain actionable and are
        # bounded; full evidence is always available with detail="full".
        if check.get('status') != 'passed':
            item['stdout'] = _text(check.get('stdout', ''), 1200)
            item['stderr'] = _text(check.get('stderr', ''), 1200)
        result.append(item)
    return result


def _attempts(attempts):
    result = []
    for attempt in attempts or []:
        requests = attempt.get('requests') or []
        responses = attempt.get('responses') or []
        item = {
            'attempt': attempt.get('attempt'),
            'request_count': len(requests),
            'response_count': len(responses),
            'request_hashes': [r.get('request_hash') for r in requests],
            'input_hashes': [r.get('input_hash') for r in requests],
            'response_hashes': [r.get('response_hash') for r in responses],
            'tool_call_count': sum(r.get('tool_call_count') for r in responses
                                    if type(r.get('tool_call_count')) is int),
            'cli_session_usage': attempt.get('cli_session_usage'),
        }
        if 'format_valid' in attempt:
            value = attempt.get('format_valid')
            item['format_valid'] = value if type(value) is bool else None
        if 'format_error' in attempt:
            value = attempt.get('format_error')
            if value is not None and (not isinstance(value, str) or value not in _SAFE_FORMAT_ERRORS):
                value = 'answer schema violation'
            item['format_error'] = value
        if any(isinstance(request, dict) and 'text_format' in request for request in requests):
            item['text_formats'] = [
                request.get('text_format')
                if (isinstance(request, dict)
                    and isinstance(request.get('text_format'), str)
                    and request.get('text_format') in _FORMAT_MODES)
                else None
                for request in requests
            ]
        result.append(item)
    return result


def _exam(exam):
    if not isinstance(exam, dict):
        return exam
    result = {key: exam.get(key) for key in (
        'mode', 'executor_thread_id', 'session_id', 'commit', 'same_executor',
        'tool_call_count', 'public_payload_hash', 'backend', 'answers', 'usage',
        'usage_scope', 'skipped') if key in exam}
    if 'attempts' in exam:
        result['attempts'] = _attempts(exam.get('attempts'))
    return result


def _evidence(evidence):
    if not isinstance(evidence, dict):
        return evidence
    result = {key: evidence.get(key) for key in (
        'session_id', 'round', 'commit', 'contract_hash', 'assessment_input_hash',
        'job_id', 'reasoning_decision', 'check_evidence', 'dependency_contracts', 'audit_check_corrections', 'test_path_corrections', 'check_revisions') if key in evidence}
    if 'checks' in evidence:
        result['checks'] = _checks(evidence.get('checks'))
    if 'exam' in evidence:
        result['exam'] = _exam(evidence.get('exam'))
    if 'submission' in evidence:
        submission = evidence.get('submission')
        if isinstance(submission, dict):
            result['submission'] = {k: submission.get(k) for k in ('commit', 'status', 'backend') if k in submission}
    if 'audit' in evidence:
        result['audit'] = evidence.get('audit')
    if 'delivery' in evidence:
        from delivery import summary
        result['delivery'] = summary(evidence['delivery'])
    if 'deliveries' in evidence:
        from delivery import summary
        result['deliveries'] = {key: {**value, 'delivery': summary(value.get('delivery'))}
                                for key, value in evidence['deliveries'].items()}
    return result


def _verdict(value):
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in (
        'accepted', 'score', 'threshold', 'critical_failed', 'checks_passed', 'blockers',
        'commit', 'packet_id', 'contract_hash', 'round', 'task_type',
        'audit_manifest_hash', 'system_health') if key in value}


def _last_result(value):
    if not isinstance(value, dict):
        return value
    result = {key: value.get(key) for key in (
        'round', 'checks_passed', 'exam_received', 'next_action', 'executor_thread_id',
        'assessment_mode', 'exam_usage_scope', 'usage_scope') if key in value}
    if 'usage' in value:
        result['usage'] = value.get('usage')
    if 'exam_usage' in value:
        result['exam_usage'] = value.get('exam_usage')
    return result


def status(full):
    result = {key: full.get(key) for key in (
        'session_id', 'phase', 'accepted', 'model', 'worktree', 'base_commit',
        'submitted_commit', 'packet_id', 'contract_hash', 'turns', 'rounds',
        'job_id', 'job_kind', 'infra_failures', 'next_retry_at', 'last_failure_kind',
        'project_id', 'task_type', 'discussion_complete', 'version', 'workflow', 'executor', 'budget', 'work_turn_budget', 'usage', 'timing',
        'next_action', 'clarification', 'archived', 'audit_artifacts', 'audit_report', 'message',
        'reasoning_policy', 'reasoning_decision', 'progress_guard', 'environment', 'role_snapshot', 'experience_refs',
        'auto_continue_checkpoints', 'auto_continue_pending', 'auto_continuations', 'auto_run_checks', 'automatic_checks', 'audit_check_correction', 'test_path_correction', 'check_revision') if key in full}
    for key, view in (('usage', _usage), ('timing', _timing), ('auto_continuations', _continuations)):
        if key in result:
            result[key] = view(result[key])
    if 'last_error' in full:
        result['last_error'] = _text(full.get('last_error'), 1200)
    if 'acceptance' in full:
        result['acceptance'] = _verdict(full.get('acceptance'))
    if 'last_result' in full:
        result['last_result'] = _last_result(full.get('last_result'))
    if 'delivery' in full:
        from delivery import summary
        result['delivery'] = summary(full['delivery'])
    channel = full.get('return_channel')
    if isinstance(channel, dict):
        compact = {key: channel.get(key) for key in (
            'enabled', 'health', 'transport', 'continuation', 'owner_thread_id')}
        control = channel.get('last_control')
        if isinstance(control, dict):
            compact['last_control'] = {key: control.get(key) for key in (
                'action', 'at', 'delivery_may_have_started', 'warnings') if key in control}
        event = channel.get('event')
        if isinstance(event, dict):
            compact['event'] = {key: event.get(key) for key in (
                'id', 'status', 'attempts', 'last_error', 'received_at', 'turn_id', 'turn_status') if key in event}
        result['return_channel'] = compact
    result['detail'] = 'summary'
    result['detail_hint'] = 'Call telos_status with detail="full" for the complete state and logs.'
    return result


def review(full):
    result = {key: full.get(key) for key in (
        'packet_id', 'questions', 'instruction', 'session_id', 'worktree', 'submitted_commit',
        'contract_hash', 'identity', 'blockers', 'acceptance', 'budget', 'work_turn_budget', 'usage', 'timing', 'audit_check_correction', 'test_path_correction', 'check_revision') if key in full}
    for key, view in (('usage', _usage), ('timing', _timing)):
        if key in result:
            result[key] = view(result[key])
    result['evidence'] = _evidence(full.get('evidence'))
    result['detail'] = 'summary'
    result['detail_hint'] = ('Call telos_review with detail="full" for complete successful logs, '
                             'raw attempt receipts and the unchanged private packet.')
    return result


def receive(full):
    result = {'proceed': full.get('proceed'), 'event_id': full.get('event_id'),
              'session_id': full.get('session_id'), 'next_action': full.get('next_action'),
              'detail': 'summary',
              'detail_hint': 'Call telos_receive with detail="full" for the complete receipt and state.'}
    if isinstance(full.get('result'), dict):
        result['result'] = status(full['result'])
    else:
        result['result'] = full.get('result')
    return result


def _integration(value):
    if not isinstance(value, dict):
        return value
    result = {key: value.get(key) for key in (
        'worktree', 'pins', 'commit', 'packet_id', 'dependency_paths', 'dependency_contracts') if key in value}
    if 'conflict' in value:
        result['conflict'] = value.get('conflict')
    if 'error' in value:
        result['error'] = _text(value.get('error'), 1200)
    return result


def _project_task(task, discussion=False):
    result = {key: task.get(key) for key in (
        'id', 'phase', 'depends_on', 'allowed_paths', 'provider', 'model', 'executor_profile',
        'workflow_version', 'reasoning_effort', 'session_id', 'event_id', 'accepted_commit',
        'base_commit', 'budget', 'usage', 'timing', 'revision', 'delivery', 'clarification', 'role_snapshot', 'experience_tags',
        'start_error', 'next_action', 'environment', 'auto_continue_pending', 'auto_continue_checkpoints', 'auto_run_checks', 'automatic_checks', 'role', 'active', 'queued_followup', 'reasoning_policy', 'reasoning_decision', 'progress_guard', 'audit_check_correction', 'test_path_correction', 'check_revision') if key in task}
    for key, view in (('usage', _task_usage), ('timing', _task_timing), ('environment', _environment),
                      ('delivery', lambda value: _task_delivery(value, discussion))):
        if key in result:
            result[key] = view(result[key])
    if 'error' in task:
        result['error'] = _text(task.get('error'), 1200)
    return result


def _return_event(value):
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ('id', 'status')}


def project(full):
    result = {key: full.get(key) for key in (
        'project_id', 'phase', 'accepted', 'continuation', 'execution_mode', 'strategy',
        'effective_parallel', 'max_parallel', 'max_jobs', 'jobs_started', 'local_checks_started', 'base_commit',
        'decision_reason', 'return_health', 'return_error', 'usage', 'timing', 'contract_hash', 'kind', 'discussion',
        'job_budget') if key in full}
    if 'timing' in result:
        result['timing'] = _timing(result['timing'])
    result['return_event'] = _return_event(full.get('return_event'))
    discussion = full.get('kind') == 'discussion'
    result['tasks'] = [_project_task(task, discussion) for task in full.get('tasks') or []]
    if 'integration' in full:
        result['integration'] = _integration(full.get('integration'))
    if 'final_review' in full:
        result['final_review'] = _verdict(full.get('final_review'))
    result['detail'] = 'summary'
    result['detail_hint'] = ('Call telos_project_status with detail="full" for complete project records; '
                             'full task deliveries are in telos_project_receive and telos_status.')
    return result


def project_review(full):
    result = {key: full.get(key) for key in (
        'packet_id', 'questions', 'instruction', 'project_id', 'worktree', 'commit', 'pins',
        'contract_hash', 'blockers', 'final_review', 'identity', 'usage', 'timing', 'job_budget') if key in full}
    for key, view in (('usage', _usage), ('timing', _timing)):
        if key in result:
            result[key] = view(result[key])
    result['evidence'] = _evidence(full.get('evidence'))
    result['detail'] = 'summary'
    result['detail_hint'] = ('Call telos_project_review with detail="full" for complete successful logs, '
                             'raw receipts and the unchanged integration packet.')
    return result


def project_receive(full):
    result = {'proceed': full.get('proceed'), 'project_id': full.get('project_id'),
              'instruction': full.get('instruction'),
              'detail': 'summary',
              'detail_hint': ('Call telos_project_receive with detail="full" for the immutable original child receipts; '
                              'that lookup is read-only and returns proceed=false.')}
    if 'replayed' in full:
        result['replayed'] = full.get('replayed')
    if 'read_only' in full:
        result['read_only'] = full.get('read_only')
    if 'continuation_hint' in full:
        result['continuation_hint'] = full.get('continuation_hint')
    events = []
    for event in full.get('events') or []:
        item = {'task_id': event.get('task_id'), 'session_id': event.get('session_id'),
                'proceed': event.get('proceed'),
                'event_id': event.get('event_id'), 'next_action': event.get('next_action')}
        if isinstance(event.get('result'), dict):
            item['result'] = status(event['result'])
        else:
            item['result'] = event.get('result')
        events.append(item)
    result['events'] = events
    if isinstance(full.get('project'), dict):
        result['project'] = project(full['project'])
    else:
        result['project'] = full.get('project')
    return result

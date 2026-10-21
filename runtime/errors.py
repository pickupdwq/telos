"""Stable MCP error metadata without inferring recovery from error prose."""
from timing import number


class ToolError(Exception):
    code = 'gate_rejected'
    retryable = False
    next_action = 'inspect_error'

    def __init__(self, message, *, code=None, retryable=None, next_action=None,
                 retry_after_seconds=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        if next_action is not None:
            self.next_action = next_action
        self.retry_after_seconds = retry_after_seconds


def payload(error):
    from activity import clean
    if isinstance(error, ToolError):
        code, message = error.code, clean(str(error))[:3000]
        retryable, action = error.retryable, error.next_action
        after = error.retry_after_seconds
    else:
        # Unexpected exception strings may contain provider bodies, arguments or
        # credentials. They are not a public diagnostic/retry contract.
        code, message = 'internal_error', 'Unexpected internal failure; inspect task status before retrying.'
        retryable, action, after = False, 'inspect_status_before_retry', None
    if not number(after):
        after = None
    result = {'code': code, 'message': message, 'retryable': retryable,
              'retry_after_seconds': after, 'next_action': action}
    if not isinstance(error, ToolError):
        result['exception_type'] = type(error).__name__[:100]
    snapshot = getattr(error, 'snapshot', None)
    if isinstance(snapshot, dict):
        # A usage snapshot contains counters, never the raw request ledger.
        result['usage'] = {key: snapshot[key] for key in (
            'version', 'budget', 'totals', 'phases', 'unknown_fields', 'budget_state',
            'work_budget', 'work_budget_state', 'request_latency') if key in snapshot}
    return {'error': result}

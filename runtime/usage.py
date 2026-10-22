"""Durable per-request usage accounting and frozen task token budgets.

Only normalized counters and bounded context are persisted. Prompts, completions,
headers, credentials and arbitrary provider errors never enter the ledger.
Reasoning tokens are a subset of output tokens and cached tokens are a subset of
input tokens; they are reported separately but never added twice.
"""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
import timing
from errors import ToolError

BUDGET_FIELDS = ('max_requests', 'max_input_tokens', 'max_uncached_input_tokens',
                 'max_output_tokens', 'max_total_tokens')
EXAM_RESERVES = {'exam_reserve_requests': 'max_requests',
                 'exam_reserve_tokens': 'max_total_tokens'}
TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'uncached_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')
PHASES = ('work', 'exam')
FAILURE_STAGES = frozenset(('relay', 'upstream_open', 'upstream_read', 'response_validation'))
FAILURE_TYPES = frozenset(('timeout', 'tls', 'connection', 'os_error', 'value_error', 'other'))
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class BudgetExceeded(ToolError):
    next_action = 'inspect_usage'

    def __init__(self, message, snapshot=None):
        budget_state = snapshot.get('budget_state') if isinstance(snapshot, dict) else None
        state = budget_state.get('state') if isinstance(budget_state, dict) else None
        code = ('budget_unknown' if state == 'unknown' else
                'budget_unavailable' if snapshot is None else 'budget_exhausted')
        super().__init__(message, code=code)
        self.snapshot = snapshot


def _count(value):
    # bool is an int subclass but is not a token count.
    return value if type(value) is int and value >= 0 else None


def _details(raw, key):
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def normalize_usage(raw_usage):
    """Normalize Chat or Responses usage without inventing missing counters.

    Absent or unusable main input/output counts return None. Otherwise known
    counters are retained and unavailable detail counters stay None.
    """
    if not isinstance(raw_usage, dict):
        return None
    chat = 'prompt_tokens' in raw_usage or 'completion_tokens' in raw_usage
    input_tokens = _count(raw_usage.get('prompt_tokens' if chat else 'input_tokens'))
    output_tokens = _count(raw_usage.get('completion_tokens' if chat else 'output_tokens'))
    # Both main counters are required by the public helper contract. A partial
    # request is recorded as unknown by the ledger, while other valid requests
    # keep contributing their known aggregates.
    if input_tokens is None or output_tokens is None:
        return None
    result = {key: None for key in TOKEN_FIELDS}
    details_key = 'prompt_tokens_details' if chat else 'input_tokens_details'
    output_key = 'completion_tokens_details' if chat else 'output_tokens_details'
    cached = _count(_details(raw_usage, details_key).get('cached_tokens'))
    reasoning = _count(_details(raw_usage, output_key).get('reasoning_tokens'))
    # Subset violations are unknown rather than silently clamped.
    if cached is not None and input_tokens is not None and cached > input_tokens:
        cached = None
    if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
        reasoning = None
    uncached = (input_tokens - cached if input_tokens is not None and cached is not None else None)
    total = _count(raw_usage.get('total_tokens'))
    reported_minimum = input_tokens + output_tokens
    # A contradictory smaller reported total must never undercount a total
    # token budget; the main counters are the conservative lower bound.
    total = reported_minimum if total is None else max(total, reported_minimum)
    result.update(input_tokens=input_tokens, cached_input_tokens=cached,
                  uncached_input_tokens=uncached, output_tokens=output_tokens,
                  reasoning_output_tokens=reasoning, total_tokens=total)
    return result


def validate_budget(budget):
    """Return a frozen copy of a positive-int budget or raise ValueError."""
    if budget is None:
        return None
    fields = (*BUDGET_FIELDS, *EXAM_RESERVES, 'closeout_reserve_seconds')
    if not isinstance(budget, dict) or not budget or set(budget) - set(fields):
        raise ValueError('Task budget must contain only ' + ', '.join(fields))
    result = {}
    for key, value in budget.items():
        if type(value) is not int or value <= 0:
            raise ValueError('Task budget limits must be positive integers: ' + key)
        result[key] = value
    for reserve, limit in EXAM_RESERVES.items():
        if reserve in result and (limit not in result or result[reserve] >= result[limit]):
            raise ValueError(reserve + ' requires a larger ' + limit)
    return result


def _lock_for(path):
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class UsageLedger:
    """Append-only reservations with atomic completion and budget enforcement.

    The request count is hard: a reservation is written under a lock before the
    upstream call. Token limits are observed-spend thresholds: the next request is
    rejected once a completed/failed attempt reaches the limit. An in-flight
    response can therefore overshoot a token limit. If usage needed to evaluate a
    configured token limit is unknown, further paid requests are rejected.
    """

    def __init__(self, path, budget=None):
        self.path = Path(path)
        self.budget = validate_budget(budget)
        with self._locked():
            data = self._read()
            stored = data.get('budget')
            if stored is not None and self.budget is not None and stored != self.budget:
                raise ValueError('Frozen task budget changed after accounting began')
            if stored is None and self.budget is not None:
                # Freeze on first construction, including before the first request.
                # A legacy ledger with prior usage cannot gain a budget later.
                if data['requests']:
                    raise ValueError('Task budget cannot be added after usage accounting began')
                data['budget'] = self.budget
                self._write(data)

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(self.path.name + '.lock')
        with _lock_for(self.path), lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {'version': 1, 'budget': None, 'requests': {}}
        except ValueError:
            raise ValueError('Usage ledger is not valid JSON') from None
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('requests'), dict):
            raise ValueError('Unsupported usage ledger format')
        return data

    def _write(self, data):
        if data.get('budget') is None and self.budget is not None:
            data['budget'] = self.budget
        temp = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)

    @staticmethod
    def _entry(data, request_id):
        entry = data['requests'].get(request_id)
        if not isinstance(entry, dict):
            raise ValueError('Unknown usage request ID')
        return entry

    @staticmethod
    def _phase(phase):
        if phase == 'repair':
            return 'work'
        if phase not in PHASES:
            raise ValueError('Usage phase must be work or exam')
        return phase

    def _snapshot(self, data):
        totals = {'requests': 0, 'failed_requests': 0,
                  'unknown_usage_requests': 0, 'in_flight_requests': 0,
                  'unresolved_requests': 0}
        totals.update({key: 0 for key in TOKEN_FIELDS})
        unknown_fields = {key: 0 for key in TOKEN_FIELDS}
        phases = {phase: {'requests': 0, 'failed_requests': 0,
                          'unknown_usage_requests': 0, 'in_flight_requests': 0,
                          'unresolved_requests': 0,
                          **{key: 0 for key in TOKEN_FIELDS}} for phase in PHASES}
        for entry in data['requests'].values():
            phase = phases[self._phase(entry.get('phase', 'work'))]
            totals['requests'] += 1
            phase['requests'] += 1
            status = entry.get('status', 'reserved')
            if status == 'reserved':
                totals['in_flight_requests'] += 1
                totals['unresolved_requests'] += 1
                totals['unknown_usage_requests'] += 1
                phase['in_flight_requests'] += 1
                phase['unresolved_requests'] += 1
                phase['unknown_usage_requests'] += 1
                # A killed or interrupted request has unknown spend. Never let
                # that uncertainty bypass a configured token threshold.
                for key in TOKEN_FIELDS:
                    unknown_fields[key] += 1
                continue
            if status == 'failed':
                totals['failed_requests'] += 1
                phase['failed_requests'] += 1
            usage = entry.get('usage')
            if not isinstance(usage, dict):
                usage = {}
            if usage.get('input_tokens') is None or usage.get('output_tokens') is None:
                totals['unknown_usage_requests'] += 1
                phase['unknown_usage_requests'] += 1
            for key in TOKEN_FIELDS:
                value = _count(usage.get(key))
                if value is None:
                    unknown_fields[key] += 1
                else:
                    totals[key] += value
                    phase[key] += value
        budget = data.get('budget') if data.get('budget') is not None else self.budget
        budget_state = self._budget_state(totals, unknown_fields, budget)
        result = {'version': data.get('version', 1), 'budget': budget,
                  'totals': totals, 'phases': phases, 'unknown_fields': unknown_fields,
                  'budget_state': budget_state,
                  'request_latency': timing.request_latency(data['requests'].values())}
        work_limits = {limit: budget[limit] - budget[reserve]
                       for reserve, limit in EXAM_RESERVES.items() if budget and reserve in budget}
        if work_limits:
            result['work_budget'] = work_limits
            result['work_budget_state'] = self._budget_state(phases['work'], unknown_fields, work_limits)
        return result

    def _budget_state(self, totals, unknown_fields, budget):
        if not budget:
            return {'state': 'unlimited', 'exceeded': [], 'unknown': [], 'remaining': {}}
        exceeded, unknown, remaining = [], [], {}
        checks = {'max_requests': 'requests'}
        for field in ('max_input_tokens', 'max_uncached_input_tokens', 'max_output_tokens', 'max_total_tokens'):
            checks[field] = {'max_input_tokens': 'input_tokens', 'max_uncached_input_tokens': 'uncached_input_tokens',
                             'max_output_tokens': 'output_tokens', 'max_total_tokens': 'total_tokens'}[field]
        for limit, used_key in checks.items():
            limit_value = budget.get(limit)
            if limit_value is None:
                continue
            used = totals[used_key]
            remaining[limit] = max(0, limit_value - used)
            if used >= limit_value:
                exceeded.append(limit)
            if used_key != 'requests' and unknown_fields.get(used_key, 0):
                unknown.append(limit)
        state = 'exceeded' if exceeded else ('unknown' if unknown else 'ok')
        return {'state': state, 'exceeded': exceeded, 'unknown': unknown, 'remaining': remaining}

    def _assert_available(self, data, phase=None):
        snapshot = self._snapshot(data)
        state = snapshot['budget_state']
        if state['state'] == 'exceeded':
            raise BudgetExceeded('Task budget exhausted (' + ', '.join(state['exceeded']) + '); '
                                 + _usage_summary(snapshot, state) + '; no upstream request was sent', snapshot)
        if state['state'] == 'unknown':
            raise BudgetExceeded('Task budget cannot be evaluated because usage is unknown (' + ', '.join(state['unknown'])
                                 + '); ' + _usage_summary(snapshot, state) + '; no upstream request was sent', snapshot)
        work = snapshot.get('work_budget_state', {})
        if phase == 'work' and work.get('state') == 'exceeded':
            raise BudgetExceeded('Work budget exhausted; remaining allocation is reserved for the exam ('
                                 + ', '.join(work['exceeded']) + '); no upstream request was sent', snapshot)

    def ensure_available(self, phase=None):
        if phase is not None:
            phase = self._phase(phase)
        with self._locked():
            data = self._read()
            self._assert_available(data, phase)
            return self._snapshot(data)

    def ensure_known(self):
        """Reject an unknown token-budget state without rejecting observed overspend."""
        with self._locked():
            snapshot = self._snapshot(self._read())
            state = snapshot['budget_state']
            if state['unknown']:
                raise BudgetExceeded('Task budget cannot be evaluated because usage is unknown ('
                                     + ', '.join(state['unknown']) + '); '
                                     + _usage_summary(snapshot, state)
                                     + '; no further paid request may be sent', snapshot)
            return snapshot

    def begin(self, phase, *, provider=None, model=None, job=None):
        phase = self._phase(phase)
        with self._locked():
            data = self._read()
            if data.get('budget') is not None and self.budget is not None and data['budget'] != self.budget:
                raise ValueError('Frozen task budget changed after accounting began')
            self._assert_available(data, phase)
            request_id = uuid.uuid4().hex
            data['requests'][request_id] = {
                'request_id': request_id, 'phase': phase, 'status': 'reserved',
                'started_at': time.time(), 'provider': _bounded(provider, 64),
                'model': _bounded(model, 120), 'job': _bounded(job, 64)}
            self._write(data)
            return request_id

    def finish(self, request_id, raw_usage, status='completed', *, latency_ms=None, error_kind=None,
               upstream_request_id=None, failure_stage=None, failure_type=None):
        if status not in ('completed', 'failed'):
            raise ValueError('Usage completion status must be completed or failed')
        with self._locked():
            data = self._read()
            entry = self._entry(data, request_id)
            if entry.get('status') != 'reserved':
                return self._snapshot(data)
            entry.update(status=status, finished_at=time.time(), usage=normalize_usage(raw_usage))
            if timing.number(latency_ms):
                entry['latency_ms'] = round(latency_ms, 3)
            if error_kind is not None:
                entry['error_kind'] = _bounded_error(error_kind)
            if isinstance(upstream_request_id, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', upstream_request_id):
                entry['upstream_request_id'] = upstream_request_id
            if (status == 'failed' and type(failure_stage) is str and type(failure_type) is str
                    and failure_stage in FAILURE_STAGES and failure_type in FAILURE_TYPES):
                entry['failure_stage'] = failure_stage
                entry['failure_type'] = failure_type
            self._write(data)
            return self._snapshot(data)

    def snapshot(self):
        with self._locked():
            return self._snapshot(self._read())


def _bounded(value, limit):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _bounded_error(value):
    if not isinstance(value, str):
        return 'unknown'
    value = value.strip()
    return value[:64] if re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', value) else 'unknown'


def _usage_summary(snapshot, state):
    totals = snapshot['totals']
    used = {key: totals.get(key) for key in ('requests', 'input_tokens', 'uncached_input_tokens',
                                              'output_tokens', 'total_tokens')}
    return 'used=' + json.dumps(used, sort_keys=True) + ' remaining=' + json.dumps(state.get('remaining', {}), sort_keys=True)

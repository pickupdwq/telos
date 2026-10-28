"""Versioned, append-only project job-budget policy.

The policy lives inside the single atomic project record.  It never rewrites
max_jobs, jobs_started, the frozen contract or task history; it only replaces
the *effective* admission ceiling with an explicit unlimited strategy after an
auditable original-supervisor request.  Unknown or damaged policies fail closed.
"""
from __future__ import annotations
import hashlib
import json
import re


KIND = 'telos.job_budget_policy'
VERSION = 1
STRATEGIES = ('unlimited',)
POLICY_FIELDS = frozenset(('kind', 'version', 'strategy', 'project_id', 'owner_thread_id',
                           'initial_max_jobs', 'jobs_started', 'effective_max_jobs',
                           'contract_hash', 'updated_at', 'history'))
RECORD_FIELDS = frozenset(('seq', 'request_id', 'arguments', 'reason', 'user_authorization',
                           'owner_thread_id', 'strategy', 'prior_max_jobs', 'effective_max_jobs',
                           'jobs_started', 'contract_hash', 'created_at'))
_HEX = re.compile(r'[0-9a-f]{64}')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def arguments_digest(project_id, strategy, request_id, reason, user_authorization):
    return hashlib.sha256(_canonical({
        'project_id': project_id, 'strategy': strategy, 'request_id': request_id,
        'reason': reason, 'user_authorization': user_authorization}).encode('utf-8')).hexdigest()


def _error(message):
    raise ValueError('Invalid job budget policy: ' + message)


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        _error(where)
    return value


def _keys(value, expected, where):
    if not isinstance(value, dict) or set(value) != expected:
        _error(where + ' fields')


def validate(policy, *, project=None):
    """Return a deep, validated copy of a policy; raise ValueError when damaged."""
    _keys(policy, POLICY_FIELDS, 'policy')
    if policy['kind'] != KIND or type(policy['version']) is not int or policy['version'] != VERSION:
        _error('unknown kind or version')
    if policy['strategy'] not in STRATEGIES:
        _error('unsupported strategy')
    project_id = _text(policy['project_id'], 'project_id')
    owner_id = _text(policy['owner_thread_id'], 'owner_thread_id')
    if project_id != project_id.strip():
        _error('project_id whitespace')
    if owner_id != owner_id.strip():
        _error('owner_thread_id whitespace')
    initial = policy['initial_max_jobs']
    if type(initial) is not int or initial < 1:
        _error('initial_max_jobs')
    if type(policy['jobs_started']) is not int or policy['jobs_started'] < 0:
        _error('jobs_started snapshot')
    if policy['effective_max_jobs'] is not None:
        _error('unlimited policy must have null effective_max_jobs')
    contract = _text(policy['contract_hash'], 'contract_hash')
    if not _HEX.fullmatch(contract):
        _error('contract_hash')
    if type(policy['updated_at']) not in (int, float):
        _error('updated_at')
    history = policy['history']
    if not isinstance(history, list) or not history:
        _error('history must be a nonempty list')
    seen = set()
    last_jobs = -1
    for index, record in enumerate(history, 1):
        _keys(record, RECORD_FIELDS, 'history')
        if type(record['seq']) is not int or record['seq'] != index:
            _error('history sequence')
        request_id = _text(record['request_id'], 'history request_id')
        if request_id != request_id.strip():
            _error('history request_id whitespace')
        reason = _text(record['reason'], 'history reason')
        authorization = _text(record['user_authorization'], 'history user_authorization')
        record_owner = _text(record['owner_thread_id'], 'history owner_thread_id')
        record_contract = _text(record['contract_hash'], 'history contract_hash')
        if not _HEX.fullmatch(record_contract):
            _error('history contract_hash')
        if record['strategy'] not in STRATEGIES:
            _error('history strategy')
        if record['prior_max_jobs'] != initial or record['effective_max_jobs'] is not None:
            _error('history budget binding')
        if type(record['jobs_started']) is not int or record['jobs_started'] < last_jobs:
            _error('history jobs_started')
        if type(record['created_at']) not in (int, float):
            _error('history created_at')
        if record_owner != owner_id or record_contract != contract:
            _error('history identity binding')
        if record['prior_max_jobs'] != policy['initial_max_jobs']:
            _error('history max_jobs binding')
        digest = arguments_digest(project_id, record['strategy'], request_id, reason, authorization)
        if not isinstance(record['arguments'], str) or record['arguments'] != digest:
            _error('history arguments digest')
        if request_id in seen:
            _error('duplicate request_id')
        seen.add(request_id)
        last_jobs = record['jobs_started']
    if last_jobs != policy['jobs_started']:
        _error('history jobs_started snapshot mismatch')
    if project is not None:
        owner = (project.get('owner') or {}).get('thread_id')
        if not owner or policy['owner_thread_id'] != owner:
            _error('owner mismatch')
        if project_id != project.get('project_id'):
            _error('project identity mismatch')
        if policy['contract_hash'] != project.get('contract_hash'):
            _error('contract mismatch')
        if policy['initial_max_jobs'] != project.get('max_jobs'):
            _error('max_jobs mismatch')
        if (type(project.get('jobs_started')) is not int
                or policy['jobs_started'] > project['jobs_started']):
            _error('jobs_started snapshot mismatch')
    return json.loads(json.dumps(policy))


def validate_project(project):
    """Fail closed when a policy key exists but cannot be trusted.

    Only an absent key means the legacy fixed-ceiling behavior.  An explicit
    null is damaged and must not silently grant or restore a ceiling.
    """
    if 'job_budget_policy' not in project:
        return None
    return validate(project['job_budget_policy'], project=project)


def effective_max_jobs(project):
    """Admission ceiling: int for legacy/default, None for unlimited."""
    policy = validate_project(project)
    if policy is None:
        return project.get('max_jobs')
    return None


def summary(project):
    policy = validate_project(project)
    if policy is None:
        return {'strategy': 'fixed', 'initial_max_jobs': project.get('max_jobs'),
                'effective_max_jobs': project.get('max_jobs'),
                'jobs_started': project.get('jobs_started'), 'authorization_history': []}
    return {'strategy': policy['strategy'], 'initial_max_jobs': policy['initial_max_jobs'],
            'effective_max_jobs': None, 'jobs_started': project.get('jobs_started'),
            'authorization_history': policy['history']}


def authorize(project, *, owner_thread_id, strategy, request_id, reason,
              user_authorization, now):
    """Return (updated project, replayed, policy summary) without persisting.

    The caller must hold the project coordinator lock and must not save when
    ``replayed`` is true.  request_id is stored verbatim; surrounding whitespace
    is rejected so replay comparison cannot silently normalize to a different id.
    """
    if strategy not in STRATEGIES:
        raise ValueError('Unsupported job budget strategy')
    for name, value in (('request_id', request_id), ('reason', reason),
                        ('user_authorization', user_authorization), ('owner_thread_id', owner_thread_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(name + ' must be a nonempty string')
    if request_id != request_id.strip():
        raise ValueError('request_id must not have surrounding whitespace')
    owner = (project.get('owner') or {}).get('thread_id')
    if not owner or owner != owner_thread_id:
        raise ValueError('owner mismatch')
    existing = validate_project(project)
    canonical_reason = reason.strip()
    canonical_authorization = user_authorization.strip()
    digest = arguments_digest(project['project_id'], strategy, request_id,
                              canonical_reason, canonical_authorization)
    if existing is not None:
        for record in existing['history']:
            if record['request_id'] == request_id:
                if record['arguments'] != digest:
                    raise ValueError('request_id already used with different arguments')
                return project, True, summary(project)
        raise ValueError('Project already has an unlimited job budget policy')
    record = {
        'seq': 1, 'request_id': request_id, 'arguments': digest, 'reason': canonical_reason,
        'user_authorization': canonical_authorization, 'owner_thread_id': owner,
        'strategy': strategy, 'prior_max_jobs': project['max_jobs'],
        'effective_max_jobs': None, 'jobs_started': project['jobs_started'],
        'contract_hash': project['contract_hash'], 'created_at': now,
    }
    policy = {
        'kind': KIND, 'version': VERSION, 'strategy': strategy,
        'project_id': project['project_id'], 'owner_thread_id': owner,
        'initial_max_jobs': project['max_jobs'], 'jobs_started': project['jobs_started'],
        'effective_max_jobs': None, 'contract_hash': project['contract_hash'],
        'updated_at': now, 'history': [record],
    }
    updated = dict(project)
    updated['job_budget_policy'] = validate(policy, project=project)
    return updated, False, summary(updated)

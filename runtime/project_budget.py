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
WORK_KIND = 'telos.work_turn_budget_policy'
WORK_VERSION = 1
WORK_SCOPE = 'work_turns'
WORK_POLICY_FIELDS = frozenset(('kind', 'version', 'scope', 'strategy', 'project_id',
                                'owner_thread_id', 'original_max_turns', 'turns_snapshot',
                                'turns_source', 'jobs_started',
                                'prior_policy_digest', 'contract_hash', 'effective_max_turns',
                                'updated_at', 'history'))
WORK_RECORD_FIELDS = frozenset(('seq', 'request_id', 'arguments', 'scope', 'reason',
                                'user_authorization', 'owner_thread_id', 'strategy',
                                'prior_max_turns', 'effective_max_turns', 'turns_snapshot',
                                'turns_source',
                                'jobs_started', 'prior_policy_digest', 'contract_hash',
                                'created_at'))
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


def work_arguments_digest(project_id, request_id, reason, user_authorization,
                          prior_policy_digest):
    """Canonical, scope-aware digest for the sibling work-turn policy."""
    return hashlib.sha256(_canonical({
        'scope': WORK_SCOPE, 'project_id': project_id, 'request_id': request_id,
        'reason': reason, 'user_authorization': user_authorization,
        'prior_policy_digest': prior_policy_digest}).encode('utf-8')).hexdigest()


def policy_digest(policy):
    """Stable digest of the exact validated v1 policy object."""
    return hashlib.sha256(_canonical(policy).encode('utf-8')).hexdigest()


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
    work_existing = validate_work_project(project)
    if work_existing is not None:
        for record in work_existing['history']:
            if record['request_id'] == request_id:
                raise ValueError('request_id already used in another policy scope')
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


def _work_error(message):
    raise ValueError('Invalid work turn budget policy: ' + message)


def _work_keys(value, expected, where):
    if not isinstance(value, dict) or set(value) != expected:
        _work_error(where + ' fields')


def validate_work(policy, *, project=None):
    """Return a deep, validated copy of the sibling work-turn policy."""
    _work_keys(policy, WORK_POLICY_FIELDS, 'work policy')
    if (policy['kind'] != WORK_KIND or type(policy['version']) is not int
            or policy['version'] != WORK_VERSION):
        _work_error('unknown kind or version')
    if policy['scope'] != WORK_SCOPE:
        _work_error('scope')
    if policy['strategy'] not in STRATEGIES:
        _work_error('unsupported strategy')
    project_id = _text(policy['project_id'], 'project_id')
    owner_id = _text(policy['owner_thread_id'], 'owner_thread_id')
    if project_id != project_id.strip() or owner_id != owner_id.strip():
        _work_error('identity whitespace')
    original = policy['original_max_turns']
    if type(original) is not int or original < 1:
        _work_error('original_max_turns')
    _turn_snapshot(policy['turns_snapshot'], 'turns_snapshot')
    _turns_source(policy['turns_source'])
    if type(policy['jobs_started']) is not int or policy['jobs_started'] < 0:
        _work_error('jobs_started snapshot')
    if policy['effective_max_turns'] is not None:
        _work_error('unlimited work policy must have null effective_max_turns')
    prior = _text(policy['prior_policy_digest'], 'prior_policy_digest')
    if not _HEX.fullmatch(prior):
        _work_error('prior_policy_digest')
    contract = _text(policy['contract_hash'], 'contract_hash')
    if not _HEX.fullmatch(contract):
        _work_error('contract_hash')
    if type(policy['updated_at']) not in (int, float):
        _work_error('updated_at')
    history = policy['history']
    if not isinstance(history, list) or not history:
        _work_error('history must be a nonempty list')
    seen = set()
    last_jobs = -1
    for index, record in enumerate(history, 1):
        _work_keys(record, WORK_RECORD_FIELDS, 'history')
        if type(record['seq']) is not int or record['seq'] != index:
            _work_error('history sequence')
        request_id = _text(record['request_id'], 'history request_id')
        if request_id != request_id.strip():
            _work_error('history request_id whitespace')
        if record['scope'] != WORK_SCOPE:
            _work_error('history scope')
        if record['strategy'] not in STRATEGIES:
            _work_error('history strategy')
        reason = _text(record['reason'], 'history reason')
        authorization = _text(record['user_authorization'], 'history user_authorization')
        record_owner = _text(record['owner_thread_id'], 'history owner_thread_id')
        record_contract = _text(record['contract_hash'], 'history contract_hash')
        if not _HEX.fullmatch(record_contract):
            _work_error('history contract_hash')
        record_prior = _text(record['prior_policy_digest'], 'history prior_policy_digest')
        if not _HEX.fullmatch(record_prior) or record_prior != prior:
            _work_error('history prior_policy_digest binding')
        if record['prior_max_turns'] != original or record['prior_max_turns'] != policy['original_max_turns']:
            _work_error('history original_max_turns binding')
        if record['effective_max_turns'] is not None:
            _work_error('history effective_max_turns')
        _turn_snapshot(record['turns_snapshot'], 'history turns_snapshot')
        _turns_source(record['turns_source'])
        if record['turns_snapshot'] != policy['turns_snapshot']:
            _work_error('history turns_snapshot binding')
        if record['turns_source'] != policy['turns_source']:
            _work_error('history turns_source binding')
        if type(record['jobs_started']) is not int or record['jobs_started'] < 0:
            _work_error('history jobs_started')
        if record['jobs_started'] < last_jobs:
            _work_error('history jobs_started order')
        if type(record['created_at']) not in (int, float):
            _work_error('history created_at')
        if record_owner != owner_id or record_contract != contract:
            _work_error('history identity binding')
        digest = work_arguments_digest(project_id, request_id, reason, authorization, prior)
        if not isinstance(record['arguments'], str) or record['arguments'] != digest:
            _work_error('history arguments digest')
        if request_id in seen:
            _work_error('duplicate request_id')
        seen.add(request_id)
        last_jobs = record['jobs_started']
    if history[-1]['jobs_started'] != policy['jobs_started']:
        _work_error('history jobs_started snapshot mismatch')
    if project is not None:
        base = validate_project(project)
        if base is None:
            _work_error('a valid job_budget_policy is required')
        owner = (project.get('owner') or {}).get('thread_id')
        if not owner or policy['owner_thread_id'] != owner:
            _work_error('owner mismatch')
        if project_id != project.get('project_id'):
            _work_error('project identity mismatch')
        if policy['contract_hash'] != project.get('contract_hash'):
            _work_error('contract mismatch')
        if policy['prior_policy_digest'] != policy_digest(base):
            _work_error('prior job policy binding')
        # Cross-scope request-id uniqueness is enforced on load, not only creation.
        if any(record['request_id'] in seen for record in base['history']):
            _work_error('request_id collides with another policy scope')
        current_jobs = project.get('jobs_started')
        if type(current_jobs) is not int or policy['jobs_started'] > current_jobs:
            _work_error('jobs_started exceeds current project count')
        if any(record['jobs_started'] > current_jobs for record in history):
            _work_error('history jobs_started exceeds current project count')
    return json.loads(json.dumps(policy))


def _turn_snapshot(value, where):
    if not isinstance(value, dict) or not value:
        _work_error(where + ' must be a nonempty mapping')
    for session_id, turns in value.items():
        _text(session_id, where + ' session_id')
        if turns is not None and (type(turns) is not int or turns < 0):
            _work_error(where + ' turns')
    return value


def _turns_source(value):
    if value not in ('child_session_state', 'unknown'):
        _work_error('turns_source')
    return value


def validate_work_project(project):
    """Absent sibling key is legacy; explicit null or damage fails closed."""
    if 'work_turn_budget_policy' not in project:
        return None
    return validate_work(project['work_turn_budget_policy'], project=project)


def work_turn_summary(project, configured_max_turns=None):
    policy = validate_work_project(project)
    if policy is None:
        return {'scope': WORK_SCOPE, 'strategy': 'fixed',
                'configured_max_turns': configured_max_turns,
                'initial_max_turns': configured_max_turns,
                'effective_max_turns': configured_max_turns,
                'turns_snapshot': None, 'turns_source': None,
                'authorization_history': []}
    return {'scope': WORK_SCOPE, 'strategy': policy['strategy'],
            'configured_max_turns': configured_max_turns,
            'initial_max_turns': policy['original_max_turns'],
            'effective_max_turns': None,
            'turns_snapshot': policy['turns_snapshot'],
            'turns_source': policy['turns_source'],
            'authorization_history': policy['history']}


def authorize_work(project, *, owner_thread_id, request_id, reason, user_authorization,
                   now, original_max_turns, turns_snapshot, turns_source='child_session_state'):
    """Record the sibling work-turn scope; never mutates the v1 policy object."""
    for name, value in (('request_id', request_id), ('reason', reason),
                        ('user_authorization', user_authorization),
                        ('owner_thread_id', owner_thread_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(name + ' must be a nonempty string')
    if request_id != request_id.strip():
        raise ValueError('request_id must not have surrounding whitespace')
    owner = (project.get('owner') or {}).get('thread_id')
    if not owner or owner != owner_thread_id:
        raise ValueError('owner mismatch')
    base = validate_project(project)
    if base is None:
        raise ValueError('A valid unlimited job budget policy is required first')
    for record in base['history']:
        if record['request_id'] == request_id:
            raise ValueError('request_id already used in another policy scope')
    existing = validate_work_project(project)
    canonical_reason = reason.strip()
    canonical_authorization = user_authorization.strip()
    _turn_snapshot(turns_snapshot, 'turns_snapshot')
    _turns_source(turns_source)
    snapshot = json.loads(json.dumps(turns_snapshot))
    prior = policy_digest(base)
    digest = work_arguments_digest(project['project_id'], request_id,
                                   canonical_reason, canonical_authorization, prior)
    if existing is not None:
        for record in existing['history']:
            if record['request_id'] == request_id:
                if record['arguments'] != digest:
                    raise ValueError('request_id already used with different arguments')
                return project, True, work_turn_summary(project, original_max_turns)
        raise ValueError('Project already has a work turn budget policy')
    if type(original_max_turns) is not int or original_max_turns < 1:
        raise ValueError('original_max_turns must be a positive integer')
    record = {
        'seq': 1, 'request_id': request_id, 'arguments': digest, 'scope': WORK_SCOPE,
        'reason': canonical_reason, 'user_authorization': canonical_authorization,
        'owner_thread_id': owner, 'strategy': 'unlimited',
        'prior_max_turns': original_max_turns,
        'effective_max_turns': None, 'turns_snapshot': snapshot,
        'turns_source': turns_source,
        'jobs_started': project['jobs_started'], 'prior_policy_digest': prior,
        'contract_hash': project['contract_hash'], 'created_at': now,
    }
    policy = {
        'kind': WORK_KIND, 'version': WORK_VERSION, 'scope': WORK_SCOPE,
        'strategy': 'unlimited', 'project_id': project['project_id'],
        'owner_thread_id': owner, 'original_max_turns': original_max_turns,
        'turns_snapshot': json.loads(json.dumps(snapshot)),
        'turns_source': turns_source,
        'jobs_started': project['jobs_started'],
        'prior_policy_digest': prior, 'contract_hash': project['contract_hash'],
        'effective_max_turns': None, 'updated_at': now, 'history': [record],
    }
    updated = dict(project)
    updated['work_turn_budget_policy'] = validate_work(policy, project=project)
    return updated, False, work_turn_summary(updated, original_max_turns)


def work_turn_decision(project, *, session_id, child_contract_hash=None,
                       child_owner_thread_id=None):
    """Trusted admission decision computed by the coordinator under its lock."""
    policy = validate_work_project(project)
    if policy is None:
        return None
    if project.get('continuation') != 'active':
        return None
    if project.get('phase') in ('accepted', 'cancelled'):
        return None
    task = next((t for t in project.get('tasks', []) if t.get('session_id') == session_id), None)
    if task is None or task.get('phase') in ('accepted', 'cancelled', 'setup_error'):
        return None
    if task.get('project_id') not in (None, project['project_id']):
        return None
    if not isinstance(child_contract_hash, str) or not child_contract_hash:
        return None
    if child_owner_thread_id != policy['owner_thread_id']:
        return None
    return {'scope': WORK_SCOPE, 'exempt': True, 'project_id': project['project_id'],
            'session_id': session_id, 'owner_thread_id': policy['owner_thread_id'],
            'child_contract_hash': child_contract_hash,
            'policy_digest': policy_digest(policy)}

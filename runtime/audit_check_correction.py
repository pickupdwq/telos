"""Owner-authorized correction of one invalid historical audit status spelling.

This is deliberately not a general contract editor. The original contract file
is immutable; one versioned record derives the effective contract using a fixed
AST rule. No caller-supplied program, check IDs or replacement values are accepted.
"""
import ast
import copy
import math
import time

from audit import RESULTS


KIND = 'audit-status-pass-to-passed'
KEY = 'audit_check_correction'
EVIDENCE_FIELDS = {'phase', 'rounds', 'turns', 'packet_id', 'submitted_commit',
                   'last_result', 'acceptance', 'auto_check'}


def corrected_contract(contract):
    if contract.get('task_type') != 'audit':
        raise ValueError('Only audit contracts support this status correction')
    result = copy.deepcopy(contract)
    changes = []
    for check in result['checks']:
        argv = check['argv']
        if len(argv) != 3 or argv[:2] != ['{python}', '-c']:
            raise ValueError('Audit checks must use the frozen inline Python form')
        code = argv[2]
        tree = ast.parse(code)
        offsets, position = [], 0
        for line in code.splitlines(keepends=True):
            offsets.append(position)
            position += len(line.encode('utf-8'))
        spans = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                    and isinstance(node.ops[0], ast.In)
                    and isinstance(node.left, ast.Subscript)
                    and isinstance(node.left.slice, ast.Constant)
                    and node.left.slice.value == 'status'
                    and isinstance(node.comparators[0], (ast.Tuple, ast.List, ast.Set))):
                continue
            items = node.comparators[0].elts
            if not all(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in items):
                continue
            values = [x.value for x in items]
            # Restrict the migration to a misspelled whitelist member. This
            # cannot alter exclusions, arbitrary strings or any valid status.
            if (values.count('pass') != 1 or 'passed' in values
                    or any(x not in (*RESULTS, 'pass') for x in values)):
                continue
            item = next(x for x in items if x.value == 'pass')
            spans.append((offsets[item.lineno - 1] + item.col_offset,
                          offsets[item.end_lineno - 1] + item.end_col_offset))
        if not spans:
            continue
        raw = code.encode('utf-8')
        for start, end in sorted(set(spans), reverse=True):
            raw = raw[:start] + b"'passed'" + raw[end:]
        fixed = raw.decode('utf-8')
        ast.parse(fixed)
        argv[2] = fixed
        changes.append({'check_id': check['id'], 'before_program': code,
                        'after_program': fixed, 'replacements': len(set(spans))})
    if not changes:
        raise ValueError('No supported invalid audit status membership was found; no change made')
    return result, changes


def effective(b, state, original):
    """Validate an explicit record on every load; absence alone is legacy."""
    if KEY not in state:
        if b.digest(original) != state['contract_hash']:
            raise ValueError('Frozen contract changed; acceptance is invalid')
        return original
    row = state[KEY]
    fields = {'version', 'kind', 'session_id', 'owner_thread_id', 'arguments',
              'arguments_hash', 'created_at', 'original_contract_hash',
              'effective_contract_hash', 'changes', 'prior_evidence', 'record_hash'}
    if not isinstance(row, dict) or set(row) != fields:
        raise ValueError('Invalid audit check correction record')
    if (type(row['version']) is not int or row['version'] != 1 or row['kind'] != KIND
            or row['session_id'] != state['session_id']
            or not row['owner_thread_id']
            or row['owner_thread_id'] != (state.get('owner') or {}).get('thread_id')
            or type(row['created_at']) not in (int, float) or not math.isfinite(row['created_at'])
            or row['original_contract_hash'] != b.digest(original)
            or row['record_hash'] != b.digest({k: v for k, v in row.items() if k != 'record_hash'})):
        raise ValueError('Audit check correction identity or canonical digest changed')
    arguments = row['arguments']
    if (not isinstance(arguments, dict)
            or set(arguments) != {'request_id', 'reason', 'user_authorization', 'event_id'}
            or any(not isinstance(arguments[k], str) or not arguments[k].strip()
                   or len(arguments[k]) > 4000 for k in ('request_id', 'reason', 'user_authorization'))
            or arguments['request_id'] != arguments['request_id'].strip()
            or not isinstance(arguments['event_id'], str) or not arguments['event_id'].strip()
            or row['arguments_hash'] != b.digest(arguments)):
        raise ValueError('Invalid audit check correction arguments')
    evidence = row['prior_evidence']
    if (not isinstance(evidence, dict) or set(evidence) != EVIDENCE_FIELDS
            or evidence['phase'] not in ('responded', 'not_accepted')
            or any(type(evidence[k]) is not int or evidence[k] < 0 for k in ('rounds', 'turns'))
            or any(evidence[k] is not None and not isinstance(evidence[k], str)
                   for k in ('packet_id', 'submitted_commit'))
            or any(evidence[k] is not None and not isinstance(evidence[k], dict)
                   for k in ('last_result', 'acceptance', 'auto_check'))):
        raise ValueError('Invalid audit check correction prior evidence')
    fixed, changes = corrected_contract(original)
    if (row['changes'] != changes or row['effective_contract_hash'] != b.digest(fixed)
            or state['contract_hash'] != row['effective_contract_hash']):
        raise ValueError('Audit check correction differs from its fixed AST rule')
    return fixed


def public(state):
    row = state.get(KEY)
    if not row:
        return None
    return {k: row[k] for k in ('version', 'kind', 'session_id', 'owner_thread_id',
                               'arguments', 'created_at', 'original_contract_hash',
                               'effective_contract_hash', 'record_hash')} | {
        'changed_checks': [{'check_id': c['check_id'], 'replacements': c['replacements']}
                           for c in row['changes']],
        'prior_evidence': {k: row['prior_evidence'].get(k) for k in
                           ('phase', 'packet_id', 'submitted_commit', 'rounds', 'turns')}}


def correct(b, sid, request_id, reason, user_authorization, event_id, caller):
    arguments = dict(request_id=request_id, reason=reason,
                     user_authorization=user_authorization, event_id=event_id)
    if (any(not isinstance(arguments[k], str) or not arguments[k].strip()
            or len(arguments[k]) > 4000 for k in ('request_id', 'reason', 'user_authorization'))
            or request_id != request_id.strip() or not isinstance(event_id, str) or not event_id.strip()):
        raise b.GateError('Provide bounded request_id, reason, user_authorization and received event_id')
    with b.project_manager().lock(), b.locked(sid) as root:
        state, contract = b.load_session(root)
        owner = (state.get('owner') or {}).get('thread_id')
        if not owner or caller != owner:
            raise b.GateError('Audit check correction requires the original trusted supervisor',
                              code='owner_mismatch')
        if state.get('project_id'):
            project = b.project_manager().load(state['project_id'])
            b.project_manager().authorize(project, caller)
            b.project_manager().active(project)
        if state.get('continuation', 'active') != 'active' or state.get('archived'):
            raise b.GateError('Honor paused/cancelled or archived task controls')
        if state['phase'] == 'accepted' or (state.get('acceptance') or {}).get('accepted'):
            raise b.GateError('Accepted audit tasks cannot be corrected')
        from supervisor import verify_owner
        verify_owner(b, state.get('owner'))
        old = state.get(KEY)
        if old is not None:
            if old['arguments'] != arguments:
                raise b.GateError('Audit status correction already recorded; request arguments cannot change')
            return {'session_id': sid, 'duplicate': True, 'accepted': False,
                    'correction': public(state), 'next_action': 'telos_status'}
        if (contract.get('task_type') != 'audit' or state['phase'] not in ('responded', 'not_accepted')
                or (state.get('last_result') or {}).get('exam_received')
                or state.get('clarification')
                or (state.get('auto_check') or {}).get('status') in ('queued', 'running')):
            raise b.GateError('Correction requires completed audit work without running jobs, an exam or acceptance')
        if b.event_action(state, event_id, 'correct_audit_status', arguments):
            raise b.GateError('Correction action exists without its correction record')
        for value in (reason, user_authorization):
            b.deny_leak(value, contract)
        try:
            fixed, changes = corrected_contract(contract)
        except (ValueError, SyntaxError, KeyError, TypeError) as error:
            raise b.GateError(str(error)) from None
        row = {'version': 1, 'kind': KIND, 'session_id': sid, 'owner_thread_id': owner,
               'arguments': arguments, 'arguments_hash': b.digest(arguments), 'created_at': time.time(),
               'original_contract_hash': state['contract_hash'], 'effective_contract_hash': b.digest(fixed),
               'changes': changes, 'prior_evidence': copy.deepcopy({k: state.get(k) for k in EVIDENCE_FIELDS})}
        row['record_hash'] = b.digest(row)
        state[KEY] = row
        state['contract_hash'] = row['effective_contract_hash']
        # Files and prior counters/history are retained. Only current evidence
        # pointers are invalidated, so nothing can be accepted without new gates.
        for key in ('packet_id', 'acceptance', 'auto_check'):
            state.pop(key, None)
        result = {'session_id': sid, 'accepted': False, 'correction': public(state),
                  'next_action': 'telos_evaluate', 'event_id': event_id}
        b.record_action(state, event_id, 'correct_audit_status', arguments, result)
        b.write_json(root / 'state.json', state)
        return result

"""Owner-reviewed revision of one existing check program.

Only ``check['argv'][2]`` may change.  Every other contract field, the frozen
contract bytes, counters, budget and history are retained.  Preview is
read-only; apply recomputes under the project/session locks and appends one
digest-chained revision after the two historical fixed corrections have been
validated.  This does not claim semantic equivalence of the reviewed diff.
"""
import ast
import contextlib
import fcntl
import copy
import difflib
import hashlib
import json
import math
import re
import time

KEY = 'check_revision'
KIND = 'verified-check-program-revision'
FIELDS = {'version', 'kind', 'session_id', 'owner_thread_id', 'original_contract_hash',
          'revisions', 'record_hash'}
ROW_FIELDS = {'seq', 'check_id', 'before_program', 'after_program', 'before_hash', 'after_hash',
              'arguments', 'arguments_hash', 'created_at', 'proposal_hash', 'source_commit',
              'original_contract_hash', 'prior_evidence', 'prior_evidence_hash',
              'preservation_reason', 'evidence',
              'project_id', 'base_effective_hash', 'previous_record_hash', 'legacy_record_hashes', 'source_binding',
              'effective_contract_hash', 'record_hash'}
PRIOR = ('phase', 'rounds', 'turns', 'packet_id', 'submitted_commit', 'last_result',
         'acceptance', 'auto_check')
PROPOSAL_FIELDS = ('version', 'kind', 'session_id', 'project_id', 'owner_thread_id',
                   'check_id', 'arguments', 'original_contract_hash', 'base_effective_hash',
                   'before_hash', 'after_hash', 'source_commit', 'prior_evidence',
                   'prior_evidence_hash', 'preservation_reason', 'evidence',
                   'effective_contract_hash', 'previous_record_hash', 'legacy_record_hashes', 'source_binding')
MAX_PROGRAM = 200_000


def _program_hash(program):
    if not isinstance(program, str) or len(program) > MAX_PROGRAM:
        raise ValueError('Check programs must be bounded strings')
    return hashlib.sha256(program.encode()).hexdigest()


def _arguments(request_id, reason, user_authorization, event_id, check_id,
               before_hash, after_hash, preservation_reason, evidence):
    values = dict(request_id=request_id, reason=reason, user_authorization=user_authorization,
                  event_id=event_id, check_id=check_id, before_hash=before_hash,
                  after_hash=after_hash, preservation_reason=preservation_reason, evidence=evidence)
    if (any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in values.values())
            or request_id != request_id.strip()
            or any(not re.fullmatch(r'[0-9a-f]{64}', values[k]) for k in ('before_hash', 'after_hash'))):
        raise ValueError('Provide bounded canonical revision arguments')
    return values


def _legacy_records(b, state):
    return {key: b.digest(state[key]) for key in ('audit_check_correction', 'test_path_correction') if key in state}


@contextlib.contextmanager
def _no_active_probe(root):
    # Probe execution drops the session lock during I/O. Hold its independent
    # lock as well; preview never creates a missing lock file.
    path = root / 'audit-probe.lock'
    if not path.exists():
        yield
        return
    with path.open('rb') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Wait for the running audit probe to finish') from None
        yield


def _retained_files(b, root, state):
    files = {}
    names = [f"attempt-{state['rounds']}.json", f"attempt-{state['rounds']}-checks.json", 'audit-probes.json']
    for name in names:
        path = root / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 32 * 1024**2)):
            raise ValueError('Retained acceptance evidence is redirected or unbounded')
        if path.exists():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            if name == names[0] and state.get('packet_id') and b.digest(b.read_json(path)) != state['packet_id']:
                raise ValueError('Retained packet content differs from its submitted identity')
    return files


def _live_source(b, state, contract):
    root = state.get('worktree')
    if not root:
        raise ValueError('The submitted worktree is required for source verification')
    head = b.git(root, 'rev-parse', 'HEAD')
    if state.get('submitted_commit') and state['submitted_commit'] != head:
        raise ValueError('Worktree HEAD differs from the submitted commit')
    if b.git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Worktree has changed; a clean submitted source is required')
    binding = {'commit': head, 'tree': b.git(root, 'rev-parse', 'HEAD^{tree}')}
    if contract.get('task_type') == 'audit':
        import audit
        files = audit.manifest(audit.artifacts(b, state), contract['audit']['limits']['max_artifact_bytes'])
        binding['audit_manifest_hash'] = b.digest(files)
    return binding


def _asserts(tree):
    return sum(isinstance(node, ast.Assert) for node in ast.walk(tree)) + sum(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith('assert') for node in ast.walk(tree))


def _check(contract, check_id):
    checks = contract.get('checks')
    if not isinstance(checks, list):
        raise ValueError('Contract checks are invalid')
    matches = [c for c in checks if isinstance(c, dict) and c.get('id') == check_id]
    if len(matches) != 1:
        raise ValueError('Revise exactly one existing check by id')
    return matches[0]


def revised(contract, check_id, before, after):
    """Return the single-field revision or raise; never executes either program."""
    if (not isinstance(before, str) or not isinstance(after, str)
            or len(before) > MAX_PROGRAM or len(after) > MAX_PROGRAM):
        raise ValueError('Check programs must be bounded strings')
    target = _check(contract, check_id)
    argv = target.get('argv')
    if (not isinstance(argv, list) or len(argv) < 3 or argv[0] != '{python}'
            or argv[1] != '-c' or not isinstance(argv[2], str)):
        raise ValueError('Only the frozen inline Python check form is supported')
    if argv[2] != before:
        raise ValueError('The supplied before program is not the current effective check')
    if before == after:
        raise ValueError('The proposed revision makes no change')
    try:
        old_tree = ast.parse(before, filename='<check before>')
        new_tree = ast.parse(after, filename='<check after>')
        compile(after, '<check after>', 'exec')
    except (SyntaxError, ValueError) as error:
        raise ValueError('The revised check is not valid Python: %s'
                         % getattr(error, 'msg', error)) from None
    if _asserts(new_tree) < _asserts(old_tree):
        raise ValueError('The revision removes assertion nodes; standard preservation is required')
    result = copy.deepcopy(contract)
    _check(result, check_id)['argv'][2] = after
    return result


def _evidence_hash(state):
    value = {key: state.get(key) for key in ('packet_id', 'acceptance', 'auto_check')}
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _diff(before, after):
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile='before', tofile='after'))


def _proposal(core):
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _candidate(b, root, state, contract, raw_contract, check_id, before, after, arguments,
               preservation_reason, evidence):
    fixed = revised(contract, check_id, before, after)
    if state['phase'] not in ('responded', 'not_accepted'):
        raise ValueError('Revision requires completed unaccepted work')
    if ((state.get('last_result') or {}).get('exam_received') or state.get('clarification')
            or (state.get('auto_check') or {}).get('status') in ('queued', 'running')
            or (state.get('job') or {}).get('status') in ('running', 'queued')):
        raise ValueError('Wait for running work, probes, an exam or acceptance to finish')
    source_binding = _live_source(b, state, contract)
    source_binding['retained_files'] = _retained_files(b, root, state)
    commit = source_binding['commit']
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise ValueError('A submitted immutable commit is required')
    prior = {key: copy.deepcopy(state.get(key)) for key in PRIOR}
    evidence_hash = _evidence_hash(state)
    core = {'version': 1, 'kind': KIND, 'session_id': state['session_id'],
            'project_id': state.get('project_id'),
            'owner_thread_id': state['owner']['thread_id'], 'check_id': check_id,
            'before_program': before, 'after_program': after,
            'arguments': arguments, 'arguments_hash': b.digest(arguments),
            'original_contract_hash': b.digest(raw_contract),
            'base_effective_hash': b.digest(contract),
            'before_hash': hashlib.sha256(before.encode()).hexdigest(),
            'after_hash': hashlib.sha256(after.encode()).hexdigest(),
            'source_commit': commit, 'source_binding': source_binding,
            'legacy_record_hashes': _legacy_records(b, state),
            'previous_record_hash': (state.get(KEY) or {}).get('record_hash'),
            'prior_evidence': prior,
            'prior_evidence_hash': evidence_hash, 'preservation_reason': preservation_reason,
            'evidence': evidence, 'effective_contract_hash': b.digest(fixed)}
    return {**core, 'proposal_hash': _proposal({k: core[k] for k in PROPOSAL_FIELDS})}


def effective(b, state, original, frozen):
    """Validate the full revision chain after the two historical corrections."""
    if KEY not in state:
        return frozen
    row = state[KEY]
    if row is None:
        raise ValueError('Check revision record is explicitly null')
    if ('audit_check_correction' in state and 'test_path_correction' in state
            or not isinstance(row, dict) or set(row) != FIELDS
            or type(row['version']) is not int or row['version'] != 1
            or row['kind'] != KIND or row['session_id'] != state['session_id']
            or row['owner_thread_id'] != (state.get('owner') or {}).get('thread_id')
            or row['original_contract_hash'] != b.digest(original)
            or row['record_hash'] != b.digest({k: v for k, v in row.items() if k != 'record_hash'})
            or not isinstance(row['revisions'], list) or not row['revisions']):
        raise ValueError('Invalid or conflicting check revision record')
    current = frozen
    previous_record_hash = None
    for index, item in enumerate(row['revisions'], 1):
        if (not isinstance(item, dict) or set(item) != ROW_FIELDS or item['seq'] != index
                or item['record_hash'] != b.digest({k: v for k, v in item.items()
                                                   if k != 'record_hash'})
                or item['base_effective_hash'] != b.digest(current)
                or item['previous_record_hash'] != previous_record_hash
                or item['legacy_record_hashes'] != _legacy_records(b, state)
                or item['project_id'] != state.get('project_id')
                or item['original_contract_hash'] != b.digest(original)
                or item['before_hash'] != _program_hash(item['before_program'])
                or item['after_hash'] != _program_hash(item['after_program'])
                or not isinstance(item['source_binding'], dict)
                or item['source_binding'].get('commit') != item['source_commit']
                or item['proposal_hash'] != _proposal({
                    **{k: row[k] for k in ('version', 'kind', 'session_id', 'owner_thread_id')},
                    **{k: item[k] for k in PROPOSAL_FIELDS
                       if k not in ('version', 'kind', 'session_id', 'owner_thread_id')}})):
            raise ValueError('Check revision chain is damaged or ambiguous')
        if (not isinstance(item['arguments'], dict) or item['arguments'] != _arguments(**item['arguments'])
                or any(item['arguments'].get(k) != item[k] for k in ('check_id', 'before_hash', 'after_hash', 'preservation_reason', 'evidence'))
                or item['arguments_hash'] != b.digest(item['arguments'])
                or type(item['created_at']) not in (int, float) or not math.isfinite(item['created_at'])):
            raise ValueError('Invalid check revision authorization')
        event_id = item['arguments']['event_id']
        event = (state.get('events') or {}).get(event_id) or {}
        action = (event.get('actions') or {}).get('revise_checks') or {}
        result = action.get('result') or {}
        if (action.get('arguments_hash') != item['arguments_hash']
                or action.get('action_id') != b.digest([event_id, 'revise_checks', item['arguments']])
                or (result.get('revision') or {}).get('proposal_hash') != item['proposal_hash']):
            raise ValueError('Check revision event action no longer matches its record')
        if not isinstance(item['preservation_reason'], str) or not item['preservation_reason'].strip():
            raise ValueError('Missing supervision standard-preservation reason')
        if not isinstance(item['evidence'], str) or not item['evidence'].strip():
            raise ValueError('Missing evidence reference')
        current = revised(current, item['check_id'], item['before_program'], item['after_program'])
        if item['effective_contract_hash'] != b.digest(current):
            raise ValueError('Check revision differs from its recorded diff')
        previous = {k: v for k, v in row.items() if k not in ('record_hash', 'revisions')}
        previous['revisions'] = row['revisions'][:index]
        previous_record_hash = b.digest(previous)
    if b.digest(current) != state.get('contract_hash'):
        raise ValueError('Check revision chain does not match the effective contract hash')
    return current


def public(state):
    row = state.get(KEY)
    if row is None:
        return None
    return {k: row[k] for k in ('version', 'kind', 'session_id', 'owner_thread_id',
                               'original_contract_hash', 'record_hash')} | {
        'revisions': [{k: item[k] for k in ('seq', 'check_id', 'arguments', 'created_at',
                                           'proposal_hash', 'source_commit', 'before_hash',
                                           'after_hash', 'effective_contract_hash',
                                           'preservation_reason', 'evidence')}
                      for item in row['revisions']]}


def _prior_hashes(state):
    return {'packet_id': _evidence_hash(state),
            'acceptance': hashlib.sha256(json.dumps(state.get('acceptance'), sort_keys=True,
                                                    separators=(',', ':')).encode()).hexdigest(),
            'auto_check': hashlib.sha256(json.dumps(state.get('auto_check'), sort_keys=True,
                                                    separators=(',', ':')).encode()).hexdigest(),
            'commit': state.get('submitted_commit')}


def revise(b, sid, request_id, reason, user_authorization, event_id, check_id,
           before_program, after_program, preservation_reason, evidence,
           caller, apply=False, proposal_hash=None):
    try:
        arguments = _arguments(request_id, reason, user_authorization, event_id, check_id,
                               _program_hash(before_program), _program_hash(after_program),
                               preservation_reason, evidence)
        if (not isinstance(check_id, str) or not check_id
                or not isinstance(preservation_reason, str) or not preservation_reason.strip()
                or len(preservation_reason) > 4000
                or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000):
            raise ValueError('Provide check_id, a standard-preservation reason and evidence references')
        if type(apply) is not bool or apply and (not isinstance(proposal_hash, str)
                                                 or not re.fullmatch(r'[0-9a-f]{64}', proposal_hash)):
            raise ValueError('Apply requires the exact reviewed proposal_hash')
        with b.project_manager().lock(), b.locked(sid) as root, _no_active_probe(root):
            state, contract = b.load_session(root, read_only=not apply)
            owner = (state.get('owner') or {}).get('thread_id')
            if not owner or owner != caller:
                raise b.GateError('Check revision requires the original trusted supervisor',
                                  code='owner_mismatch')
            if state.get('project_id'):
                manager = b.project_manager(); project = manager.load(state['project_id'])
                manager.authorize(project, caller); manager.active(project)
            if state.get('continuation', 'active') != 'active' or state.get('archived'):
                raise ValueError('Honor paused, cancelled or archived task controls')
            if state['phase'] == 'accepted' or (state.get('acceptance') or {}).get('accepted'):
                raise ValueError('Accepted tasks cannot be revised')
            from supervisor import verify_owner
            verify_owner(b, state.get('owner'))
            row = state.get(KEY)
            if KEY in state and row is None:
                raise ValueError('Check revision record is explicitly null')
            if row is not None:
                prior = next((item for item in row['revisions']
                              if item['arguments']['request_id'] == arguments['request_id']), None)
                if prior:
                    if prior['arguments'] != arguments:
                        raise ValueError('This revision request_id was already recorded with different arguments')
                    if apply and proposal_hash != prior['proposal_hash']:
                        raise ValueError('Revision proposal changed; preview and review again')
                    return {'session_id': sid, 'duplicate': True, 'accepted': False,
                            'revision': public(state), 'replayed_seq': prior['seq'], 'next_action': 'telos_status'}
            if b.event_action(state, event_id, 'revise_checks', arguments):
                raise ValueError('Revision action exists without its record')
            for value in (reason, user_authorization, preservation_reason, evidence):
                b.deny_leak(value, contract)
            raw_contract = b.read_json(root / 'contract.json')
            candidate = _candidate(b, root, state, contract, raw_contract, check_id, before_program, after_program,
                                   arguments, preservation_reason, evidence)
            preview = {'check_id': check_id, 'before': before_program,
                       'after': after_program, 'diff': _diff(before_program, after_program),
                       'proposal_hash': candidate['proposal_hash'],
                       'prior_evidence_hashes': _prior_hashes(state),
                       'authorization': arguments, 'source_commit': candidate['source_commit']}
            if not apply:
                return {'session_id': sid, 'preview': True, 'accepted': False,
                        'revision': preview, 'next_action': 'review_proposal_then_apply'}
            if proposal_hash != candidate['proposal_hash']:
                raise ValueError('Revision proposal changed; preview and review again')
            seq = 1 if row is None else len(row['revisions']) + 1
            item = {'seq': seq, **{k: candidate[k] for k in (
                'check_id', 'before_program', 'after_program', 'before_hash', 'after_hash',
                'arguments', 'arguments_hash', 'source_commit', 'prior_evidence',
                'prior_evidence_hash', 'preservation_reason', 'evidence', 'project_id',
                'original_contract_hash', 'base_effective_hash',
                'previous_record_hash', 'legacy_record_hashes', 'source_binding',
                'effective_contract_hash', 'proposal_hash')}, 'created_at': time.time()}
            item['record_hash'] = b.digest(item)
            base = {'version': 1, 'kind': KIND, 'session_id': sid,
                    'owner_thread_id': owner,
                    'original_contract_hash': candidate['original_contract_hash'],
                    'revisions': ([] if row is None else row['revisions']) + [item]}
            base['record_hash'] = b.digest(base)
            state[KEY] = base
            state['contract_hash'] = candidate['effective_contract_hash']
            for key in ('packet_id', 'acceptance', 'auto_check'):
                state.pop(key, None)
            result = {'session_id': sid, 'accepted': False, 'event_id': event_id,
                      'revision': preview | {'seq': seq}, 'next_action': 'telos_evaluate'}
            b.record_action(state, event_id, 'revise_checks', arguments, result)
            b.write_json(root / 'state.json', state)
            return result
    except (ValueError, TypeError, KeyError, SyntaxError) as error:
        raise b.GateError(str(error)) from None

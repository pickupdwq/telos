"""One owner-reviewed pytest path correction; never a general contract editor."""
import ast
import copy
import hashlib
import math
from pathlib import Path
import re
import time

KEY = 'test_path_correction'
KIND = 'pytest-device-template-path-v1'
OLD = 'tests/test_device_import_preview.py'
NEW = 'tests/test_device_template.py'
APP = 'ai-device-risk'
RUNNER = '/private/tmp/ai-risk-gap-20261029-env/run.py'
MAX_RECOVERY = 5
PRIOR = {'phase', 'rounds', 'turns', 'packet_id', 'submitted_commit', 'last_result', 'acceptance', 'auto_check'}
CORE = {'version', 'kind', 'session_id', 'project_id', 'owner_thread_id', 'arguments',
        'arguments_hash', 'original_contract_hash', 'effective_contract_hash', 'change',
        'source_proof', 'recovery', 'prior_evidence'}


def _bindings(tree):
    bindings = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            bindings[statement.targets[0].id] = statement.value
    def single(name):
        return sum(isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == name
                   or isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
                   for n in ast.walk(tree)) == 1
    return {name: value for name, value in bindings.items() if single(name)}


def _unshadowed(tree, name, *, imported=False):
    aliases = [n for n in ast.walk(tree) if isinstance(n, ast.alias) and (n.asname or n.name.split('.')[0]) == name]
    imports = [n for statement in tree.body if isinstance(statement, ast.Import)
               for n in statement.names if n.name == name and n.asname is None]
    return (len(aliases) == (1 if imported else 0) and (not imported or len(imports) == 1)
            and not any(isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == name
                        or isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
                        for n in ast.walk(tree)))


def _literal(node, bindings):
    node = bindings.get(node.id) if isinstance(node, ast.Name) else node
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _root(node, bindings, tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'str' and len(node.args) == 1 and not node.keywords:
        return _unshadowed(tree, 'str') and _root(node.args[0], bindings, tree)
    node = bindings.get(node.id) if isinstance(node, ast.Name) else node
    return (isinstance(node, ast.Call) and not node.args and not node.keywords
            and ast.dump(node.func) == ast.dump(ast.parse('pathlib.Path.cwd()', mode='eval').body.func)
            and _unshadowed(tree, 'pathlib', imported=True))


def _application(node, bindings, tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'str' and len(node.args) == 1 and not node.keywords:
        return _unshadowed(tree, 'str') and _application(node.args[0], bindings, tree)
    return (isinstance(node, ast.Constant) and node.value == APP or
            isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and _root(node.left, bindings, tree) and isinstance(node.right, ast.Constant) and node.right.value == APP)


def _python(node, bindings, tree):
    value = _literal(node, bindings)
    return (bool(value and Path(value).is_absolute() and re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(value).name))
            or isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == 'sys' and node.attr == 'executable' and _unshadowed(tree, 'sys', imported=True))


def _static_program(tree):
    protected = {'subprocess', 'pathlib', 'sys', 'os'}
    reflection = {'exec', 'eval', 'compile', 'globals', 'locals', 'vars', 'getattr', 'setattr', 'delattr', '__import__',
                  '__getattribute__', 'chdir', 'fchdir'}
    def base(node):
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return False
        if isinstance(node, ast.Name) and node.id.startswith('__') or isinstance(node, ast.Attribute) and node.attr.startswith('__'):
            return False
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in reflection:
            return False
        if isinstance(node, ast.ImportFrom) and (node.module in ('builtins', 'importlib') or any(n.name in reflection for n in node.names)):
            return False
        if isinstance(node, ast.Import) and any(n.name in ('builtins', 'importlib', 'ctypes') for n in node.names):
            return False
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in reflection:
            return False
        if isinstance(node, ast.Attribute) and node.attr in reflection:
            return False
        if isinstance(node, (ast.Attribute, ast.Subscript)) and base(node) in protected:
            if isinstance(node.ctx, (ast.Store, ast.Del)) or isinstance(node, ast.Attribute) and node.attr.startswith('__'):
                return False
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in protected | reflection:
            return False
    return True


def corrected_contract(original):
    if original.get('task_type', 'implementation') != 'implementation':
        raise ValueError('Test path correction is for implementation tasks only')
    matches = []
    for check in original['checks']:
        if check['argv'][:2] != ['{python}', '-c'] or len(check['argv']) != 3:
            raise ValueError('Correction requires frozen inline Python checks')
        code = check['argv'][2]
        tree = ast.parse(code)
        if not _static_program(tree):
            continue
        for statement in tree.body:
            call = statement.value if isinstance(statement, (ast.Assign, ast.Expr)) else None
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == 'subprocess'
                    and call.func.attr in ('run', 'check_call') and _unshadowed(tree, 'subprocess', imported=True)
                    and len(call.args) == 1 and isinstance(call.args[0], ast.List)
                    and all(k.arg in {'cwd', 'check', 'capture_output', 'text', 'encoding', 'errors',
                                      'timeout', 'stdout', 'stderr'} for k in call.keywords)):
                continue
            if any(k.arg != 'cwd' and not isinstance(k.value, ast.Constant)
                   and not (k.arg in ('stdout', 'stderr') and isinstance(k.value, ast.Attribute)
                            and isinstance(k.value.value, ast.Name) and k.value.value.id == 'subprocess'
                            and k.value.attr in ('PIPE', 'STDOUT', 'DEVNULL')) for k in call.keywords):
                continue
            args = call.args[0].elts
            bindings = {name: value for name, value in _bindings(tree).items() if value.lineno < call.lineno}
            if len(args) < 4 or not _python(args[0], bindings, tree):
                continue
            cwd = next((k.value for k in call.keywords if k.arg == 'cwd'), None)
            runner = (len(args) >= 5 and _literal(args[1], bindings) == RUNNER
                      and _root(args[2], bindings, tree) and isinstance(args[3], ast.Constant) and args[3].value == 'pytest'
                      and (cwd is None or _root(cwd, bindings, tree)))
            standard = (isinstance(args[1], ast.Constant) and args[1].value == '-m'
                        and isinstance(args[2], ast.Constant) and args[2].value == 'pytest'
                        and _application(cwd, bindings, tree))
            if not (runner or standard):
                continue
            operands = args[4 if runner else 3:]
            if not all(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in operands):
                continue
            # This migration supports only an explicit test-file prefix followed
            # by display flags. An option value is never a test path: changing
            # --ignore OLD to --ignore NEW would weaken the frozen check.
            split = next((i for i, n in enumerate(operands) if n.value.startswith('-')), len(operands))
            files, flags = operands[:split], operands[split:]
            if (not files or any(not re.fullmatch(r'tests/[A-Za-z0-9_./-]+\.py', n.value)
                                 or '..' in Path(n.value).parts for n in files)
                    or any(n.value not in ('-q', '-v', '--disable-warnings') for n in flags)):
                continue
            for node in files:
                if node.value == OLD:
                    # Require the simple, contiguous literal used by this rule.
                    lines = code.splitlines(keepends=True)
                    start = sum(len(x.encode()) for x in lines[:node.lineno - 1]) + node.col_offset
                    end = sum(len(x.encode()) for x in lines[:node.end_lineno - 1]) + node.end_col_offset
                    raw = code.encode(); token = raw[start:end]
                    if token not in (b"'" + OLD.encode() + b"'", b'"' + OLD.encode() + b'"'):
                        continue
                    fixed = (raw[:start] + token[:1] + NEW.encode() + token[-1:] + raw[end:]).decode()
                    matches.append({'check_id': check['id'], 'before_program': code, 'after_program': fixed,
                                    'span': [start, end], 'invocation': 'fixed_runner' if runner else 'python_pytest'})
    if len(matches) != 1:
        raise ValueError('Exactly one statically supported fixed pytest path literal is required')
    change = matches[0]
    fixed = copy.deepcopy(original)
    next(c for c in fixed['checks'] if c['id'] == change['check_id'])['argv'][2] = change['after_program']
    return fixed, change


def source_proof(b, state, commit):
    baseline = state['base_commit']
    parents_checked, entries = set(), {}
    def entry(revision, name):
        if not isinstance(revision, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', revision):
            raise ValueError('A full immutable Git commit is required')
        if (revision, name) in entries:
            return entries[(revision, name)]
        if revision not in parents_checked:
            for parent in (APP, APP + '/tests'):
                parent_entry = b.git(state['repo'], 'ls-tree', revision, '--', parent)
                if not parent_entry.startswith('040000 tree ') or not parent_entry.endswith('\t' + parent):
                    raise ValueError('Test parent paths must be ordinary Git trees, never links')
            parents_checked.add(revision)
        text = b.git(state['repo'], 'ls-tree', revision, '--', APP + '/' + name)
        if not text:
            entries[(revision, name)] = None
            return None
        metadata, path = text.split('\t', 1)
        mode, kind, blob = metadata.split()
        if path != APP + '/' + name or mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('Replacement must be an ordinary tracked repository file')
        entries[(revision, name)] = {'mode': mode, 'blob': blob}
        return entries[(revision, name)]
    if entry(baseline, OLD) is not None or entry(commit, OLD) is not None:
        raise ValueError('The old test path exists; this is not the supported typo')
    first, latest = entry(baseline, NEW), entry(commit, NEW)
    if first is None or latest is None or first != latest:
        raise ValueError('Replacement test must already exist unchanged in baseline and submission')
    b.git(state['repo'], 'merge-base', '--is-ancestor', baseline, commit)
    return {'baseline': baseline, 'submission': commit, 'old_path': APP + '/' + OLD,
            'new_path': APP + '/' + NEW, **first}


def _attempt(b, root, state, original, change, number, proofs=None):
    path = root / f'attempt-{number}.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024**2:
        return None
    raw = path.read_bytes()
    import json
    packet = json.loads(raw)
    if not isinstance(packet, dict) or not isinstance(packet.get('exam'), dict):
        return None
    if (packet.get('session_id') != state['session_id'] or type(packet.get('round')) is not int or packet['round'] != number
            or packet.get('contract_hash') != b.digest(original)
            or packet['exam'] != {'skipped': 'executable_checks_failed'}):
        return None
    checks = packet.get('checks')
    if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks) or [c.get('id') for c in checks] != [c['id'] for c in original['checks']]:
        return None
    target = next(c for c in checks if c['id'] == change['check_id'])
    if (target.get('status') != 'failed' or type(target.get('returncode')) is not int or target['returncode'] not in (1, 4)
            or any(c.get('status') != 'passed' for c in checks if c['id'] != change['check_id'])):
        return None
    output = str(target.get('stdout', '')) + '\n' + str(target.get('stderr', ''))
    missing = re.search(r'(?m)^ERROR: file or directory not found: ' + re.escape(OLD) + r'\s*$', output)
    no_tests = re.search(r'(?m)^no tests ran in [0-9.]+s\s*$', output)
    exit_four = target['returncode'] == 4 or bool(re.search(r'(?:exit status|returncode[=:]?)\s*4\b', output))
    if not (missing and no_tests and exit_four):
        return None
    proofs = {} if proofs is None else proofs
    commit = packet.get('commit')
    if not isinstance(commit, str):
        raise ValueError('Historical attempt needs an immutable commit')
    if commit not in proofs:
        proofs[commit] = source_proof(b, state, commit)
    return {'round': number, 'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
            'packet_hash': b.digest(packet), 'commit': packet['commit']}


def _arguments(request_id, reason, user_authorization, event_id):
    values = dict(request_id=request_id, reason=reason, user_authorization=user_authorization, event_id=event_id)
    if (any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in values.values())
            or request_id != request_id.strip()):
        raise ValueError('Provide bounded request_id, reason, user_authorization and received event_id')
    return values


def _candidate(b, root, state, original, arguments):
    fixed, change = corrected_contract(original)
    if b.git(state['worktree'], '--no-optional-locks', 'status', '--porcelain'):
        raise ValueError('Submit a clean pinned worktree before correcting its check path')
    commit = b.git(state['worktree'], 'rev-parse', 'HEAD')
    proof = source_proof(b, state, commit)
    proofs = {commit: proof}
    if not 0 <= state['rounds'] <= original['max_rounds']:
        raise ValueError('Existing round count is inconsistent with the original limit')
    attempts = [row for n in range(1, state['rounds'] + 1)
                if (row := _attempt(b, root, state, original, change, n, proofs)) is not None]
    return {'version': 1, 'kind': KIND, 'session_id': state['session_id'], 'project_id': state.get('project_id'),
            'owner_thread_id': state['owner']['thread_id'], 'arguments': arguments,
            'arguments_hash': b.digest(arguments), 'original_contract_hash': b.digest(original),
            'effective_contract_hash': b.digest(fixed), 'change': change, 'source_proof': proof,
            'recovery': {'original_max_rounds': original['max_rounds'], 'rounds_at_correction': state['rounds'],
                         'eligible_attempts': attempts, 'granted': min(MAX_RECOVERY, len(attempts))},
            'prior_evidence': copy.deepcopy({k: state.get(k) for k in PRIOR})}


def effective(b, state, original):
    row = state[KEY]
    if 'audit_check_correction' in state or not isinstance(row, dict) or set(row) != CORE | {'proposal_hash', 'created_at', 'record_hash'}:
        raise ValueError('Invalid or conflicting test path correction record')
    core = {k: row[k] for k in CORE}
    if (type(row['version']) is not int or row['version'] != 1 or row['kind'] != KIND
            or row['session_id'] != state['session_id'] or row['project_id'] != state.get('project_id')
            or not row['owner_thread_id'] or row['owner_thread_id'] != (state.get('owner') or {}).get('thread_id')
            or type(row['created_at']) not in (int, float) or not math.isfinite(row['created_at']) or row['created_at'] <= 0
            or row['original_contract_hash'] != b.digest(original) or row['proposal_hash'] != b.digest(core)
            or row['record_hash'] != b.digest({k: v for k, v in row.items() if k != 'record_hash'})):
        raise ValueError('Test path correction identity or canonical digest changed')
    if not isinstance(row['arguments'], dict) or row['arguments'] != _arguments(**row['arguments']) or row['arguments_hash'] != b.digest(row['arguments']):
        raise ValueError('Invalid test path correction authorization arguments')
    event_id = row['arguments']['event_id']
    event = state.get('events', {}).get(event_id) or {}
    if not isinstance(event.get('actions'), dict):
        raise ValueError('Correction event has no valid retained actions')
    action = event['actions'].get('correct_test_path') or {}
    if not isinstance(action, dict) or not isinstance(action.get('result'), dict):
        raise ValueError('Correction retained action is damaged')
    result = action.get('result') or {}
    if not isinstance(result.get('correction'), dict):
        raise ValueError('Correction retained result is damaged')
    if (event.get('id') != event_id or action.get('arguments_hash') != row['arguments_hash']
            or action.get('action_id') != b.digest([event_id, 'correct_test_path', row['arguments']])
            or result.get('session_id') != state['session_id'] or result.get('event_id') != event_id
            or (result.get('correction') or {}).get('proposal_hash') != row['proposal_hash']):
        raise ValueError('Correction authorization no longer matches its retained event action')
    fixed, change = corrected_contract(original)
    if row['change'] != change or row['effective_contract_hash'] != b.digest(fixed) or state['contract_hash'] != b.digest(fixed):
        raise ValueError('Correction differs from its fixed AST rule')
    proof = row['source_proof']
    if not isinstance(proof, dict) or proof != source_proof(b, state, proof.get('submission')):
        raise ValueError('Correction source proof changed')
    proofs = {proof['submission']: proof}
    prior = row['prior_evidence']
    if (not isinstance(prior, dict) or set(prior) != PRIOR or prior['phase'] not in ('responded', 'not_accepted')
            or any(type(prior[k]) is not int or prior[k] < 0 for k in ('rounds', 'turns'))
            or any(prior[k] is not None and not isinstance(prior[k], str) for k in ('packet_id', 'submitted_commit'))
            or any(prior[k] is not None and not isinstance(prior[k], dict) for k in ('last_result', 'acceptance', 'auto_check'))):
        raise ValueError('Invalid correction prior evidence')
    recovery = row['recovery']
    if (not isinstance(recovery, dict) or set(recovery) != {'original_max_rounds', 'rounds_at_correction', 'eligible_attempts', 'granted'}
            or type(recovery['rounds_at_correction']) is not int or recovery['rounds_at_correction'] != prior['rounds']
            or not 0 <= prior['rounds'] <= original['max_rounds'] or state['rounds'] < prior['rounds']
            or type(recovery['original_max_rounds']) is not int or recovery['original_max_rounds'] != original['max_rounds']
            or type(recovery['granted']) is not int):
        raise ValueError('Invalid bounded check recovery policy')
    attempts = [item for n in range(1, prior['rounds'] + 1)
                if (item := _attempt(b, b.run_dir(state['session_id']), state, original, change, n, proofs)) is not None]
    if (recovery['eligible_attempts'] != attempts or recovery['granted'] != min(MAX_RECOVERY, len(attempts))
            or state['rounds'] > original['max_rounds'] + recovery['granted']):
        raise ValueError('Recovery does not match retained missing-path attempts')
    return fixed


def round_limit(state, contract):
    row = state.get(KEY)
    return contract['max_rounds'] + (row['recovery']['granted'] if row else 0)


def public(state):
    row = state.get(KEY)
    if row is None:
        return None
    recovery = row['recovery']; used = max(0, state['rounds'] - recovery['original_max_rounds'])
    return {k: row[k] for k in ('version', 'kind', 'session_id', 'project_id', 'owner_thread_id', 'arguments',
                               'original_contract_hash', 'effective_contract_hash', 'proposal_hash')} | {
        'change': {'check_id': row['change']['check_id'], 'old_path': OLD, 'new_path': NEW,
                   'invocation': row['change']['invocation']}, 'source_proof': row['source_proof'],
        'prior_evidence': {k: row['prior_evidence'][k] for k in ('phase', 'rounds', 'turns', 'packet_id', 'submitted_commit')},
        'recovery': {**recovery, 'original_remaining': max(0, recovery['original_max_rounds'] - state['rounds']),
                     'consumed': used, 'remaining': max(0, recovery['granted'] - used)}}


def correct(b, sid, request_id, reason, user_authorization, event_id, caller, apply=False, proposal_hash=None):
    try:
        arguments = _arguments(request_id, reason, user_authorization, event_id)
        if type(apply) is not bool or apply and (not isinstance(proposal_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', proposal_hash)):
            raise ValueError('Apply requires the exact reviewed proposal_hash')
        with b.project_manager().lock(), b.locked(sid) as root:
            state, contract = b.load_session(root)
            owner = (state.get('owner') or {}).get('thread_id')
            if not owner or owner != caller:
                raise b.GateError('Path correction requires the original trusted supervisor', code='owner_mismatch')
            if state.get('project_id'):
                manager = b.project_manager(); project = manager.load(state['project_id'])
                manager.authorize(project, caller); manager.active(project)
                if project['phase'] in ('verifying', 'awaiting_final_review'):
                    raise ValueError('Wait for project verification before correcting a task')
            if state.get('continuation', 'active') != 'active' or state.get('archived') or state['phase'] == 'accepted' or (state.get('acceptance') or {}).get('accepted'):
                raise ValueError('Honor paused, cancelled, archived or accepted task controls')
            from supervisor import verify_owner
            verify_owner(b, state.get('owner'))
            if KEY in state:
                old = state[KEY]
                if old['arguments'] != arguments or apply and proposal_hash != old['proposal_hash']:
                    raise ValueError('Correction already recorded; arguments cannot change')
                return {'session_id': sid, 'duplicate': True, 'accepted': False,
                        'correction': public(state), 'next_action': 'telos_status'}
            if ('audit_check_correction' in state or contract.get('task_type', 'implementation') != 'implementation'
                    or state['phase'] not in ('responded', 'not_accepted') or state.get('clarification')
                    or (state.get('last_result') or {}).get('exam_received')
                    or (state.get('auto_check') or {}).get('status') in ('queued', 'running')):
                raise ValueError('Completed unaccepted implementation without an active check or exam is required')
            if b.event_action(state, event_id, 'correct_test_path', arguments):
                raise ValueError('Correction action exists without its record')
            for value in (reason, user_authorization):
                b.deny_leak(value, contract)
            candidate = _candidate(b, root, state, contract, arguments)
            candidate['proposal_hash'] = b.digest(candidate)
            preview = public({**state, KEY: candidate})
            if not apply:
                return {'session_id': sid, 'preview': True, 'accepted': False, 'proposal_hash': candidate['proposal_hash'],
                        'correction': preview, 'next_action': 'review_proposal_then_apply'}
            if proposal_hash != candidate['proposal_hash']:
                raise ValueError('Correction proposal changed; preview and review again')
            candidate['created_at'] = time.time(); candidate['record_hash'] = b.digest(candidate)
            state[KEY] = candidate; state['contract_hash'] = candidate['effective_contract_hash']
            for key in ('packet_id', 'acceptance', 'auto_check'):
                state.pop(key, None)
            result = {'session_id': sid, 'accepted': False, 'correction': public(state),
                      'event_id': event_id, 'next_action': 'telos_evaluate'}
            b.record_action(state, event_id, 'correct_test_path', arguments, result)
            b.write_json(root / 'state.json', state)
            return result
    except (ValueError, TypeError, KeyError, SyntaxError) as error:
        raise b.GateError(str(error)) from None

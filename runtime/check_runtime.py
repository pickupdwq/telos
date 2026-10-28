"""Prepare a private check HOME from explicitly authorized dependency directories."""
from pathlib import Path
import shutil


def dependency_home(paths, original_home, check_home, *, links=False):
    """Mirror only declared home-relative dependencies, preserving symlinks.

    Check programs sometimes discover a tool under HOME (for example ~/.npm).
    They must see the declared tool without seeing the rest of the real home.
    Symlinks are copied as links, never followed while copying host files; the
    check sandbox still enforces their targets. Returned roots are write-denied.
    """
    selected = []
    roots = set()
    for value in sorted(paths, key=lambda p: (len(Path(p).parts), p)):
        source = Path(value)
        if any(source == parent or parent in source.parents for parent in selected):
            continue
        try:
            relative = source.relative_to(original_home)
        except ValueError:
            continue
        destination = check_home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if links:
            if destination.is_symlink() and destination.resolve() == source.resolve():
                pass
            elif destination.exists() or destination.is_symlink():
                raise ValueError('Declared dependency HOME path conflicts with existing workspace data')
            else:
                destination.symlink_to(source, target_is_directory=True)
        else:
            shutil.copytree(source, destination, symlinks=True)
        selected.append(source)
        roots.add(check_home / relative.parts[0])
    return sorted(roots)


# Check execution is an existing evaluation stage, never a model request or an
# acceptance decision. Records live in the supervisor-only session directory.
import contextlib
import hashlib
import json
import os
import platform
import sys
import time

from serialization import digest
from providers import ProviderError
import progress_guard


def freeze(value, config):
    value = config.get('auto_run_checks', False) if value is None else value
    if type(value) is not bool:
        raise ProviderError('auto_run_checks must be a boolean')
    return value


def validate_freshness(check):
    value = check.get('freshness', {'mode': 'never'})
    if not isinstance(value, dict) or set(value) - {'mode', 'max_age_seconds'}:
        raise ProviderError('Invalid check freshness declaration')
    mode = value.get('mode')
    if mode not in ('never', 'snapshot', 'max_age'):
        raise ProviderError('Check freshness mode must be never, snapshot or max_age')
    if mode == 'max_age':
        if type(value.get('max_age_seconds')) is not int or not 1 <= value['max_age_seconds'] <= 3600:
            raise ProviderError('max_age freshness requires 1-3600 max_age_seconds')
    elif 'max_age_seconds' in value:
        raise ProviderError('Only max_age freshness accepts max_age_seconds')
    if mode == 'snapshot' and check.get('network'):
        raise ProviderError('Network checks require max_age or never freshness')
    return value


def runner_identity(b):
    names = ('telos.py', 'check_runtime.py', 'project_environment.py', 'preflight.py',
             'isolation.py', 'processes.py', 'serialization.py', 'identity.py')
    return {'files': {n: hashlib.sha256((b.INSTALL / n).read_bytes()).hexdigest() for n in names},
            'python': [sys.executable, sys.version, hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()],
            'platform': [sys.platform, platform.machine(), platform.release()], 'version': b.VERSION,
            'sandbox_base': digest(b.sandbox_profile()), 'install': str(b.INSTALL), 'home': str(b.HOME)}


def environment_identity(b, contract):
    """Content identities, not mtime-only shortcuts. Bounded scans fail closed.

    A snapshot freshness declaration asserts deterministic use of these declared
    inputs. Undeclared/time-dependent inputs must use max_age or never instead.
    A dependency may link only to another explicitly declared dependency path;
    such links are hashed through their target content, while any undeclared
    escape still blocks reuse. Source/output trees keep the strict rules.
    """
    import project_environment as pe
    req = contract.get('environment_requirements') or {}
    dependencies = b.validate_dependencies(contract.get('dependency_paths', []))
    if dependencies != contract.get('dependency_paths', []):
        raise ProviderError('Frozen dependency paths were redirected')
    paths = set(dependencies + req.get('download_caches', []))
    paths.update(t['path'] for t in req.get('tools', []))
    browser = req.get('browser') or {}
    if 'checks' in browser.get('phases', ['work', 'checks']):
        paths.update(browser[k] for k in ('node', 'module', 'executable') if k in browser)
    identities = {}
    declared = sorted(paths)
    for name in sorted(paths):
        path = Path(name)
        resolved = path.resolve(strict=True)
        identities[name] = [str(resolved), declared_tree_hash(resolved, declared)]
    env = b.minimal_env()
    env.pop('TMPDIR', None)  # Always replaced by a unique private check scratch.
    return {'requirements': req, 'dependencies': identities, 'environment': env,
            'permissions': {'source_read_only': True, 'scratch_only_writes': True,
                'network': [bool(c.get('network', False)) for c in contract['checks']],
                'services': pe.service_ports(contract, 'checks'), 'local_ipc': bool(req.get('local_ipc'))}}


def declared_tree_hash(path, declared):
    """Hash one declared dependency, following links only to declared paths.

    Symlinks are never trusted by their text: a link that resolves inside any
    explicitly declared dependency roots contributes both its raw link text and
    the content identity of its target, so relinking or editing the target
    invalidates reuse. Undeclared targets, dangling links and link cycles fail
    closed with the same bounded scan budget as strict artifact trees.
    """
    import hashlib
    import os
    import stat
    import time
    import project_environment as pe

    roots = []
    for item in declared:
        candidate = Path(item).resolve()
        if candidate not in roots:
            roots.append(candidate)
    budget = {'files': 0, 'bytes': 0}
    deadline = time.monotonic() + 30
    active = []

    def tick():
        budget['files'] += 1
        if budget['files'] > 100000 or time.monotonic() > deadline:
            raise pe.EnvironmentBlocked('integrity', 'Artifact tree exceeds bounded integrity scan')

    def node(target_path):
        tick()
        if target_path.is_symlink():
            text = os.readlink(target_path)
            try:
                target = target_path.resolve(strict=True)
            except OSError:
                raise pe.EnvironmentBlocked('integrity', 'Dependency link target is missing') from None
            except RuntimeError:
                raise pe.EnvironmentBlocked('integrity', 'Dependency link cycle is not verifiable') from None
            if not any(target == root or target.is_relative_to(root) for root in roots):
                raise pe.EnvironmentBlocked('integrity', 'Output link points outside its declared artifact tree')
            if any(open_root == target or open_root.is_relative_to(target) for open_root in active):
                raise pe.EnvironmentBlocked('integrity', 'Dependency link cycle is not verifiable')
            return ['link', text, str(target), scan(target)]
        mode = stat.S_IMODE(target_path.stat().st_mode)
        if target_path.is_dir():
            return ['directory', mode]
        if not target_path.is_file():
            raise pe.EnvironmentBlocked('integrity', 'Artifact contains an unsupported special file')
        h = hashlib.sha256()
        with target_path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                budget['bytes'] += len(chunk)
                if budget['bytes'] > 4 * 1024**3 or time.monotonic() > deadline:
                    raise pe.EnvironmentBlocked('integrity', 'Artifact tree exceeds bounded integrity scan')
                h.update(chunk)
        return ['file', mode, h.hexdigest()]

    def scan(root_path):
        root_path = Path(root_path)
        first = node(root_path)
        if root_path.is_symlink() or not root_path.is_dir():
            return pe.digest(first)
        rows = [['.', first]]
        active.append(root_path)
        try:
            for base, dirs, files in os.walk(root_path, followlinks=False):
                dirs.sort()
                for name in sorted(dirs + files):
                    item = Path(base) / name
                    rows.append([str(item.relative_to(root_path)), node(item)])
        finally:
            active.pop()
        return pe.digest(rows)

    return scan(Path(path))


def identity(b, state, contract, commit):
    return {'session_id': state['session_id'], 'contract_hash': state['contract_hash'],
            'commit': commit, 'tree': b.git(state['worktree'], 'rev-parse', commit + '^{tree}'),
            'checks_hash': digest(contract['checks']), 'runner': runner_identity(b),
            'environment': environment_identity(b, contract)}


def pending(state):
    row = state.get('auto_check') or {}
    return row.get('source_job_id') if row.get('status') == 'queued' else None


def prepare(b, state, contract):
    receipt = state.get('delivery') or {}
    if (contract.get('auto_run_checks') is not True or contract.get('work_reply_version') != 2
            or contract.get('task_type', 'implementation') != 'implementation'
            or state['phase'] != 'responded' or receipt.get('status') != 'completed'
            or receipt.get('job_id') != state.get('job_id') or state.get('clarification')):
        return
    source = state['job_id']
    if source in state.get('auto_check_history', {}):
        return
    now = time.time()
    row = {'source_job_id': source, 'delivery_id': receipt['delivery_id'],
           'contract_hash': state['contract_hash'], 'status': 'queued', 'queued_at': now}
    state['auto_check'] = row
    state.setdefault('auto_check_history', {})[source] = row
    import timing
    timing.mark(state, 'delivery_completed', source, at=state.get('job_finished', now))
    timing.mark(state, 'auto_checks_queued', source, at=now)
    timing.begin(state, 'environment_preparation', 'pin:' + source, at=now)
    try:
        if state.get('continuation', 'active') != 'active' or progress_guard.blocked(state):
            raise ProviderError('Automatic checks stopped by user control or no-progress guard')
        commit, tree = b.snapshot(state, 'auto-' + source)
        row.update(commit=commit, snapshot=str(tree), tree=b.git(state['worktree'], 'rev-parse', commit+'^{tree}'),
                   snapshot_hash=__import__('project_environment')._tree_hash(tree, confined=tree))
        timing.end(state, 'pin:' + source)
        timing.begin(state, 'scheduler_wait', 'checks:' + source + ':control:' + str(state.get('control_revision', 0)))
    except Exception as error:
        row.update(status='blocked', diagnostic=str(error)[:1000], finished_at=time.time())
        timing.end(state, 'pin:' + source, outcome='blocked')
        timing.end(state, 'checks:' + source, outcome='blocked')


def authorize(b, state, contract, source):
    row = state.get('auto_check') or {}
    if (pending(state) != source or state.get('job_id') != source or row.get('contract_hash') != state['contract_hash']
            or row.get('delivery_id') != (state.get('delivery') or {}).get('delivery_id')
            or contract.get('auto_run_checks') is not True or state['phase'] != 'responded'
            or state.get('continuation', 'active') != 'active' or state.get('archived')):
        raise ProviderError('Automatic check identity is stale or stopped', code='checks_stopped', next_action='inspect_status_before_retry')
    progress_guard.check(state)
    source_matches(b, state, row['commit'])
    return row


def source_matches(b, state, commit):
    if b.git(state['worktree'], 'rev-parse', 'HEAD') != commit or b.git(state['worktree'], 'status', '--porcelain'):
        raise ProviderError('Source changed after frozen checks; inspect and submit the new work',
                            code='check_evidence_stale', next_action='inspect_then_send_public_repair')


def read_evidence(b, state, contract, *, freshness=True, expected=None):
    row = state.get('auto_check') or {}
    if row.get('status') != 'passed' or not row.get('evidence_file'):
        raise ProviderError('Automatic checks have no successful complete evidence', code='checks_not_passed',
                            next_action='inspect_checks_then_send_public_repair')
    try:
        evidence = b.read_json(b.run_dir(state['session_id']) / row['evidence_file'])
        if (digest(evidence) != row['evidence_hash'] or expected is not None and row['evidence_hash'] != expected
                or evidence['attempt_id'] != row['job_id'] or evidence['source_job_id'] != row['source_job_id']
                or evidence['delivery_id'] != row['delivery_id'] or evidence['round'] != row['round']
                or evidence['identity'] != identity(b, state, contract, row['commit'])
                or evidence.get('complete') is not True or not evidence['checks']
                or [c['id'] for c in evidence['checks']] != [c['id'] for c in contract['checks']]
                or any(c.get('status') != 'passed' or c.get('source_read_only') is not True
                       or not c.get('sandbox_hash') or c.get('output_incomplete') for c in evidence['checks'])):
            raise ValueError('Evidence identity/integrity mismatch')
        source_matches(b, state, evidence['commit'])
        if freshness:
            age = time.time() - evidence['finished_at']
            req = contract.get('environment_requirements') or {}
            volatile = bool(req.get('local_ipc') or any('checks' in x.get('phases', ['work','checks'])
                for key in ('services','registries') for x in req.get(key, [])))
            for check in contract['checks']:
                rule = validate_freshness(check)
                if (age < 0 or rule['mode'] == 'never' or volatile and rule['mode'] == 'snapshot'
                        or rule['mode'] == 'max_age' and age > rule['max_age_seconds']):
                    raise ProviderError('Check freshness is absent, expired or cannot cover external state',
                                        code='check_freshness_required', next_action='telos_evaluate')
        return evidence
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProviderError('Check evidence cannot be reused: ' + str(error)[:400],
                            code='check_evidence_invalid', next_action='inspect_then_send_public_repair') from None


def evaluate_gate(b, state, contract):
    row = state.get('auto_check')
    if not row or (state.get('delivery') or {}).get('delivery_id') != row.get('delivery_id'):
        return None
    if row['status'] in ('queued', 'running'):
        return {'session_id': state['session_id'], 'job_id': row.get('job_id'), 'phase': state['phase'],
                'accepted': False, 'next_action': 'await_checks_completion', 'automatic_checks': public(state)}
    reuse_mode(b, state, contract)
    return None


def reuse_mode(b, state, contract):
    read_evidence(b, state, contract, freshness=False)
    try:
        read_evidence(b, state, contract)
        return 'reuse'
    except ProviderError as error:
        if error.code != 'check_freshness_required':
            raise
        if state['auto_check'].get('recheck_used'):
            raise ProviderError('Freshness recheck already used for this delivery; inspect and submit new work',
                                code='checks_retry_exhausted', next_action='inspect_then_send_public_repair')
        return 'recheck'


def public(state):
    row = state.get('auto_check')
    if not row:
        return None
    return {k: row.get(k) for k in ('status','source_job_id','job_id','commit','tree','round','queued_at',
                'started_at','finished_at','evidence_hash','diagnostic','evaluation_job_id') if k in row}


def ensure_active(b, state):
    with b.locked(state['session_id']) as root:
        current, _ = b.load_session(root)
        if (current.get('job_id') != state['job_id'] or current.get('continuation', 'active') != 'active'
                or current.get('control_revision', 0) != state.get('control_revision', 0)):
            raise ProviderError('Check execution stopped by control or changed job identity', code='checks_stopped')
        progress_guard.check(current)


def recover(b, state, contract):
    """Reconcile a completed durable result; never replay an uncertain program."""
    row = state.get('auto_check')
    if not row or state.get('job_kind') != 'checks' or row.get('job_id') != state.get('job_id'):
        return False
    path = b.run_dir(state['session_id']) / ('job-' + row['job_id'] + '-checks.json')
    # A hash committed into state is required. A file alone after a crash cannot
    # establish ownership/completeness and is retained for manual diagnosis.
    if row.get('evidence_hash') and path.exists():
        try:
            evidence = b.read_json(path)
            if digest(evidence) == row['evidence_hash']:
                finish(b, state, evidence)
                return True
        except (ValueError, KeyError, OSError):
            pass
    row.update(status='uncertain', finished_at=time.time(),
               diagnostic='Check worker disappeared without a committed result; no automatic replay')
    state['auto_check_history'][row['source_job_id']] = dict(row)
    return False


def finish(b, state, evidence):
    row = state['auto_check']
    passed = evidence.get('complete') is True and bool(evidence.get('checks')) and all(
        c['status'] == 'passed' and not c.get('output_incomplete') for c in evidence['checks'])
    row.update(status='passed' if passed else 'failed', finished_at=evidence['finished_at'],
               evidence_file='job-' + row['job_id'] + '-checks.json', evidence_hash=digest(evidence))
    state['auto_check_history'][row['source_job_id']] = dict(row)
    state.update(phase='responded' if passed else 'not_accepted', job_finished=evidence['finished_at'])


import threading
_CONTEXT = threading.local()


@contextlib.contextmanager
def execution_context(b, state):
    previous = getattr(_CONTEXT, 'value', None)
    _CONTEXT.value = (b, state)
    try:
        ensure_active(b, state)
        yield
    finally:
        _CONTEXT.value = previous


def checkpoint():
    value = getattr(_CONTEXT, 'value', None)
    if value:
        ensure_active(*value)


def observe(operation, *args, **kwargs):
    value = getattr(_CONTEXT, 'value', None)
    if value:
        import timing
        timing.record(*value, operation, *args, **kwargs)


def run(b, state, contract):
    """Run the frozen local stage using the existing check sandbox and scratch."""
    import project_environment as pe
    row = state['auto_check']
    source_matches(b, state, row['commit'])
    tree = Path(row['snapshot'])
    original = pe._tree_hash(tree, confined=tree)
    if original != row['snapshot_hash']:
        raise ProviderError('Frozen snapshot was modified before checks', code='check_inputs_changed')
    inputs = identity(b, state, contract, row['commit'])
    with execution_context(b, state):
        checks = b.executable_checks(contract, tree)
        checkpoint()
    source_matches(b, state, row['commit'])
    if identity(b, state, contract, row['commit']) != inputs or pe._tree_hash(tree, confined=tree) != original:
        raise ProviderError('Source, runner or environment changed during checks; result is invalid', code='check_inputs_changed')
    evidence = {'version': 1, 'attempt_id': row['job_id'], 'job_id': row['job_id'],
        'source_job_id': row['source_job_id'], 'delivery_id': row['delivery_id'],
        'round': row['round'], 'commit': row['commit'], 'contract_hash': state['contract_hash'],
        'identity': inputs, 'snapshot_hash': original, 'checks': checks,
        'started_at': row['started_at'], 'finished_at': time.time(), 'complete': True}
    root = b.run_dir(state['session_id'])
    b.write_json(root / ('job-' + row['job_id'] + '-checks.json'), evidence)
    b.write_json(root / ('attempt-' + str(row['round']) + '-checks.json'), evidence)
    # Commit the proof before the final phase update: recovery can finish the
    # state transition after a crash, but cannot re-execute the check program.
    with b.locked(state['session_id']) as root:
        current, _ = b.load_session(root)
        if current.get('job_id') != row['job_id']:
            raise ProviderError('Check job changed before result persistence')
        current['auto_check'].update(evidence_file='job-' + row['job_id'] + '-checks.json',
                                     evidence_hash=digest(evidence))
        b.write_json(root / 'state.json', current)
    return evidence


def packet_reference(state, reused):
    row = state['auto_check']
    return {'attempt_id': row['job_id'], 'source_job_id': row['source_job_id'],
            'evidence_hash': row['evidence_hash'], 'reused': reused}


def verify_packet(b, state, contract, packet):
    ref = packet.get('check_evidence')
    if ref is None:
        if contract.get('auto_run_checks') and state.get('auto_check') and packet.get('exam', {}).get('skipped') is None:
            raise ProviderError('Automatic-check packet is missing its evidence identity')
        return
    evidence = read_evidence(b, state, contract, expected=ref['evidence_hash'], freshness=ref['reused'] or any(c.get('freshness', {}).get('mode') == 'max_age' for c in contract['checks']))
    row = state['auto_check']
    if (ref['attempt_id'] != row['job_id'] or ref['source_job_id'] != row['source_job_id']
            or packet['commit'] != evidence['commit'] or packet['checks'] != evidence['checks']
            or packet['round'] != state['rounds'] or packet['round'] < evidence['round']):
        raise ProviderError('Packet is not bound to the successful check attempt')


def observation_id(suffix):
    value = getattr(_CONTEXT, 'value', None)
    return value[1]['job_id'] + ':' + suffix if value else suffix


@contextlib.contextmanager
def launch_permission():
    value = getattr(_CONTEXT, 'value', None)
    if not value:
        yield
        return
    b, state = value
    with b.locked(state['session_id']) as root:
        current, _ = b.load_session(root)
        if (current.get('job_id') != state['job_id'] or current.get('continuation', 'active') != 'active'
                or current.get('control_revision', 0) != state.get('control_revision', 0)):
            raise ProviderError('Checks stopped before command launch', code='checks_stopped')
        progress_guard.check(current)
        yield


def verify_retained(b, state, packet):
    ref = packet.get('check_evidence')
    if ref:
        path = b.run_dir(state['session_id']) / ('job-' + ref['attempt_id'] + '-checks.json')
        if digest(b.read_json(path)) != ref['evidence_hash']:
            raise ProviderError('Accepted check evidence changed; acceptance is invalid')

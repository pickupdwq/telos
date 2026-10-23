"""Opt-in, serial, manually supervised comparisons of frozen Telos runtimes.

No model client, automatic grader, MCP registration or installation upgrade lives
here. Each trial uses the selected runtime's existing manual task/exam/review
gate in a separate process, repository, state directory and executor home.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid

SIDES = ('baseline', 'candidate')
READ_TOOLS = {'telos_status', 'telos_review'}
TOOLS = READ_TOOLS | {'telos_receive', 'telos_send', 'telos_repair',
                      'telos_evaluate', 'telos_grade'}
PAID_TOOLS = {'telos_send', 'telos_repair', 'telos_evaluate'}
TERMINAL = {'passed', 'failed', 'cancelled'}
SOURCE = Path(__file__).resolve().parent
WORK_MESSAGE = 'Complete the frozen task within its scope. Run relevant checks and report actual results and limitations.'


class ComparisonError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(tmp, path)


def integer(value, label, low=1, high=10000000):
    if type(value) is not int or not low <= value <= high:
        raise ComparisonError(f'{label} must be an integer in {low}..{high}')
    return value


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or Path(value).is_absolute() or any(p in ('', '.', '..') for p in value.split('/'))
            or '.git' in Path(value).parts):
        raise ComparisonError('Expected a plain relative fixture path')
    return value


def tree_hashes(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        if '__pycache__' in path.parts:
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ComparisonError('Runtime snapshots cannot contain symlinks or special files')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def snapshot_runtime(source, destination, cfg):
    source = Path(source).expanduser().resolve()
    if (source / 'runtime/telos.py').is_file():
        source = source / 'runtime'
    if not (source / 'telos.py').is_file():
        raise ComparisonError('Runtime needs telos.py: ' + str(source))
    destination.mkdir(parents=True, mode=0o700)
    for path in sorted(source.iterdir()):
        # Installed credentials, manifests, caches, reports and launchers are
        # not runtime source. Both sides use the same dedicated Codex CLI.
        if path.suffix not in ('.py', '.cjs') and path.name not in ('deepseek_models.json', 'bailian_models.json'):
            continue
        if path.is_symlink() or not path.is_file():
            raise ComparisonError('Runtime source must consist of regular files')
        shutil.copyfile(path, destination / path.name)
    write(destination / 'config.json', cfg)
    hashes = tree_hashes(destination)
    return {'source': str(source), 'files': hashes, 'sha256': digest(hashes)}


def git(repo, *args):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                             '-C', str(repo), *args], env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ComparisonError('Git failed: ' + result.stderr[-700:])
    return result.stdout.strip()


def make_repo(root, files):
    root.mkdir(parents=True, mode=0o700)
    git(root, 'init', '-q')
    for name, content in files.items():
        path = root / relative(name)
        if not isinstance(content, str):
            raise ComparisonError('Fixture contents must be text')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Telos Evaluation', '-c', 'user.email=evaluation@localhost',
        'commit', '-qm', 'Frozen evaluation fixture')
    return git(root, 'rev-parse', 'HEAD')


def no_inline_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in ('api_key', 'apikey', 'authorization', 'password', 'token', 'secret'):
                raise ComparisonError('Configuration must reference credentials, not embed them')
            no_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_inline_secrets(item)
    elif isinstance(value, str) and (re.search(r'\bsk-[A-Za-z0-9_-]{16,}', value)
                                     or re.search(r'https?://[^/\s]+@', value)):
        raise ComparisonError('Do not embed API keys or URL credentials in comparison configuration')


def validate_cases(cases):
    import telos as b
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ComparisonError('Provide 1..100 fixed cases')
    seen = set()
    for case in cases:
        allowed = {'id', 'category', 'split', 'files', 'task', 'questions', 'checks',
                   'allowed_paths', 'audit', 'context', 'expected_findings'}
        required = {'id', 'category', 'split', 'files', 'task', 'questions', 'checks'}
        if not isinstance(case, dict) or set(case) - allowed or not required <= set(case):
            raise ComparisonError('Missing or unknown case fields')
        ident = case.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9_-]{1,48}', ident) or ident in seen:
            raise ComparisonError('Case IDs must be unique plain identifiers')
        seen.add(ident)
        if case.get('split') not in ('development', 'validation', 'holdout'):
            raise ComparisonError('Each case must name its split')
        if not isinstance(case.get('category'), str) or not case['category'].strip():
            raise ComparisonError('Each case needs a category')
        if not isinstance(case.get('task'), str) or not case['task'].strip():
            raise ComparisonError('Nonempty case task required')
        if not isinstance(case.get('files'), dict) or not case['files']:
            raise ComparisonError('Each case needs frozen text files')
        for name in case['files']:
            relative(name)
            if not isinstance(case['files'][name], str):
                raise ComparisonError('Fixture contents must be text')
        b.validate_exam(case['questions'], case['checks'], 90, 2)
        if any(c.get('network', False) for c in case['checks']):
            raise ComparisonError('Comparison checks cannot access the network')
        if 'audit' in case:
            import audit
            audit.validate(case['audit'])
            if case.get('allowed_paths') or case['audit']['targets'] or case['audit'].get('browser'):
                raise ComparisonError('Initial comparison suites support offline code audits only')
            expected = case.get('expected_findings')
            ids = {x['id'] for x in case['audit']['checklist']}
            if (not isinstance(expected, list) or not all(isinstance(x, str) for x in expected)
                    or not set(expected) <= ids or len(expected) != len(set(expected))):
                raise ComparisonError('Audit oracle must name distinct checklist IDs')
        else:
            if not case.get('allowed_paths'):
                raise ComparisonError('Implementation cases require explicit write paths')
            b.project_manager().scope(case['allowed_paths'])


def initialize(root, spec):
    import telos as b
    from evaluation_suite import cases as builtin_cases
    from usage import validate_budget
    root = Path(root).expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise ComparisonError('Use a new experiment directory; existing data was preserved')
    allowed = {'baseline', 'candidate', 'config', 'executor_profile', 'provider', 'model',
               'reasoning_effort', 'cases', 'case_ids', 'repeats', 'budget', 'max_rounds',
               'upstream_revision', 'label'}
    if not isinstance(spec, dict) or set(spec) - allowed or not {'baseline', 'candidate', 'config'} <= set(spec):
        raise ComparisonError('Missing or unknown comparison specification fields')
    cfg = read(Path(spec['config']).expanduser())
    if not isinstance(cfg, dict):
        raise ComparisonError('Configuration must be a JSON object')
    no_inline_secrets(cfg)
    # A single immutable configuration is shared by both runtime snapshots.
    # Experimental experiences can never influence another trial.
    cfg.update(experience_enabled=False)
    selection = {k: spec[k] for k in ('executor_profile', 'provider', 'model', 'reasoning_effort') if k in spec}
    binding = b.providers.resolve_executor({**b.DEFAULT, **cfg}, **selection)
    repeats = integer(spec.get('repeats', 1), 'repeats', high=10)
    rounds = integer(spec.get('max_rounds', 2), 'max_rounds', high=5)
    budget = validate_budget(spec.get('budget'))
    if not budget or 'max_requests' not in budget:
        raise ComparisonError('Every trial requires an explicit max_requests budget')
    all_cases = spec.get('cases', builtin_cases())
    validate_cases(all_cases)
    if 'case_ids' in spec:
        selected = spec['case_ids']
        if (not isinstance(selected, list) or not selected or not all(isinstance(x, str) for x in selected)
                or len(set(selected)) != len(selected)
                or not set(selected) <= {x['id'] for x in all_cases}):
            raise ComparisonError('case_ids must select distinct known cases')
        all_cases = [c for c in all_cases if c['id'] in selected]
    # No network or model request is made by init, next, report or control.
    root.mkdir(parents=True, mode=0o700)
    try:
        snapshots = {side: snapshot_runtime(spec[side], root / 'versions' / side, cfg) for side in SIDES}
        case_records = []
        for case in all_cases:
            sha = make_repo(root / 'sources' / case['id'], case['files'])
            case_records.append({**case, 'base_commit': sha})
        trials = []
        for repetition in range(repeats):
            for index, case in enumerate(case_records):
                # Adjacent paired trials; counterbalance order across cases and repeats.
                for side in SIDES[::1 if (index + repetition) % 2 == 0 else -1]:
                    trials.append({'id': f'{case["id"]}-r{repetition + 1}-{side}',
                                   'case_id': case['id'], 'repeat': repetition + 1, 'side': side})
        manifest = {'schema_version': 1, 'label': spec.get('label', 'Telos comparison'),
                    'created_at': time.time(), 'versions': snapshots, 'cases': case_records,
                    'trials': trials, 'selection': selection, 'executor': binding, 'budget': budget,
                    'max_rounds': rounds, 'repeats': repeats, 'initial_message': WORK_MESSAGE,
                    'upstream_revision': spec.get('upstream_revision'),
                    'environment': {'python': sys.version, 'platform': platform.platform()},
                    'request_ceiling': len(trials) * budget['max_requests']}
        # Check both versions can resolve exactly the same binding before a paid trial.
        write(root / 'manifest.json', manifest)
        write(root / 'progress.json', {'manifest_hash': digest(manifest), 'continuation': 'active',
                                       'active_trial': None, 'trials': {}})
        for side in SIDES:
            selected = worker(root, {'side': side}, 'binding', {})
            if selected['executor'] != binding:
                raise ComparisonError('Runtime versions resolve different executor bindings')
            manifest['versions'][side]['version'] = selected['runtime_version']
        write(root / 'manifest.json', manifest)
        write(root / 'progress.json', {'manifest_hash': digest(manifest), 'continuation': 'active',
                                       'active_trial': None, 'trials': {}})
        return {'run': str(root), 'trials': len(trials), 'manifest_hash': digest(manifest),
                'executor': b.providers.public_binding(binding), 'request_ceiling': manifest['request_ceiling'],
                'model_requests_sent': 0, 'next': 'compare next --run PATH; compare start --run PATH --live'}
    except Exception:
        # Only remove the new directory created by this failed init.
        shutil.rmtree(root)
        raise


@contextlib.contextmanager
def locked(root):
    root = Path(root).expanduser().resolve()
    if not (root / 'manifest.json').is_file():
        raise ComparisonError('Unknown experiment directory')
    with (root / 'lock').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        manifest, progress = read(root / 'manifest.json'), read(root / 'progress.json')
        if digest(manifest) != progress['manifest_hash']:
            raise ComparisonError('Frozen experiment manifest changed')
        yield root, manifest, progress


def verify_runtime(root, manifest, side):
    if tree_hashes(root / 'versions' / side) != manifest['versions'][side]['files']:
        raise ComparisonError('Frozen runtime or configuration changed')


def worker(root, trial, operation, arguments):
    """Separate interpreter prevents candidate imports/globals contaminating the host."""
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    request = {'root': str(root), 'trial': trial, 'operation': operation, 'arguments': arguments}
    result = subprocess.run([sys.executable, '-I', str(SOURCE / 'comparison.py'), '_worker'],
                            input=json.dumps(request), env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ComparisonError('Isolated runtime failed: ' + result.stderr[-1200:])
    reply = json.loads(result.stdout)
    if 'error' in reply:
        raise ComparisonError(reply['error'])
    return reply['result']


def _worker():
    request = json.load(sys.stdin)
    root, trial = Path(request['root']), request['trial']
    trial_root = root / 'trials' / trial.get('id', '_preflight')
    os.environ['TELOS_STATE_DIR'] = str(trial_root / 'state')
    os.environ['TELOS_CACHE_DIR'] = str(trial_root / 'cache')
    sys.path.insert(0, str(root / 'versions' / trial['side']))
    import telos as b
    operation, args = request['operation'], request['arguments']
    if operation == 'binding':
        return {'executor': b.providers.resolve_executor(b.config(), **read(root / 'manifest.json')['selection']),
                'runtime_version': b.VERSION}
    if operation == 'measure':
        sid = args['session_id']
        status = b.status(sid)
        state, contract = b.load_session(b.run_dir(sid))
        ledger = b.usage_ledger(state, contract)
        snapshot = ledger.snapshot()
        raw_path = b.usage_path(sid)
        raw = b.read_json(raw_path) if raw_path.exists() else {'requests': {}}
        requests = list(raw['requests'].values())
        latencies = [x.get('latency_ms') for x in requests]
        return {'accepted': status.get('accepted', False), 'phase': status['phase'],
                'continuation': state.get('continuation'), 'worktree': state['worktree'],
                'commit': b.git(state['worktree'], 'rev-parse', 'HEAD'),
                'dirty': bool(b.git(state['worktree'], 'status', '--porcelain')),
                'contract': contract, 'contract_hash': state['contract_hash'],
                'usage': snapshot, 'turns': state['turns'], 'rounds': state['rounds'],
                'audit_review': (state.get('acceptance') or {}).get('audit_review'),
                'provider_seconds': round(sum(latencies) / 1000, 3) if all(isinstance(n, (int, float)) for n in latencies) else None,
                # status() has just verified the packet/verdict/commit binding.
                # review_packet intentionally permits only awaiting_review.
                'evidence': b.read_json(b.run_dir(sid) / f'attempt-{state["rounds"]}.json') if status.get('accepted') else None}
    if operation == 'telos_open':
        schema = next(tool['inputSchema'] for tool in b.TOOLS if tool['name'] == operation)
        if 'skip_meeting_reason' in schema['properties']:
            # This explicit experiment compares the same frozen single-executor
            # cases on both releases; planning calls would change its subject.
            args['skip_meeting_reason'] = 'Controlled comparison uses the preplanned frozen case on both releases.'
    return b.call_tool(operation, args)


def select_trial(manifest, progress):
    if progress['active_trial']:
        return next(t for t in manifest['trials'] if t['id'] == progress['active_trial'])
    return next((t for t in manifest['trials'] if t['id'] not in progress['trials']), None)


def normal_tasks_idle():
    from identity import runtime_path
    normal = runtime_path(Path.home(), 'STATE')
    for path in (normal / 'runs').glob('*/state.json'):
        state = read(path)
        if state.get('phase') in ('running', 'stale'):
            raise ComparisonError('A normal Telos job is active; comparison yields before sending a model request')
    for path in (normal / 'projects').glob('*/project.json'):
        state = read(path)
        if state.get('phase') == 'verifying' or (state.get('phase') == 'running' and state.get('continuation') == 'active'):
            raise ComparisonError('A normal Telos project is active; comparison yields')


def scheduler_root():
    from identity import runtime_path
    return runtime_path(Path.home(), 'STATE').parent / 'telos-comparison-scheduler'


@contextlib.contextmanager
def scheduler(root, release=False):
    """One active comparison trial across all experiment directories on this host."""
    directory = scheduler_root()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'lock').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        path = directory / 'active.json'
        owner = read(path).get('run') if path.exists() else None
        if owner and owner != str(root):
            raise ComparisonError('Another comparison owns the model slot: ' + owner + '; finish/cancel that trial first')
        if not release:
            write(path, {'run': str(root)})
        try:
            yield
        except Exception:
            if owner is None:
                path.unlink(missing_ok=True)
            raise
        finally:
            if release and path.exists() and read(path).get('run') == str(root):
                path.unlink()


def next_trial(root):
    with locked(root) as (root, manifest, progress):
        trial = select_trial(manifest, progress)
        return {'continuation': progress['continuation'], 'next_trial': trial,
                'record': progress['trials'].get(trial['id']) if trial else None,
                'completed': sum(t.get('outcome') in TERMINAL for t in progress['trials'].values()),
                'planned': len(manifest['trials'])}


def start(root, live=False):
    if not live:
        raise ComparisonError('--live is required to authorize a paid trial')
    with locked(root) as (root, manifest, progress):
        if progress['continuation'] != 'active':
            raise ComparisonError('Experiment is paused or cancelled')
        if progress['active_trial']:
            # A repeated start never sends a duplicate work request.
            return {'trial': progress['active_trial'], **progress['trials'][progress['active_trial']]}
        trial = select_trial(manifest, progress)
        if not trial:
            return {'complete': True}
        normal_tasks_idle()
        verify_runtime(root, manifest, trial['side'])
        case = next(c for c in manifest['cases'] if c['id'] == trial['case_id'])
        source = root / 'sources' / case['id']
        if git(source, 'rev-parse', 'HEAD') != case['base_commit'] or git(source, 'status', '--porcelain'):
            raise ComparisonError('Frozen source fixture changed')
        # Keep a persistent lease between CLI invocations; no second experiment
        # may consume the same host's executor slot until this trial is finished.
        with scheduler(root):
            trial_root = root / 'trials' / trial['id']
            trial_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            repo = trial_root / 'repo'
            if repo.exists():
                raise ComparisonError('Interrupted trial setup; preserve evidence and start a fresh experiment')
            git(trial_root, 'clone', '--no-hardlinks', '--quiet', str(source), str(repo))
            args = {k: case[k] for k in ('task', 'questions', 'checks', 'context', 'allowed_paths', 'audit') if k in case}
            args.update(repo=str(repo), notify=False, use_experience=False, threshold=90,
                        max_rounds=manifest['max_rounds'], budget=manifest['budget'], **manifest['selection'])
            if 'audit' in case:
                args['task_type'] = 'audit'
            result = worker(root, trial, 'telos_open', args)
            sid = result['session_id']
            record = {'session_id': sid, 'started_at': time.time(), 'outcome': 'active',
                      'worktree': result['worktree'], 'open_contract_hash': result['contract_hash']}
            progress['trials'][trial['id']] = record
            progress['active_trial'] = trial['id']
            write(root / 'progress.json', progress)
        # Sending is a separate explicit call: a crash can never silently repeat it.
        return {'trial': trial, **record, 'next': 'call telos_send with a public work message'}


def call(root, tool, arguments):
    if tool not in TOOLS:
        raise ComparisonError('Only the existing task/review tools are allowed in a trial')
    with locked(root) as (root, manifest, progress):
        trial = select_trial(manifest, progress)
        if not progress['active_trial'] or trial is None:
            raise ComparisonError('No active trial')
        if tool not in READ_TOOLS and progress['continuation'] != 'active':
            raise ComparisonError('Experiment is paused or cancelled')
        verify_runtime(root, manifest, trial['side'])
        record = progress['trials'][trial['id']]
        if not isinstance(arguments, dict) or 'session_id' in arguments or any(k.startswith('_') for k in arguments):
            raise ComparisonError('Session selection is owned by the comparison harness')
        if tool in PAID_TOOLS:
            normal_tasks_idle()
        if tool == 'telos_send' and not record.get('work_started'):
            if set(arguments) - {'message'} or arguments.get('message', WORK_MESSAGE) != manifest['initial_message']:
                raise ComparisonError('Both variants must receive the same frozen first work message; pass {}')
            arguments = {'message': manifest['initial_message']}
            existing = worker(root, trial, 'telos_status', {'session_id': record['session_id']})
            if existing.get('turns', 0):
                raise ComparisonError('Initial work was already dispatched; inspect status before any repair')
        with scheduler(root):
            result = worker(root, trial, tool, {**arguments, 'session_id': record['session_id']})
        if tool == 'telos_send':
            record['work_started'] = True
        if tool not in READ_TOOLS:
            record.setdefault('actions', []).append({'tool': tool, 'at': time.time(), 'arguments': arguments})
            write(root / 'progress.json', progress)
        return {'trial_id': trial['id'], 'result': result}


def outcome_checks(root, case, measured):
    """Run the frozen outcome oracle with the harness, never candidate test code."""
    import telos as b
    if 'audit' in case:
        report = measured['evidence']['audit']['report']
        expected = set(case['expected_findings'])
        review = measured.get('audit_review')
        if not isinstance(review, dict):
            raise ComparisonError('Accepted audit requires the original supervisor finding decisions')
        import audit
        audit.validate_review(measured['evidence']['audit'], review)
        confirmed = {d['id'] for d in review['findings'] if d['disposition'] == 'confirmed'}
        found = {f['check_id'] for f in report['findings']
                 if f['category'] == 'defect' and f['id'] in confirmed}
        return {'passed': expected == found, 'true_positive': len(expected & found),
                'false_positive': len(found - expected), 'false_negative': len(expected - found)}
    # Use a clean detached snapshot and the current trusted harness's sandbox.
    snapshot = root / 'oracle-snapshot'
    git(Path(measured['worktree']), 'worktree', 'add', '--detach', str(snapshot), measured['commit'])
    try:
        checks = b.executable_checks({'checks': case['checks'], 'dependency_paths': []}, snapshot)
        return {'passed': bool(checks) and all(x['status'] == 'passed' for x in checks), 'checks': checks}
    finally:
        git(Path(measured['worktree']), 'worktree', 'remove', '--force', str(snapshot))


def finish(root, reason=None):
    with locked(root) as (root, manifest, progress):
        if not progress['active_trial']:
            raise ComparisonError('No active trial to finish')
        trial = select_trial(manifest, progress)
        verify_runtime(root, manifest, trial['side'])
        record = progress['trials'][trial['id']]
        measured = worker(root, trial, 'measure', {'session_id': record['session_id']})
        if measured['phase'] in ('running', 'stale'):
            raise ComparisonError('Wait for the in-flight trial request before finishing')
        case = next(c for c in manifest['cases'] if c['id'] == trial['case_id'])
        if measured['contract_hash'] != record['open_contract_hash'] or measured['contract']['executor'] != manifest['executor']:
            raise ComparisonError('Frozen trial identity changed')
        oracle = None
        if measured['accepted']:
            oracle = outcome_checks(root, case, measured)
            outcome = 'passed' if oracle['passed'] else 'failed'
        else:
            if not reason or not isinstance(reason, str) or not reason.strip():
                raise ComparisonError('Unaccepted trials need an explicit failure reason; never auto-grade')
            outcome = 'cancelled' if progress['continuation'] == 'cancelled' else 'failed'
            worker(root, trial, 'telos_return_control', {'session_id': record['session_id'], 'action': 'cancel'})
        measured.pop('contract')
        evidence = {'trial': trial, 'measurement': measured, 'oracle': oracle,
                    'outcome': outcome, 'reason': reason, 'finished_at': time.time()}
        evidence_path = root / 'results' / (trial['id'] + '.json')
        write(evidence_path, evidence)
        record.update(outcome=outcome, finished_at=evidence['finished_at'],
                      result_hash=digest(evidence), result=str(evidence_path.relative_to(root)))
        progress['active_trial'] = None
        write(root / 'progress.json', progress)
        with scheduler(root, release=True):
            pass
        return {'trial_id': trial['id'], 'outcome': outcome, 'oracle': oracle,
                'usage': measured['usage'], 'next': 'compare start --live or compare report'}


def control(root, action):
    if action not in ('pause', 'resume', 'cancel'):
        raise ComparisonError('Unknown control action')
    with locked(root) as (root, manifest, progress):
        if progress['continuation'] == 'cancelled' and action != 'cancel':
            raise ComparisonError('Cancellation is permanent')
        if progress['active_trial']:
            trial = select_trial(manifest, progress)
            verify_runtime(root, manifest, trial['side'])
            worker(root, trial, 'telos_return_control', {
                'session_id': progress['trials'][trial['id']]['session_id'], 'action': action})
        progress['continuation'] = {'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled'}[action]
        write(root / 'progress.json', progress)
        # A crash during setup can leave a lease before an active task is
        # recorded. No paid call can start in that state; cancellation releases it.
        lease = scheduler_root() / 'active.json'
        if action == 'cancel' and not progress['active_trial'] and lease.exists() and read(lease).get('run') == str(root):
            with scheduler(root, release=True):
                pass
        return {'continuation': progress['continuation'], 'note': 'In-flight requests may finish; no automatic next stage'}


def summarize(manifest, results):
    sides = {}
    for side in SIDES:
        planned = [t for t in manifest['trials'] if t['side'] == side]
        completed = [results[t['id']] for t in planned if t['id'] in results]
        passed = sum(r['outcome'] == 'passed' for r in completed)
        usage = [r['measurement']['usage'] for r in completed]
        observed = sum(u['totals']['total_tokens'] for u in usage)
        unknown = sum(u['unknown_fields']['total_tokens'] for u in usage)
        exact = observed if not unknown else None
        sides[side] = {'planned': len(planned), 'completed': len(completed), 'passed': passed,
                       'failed': sum(r['outcome'] == 'failed' for r in completed),
                       'cancelled': sum(r['outcome'] == 'cancelled' for r in completed),
                       'pending': len(planned) - len(completed),
                       'success_rate': passed / len(planned),
                       'requests': sum(u['totals']['requests'] for u in usage),
                       'observed_total_tokens': observed, 'total_tokens': exact,
                       'unknown_token_requests': unknown,
                       # Includes failed trials and repair/exam requests, not just winners.
                       'tokens_per_success': exact / passed if passed and exact is not None else None,
                       'trial_wall_seconds': round(sum(r['finished_at'] - r['started_at'] for r in completed), 3),
                       'provider_seconds': (round(sum(r['measurement']['provider_seconds'] for r in completed), 3)
                           if all(r['measurement']['provider_seconds'] is not None for r in completed) else None)}
        for key in ('true_positive', 'false_positive', 'false_negative'):
            sides[side][key] = sum((r.get('oracle') or {}).get(key, 0) for r in completed)
        sides[side]['audit_oracles_scored'] = sum('true_positive' in (r.get('oracle') or {}) for r in completed)
    pairs = {}
    for trial in manifest['trials']:
        if trial['id'] in results:
            pairs.setdefault((trial['case_id'], trial['repeat']), {})[trial['side']] = results[trial['id']]['outcome']
    regressions, improvements = [], []
    for (case_id, repetition), pair in pairs.items():
        if set(pair) != set(SIDES):
            continue
        entry = {'case_id': case_id, 'repeat': repetition}
        if pair['baseline'] == 'passed' and pair['candidate'] == 'failed':
            regressions.append(entry)
        if pair['candidate'] == 'passed' and pair['baseline'] == 'failed':
            improvements.append(entry)
    complete = len(results) == len(manifest['trials'])
    comparable = complete and all(not sides[s]['cancelled'] for s in SIDES)
    token_delta = (sides['candidate']['total_tokens'] - sides['baseline']['total_tokens']
                   if comparable and all(sides[s]['total_tokens'] is not None for s in SIDES) else None)
    return {'complete': complete, 'comparable': comparable, 'sides': sides,
            'regressions': regressions, 'improvements': improvements,
            'candidate_minus_baseline_tokens': token_delta,
            'automatic_promotion': False,
            'limitations': ['Small fixed suites are engineering evidence, not proof of general capability or RSI.',
                'Token totals cover executor work, repairs and exams; original supervisor usage and money cost are not available.',
                'Wall time includes manual review/wait; provider time is reported separately when known.',
                'Model aliases/gateway routing and remote load can drift; paired order does not prove identical upstream weights.',
                'Public built-in fixtures are development examples, not a private uncontaminated holdout.',
                'Original-supervisor review is manual and not blinded; initial instructions and rubric are frozen, repair feedback is recorded.',
                'Summary usage covers finished trials; active trial usage is available through call telos_status.',
                'Audit detection counts cover only independently scored, accepted audit reports; audit_oracles_scored states that coverage.',
                'Normal jobs are checked before paid actions; already-running upstream requests are not preempted.']}


def report(root):
    with locked(root) as (root, manifest, progress):
        for side in SIDES:
            verify_runtime(root, manifest, side)
        results = {}
        for trial_id, record in progress['trials'].items():
            if record['outcome'] not in TERMINAL:
                continue
            result = read(root / record['result'])
            if digest(result) != record['result_hash']:
                raise ComparisonError('Saved trial evidence changed')
            results[trial_id] = {**result, 'started_at': record['started_at']}
        summary = summarize(manifest, results)
        output = {'manifest_hash': progress['manifest_hash'], 'label': manifest['label'],
                  'versions': {s: manifest['versions'][s]['sha256'] for s in SIDES},
                  'runtime_versions': {s: manifest['versions'][s].get('version') for s in SIDES},
                  'executor': {k: manifest['executor'].get(k) for k in ('provider', 'model', 'reasoning_effort')},
                  'upstream_revision': manifest['upstream_revision'],
                  'environment': manifest['environment'], 'request_ceiling': manifest['request_ceiling'],
                  'summary': summary,
                  'trials': []}
        for trial in manifest['trials']:
            result = results.get(trial['id'])
            case = next(x for x in manifest['cases'] if x['id'] == trial['case_id'])
            row = {**trial, 'category': case['category'], 'split': case['split'],
                   'outcome': result['outcome'] if result else ('active' if trial['id'] == progress['active_trial'] else 'pending')}
            if result:
                measured = result['measurement']
                usage = measured['usage']
                row.update(reason=result.get('reason'), evidence=progress['trials'][trial['id']]['result'],
                           requests=usage['totals']['requests'],
                           observed_total_tokens=usage['totals']['total_tokens'],
                           total_tokens=usage['totals']['total_tokens'] if not usage['unknown_fields']['total_tokens'] else None,
                           provider_seconds=measured['provider_seconds'],
                           wall_seconds=round(result['finished_at'] - result['started_at'], 3),
                           rounds=measured['rounds'], turns=measured['turns'], oracle=result.get('oracle'))
            output['trials'].append(row)
        write(root / 'report.json', output)
        lines = ['# Telos comparison / 版本对比', '',
                 f'Complete: {summary["complete"]}; comparable: {summary["comparable"]}. No automatic promotion.', '',
                 '| Metric | Baseline | Candidate |', '| --- | ---: | ---: |']
        for key in ('planned', 'completed', 'passed', 'failed', 'cancelled', 'pending', 'success_rate',
                    'requests', 'total_tokens', 'unknown_token_requests', 'tokens_per_success',
                    'trial_wall_seconds', 'provider_seconds', 'audit_oracles_scored', 'true_positive', 'false_positive', 'false_negative'):
            cells = [summary['sides'][s][key] for s in SIDES]
            lines.append('| ' + key + ' | ' + ' | '.join('unknown' if x is None else str(round(x, 3) if isinstance(x, float) else x) for x in cells) + ' |')
        lines += ['', '## Paired outcomes', '', 'Regressions: ' + json.dumps(summary['regressions']),
                  '', 'Improvements: ' + json.dumps(summary['improvements']), '', '## Limits', '']
        lines.extend('- ' + text for text in summary['limitations'])
        (root / 'report.md').write_text('\n'.join(lines) + '\n')
        return {'report': str(root / 'report.json'), 'markdown': str(root / 'report.md'), **summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'suite', 'next', 'start', 'call', 'finish', 'report', 'pause', 'resume', 'cancel'])
    parser.add_argument('--run', type=Path)
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--tool', choices=sorted(TOOLS))
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--reason')
    args = parser.parse_args(argv)
    if args.action == 'suite':
        from evaluation_suite import cases
        result = [{k: c[k] for k in ('id', 'category', 'split')} for c in cases()]
    elif args.run is None:
        parser.error('--run is required')
    elif args.action == 'init':
        if args.spec is None:
            parser.error('--spec is required')
        result = initialize(args.run, read(args.spec))
    elif args.action == 'start':
        result = start(args.run, args.live)
    elif args.action == 'call':
        if args.tool is None:
            parser.error('--tool is required; JSON arguments are read from stdin')
        result = call(args.run, args.tool, json.load(sys.stdin))
    elif args.action == 'finish':
        result = finish(args.run, args.reason)
    elif args.action in ('pause', 'resume', 'cancel'):
        result = control(args.run, args.action)
    else:
        result = {'next': next_trial, 'report': report}[args.action](args.run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    os.umask(0o077)
    if sys.argv[1:] == ['_worker']:
        try:
            print(json.dumps({'result': _worker()}, ensure_ascii=False))
        except Exception as error:
            print(json.dumps({'error': str(error)[:2000]}))
    else:
        try:
            sys.path.insert(0, str(SOURCE))
            main()
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)

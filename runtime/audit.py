"""Frozen public audit boundaries and evidence-backed, supervisor-owned review.

An audit accepts the quality of an investigation, never the health of its target.
Source stays read-only; findings and business probe failures are report data.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import unquote, urlsplit

from providers import ProviderError as GateError

KINDS = ('code', 'api', 'interaction', 'visual')
RESULTS = ('passed', 'finding', 'blocked', 'not_tested')
ACTIONS = ('click', 'fill', 'select', 'press', 'assert_text', 'assert_visible', 'assert_url', 'screenshot')


def text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise GateError(label + ' must be nonempty bounded text')
    return value


def strings(value, label, empty=False):
    if not isinstance(value, list) or len(value) > 100 or (not value and not empty):
        raise GateError(label + ' must be a bounded list')
    for item in value:
        text(item, label)
    if len(set(value)) != len(value):
        raise GateError(label + ' contains duplicates')
    return value


def fields(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise GateError('Invalid ' + label + ' fields')


def relative(value, root_allowed=False):
    if value == '.' and root_allowed:
        return value
    if (not isinstance(value, str) or not value or '\\' in value or any(c in value for c in '*?[\x00\n')
            or PurePosixPath(value).is_absolute() or any(x in ('', '.', '..', '.git') for x in value.split('/'))):
        raise GateError('Audit paths must be literal relative paths without links or traversal')
    return value


def origin(url):
    try:
        u = urlsplit(url)
        port = u.port
        if (u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password
                or '\\' in url or any(ord(c) < 33 for c in url)):
            raise ValueError()
        host = '[' + u.hostname + ']' if ':' in u.hostname else u.hostname
        suffix = '' if port is None or port == (443 if u.scheme == 'https' else 80) else ':' + str(port)
        return u.scheme + '://' + host.lower() + suffix
    except (ValueError, TypeError):
        raise GateError('Audit targets require explicit HTTP(S) origins without credentials') from None


def url_allowed(policy, url, method='GET'):
    """No redirects or encoded path traversal may expand a frozen target scope."""
    try:
        host = origin(url)
        path = urlsplit(url).path or '/'
        decoded = unquote(path)
        if unquote(decoded) != decoded or '\\' in decoded or any(x in ('.', '..') for x in decoded.split('/')):
            return False
        return any(host == t['origin'] and method in t['methods'] and
                   (t['path_prefix'] == '/' or decoded == t['path_prefix'] or decoded.startswith(t['path_prefix'] + '/'))
                   for t in policy['targets'])
    except GateError:
        return False


def validate(value, b=None):
    required = ('environment', 'scope_paths', 'requirements', 'out_of_scope', 'stop_conditions', 'checklist', 'targets', 'mutation_policy')
    fields(value, (*required, 'limits', 'browser'), required, 'audit boundary')
    value = json.loads(json.dumps(value))
    fields(value['environment'], ('kind', 'name'), ('kind', 'name'), 'audit environment')
    if value['environment']['kind'] not in ('local', 'test', 'staging'):
        raise GateError('Audit probes require an explicitly designated local/test/staging environment')
    text(value['environment']['name'], 'environment name')
    for key in ('scope_paths', 'requirements', 'stop_conditions'):
        strings(value[key], key)
    strings(value['out_of_scope'], 'out_of_scope', empty=True)
    for path in value['scope_paths']:
        relative(path, root_allowed=True)
    text(value['mutation_policy'], 'mutation_policy')
    if not isinstance(value['checklist'], list) or not 1 <= len(value['checklist']) <= 100:
        raise GateError('Provide 1-100 public audit checklist items')
    ids = set()
    for item in value['checklist']:
        fields(item, ('id', 'kind', 'prompt', 'expected', 'required', 'viewports'), ('id', 'kind', 'prompt', 'expected'), 'checklist')
        if not isinstance(item['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', item['id']) or item['id'] in ids:
            raise GateError('Audit checklist IDs must be unique simple identifiers')
        ids.add(item['id'])
        if item['kind'] not in KINDS:
            raise GateError('Unknown audit checklist kind')
        text(item['prompt'], 'checklist prompt'); text(item['expected'], 'expected behavior')
        item.setdefault('required', True)
        if type(item['required']) is not bool:
            raise GateError('Checklist required must be boolean')
    if not isinstance(value['targets'], list) or len(value['targets']) > 30:
        raise GateError('Audit targets must be a bounded array (empty for offline code audits)')
    for target in value['targets']:
        fields(target, ('origin', 'path_prefix', 'methods'), ('origin', 'path_prefix', 'methods'), 'network target')
        if origin(target['origin']) != target['origin'] or urlsplit(target['origin']).path:
            raise GateError('Use canonical origins without paths, trailing slashes or default ports')
        path = target['path_prefix']
        if not isinstance(path, str) or not path.startswith('/') or '?' in path or '#' in path:
            raise GateError('Target path_prefix must be an absolute URL path')
        if path != '/':
            relative(path[1:])
        if unquote(path) != path:
            raise GateError('Encoded target prefixes are not supported')
        strings(target['methods'], 'target methods')
        if any(m not in ('GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE') for m in target['methods']):
            raise GateError('Unsupported HTTP method')
    limits = value.setdefault('limits', {})
    fields(limits, ('max_probes', 'probe_timeout_seconds', 'max_artifact_bytes'), (), 'audit limits')
    for key, default, upper in [('max_probes', 30, 200), ('probe_timeout_seconds', 60, 300),
                                ('max_artifact_bytes', 30_000_000, 200_000_000)]:
        limits.setdefault(key, default)
        if type(limits[key]) is not int or not 1 <= limits[key] <= upper:
            raise GateError('Invalid audit ' + key)
    browser = value.get('browser')
    if browser is not None:
        fields(browser, ('module_path', 'executable_path', 'viewports', 'actions'),
               ('module_path', 'executable_path', 'viewports', 'actions'), 'browser adapter')
        for key in ('module_path', 'executable_path'):
            p = Path(text(browser[key], key))
            if not p.is_absolute() or not p.is_file() or p.is_symlink():
                raise GateError('Browser adapter needs existing absolute module/executable paths; Telos installs no dependencies')
            browser[key] = str(p.resolve())
        strings(browser['actions'], 'browser actions', empty=True)
        if set(browser['actions']) - set(ACTIONS):
            raise GateError('Unsupported browser actions')
        if not isinstance(browser['viewports'], list) or not 1 <= len(browser['viewports']) <= 10:
            raise GateError('Provide 1-10 browser viewports')
        names = set()
        for viewport in browser['viewports']:
            fields(viewport, ('name', 'width', 'height'), ('name', 'width', 'height'), 'viewport')
            text(viewport['name'], 'viewport name')
            if viewport['name'] in names:
                raise GateError('Duplicate viewport name')
            names.add(viewport['name'])
            if any(type(viewport[k]) is not int or not 240 <= viewport[k] <= 3840 for k in ('width', 'height')):
                raise GateError('Viewport dimensions must be 240-3840')
    if any(i['kind'] in ('interaction', 'visual') for i in value['checklist']) and not browser:
        raise GateError('Page audits require an existing Playwright module, browser executable and viewports')
    if any(i['kind'] != 'code' for i in value['checklist']) and not value['targets']:
        raise GateError('Network/page checklist items require explicit targets')
    for item in value['checklist']:
        if item['kind'] in ('visual', 'interaction'):
            item.setdefault('viewports', [v['name'] for v in browser['viewports']])
            strings(item['viewports'], 'checklist viewports')
            if not set(item['viewports']) <= {v['name'] for v in browser['viewports']}:
                raise GateError('Checklist refers to undeclared viewports')
        elif 'viewports' in item:
            raise GateError('Only page checklist items may declare viewports')
    return value


def artifacts(b, state):
    return b.CACHE / 'audits' / state['session_id'] / 'artifacts'


def source_paths(state, policy):
    root = Path(state['worktree']).resolve()
    result = []
    for name in policy['scope_paths']:
        path = root / name
        if not path.exists() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise GateError('An audit scope path is missing or redirected: ' + name)
        # A symlink inside an allowed directory must not grant additional reads.
        candidates = path.rglob('*') if path.is_dir() else (path,)
        if any(p.is_symlink() for p in candidates):
            raise GateError('Audit source scope contains symlinks; choose ordinary source paths')
        result.append(path)
    return result


def validate_source_scope(b, repo, commit, policy):
    """Inspect tracked entries, not ignored dependencies in the user's checkout."""
    entries = []
    for row in b.git(repo, 'ls-tree', '-r', '-z', '--full-tree', commit).split('\0'):
        if row:
            metadata, name = row.split('\t', 1)
            entries.append((metadata.split()[0], name))
    for scope in policy['scope_paths']:
        matched = [(mode, name) for mode, name in entries if scope == '.' or name == scope or name.startswith(scope + '/')]
        if not matched:
            raise GateError('Audit scope has no tracked source: ' + scope)
        if any(mode not in ('100644', '100755') for mode, _ in matched):
            raise GateError('Audit scope contains tracked links/submodules; choose ordinary source paths')


def initialize(b, state, contract):
    source_paths(state, contract['audit'])
    root = artifacts(b, state)
    root.mkdir(parents=True, mode=0o700)
    template = {'source_commit': state['base_commit'], 'environment': contract['audit']['environment'],
                'results': [{'id': i['id'], 'status': 'not_tested', 'actual': 'Not checked yet',
                             'steps': [], 'evidence': [], 'reason': 'Not checked yet'} for i in contract['audit']['checklist']],
                'findings': []}
    b.write_json(root / 'report.json', template)
    b.write_json(root / 'audit-report-help.json', {
        'statuses': list(RESULTS),
        'result': {'id': 'existing checklist ID', 'status': 'finding', 'actual': 'Observed behavior',
                   'steps': ['Concrete reproduction step'], 'evidence': ['relative/evidence-file.json']},
        'finding': {'id': 'F1', 'check_id': 'existing checklist ID', 'category': 'defect', 'severity': 'medium',
                    'title': 'Concise issue title', 'expected': 'Requirement or design expectation',
                    'actual': 'Observed behavior', 'steps': ['Concrete reproduction step'],
                    'evidence': ['relative/evidence-file.json'], 'location': 'Source path/line, API or page location'},
        'categories': ['defect', 'suspected', 'suggestion'], 'severities': ['critical', 'high', 'medium', 'low', 'info'],
        'rules': ['Keep source_commit and environment unchanged. Include every checklist ID exactly once.',
                  'Use blocked/not_tested with reason for unavailable checks; required unverified checks prevent acceptance.',
                  'A finding status requires a corresponding structured finding. Do not invent evidence.',
                  'Evidence is an array of relative file paths under the artifact directory, not text claims or external paths.',
                  'API/page results must cite matching broker probe evidence. Page results must cover every assigned viewport.',
                  'Include probe evidence and screenshots in the result; finding evidence must also appear in its result.',
                  'Findings do not need to be repaired for the investigation to pass. The supervisor makes final decisions.']})


def read_regular(root, name, maximum):
    """Traverse descriptor-relative paths without following swapped symlinks."""
    relative(name)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                raise GateError('Audit evidence must be a bounded regular file without links')
            data = stream.read(maximum + 1)
            if len(data) != info.st_size or len(data) > maximum:
                raise GateError('Audit evidence changed while reading')
            return data
    except OSError:
        raise GateError('Audit evidence path changed or contains links') from None
    finally:
        os.close(directory)


def copy_evidence(source, destination, files):
    destination.mkdir(parents=True, mode=0o700)
    for name, info in files.items():
        data = read_regular(source, name, info['bytes'])
        if hashlib.sha256(data).hexdigest() != info['sha256']:
            raise GateError('Audit evidence changed while copying')
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o600)


def manifest(root, maximum):
    if root.is_symlink() or not root.is_dir():
        raise GateError('Audit artifact directory is missing or redirected')
    result = {}; size = 0
    for path in sorted(root.rglob('*')):
        mode = path.lstat()
        if path.is_symlink() or not (stat.S_ISREG(mode.st_mode) or stat.S_ISDIR(mode.st_mode)):
            raise GateError('Audit artifacts must be ordinary files without links')
        if path.is_dir():
            continue
        if mode.st_nlink != 1:
            raise GateError('Hard-linked audit artifacts are forbidden')
        name = path.relative_to(root).as_posix()
        relative(name)
        size += mode.st_size
        if size > maximum or len(result) >= 1000:
            raise GateError('Audit artifact budget exceeded')
        data = read_regular(root, name, min(maximum, mode.st_size))
        result[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    return result


def validate_report(b, state, contract, root, files):
    if 'report.json' not in files or files['report.json']['bytes'] > 2_000_000:
        raise GateError('A bounded audit report.json is required')
    report = json.loads(read_regular(root, 'report.json', 2_000_000))
    fields(report, ('source_commit', 'environment', 'results', 'findings'),
           ('source_commit', 'environment', 'results', 'findings'), 'audit report')
    if report['source_commit'] != state['base_commit'] or report['environment'] != contract['audit']['environment']:
        raise GateError('Audit report source/environment does not match the frozen task')
    checklist = {i['id']: i for i in contract['audit']['checklist']}
    if not isinstance(report['results'], list) or not isinstance(report['findings'], list) or len(report['findings']) > 200:
        raise GateError('Audit results/findings must be bounded arrays')
    seen = set(); incomplete = []; screenshots = set()
    probe_index = b.run_dir(state['session_id']) / 'audit-probes.json'
    probes = b.read_json(probe_index).get('completed', []) if probe_index.exists() else []
    for result in report['results']:
        fields(result, ('id', 'status', 'actual', 'steps', 'evidence', 'reason'),
               ('id', 'status', 'actual', 'steps', 'evidence'), 'audit result')
        ident = result['id']
        if not isinstance(ident, str) or ident not in checklist or ident in seen or result['status'] not in RESULTS:
            raise GateError('Every checklist item needs exactly one valid result')
        seen.add(ident); text(result['actual'], 'actual behavior')
        strings(result['steps'], 'reproduction steps', empty=True)
        strings(result['evidence'], 'evidence paths', empty=True)
        if any(name not in files or name == 'report.json' for name in result['evidence']):
            raise GateError('Audit result references missing evidence')
        if result['status'] in ('blocked', 'not_tested'):
            text(result.get('reason'), 'unverified reason')
            if checklist[ident]['required']:
                incomplete.append(ident)
            continue
        if not result['steps'] or not result['evidence']:
            raise GateError('Checked audit items require steps and actual evidence')
        if checklist[ident]['kind'] != 'code':
            trusted = [p for p in probes if p['check_id'] == ident and p['evidence'] in result['evidence']
                       and all(files.get(name, {}).get('sha256') == sha for name, sha in p['files'].items())]
            if not trusted:
                raise GateError('API/page results require matching bridge-captured probe evidence')
            expected_kind = 'http' if checklist[ident]['kind'] == 'api' else 'browser'
            if not any(p['kind'] == expected_kind for p in trusted):
                raise GateError('Evidence probe kind does not match the checklist')
            if checklist[ident]['kind'] in ('visual', 'interaction'):
                if not set(checklist[ident]['viewports']) <= {p.get('viewport') for p in trusted}:
                    raise GateError('Page evidence is missing a required viewport')
                for probe in trusted:
                    screenshots.update(name for name in probe['files'] if name.endswith('.png'))
                if not any(name.endswith('.png') for p in trusted for name in p['files']):
                    raise GateError('Page results require captured screenshots')
    if seen != set(checklist):
        raise GateError('Audit checklist coverage is incomplete; report every item, including untested ones')
    findings = set(); linked = set()
    for item in report['findings']:
        required = ('id', 'check_id', 'category', 'severity', 'title', 'expected', 'actual', 'steps', 'evidence', 'location')
        fields(item, required, required, 'audit finding')
        text(item['id'], 'finding id')
        if item['id'] in findings or item['check_id'] not in checklist:
            raise GateError('Findings need unique IDs and a known checklist item')
        findings.add(item['id']); linked.add(item['check_id'])
        if item['category'] not in ('defect', 'suspected', 'suggestion') or item['severity'] not in ('critical', 'high', 'medium', 'low', 'info'):
            raise GateError('Invalid finding category/severity')
        for key in ('title', 'expected', 'actual', 'location'):
            text(item[key], key)
        strings(item['steps'], 'finding reproduction steps'); strings(item['evidence'], 'finding evidence')
        owner = next(r for r in report['results'] if r['id'] == item['check_id'])
        if any(name not in owner['evidence'] for name in item['evidence']):
            raise GateError('Finding evidence must belong to its checklist result')
    if any(r['status'] == 'finding' and r['id'] not in linked for r in report['results']):
        raise GateError('Finding results need corresponding structured findings')
    return report, incomplete, sorted(screenshots)


def collect(b, state, contract, target):
    root = artifacts(b, state)
    files = manifest(root, contract['audit']['limits']['max_artifact_bytes'])
    report, incomplete, screenshots = validate_report(b, state, contract, root, files)
    pinned = b.run_dir(state['session_id']) / ('audit-evidence-' + str(state['rounds']) + '-' + state['job_id'])
    if pinned.exists():
        raise GateError('Audit evidence round already exists; start a fresh evaluation')
    copy_evidence(root, pinned, files)
    if manifest(pinned, contract['audit']['limits']['max_artifact_bytes']) != files or manifest(root, contract['audit']['limits']['max_artifact_bytes']) != files:
        raise GateError('Audit evidence changed while pinning')
    # Private quality checks see an immutable copy in the verification snapshot.
    copy_evidence(pinned, target / '.telos-audit', files)
    result = {'report': report, 'manifest': files, 'manifest_hash': b.digest(files), 'job_id': state['job_id'],
              'evidence_dir': str(pinned), 'screenshots': screenshots, 'incomplete_required': incomplete}
    return result, {'id': 'telos-audit-coverage', 'status': 'failed' if incomplete else 'passed',
                    'stdout': 'Required items not verified: ' + ', '.join(incomplete) if incomplete else 'Report and evidence validated',
                    'returncode': 1 if incomplete else 0}


def verify(b, state, contract, evidence, live=True):
    maximum = contract['audit']['limits']['max_artifact_bytes']
    expected = b.run_dir(state['session_id']) / ('audit-evidence-' + str(state['rounds']) + '-' + state['job_id'])
    if evidence.get('evidence_dir') != str(expected) or b.digest(evidence.get('manifest')) != evidence.get('manifest_hash'):
        raise GateError('Audit evidence identity changed')
    if manifest(expected, maximum) != evidence['manifest']:
        raise GateError('Pinned audit evidence changed')
    if b.read_json(expected / 'report.json') != evidence['report']:
        raise GateError('Pinned audit report changed')
    if live and not state.get('archived') and manifest(artifacts(b, state), maximum) != evidence['manifest']:
        raise GateError('Audit artifacts changed after evaluation; re-evaluate')
    if live and not state.get('archived') and (b.git(state['worktree'], 'rev-parse', 'HEAD') != state['base_commit']
            or b.git(state['worktree'], 'status', '--porcelain', '--ignored')):
        raise GateError('Audit source changed after evaluation')


def validate_review(evidence, review):
    fields(review, ('findings', 'limitations', 'visual_evidence_reviewed', 'coverage_rationale'),
           ('findings', 'limitations', 'visual_evidence_reviewed', 'coverage_rationale'), 'supervisor audit review')
    text(review['coverage_rationale'], 'coverage rationale')
    report = evidence['report']
    expected = {f['id'] for f in report['findings']}
    seen = set()
    if not isinstance(review['findings'], list):
        raise GateError('Supervisor must decide every finding')
    for decision in review['findings']:
        fields(decision, ('id', 'disposition', 'rationale'), ('id', 'disposition', 'rationale'), 'finding decision')
        if decision['id'] not in expected or decision['id'] in seen or decision['disposition'] not in ('confirmed', 'unconfirmed', 'dismissed', 'suggestion'):
            raise GateError('Invalid or duplicate supervisor finding decision')
        seen.add(decision['id']); text(decision['rationale'], 'finding rationale')
    if seen != expected:
        raise GateError('Supervisor must decide every finding exactly once')
    strings(review['limitations'], 'acknowledged limitations', empty=True)
    limitations = {r['id'] for r in report['results'] if r['status'] in ('blocked', 'not_tested')}
    if set(review['limitations']) != limitations:
        raise GateError('Supervisor must acknowledge all unverified checklist items')
    strings(review['visual_evidence_reviewed'], 'reviewed screenshots', empty=True)
    if set(review['visual_evidence_reviewed']) != set(evidence['screenshots']):
        raise GateError('Supervisor must inspect all captured page screenshots before accepting')


def repair_source(b, value, repo, caller=None):
    fields(value, ('session_id', 'finding_ids'), ('session_id', 'finding_ids'), 'repair source')
    strings(value['finding_ids'], 'repair finding IDs')
    state, contract = b.load_session(b.run_dir(value['session_id']))
    if state.get('owner') and state['owner']['thread_id'] != caller:
        raise GateError('Repair handoff belongs to another supervisor thread')
    if state['repo'] != str(repo) or contract.get('task_type') != 'audit' or not b.status(state['session_id'])['accepted']:
        raise GateError('Repair source must be an accepted audit of the same repository')
    confirmed = {d['id'] for d in state['acceptance']['audit_review']['findings'] if d['disposition'] == 'confirmed'}
    if not set(value['finding_ids']) <= confirmed:
        raise GateError('Repair handoff requires supervisor-confirmed findings')
    return {**value, 'packet_id': state['packet_id'], 'source_commit': state['submitted_commit']}


def public_prompt(b, state, contract):
    return ('You are conducting a SYSTEM AUDIT. The source is read-only. Do not fix source or expand scope. '
            'Write report.json and evidence only in TELOS_AUDIT_ARTIFACTS. Inspect the public checklist with tools; '
            'private assessment questions are separate. Findings are not acceptance. Mark unavailable checks blocked/not_tested '
            'with reasons; do not invent evidence or equate an untested system with a healthy one. Record out-of-scope '
            'leads and continue independent in-scope work; stop affected steps at the frozen boundaries. '
            'API/browser actions must use the authenticated local audit probe service, never direct network access. '
            'Use Python urllib.request: POST JSON to os.environ["TELOS_AUDIT_URL"] with Authorization: Bearer '
            '+ os.environ["TELOS_AUDIT_TOKEN"]. Read audit-report-help.json and audit-probe-help.json in the artifact directory '
            'for report/finding schemas, evidence rules and probe request shapes. '
            'Additional read-only dependencies: ' + b.encoded(contract.get('dependency_paths', [])) + '\n'
            'Source root: ' + state['worktree'] + ' (also TELOS_AUDIT_SOURCE). '
            'Source commit: ' + state['base_commit'] + '\nPUBLIC AUDIT CONTRACT:\n' + b.encoded(contract['audit']) + '\n')


def summary_markdown(evidence, review):
    report = evidence['report']
    decisions = {d['id']: d for d in review['findings']}
    lines = ['# System audit report', '', 'Audit acceptance concerns investigation quality, not absence of bugs.',
             '', 'Source commit: `' + report['source_commit'] + '`', 'Environment: ' + report['environment']['name'],
             '', '## Coverage', '']
    for item in report['results']:
        lines += [f"- {item['id']}: {item['status']} — {item['actual']}"]
        if item['status'] in ('blocked', 'not_tested') and item.get('reason'):
            lines += ['  Limitation: ' + item['reason']]
    lines += ['', '## Findings', '']
    for item in report['findings']:
        lines += [f"### {item['id']}: {item['title']}", '',
                  f"{item['severity']} / {decisions[item['id']]['disposition']}", '',
                  'Location: ' + item['location'], 'Expected: ' + item['expected'], 'Actual: ' + item['actual'],
                  'Reproduce: ' + ' → '.join(item['steps']), 'Evidence: ' + ', '.join(item['evidence']),
                  'Supervisor: ' + decisions[item['id']]['rationale'], '']
    return '\n'.join(lines) + '\n'

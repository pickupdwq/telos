"""Evidence and public design documents for a read-only refactoring meeting."""
from pathlib import PurePosixPath
from urllib.parse import quote
import hashlib
import os

from providers import ProviderError as GateError


def records(**fields):
    return {'type': 'array', 'items': {'type': 'object', 'properties': fields,
            'required': list(fields), 'additionalProperties': False}}


TEXT = {'type': 'string'}
PATHS = {'type': 'array', 'items': TEXT}
COVERAGE = ('exercised', 'source_only', 'unverified', 'excluded')
DISCOVERY = {'source_commit': TEXT, 'walkthrough': records(paths=PATHS, status={'enum': list(COVERAGE)}, finding=TEXT),
             'current_prd': records(flow=TEXT, behavior=TEXT, sources=PATHS)}
DESIGN = {'detailed_prd': records(flow=TEXT, preserve=TEXT, internal_changes=TEXT, verify=TEXT),
          'structure': records(path=TEXT, responsibility=TEXT), 'technology': records(choice=TEXT, rationale=TEXT),
          'index_path': TEXT, 'code_index': records(flow=TEXT, paths=PATHS)}


def invalid(message):
    raise GateError(message, code='invalid_refactor_design', next_action='correct_arguments')


def require_fields(value, fields):
    """All fields in a design document are required; empty prose is not a decision."""
    if not isinstance(value, dict) or set(value) != set(fields):
        invalid('Expected refactor fields: ' + ', '.join(fields))
    for key, schema in fields.items():
        item = value[key]
        if 'enum' in schema:
            if item not in schema['enum']:
                invalid('Invalid ' + key)
        elif schema['type'] == 'array':
            if not isinstance(item, list) or not item:
                invalid('Provide nonempty ' + key)
            for row in item:
                if schema['items']['type'] == 'object':
                    require_fields(row, schema['items']['properties'])
                elif not isinstance(row, str) or not row.strip():
                    invalid('Provide concrete entries in ' + key)
        elif not isinstance(item, str) or not item.strip():
            invalid('Provide nonempty ' + key)


def repo_path(value):
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or value == '.' or '..' in path.parts
            or '.git' in path.parts or '\\' in value or any(ord(c) < 32 for c in value)):
        invalid('Use literal repository-relative paths: ' + repr(value))
    return value


def flows(rows):
    names = {row['flow'] for row in rows}
    if len(names) != len(rows):
        invalid('Each business flow must appear exactly once per section.')
    return names


def validate_discovery(b, repo, commit, data):
    require_fields(data, DISCOVERY)
    if data['source_commit'] != commit:
        invalid('Walk through the current source commit before the meeting.')
    tracked = set(os.fsdecode(b.git(repo, 'ls-tree', '-rz', '--name-only', commit, raw=True)).split('\0')) - {''}
    inventory = set()
    for name in tracked:
        path = PurePosixPath(name)
        inventory.add(name)
        inventory.update(str(p) for p in path.parents if p != PurePosixPath('.'))
    areas = {}
    for row in data['walkthrough']:
        for entry in row['paths']:
            name = repo_path(entry.rstrip('/'))
            if name not in inventory:
                invalid('Walkthrough path is not in the pinned source: ' + name)
            if name in areas:
                invalid('Walkthrough repeats a path: ' + name)
            areas[name] = row['status']
    def status(name):
        if name in areas:
            return areas[name]
        return next((areas[str(p)] for p in PurePosixPath(name).parents if str(p) in areas), None)
    missing = sorted(name for name in tracked if status(name) is None)
    if missing:
        invalid('Walkthrough has unaccounted tracked paths, e.g. ' + ', '.join(missing[:5]))
    flows(data['current_prd'])
    for row in data['current_prd']:
        for name in row['sources']:
            if repo_path(name) not in tracked or status(name) not in ('exercised', 'source_only'):
                invalid('Current PRD needs inspected source files for ' + row['flow'] + ': ' + name)


def validate_design(discovery, design):
    require_fields(design, DESIGN)
    current = flows(discovery['current_prd'])
    if flows(design['detailed_prd']) != current or flows(design['code_index']) != current:
        invalid('Detailed PRD and code index must cover the same business flows as the current PRD.')
    index = repo_path(design['index_path'])
    layout = [repo_path(row['path'].rstrip('/')) for row in design['structure']]
    if len(layout) != len(set(layout)):
        invalid('Describe each module path once, with its distinct responsibility.')
    for row in design['code_index']:
        for path in row['paths']:
            if repo_path(path) == index:
                invalid('Index entries must point to the implementing code or tests, not the index itself.')


def validate_batches(design, tasks):
    known = flows(design['detailed_prd'])
    for task in tasks:
        selected = task.get('refactor_flows')
        if (not isinstance(selected, list) or not selected
                or any(not isinstance(flow, str) or flow not in known for flow in selected)):
            invalid('Map task ' + task['id'] + ' to existing PRD flow(s) via refactor_flows.')


def owns_index(task, path):
    return any(area == '.' or area == path or path.startswith(area + '/') for area in task['allowed_paths'])


def handoff(b, design, task):
    public = {key: design[key] for key in ('structure', 'technology', 'index_path')}
    for key in ('detailed_prd', 'code_index'):
        public[key] = [r for r in design[key] if r['flow'] in task['refactor_flows']]
    text = '\nApproved PRD and architecture for this batch (compare before/after behavior):\n' + b.encoded(public)
    if owns_index(task, design['index_path']):
        text += ('\nMaintain this generated section at ' + design['index_path'] +
                 '; preserve hand-written content outside the section:\n' + index_block(design))
    return text


def index_block(design):
    parent = str(PurePosixPath(design['index_path']).parent)
    lines = ['<!-- telos:codemap -->']
    for row in design['code_index']:
        lines += ['', '### ' + row['flow'], '']
        for name in row['paths']:
            target = quote(os.path.relpath(name, parent), safe='/.')
            lines.append(f'- [`{name}`]({target})')
    return '\n'.join(lines + ['', '<!-- /telos:codemap -->'])


def documents(discovery, design=None, discussion=None):
    lines = ['# 现状 PRD', '', '走查版本：' + discovery['source_commit'], '', '## 全程序走查', '']
    for row in discovery['walkthrough']:
        lines.append(f"- {', '.join(row['paths'])} [{row['status']}]：{row['finding']}")
    for flow in discovery['current_prd']:
        lines += ['', '## ' + flow['flow'], '', flow['behavior'], '', '来源：' + '、'.join(flow['sources'])]
    docs = {'current-prd.md': '\n'.join(lines) + '\n'}
    if design is None:
        return docs
    lines = ['# 详细 PRD 与重构设计']
    for flow in design['detailed_prd']:
        lines += ['', '## ' + flow['flow'], '', '保持：' + flow['preserve'],
                  '', '内部调整：' + flow['internal_changes'], '', '验证：' + flow['verify']]
    lines += ['', '## 目录与职责', '']
    lines.extend(f"- {r['path']}：{r['responsibility']}" for r in design['structure'])
    lines += ['', '## 技术选型', '']
    lines.extend(f"- {r['choice']}：{r['rationale']}" for r in design['technology'])
    if discussion:
        lines += ['', '## 讨论结论', '', discussion['rationale']]
        for key, title in [('requirements', '需求'), ('implementation', '实现'), ('workstreams', '分工'), ('verification', '验证')]:
            lines += ['', '### ' + title, '']
            lines.extend('- ' + point for point in discussion['plan'][key])
        for item in discussion['resolutions']:
            lines += ['', '问：' + item['question'], '', '答：' + item['answer']]
    lines += ['', '## AI 代码索引', '', '仓库内路径：' + design['index_path'], '', index_block(design)]
    docs['refactor-design.md'] = '\n'.join(lines) + '\n'
    return docs


def write_documents(folder, discovery, design=None, discussion=None):
    """Publish derived, human-readable copies; frozen records remain the authority."""
    from tempfile import NamedTemporaryFile
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    hashes = {}
    for name, contents in documents(discovery, design, discussion).items():
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False) as out:
            out.write(contents)
        os.replace(out.name, folder / name)
        hashes[name] = hashlib.sha256(contents.encode('utf-8')).hexdigest()
    return hashes


def verify_documents(folder, hashes):
    for name, expected in hashes.items():
        try:
            with (folder / name).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() == expected:
                    continue
        except OSError:
            pass
        raise GateError('Review document changed or is missing: ' + name + '. Review a new proposal before dispatch.',
                        code='refactor_document_changed', next_action='telos_meeting_open')


def check_index(root, design):
    """Check the agreed section and each target in the actual integrated snapshot."""
    index = root / design['index_path']
    def local_file(path):
        return path.resolve().is_relative_to(root.resolve()) and path.is_file()
    error = ''
    try:
        if not local_file(index) or index_block(design) not in index.read_text(encoding='utf-8'):
            error = 'Missing agreed business-flow index section: ' + design['index_path']
        else:
            missing = [p for row in design['code_index'] for p in row['paths'] if not local_file(root / p)]
            if missing:
                error = 'Indexed code or test paths missing: ' + ', '.join(missing[:10])
    except (OSError, UnicodeError, RuntimeError) as exc:
        error = 'Unreadable code index: ' + str(exc)
    return {'id': 'telos_refactor_code_index', 'status': 'failed' if error else 'passed', 'stderr': error}


def check_structure(root, design):
    missing = []
    for row in design['structure']:
        name = row['path']
        path = root / name
        try:
            local = path.resolve().is_relative_to(root.resolve())
            if local and (path.is_dir() if name.endswith('/') else path.exists()):
                continue
        except (OSError, RuntimeError):
            pass
        missing.append(name)
    return {'id': 'telos_refactor_structure', 'status': 'failed' if missing else 'passed',
            'stderr': 'Missing agreed module paths: ' + ', '.join(missing[:10]) if missing else ''}

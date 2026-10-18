"""Explicit per-task inventory, durable archive, and preview-first reclamation.

Never delete by age. A complete archive and clean terminal workspaces are
required; coordinator/session locks serialize checks with bridge mutations.
"""
import hashlib
import os
from pathlib import Path
import shutil
import time


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def size(path):
    total = 0
    if not path.exists():
        return total
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            total += (Path(root) / name).lstat().st_size
    return total


class Storage:
    def __init__(self, bridge):
        self.b = bridge

    def scope(self, ident):
        b = self.b
        b.run_dir(ident)
        project_path = b.project_manager().path(ident)
        if project_path.exists():
            project = b.project_manager().load(ident)
            sessions = [b.read_json(p) for p in (b.STATE / 'runs').glob('*/state.json')
                        if b.read_json(p).get('project_id') == ident and not b.read_json(p).get('notification_only')]
            return project, sessions
        state = b.read_json(b.run_dir(ident) / 'state.json')
        if state.get('project_id'):
            raise b.GateError('Manage the whole parent project, including dependencies and earlier assignments')
        return None, [state]

    def targets(self, ident, project, sessions):
        b = self.b
        paths = []
        for s in sessions:
            sid = s['session_id']
            paths += [b.CACHE / k / sid for k in ('worktrees', 'snapshots', 'homes')]
        if project:
            paths += [b.CACHE / 'projects' / ident, b.CACHE / 'snapshots' / ident]
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(b.CACHE.resolve()):
                raise b.GateError('Unsafe cache path; cleanup refused')
        return list(dict.fromkeys(paths))

    def worktrees(self, paths):
        result = []
        for path in paths:
            if (path / '.git').exists():
                result.append(path)
            elif path.is_dir():
                result += [p.parent for p in path.glob('*/.git')]
        return result

    def inventory(self, ident=None):
        b = self.b
        if ident is None:
            ids = [p.parent.name for p in (b.STATE / 'projects').glob('*/project.json')]
            ids += [p.parent.name for p in (b.STATE / 'runs').glob('*/state.json') if not b.read_json(p).get('project_id')]
            return {'tasks': [self.inventory(i) for i in sorted(ids)], 'cache_root': str(b.CACHE)}
        project, sessions = self.scope(ident)
        paths = self.targets(ident, project, sessions)
        return {'target_id': ident, 'phase': (project or sessions[0])['phase'],
                'bytes': sum(size(p) for p in paths), 'paths': [{'path': str(p), 'bytes': size(p)} for p in paths],
                'archive': str(b.STATE / 'archives' / ident),
                'note': 'Read-only inventory. Cleanup requires a clean terminal task and verified archive.'}

    def safe(self, project, sessions, paths):
        b = self.b
        owner = project or sessions[0]
        if owner.get('phase') != 'accepted' and owner.get('continuation') != 'cancelled':
            raise b.GateError('Archive/cleanup requires accepted or explicitly cancelled task; pause alone is insufficient')
        if project and project['phase'] == 'verifying' or any(s['phase'] in ('running', 'stale') for s in sessions):
            raise b.GateError('Running work cannot be archived or cleaned')
        heads = {}
        for tree in self.worktrees(paths):
            # Include ignored files: dependency caches can contain valuable local
            # files too. Never silently classify them as disposable.
            if b.git(tree, 'status', '--porcelain', '--untracked-files=all', '--ignored'):
                raise b.GateError('Uncommitted/ignored files require explicit preservation before cleanup: ' + str(tree))
            if b.git(tree, 'diff', '--name-only', '--diff-filter=U'):
                raise b.GateError('Unresolved merge prevents cleanup')
            heads[str(tree)] = b.git(tree, 'rev-parse', 'HEAD')
        for state in sessions:
            if state['phase'] == 'accepted' and not b.status(state['session_id'])['accepted']:
                raise b.GateError('Accepted code/evidence changed; preserve and re-review before cleanup')
        if project and project['phase'] == 'accepted':
            integ = project['integration']
            if heads.get(integ['worktree']) != integ['commit'] and not self.archived_commit(project, integ['worktree'], integ['commit']):
                raise b.GateError('Accepted integration changed; cleanup refused')
            packet = b.read_json(b.project_manager().path(project['project_id']).parent / 'integration-review.json')
            if b.digest(packet) != integ['packet_id']:
                raise b.GateError('Integration evidence changed')
        return heads

    def evidence(self, ident, sessions):
        b = self.b
        paths = [b.run_dir(s['session_id']) for s in sessions]
        if b.project_manager().path(ident).exists():
            paths += [b.run_dir(ident), b.project_manager().path(ident).parent]
        # State/lock/outbox are mutable; immutable contracts and evidence only.
        return {str(p.relative_to(b.STATE)): file_hash(p) for root in paths for p in root.glob('*.json')
                if p.name not in ('state.json', 'project.json') and not p.name.endswith('-error.json')}

    def verify_archive(self, ident):
        b = self.b
        folder = b.STATE / 'archives' / ident
        manifest = b.read_json(folder / 'manifest.json')
        for path, expected in manifest['files'].items():
            candidate = folder / path
            if not candidate.resolve().is_relative_to(folder.resolve()) or candidate.is_symlink() or file_hash(candidate) != expected:
                raise b.GateError('Archive changed; cleanup/archived acceptance refused')
        return manifest

    def archive(self, ident, project, sessions, paths, heads):
        b = self.b
        destination = b.STATE / 'archives' / ident
        evidence = self.evidence(ident, sessions)
        if destination.exists():
            manifest = self.verify_archive(ident)
            resuming = (project or sessions[0]).get('archived', {}).get('manifest_hash') == b.digest(manifest)
            heads_match = manifest['heads'] == heads or (resuming and all(manifest['heads'].get(k) == v for k, v in heads.items()))
            if not heads_match or manifest['evidence'] != evidence:
                raise b.GateError('Existing archive differs from current work; preserve it and inspect manually')
            return manifest
        tmp = destination.with_name(ident + '.tmp-' + str(time.time_ns()))
        tmp.mkdir(parents=True, mode=0o700)
        repo = (project or sessions[0])['repo']
        refs = []
        for index, commit in enumerate(sorted(set(heads.values()) | {s.get('submitted_commit') for s in sessions if s.get('submitted_commit')})):
            ref = f'refs/exam-loop/archive/{ident}/{index}'
            b.git(repo, 'update-ref', ref, commit); refs.append(ref)
        if not refs:
            raise b.GateError('No code commits to archive')
        b.git(repo, 'bundle', 'create', str(tmp / 'code.bundle'), *refs)
        b.git(repo, 'bundle', 'verify', str(tmp / 'code.bundle'))
        for relative in evidence:
            target = tmp / 'evidence' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(b.STATE / relative, target)
        b.write_json(tmp / 'task-state.json', {'project': project, 'sessions': sessions})
        files = {str(p.relative_to(tmp)): file_hash(p) for p in tmp.rglob('*') if p.is_file()}
        manifest = {'target_id': ident, 'created_at': time.time(), 'heads': heads, 'evidence': evidence,
                    'refs': refs, 'files': files, 'repo': repo}
        b.write_json(tmp / 'manifest.json', manifest)
        os.replace(tmp, destination)
        return self.verify_archive(ident)

    def manage(self, action='inventory', target_id=None, dry_run=True, caller=None):
        b = self.b
        if action == 'inventory':
            return self.inventory(target_id)
        if action not in ('archive', 'cleanup') or not target_id or type(dry_run) is not bool:
            raise b.GateError('Use inventory/archive/cleanup with target_id; dry_run defaults to true')
        with b.project_manager().lock():
            project, sessions = self.scope(target_id)
            owner = (project or sessions[0]).get('owner')
            if owner and owner['thread_id'] != caller:
                raise b.GateError('Only original supervisor may archive or clean this task')
            paths = self.targets(target_id, project, sessions)
            heads = self.safe(project, sessions, paths)
            if dry_run:
                return {**self.inventory(target_id), 'dry_run': True, 'eligible': True, 'action': action,
                        'preserve': 'Git bundle, immutable review/check evidence, state and experience records'}
            manifest = self.archive(target_id, project, sessions, paths, heads)
            if action == 'cleanup':
                # Recheck after archive creation, before any removal.
                if self.safe(project, sessions, paths) != heads:
                    raise b.GateError('Workspace changed during archiving')
                for s in sessions:
                    with b.locked(s['session_id']) as root:
                        current, _ = b.load_session(root)
                        current['archived'] = {'target_id': target_id, 'commit': heads.get(current.get('worktree')), 'manifest_hash': b.digest(manifest),
                                               'at': time.time()}
                        b.write_json(root / 'state.json', current)
                if project:
                    project['archived'] = {'target_id': target_id, 'at': time.time(), 'manifest_hash': b.digest(manifest)}
                    b.project_manager().save(project)
                for tree in self.worktrees(paths):
                    b.git(tree, 'worktree', 'remove', str(tree))  # never --force
                for path in paths:
                    if path.exists():
                        shutil.rmtree(path)
            return {'target_id': target_id, 'action': action, 'dry_run': False,
                    'archive': str(b.STATE / 'archives' / target_id), 'preserved_commits': sorted(set(manifest['heads'].values()))}

    def archived_commit(self, state, worktree, expected):
        if not state.get('archived') or Path(worktree).exists():
            return False
        manifest = self.verify_archive(state['archived']['target_id'])
        if self.b.digest(manifest) != state['archived']['manifest_hash']:
            raise self.b.GateError('Archive manifest changed')
        return manifest['heads'].get(worktree) == expected

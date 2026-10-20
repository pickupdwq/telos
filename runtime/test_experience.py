import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from experience import Experience
from projects import Projects
from test_projects import ProjectTests, b


class ExperienceTests(ProjectTests):
    def accepted_project(self, feedback=None):
        pid = self.create([self.task('a')]); self.start(pid)
        s = self.child(pid, 'a')
        if feedback:
            s['public_repair_feedback'] = feedback
            b.write_json(b.run_dir(s['session_id']) / 'state.json', s)
        self.submit(pid, 'a')
        self.assertEqual(b.experience_manager().lookup(self.repo, ['a.py']), [])
        b.project_manager().integrate(pid); review = self.check_sync(pid)
        b.project_manager().finish(pid, review['packet_id'], self.scores(), [], 'Code and integrated checks reviewed')
        return pid

    def test_auto_accumulates_and_injects_verified_repair_advice(self):
        advice = 'Keep the public function name stable and handle empty input explicitly.'
        self.accepted_project([advice])
        records = b.experience_manager().lookup(self.repo, ['a.py'])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['public_lessons'], [advice])
        pid = self.create([self.task('a')]); self.start(pid)
        s = self.child(pid, 'a')
        c = b.read_json(b.run_dir(s['session_id']) / 'contract.json')
        self.assertIn(advice, c['experience_context'])
        self.assertEqual(c['experience_ids'], [records[0]['id']])
        self.assertNotIn(advice, json.dumps(b.public_exam(c)))

    def test_failed_and_unintegrated_work_is_not_reused(self):
        pid = self.create([self.task('a')]); self.start(pid); self.submit(pid, 'a', passed=False)
        self.assertEqual(b.experience_manager().lookup(self.repo), [])
        records = b.experience_manager().lookup(self.repo, include_candidates=True)
        self.assertEqual(records[0]['outcome'], 'needs_repair')
        self.assertIn('executable_checks', records[0]['failure_categories'])

    def test_no_private_answers_checks_or_credentials_stored(self):
        self.accepted_project([self.q[0]['reference'], self.checks[0]['argv'][2], 'api_key=SUPERSECRET', 'Useful public repair advice'])
        folder = b.STATE / 'experience'
        text = '\n'.join(p.read_text() for p in folder.glob('*.json'))
        for secret in [self.q[0]['reference'], self.checks[0]['argv'][2], 'SUPERSECRET']:
            self.assertNotIn(secret, text)
        self.assertIn('Useful public repair advice', text)

    def test_repository_scope_path_matching_and_disable(self):
        self.accepted_project(['Preserve function compatibility.'])
        self.assertEqual(b.experience_manager().lookup(self.repo / 'other'), [])
        self.assertEqual(b.experience_manager().lookup(self.repo, ['unrelated.py']), [])
        entry = b.experience_manager().lookup(self.repo, ['a.py'])[0]
        b.experience_manager().forget(entry['id'])
        self.assertEqual(b.experience_manager().lookup(self.repo, ['a.py']), [])

    def test_can_opt_out_and_tampered_memory_is_ignored(self):
        self.accepted_project(['Check edge cases.'])
        pid = self.create([self.task('a')], use_experience=False); self.start(pid)
        s = self.child(pid, 'a'); c = b.read_json(b.run_dir(s['session_id']) / 'contract.json')
        self.assertEqual(c['experience_context'], '')
        entry = b.experience_manager().lookup(self.repo)[0]
        path = b.STATE / 'experience' / (entry['id'] + '.json')
        entry['public_lessons'] = ['unreviewed alteration']
        b.write_json(path, entry)
        self.assertEqual(b.experience_manager().lookup(self.repo), [])

    def test_public_feedback_is_captured_only_after_initial_turn(self):
        pid = self.create([self.task('a')]); self.start(pid)
        s = self.child(pid, 'a'); c = b.read_json(b.run_dir(s['session_id']) / 'contract.json')
        s['turns'] = 0
        b.experience_manager().capture_feedback(s, c, 'Initial assignment')
        self.assertNotIn('public_repair_feedback', s)
        s['turns'] = 1
        b.experience_manager().capture_feedback(s, c, 'Maintain the documented error behavior.')
        b.experience_manager().capture_feedback(s, c, self.q[0]['reference'])
        self.assertEqual(s['public_repair_feedback'], ['Maintain the documented error behavior.'])

    def test_supervisor_lessons_are_validated_and_recorded_with_acceptance(self):
        from test_telos import BridgeTests
        sid = b.create_session(repo=str(self.repo), task='Improve addition', questions=self.q, checks=self.checks, assessment_mode='closed_book')['session_id']
        ready = BridgeTests.ready_review(self, sid)
        with self.assertRaises(b.GateError):
            b.grade(sid, ready['packet_id'], self.scores(), [], public_lessons=[self.q[0]['reference']])
        lesson = 'Preserve the public add function and Python numeric semantics.'
        result = b.grade(sid, ready['packet_id'], self.scores(), [], public_lessons=[lesson])
        self.assertTrue(result['accepted'])
        self.assertEqual(b.experience_manager().lookup(self.repo)[0]['public_lessons'], [lesson])


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.repo = root / 'repo'
        self.repo.mkdir()
        self.state = root / 'state'
        self.bridge = SimpleNamespace(
            STATE=self.state, GateError=b.GateError, digest=b.digest,
            read_json=b.read_json, write_json=b.write_json,
            project_manager=lambda: SimpleNamespace(overlap=Projects.overlap))
        self.experience = Experience(self.bridge)

    def seed(self, session_id, created_at, paths=('a.py',), lessons=(), *, repo=None,
             reusable=True, disabled=False, packet_id=None):
        packet_id = packet_id or b.digest({'packet': session_id, 'created_at': created_at})
        ident = b.digest({'record': session_id, 'packet': packet_id})
        entry = {'id': ident, 'repo': str(Path(self.repo if repo is None else repo).resolve()),
                 'session_id': session_id, 'project_id': None,
                 'source_commit': '0' * 40, 'source_packet_id': packet_id,
                 'created_at': created_at, 'task': 'Task ' + session_id,
                 'paths': list(paths), 'changed_files': list(paths),
                 'public_lessons': list(lessons), 'outcome': 'accepted',
                 'reusable': reusable, 'disabled': disabled}
        entry['content_hash'] = b.digest({k: v for k, v in entry.items() if k != 'content_hash'})
        b.write_json(self.state / 'experience' / (entry['id'] + '.json'), entry)
        return entry

    def test_lookup_checks_only_ranked_matching_sources_until_limit(self):
        lesson = self.seed('lesson', 30, lessons=['Keep the API stable.'])
        newest = self.seed('newest', 40)
        older = self.seed('older', 20)
        unchecked = self.seed('unchecked', 10)
        for i in range(40):
            self.seed('other-%02d' % i, 100 + i, paths=['other.py'], lessons=['Not relevant.'])
        self.seed('disabled', 200, lessons=['Not relevant.'], disabled=True)
        tampered = self.seed('tampered', 201, lessons=['Not relevant.'])
        tampered['public_lessons'] = ['Changed after hashing.']
        b.write_json(self.state / 'experience' / (tampered['id'] + '.json'), tampered)
        self.seed('foreign', 202, lessons=['Not relevant.'], repo=self.repo / 'other')
        calls = []
        with patch.object(self.experience, 'source_valid',
                          side_effect=lambda e: calls.append(e['session_id']) or True):
            records = self.experience.lookup(self.repo, ['a.py'], limit=3)
        self.assertEqual([e['session_id'] for e in records], ['lesson', 'newest', 'older'])
        self.assertEqual(calls, ['lesson', 'newest', 'older'])
        self.assertNotIn(unchecked['session_id'], calls)

    def test_lookup_rejects_source_then_reuses_session_before_stopping(self):
        self.seed('session-a', 40, lessons=['Stale lesson.'], packet_id='bad')
        self.seed('session-a', 30, packet_id='good')
        self.seed('session-b', 20, packet_id='b-good')
        self.seed('session-c', 10, packet_id='c-good')
        calls = []

        def source_valid(entry):
            calls.append((entry['session_id'], entry['source_packet_id']))
            return entry['source_packet_id'] != 'bad'

        with patch.object(self.experience, 'source_valid', side_effect=source_valid):
            records = self.experience.lookup(self.repo, ['a.py'], limit=2)
        self.assertEqual([(e['session_id'], e['source_packet_id']) for e in records],
                         [('session-a', 'good'), ('session-b', 'b-good')])
        self.assertEqual(calls, [('session-a', 'bad'), ('session-a', 'good'), ('session-b', 'b-good')])

    def test_lookup_candidates_skip_provenance_checks(self):
        candidate = self.seed('candidate', 10, lessons=['Unverified candidate.'], reusable=False)
        with patch.object(self.experience, 'source_valid',
                          side_effect=AssertionError('candidate was checked')):
            records = self.experience.lookup(self.repo, ['a.py'], include_candidates=True)
        self.assertEqual([e['session_id'] for e in records], ['candidate'])
        self.assertEqual(records[0], candidate)
        self.assertFalse(records[0]['reusable'])


class LookupRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'state'
        self.bridge = SimpleNamespace(
            STATE=self.state, GateError=b.GateError, digest=b.digest,
            read_json=b.read_json, write_json=b.write_json,
            project_manager=lambda: SimpleNamespace(overlap=Projects.overlap))
        self.experience = Experience(self.bridge)
        self.folder = self.state / 'experience'

    def path(self, entry):
        return self.folder / (entry['id'] + '.json')

    def seed(self, session_id, created_at, paths=('a.py',), lessons=(), *,
             repo=None, reusable=True, disabled=False, packet_id=None):
        packet_id = packet_id or b.digest({'packet': session_id, 'created_at': created_at})
        ident = b.digest({'record': session_id, 'packet': packet_id})
        entry = {'id': ident, 'repo': str(Path(self.repo if repo is None else repo).resolve()),
                 'session_id': session_id, 'project_id': None,
                 'source_commit': '0' * 40, 'source_packet_id': packet_id,
                 'created_at': created_at, 'task': 'Task ' + session_id,
                 'paths': list(paths), 'changed_files': list(paths),
                 'public_lessons': list(lessons), 'outcome': 'accepted',
                 'reusable': reusable, 'disabled': disabled}
        entry['content_hash'] = b.digest({k: v for k, v in entry.items() if k != 'content_hash'})
        b.write_json(self.path(entry), entry)
        return entry

    def rewrite(self, entry, path=None, valid=True):
        if valid:
            entry['content_hash'] = b.digest({k: v for k, v in entry.items() if k != 'content_hash'})
        b.write_json(path or self.path(entry), entry)

    def raw(self, name, text):
        path = self.folder / name
        path.write_text(text)
        return path

    def test_valid_neighbor_survives_truncated_array_and_missing_fields(self):
        good = self.seed('good', 5, lessons=['Keep the API stable.'])
        self.raw('truncated.json', '{"id": "cut off')
        self.raw('array.json', '[]')
        self.raw('null.json', 'null')
        missing = self.seed('missing-repo', 50)
        missing.pop('repo')
        self.rewrite(missing)
        tampered = self.seed('tampered', 60)
        tampered['task'] = 'Changed after hashing.'
        self.rewrite(tampered, valid=False)
        with patch.object(self.experience, 'source_valid', return_value=True) as source_valid:
            records = self.experience.lookup(self.repo, ['a.py'])
        self.assertEqual([e['session_id'] for e in records], ['good'])
        self.assertEqual(records[0], good)
        source_valid.assert_called_once()

    def test_strict_typing_skips_only_ill_shaped_records(self):
        good = self.seed('good', 5)
        mutations = {
            'reusable-int': lambda e: e.update(reusable=1),
            'disabled-string': lambda e: e.update(disabled='false'),
            'paths-mixed': lambda e: e.update(paths=['a.py', 7]),
            'paths-string': lambda e: e.update(paths='a.py'),
            'lessons-string': lambda e: e.update(public_lessons='lesson'),
            'lessons-mixed': lambda e: e.update(public_lessons=['ok', None]),
            'created-bool': lambda e: e.update(created_at=True),
            'created-nan': lambda e: e.update(created_at=float('nan')),
            'created-string': lambda e: e.update(created_at='10'),
            'task-missing': lambda e: e.pop('task'),
            'repo-int': lambda e: e.update(repo=7),
            'commit-int': lambda e: e.update(source_commit=7),
            'packet-int': lambda e: e.update(source_packet_id=7),
            'session-int': lambda e: e.update(session_id=7),
            'id-int': lambda e: e.update(id=7),
            'project-int': lambda e: e.update(project_id=7),
        }
        for offset, (name, mutate) in enumerate(mutations.items(), start=100):
            entry = self.seed(name, offset)
            path = self.path(entry)
            mutate(entry)
            self.rewrite(entry, path=path)
        legacy = self.seed('legacy', 10 ** 30)
        del legacy['disabled']
        legacy['extra_metadata'] = {'kept': True}
        self.rewrite(legacy)
        with patch.object(self.experience, 'source_valid', return_value=True):
            records = self.experience.lookup(self.repo, ['a.py'])
        self.assertEqual([e['session_id'] for e in records], ['legacy', 'good'])
        self.assertNotIn('disabled', records[0])
        self.assertEqual(records[0]['extra_metadata'], {'kept': True})
        self.assertEqual(records[0]['created_at'], 10 ** 30)

    def test_expected_file_and_decoding_failures_are_isolated(self):
        good = self.seed('good', 1)
        gone = self.seed('gone', 30)
        gone_path = self.path(gone)
        real_read = b.read_json

        def read_json(path):
            if Path(path) == gone_path:
                raise FileNotFoundError(str(path))
            return real_read(path)

        (self.folder / 'utf8.json').write_bytes(b'\xff\xfe\x00broken')
        (self.folder / 'directory.json').mkdir()
        with patch.object(self.bridge, 'read_json', side_effect=read_json), \
                patch.object(self.experience, 'source_valid', return_value=True):
            records = self.experience.lookup(self.repo, ['a.py'])
        self.assertEqual([e['session_id'] for e in records], ['good'])
        self.assertEqual(records[0], good)

    def test_ranking_and_lazy_provenance_ignore_damaged_records(self):
        self.seed('lesson', 30, lessons=['Reuse the reviewed approach.'])
        self.seed('newest', 40)
        older = self.seed('older', 20)
        self.seed('rejected', 35, lessons=['Stale lesson.'], packet_id='bad')
        self.raw('damaged.json', '{"repo": ')
        calls = []

        def source_valid(entry):
            calls.append(entry['session_id'])
            return entry['session_id'] != 'rejected'

        with patch.object(self.experience, 'source_valid', side_effect=source_valid):
            records = self.experience.lookup(self.repo, ['a.py'], limit=2)
        self.assertEqual([e['session_id'] for e in records], ['lesson', 'newest'])
        self.assertEqual(calls, ['rejected', 'lesson', 'newest'])
        self.assertNotIn(older['session_id'], calls)

    def test_candidates_mode_excludes_damaged_records_without_provenance(self):
        candidate = self.seed('candidate', 10, lessons=['Unverified candidate.'], reusable=False)
        self.raw('broken.json', '{')
        self.raw('scalar.json', '"just a string"')
        with patch.object(self.experience, 'source_valid',
                          side_effect=AssertionError('candidate was checked')):
            records = self.experience.lookup(self.repo, ['a.py'], include_candidates=True)
        self.assertEqual(records, [candidate])
        self.assertFalse(records[0]['reusable'])

    def test_context_renders_only_valid_neighbors(self):
        good = self.seed('good', 5, lessons=['Preserve the public API.'])
        disabled = self.seed('disabled', 50, lessons=['Disabled lesson.'], disabled=True)
        self.raw('damaged.json', '{"task": "render this damaged task"}')
        with patch.object(self.experience, 'source_valid', return_value=True) as source_valid:
            text, ids = self.experience.context(self.repo, ['a.py'])
        self.assertEqual(ids, [good['id']])
        self.assertIn('Preserve the public API.', text)
        self.assertNotIn('render this damaged task', text)
        self.assertNotIn(disabled['id'][:12], text)
        source_valid.assert_called_once()

    def test_lookup_and_context_never_rewrite_or_quarantine_records(self):
        good = self.seed('good', 5)
        tampered = self.seed('tampered', 10)
        tampered['task'] = 'Tampered.'
        self.rewrite(tampered, valid=False)
        self.raw('broken.json', 'not json at all')
        before = {p.name: p.read_bytes() for p in self.folder.glob('*.json')}
        with patch.object(self.experience, 'source_valid', return_value=True):
            self.experience.lookup(self.repo, ['a.py'])
            self.experience.context(self.repo, ['a.py'])
        after = {p.name: p.read_bytes() for p in self.folder.glob('*.json')}
        self.assertEqual(after, before)
        self.assertIn(self.path(good).name, after)
        self.assertIn(self.path(tampered).name, after)

    def test_unexpected_errors_remain_visible(self):
        self.seed('good', 5)
        real_read = b.read_json
        boom = self.folder / 'boom.json'
        boom.write_text('{}')

        def read_json(path):
            if Path(path) == boom:
                raise RuntimeError('unexpected reader failure')
            return real_read(path)

        with patch.object(self.bridge, 'read_json', side_effect=read_json):
            with self.assertRaises(RuntimeError):
                self.experience.lookup(self.repo, ['a.py'])
        with patch.object(self.experience, 'source_valid', side_effect=RuntimeError('provenance bug')):
            with self.assertRaises(RuntimeError):
                self.experience.lookup(self.repo, ['a.py'])
        blocked = self.root / 'blocked-state'
        blocked.write_text('')
        with patch.object(self.bridge, 'STATE', blocked):
            with self.assertRaises(OSError):
                self.experience.lookup(self.repo, ['a.py'])


class SourceIdentityTests(unittest.TestCase):
    """Pure record/source consistency checks against explicit status fixtures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'state'
        self.folder = self.state / 'experience'
        self.statuses = {}
        self.projects = {}
        self.heads = {}
        self.dirty = {}
        self.archived = set()
        manager = SimpleNamespace(overlap=Projects.overlap, load=self.load_project, path=self.project_path)
        storage = SimpleNamespace(archived_commit=self.archived_commit)
        self.bridge = SimpleNamespace(
            STATE=self.state, GateError=b.GateError, digest=b.digest,
            read_json=b.read_json, write_json=b.write_json,
            status=self.status, git=self.git,
            project_manager=lambda: manager, storage_manager=lambda: storage)
        self.experience = Experience(self.bridge)

    def status(self, session_id):
        return self.statuses[session_id]

    def load_project(self, project_id):
        return self.projects[project_id]

    def project_path(self, project_id):
        return self.state / 'projects' / project_id / 'project.json'

    def git(self, tree, *args):
        if args == ('rev-parse', 'HEAD'):
            return self.heads.get(str(tree), 'f' * 40)
        if args == ('status', '--porcelain'):
            return self.dirty.get(str(tree), '')
        raise AssertionError('unexpected git arguments: %r' % (args,))

    def archived_commit(self, project, worktree, expected):
        return (str(worktree), expected) in self.archived

    def accepted(self, session_id, commit, packet_id, project_id=None):
        self.statuses[session_id] = {
            'accepted': True, 'project_id': project_id,
            'acceptance': {'commit': commit, 'packet_id': packet_id}}

    def add_project(self, project_id, *, phase='accepted', integration_commit='i' * 40,
                    packet=None, worktree=None):
        worktree = worktree or str(self.root / 'trees' / project_id)
        packet = {'project': project_id, 'accepted': True} if packet is None else packet
        path = self.project_path(project_id)
        b.write_json(path, {'project_id': project_id})
        b.write_json(path.parent / 'integration-review.json', packet)
        self.projects[project_id] = {
            'project_id': project_id, 'phase': phase,
            'integration': {'commit': integration_commit,
                            'packet_id': b.digest(packet), 'worktree': worktree}}
        self.heads[str(worktree)] = integration_commit
        return self.projects[project_id]['integration']

    def seed(self, session_id, created_at, *, project_id=None, project_commit=None,
             source_commit='a' * 40, packet_id='packet-a', paths=('a.py',), lessons=()):
        ident = b.digest({'identity': session_id, 'packet': packet_id, 'created': created_at})
        entry = {'id': ident, 'repo': str(self.repo), 'session_id': session_id,
                 'project_id': project_id, 'source_commit': source_commit,
                 'source_packet_id': packet_id, 'created_at': created_at,
                 'task': 'Task ' + session_id, 'paths': list(paths),
                 'changed_files': list(paths), 'public_lessons': list(lessons),
                 'outcome': 'accepted', 'reusable': True, 'disabled': False}
        if project_commit is not None:
            entry['project_commit'] = project_commit
        entry['content_hash'] = b.digest({k: v for k, v in entry.items() if k != 'content_hash'})
        b.write_json(self.folder / (ident + '.json'), entry)
        return entry

    def rewrite(self, entry):
        entry['content_hash'] = b.digest({k: v for k, v in entry.items() if k != 'content_hash'})
        b.write_json(self.folder / (entry['id'] + '.json'), entry)

    def test_standalone_matching_commit_and_packet_is_usable(self):
        self.accepted('standalone', 'a' * 40, 'packet-a')
        record = self.seed('standalone', 10)
        self.assertTrue(self.experience.source_valid(record))
        self.assertEqual(self.experience.lookup(self.repo, ['a.py']), [record])

    def test_mismatched_commit_or_packet_is_not_usable(self):
        self.accepted('commit-mismatch', 'a' * 40, 'packet-a')
        self.seed('commit-mismatch', 30, source_commit='d' * 40, packet_id='packet-a')
        self.accepted('packet-mismatch', 'b' * 40, 'packet-b')
        self.seed('packet-mismatch', 20, source_commit='b' * 40, packet_id='packet-old')
        self.accepted('good', 'c' * 40, 'packet-c')
        good = self.seed('good', 10, source_commit='c' * 40, packet_id='packet-c')
        records = self.experience.lookup(self.repo, ['a.py'], limit=3)
        self.assertEqual([e['session_id'] for e in records], ['good'])
        self.assertEqual(records[0], good)

    def test_missing_commit_evidence_is_never_accepted(self):
        self.accepted('record-missing', 'a' * 40, 'packet-a')
        record = self.seed('record-missing', 30)
        record.pop('source_commit')
        self.rewrite(record)
        self.assertFalse(self.experience.source_valid(record))
        self.assertEqual(self.experience.lookup(self.repo, ['a.py']), [])
        self.statuses['status-missing'] = {
            'accepted': True, 'project_id': None,
            'acceptance': {'packet_id': 'packet-b'}}
        status_record = self.seed('status-missing', 20, source_commit='b' * 40, packet_id='packet-b')
        self.assertFalse(self.experience.source_valid(status_record))

    def test_project_source_requires_matching_project_membership(self):
        project = self.add_project('project-a')
        self.add_project('project-b', integration_commit='j' * 40)
        self.accepted('project-good', 'c' * 40, 'packet-good', project_id='project-a')
        good = self.seed('project-good', 50, project_id='project-a', source_commit='c' * 40,
                         packet_id='packet-good', project_commit=project['commit'])
        self.assertTrue(self.experience.source_valid(good))
        self.assertEqual(self.experience.lookup(self.repo, ['a.py']), [good])

        for name in ('omitted', 'null', 'empty', 'foreign'):
            self.accepted('child-' + name, 'c' * 40, 'packet-' + name, project_id='project-a')
        omitted = self.seed('child-omitted', 40, project_id='project-a', source_commit='c' * 40,
                            packet_id='packet-omitted', project_commit=project['commit'])
        omitted.pop('project_id')
        self.rewrite(omitted)
        variants = {'omitted': omitted,
                    'null': self.seed('child-null', 30, project_id=None, source_commit='c' * 40,
                                      packet_id='packet-null', project_commit=project['commit']),
                    'empty': self.seed('child-empty', 20, project_id='', source_commit='c' * 40,
                                       packet_id='packet-empty', project_commit=project['commit']),
                    'foreign': self.seed('child-foreign', 10, project_id='project-b', source_commit='c' * 40,
                                         packet_id='packet-foreign', project_commit='j' * 40)}
        for name, entry in variants.items():
            with self.subTest(membership=name):
                self.assertFalse(self.experience.source_valid(entry))
        self.assertEqual(self.experience.lookup(self.repo, ['a.py']), [good])

    def test_standalone_source_does_not_accept_project_markers(self):
        self.add_project('project-a')
        for session_id, commit, packet_id in (
                ('plain-absent', 'a' * 40, 'packet-absent'),
                ('plain-none', 'b' * 40, 'packet-none'),
                ('plain-empty', 'c' * 40, 'packet-empty')):
            self.accepted(session_id, commit, packet_id)
        absent = self.seed('plain-absent', 30, source_commit='a' * 40, packet_id='packet-absent')
        absent.pop('project_id')
        self.rewrite(absent)
        self.seed('plain-none', 20, source_commit='b' * 40, packet_id='packet-none')
        self.seed('plain-empty', 10, source_commit='c' * 40, packet_id='packet-empty',
                  project_id='')
        self.accepted('plain-child', 'd' * 40, 'packet-child')
        foreign = self.seed('plain-child', 40, project_id='project-a', source_commit='d' * 40,
                            packet_id='packet-child', project_commit='i' * 40)
        records = self.experience.lookup(self.repo, ['a.py'])
        self.assertEqual([e['session_id'] for e in records],
                         ['plain-absent', 'plain-none', 'plain-empty'])
        self.assertFalse(self.experience.source_valid(foreign))

    def test_live_and_archived_project_records_remain_usable(self):
        live = self.add_project('live-project')
        self.accepted('live', 'a' * 40, 'packet-live', project_id='live-project')
        live_record = self.seed('live', 30, project_id='live-project', source_commit='a' * 40,
                                packet_id='packet-live', project_commit=live['commit'])
        self.assertTrue(self.experience.source_valid(live_record))
        self.dirty[str(live['worktree'])] = ' M changed.py'
        self.assertFalse(self.experience.source_valid(live_record))
        self.dirty[str(live['worktree'])] = ''

        archived = self.add_project('archived-project', integration_commit='j' * 40)
        self.archived.add((str(archived['worktree']), 'j' * 40))
        self.heads[str(archived['worktree'])] = 'e' * 40
        self.accepted('archived', 'b' * 40, 'packet-archived', project_id='archived-project')
        archived_record = self.seed('archived', 20, project_id='archived-project',
                                    source_commit='b' * 40, packet_id='packet-archived',
                                    project_commit=archived['commit'])
        self.assertTrue(self.experience.source_valid(archived_record))
        self.assertEqual({e['session_id'] for e in self.experience.lookup(self.repo)},
                         {'live', 'archived'})

    def test_project_acceptance_evidence_is_still_required(self):
        pending = self.add_project('pending-project', phase='running')
        self.accepted('pending', 'a' * 40, 'packet-pending', project_id='pending-project')
        pending_record = self.seed('pending', 50, project_id='pending-project',
                                   source_commit='a' * 40, packet_id='packet-pending',
                                   project_commit=pending['commit'])
        self.assertFalse(self.experience.source_valid(pending_record))

        changed = self.add_project('changed-project', integration_commit='j' * 40)
        self.accepted('changed', 'b' * 40, 'packet-changed', project_id='changed-project')
        changed_record = self.seed('changed', 40, project_id='changed-project',
                                   source_commit='b' * 40, packet_id='packet-changed',
                                   project_commit='k' * 40)
        self.assertFalse(self.experience.source_valid(changed_record))

        altered = self.add_project('altered-project', integration_commit='l' * 40)
        b.write_json(self.project_path('altered-project').parent / 'integration-review.json',
                     {'project': 'altered-project', 'accepted': False})
        self.accepted('altered', 'c' * 40, 'packet-altered', project_id='altered-project')
        altered_record = self.seed('altered', 30, project_id='altered-project',
                                   source_commit='c' * 40, packet_id='packet-altered',
                                   project_commit=altered['commit'])
        self.assertFalse(self.experience.source_valid(altered_record))

    def test_mismatched_high_rank_is_skipped_for_valid_fallback(self):
        self.accepted('stale', 'a' * 40, 'packet-stale')
        self.seed('stale', 40, source_commit='d' * 40, packet_id='packet-stale')
        self.accepted('bad-packet', 'b' * 40, 'packet-new')
        self.seed('bad-packet', 35, source_commit='b' * 40, packet_id='packet-old')
        self.accepted('valid-newer', 'c' * 40, 'packet-valid-newer')
        valid_newer = self.seed('valid-newer', 20, source_commit='c' * 40,
                                packet_id='packet-valid-newer')
        self.accepted('valid-older', 'e' * 40, 'packet-valid-older')
        valid_older = self.seed('valid-older', 10, source_commit='e' * 40,
                                packet_id='packet-valid-older')
        records = self.experience.lookup(self.repo, ['a.py'], limit=2)
        self.assertEqual([e['session_id'] for e in records], ['valid-newer', 'valid-older'])
        self.assertEqual(records, [valid_newer, valid_older])

    def test_context_excludes_mismatched_identity(self):
        self.accepted('stale', 'a' * 40, 'packet-stale')
        self.seed('stale', 40, source_commit='d' * 40, packet_id='packet-stale',
                  lessons=['Stale lesson must not render.'])
        self.accepted('trusted', 'b' * 40, 'packet-trusted')
        trusted = self.seed('trusted', 10, source_commit='b' * 40, packet_id='packet-trusted',
                            lessons=['Trusted lesson remains visible.'])
        text, ids = self.experience.context(self.repo, ['a.py'])
        self.assertEqual(ids, [trusted['id']])
        self.assertIn('Trusted lesson remains visible.', text)
        self.assertNotIn('Stale lesson must not render.', text)
        self.assertNotIn('Task stale', text)

    def test_fresh_reads_follow_current_source_status_without_rewriting(self):
        self.accepted('source', 'a' * 40, 'packet-a')
        record = self.seed('source', 10)
        path = self.folder / (record['id'] + '.json')
        before = path.read_bytes()
        self.assertEqual(self.experience.lookup(self.repo), [record])
        self.statuses['source']['acceptance']['commit'] = 'b' * 40
        self.assertEqual(self.experience.lookup(self.repo), [])
        self.assertEqual(self.experience.context(self.repo, ['a.py']), ('', []))
        self.statuses['source']['acceptance']['commit'] = 'a' * 40
        self.assertEqual(self.experience.lookup(self.repo), [record])
        self.assertEqual(path.read_bytes(), before)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(case(name) for case in (ExperienceTests, LookupTests, LookupRobustnessTests,
                                                      SourceIdentityTests)
                              for name in case.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()

"""Cross-repository lessons require reviewed, still-valid original evidence."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

from test_telos import b, BridgeTests
from test_projects import ProjectTests


LESSON = {'problem': 'Signed input was accidentally rejected.', 'cause': 'Validation assumed positive values.',
          'resolution': 'Preserve signed numeric input and check negative and zero cases.',
          'applicability': 'Numeric APIs whose requirements permit signed values.',
          'limitations': 'Do not apply to fields explicitly constrained to positive values.',
          'required_tags': ['numeric-input'], 'roles': ['developer', 'tester']}


class SharedExperienceTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    scores = BridgeTests.scores

    def source(self, passed=True):
        sid = b.create_session(repo=str(self.repo), task='Preserve numeric input behavior.',
            questions=self.q, checks=self.checks)['session_id']
        ready = BridgeTests.ready_review(self, sid, 'passed' if passed else 'failed')
        b.grade(sid, ready['packet_id'], self.scores(), [], public_lessons=[LESSON['resolution']])
        records = b.experience_manager().lookup(self.repo, include_candidates=True)
        return sid, records[0]

    def publish(self, entry, lesson=None, caller=None):
        return b.call_tool('telos_experience_publish', {'experience_id': entry['id'],
            'lesson': copy.deepcopy(LESSON if lesson is None else lesson),
            'rationale': 'Reviewed the accepted repair; the stated numeric preconditions are reusable.'},
            {'threadId': caller} if caller else None)

    def search(self, **kw):
        return b.call_tool('telos_experience_search', {'repo': str(self.repo / 'project-b'),
            'tags': ['numeric-input'], **kw})['experiences']

    def test_publication_tool_exists(self):
        self.assertIn('telos_experience_publish', [t['name'] for t in b.TOOLS])

    def test_explicit_publication_crosses_repositories_without_copying_source(self):
        sid, entry = self.source()
        raw = (b.STATE / 'experience' / (entry['id'] + '.json')).read_bytes()
        self.assertEqual(self.search(), [])
        published = self.publish(entry)
        records = self.search(role_profile='designer')  # roles rank; they do not hide relevant facts
        self.assertEqual([r['id'] for r in records], [published['experience_id']])
        self.assertEqual(records[0]['scope'], 'shared')
        self.assertEqual(records[0]['lesson'], LESSON)
        output = json.dumps(records)
        for private in [str(self.repo), sid, entry['task'], self.q[0]['reference'], self.checks[0]['argv'][2]]:
            self.assertNotIn(private, output)
        self.assertEqual(raw, (b.STATE / 'experience' / (entry['id'] + '.json')).read_bytes())

    def test_matching_requires_all_conditions_and_can_be_disabled(self):
        _, entry = self.source()
        self.publish(entry, {**LESSON, 'required_tags': ['numeric-input', 'python']})
        self.assertEqual(self.search(), [])
        self.assertEqual(len(self.search(tags=['python', 'numeric-input'])), 1)
        self.assertEqual(self.search(tags=['python', 'numeric-input'], include_shared=False), [])
        b.write_json(b.CONFIG, {'shared_experience_enabled': False})
        self.assertEqual(self.search(tags=['python', 'numeric-input']), [])
        self.assertEqual(len(b.experience_manager().lookup(self.repo)), 1)

    def test_failed_sources_private_answers_and_credentials_cannot_be_published(self):
        _, failed = self.source(False)
        with self.assertRaises(b.GateError): self.publish(failed)
        _, entry = self.source()
        for text in [self.q[0]['reference'], self.checks[0]['argv'][2], 'api_key=NOT_A_REAL_SECRET']:
            with self.subTest(text=text), self.assertRaises(b.GateError):
                self.publish(entry, {**LESSON, 'resolution': text})
        self.assertEqual(self.search(), [])

    def test_original_owner_required_and_no_destination_override(self):
        sid, entry = self.source()
        path = b.run_dir(sid) / 'state.json'
        state = b.read_json(path)
        state['owner'] = {'thread_id': 'original-supervisor'}
        b.write_json(path, state)
        with self.assertRaises(b.GateError): self.publish(entry, caller='another-supervisor')
        with self.assertRaises(b.GateError): self.publish(entry)

    def test_source_disablement_or_changed_evidence_invalidates_shared_lesson(self):
        sid, entry = self.source()
        self.publish(entry)
        self.assertEqual(len(self.search()), 1)
        state = b.read_json(b.run_dir(sid) / 'state.json')
        (Path(state['worktree']) / 'unreviewed.txt').write_text('changed')
        self.assertEqual(self.search(), [])
        (Path(state['worktree']) / 'unreviewed.txt').unlink()
        b.experience_manager().forget(entry['id'])
        self.assertEqual(self.search(), [])

    def test_concurrent_publication_is_idempotent_and_disabled_entry_stays_disabled(self):
        _, entry = self.source()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.publish(entry), range(3)))
        self.assertEqual(len({x['experience_id'] for x in results}), 1)
        self.assertEqual(len(self.search()), 1)
        ident = results[0]['experience_id']
        b.call_tool('telos_experience_forget', {'experience_id': ident})
        self.assertTrue(self.publish(entry)['disabled'])
        self.assertEqual(self.search(), [])

    def test_new_task_receives_shared_snapshot_but_existing_task_is_unchanged(self):
        _, entry = self.source()
        repo_b = self.base / 'project-b'
        b.git(self.repo, 'clone', '-q', str(self.repo), str(repo_b))
        def create():
            return b.create_session(repo=str(repo_b), task='Maintain signed inputs.', questions=self.q,
                checks=self.checks, experience_tags=['numeric-input'], role_profile='developer')['session_id']
        before_id = create()
        before_path = b.run_dir(before_id) / 'contract.json'
        before = before_path.read_bytes()
        published = self.publish(entry)
        later = b.read_json(b.run_dir(create()) / 'contract.json')
        self.assertIn(LESSON['resolution'], later['experience_context'])
        self.assertEqual(later['experience_ids'], [published['experience_id']])
        self.assertEqual(later['experience_refs'][0]['version'], 1)
        self.assertEqual(before_path.read_bytes(), before)
        self.assertLessEqual(len(later['experience_context']), 6000)
        self.assertNotIn(LESSON['resolution'], json.dumps(b.public_exam(later)))

    def test_malformed_shared_version_is_isolated_from_valid_neighbors(self):
        _, entry = self.source()
        first = self.publish(entry)
        second = self.publish(entry, {**LESSON, 'limitations': 'Another explicitly bounded variant.'})
        p = b.STATE / 'experience/shared' / (first['experience_id'] + '.json')
        value = b.read_json(p); value['version'] = True
        value['content_hash'] = b.digest({k: v for k, v in value.items() if k != 'content_hash'})
        b.write_json(p, value)
        self.assertEqual([r['id'] for r in self.search()], [second['experience_id']])

    def test_same_generalization_from_two_sources_is_one_lesson_with_two_receipts(self):
        _, first = self.source(); p1 = self.publish(first)
        sid, second = self.source(); p2 = self.publish(second)
        self.assertEqual(p1['experience_id'], p2['experience_id'])
        self.assertEqual(p2['source_count'], 2)
        b.experience_manager().forget(first['id'])
        self.assertEqual(self.search()[0]['source_count'], 1)

    def test_context_limit_tracks_only_delivered_complete_lessons(self):
        _, entry = self.source()
        for i in range(3):
            self.publish(entry, {**LESSON, 'problem': str(i) + 'x' * 700,
                                'applicability': 'x' * 700, 'limitations': 'x' * 700})
        text, ids, refs = b.experience_manager().context(self.repo / 'project-b', None,
            tags=['numeric-input'], with_refs=True)
        self.assertLessEqual(len(text), 6000)
        self.assertLess(len(ids), 3)
        self.assertEqual(ids, [r['id'] for r in refs])
        for ident in ids:
            self.assertIn(ident, text)
        self.assertEqual(text.count('limitations:'), len(ids))

    def test_long_legacy_lesson_remains_available_with_bounded_excerpt(self):
        sid = b.create_session(repo=str(self.repo), task='Legacy detailed repair.',
            questions=self.q, checks=self.checks)['session_id']
        ready = BridgeTests.ready_review(self, sid)
        b.grade(sid, ready['packet_id'], self.scores(), [], public_lessons=['LEGACY_DETAIL ' + 'x' * 5900])
        text, ids = b.experience_manager().context(self.repo, None)
        self.assertIn('LEGACY_DETAIL', text)
        self.assertEqual(len(ids), 1)
        self.assertLessEqual(len(text), 6000)


class NativeRoleExperienceTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    scores = BridgeTests.scores
    source = SharedExperienceTests.source
    publish = SharedExperienceTests.publish

    def test_real_cli_receives_role_and_shared_lesson_without_private_material(self):
        from test_closed_book import WorkflowTests, message
        from test_providers import configured
        _, entry = self.source()
        published = self.publish(entry)
        repo_b = self.base / 'project-b'
        b.git(self.repo, 'clone', '-q', str(self.repo), str(repo_b))
        cfg = configured(); cfg['job_timeout'] = 35
        b.write_json(b.CONFIG, cfg)
        sid = b.create_session(repo=str(repo_b), task='Check signed numeric input.', questions=self.q,
            checks=self.checks, role_profile='tester', experience_tags=['numeric-input'])['session_id']
        state, contract = b.load_session(b.run_dir(sid))
        with WorkflowTests.model(self, lambda req, n: message('Checked the assigned numeric behavior.')) as captured:
            result = b.peer_turn(state, contract, 'Inspect calc.py and report the verification boundaries.')
        self.assertTrue(result['thread_id'])
        self.assertEqual(len(captured), 1)
        payload = json.dumps(captured, ensure_ascii=False)
        self.assertIn('测试工程师', payload)
        self.assertIn(LESSON['resolution'], payload)
        self.assertIn(published['experience_id'], payload)
        for text in (self.q[0]['reference'], self.checks[0]['argv'][2]):
            self.assertNotIn(text, payload)


class SharedProjectTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child
    submit = ProjectTests.submit
    check_sync = ProjectTests.check_sync
    scores = BridgeTests.scores
    publish = SharedExperienceTests.publish

    def test_project_lesson_waits_for_integrated_acceptance(self):
        pid = self.create([self.task('a')]); self.start(pid); self.submit(pid, 'a')
        entry = b.experience_manager().lookup(self.repo, include_candidates=True)[0]
        with self.assertRaises(b.GateError): self.publish(entry)
        b.project_manager().integrate(pid)
        review = self.check_sync(pid)
        b.project_manager().finish(pid, review['packet_id'], self.scores(), [], 'Reviewed integrated changes.')
        result = self.publish(entry)
        self.assertEqual(result['scope'], 'shared')


if __name__ == '__main__':
    unittest.main()

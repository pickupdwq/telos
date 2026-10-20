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


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(case(name) for case in (ExperienceTests, LookupTests)
                              for name in case.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()

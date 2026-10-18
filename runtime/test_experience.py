import json
from pathlib import Path
import unittest
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


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ExperienceTests(name) for name in ExperienceTests.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()

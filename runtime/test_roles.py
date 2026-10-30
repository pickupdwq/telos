"""Role profiles remain public task guidance, never new authority or graders."""
import copy
import unittest

from test_telos import b, BridgeTests
from test_projects import ProjectTests


class RoleTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown

    def open(self, **kw):
        return b.call_tool('telos_open', dict(repo=str(self.repo), task='Maintain addition.',
            questions=self.q, checks=self.checks, notify=False,
            skip_meeting_reason='User requested a small direct implementation.', **kw))

    def contract(self, sid):
        return b.read_json(b.run_dir(sid) / 'contract.json')

    def test_role_catalog_exposes_three_reusable_jobs(self):
        self.assertIn('telos_roles', [t['name'] for t in b.TOOLS])
        catalog = b.call_tool('telos_roles', {})
        self.assertEqual({r['id'] for r in catalog['roles']}, {'designer', 'developer', 'tester'})
        for role in catalog['roles']:
            for key in ('version', 'responsibilities', 'workflow', 'deliverables', 'review_focus', 'content_hash'):
                self.assertTrue(role[key])

    def test_snapshot_preserves_task_and_private_acceptance(self):
        result = self.open(role_profile='developer', experience_tags=['python', 'numeric-input'])
        c = self.contract(result['session_id'])
        self.assertEqual(c['role_snapshot']['id'], 'developer')
        self.assertEqual(c['questions'], self.q)
        self.assertEqual(c['checks'], self.checks)
        self.assertEqual(c['experience_tags'], ['numeric-input', 'python'])
        self.assertEqual(result['role_snapshot'], c['role_snapshot'])
        from roles import prompt
        text = prompt(c['role_snapshot'])
        self.assertIn('deliverables', text.lower())
        self.assertNotIn(self.q[0]['reference'], text)
        self.assertFalse(c['command_network'])
        self.assertEqual(c['executor'], b.backend_bindings(c)['exam'])
        self.assertEqual(b.status(result['session_id'], detail='summary')['role_snapshot'], c['role_snapshot'])

    def test_custom_profile_defaults_and_explicit_model_override(self):
        b.write_json(b.CONFIG, {'role_profiles': {'developer': {'version': 2, 'executor_profile': 'role_backend'}},
            'profiles': {'role_backend': {'provider': 'bailian', 'model': 'deepseek-v4.1-flash-1017', 'reasoning_effort': 'high'}}})
        first = self.contract(self.open(role_profile='developer')['session_id'])
        self.assertEqual(first['executor']['provider'], 'bailian')
        self.assertEqual(first['role_snapshot']['version'], 2)
        second = self.contract(self.open(role_profile='developer', provider='openai', model='gpt-6-astra')['session_id'])
        self.assertEqual(second['executor']['provider'], 'openai')
        before = copy.deepcopy(first)
        b.write_json(b.CONFIG, {'roles_enabled': False})
        self.assertEqual(first, before)
        self.assertEqual(b.call_tool('telos_roles', {})['roles'], [])
        self.open()  # legacy callers remain usable
        with self.assertRaises(b.GateError): self.open(role_profile='developer')

    def test_invalid_role_and_private_or_privileged_template_rejected(self):
        with self.assertRaises(b.GateError): self.open(role_profile='unknown')
        for definition in ({'command_network': True}, {'responsibilities': [self.q[0]['reference']]},
                           {'workflow': ['api_key=NOT_A_REAL_SECRET']}, {'version': True}):
            b.write_json(b.CONFIG, {'role_profiles': {'developer': definition}})
            with self.subTest(definition=definition), self.assertRaises(b.GateError):
                self.open(role_profile='developer')

    def test_legacy_contract_and_disabled_experience_stay_compatible(self):
        c = self.contract(self.open(use_experience=False)['session_id'])
        self.assertNotIn('role_snapshot', c)
        self.assertEqual(c['experience_context'], '')
        self.assertEqual(c['experience_ids'], [])

    def test_catalog_bounds_each_role_without_rejecting_multiple_valid_roles(self):
        from roles import BUILTINS
        custom = {}
        for i in range(4):
            custom['specialist_' + str(i)] = {**copy.deepcopy(BUILTINS['developer']),
                'workflow': ['Check the relevant boundary. ' * 20] * 4}
        b.write_json(b.CONFIG, {'role_profiles': custom})
        self.assertEqual(len(b.call_tool('telos_roles', {})['roles']), 7)


class ProjectRoleTests(unittest.TestCase):
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    task = ProjectTests.task
    create = ProjectTests.create
    start = ProjectTests.start
    state = ProjectTests.state
    child = ProjectTests.child

    def test_queued_role_frozen_before_configuration_changes(self):
        pid = self.create([self.task('a', role_profile='developer')])
        original = self.state(pid)['tasks'][0]['role_snapshot']
        b.write_json(b.CONFIG, {'role_profiles': {'developer': {'version': 9, 'responsibilities': ['Different future job.']}}})
        self.start(pid)
        state = self.child(pid, 'a')
        c = b.read_json(b.run_dir(state['session_id']) / 'contract.json')
        self.assertEqual(c['role_snapshot'], original)
        self.assertEqual(c['allowed_paths'], ['a.py'])
        view = b.project_manager().status(pid, 'summary')
        self.assertEqual(view['tasks'][0]['role_snapshot'], original)

    def test_meeting_profile_is_frozen_and_does_not_gain_write_permission(self):
        mid = b.call_tool('telos_meeting_open', {'repo': str(self.repo), 'requirement': 'Review signed inputs.',
            'participants': [{'id': 'design', 'role': 'Assess the input workflow', 'role_profile': 'designer'},
                             {'id': 'qa', 'role': 'Assess edge cases', 'role_profile': 'tester'}],
            'decision_reason': 'Independent usability and verification perspectives.', 'notify': False})['project_id']
        self.start(mid)
        for ident, profile in [('design', 'designer'), ('qa', 'tester')]:
            state = self.child(mid, ident)
            c = b.read_json(b.run_dir(state['session_id']) / 'contract.json')
            self.assertEqual(c['role_snapshot']['id'], profile)
            self.assertEqual(c['allowed_paths'], [])
            self.assertEqual(c['task_type'], 'discussion')


if __name__ == '__main__':
    unittest.main()

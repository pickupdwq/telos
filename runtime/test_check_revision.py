"""Targeted generic check revision tests.

RevisionStateTests use real temporary Git repositories and state files while
stubbing only the execution-model/check-runner boundary.  LifecycleTests use
real file locks and reload the persisted state through the public effective
chain.  Full macOS sandbox lifecycle remains a host-runner responsibility.
"""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import check_revision
import preflight
import telos

BEFORE = "assert 1 + 1 == 2\nassert 'ok' in ['ok']\n"
AFTER = "assert 1 + 1 == 2\nassert 'ok' in ['ok']\nassert len('ok') == 2\n"
AFTER2 = AFTER + "assert True\n"
OTHER_BEFORE = "assert True\n"


class FakeProjects:
    def __init__(self):
        self.project = {'project_id': 'p' * 32, 'phase': 'running', 'owner': {'thread_id': 'owner'}}

    @contextlib.contextmanager
    def lock(self):
        yield

    def load(self, pid):
        return copy.deepcopy(self.project)

    def authorize(self, project, caller):
        if caller != 'owner':
            raise telos.GateError('not authorized')

    def active(self, project):
        if project.get('phase') in ('paused', 'cancelled'):
            raise telos.GateError('not active')


class TempBridge:
    """Real files/Git, no model or sandbox execution."""
    GateError = telos.GateError

    def __init__(self, root, repo):
        self.root = Path(root)
        self.repo = Path(repo)
        self._projects = FakeProjects()

    def digest(self, value):
        return telos.digest(value)

    def config(self):
        return {}

    def read_json(self, path):
        return json.loads(Path(path).read_text())

    def write_json(self, path, value):
        path = Path(path)
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(json.dumps(value, sort_keys=True) + '\n')
        os.replace(temp, path)

    @contextlib.contextmanager
    def locked(self, sid):
        with (self.root / 'lock').open('a+') as stream:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield self.root

    def project_manager(self):
        return self._projects

    def load_session(self, root, *, read_only=False):
        state = self.read_json(Path(root) / 'state.json')
        raw = self.read_json(Path(root) / 'contract.json')
        if check_revision.KEY in state:
            return state, check_revision.effective(self, state, raw, raw)
        return state, raw

    def event_action(self, state, event_id, kind, arguments):
        event = (state.get('events') or {}).get(event_id)
        if not event:
            raise telos.GateError('event receipt required')
        previous = event.get('actions', {}).get(kind)
        if previous:
            if previous['arguments_hash'] != self.digest(arguments):
                raise telos.GateError('different arguments')
            return {'duplicate': True}
        if event_id != state.get('last_event_id') or event.get('status') not in ('received', 'processed'):
            raise telos.GateError('Event is stale or not received')
        return None

    def record_action(self, state, event_id, kind, arguments, result):
        state['events'][event_id]['actions'][kind] = {
            'arguments_hash': self.digest(arguments), 'result': result,
            'action_id': self.digest([event_id, kind, arguments])}
        state['events'][event_id]['status'] = 'processed'

    def deny_leak(self, message, contract):
        if 'SECRET_REFERENCE' in message:
            raise telos.GateError('private material')

    def git(self, repo, *args):
        env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        result = subprocess.run([preflight.git_executable() or 'git', '-C', str(repo), *args],
                                check=False, capture_output=True, text=True, env=env)
        if result.returncode:
            raise RuntimeError('git %s failed (%s): %s'
                               % (' '.join(args), result.returncode, result.stderr[-1200:]))
        return result.stdout.strip()


class RevisionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / 'repo'
        self.repo.mkdir()
        self.b = TempBridge(root, self.repo)
        self.b.git(self.repo, 'init', '-q')
        self.b.git(self.repo, 'config', 'user.email', 'test@example.invalid')
        self.b.git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'app.txt').write_text('base\n')
        self.b.git(self.repo, 'add', 'app.txt')
        self.b.git(self.repo, 'commit', '-q', '-m', 'base')
        self.commit = self.b.git(self.repo, 'rev-parse', 'HEAD')
        self.sid = 'a' * 32
        self.event = 'e' * 32
        self.contract = {
            'task_type': 'implementation', 'checks': [
                {'id': 'c1', 'argv': ['{python}', '-c', BEFORE], 'points': 2},
                {'id': 'c2', 'argv': ['{python}', '-c', OTHER_BEFORE], 'points': 1}],
            'max_rounds': 5, 'threshold': 2, 'budget': {'max_requests': 9}}
        self.state = {
            'session_id': self.sid, 'owner': {'thread_id': 'owner'},
            'phase': 'responded', 'rounds': 2, 'turns': 3, 'continuation': 'active',
            'contract_hash': self.b.digest(self.contract),
            'events': {self.event: {'id': self.event, 'status': 'received', 'actions': {}}},
            'last_event_id': self.event, 'submitted_commit': self.commit, 'worktree': str(self.repo),
            'packet_id': 'packet-before', 'acceptance': None, 'auto_check': None,
            'budget': {'max_requests': 9}, 'history': [{'round': 1}], 'usage_id': self.sid}
        self.b.write_json(Path(self.tmp.name) / 'contract.json', self.contract)
        self.b.write_json(Path(self.tmp.name) / 'state.json', self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def kwargs(self, after=AFTER, event=None, request='r1', apply=False, proposal=None):
        return dict(request_id=request, reason='fix deterministic check defect',
                    user_authorization='user authorized this supervised revision',
                    event_id=event or self.event, check_id='c1', before_program=BEFORE,
                    after_program=after, preservation_reason='same assertion standard and evidence',
                    evidence='diff reviewed against retained failure', caller='owner',
                    apply=apply, proposal_hash=proposal)

    def test_preview_is_read_only_and_complete(self):
        before = (Path(self.tmp.name) / 'state.json').read_bytes()
        result = check_revision.revise(self.b, self.sid, **self.kwargs())
        self.assertTrue(result['preview'])
        self.assertIn('before', result['revision'])
        self.assertIn('after', result['revision'])
        self.assertIn('diff', result['revision'])
        self.assertEqual(before, (Path(self.tmp.name) / 'state.json').read_bytes())
        self.assertEqual(len(result['revision']['proposal_hash']), 64)

    def test_apply_changes_only_program_and_preserves_contract_and_counters(self):
        preview = check_revision.revise(self.b, self.sid, **self.kwargs())
        result = check_revision.revise(self.b, self.sid, **self.kwargs(
            apply=True, proposal=preview['revision']['proposal_hash']))
        self.assertFalse(result['accepted'])
        saved_contract = self.b.read_json(Path(self.tmp.name) / 'contract.json')
        self.assertEqual(saved_contract, self.contract)
        state = self.b.read_json(Path(self.tmp.name) / 'state.json')
        self.assertEqual(state['rounds'], 2)
        self.assertEqual(state['turns'], 3)
        self.assertEqual(state['budget'], {'max_requests': 9})
        self.assertEqual(state['history'], [{'round': 1}])
        self.assertNotIn('packet_id', state)
        self.assertNotIn('acceptance', state)
        self.assertNotIn('auto_check', state)
        reloaded_state, effective = self.b.load_session(Path(self.tmp.name))
        self.assertEqual(effective['checks'][0]['argv'][2], AFTER)
        self.assertEqual(effective['checks'][1], self.contract['checks'][1])
        self.assertEqual(self.b.digest(effective), reloaded_state['contract_hash'])

    def test_idempotent_same_arguments_does_not_write(self):
        preview = check_revision.revise(self.b, self.sid, **self.kwargs())
        check_revision.revise(self.b, self.sid, **self.kwargs(
            apply=True, proposal=preview['revision']['proposal_hash']))
        before = (Path(self.tmp.name) / 'state.json').read_bytes()
        duplicate = check_revision.revise(self.b, self.sid, **self.kwargs())
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(before, (Path(self.tmp.name) / 'state.json').read_bytes())

    def test_different_arguments_and_stale_event_are_rejected(self):
        preview = check_revision.revise(self.b, self.sid, **self.kwargs())
        check_revision.revise(self.b, self.sid, **self.kwargs(
            apply=True, proposal=preview['revision']['proposal_hash']))
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **self.kwargs(request='r2'))
        state = self.b.read_json(Path(self.tmp.name) / 'state.json')
        state['events']['f' * 32] = {'id': 'f' * 32, 'status': 'received', 'actions': {}}
        state['last_event_id'] = 'f' * 32
        self.b.write_json(Path(self.tmp.name) / 'state.json', state)
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **self.kwargs(event=self.event, request='r3'))

    def test_tampered_proposal_and_assert_reduction_are_rejected(self):
        preview = check_revision.revise(self.b, self.sid, **self.kwargs())
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **self.kwargs(
                apply=True, proposal='0' * 64))
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **self.kwargs(after="assert True\n", request='r9'))

    def test_owner_project_and_active_gates(self):
        args = self.kwargs()
        args['caller'] = 'intruder'
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **args)
        self.state['continuation'] = 'paused'
        self.b.write_json(Path(self.tmp.name) / 'state.json', self.state)
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **self.kwargs())

    def test_second_revision_builds_valid_version_chain(self):
        first = check_revision.revise(self.b, self.sid, **self.kwargs())
        check_revision.revise(self.b, self.sid, **self.kwargs(
            apply=True, proposal=first['revision']['proposal_hash']))
        state = self.b.read_json(Path(self.tmp.name) / 'state.json')
        second_event = 'b' * 32
        state['events'][second_event] = {'id': second_event, 'status': 'received', 'actions': {}}
        state['last_event_id'] = second_event
        self.b.write_json(Path(self.tmp.name) / 'state.json', state)
        args = self.kwargs(after=AFTER2, event=second_event, request='r2')
        args['before_program'] = AFTER
        second = check_revision.revise(self.b, self.sid, **args)
        args['apply'] = True
        args['proposal_hash'] = second['revision']['proposal_hash']
        check_revision.revise(self.b, self.sid, **args)
        state = self.b.read_json(Path(self.tmp.name) / 'state.json')
        self.assertEqual([x['seq'] for x in state[check_revision.KEY]['revisions']], [1, 2])
        _, effective = self.b.load_session(Path(self.tmp.name))
        self.assertEqual(effective['checks'][0]['argv'][2], AFTER2)

    def test_same_request_id_binds_every_revision_parameter(self):
        args = self.kwargs()
        preview = check_revision.revise(self.b, self.sid, **args)
        check_revision.revise(self.b, self.sid, **(args | {
            'apply': True, 'proposal_hash': preview['revision']['proposal_hash']}))
        state_file = Path(self.tmp.name) / 'state.json'
        unchanged = state_file.read_bytes()
        for key, value in {'after_program': AFTER2, 'before_program': AFTER,
                           'check_id': 'c2', 'preservation_reason': 'different reason',
                           'evidence': 'different evidence', 'event_id': 'f' * 32}.items():
            with self.subTest(key=key), self.assertRaises(telos.GateError):
                check_revision.revise(self.b, self.sid, **(args | {key: value}))
            self.assertEqual(unchanged, state_file.read_bytes())

    def test_different_check_version_chain_and_earlier_idempotent_replay(self):
        first = check_revision.revise(self.b, self.sid, **self.kwargs())
        check_revision.revise(self.b, self.sid, **self.kwargs(
            apply=True, proposal=first['revision']['proposal_hash']))
        state_file = Path(self.tmp.name) / 'state.json'
        state = self.b.read_json(state_file)
        event = 'f' * 32
        state['events'][event] = {'id': event, 'status': 'received', 'actions': {}}
        state['last_event_id'] = event
        self.b.write_json(state_file, state)
        args = self.kwargs(event=event, request='second') | {
            'check_id': 'c2', 'before_program': OTHER_BEFORE, 'after_program': 'assert 2 == 2\n'}
        second = check_revision.revise(self.b, self.sid, **args)
        check_revision.revise(self.b, self.sid, **(args | {
            'apply': True, 'proposal_hash': second['revision']['proposal_hash']}))
        saved = state_file.read_bytes()
        state, effective = self.b.load_session(Path(self.tmp.name))
        self.assertEqual(effective['checks'][0]['argv'][2], AFTER)
        self.assertEqual(effective['checks'][1]['argv'][2], args['after_program'])
        duplicate = check_revision.revise(self.b, self.sid, **self.kwargs())
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['replayed_seq'], 1)
        self.assertEqual(saved, state_file.read_bytes())

    def test_source_head_and_dirty_changes_invalidate_preview(self):
        token = check_revision.revise(self.b, self.sid, **self.kwargs())['revision']['proposal_hash']
        (self.repo / 'app.txt').write_text('changed\n')
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **self.kwargs(apply=True, proposal=token))
        self.b.git(self.repo, 'add', 'app.txt')
        self.b.git(self.repo, 'commit', '-q', '-m', 'new')
        with self.assertRaises(telos.GateError):
            check_revision.revise(self.b, self.sid, **self.kwargs(apply=True, proposal=token))

    def test_safe_summary_survives_compact_views_without_programs(self):
        import compact
        preview = check_revision.revise(self.b, self.sid, **self.kwargs())
        check_revision.revise(self.b, self.sid, **self.kwargs(
            apply=True, proposal=preview['revision']['proposal_hash']))
        state = self.b.read_json(Path(self.tmp.name) / 'state.json')
        summary = check_revision.public(state)
        views = [compact.status({'check_revision': summary}),
                 compact.review({'check_revision': summary}),
                 compact.project({'tasks': [{'check_revision': summary}]}),
                 compact.project_review({'evidence': {'check_revisions': {'child': summary}}})]
        for view in views:
            encoded = json.dumps(view)
            self.assertIn(summary['record_hash'], encoded)
            self.assertNotIn('before_program', encoded)
            self.assertNotIn('after_program', encoded)
            self.assertNotIn('assert 1 + 1', encoded)


class LifecycleTests(unittest.TestCase):
    """Host lifecycle approximation with real locks, files and Git; no sandbox."""

    def test_preview_apply_reload_retains_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / 'repo'; repo.mkdir()
            b = TempBridge(root, repo)
            b.git(repo, 'init', '-q'); b.git(repo, 'config', 'user.email', 't@e.invalid')
            b.git(repo, 'config', 'user.name', 'T')
            (repo / 'a').write_text('a')
            b.git(repo, 'add', 'a'); b.git(repo, 'commit', '-q', '-m', 'a')
            commit = b.git(repo, 'rev-parse', 'HEAD')
            contract = {'task_type': 'implementation', 'checks': [
                {'id': 'c1', 'argv': ['{python}', '-c', BEFORE]}], 'max_rounds': 4,
                'threshold': 1, 'budget': {'max_requests': 4}}
            event = 'c' * 32
            state = {'session_id': 'd' * 32, 'owner': {'thread_id': 'owner'},
                     'phase': 'not_accepted', 'rounds': 1, 'turns': 1,
                     'continuation': 'active', 'contract_hash': b.digest(contract),
                     'events': {event: {'id': event, 'status': 'received', 'actions': {}}},
                     'last_event_id': event, 'submitted_commit': commit, 'worktree': str(repo),
                     'packet_id': 'p', 'acceptance': None, 'auto_check': None,
                     'budget': {'max_requests': 4}, 'history': [], 'usage_id': 'd' * 32}
            original = json.dumps(contract, sort_keys=True).encode()
            b.write_json(root / 'contract.json', contract)
            b.write_json(root / 'state.json', state)
            args = dict(request_id='life', reason='r', user_authorization='u',
                        event_id=event, check_id='c1', before_program=BEFORE,
                        after_program=AFTER, preservation_reason='same standard',
                        evidence='reviewed diff', caller='owner')
            preview = check_revision.revise(b, 'd' * 32, **args)
            self.assertTrue(preview['preview'])
            applied = check_revision.revise(b, 'd' * 32, **args, apply=True,
                                            proposal_hash=preview['revision']['proposal_hash'])
            self.assertFalse(applied['accepted'])
            self.assertEqual(original, json.dumps(b.read_json(root / 'contract.json'), sort_keys=True).encode())
            _, effective = b.load_session(root)
            self.assertEqual(effective['checks'][0]['argv'][2], AFTER)


class RealLoaderRevisionTests(unittest.TestCase):
    """Real temporary Telos audit session, real b.load_session, no TempBridge."""
    tearDown = __import__('test_audit_check_correction').CorrectionTests.tearDown
    scores = __import__('test_audit_check_correction').CorrectionTests.scores
    data = __import__('test_audit_check_correction').CorrectionTests.data
    report = __import__('test_audit_check_correction').CorrectionTests.report
    review = __import__('test_audit_check_correction').CorrectionTests.review
    setUp = __import__('test_audit_check_correction').CorrectionTests.setUp
    create = __import__('test_audit_check_correction').CorrectionTests.create

    def test_work_turn_policy_and_revision_preserve_each_other_in_both_orders(self):
        from test_telos import b
        from test_audit_check_correction import OWNER
        from test_shared_experience import SharedExperienceTests
        import task_budget
        _, entry = SharedExperienceTests.source(self)
        published = SharedExperienceTests.publish(self, entry)
        destination = self.base / 'role-and-revision-project'
        b.git(self.repo, 'clone', '-q', str(self.repo), str(destination))
        for policy_first in (True, False):
            with self.subTest(policy_first=policy_first):
                sid = self.create(repo=str(destination), role_profile='tester',
                                  experience_tags=['numeric-input'])
                root = b.run_dir(sid)
                state, contract = self.data(sid)
                self.assertEqual(contract['role_snapshot']['id'], 'tester')
                self.assertIn(published['experience_id'], contract['experience_ids'])
                original = (root / 'contract.json').read_bytes()
                counts = state['turns'], state['rounds']
                budget_args = dict(session_id=sid, request_id='integrated-policy',
                                   reason='Keep the same task and spent turns',
                                   user_authorization='Explicit synthetic unlimited work-turn authorization')
                if policy_first:
                    b.call_tool('telos_task_budget', budget_args, {'threadId': OWNER})
                before = contract['checks'][0]['argv'][2]
                args = dict(session_id=sid, request_id='integrated-revision',
                            reason='Correct status spelling without changing requirements',
                            user_authorization='Explicit synthetic check revision authorization',
                            event_id=state['last_event_id'], check_id='quality',
                            before_program=before, after_program=before.replace("'pass'", "'passed'"),
                            preservation_reason='Same required statuses and evidence',
                            evidence='Synthetic immutable report schema and exact diff')
                preview = b.call_tool('telos_revise_checks', args, {'threadId': OWNER})
                b.call_tool('telos_revise_checks', args | dict(apply=True,
                            proposal_hash=preview['revision']['proposal_hash']), {'threadId': OWNER})
                if not policy_first:
                    b.call_tool('telos_task_budget', budget_args, {'threadId': OWNER})
                loaded, effective = b.load_session(root, read_only=True)
                self.assertEqual((loaded['turns'], loaded['rounds']), counts)
                self.assertEqual((root / 'contract.json').read_bytes(), original)
                self.assertEqual(effective['checks'][0]['argv'][2], args['after_program'])
                for key in ('role_snapshot', 'executor', 'experience_context',
                            'experience_ids', 'experience_refs', 'experience_tags'):
                    self.assertEqual(effective[key], contract[key])
                summary = b.status(sid, detail='summary')
                self.assertEqual(summary['role_snapshot'], contract['role_snapshot'])
                self.assertEqual(summary['experience_refs'], contract['experience_refs'])
                self.assertEqual(summary['check_revision']['record_hash'], loaded['check_revision']['record_hash'])
                self.assertIsNone(summary['work_turn_budget']['effective_max_turns'])
                self.assertEqual(loaded[task_budget.KEY]['original_contract_hash'], b.digest(contract))
                self.assertIsNone(task_budget.summary(loaded, 20)['effective_max_turns'])
                saved = (root / 'state.json').read_bytes()
                self.assertTrue(b.call_tool('telos_task_budget', budget_args, {'threadId': OWNER})['replayed'])
                self.assertEqual((root / 'state.json').read_bytes(), saved)
                loaded[task_budget.KEY] = None
                b.write_json(root / 'state.json', loaded)
                damaged = (root / 'state.json').read_bytes()
                with self.assertRaises(b.GateError):
                    b.call_tool('telos_preflight_checks', {'session_id': sid}, {'threadId': OWNER})
                self.assertEqual((root / 'state.json').read_bytes(), damaged)

    def test_preview_apply_then_real_load_session_keeps_frozen_files(self):
        from test_telos import b
        from test_audit_check_correction import OWNER
        sid = self.create()
        root = b.run_dir(sid)
        state, contract = self.data(sid)
        old_contract = (root / 'contract.json').read_bytes()
        old_counts = (state['rounds'], state['turns'])
        old_program = contract['checks'][0]['argv'][2]
        after = old_program.replace("'pass'", "'passed'")
        self.assertNotEqual(old_program, after)
        args = {'session_id': sid, 'request_id': 'real-loader-revision',
                'reason': 'Correct the historical audit status literal',
                'user_authorization': 'Authorized supervised check program revision',
                'event_id': state['last_event_id'], 'check_id': 'quality',
                'before_program': old_program, 'after_program': after,
                'preservation_reason': 'Same assertions and evidence requirements',
                'evidence': 'Private diff reviewed by the original supervisor'}
        preview = b.call_tool('telos_revise_checks', args, {'threadId': OWNER})
        self.assertTrue(preview['preview'])
        applied = b.call_tool('telos_revise_checks', args | {
            'apply': True, 'proposal_hash': preview['revision']['proposal_hash']},
            {'threadId': OWNER})
        self.assertFalse(applied['accepted'])
        loaded_state, loaded = b.load_session(root)
        self.assertEqual(loaded['checks'][0]['argv'][2], after)
        self.assertEqual(b.digest(loaded), loaded_state['contract_hash'])
        self.assertEqual((root / 'contract.json').read_bytes(), old_contract)
        self.assertEqual((loaded_state['rounds'], loaded_state['turns']), old_counts)

    def test_explicit_null_revision_is_rejected(self):
        from test_telos import b
        from test_audit_check_correction import OWNER
        sid = self.create()
        root = b.run_dir(sid)
        state, contract = self.data(sid)
        state['check_revision'] = None
        b.write_json(root / 'state.json', state)
        with self.assertRaises(Exception):
            b.load_session(root)

    def test_read_only_preflight_rejects_damage_without_quarantining_or_writing(self):
        from test_telos import b
        from test_audit_check_correction import OWNER
        sid = self.create()
        root = b.run_dir(sid)
        state, contract = self.data(sid)
        state['check_revision'] = None
        b.write_json(root / 'state.json', state)
        original = {p.relative_to(b.STATE): p.read_bytes() for p in b.STATE.rglob('*') if p.is_file()}
        with self.assertRaises(b.GateError):
            b.call_tool('telos_preflight_checks', {'session_id': sid}, {'threadId': OWNER})
        self.assertEqual(original, {p.relative_to(b.STATE): p.read_bytes()
                                   for p in b.STATE.rglob('*') if p.is_file()})

    def test_legacy_fixed_correction_then_generic_revision_preserves_both(self):
        from test_telos import b
        from test_audit_check_correction import OWNER
        sid = self.create()
        root = b.run_dir(sid)
        state, _ = self.data(sid)
        original = (root / 'contract.json').read_bytes()
        b.call_tool('telos_correct_audit_status', {'session_id': sid,
            'request_id': 'legacy', 'reason': 'Synthetic status typo',
            'user_authorization': 'Synthetic authorization', 'event_id': state['last_event_id']},
            {'threadId': OWNER})
        state, current = b.load_session(root)
        legacy = copy.deepcopy(state['audit_check_correction'])
        code = current['checks'][0]['argv'][2]
        args = dict(session_id=sid, request_id='generic', reason='Synthetic assertion addition',
                    user_authorization='Synthetic authorization', event_id=state['last_event_id'],
                    check_id='quality', before_program=code, after_program=code + '\nassert True\n',
                    preservation_reason='All original assertions retained', evidence='Synthetic diff')
        preview = b.call_tool('telos_revise_checks', args, {'threadId': OWNER})
        b.call_tool('telos_revise_checks', args | {'apply': True,
            'proposal_hash': preview['revision']['proposal_hash']}, {'threadId': OWNER})
        saved, revised = b.load_session(root)
        self.assertEqual(saved['audit_check_correction'], legacy)
        self.assertEqual((root / 'contract.json').read_bytes(), original)
        self.assertEqual(revised['checks'][0]['argv'][2], args['after_program'])
        self.assertEqual(saved['check_revision']['revisions'][0]['legacy_record_hashes'],
                         {'audit_check_correction': b.digest(legacy)})

    def test_new_session_creation_rejects_deterministic_syntax_error(self):
        from test_telos import b
        from test_audit import boundary
        with self.assertRaises(telos.GateError) as raised:
            b.create_session(repo=str(self.repo), task='Reject invalid check syntax',
                             questions=self.q, task_type='audit', audit=boundary(),
                             checks=[{'id': 'bad', 'argv': ['{python}', '-c', 'assert (']}],
                             notify=False)
        self.assertEqual(getattr(raised.exception, 'code', None), 'check_preflight_failed')


class ProjectCreationPreflightTests(unittest.TestCase):
    setUp = __import__('test_projects').ProjectTests.setUp
    tearDown = __import__('test_projects').ProjectTests.tearDown
    scores = __import__('test_projects').ProjectTests.scores
    task = __import__('test_projects').ProjectTests.task
    create = __import__('test_projects').ProjectTests.create

    def bad_checks(self):
        return [{'id': 'bad', 'argv': ['{python}', '-c', 'assert (']}]

    def test_project_child_creation_rejects_deterministic_syntax_error(self):
        from test_projects import b
        task = self.task('bad')
        task['checks'] = self.bad_checks()
        with self.assertRaises(telos.GateError) as raised:
            self.create([task])
        self.assertEqual(getattr(raised.exception, 'code', None), 'check_preflight_failed')

    def test_project_final_creation_rejects_deterministic_syntax_error(self):
        from test_projects import b
        with self.assertRaises(telos.GateError) as raised:
            self.create(checks=self.bad_checks())
        self.assertEqual(getattr(raised.exception, 'code', None), 'check_preflight_failed')

    def test_valid_frozen_project_remains_readable(self):
        from test_projects import b
        pid = self.create()
        project = b.project_manager().load(pid)
        self.assertEqual(b.digest(project['contract']), project['contract_hash'])
        self.assertEqual(project['phase'], 'planned')

    def test_project_addition_preflights_before_persisting(self):
        from test_projects import b
        pid = self.create()
        task = self.task('addition')
        task['checks'] = self.bad_checks()
        path = b.project_manager().path(pid)
        original = path.read_bytes()
        with self.assertRaises(telos.GateError) as raised:
            b.project_manager().update(pid, 'new task', add_tasks=[task])
        self.assertEqual(getattr(raised.exception, 'code', None), 'check_preflight_failed')
        self.assertEqual(original, path.read_bytes())


if __name__ == '__main__':
    unittest.main()

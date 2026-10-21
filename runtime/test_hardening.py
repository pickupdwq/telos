"""Communication identity and frozen dependency permissions across project checks."""
import copy
import json
import sys
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from desktop_rpc import DesktopRPC, RPCError
from providers import ProviderError
from supervisor import policy, verify_owner, verify_thread
from test_callbacks import CallbackTests, FakeRPC, OWNER
from test_telos import b
from test_projects import ProjectTests


class SupervisorAliasTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'supervisor': {'provider': 'openai', 'models': ['gpt-6-astra']}}
        self.thread = {'id': OWNER, 'model': 'gpt-6-astra', 'modelProvider': 'openai_http'}

    def test_alias_is_rejected_until_explicitly_trusted(self):
        with self.assertRaisesRegex(ProviderError, 'openai_http'):
            verify_thread(self.thread, self.cfg)

    def test_trusted_alias_preserves_actual_identity_and_builtin_provider(self):
        self.cfg['supervisor']['provider_aliases'] = ['openai_http']
        before = copy.deepcopy(self.cfg)
        self.assertEqual(verify_thread(self.thread, self.cfg),
                         {'model': 'gpt-6-astra', 'modelProvider': 'openai_http'})
        verify_thread({**self.thread, 'modelProvider': 'openai'}, self.cfg)
        self.assertEqual(self.cfg, before)

    def test_alias_does_not_relax_model_or_other_provider_restrictions(self):
        self.cfg['supervisor']['provider_aliases'] = ['openai_http']
        for fields in ({'model': 'gpt-mini'}, {'model': 'deepseek-flash'},
                       {'modelProvider': 'custom'}, {'modelProvider': None}, {'model': None}):
            with self.subTest(fields=fields), self.assertRaises(ProviderError):
                verify_thread({**self.thread, **fields}, self.cfg)

    def test_malformed_alias_policy_is_rejected(self):
        for aliases in ('openai_http', None, [None], [{}], [''], ['openai'],
                        ['openai_http', 'openai_http'], ['openai.*']):
            with self.subTest(aliases=aliases), self.assertRaisesRegex(ProviderError, 'provider_aliases'):
                policy({'supervisor': {**self.cfg['supervisor'], 'provider_aliases': aliases}})

    def test_alias_cannot_change_canonical_provider_or_add_unknown_fields(self):
        for fields in ({'provider': 'custom'}, {'trust_all': True}):
            with self.subTest(fields=fields), self.assertRaises(ProviderError):
                policy({'supervisor': {**self.cfg['supervisor'], **fields, 'provider_aliases': ['openai_http']}})

    def test_owner_is_revalidated_before_grading(self):
        from types import SimpleNamespace
        self.cfg['supervisor']['provider_aliases'] = ['openai_http']
        db = {'thread': self.thread, 'calls': []}
        bridge = SimpleNamespace(config=lambda: self.cfg, owner_rpc=lambda _: FakeRPC(db))
        owner = {'thread_id': OWNER, 'model': 'gpt-6-astra', 'modelProvider': 'openai_http'}
        verify_owner(bridge, owner)
        db['thread'] = {**self.thread, 'modelProvider': 'custom'}
        with self.assertRaises(ProviderError):
            verify_owner(bridge, owner)
        db['thread'] = {**self.thread, 'id': 'another-thread'}
        with self.assertRaisesRegex(ProviderError, 'identity changed'):
            verify_owner(bridge, owner)


class DesktopIdentityTests(unittest.TestCase):
    def test_owner_settings_reach_strict_supervisor_validation(self):
        rpc = object.__new__(DesktopRPC)
        rpc.thread_id, rpc.client_id, rpc.owner_client_id = OWNER, 'probe', 'owner'
        rpc.timeout = 1
        def reply(*args, **kwargs):
            rpc.state = {'id': OWNER, 'cwd': '/fixture', 'threadRuntimeStatus': {'type': 'idle'}}
            return {'result': {'settings': {'model': 'gpt-6-astra', 'modelProvider': 'openai'}}}
        with patch.object(rpc, '_send'), patch.object(rpc, '_request', side_effect=reply):
            rpc.refresh()
        thread = rpc.call('thread/read', {'threadId': OWNER})['thread']
        verify_thread(thread, {'supervisor': {'provider': 'openai', 'models': ['gpt-6-astra']}})
        self.assertEqual(thread['model'], 'gpt-6-astra')

    def test_missing_identity_cannot_reuse_previous_settings(self):
        rpc = object.__new__(DesktopRPC)
        rpc.thread_id, rpc.client_id, rpc.owner_client_id = OWNER, 'probe', 'owner'
        rpc.timeout = 1
        for settings in (None, {}, {'model': 'gpt-6-astra'}, {'model': 'gpt-6-astra', 'modelProvider': None}):
            rpc.model_settings = {'model': 'gpt-6-astra', 'modelProvider': 'openai'}
            with self.subTest(settings=settings), patch.object(rpc, '_send'), patch.object(
                    rpc, '_request', return_value={'result': {'settings': settings}}):
                with self.assertRaisesRegex(RPCError, 'model identity'):
                    rpc.refresh()
                self.assertIsNone(rpc.model_settings)

    def test_foreign_provider_is_not_inferred_from_gpt_model_name(self):
        rpc = object.__new__(DesktopRPC)
        rpc.thread_id = OWNER
        rpc.state = {'cwd': '/fixture', 'threadRuntimeStatus': {'type': 'idle'},
                     'model': 'gpt-6-astra', 'modelProvider': 'openai'}
        rpc.model_settings = {'model': 'gpt-6-astra', 'modelProvider': 'untrusted'}
        with self.assertRaises(ProviderError):
            verify_thread(rpc.call('thread/read', {'threadId': OWNER})['thread'],
                          {'supervisor': {'provider': 'openai', 'models': ['gpt-6-astra']}})


class ProjectDependencyTests(ProjectTests):
    def test_final_checks_inherit_frozen_child_dependencies_read_only(self):
        dep = self.base / 'typescript'
        dep.mkdir(); (dep / 'tsc.js').write_text('declared TypeScript runtime')
        sibling = self.base / 'undeclared.js'; sibling.write_text('not granted')
        code = ('from pathlib import Path\n'
                f'p=Path({str(dep / "tsc.js")!r})\n'
                'assert p.read_text()=="declared TypeScript runtime"\n'
                'try: p.write_text("overwrite")\n'
                f'except OSError as e: assert e.errno in {(1,13,30) if sys.platform == "linux" else (1,13)!r}, e\n'
                'else: raise AssertionError("dependency was writable")\n'
                f'try: Path({str(sibling)!r}).read_text()\n'
                f'except OSError as e: assert e.errno in {(1,2,13) if sys.platform == "linux" else (1,13)!r}, e\n'
                'else: raise AssertionError("undeclared sibling was readable")\n')
        pid = self.create([self.task('a', dependency_paths=[str(dep)])],
                          checks=[{'id': 'dependency-access', 'argv': ['{python}', '-c', code]}])
        self.start(pid); self.submit(pid, 'a')
        manager = b.project_manager()
        manager.integrate(pid)
        original = copy.deepcopy(self.state(pid)['contract'])
        evidence = self.check_sync(pid)['evidence']
        self.assertEqual(evidence['checks'][0]['status'], 'passed', evidence['checks'])
        self.assertEqual(evidence['dependency_paths'], [str(dep)])
        self.assertEqual(self.state(pid)['contract'], original)
        self.assertEqual((dep / 'tsc.js').read_text(), 'declared TypeScript runtime')

    def test_explicit_project_and_frozen_children_union_ignores_mutable_plan(self):
        paths = [self.base / name for name in ('one', 'two', 'project-only', 'untrusted')]
        for path in paths: path.mkdir()
        pid = self.create([self.task('a', dependency_paths=[str(paths[0])]),
                           self.task('b', dependency_paths=[str(paths[1]), str(paths[0])])],
                          dependency_paths=[str(paths[2]), str(paths[0])])
        self.start(pid)
        project = self.state(pid)
        project['tasks'][0]['dependency_paths'].append(str(paths[3]))
        b.project_manager().save(project)
        contract, sources = b.project_manager().verification_contract(self.state(pid))
        self.assertEqual(contract['dependency_paths'], sorted(str(p) for p in paths[:3]))
        self.assertEqual(set(sources), {'a', 'b'})
        self.assertEqual(self.state(pid)['contract']['dependency_paths'], [str(paths[2]), str(paths[0])])

    def test_legacy_project_contract_is_not_rewritten(self):
        dep = self.base / 'dependency'; dep.mkdir()
        pid = self.create([self.task('a', dependency_paths=[str(dep)])]); self.start(pid)
        project = self.state(pid)
        project['contract'].pop('dependency_paths')
        project['contract_hash'] = b.digest(project['contract'])
        b.project_manager().save(project)
        before = b.project_manager().path(pid).read_bytes()
        contract, _ = b.project_manager().verification_contract(self.state(pid))
        self.assertEqual(contract['dependency_paths'], [str(dep)])
        self.assertEqual(b.project_manager().path(pid).read_bytes(), before)

    def test_private_or_invalid_project_dependencies_are_rejected(self):
        for paths in ([str(b.STATE)], [str(b.HOME)], 'not-an-array'):
            with self.subTest(paths=paths), self.assertRaises(b.GateError):
                self.create([self.task('a')], dependency_paths=paths)
        schema = next(t for t in b.TOOLS if t['name'] == 'telos_project_open')['inputSchema']
        self.assertIn('dependency_paths', schema['properties'])

    def test_changed_child_contract_cannot_grant_extra_read_permissions(self):
        pid = self.create([self.task('a')]); self.start(pid)
        child = self.child(pid, 'a')
        path = b.run_dir(child['session_id']) / 'contract.json'
        contract = b.read_json(path); contract['dependency_paths'] = [str(self.repo)]
        b.write_json(path, contract)
        with self.assertRaisesRegex(b.GateError, 'Frozen contract changed'):
            b.project_manager().verification_contract(self.state(pid))

    def test_dependency_directory_cannot_be_redirected_after_freezing(self):
        dep = self.base / 'dependency'; dep.mkdir()
        other = self.base / 'other'; other.mkdir()
        pid = self.create([self.task('a', dependency_paths=[str(dep)])]); self.start(pid)
        dep.rmdir(); dep.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(b.GateError, 'redirected'):
            b.project_manager().verification_contract(self.state(pid))

    def test_failed_recheck_does_not_expose_stale_success_evidence(self):
        dep = self.base / 'dependency'; dep.mkdir()
        pid = self.create([self.task('a', dependency_paths=[str(dep)])])
        self.start(pid); self.submit(pid, 'a'); b.project_manager().integrate(pid)
        self.assertTrue(all(c['status'] == 'passed' for c in self.check_sync(pid)['evidence']['checks']))
        dep.rmdir()
        with self.assertRaisesRegex(b.GateError, 'No completed integration checks'):
            self.check_sync(pid)
        project = self.state(pid)
        self.assertEqual(project['phase'], 'integration_failed')
        self.assertNotIn('packet_id', project['integration'])
        self.assertIn('Dependency', project['integration']['error'])

    def test_home_based_tool_discovery_uses_only_read_only_declared_dependencies(self):
        dep = self.base / '.npm/cache/typescript'; dep.mkdir(parents=True)
        (dep / 'typescript.js').write_text('declared runtime')
        secret = b.STATE / 'private-key'; secret.parent.mkdir(parents=True)
        secret.write_text('private')
        (dep / 'secret-link').symlink_to(secret)
        hidden = self.base / '.npm/unrelated'; hidden.mkdir()
        (hidden / 'token').write_text('unrelated')
        code = ('from pathlib import Path\n'
                'p=Path.home()/".npm/cache/typescript/typescript.js"\n'
                'assert p.read_text()=="declared runtime"\n'
                'assert not (Path.home()/".npm/unrelated").exists()\n'
                'for file in (p, p.with_name("created.js")):\n'
                ' try: file.write_text("tamper")\n'
                f' except OSError as e: assert e.errno in {(1,13,30) if sys.platform == "linux" else (1,13)!r}, e\n'
                ' else: raise AssertionError("dependency view was writable")\n'
                'try: p.with_name("secret-link").read_text()\n'
                f'except OSError as e: assert e.errno in {(1,2,13) if sys.platform == "linux" else (1,13)!r}, e\n'
                'else: raise AssertionError("private symlink was followed")\n')
        with patch.object(b, 'HOME', self.base):
            results = b.executable_checks({'dependency_paths': [str(dep)],
                'checks': [{'id': 'discovery', 'argv': ['{python}', '-c', code]}]}, self.repo)
        self.assertEqual(results[0]['status'], 'passed', results)
        self.assertEqual(secret.read_text(), 'private')


class ReminderLifecycleTests(CallbackTests):
    def test_new_stage_retires_and_dequeues_old_reminder(self):
        sid, eid = self.event(); b.receive_event(sid, eid)
        state = self.state(sid)
        state['events'][eid]['received_at'] = time.time() - 1000
        b.write_json(b.run_dir(sid) / 'state.json', state)
        b.recover_progress(sid); b.deliver_once(sid)
        self.assertEqual(len(self.db['queue']), 1)
        state = self.state(sid)
        state.update(job_id='b' * 32, phase='awaiting_review')
        b.record_event(state)
        self.assertEqual(state['events'][eid]['reminder']['status'], 'superseded')
        self.assertEqual(self.db['queue'], [])

    def test_unloaded_owner_has_health_and_backoff_without_automatic_resume(self):
        sid, eid = self.event()
        self.db['thread']['status'] = {'type': 'notLoaded'}
        b.deliver_once(sid)
        count = len(self.db['calls'])
        b.deliver_once(sid)
        self.assertEqual(len(self.db['calls']), count)
        health = b.status(sid)['return_channel']['health']
        self.assertEqual(health['status'], 'waiting_for_owner')
        self.assertEqual(health['next_action'], 'open_original_thread')
        self.assertEqual(self.state(sid)['continuation'], 'active')
        self.assertEqual(self.added(), [])
        self.db['thread']['status'] = {'type': 'idle'}
        state = self.state(sid); state['events'][eid]['retry_at'] = time.time() - 1
        b.write_json(b.run_dir(sid) / 'state.json', state)
        b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)
        self.assertNotIn('delivery_blocker', self.state(sid)['events'][eid])

    def test_failed_queue_cleanup_is_retried_without_duplicate_notification(self):
        sid, eid = self.event(); b.receive_event(sid, eid)
        state = self.state(sid); state['events'][eid]['received_at'] = time.time() - 1000
        b.write_json(b.run_dir(sid) / 'state.json', state)
        b.recover_progress(sid); b.deliver_once(sid)
        self.db['delete_error'] = True
        state = self.state(sid); state.update(job_id='b' * 32, phase='running')
        b.record_event(state); b.write_json(b.run_dir(sid) / 'state.json', state)
        self.assertTrue(state['events'][eid]['reminder']['queue_cleanup_pending'])
        self.db.pop('delete_error'); b.recover_progress(sid)
        self.assertEqual(self.db['queue'], [])
        self.assertFalse(self.state(sid)['events'][eid]['reminder']['queue_cleanup_pending'])
        self.assertEqual(len(self.added()), 1)

    def test_terminal_notification_never_restarts_work(self):
        sid, eid = self.event()
        state = self.state(sid)
        b.retire_reminders(state, complete=True)
        b.write_json(b.run_dir(sid) / 'state.json', state)
        b.deliver_once(sid)
        self.assertEqual(self.added(), [])
        self.assertEqual(b.status(sid)['return_channel']['health']['status'], 'complete')
        self.assertFalse(b.receive_event(sid, eid)['proceed'])

    def test_explicit_resume_resets_only_reminders_not_work_budgets(self):
        sid, eid = self.event()
        state = self.state(sid)
        state.update(rounds=2, turns=4)
        state['events'][eid].update(status='processed', retry_at=time.time()+60,
            progress={'status': 'needs_manual_continuation', 'reminders': 3, 'waiting_since': 0},
            reminder={'id': 'old', 'status': 'unacknowledged'})
        b.write_json(b.run_dir(sid) / 'state.json', state)
        b.return_control(sid, 'resume')
        current = self.state(sid)
        self.assertEqual((current['rounds'], current['turns']), (2, 4))
        self.assertEqual(current['events'][eid]['progress']['reminders'], 0)
        self.assertEqual(current['events'][eid]['status'], 'pending')
        self.assertNotIn('retry_at', current['events'][eid])
        self.assertEqual(current['events'][eid]['reminder']['status'], 'cancelled')

    def test_manual_receipt_also_withdraws_already_processed_reminder(self):
        sid, eid = self.event()
        state = self.state(sid)
        state['events'][eid]['reminder'] = {'id': 'reminder', 'status': 'processed', 'queue_id': 'queue'}
        self.db['queue'] = [{'id': 'queue'}]
        b.retire_reminders(state, complete=True)
        self.assertEqual(self.db['queue'], [])
        self.assertTrue(state['events'][eid]['reminder']['queue_removed_at'])

    def test_superseded_reminder_cannot_reopen_processed_event(self):
        sid, eid = self.event()
        state = self.state(sid)
        state['events'][eid].update(status='processed', reminder={'id': 'old', 'status': 'superseded'})
        b.write_json(b.run_dir(sid) / 'state.json', state)
        self.assertFalse(b.receive_event(sid, eid)['proceed'])


class ProjectReminderTests(ProjectTests):
    def test_completed_project_cleans_historical_and_latest_reminders(self):
        owner = {'thread_id': OWNER, 'cwd': str(self.repo), 'socket': '/fixture', 'transport': 'cli_queue'}
        db = {'calls': [], 'queue': []}
        with patch.object(b, 'bind_owner', return_value=owner), patch.object(b, 'spawn_dispatch'):
            pid = self.create([self.task('a')], notify=True, caller=OWNER)
        project = self.state(pid); project['phase'] = 'accepted'
        b.project_manager().save(project)
        state = b.read_json(b.run_dir(pid) / 'state.json')
        for eid in ('a' * 32, 'b' * 32):
            state['events'][eid] = {'id': eid, 'status': 'processed', 'created_at': time.time(),
                'reminder': {'id': eid+'r', 'status': 'queued', 'queue_id': eid+'q'}}
            db['queue'].append({'id': eid+'q'})
        state['last_event_id'] = 'b' * 32
        b.write_json(b.run_dir(pid) / 'state.json', state)
        with patch.object(b, 'owner_rpc', return_value=FakeRPC(db)):
            b.recover_progress(pid)
            before = (b.run_dir(pid) / 'state.json').read_bytes()
            b.recover_progress(pid)
        self.assertEqual((b.run_dir(pid) / 'state.json').read_bytes(), before)
        state = b.read_json(b.run_dir(pid) / 'state.json')
        self.assertTrue(state['return_complete'])
        self.assertTrue(all(e['reminder']['status'] == 'superseded' for e in state['events'].values()))
        self.assertEqual(db['queue'], [])
        self.assertFalse(b.project_manager().receive(pid, 'b' * 32)['proceed'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name)
        for cls in (DesktopIdentityTests, ProjectDependencyTests, ReminderLifecycleTests, ProjectReminderTests)
        for name in cls.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()

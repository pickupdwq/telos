"""RSI reproductions at real Git, process and macOS sandbox boundaries."""
import copy
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch, Mock
from test_telos import b, BridgeTests
from test_callbacks import CallbackTests, OWNER, OTHER
from test_projects import ProjectTests
from isolation import model_relay


class EvaluationTests(BridgeTests):
    def state(self, sid):
        return b.read_json(b.run_dir(sid) / 'state.json')

    def evaluate(self, sid, error=None):
        state = self.state(sid)
        if state['phase'] == 'ready':
            state.update(phase='responded', thread_id=OTHER)
            b.write_json(b.run_dir(sid) / 'state.json', state)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            job = b.start_job(sid, 'evaluate')['job_id']
        seen = []
        def answer(contract):
            seen.append(b.public_exam(contract))
            if error:
                raise error
            return {'answers': [{'id': q['id'], 'answer': 'Source reviewed.'} for q in contract['questions']],
                    'public_payload_hash': b.digest(b.public_exam(contract))}
        with patch.object(b, 'answer_exam', side_effect=answer), patch.object(b, 'credential', return_value=('fixture-only', 'test')):
            b.background_job(sid, job)
        return seen

    def test_legacy_code_review_binds_current_commit_and_private_fields_stay_private(self):
        sid = b.create_session(str(self.repo), 'Implement addition', self.q, self.checks, _workflow_version=2, assessment_mode='code_review')['session_id']
        tree = Path(self.state(sid)['worktree'])
        (tree / 'calc.py').write_text('def add(a, b):\n    return a + b\n# new submitted revision\n')
        seen = self.evaluate(sid)
        packet = b.review_packet(sid)['evidence']
        self.assertEqual(packet['commit'], seen[0]['submission']['commit'])
        self.assertIn('new submitted revision', seen[0]['submission']['files'][0]['source'])
        for private in [self.q[0]['reference'], self.checks[0]['argv'][2]]:
            self.assertNotIn(private, json.dumps(seen))
        self.assertTrue(b.grade(sid, self.state(sid)['packet_id'], self.scores(), [])['accepted'])
        self.assertEqual(list((b.CACHE / 'snapshots' / sid).iterdir()), [])

    def test_review_binding_mismatch_cannot_be_accepted(self):
        sid = self.create(assessment_mode='code_review'); self.evaluate(sid)
        state = self.state(sid); path = b.run_dir(sid) / 'attempt-1.json'
        packet = b.read_json(path); packet['exam']['public_payload_hash'] = 'wrong'
        b.write_json(path, packet); state['packet_id'] = b.digest(packet)
        b.write_json(b.run_dir(sid) / 'state.json', state)
        with self.assertRaisesRegex(b.GateError, 'not bound'):
            b.grade(sid, state['packet_id'], self.scores(), [])

    def test_checks_mutating_snapshot_fail_before_exam_and_preserve_review_source(self):
        checks = [{'id': 'mutate', 'argv': ['{python}', '-c', "from pathlib import Path; Path('calc.py').write_text('POISON')"]}]
        sid = self.create(assessment_mode='code_review', checks=checks)
        seen = self.evaluate(sid)
        self.assertEqual(seen, [])
        packet = b.review_packet(sid)['evidence']
        self.assertEqual(packet['checks'][0]['status'], 'failed')
        self.assertNotIn('POISON', json.dumps(packet['submission']))
        self.assertIn('a + b', json.dumps(packet['submission']))

    def test_failed_real_check_skips_model_and_goes_directly_to_repair(self):
        sid = self.create(checks=[{'id': 'failure', 'argv': ['{python}', '-c', 'assert False']}])
        self.assertEqual(self.evaluate(sid), [])
        state = self.state(sid)
        self.assertEqual(state['phase'], 'not_accepted')
        self.assertEqual(state['rounds'], 1)
        self.assertEqual(b.review_packet(sid)['evidence']['exam']['skipped'], 'executable_checks_failed')
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.repair_feedback(sid, {'symptoms': ['Public behavior is incorrect'], 'locations': ['calc.py']})
        self.assertEqual(self.state(sid)['phase'], 'running')

    def test_five_timeouts_do_not_consume_code_rounds_or_block_repairs(self):
        sid = self.create()
        with patch.object(b, 'config', return_value={**b.config(), 'infra_backoff_seconds': 0}):
            for index in range(5):
                if b.progress_guard.blocked(self.state(sid)):
                    b.progress_guard.replan(b.bridge_services(), sid, 'retry-' + str(index),
                        'Exam transport timed out with unchanged passing checks.',
                        'Retry after examining service availability.', 'Service diagnostics reviewed.',
                        'The same-thread exam returns valid answers.')
                self.evaluate(sid, TimeoutError('fixture timeout'))
                self.assertEqual(self.state(sid)['rounds'], 0)
                self.assertEqual(self.state(sid)['infra_failures'], index + 1)
            b.progress_guard.replan(b.bridge_services(), sid, 'repair-plan',
                'Repeated exam transport failure.', 'Repair the public implementation before any more evaluation.',
                'Retained check evidence reviewed.', 'A new implementation artifact is produced.')
            with self.assertRaisesRegex(b.GateError, 'Infrastructure retry budget'):
                b.start_job(sid, 'evaluate')
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.start_job(sid, 'send', 'Continue a useful implementation repair')
        self.assertEqual(self.state(sid)['phase'], 'running')
        self.assertEqual(list((b.CACHE / 'snapshots' / sid).iterdir()), [])

    def test_backoff_and_successful_retry(self):
        sid = self.create(); self.evaluate(sid, TimeoutError('timeout'))
        with self.assertRaisesRegex(b.GateError, 'backoff'):
            b.start_job(sid, 'evaluate')
        state = self.state(sid); state['next_retry_at'] = 0
        b.write_json(b.run_dir(sid) / 'state.json', state)
        self.evaluate(sid)
        self.assertEqual(self.state(sid)['rounds'], 1)
        self.assertTrue(b.grade(sid, self.state(sid)['packet_id'], self.scores(), [])['accepted'])

    def test_dead_evaluation_process_refunds_reserved_round(self):
        sid = self.create(); state = self.state(sid)
        state.update(phase='responded', thread_id=OTHER); b.write_json(b.run_dir(sid) / 'state.json', state)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=99999999)):
            b.start_job(sid, 'evaluate')
        with patch.object(b.os, 'kill', side_effect=ProcessLookupError):
            b.status(sid); b.status(sid)
        self.assertEqual(self.state(sid)['rounds'], 0)
        self.assertEqual(self.state(sid)['infra_failures'], 1)

    def test_short_answers_allow_public_repairs_but_private_fields_rejected(self):
        for ref, message in [('是', '请检查边界条件是否正确'), ('5', '请覆盖 5 个边界情况')]:
            contract = {'questions': [{'reference': ref}], 'checks': []}
            b.deny_leak(message, contract)
            self.assertTrue(b.experience_manager().safe_public(message, [contract]))
            with self.assertRaises(b.GateError):
                b.deny_leak('参考答案：' + ref, contract)
        sid = self.create()
        with self.assertRaises(b.GateError):
            b.repair_feedback(sid, {'reference': 'secret', 'symptoms': ['bad']})

    def test_infrastructure_messages_are_not_learned_as_repairs(self):
        sid = self.create(); state, contract = b.load_session(b.run_dir(sid))
        state.update(turns=2, last_failure_kind='infrastructure')
        b.experience_manager().capture_feedback(state, contract, 'The service timed out; try later.')
        self.assertNotIn('public_repair_feedback', state)

    def test_changed_accepted_evidence_invalidates_experience_retrieval(self):
        sid = self.create(); s = self.ready_review(sid)
        b.grade(sid, s['packet_id'], self.scores(), [], public_lessons=['Preserve arithmetic behavior.'])
        self.assertEqual(len(b.experience_manager().lookup(self.repo)), 1)
        (Path(s['worktree']) / 'calc.py').write_text('broken')
        self.assertEqual(b.experience_manager().lookup(self.repo), [])


class ProgressTests(CallbackTests):
    def overdue(self, sid):
        state = self.state(sid)
        e = state['events'][state['last_event_id']]
        e['received_at'] = time.time() - 1000
        e.pop('progress', None)
        b.write_json(b.run_dir(sid) / 'state.json', state)

    def test_received_exit_gets_distinct_reminder_without_reexecuting(self):
        sid, eid = self.event(); b.receive_event(sid, eid); self.overdue(sid)
        with patch.object(b, 'start_job') as jobs:
            b.recover_events(); b.recover_events()
        jobs.assert_not_called()
        event = self.state(sid)['events'][eid]
        self.assertEqual(event['status'], 'received')
        self.assertNotEqual(event['reminder']['id'], eid)
        b.deliver_once(sid); b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)
        self.assertIn('event_id=' + eid, self.added()[0]['input'][0]['text'])
        self.assertTrue(b.receive_event(sid, eid)['proceed'])
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            first = b.start_job(sid, 'evaluate', event_id=eid)
            second = b.start_job(sid, 'evaluate', event_id=eid)
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['previous_result'], first)
        self.assertEqual(self.state(sid)['events'][eid]['reminder']['status'], 'processed')

    def test_pause_blocks_reminder_and_removes_queued_reminder(self):
        sid, eid = self.event(); b.receive_event(sid, eid); self.overdue(sid)
        b.return_control(sid, 'pause'); b.recover_events()
        self.assertNotIn('reminder', self.state(sid)['events'][eid])
        b.return_control(sid, 'resume'); b.receive_event(sid, eid); self.overdue(sid)
        b.recover_events(); b.deliver_once(sid)
        self.assertEqual(len(self.db['queue']), 1)
        b.return_control(sid, 'pause')
        self.assertEqual(self.db['queue'], [])

    def test_running_stage_never_gets_a_reminder(self):
        sid, eid = self.event(); b.receive_event(sid, eid); self.overdue(sid)
        with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
            b.start_job(sid, 'evaluate', event_id=eid)
        b.recover_events()
        self.assertNotIn('reminder', self.state(sid)['events'][eid])

    def test_reminders_have_a_bounded_budget(self):
        sid, eid = self.event(); b.receive_event(sid, eid); self.overdue(sid)
        for _ in range(3):
            b.recover_events()
            state = self.state(sid); event = state['events'][eid]
            self.assertEqual(event['reminder']['status'], 'pending')
            event['reminder']['status'] = 'unacknowledged'
            event['progress']['waiting_since'] = time.time() - 1000
            b.write_json(b.run_dir(sid) / 'state.json', state)
        b.recover_events()
        progress = self.state(sid)['events'][eid]['progress']
        self.assertEqual(progress['reminders'], 3)
        self.assertEqual(progress['status'], 'needs_manual_continuation')


class ProjectProgressTests(ProjectTests):
    def test_batch_receipt_without_child_action_is_recovered(self):
        owner = {'thread_id': OWNER, 'cwd': str(self.repo), 'socket': '/fixture', 'transport': 'cli_queue'}
        with patch.object(b, 'bind_owner', return_value=owner), patch.object(b, 'spawn_dispatch'):
            pid = self.create([self.task('a')], notify=True, caller=OWNER); self.start(pid)
            child = self.child(pid, 'a'); child.update(phase='responded', thread_id=OTHER)
            b.record_event(child); b.write_json(b.run_dir(child['session_id']) / 'state.json', child)
            b.project_manager().tick(pid)
            state = b.read_json(b.run_dir(pid) / 'state.json'); eid = state['last_event_id']
            b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            state = b.read_json(b.run_dir(pid) / 'state.json')
            state['events'][eid]['received_at'] = time.time() - 1000
            b.write_json(b.run_dir(pid) / 'state.json', state)
            b.recover_events()
            event = b.read_json(b.run_dir(pid) / 'state.json')['events'][eid]
            self.assertIn('reminder', event)
            receipt = b.call_tool('telos_project_receive', {'project_id': pid, 'event_id': eid}, {'threadId': OWNER})
            self.assertTrue(receipt['proceed']); self.assertEqual(len(receipt['events']), 1)
            self.assertEqual(self.state(pid)['jobs_started'], 1)

    def test_project_archive_keeps_child_and_final_acceptance(self):
        pid = self.create([self.task('a')]); self.start(pid); self.submit(pid, 'a')
        b.project_manager().integrate(pid); packet = self.check_sync(pid)
        b.project_manager().finish(pid, packet['packet_id'], self.scores(), [], 'Exact integration reviewed')
        b.storage_manager().manage('cleanup', pid, dry_run=False)
        self.assertTrue(b.project_manager().status(pid)['accepted'])
        self.assertEqual(len(b.experience_manager().lookup(self.repo)), 1)
        self.assertFalse((b.CACHE / 'projects' / pid).exists())


class ArchiveTests(BridgeTests):
    def test_inventory_reuses_observations_and_refreshes_on_the_next_query(self):
        import storage
        sid = self.create(notify=False)
        state = b.read_json(b.run_dir(sid) / 'state.json')
        tree = Path(state['worktree'])
        growth = b'created during inventory\n'
        original = storage.size
        observed = []
        def measure(path):
            value = original(path)
            observed.append(str(path))
            if path == tree:
                (tree / 'new-output.txt').write_bytes(growth)
            return value
        with patch.object(storage, 'size', side_effect=measure):
            first = b.storage_manager().manage('inventory', sid)
        self.assertEqual(first['bytes'], sum(item['bytes'] for item in first['paths']))
        self.assertEqual(observed, [item['path'] for item in first['paths']])
        second = b.storage_manager().manage('inventory', sid)
        self.assertEqual(second['bytes'], first['bytes'] + len(growth))
        self.assertEqual((tree / 'new-output.txt').read_bytes(), growth)

    def accepted(self):
        sid = self.create(); state = self.ready_review(sid)
        b.grade(sid, state['packet_id'], self.scores(), [])
        return sid, state

    def test_preview_and_cleanup_preserve_restorable_code_and_acceptance(self):
        sid, state = self.accepted()
        preview = b.storage_manager().manage('cleanup', sid)
        self.assertTrue(preview['dry_run']); self.assertTrue(Path(state['worktree']).exists())
        result = b.storage_manager().manage('cleanup', sid, dry_run=False)
        self.assertFalse(Path(state['worktree']).exists())
        self.assertTrue(b.status(sid)['accepted'])
        archive = Path(result['archive'])
        restored = self.base / 'restored'
        restored.mkdir(); b.git(restored, 'init', '-q')
        b.git(restored, 'fetch', str(archive / 'code.bundle'), '+refs/exam-loop/archive/*:refs/heads/recovered/*')
        manifest = b.read_json(archive / 'manifest.json')
        b.git(restored, 'checkout', state['submitted_commit'])
        self.assertIn('a + b', (restored / 'calc.py').read_text())
        self.assertTrue(manifest['evidence'])
        self.assertTrue(b.read_json(b.run_dir(sid) / 'state.json')['archived'])
        self.assertEqual(b.storage_manager().manage('cleanup', sid, dry_run=False)['archive'], str(archive))

    def test_dirty_or_running_task_cannot_be_cleaned(self):
        sid, state = self.accepted()
        (Path(state['worktree']) / 'important.txt').write_text('unsubmitted')
        with self.assertRaisesRegex(b.GateError, 'Uncommitted'):
            b.storage_manager().manage('cleanup', sid, dry_run=False)
        self.assertTrue((Path(state['worktree']) / 'important.txt').exists())
        other = self.create()
        with self.assertRaises(b.GateError):
            b.storage_manager().manage('cleanup', other, dry_run=False)

    def test_corrupt_archive_blocks_cleanup(self):
        sid, state = self.accepted()
        result = b.storage_manager().manage('archive', sid, dry_run=False)
        (Path(result['archive']) / 'code.bundle').write_text('broken')
        with self.assertRaises(b.GateError):
            b.storage_manager().manage('cleanup', sid, dry_run=False)
        self.assertTrue(Path(state['worktree']).exists())

    def test_clean_but_changed_accepted_commit_is_not_deleted(self):
        sid, state = self.accepted()
        (Path(state['worktree']) / 'calc.py').write_text('changed')
        b.snapshot(state, 'new-commit')
        with self.assertRaises(b.GateError):
            b.storage_manager().manage('cleanup', sid, dry_run=False)
        self.assertTrue(Path(state['worktree']).exists())

    def test_cancelled_detached_commits_are_preserved_before_cleanup(self):
        sid = self.create(); state = b.read_json(b.run_dir(sid) / 'state.json')
        (Path(state['worktree']) / 'valuable.py').write_text('VALUE = 42')
        commit, _ = b.snapshot(state, 'preserve')
        b.return_control(sid, 'cancel')
        result = b.storage_manager().manage('cleanup', sid, dry_run=False)
        self.assertIn(commit, result['preserved_commits'])
        self.assertEqual(b.git(self.repo, 'rev-parse', f'refs/exam-loop/{sid}/{commit}'), commit)


class IsolationTests(BridgeTests):
    def test_real_codex_shell_uses_relay_without_keys_or_external_reads(self):
        captured = []
        secret = self.base / 'other-project.env'; secret.write_text('SYNTHETIC_ONLY')
        with socket.socket() as forbidden:
            forbidden.bind(('127.0.0.1', 0)); forbidden.listen()
            code = ("import os,socket\nfrom pathlib import Path\n"
                    "assert not any(os.environ.get(k) for k in ['ARK_API_KEY','OPENAI_API_KEY','TELOS_RELAY_TOKEN'])\n"
                    "assert 'a + b' in Path('calc.py').read_text()\n"
                    f"try:\n Path({str(secret)!r}).read_text()\n raise AssertionError('read escaped')\nexcept PermissionError: pass\n"
                    f"try:\n socket.create_connection(('127.0.0.1',{forbidden.getsockname()[1]}),timeout=2)\n raise AssertionError('network escaped')\nexcept PermissionError: pass\n"
                    "Path('guard.txt').write_text('ISOLATION_OK')\nprint('ISOLATION_OK')")
            command = "cat <<'EOF'\nHEREDOC_OK\nEOF\n" + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
            class Model(BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_POST(self):
                    req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    captured.append(req)
                    done = any(x.get('type') == 'function_call_output' for x in req.get('input', []))
                    item = ({'type': 'message', 'id': 'msg_probe', 'role': 'assistant', 'content': [
                                {'type': 'output_text', 'text': 'Probe complete.', 'annotations': []}]} if done else
                            {'type': 'function_call', 'id': 'fc_probe', 'call_id': 'call_probe', 'name': 'exec_command',
                             'arguments': json.dumps({'cmd': command, 'max_output_tokens': 1000})})
                    response = {'id': 'resp_probe', 'object': 'response', 'status': 'completed', 'model': 'probe',
                                'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                    events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                              {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                              {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                              {'type': 'response.completed', 'response': response}]
                    data = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
                    self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            service = ThreadingHTTPServer(('127.0.0.1', 0), Model)
            threading.Thread(target=service.serve_forever, daemon=True).start()
            try:
                sid = self.create(); state, contract = b.load_session(b.run_dir(sid))
                with model_relay(f'http://127.0.0.1:{service.server_port}/v1', 'SYNTHETIC_UPSTREAM_KEY') as (url, token, port):
                    with patch.object(b, 'config', return_value={**b.config(), 'job_timeout': 40}):
                        result = b.isolated_peer_turn(state, contract, 'Run the local isolated probe.', url, token, port)
                self.assertTrue(result['thread_id'])
                self.assertEqual((Path(state['worktree']) / 'guard.txt').read_text(), 'ISOLATION_OK')
                outputs = [x for req in captured for x in req.get('input', []) if x.get('type') == 'function_call_output']
                self.assertIn('ISOLATION_OK', json.dumps(outputs))
                self.assertIn('HEREDOC_OK', json.dumps(outputs))
                self.assertNotIn('SYNTHETIC_UPSTREAM_KEY', json.dumps(captured))
            finally:
                service.shutdown(); service.server_close()

    def test_read_allowlist_blocks_synthetic_secrets_and_other_project(self):
        sid = self.create(); state = b.read_json(b.run_dir(sid) / 'state.json')
        fake_home = self.base / 'fake-home'; fake_home.mkdir()
        probes = [fake_home / '.ssh/id_rsa', fake_home / '.aws/credentials',
                  fake_home / '.local/share/opencode/auth.json', self.base / 'other/.env']
        for p in probes:
            p.parent.mkdir(parents=True, exist_ok=True); p.write_text('SYNTHETIC_ONLY')
        with patch.object(b, 'HOME', fake_home):
            profile = b.sandbox_profile(writable=[state['worktree']])
        code = 'from pathlib import Path\n'
        code += "assert 'a + b' in Path('calc.py').read_text()\n"
        for p in probes:
            code += f"try:\n Path({str(p)!r}).read_text()\n raise AssertionError('read escaped')\nexcept PermissionError: pass\n"
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', code],
                                cwd=state['worktree'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_dependency_is_read_only(self):
        dependency = self.base / 'dependency'; dependency.mkdir(); (dependency / 'data').write_text('fixture')
        sid = self.create(dependency_paths=[str(dependency)]); state, contract = b.load_session(b.run_dir(sid))
        _, tree = b.snapshot(state, 'dep')
        code = f"from pathlib import Path\np=Path({str(dependency / 'data')!r})\nassert p.read_text() == 'fixture'\ntry:\n p.write_text('bad')\n raise AssertionError('write escaped')\nexcept PermissionError: pass"
        result = b.executable_checks({**contract, 'checks': [{'id': 'dep', 'argv': ['{python}', '-c', code]}]}, tree)
        self.assertEqual(result[0]['status'], 'passed', result)

    def test_relay_rejects_unauthenticated_arbitrary_paths_and_network_escape(self):
        with model_relay('http://127.0.0.1:1/v1', 'FAKE_KEY') as (url, token, port):
            for path, auth, expected in [('/responses', '', 403), ('/untrusted', 'Bearer ' + token, 404)]:
                req = urllib.request.Request(url + path, data=b'{}', headers={'Authorization': auth})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, expected)
            with socket.socket() as other:
                other.bind(('127.0.0.1', 0)); other.listen()
                sid = self.create(); state = b.read_json(b.run_dir(sid) / 'state.json')
                profile = b.sandbox_profile(writable=[state['worktree']], relay_port=port)
                code = f"import socket\ns=socket.socket()\ntry:\n s.connect(('127.0.0.1',{other.getsockname()[1]}))\n raise AssertionError('network escaped')\nexcept PermissionError: pass"
                result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', code],
                                        cwd=state['worktree'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (EvaluationTests, ProgressTests, ProjectProgressTests, ArchiveTests, IsolationTests):
        suite.addTests(cls(name) for name in cls.__dict__ if name.startswith('test_'))
    return suite

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).with_name("telos.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def synthetic_exam(state, contract, commit):
    """A deterministic exam boundary for tests focused on scheduling and grading."""
    from closed_book import question_payload
    state['thread_id'] = state.get('thread_id') or '00000000-0000-4000-8000-000000000001'
    binding = contract['executor']
    return {'mode': 'closed_book', 'executor_thread_id': state['thread_id'], 'session_id': state['session_id'],
            'commit': commit, 'same_executor': True, 'tool_call_count': 0,
            'public_payload_hash': b.digest(question_payload(contract)), 'backend': b.providers.public_binding(binding),
            'answers': [{'id': 'q1', 'answer': '5'}, {'id': 'q2', 'answer': '3'}],
            'attempts': [{'requests': [{'tools': [], 'tool_choice': 'none', 'model': binding['model'],
                                       'reasoning_effort': binding['reasoning_effort'], 'request_hash': '0' * 64,
                                       'input_hash': '0' * 64}],
                          'responses': [{'tool_call_count': 0, 'response_hash': '0' * 64}]}]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        # Scheduling fixtures mock subprocess.Popen; keep observer discovery
        # away from both that mock and the developer's real terminal panes.
        self.patches = [patch.object(b, "STATE", self.base / "private"),
                        patch.object(b, "CACHE", self.base / "cache"),
                        patch.object(b, "CONFIG", self.base / "config.json"),
                        patch.object(b.watch_runtime, "tmux",
                                     side_effect=OSError("tmux is isolated in bridge fixtures"))]
        for p in self.patches:
            p.start()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        b.git(self.repo, "init", "-q")
        (self.repo / "calc.py").write_text("def add(a, b): return a + b\n")
        b.git(self.repo, "add", "calc.py")
        b.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "initial")
        self.q = [{"id": "q1", "prompt": "What is add(2, 3)?", "reference": "The correct result is exactly five.", "points": 50, "minimum": 40},
                  {"id": "q2", "prompt": "What is add(-2, 5)?", "reference": "The correct result is exactly three.", "points": 50}]
        self.checks = [{"id": "add", "argv": ["{python}", "-c", "from calc import add; assert add(2,3)==5; assert add(-2,5)==3"]}]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def create(self, **kw):
        # Legacy-gate regression fixtures intentionally retain closed-book mode.
        data = {"repo": str(self.repo), "task": "Implement addition.", "questions": self.q, "checks": self.checks, 'assessment_mode': 'closed_book', '_workflow_version': 2}
        data.update(kw)
        return b.create_session(**data)["session_id"]

    def ready_review(self, sid, check_status="passed"):
        root = b.run_dir(sid)
        s = b.read_json(root / "state.json")
        sha = b.git(s["worktree"], "rev-parse", "HEAD")
        packet = {"session_id": sid, "round": 1, "commit": sha, "contract_hash": s["contract_hash"],
                  "checks": [{"id": "add", "status": check_status}],
                  "exam": {"answers": [{"id": "q1", "answer": "5"}, {"id": "q2", "answer": "3"}]}}
        contract = b.read_json(root / 'contract.json')
        if contract.get('workflow_version') == 3:
            packet['exam'] = synthetic_exam(s, contract, sha)
        b.write_json(root / "attempt-1.json", packet)
        s.update(phase="awaiting_review", rounds=1, packet_id=b.digest(packet), submitted_commit=sha)
        b.write_json(root / "state.json", s)
        return s

    def scores(self, a=50, c=50):
        return [{"id": "q1", "points": a, "rationale": "Reviewed result."},
                {"id": "q2", "points": c, "rationale": "Reviewed result."}]

    def test_real_checks_and_archive(self):
        sid = self.create()
        s = b.read_json(b.run_dir(sid) / "state.json")
        sha, tree = b.snapshot(s, 1)
        result = b.executable_checks(b.read_json(b.run_dir(sid) / "contract.json"), tree)
        self.assertEqual(result[0]["status"], "passed", result)
        self.assertEqual(sha, s["base_commit"])

    def test_failed_checks_override_perfect_answers(self):
        sid = self.create()
        s = self.ready_review(sid, "failed")
        out = b.grade(sid, s["packet_id"], self.scores(), [])
        self.assertFalse(out["accepted"])
        self.assertEqual(out["score"], 100)

    def test_critical_minimum_and_total(self):
        sid = self.create(threshold=80)
        s = self.ready_review(sid)
        out = b.grade(sid, s["packet_id"], self.scores(35, 50), [])
        self.assertFalse(out["accepted"])
        self.assertEqual(out["critical_failed"], ["q1"])

    def test_accept_then_dirty_invalidates_status(self):
        sid = self.create()
        s = self.ready_review(sid)
        self.assertTrue(b.grade(sid, s["packet_id"], self.scores(), [])["accepted"])
        self.assertTrue(b.status(sid)["accepted"])
        (Path(s["worktree"]) / "extra.txt").write_text("change")
        self.assertFalse(b.status(sid)["accepted"])

    def test_dirty_before_grade_rejected(self):
        sid = self.create()
        s = self.ready_review(sid)
        (Path(s["worktree"]) / "calc.py").write_text("bad")
        with self.assertRaises(b.GateError):
            b.grade(sid, s["packet_id"], self.scores(), [])

    def test_contract_tamper_rejected(self):
        sid = self.create()
        path = b.run_dir(sid) / "contract.json"
        d = b.read_json(path)
        d["threshold"] = 1
        b.write_json(path, d)
        with self.assertRaises(b.GateError):
            b.status(sid)

    def test_missing_duplicate_bool_and_inflated_scores(self):
        for scores in [self.scores()[:1], [self.scores()[0]] * 2, self.scores(51, 50), self.scores(True, 50)]:
            sid = self.create()
            s = self.ready_review(sid)
            with self.assertRaises(b.GateError):
                b.grade(sid, s["packet_id"], scores, [])

    def test_no_checks_and_dirty_source_rejected(self):
        with self.assertRaises(b.GateError):
            self.create(checks=[])
        (self.repo / "untracked").write_text("keep")
        with self.assertRaises(b.GateError):
            self.create()
        self.assertEqual((self.repo / "untracked").read_text(), "keep")

    def test_private_reference_not_sent_in_exam(self):
        contract = {"task": "Add", "context": "Math", "questions": self.q, "checks": self.checks, "model": "glm-5.3"}
        seen = []
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): self.close()
        def request(req, **kw):
            payload = json.loads(req.data)
            seen.append(payload)
            return Response(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"answers":[{"id":"q1","answer":"5"},{"id":"q2","answer":"3"}]}'}}], "model": "glm-5.3"}).encode())
        with patch.object(b, "credential", return_value=("test-secret", "test")), patch.object(b, "model_request", request):
            result = b.answer_exam(contract)
        payload = json.dumps(seen[0])
        self.assertNotIn("tools", seen[0])
        self.assertEqual(len(seen[0]["messages"]), 2)
        self.assertNotIn(self.q[0]["reference"], payload)
        self.assertNotIn("from calc", payload)
        self.assertEqual(result["tool_count"], 0)

    def test_invalid_exam_answers_fail_closed(self):
        for text in ["Not JSON", '{"answers":[]}', '{"answers":[{"id":"q1","answer":""}]}',
                     '{"answers":[{"id":"q1","answer":"5"},{"id":"q1","answer":"5"}]}']:
            with self.assertRaises(b.GateError):
                b.parse_answers(text, self.q)

    def test_path_traversal_and_leak_guard(self):
        with self.assertRaises(b.GateError):
            b.run_dir("../../other")
        with self.assertRaises(b.GateError):
            b.deny_leak(self.q[0]["reference"], {"questions": self.q, "checks": self.checks})

    def test_check_cannot_read_private_and_has_no_key(self):
        sid = self.create()
        s = b.read_json(b.run_dir(sid) / "state.json")
        _, tree = b.snapshot(s, 1)
        secret = b.run_dir(sid) / "contract.json"
        code = f"import os,pathlib; assert not os.environ.get('ARK_API_KEY'); pathlib.Path({str(secret)!r}).read_text()"
        with patch.dict(os.environ, {"ARK_API_KEY": "not-for-checks"}):
            result = b.executable_checks({"checks": [{"id": "blocked", "argv": ["{python}", "-c", code]}]}, tree)
        self.assertEqual(result[0]["status"], "failed")
        self.assertIn("Operation not permitted", result[0]["stderr"])

    def test_mcp_dispatch_and_error(self):
        sid = self.create()
        self.assertEqual(b.call_tool("telos_status", {"session_id": sid})["phase"], "ready")
        with self.assertRaises(b.GateError):
            b.call_tool("telos_review", {"session_id": sid})

    def test_evidence_tamper_rejected(self):
        sid = self.create()
        self.ready_review(sid)
        p = b.run_dir(sid) / "attempt-1.json"
        v = b.read_json(p)
        v["checks"][0]["status"] = "failed"
        b.write_json(p, v)
        with self.assertRaises(b.GateError):
            b.review_packet(sid)

    def test_stale_packet_and_blockers(self):
        sid = self.create()
        s = self.ready_review(sid)
        with self.assertRaises(b.GateError):
            b.grade(sid, "old", self.scores(), [])
        self.assertFalse(b.grade(sid, s["packet_id"], self.scores(), ["Known blocker"])["accepted"])

    def test_cannot_send_while_pending_review(self):
        sid = self.create(max_rounds=1)
        self.ready_review(sid)
        with self.assertRaises(b.GateError):
            b.start_job(sid, "send", "more work")

    def test_round_budget_cannot_be_bypassed(self):
        sid = self.create(max_rounds=1)
        s = self.ready_review(sid)
        b.grade(sid, s["packet_id"], self.scores(30, 30), [])
        with self.assertRaises(b.GateError):
            b.start_job(sid, "send", "try again")

    def test_failed_spawn_does_not_leave_running(self):
        sid = self.create()
        with patch.object(b.subprocess, "Popen", side_effect=OSError("no process")):
            with self.assertRaises(b.GateError):
                b.start_job(sid, "send", "Implement addition")
        self.assertEqual(b.status(sid)["phase"], "ready")
        self.assertEqual(b.status(sid)["turns"], 0)

    def test_external_check_code_rejected(self):
        with self.assertRaises(b.GateError):
            self.create(checks=[{"id": "external", "argv": ["python3", "/tmp/check.py"]}])

    def test_check_cannot_write_source_repo(self):
        sid = self.create()
        s = b.read_json(b.run_dir(sid) / "state.json")
        _, tree = b.snapshot(s, 1)
        code = f"from pathlib import Path; Path({str(self.repo / 'calc.py')!r}).write_text('tampered')"
        result = b.executable_checks({"checks": [{"id": "write-blocked", "argv": ["{python}", "-c", code]}]}, tree)
        self.assertEqual(result[0]["status"], "failed")
        self.assertIn("a + b", (self.repo / "calc.py").read_text())

    def test_worker_cannot_write_git_metadata_or_use_unix_socket(self):
        sid = self.create()
        s = b.read_json(b.run_dir(sid) / "state.json")
        worktree = Path(s["worktree"])
        gitdir = self.repo / ".git"
        profile = b.sandbox_profile(writable=[worktree], protected=[gitdir, worktree / ".git"])
        p = subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-c',
                            f"from pathlib import Path; Path({str(gitdir / 'config')!r}).write_text('bad')"],capture_output=True)
        self.assertNotEqual(p.returncode, 0)
        import socket
        socket_path = self.base / 'socket'
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(socket_path)); server.listen()
            p = subprocess.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-c',
                                f"import socket; s=socket.socket(socket.AF_UNIX); s.connect({str(socket_path)!r})"],capture_output=True)
        self.assertNotEqual(p.returncode, 0)

    def test_timeout_check_is_not_pass(self):
        sid = self.create()
        s = b.read_json(b.run_dir(sid) / 'state.json')
        _, tree = b.snapshot(s, 1)
        result = b.executable_checks({'checks':[{'id':'timeout','argv':['{python}','-c','import time; time.sleep(20)'],'timeout_seconds':1}]},tree)
        self.assertEqual(result[0]['status'],'timeout')

    def test_modified_accepted_evidence_is_rejected(self):
        sid = self.create()
        s = self.ready_review(sid)
        b.grade(sid,s['packet_id'],self.scores(),[])
        p=b.run_dir(sid)/'attempt-1.json'
        d=b.read_json(p);d['exam']['answers'][0]['answer']='changed';b.write_json(p,d)
        with self.assertRaises(b.GateError):b.status(sid)

    def test_only_matching_opencode_provider_credential_is_reused(self):
        fake_home=self.base/'home'
        p=fake_home/'.config/opencode/opencode.json';p.parent.mkdir(parents=True)
        d={'provider':{'huoshancodingplan':{'options':{'baseURL':b.DEFAULT['base_url'],'apiKey':'fixture-key'}}}}
        p.write_text(json.dumps(d))
        with patch.object(b,'HOME',fake_home),patch.dict(os.environ,{'ARK_API_KEY':''}):
            self.assertEqual(b.credential()[0],'fixture-key')
            d['provider']['huoshancodingplan']['options']['baseURL']='https://other.example/api'
            p.write_text(json.dumps(d))
            with self.assertRaises(b.GateError):b.credential()



class AutoChecksTests(unittest.TestCase):
    """Real Git and macOS check sandbox; only execution-model boundaries are offline."""
    setUp = BridgeTests.setUp
    tearDown = BridgeTests.tearDown
    scores = BridgeTests.scores

    def create(self, **kw):
        checks = [dict(c, freshness={'mode': 'snapshot'}) for c in self.checks]
        return b.create_session(repo=str(self.repo), task='Implement addition', questions=self.q,
            checks=kw.pop('checks', checks), notify=False, auto_run_checks=kw.pop('auto_run_checks', True), **kw)['session_id']

    def state(self, sid):
        return b.read_json(b.run_dir(sid) / 'state.json')

    @contextlib.contextmanager
    def offline_jobs(self):
        real = subprocess.Popen
        def launch(argv, **kw):
            if '_job' in argv:
                return Mock(pid=os.getpid())
            return real(argv, **kw)
        with patch.object(b.subprocess, 'Popen', side_effect=launch), patch.object(b, 'spawn_dispatch'):
            yield

    def deliver(self, sid, status='completed'):
        reply = dict(protocol_version=2, status=status, summary='Addition implemented',
            changed_files=['calc.py'], checks_performed=[], remaining_issues=[], clarification_questions=[])
        if status == 'checkpoint':
            reply.update(completed_work=[], remaining_work=['Finish addition'], next_step='Finish addition', evidence_paths=[])
        if status == 'blocked_environment':
            reply.update(blockers=['Missing tool'], repair_actions=['Restore tool'], evidence_paths=[])
        if status == 'needs_clarification':
            reply['clarification_questions'] = ['Which addition behavior?']
        with self.offline_jobs():
            job = b.start_job(sid, 'send', 'Implement addition')['job_id']
            with patch.object(b, 'peer_turn', return_value={'thread_id': '00000000-0000-4000-8000-000000000001',
                    'summary': json.dumps(reply) if status != 'protocol_error' else 'broken'}):
                b.background_job(sid, job)
        return job

    def complete_checks(self, sid):
        state = self.state(sid)
        with self.offline_jobs(), patch.object(b, 'peer_turn', side_effect=AssertionError('No model in checks')):
            b.background_job(sid, state['job_id'])
        return self.state(sid)

    def evaluate(self, sid):
        with self.offline_jobs():
            job = b.start_job(sid, 'evaluate')['job_id']
            with patch.object(b, 'closed_book_exam', side_effect=synthetic_exam):
                b.background_job(sid, job)
        return self.state(sid)

    def test_new_contract_freezes_disabled_default_without_changing_legacy(self):
        sid = b.create_session(repo=str(self.repo), task='Addition', questions=self.q,
                               checks=self.checks, notify=False)['session_id']
        contract = b.read_json(b.run_dir(sid) / 'contract.json')
        self.assertIn('auto_run_checks', contract)
        self.assertIs(contract['auto_run_checks'], False)
        legacy = BridgeTests.create(self, notify=False)
        self.assertNotIn('auto_run_checks', b.read_json(b.run_dir(legacy) / 'contract.json'))

    def test_completed_starts_once_without_receive_or_model_then_reuses_and_grades(self):
        sid = self.create()
        source = self.deliver(sid)
        running = self.state(sid)
        self.assertEqual(running['job_kind'], 'checks')
        self.assertEqual(running['auto_check']['source_job_id'], source)
        self.assertEqual(running['rounds'], 1)
        self.assertFalse(running['events'])  # manual fixture; no invented receipts
        with self.offline_jobs():
            b.project_manager().continue_pending(sid)
        self.assertEqual(self.state(sid)['job_id'], running['job_id'])
        done = self.complete_checks(sid)
        self.assertEqual(done['auto_check']['status'], 'passed')
        self.assertEqual(done['phase'], 'responded')
        self.assertEqual(b.status(sid)['usage']['totals']['requests'], 0)
        with patch.object(b, 'executable_checks', side_effect=AssertionError('Successful evidence must be reused')):
            reviewed = self.evaluate(sid)
        self.assertEqual(reviewed['phase'], 'awaiting_review', reviewed.get('last_error'))
        self.assertEqual(reviewed['rounds'], 1)
        packet = b.review_packet(sid)
        self.assertTrue(packet['evidence']['check_evidence']['reused'])
        self.assertTrue(b.grade(sid, packet['packet_id'], self.scores(), [])['accepted'])

    def test_other_statuses_never_start_checks(self):
        for status in ('checkpoint', 'blocked_environment', 'needs_clarification', 'protocol_error'):
            with self.subTest(status=status):
                sid = self.create(auto_continue_checkpoints=False)
                # Protocol correction is separately authorized; prevent its launch in this assertion.
                with patch.object(b.project_manager().__class__, 'continue_pending'):
                    self.deliver(sid, status)
                self.assertNotIn('auto_check', self.state(sid))
                self.assertEqual(self.state(sid)['rounds'], 0)

    def test_running_evaluate_returns_status_without_new_job_or_round(self):
        sid = self.create(); self.deliver(sid)
        before = self.state(sid)
        with self.offline_jobs():
            result = b.start_job(sid, 'evaluate')
        self.assertEqual(result['next_action'], 'await_checks_completion')
        self.assertEqual(self.state(sid)['job_id'], before['job_id'])
        self.assertEqual(self.state(sid)['rounds'], before['rounds'])

    def test_failed_and_timed_out_checks_never_enter_exam(self):
        for program, expected in [('assert False', 'failed'), ('import time; time.sleep(5)', 'timeout')]:
            sid = self.create(checks=[{'id': 'failure', 'argv': ['{python}', '-c', program],
                                      'timeout_seconds': 1, 'freshness': {'mode': 'snapshot'}}])
            self.deliver(sid); done = self.complete_checks(sid)
            self.assertEqual(done['phase'], 'not_accepted')
            self.assertEqual(b.review_packet(sid)['evidence']['checks'][0]['status'], expected)
            with self.offline_jobs(), self.assertRaises(b.GateError):
                b.start_job(sid, 'evaluate')
            self.assertFalse(b.status(sid)['accepted'])

    def test_source_or_evidence_change_blocks_reuse(self):
        for change in ('source', 'evidence', 'runner', 'environment'):
            with self.subTest(change=change):
                sid = self.create(); self.deliver(sid); done = self.complete_checks(sid)
                if change == 'source':
                    (Path(done['worktree']) / 'calc.py').write_text('def add(a,b): return 0\n')
                elif change == 'evidence':
                    p = b.run_dir(sid) / done['auto_check']['evidence_file']
                    d = b.read_json(p); d['checks'][0]['stdout'] = 'modified'; b.write_json(p, d)
                import check_runtime
                with contextlib.ExitStack() as stack:
                    if change == 'runner': stack.enter_context(patch.object(check_runtime, 'runner_identity', return_value={'changed': True}))
                    if change == 'environment': stack.enter_context(patch.object(check_runtime, 'environment_identity', return_value={'changed': True}))
                    stack.enter_context(self.offline_jobs())
                    with self.assertRaises(b.GateError): b.start_job(sid, 'evaluate')
                self.assertEqual(self.state(sid)['rounds'], 1)

    def test_pause_cancel_before_check_execution_never_runs_program(self):
        for action in ('pause', 'cancel'):
            sid = self.create(); self.deliver(sid)
            with patch.object(b, 'spawn_dispatch'): b.return_control(sid, action)
            with patch.object(b, 'executable_checks', side_effect=AssertionError('Stopped checks executed')):
                done = self.complete_checks(sid)
            self.assertNotEqual(done['auto_check']['status'], 'passed')
            self.assertFalse(b.status(sid)['accepted'])

    def test_crashed_check_without_durable_result_is_uncertain_and_not_replayed(self):
        sid = self.create(); self.deliver(sid)
        state = self.state(sid); state['execution_claim'] = state['job_id']; b.write_json(b.run_dir(sid)/'state.json', state)
        with patch.object(b, 'process_running', return_value=False): b.status(sid)
        with self.offline_jobs(): b.project_manager().continue_pending(sid)
        self.assertEqual(self.state(sid)['auto_check']['status'], 'uncertain')
        self.assertEqual(self.state(sid)['rounds'], 1)

    def test_expired_or_undeclared_freshness_rechecks_once_in_same_round(self):
        for freshness in (None, {'mode': 'max_age', 'max_age_seconds': 1}):
            checks = [dict(self.checks[0])]
            if freshness: checks[0]['freshness'] = freshness
            sid = self.create(checks=checks); self.deliver(sid); done = self.complete_checks(sid)
            import check_runtime
            with patch.object(check_runtime.time, 'time', return_value=done['auto_check']['finished_at'] + 2):
                with patch.object(b, 'executable_checks', wraps=b.executable_checks) as checks_run:
                    reviewed = self.evaluate(sid)
                self.assertEqual(checks_run.call_count, 1)
                self.assertEqual(reviewed['rounds'], 1)
                self.assertEqual(reviewed['phase'], 'awaiting_review')


    def test_durable_result_recovers_without_reexecution(self):
        sid = self.create(); self.deliver(sid)
        state = self.state(sid); write = b.write_json
        def crash(path, value):
            if str(path).endswith('state.json') and value.get('job_kind') == 'checks' and value.get('phase') == 'responded':
                raise RuntimeError('fixture crash after durable result')
            return write(path, value)
        with self.offline_jobs(), patch.object(b, 'write_json', side_effect=crash):
            with self.assertRaises(RuntimeError): b.background_job(sid, state['job_id'])
        with patch.object(b, 'process_running', return_value=False), patch.object(b, 'executable_checks', side_effect=AssertionError('replay')):
            recovered = b.status(sid)
        self.assertEqual(recovered['automatic_checks']['status'], 'passed')
        self.assertEqual(recovered['rounds'], 1)
        self.assertEqual(self.evaluate(sid)['phase'], 'awaiting_review')

    def test_concurrent_dispatch_claims_only_one_check_job(self):
        import concurrent.futures
        from projects import Projects
        sid = self.create()
        with patch.object(Projects, 'continue_pending'):
            self.deliver(sid)
        source = self.state(sid)['job_id']
        with self.offline_jobs():
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: b.project_manager().continue_pending(sid), range(2)))
        state = self.state(sid)
        self.assertNotEqual(state['job_id'], source)
        self.assertEqual(state['rounds'], 1)
        self.assertEqual(state['auto_check']['status'], 'running')
        self.assertEqual(len([p for p in b.run_dir(sid).glob('job-*.json') if len(p.stem) == 36]), 2)

    def test_source_changes_while_program_runs_never_bind_to_new_commit(self):
        sid = self.create(); self.deliver(sid); before = self.state(sid)
        real = b.executable_checks
        def checks(contract, tree):
            result = real(contract, tree)
            (Path(before['worktree']) / 'calc.py').write_text('def add(a,b): return -1\n')
            return result
        with patch.object(b, 'executable_checks', side_effect=checks): done = self.complete_checks(sid)
        self.assertEqual(done['auto_check']['status'], 'blocked')
        self.assertFalse(b.status(sid)['accepted'])

    def test_environment_block_prevents_checks_and_exam(self):
        sid = self.create(); self.deliver(sid)
        with patch.object(b, 'executable_checks', side_effect=b.project_environment.EnvironmentBlocked('fixture', 'Unavailable tool')):
            done = self.complete_checks(sid)
        self.assertEqual(done['phase'], 'blocked_environment')
        self.assertEqual(done['auto_check']['status'], 'blocked')
        with self.offline_jobs(), self.assertRaises(b.GateError): b.start_job(sid, 'evaluate')

    def test_last_round_can_continue_exam_but_cannot_allocate_more_checks(self):
        sid = self.create(max_rounds=1); self.deliver(sid); self.complete_checks(sid)
        done = self.evaluate(sid)
        self.assertEqual(done['phase'], 'awaiting_review')
        self.assertEqual(done['rounds'], 1)
        b.grade(sid, done['packet_id'], self.scores(0, 0), [])
        with self.offline_jobs(), self.assertRaises(b.GateError): b.start_job(sid, 'send', 'Try again')

    def test_contract_and_check_definition_tampering_cannot_reuse(self):
        for field in ('task', 'checks'):
            sid = self.create(); self.deliver(sid); self.complete_checks(sid)
            path = b.run_dir(sid) / 'contract.json'; contract = b.read_json(path)
            if field == 'task': contract['task'] += ' changed'
            else: contract['checks'][0]['argv'][2] = 'pass'
            b.write_json(path, contract)
            with self.offline_jobs(), self.assertRaises(b.GateError): b.start_job(sid, 'evaluate')

    def test_grade_rejects_evidence_modified_after_exam(self):
        sid = self.create(); self.deliver(sid); self.complete_checks(sid); done = self.evaluate(sid)
        p = b.run_dir(sid) / done['auto_check']['evidence_file']; evidence = b.read_json(p)
        evidence['checks'][0]['status'] = 'failed'; b.write_json(p, evidence)
        with self.assertRaises(b.GateError): b.grade(sid, done['packet_id'], self.scores(), [])


    def test_snapshot_modified_after_queue_is_not_labeled_as_original_commit(self):
        sid = self.create(); self.deliver(sid); state = self.state(sid)
        (Path(state['auto_check']['snapshot']) / 'calc.py').write_text('def add(a,b): return 99\n')
        done = self.complete_checks(sid)
        self.assertEqual(done['auto_check']['status'], 'blocked')
        self.assertEqual(done['last_failure_kind'], 'execution')

    def test_pause_during_preflight_prevents_command_launch(self):
        sid = self.create(); self.deliver(sid)
        def stop(*args, **kwargs):
            b.return_control(sid, 'pause')
        with patch.object(b.preflight, 'project', side_effect=stop):
            done = self.complete_checks(sid)
        self.assertEqual(done['auto_check']['status'], 'blocked')
        self.assertFalse((b.run_dir(sid) / done['auto_check'].get('evidence_file', 'missing')).is_file())


    def test_rejected_grade_requires_a_new_round_even_with_reused_checks(self):
        sid = self.create(max_rounds=2); self.deliver(sid); self.complete_checks(sid)
        first = self.evaluate(sid)
        b.grade(sid, first['packet_id'], self.scores(0,0), [])
        with patch.object(b, 'executable_checks', side_effect=AssertionError('Fresh checks already exist')):
            second = self.evaluate(sid)
        self.assertEqual(second['rounds'], 2)
        self.assertEqual(second['phase'], 'awaiting_review')
        b.grade(sid, second['packet_id'], self.scores(0,0), [])
        with self.offline_jobs(), self.assertRaises(b.GateError): b.start_job(sid, 'evaluate')


if __name__ == "__main__":
    unittest.main(verbosity=2)

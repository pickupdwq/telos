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


if __name__ == "__main__":
    unittest.main(verbosity=2)

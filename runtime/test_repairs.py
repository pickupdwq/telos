import json
import socket
import struct
import time
import unittest
from unittest.mock import patch, Mock

from test_telos import b
from test_callbacks import CallbackTests, OWNER, FakeRPC
from desktop_rpc import DesktopRPC
from codex_rpc import RPCError


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.contract = {"task": "Implement addition", "context": "Only answer the questions", "model": "glm-5.3-flash",
                         "questions": [{"id": "q1", "prompt": "What is 2+3?", "reference": "PRIVATE_REFERENCE", "points": 100}],
                         "checks": [{"argv": ["{python}", "-c", "PRIVATE_CHECK"]}]}

    def response(self, content, **overrides):
        return {"choices": [{"finish_reason": "stop", "message": {"content": content}, **overrides}],
                "model": "glm-5-3-flash", "usage": {"total_tokens": 8}}

    def test_bad_shape_retries_public_exam_without_changing_answers(self):
        seen = []
        responses = iter([self.response('{"answers":[{"id":"q1","answer":{"value":5}}]}'),
                          self.response('{"answers":[{"id":"q1","answer":"5"}]}')])
        def request(payload, *_):
            seen.append(json.loads(json.dumps(payload)))
            return next(responses)
        with patch.object(b, "credential", return_value=("SECRET", "test")), patch.object(b, "request_exam", request):
            result = b.answer_exam(self.contract)
        self.assertEqual(result["format_attempts"], 2)
        self.assertEqual(result["answers"], [{"id": "q1", "answer": "5"}])
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0]["response_format"], {"type": "json_object"})
        self.assertEqual(len(seen[0]["messages"]), 2)
        for private in ["PRIVATE_REFERENCE", "PRIVATE_CHECK", "SECRET"]:
            self.assertNotIn(private, json.dumps(seen))
        self.assertNotIn("tools", seen[0])

    def test_bad_answers_exhaust_bounded_attempts_without_acceptance(self):
        with patch.object(b, "credential", return_value=("SECRET", "test")), patch.object(
                b, "request_exam", return_value=self.response('{"answers":[]}')) as request:
            with self.assertRaises(b.ExamFormatError) as error:
                b.answer_exam(self.contract)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(len(error.exception.diagnostics["format_failures"]), 3)
        self.assertNotIn('"answers"', json.dumps(error.exception.diagnostics))

    def test_tool_call_and_truncation_are_not_retried_as_format(self):
        for response in [self.response('{}', finish_reason="length"),
                         self.response('{}', message={"content": '{}', "tool_calls": [{}]})]:
            with patch.object(b, "credential", return_value=("SECRET", "test")), patch.object(
                    b, "request_exam", return_value=response) as request:
                with self.assertRaises(b.GateError):
                    b.answer_exam(self.contract)
            self.assertEqual(request.call_count, 1)

    def test_non_string_and_duplicate_ids_remain_errors(self):
        for answers in [[{"id": [], "answer": "5"}], [{"id": "q1", "answer": 5}],
                        [{"id": "q1", "answer": "5", "score": 100}],
                        [{"id": "q1", "answer": "5"}, {"id": "q1", "answer": "5"}]]:
            with self.assertRaises(b.ExamFormatError):
                b.parse_answers(json.dumps({"answers": answers}), self.contract["questions"])


class DesktopCallbackTests(CallbackTests):
    # Exercise only the new scenarios here; the original suite runs separately.
    def test_desktop_fallback_binds_exact_original_thread(self):
        with patch.object(b, "CodexRPC", side_effect=RPCError("offline")), patch.object(
                b, "DesktopRPC", return_value=FakeRPC(self.db)) as desktop:
            owner = b.bind_owner(OWNER)
        self.assertEqual(owner["thread_id"], OWNER)
        self.assertEqual(owner["transport"], "desktop_ipc")
        self.assertEqual(desktop.call_args.args[1], OWNER)

    def test_explicit_cli_socket_never_reroutes_to_desktop(self):
        cfg = {**b.config(), "codex_socket": "/explicit/service.sock"}
        with patch.object(b, "config", return_value=cfg), patch.object(b, "CodexRPC", side_effect=RPCError("offline")), patch.object(b, "DesktopRPC") as desktop:
            with self.assertRaises(b.GateError):
                b.bind_owner(OWNER)
        desktop.assert_not_called()

    def test_desktop_waits_while_busy_then_delivers_once(self):
        sid, eid = self.event()
        state = self.state(sid); state["owner"]["transport"] = "desktop_ipc"
        b.write_json(b.run_dir(sid) / "state.json", state)
        self.db["thread"]["status"] = {"type": "active"}
        with patch.object(b, "owner_rpc", return_value=FakeRPC(self.db)):
            self.assertTrue(b.deliver_once(sid))
            self.assertEqual(len(self.added()), 0)
            self.db["thread"]["status"] = {"type": "idle"}
            self.assertFalse(b.deliver_once(sid))
            self.assertFalse(b.deliver_once(sid))
        self.assertEqual(len(self.added()), 1)

    def test_desktop_admitted_event_reconciles_without_duplicate(self):
        sid, eid = self.event()
        state = self.state(sid); state["owner"]["transport"] = "desktop_ipc"
        b.write_json(b.run_dir(sid) / "state.json", state)
        rpc = FakeRPC(self.db)
        original = rpc.call
        def call(method, params):
            if method == "thread/queue/add":
                self.db["calls"].append((method, params))
                self.db["turns"] = [{"id": "delivered-turn", "status": "inProgress", "items": [
                    {"type": "userMessage", "content": params["input"]}]}]
                self.db["thread"]["status"] = {"type": "active"}
                return {"desktopStarted": True, "queuedSubmission": {"id": eid}}
            return original(method, params)
        with patch.object(b, "owner_rpc", return_value=rpc), patch.object(rpc, "call", side_effect=call):
            b.deliver_once(sid)
            self.assertEqual(self.state(sid)["events"][eid]["status"], "delivered")
            b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)
        self.assertEqual(self.state(sid)["events"][eid]["turn_id"], "delivered-turn")

    def test_desktop_pending_event_is_stopped_by_pause(self):
        sid, eid = self.event()
        state = self.state(sid); state["owner"]["transport"] = "desktop_ipc"
        b.write_json(b.run_dir(sid) / "state.json", state)
        b.return_control(sid, "pause")
        with patch.object(b, "owner_rpc") as rpc:
            self.assertFalse(b.deliver_once(sid))
        rpc.assert_not_called()
        self.assertEqual(self.state(sid)["events"][eid]["status"], "pending")

    def test_exam_error_can_retry_without_another_coding_turn(self):
        sid = self.create(max_rounds=2)
        state = self.state(sid)
        state.update(phase="error", job_kind="evaluate", rounds=1, thread_id=OWNER)
        b.write_json(b.run_dir(sid) / "state.json", state)
        with patch.object(b.subprocess, "Popen", return_value=Mock(pid=123)):
            b.start_job(sid, "evaluate")
        self.assertEqual(self.state(sid)["rounds"], 2)
        self.assertEqual(self.state(sid)["turns"], 0)
        with self.assertRaises(b.GateError):
            b.start_job(sid, "evaluate")

    def test_coding_error_cannot_be_examined(self):
        sid = self.create()
        state = self.state(sid)
        state.update(phase="error", job_kind="send", thread_id=OWNER)
        b.write_json(b.run_dir(sid) / "state.json", state)
        with self.assertRaises(b.GateError):
            b.start_job(sid, "evaluate")

    def test_exam_format_failure_keeps_real_check_evidence(self):
        sid = self.create()
        root = b.run_dir(sid)
        state = self.state(sid)
        job = "b" * 32
        state.update(phase="running", rounds=1, job_kind="evaluate", job_id=job)
        b.write_json(root / "state.json", state)
        b.write_json(root / ("job-" + job + ".json"), {"kind": "evaluate"})
        error = b.ExamFormatError("format failed")
        error.diagnostics = {"format_failures": [{"attempt": 1}], "request_usage": []}
        with patch.object(b, "answer_exam", side_effect=error), patch.object(b, "credential", return_value=("SECRET", "test")):
            b.background_job(sid, job)
        self.assertEqual(self.state(sid)["phase"], "error")
        self.assertEqual(b.read_json(root / "attempt-1-checks.json")["checks"][0]["status"], "passed")
        self.assertTrue((root / "attempt-1-format-error.json").is_file())
        self.assertFalse((root / "attempt-1.json").exists())
        self.assertFalse(b.status(sid)["accepted"])


class DesktopProtocolTests(unittest.TestCase):
    def adapter(self):
        rpc = object.__new__(DesktopRPC)
        rpc.thread_id, rpc.owner_client_id = OWNER, "owner"
        rpc.state = {"id": OWNER, "cwd": "/fixture", "threadRuntimeStatus": {"type": "active"}}
        rpc.timeout = 1
        return rpc

    def test_busy_delivery_refused_before_start_request(self):
        rpc = self.adapter()
        with patch.object(rpc, "refresh"), patch.object(rpc, "_request") as request:
            with self.assertRaises(RPCError) as error:
                rpc.call("thread/queue/add", {"threadId": OWNER, "input": [], "clientUserMessageId": "event"})
        self.assertFalse(error.exception.uncertain)
        request.assert_not_called()

    def test_original_settings_are_inherited_and_event_id_preserved(self):
        rpc = self.adapter(); rpc.state["threadRuntimeStatus"]["type"] = "idle"
        with patch.object(rpc, "refresh"), patch.object(rpc, "_request", return_value={"result": {}}) as request:
            result = rpc.call("thread/queue/add", {"threadId": OWNER,
                "input": [{"type": "text", "text": "event"}], "clientUserMessageId": "event-id"})
        self.assertTrue(result["desktopStarted"])
        operation = request.call_args.args[1]["turnStart"]
        self.assertEqual(operation["request"]["clientUserMessageId"], "event-id")
        self.assertEqual(operation["context"], {"inheritThreadSettings": True})
        self.assertNotIn("model", operation["request"])
        self.assertNotIn("permissions", operation["request"])

    def test_foreign_thread_and_protocol_version_are_rejected(self):
        rpc = self.adapter()
        with self.assertRaises(RPCError):
            rpc.call("thread/read", {"threadId": "another-chat"})
        rpc.sock, peer = socket.socketpair()
        try:
            payload = json.dumps({"type": "broadcast", "method": "thread-stream-state-changed",
                "version": 99, "sourceClientId": "owner", "params": {"hostId": "local", "conversationId": OWNER,
                "change": {"type": "snapshot", "conversationState": {"id": OWNER}}}}).encode()
            peer.sendall(struct.pack("<I", len(payload)) + payload)
            with self.assertRaises(RPCError):
                rpc._receive(time.monotonic() + 1)
        finally:
            rpc.close(); peer.close()

    def test_recent_admitted_inputs_support_duplicate_reconciliation(self):
        rpc = self.adapter()
        rpc.state["turnHistory"] = {"kind": "canonical", "history": {"entitiesByKey": {
            "old": {"turnId": "old", "turnStartedAtMs": 1, "status": "completed", "params": {"input": []}},
            "new": {"turnId": "new", "turnStartedAtMs": 2, "status": "inProgress", "params": {
                "input": [{"type": "text", "text": "exact completion event"}]}}}}}
        result = rpc.call("thread/turns/list", {"threadId": OWNER, "limit": 1})
        self.assertEqual(result["data"][0]["id"], "new")
        self.assertEqual(result["data"][0]["items"][0]["content"][0]["text"], "exact completion event")

    def test_protocol_error_after_send_is_an_uncertain_delivery(self):
        rpc = self.adapter(); rpc.client_id = "test-client"
        with patch.object(rpc, "_send"), patch.object(rpc, "_receive", side_effect=RPCError("bad frame")):
            with self.assertRaises(RPCError) as error:
                rpc._request("thread-follower-start-turn", {}, 3, target=True, mutating=True)
        self.assertTrue(error.exception.uncertain)


if __name__ == "__main__":
    # Avoid re-running inherited legacy CallbackTests in the repair-only suite.
    suite = unittest.TestSuite()
    for cls in [FormatTests, DesktopCallbackTests, DesktopProtocolTests]:
        for name in cls.__dict__:
            if name.startswith("test_"):
                suite.addTest(cls(name))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

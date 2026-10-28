import json
import concurrent.futures
import threading
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

from test_telos import b, BridgeTests
from codex_rpc import RPCError

OWNER = "01900000-0000-7000-8000-000000000001"
OTHER = "01900000-0000-7000-8000-000000000002"


class FakeRPC:
    def __init__(self, database):
        self.db = database

    def __enter__(self):
        if self.db.get("offline"):
            raise RPCError("offline")
        return self

    def __exit__(self, *_):
        pass

    def call(self, method, params):
        self.db["calls"].append((method, params))
        if method == "thread/read":
            return {"thread": self.db["thread"]}
        if method == "thread/turns/list":
            if self.db.get("unmaterialized"):
                raise RPCError("thread is not materialized yet; unavailable before first user message")
            return {"data": self.db.get("turns", [])}
        if method == "thread/queue/list":
            return {"data": self.db["queue"], "nextCursor": None}
        if method == "thread/queue/add":
            result = {"id": "queue-" + str(len(self.db["queue"])),
                      "clientUserMessageId": params["clientUserMessageId"], "input": params["input"]}
            self.db["queue"].append(result)
            if self.db.get("uncertain"):
                raise RPCError("timeout after accepted", uncertain=True)
            return {"queuedSubmission": result}
        if method == "thread/queue/delete":
            if self.db.get("delete_error"):
                raise RPCError("Submission already consumed")
            self.db["queue"] = [q for q in self.db["queue"] if q["id"] != params["queuedSubmissionId"]]
            return {}
        raise AssertionError(method)


class CallbackTests(unittest.TestCase):
    create = BridgeTests.create
    ready_review = BridgeTests.ready_review
    scores = BridgeTests.scores

    def setUp(self):
        BridgeTests.setUp(self)
        self.db = {"thread": {"id": OWNER, "status": {"type": "idle"}, "cwd": str(self.repo)},
                   "queue": [], "calls": []}
        self.rpc = patch.object(b, "CodexRPC", side_effect=lambda *_: FakeRPC(self.db))
        self.real_spawn = b.spawn_dispatch
        self.spawn = patch.object(b, "spawn_dispatch")
        self.rpc.start(); self.spawn.start()
        self.desktop = patch.object(b, "DesktopRPC", side_effect=RPCError("No test Desktop owner"))
        self.desktop.start()

    def tearDown(self):
        self.rpc.stop(); self.spawn.stop(); self.desktop.stop()
        BridgeTests.tearDown(self)

    def event(self, phase="responded", **kw):
        sid = self.create(notify=True, owner_thread_id=OWNER, **kw)
        root = b.run_dir(sid)
        state = b.read_json(root / "state.json")
        state.update(phase=phase, job_id="a" * 32, job_kind="send", thread_id=OTHER,
                     last_result={"summary": "PRIVATE RESULT NEVER IN NOTIFICATION"})
        b.record_event(state)
        b.write_json(root / "state.json", state)
        return sid, state["last_event_id"]

    def state(self, sid):
        return b.read_json(b.run_dir(sid) / "state.json")

    def added(self):
        return [p for m, p in self.db["calls"] if m == "thread/queue/add"]

    def test_metadata_binds_original_caller_and_rejects_override(self):
        data = {"repo": str(self.repo), "task": "addition", "questions": self.q, "checks": self.checks,
                "skip_meeting_reason": "Direct callback ownership fixture."}
        out = b.call_tool("telos_open", data, {"threadId": OWNER})
        self.assertEqual(out["return_channel"]["owner_thread_id"], OWNER)
        with self.assertRaises(b.GateError):
            b.call_tool("telos_open", {**data, "owner_thread_id": OTHER}, {"threadId": OWNER})
        for args in (data, {**data, "owner_thread_id": OWNER}):
            with self.assertRaises(b.GateError):
                b.call_tool("telos_open", args)
        self.assertNotIn("owner_thread_id", b.TOOLS[0]["inputSchema"]["properties"])
        manual = b.call_tool("telos_open", {**data, "notify": False})
        self.assertFalse(manual["return_channel"]["enabled"])

    def test_missing_or_unloaded_owner_fails_closed(self):
        with self.assertRaises(b.GateError):
            self.create(notify=True)
        self.db["thread"]["status"] = {"type": "notLoaded"}
        with self.assertRaises(b.GateError):
            self.create(notify=True, owner_thread_id=OWNER)

    def test_completion_is_durable_and_notice_contains_no_private_text(self):
        sid, eid = self.event()
        self.assertFalse(b.deliver_once(sid))
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual(len(self.added()), 1)
        text = self.added()[0]["input"][0]["text"]
        self.assertIn(eid, text)
        self.assertNotIn("PRIVATE RESULT", text)
        self.assertNotIn(self.q[0]["reference"], text)
        self.assertNotIn(self.checks[0]["argv"][2], text)
        self.assertEqual(self.state(sid)["events"][eid]["status"], "queued")

    def test_receive_checks_owner_and_processed_event_does_not_repeat(self):
        sid, eid = self.event()
        with self.assertRaises(b.GateError):
            b.call_tool("telos_receive", {"session_id": sid, "event_id": eid}, {"threadId": OTHER})
        reply = b.call_tool("telos_receive", {"session_id": sid, "event_id": eid}, {"threadId": OWNER})
        self.assertTrue(reply["proceed"])
        with patch.object(b.subprocess, "Popen", return_value=Mock(pid=123)):
            first = b.start_job(sid, "evaluate", event_id=eid)
            repeat = b.start_job(sid, "evaluate", event_id=eid)
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(repeat["previous_result"]["job_id"], first["job_id"])
        self.assertEqual(self.state(sid)["rounds"], 1)
        self.assertFalse(b.receive_event(sid, eid)["proceed"])

    def test_ambiguous_response_reconciles_pending_queue_without_resending(self):
        sid, eid = self.event()
        self.db["uncertain"] = True
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual(self.state(sid)["events"][eid]["status"], "uncertain")
        self.db["uncertain"] = False
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual(self.state(sid)["events"][eid]["status"], "queued")
        self.assertEqual(len(self.added()), 1)

    def test_consumed_ambiguous_message_is_never_blindly_replayed(self):
        sid, eid = self.event()
        self.db["uncertain"] = True
        b.deliver_once(sid)
        self.db["queue"] = []
        self.db["uncertain"] = False
        b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)
        self.assertEqual(self.state(sid)["events"][eid]["status"], "uncertain")
        self.assertTrue(b.receive_event(sid, eid)["proceed"])

    def test_offline_retries_without_losing_event(self):
        sid, eid = self.event()
        self.db["offline"] = True
        self.assertTrue(b.deliver_once(sid))
        self.assertEqual(self.state(sid)["events"][eid]["status"], "pending")
        self.db["offline"] = False
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual(len(self.added()), 1)

    def test_pause_removes_pending_notification_and_blocks_stages(self):
        sid, eid = self.event()
        b.deliver_once(sid)
        b.return_control(sid, "pause")
        self.assertFalse(self.db["queue"])
        self.assertFalse(b.receive_event(sid, eid)["proceed"])
        self.assertFalse(b.deliver_once(sid))
        with self.assertRaises(b.GateError):
            b.start_job(sid, "evaluate", event_id=eid)
        b.return_control(sid, "resume")
        b.deliver_once(sid)
        self.assertEqual(len(self.added()), 2)

    def test_cancel_is_terminal_even_if_glm_finishes_later(self):
        sid, eid = self.event()
        b.return_control(sid, "cancel")
        self.assertFalse(b.receive_event(sid, eid)["proceed"])
        with self.assertRaises(b.GateError):
            b.return_control(sid, "resume")
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual(len(self.added()), 0)

    def test_cancel_does_not_wait_for_callback_rpc_or_enqueue_after_stop(self):
        for method in ('thread/read', 'thread/queue/add'):
            with self.subTest(method=method):
                sid, eid = self.event()
                entered, release = threading.Event(), threading.Event()
                database = self.db
                class SlowRPC(FakeRPC):
                    def call(self, name, params):
                        if name == method:
                            entered.set()
                            if not release.wait(5):
                                raise AssertionError('Test did not release callback')
                        return super().call(name, params)
                before = len(self.added())
                with patch.object(b, 'owner_rpc', side_effect=lambda owner: SlowRPC(database)):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        delivery = pool.submit(b.deliver_once, sid)
                        try:
                            self.assertTrue(entered.wait(2))
                            result = pool.submit(b.return_control, sid, 'cancel').result(timeout=.5)
                            self.assertEqual(result['continuation'], 'cancelled')
                            self.assertEqual(self.state(sid)['continuation'], 'cancelled')
                        finally:
                            release.set()
                        delivery.result(timeout=2)
                self.assertEqual(self.state(sid)['events'][eid]['status'], 'cancelled')
                self.assertFalse(self.db['queue'])
                self.assertEqual(len(self.added()) - before, int(method == 'thread/queue/add'))

    def test_pause_resume_invalidates_inflight_preparation(self):
        sid, eid = self.event()
        entered, release = threading.Event(), threading.Event()
        database = self.db
        class SlowRPC(FakeRPC):
            def call(self, method, params):
                if method == 'thread/read':
                    entered.set()
                    release.wait(5)
                return super().call(method, params)
        with patch.object(b, 'owner_rpc', side_effect=lambda owner: SlowRPC(database)):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(b.deliver_once, sid)
                try:
                    self.assertTrue(entered.wait(2))
                    b.return_control(sid, 'pause')
                    b.return_control(sid, 'resume')
                finally:
                    release.set()
                future.result(timeout=2)
        self.assertFalse(self.added())
        b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)

    def test_failed_late_queue_cleanup_survives_cancel_and_recovers(self):
        sid, eid = self.event()
        database = self.db
        class CancellingRPC(FakeRPC):
            def call(self, method, params):
                if method == 'thread/queue/add':
                    b.return_control(sid, 'cancel')
                return super().call(method, params)
        self.db['delete_error'] = True
        with patch.object(b, 'owner_rpc', side_effect=lambda owner: CancellingRPC(database)):
            b.deliver_once(sid)
        self.assertTrue(self.state(sid)['events'][eid]['queue_cleanup_pending'])
        self.db['delete_error'] = False
        b.recover_events()
        self.assertFalse(self.db['queue'])
        self.assertFalse(self.state(sid)['events'][eid]['queue_cleanup_pending'])
        self.assertEqual(self.state(sid)['continuation'], 'cancelled')

    def test_cancel_during_uncertain_queue_reconciliation_withdraws_found_message(self):
        sid, eid = self.event()
        self.db['uncertain'] = True
        b.deliver_once(sid)
        self.db['uncertain'] = False
        database = self.db
        class CancellingRPC(FakeRPC):
            def call(self, method, params):
                if method == 'thread/queue/list':
                    b.return_control(sid, 'cancel')
                return super().call(method, params)
        with patch.object(b, 'owner_rpc', side_effect=lambda owner: CancellingRPC(database)):
            b.deliver_once(sid)
        self.assertFalse(self.db['queue'])
        self.assertEqual(len(self.added()), 1)
        self.assertEqual(self.state(sid)['events'][eid]['status'], 'cancelled')

    def test_receipt_during_enqueue_is_not_overwritten(self):
        sid, eid = self.event()
        database = self.db
        class ReceivingRPC(FakeRPC):
            def call(self, method, params):
                result = super().call(method, params)
                if method == 'thread/queue/add':
                    self.db['queue'] = []  # Owner consumed the queued message.
                    self.db['receipt'] = b.receive_event(sid, eid)
                return result
        with patch.object(b, 'owner_rpc', side_effect=lambda owner: ReceivingRPC(database)):
            b.deliver_once(sid)
        self.assertTrue(self.db['receipt']['proceed'])
        self.assertEqual(self.state(sid)['events'][eid]['status'], 'received')
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual(len(self.added()), 1)

    def test_interrupted_original_turn_pauses_callback(self):
        sid, eid = self.event()
        self.db["turns"] = [{"id": "interrupt-1", "status": "interrupted"}]
        self.assertFalse(b.deliver_once(sid))
        self.assertEqual(self.state(sid)["continuation"], "paused")
        self.assertFalse(self.added())
        b.return_control(sid, "resume")
        b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)
        self.db["turns"] = [{"id": "interrupt-2", "status": "interrupted"}]
        b.deliver_once(sid)
        self.assertEqual(self.state(sid)["continuation"], "paused")

    def test_consumed_queue_is_reconciled_to_turn_without_duplicate(self):
        sid, eid = self.event()
        b.deliver_once(sid)
        text = self.added()[0]["input"][0]["text"]
        self.db["queue"] = []
        self.db["turns"] = [{"id": "callback-turn", "status": "inProgress", "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": text}]}]}]
        b.deliver_once(sid)
        event = self.state(sid)["events"][eid]
        self.assertEqual(event["status"], "delivered")
        self.assertEqual(event["turn_id"], "callback-turn")
        self.db["turns"][0]["status"] = "failed"
        b.deliver_once(sid)
        self.assertEqual(self.state(sid)["events"][eid]["status"], "unacknowledged")
        self.assertEqual(len(self.added()), 1)

    def test_new_loaded_thread_without_history_can_receive_first_callback(self):
        sid, eid = self.event()
        self.db["unmaterialized"] = True
        b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)
        self.assertEqual(self.state(sid)["events"][eid]["status"], "queued")

    def test_pause_surfaces_already_consumed_notification_race(self):
        sid, eid = self.event()
        b.deliver_once(sid)
        self.db["delete_error"] = True
        out = b.return_control(sid, "pause")
        self.assertTrue(out["delivery"]["delivery_may_have_started"])
        self.assertIn("already consumed", out["delivery"]["warnings"][0])
        self.assertFalse(b.receive_event(sid, eid)["proceed"])

    def test_recovery_retries_failed_dispatch_spawn_and_scans_queued(self):
        sid, eid = self.event()
        with patch.object(b, "spawn_dispatch", wraps=self.real_spawn), patch.object(
                b.subprocess, "Popen", side_effect=[OSError("busy"), Mock(pid=123)]) as proc:
            b.recover_events()
            b.recover_events()
            self.assertEqual(proc.call_count, 2)
        b.deliver_once(sid)
        with patch.object(b, "spawn_dispatch") as spawn:
            b.recover_events()
            spawn.assert_called_once_with(sid)

    def test_existing_dispatch_lock_prevents_duplicate_process(self):
        sid, _ = self.event()
        with (b.run_dir(sid) / "dispatch.lock").open("a+") as lock, patch.object(b.subprocess, "Popen") as proc:
            b.fcntl.flock(lock, b.fcntl.LOCK_EX | b.fcntl.LOCK_NB)
            self.real_spawn(sid)
            proc.assert_not_called()

    def test_live_recovery_watcher_checks_again_without_restart(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch.object(b, "recover_events") as recover:
            b.recovery_loop(stop)
        self.assertEqual(recover.call_count, 2)
        self.assertEqual(stop.wait.call_count, 2)

    def test_bad_session_does_not_stop_other_sessions_or_watcher(self):
        bad, _ = self.event()
        state = self.state(bad)
        state.update(phase="running", job_pid=98765)
        b.write_json(b.run_dir(bad) / "state.json", state)
        b.write_json(b.run_dir(bad) / "contract.json", {"tampered": True})
        good, _ = self.event()
        with patch.object(b, "spawn_dispatch") as spawn:
            b.recover_events()
            spawn.assert_called_once_with(good)
        self.assertTrue((b.run_dir(bad) / "recovery-error.json").exists())
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch.object(b, "recover_events", side_effect=[RuntimeError("bad cycle"), None]) as recover:
            b.recovery_loop(stop)
        self.assertEqual(recover.call_count, 2)
        self.assertTrue((b.STATE / "recovery-error.json").exists())

    def test_spawn_failure_does_not_consume_only_exam_round(self):
        sid, eid = self.event(max_rounds=1)
        b.receive_event(sid, eid)
        with patch.object(b.subprocess, "Popen", side_effect=OSError("no process")):
            with self.assertRaises(b.GateError):
                b.start_job(sid, "evaluate", event_id=eid)
        state = self.state(sid)
        self.assertEqual(state["rounds"], 0)
        self.assertEqual(state["phase"], "responded")
        self.assertEqual(state["events"][eid]["actions"], {})
        with patch.object(b.subprocess, "Popen", return_value=Mock(pid=123)), \
                patch('time.time', return_value=b.time.time() + 100):
            b.start_job(sid, "evaluate", event_id=eid)
            self.assertTrue(b.start_job(sid, "evaluate", event_id=eid)["duplicate"])
        self.assertEqual(self.state(sid)["rounds"], 1)

    def test_accepted_task_cannot_be_reopened_by_new_send(self):
        sid, eid = self.event("awaiting_review")
        state = self.ready_review(sid)
        b.receive_event(sid, eid)
        b.grade(sid, state["packet_id"], self.scores(), [], eid)
        with self.assertRaises(b.GateError):
            b.start_job(sid, "send", "new work", eid)
        self.assertEqual(self.state(sid)["phase"], "accepted")

    def test_short_private_answer_is_blocked(self):
        with self.assertRaises(b.GateError):
            b.deny_leak("The answer is 5", {"questions": [{"reference": "5"}], "checks": []})

    def test_dead_worker_creates_error_event(self):
        sid = self.create(notify=True, owner_thread_id=OWNER)
        state = self.state(sid)
        state.update(phase="running", job_id="d" * 32, job_pid=99999999, job_started=0)
        b.write_json(b.run_dir(sid) / "state.json", state)
        with patch.object(b.os, "kill", side_effect=ProcessLookupError):
            out = b.status(sid)
        self.assertEqual(out["phase"], "error")
        self.assertEqual(self.state(sid)["events"]["d" * 32]["status"], "pending")

    def test_unloaded_or_changed_thread_is_not_resumed(self):
        sid, eid = self.event()
        self.db["thread"]["status"] = {"type": "notLoaded"}
        self.assertTrue(b.deliver_once(sid))
        self.db["thread"].update(status={"type": "idle"}, cwd="/different")
        self.assertTrue(b.deliver_once(sid))
        self.assertFalse(self.added())

    def test_grade_retry_is_idempotent_and_keeps_real_gate(self):
        sid, eid = self.event("awaiting_review")
        s = self.ready_review(sid, check_status="failed")
        b.receive_event(sid, eid)
        verdict = b.grade(sid, s["packet_id"], self.scores(), [], eid)
        self.assertFalse(verdict["accepted"])
        again = b.grade(sid, s["packet_id"], self.scores(), [], eid)
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["previous_result"]["accepted"])

    def test_superseded_callback_cannot_start_work(self):
        sid, eid = self.event()
        state = self.state(sid)
        state["job_id"] = "b" * 32
        b.record_event(state)
        b.write_json(b.run_dir(sid) / "state.json", state)
        self.assertFalse(b.receive_event(sid, eid)["proceed"])
        with self.assertRaises(b.GateError):
            b.start_job(sid, "evaluate", event_id=eid)

    def test_worker_finishes_and_saves_event_before_dispatch(self):
        sid = self.create(notify=True, owner_thread_id=OWNER)
        root = b.run_dir(sid)
        state = self.state(sid)
        state.update(phase="running", job_id="c" * 32, job_kind="send")
        b.write_json(root / "state.json", state)
        b.write_json(root / ("job-" + "c" * 32 + ".json"), {"kind": "send", "message": "work"})
        with patch.object(b, "peer_turn", return_value={"thread_id": OTHER, "summary": "done"}):
            b.background_job(sid, "c" * 32)
        result = self.state(sid)
        self.assertEqual(result["phase"], "responded")
        self.assertEqual(result["events"]["c" * 32]["status"], "pending")



class AutoCheckCallbackTests(unittest.TestCase):
    setUp = CallbackTests.setUp
    tearDown = CallbackTests.tearDown
    state = CallbackTests.state
    added = CallbackTests.added

    def test_original_owner_receipts_and_coalesced_actionable_completion(self):
        from test_telos import AutoChecksTests, synthetic_exam
        helper = AutoChecksTests()
        sid = b.create_session(repo=str(self.repo), task='Implement addition', questions=self.q,
            checks=[dict(c, freshness={'mode':'snapshot'}) for c in self.checks],
            notify=True, owner_thread_id=OWNER, auto_run_checks=True)['session_id']
        source = helper.deliver(sid)
        self.assertEqual(self.state(sid)['events'][source]['status'], 'pending')
        with helper.offline_jobs(), self.assertRaises(b.GateError): b.start_job(sid, 'evaluate', event_id=source)
        received = b.receive_event(sid, source)
        self.assertTrue(received['proceed'])
        with helper.offline_jobs(): pending = b.start_job(sid, 'evaluate', event_id=source)
        self.assertEqual(pending['next_action'], 'await_checks_completion')
        self.assertEqual(self.state(sid)['events'][source]['actions'], {})
        helper.complete_checks(sid)
        current = self.state(sid)['job_id']
        self.assertFalse(b.receive_event(sid, source)['proceed'])
        self.db['thread']['status']['type'] = 'active'
        b.deliver_once(sid); b.deliver_once(sid)
        self.assertEqual(self.added(), [])
        self.db['thread']['status']['type'] = 'idle'
        b.deliver_once(sid); b.deliver_once(sid)
        self.assertEqual(len(self.added()), 1)
        text = self.added()[0]['input'][0]['text']
        self.assertNotIn(self.checks[0]['argv'][2], text)
        self.assertTrue(b.receive_event(sid, current)['proceed'])
        with helper.offline_jobs():
            job = b.start_job(sid, 'evaluate', event_id=current)['job_id']
            with patch.object(b, 'closed_book_exam', side_effect=synthetic_exam): b.background_job(sid, job)
        self.assertTrue(b.receive_event(sid, job)['proceed'])
        packet = b.review_packet(sid)
        self.assertTrue(b.grade(sid, packet['packet_id'], BridgeTests.scores(self), [], event_id=job)['accepted'])

if __name__ == "__main__":
    unittest.main(verbosity=2)

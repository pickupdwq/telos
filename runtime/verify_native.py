"""Actual Codex service + MCP + outbox; reviewer is a deterministic local stub.

No user thread, real OpenAI credential, or external model is used in this test.
This portable test never calls a real model.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCE = Path(__file__).resolve().parent
if (SOURCE / 'runtime').exists(): SOURCE = SOURCE / 'runtime'
sys.path.insert(0, str(SOURCE))
from codex_rpc import CodexRPC


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(callback, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(.15)
    raise TimeoutError("Native integration condition not reached")


class ReviewerStub(BaseHTTPRequestHandler):
    hold_started = threading.Event()
    hold_release = threading.Event()
    calls = []
    tool_names = []
    tool_outputs = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.tool_names[:] = [{"name": t.get("name"), "type": t.get("type"),
                              "children": [x.get("name") for x in t.get("tools", [])]}
                             for t in body.get("tools", [])]
        self.tool_outputs[:] = [x.get("output") for x in body.get("input", []) if x.get("type") == "function_call_output"]
        latest = ""
        for item in body.get("input", []):
            if item.get("role") == "user":
                latest = "\n".join(p.get("text", "") for p in item.get("content", []) if isinstance(p, dict))
        if "HOLD_NATIVE_TURN" in latest:
            self.hold_started.set()
            self.hold_release.wait(30)
        match = re.search(r"session_id=([a-f0-9]{32}), event_id=([a-f0-9]{32})", latest)
        item = {"type": "message", "id": "msg_" + uuid.uuid4().hex, "role": "assistant",
                "content": [{"type": "output_text", "text": "Local transport receipt probe finished.", "annotations": []}]}
        if match:
            sid, eid = match.groups()
            call_id = "call_" + eid
            already = any(x.get("type") == "function_call_output" and x.get("call_id") == call_id
                          for x in body.get("input", []))
            if not already:
                name = "mcp__telos__telos_receive"
                names = [t.get("name", "") for t in body.get("tools", [])]
                candidates = [n for n in names if n.endswith("telos_receive")]
                if candidates:
                    name = candidates[0]
                item = {"type": "function_call", "id": "fc_" + eid, "call_id": call_id, "name": name,
                        "arguments": json.dumps({"session_id": sid, "event_id": eid})}
                for tool in body.get("tools", []):
                    if tool.get("type") == "namespace" and any(x.get("name") == "telos_receive" for x in tool.get("tools", [])):
                        item.update(name="telos_receive", namespace=tool["name"])
                self.calls.append({"session_id": sid, "event_id": eid, "name": name})
        rid = "resp_" + uuid.uuid4().hex
        response = {"id": rid, "object": "response", "status": "completed", "model": "local-probe",
                    "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        data = "".join("event: " + x["type"] + "\ndata: " + json.dumps(x) + "\n\n" for x in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--report", type=Path, default=Path(__file__).with_name("native-callback-report.json"))
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReviewerStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="gp-native-") as folder:
        base = Path(folder).resolve()
        runtime = base / "runtime"
        runtime.mkdir()
        for name in ("telos.py", "exam_loop.py", "identity.py", "codex_rpc.py", "desktop_rpc.py", "projects.py", "processes.py", "experience.py", "isolation.py", "storage.py", "providers.py", "executor.py", "closed_book.py", "supervisor.py", "legacy_exam.py", "deepseek_models.json", "check_runtime.py", "audit.py", "audit_probe.py", "audit_schema.py", "audit_browser.cjs", "local_executor.py", "chat_adapter.py"):
            shutil.copy2(args.source / name, runtime / name)
        state_root = base / "private"
        cache = base / "cache"
        socket_path = base / "control.sock"
        (runtime / "config.json").write_text(json.dumps({"codex_socket": str(socket_path),
            "callback_retry_seconds": .5, "callback_retry_window": 5, "callback_recovery_seconds": 1}))
        home = base / "leader"
        home.mkdir()
        config = '''model="local-probe"
model_provider="local_probe"
approval_policy="never"
sandbox_mode="danger-full-access"
[model_providers.local_probe]
name="Deterministic local reviewer"
base_url="http://127.0.0.1:PORT/v1"
wire_api="responses"
request_max_retries=0
stream_max_retries=0
[features]
hooks=false
plugins=false
apps=false
skip_host_skill_discovery=true
[mcp_servers.telos]
command=PYTHON
args=ARGS
[mcp_servers.telos.env]
TELOS_STATE_DIR=STATE
TELOS_CACHE_DIR=CACHE
'''
        for key, value in {"PORT": str(server.server_port), "PYTHON": json.dumps(sys.executable),
                           "ARGS": json.dumps([str(runtime / "telos.py"), "serve"]),
                           "STATE": json.dumps(str(state_root)), "CACHE": json.dumps(str(cache))}.items():
            config = config.replace(":PORT/", ":" + value + "/") if key == "PORT" else config.replace("=" + key, "=" + value)
        (home / "config.toml").write_text(config)
        spec = importlib.util.spec_from_file_location("native_bridge", runtime / "telos.py")
        b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(b)
        b.STATE, b.CACHE = state_root, cache
        repo = base / "repo"
        repo.mkdir()
        b.git(repo, "init", "-q")
        (repo / "calc.py").write_text("def add(a, b): return a + b\n")
        b.git(repo, "add", "calc.py")
        b.git(repo, "-c", "user.name=Probe", "-c", "user.email=probe@localhost", "commit", "-qm", "fixture")
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
        env["CODEX_HOME"] = str(home)
        log = (base / "service.log").open("w")
        proc = subprocess.Popen(["codex", "app-server", "--listen", "unix://" + str(socket_path)],
                                env=env, stdout=subprocess.DEVNULL, stderr=log)
        sessions = []
        try:
            wait_for(socket_path.exists)
            with CodexRPC(socket_path, timeout=30) as rpc:
                owner = rpc.call("thread/start", {"cwd": str(repo), "ephemeral": False})["thread"]["id"]
                rpc.call("mcpServerStatus/list", {"threadId": owner, "serverName": "telos"})

                def call(tool, arguments, tid=owner, expect_error=False):
                    raw = rpc.call("mcpServer/tool/call", {"threadId": tid, "server": "telos",
                                   "tool": tool, "arguments": arguments})
                    if "result" in raw:
                        raw = raw["result"]
                    if expect_error:
                        require(raw.get("isError"), "MCP accepted a request expected to be rejected")
                        return raw
                    require(not raw.get("isError"), "MCP rejected " + tool + ": " + json.dumps(raw))
                    return json.loads(raw["content"][0]["text"])

                def read(sid):
                    return b.read_json(b.run_dir(sid) / "state.json")

                fixture = {"repo": str(repo), "task": "Addition fixture; no execution needed.",
                    "questions": [{"id": "q1", "prompt": "What is addition?", "reference": "Arithmetic summation.", "points": 100}],
                    "checks": [{"id": "addition", "argv": ["{python}", "-c", "from calc import add; raise SystemExit(0 if add(1,2)==3 else 1)"]}]}

                def event_session():
                    opened = call("telos_open", fixture)
                    require(opened["return_channel"]["owner_thread_id"] == owner, "Return channel bound to wrong thread")
                    sid = opened["session_id"]
                    sessions.append(sid)
                    state = read(sid)
                    state.update(phase="responded", job_id=uuid.uuid4().hex, thread_id=str(uuid.uuid4()))
                    b.record_event(state)
                    b.write_json(b.run_dir(sid) / "state.json", state)
                    return sid, state["last_event_id"]

                sid, eid = event_session()
                b.deliver_once(sid)
                try:
                    wait_for(lambda: read(sid)["events"][eid]["status"] == "received")
                except TimeoutError:
                    print(json.dumps({"state": read(sid), "calls": ReviewerStub.calls,
                                      "tool_names": ReviewerStub.tool_names, "tool_outputs": ReviewerStub.tool_outputs,
                                      "turns": b.recent_turns(rpc, read(sid)["owner"])}), flush=True)
                    raise
                wait_for(lambda: rpc.call("thread/read", {"threadId": owner, "includeTurns": False})["thread"]["status"]["type"] == "idle")
                before = len(ReviewerStub.calls)
                b.deliver_once(sid)
                require(len(ReviewerStub.calls) == before, "Duplicate event woke the reviewer again")
                other = rpc.call("thread/start", {"cwd": str(repo), "ephemeral": False})["thread"]["id"]
                call("telos_receive", {"session_id": sid, "event_id": eid}, other, expect_error=True)
                call("telos_return_control", {"session_id": sid, "action": "cancel"})

                rpc.call("turn/start", {"threadId": owner, "input": [{"type": "text", "text": "HOLD_NATIVE_TURN"}]})
                require(ReviewerStub.hold_started.wait(10), "Busy reviewer turn did not start")
                sid2, eid2 = event_session()
                b.deliver_once(sid2)
                pending = rpc.call("thread/queue/list", {"threadId": owner})["data"]
                require(any(q["clientUserMessageId"] == eid2 for q in pending), "Busy-thread event was not queued")
                require(not any(c["event_id"] == eid2 for c in ReviewerStub.calls), "Queued event started a parallel turn")
                call("telos_return_control", {"session_id": sid2, "action": "pause"})
                require(not rpc.call("thread/queue/list", {"threadId": owner})["data"], "Pause left a pending notification")
                require(not call("telos_receive", {"session_id": sid2, "event_id": eid2})["proceed"], "Paused event allowed continuation")
                call("telos_return_control", {"session_id": sid2, "action": "resume"})
                ReviewerStub.hold_release.set()
                wait_for(lambda: read(sid2)["events"][eid2]["status"] == "received")
                call("telos_return_control", {"session_id": sid2, "action": "cancel"})
                report = {"codex_service": "real isolated app-server", "reviewer": "deterministic localhost stub",
                          "live_openai_model": False, "trusted_mcp_binding": True, "idle_auto_wakeup_and_receive": True,
                          "busy_queues_without_parallel_turn": True, "duplicate_does_not_rewake": True,
                          "wrong_thread_rejected": True, "native_queue_pause_removal": True, "resume_receipt": True}

                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            ReviewerStub.hold_release.set()
            for sid in sessions:
                try:
                    b.return_control(sid, "cancel")
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            server.shutdown()


if __name__ == "__main__":
    main()

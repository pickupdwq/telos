"""Local Desktop IPC adapter; binds only the original, already loaded chat.

Uses the Desktop's versioned follower protocol, not a second app-server. No
history files are changed and no session is resumed on a different service.
The protocol is app-version-dependent; unknown versions fail closed.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid

from codex_rpc import RPCError


class DesktopRPC:
    MAX_MESSAGE = 32 * 1024 * 1024

    def __init__(self, socket_path, thread_id, timeout=10):
        self.thread_id, self.timeout = thread_id, timeout
        self.client_id = "initializing-client"
        self.owner_client_id = None
        self.state = None
        self.model_settings = None
        self.sock = None
        path = Path(socket_path)
        try:
            info, parent = path.lstat(), path.parent.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or parent.st_mode & 0o077):
                raise RPCError("Desktop IPC must be a private socket owned by this user")
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(timeout)
            self.sock.connect(str(path))
            reply = self._request("initialize", {"clientType": "telos"}, version=0)
            self.client_id = reply["result"]["clientId"]
            reply = self._request("thread-owner-discovery", {
                "hostId": "local", "conversationId": thread_id}, version=1)
            self.owner_client_id = reply["handledByClientId"]
            self.refresh()
        except RPCError:
            self.close()
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.close()
            raise RPCError("Cannot connect to the original Desktop chat") from error

    def _send(self, message):
        body = json.dumps(message, ensure_ascii=False).encode()
        if len(body) > self.MAX_MESSAGE:
            raise RPCError("Desktop IPC request is too large")
        self.sock.sendall(struct.pack("<I", len(body)) + body)

    def _read(self, size):
        body = b""
        while len(body) < size:
            part = self.sock.recv(size - len(body))
            if not part:
                raise RPCError("Desktop IPC closed", uncertain=True)
            body += part
        return body

    def _receive(self, deadline):
        self.sock.settimeout(max(.001, deadline - time.monotonic()))
        size = struct.unpack("<I", self._read(4))[0]
        if not 0 < size <= self.MAX_MESSAGE:
            raise RPCError("Unsupported Desktop IPC frame size")
        msg = json.loads(self._read(size))
        if not isinstance(msg, dict):
            raise RPCError("Invalid Desktop IPC message")
        if msg.get("type") == "client-discovery-request":
            self._send({"type": "client-discovery-response", "requestId": msg["requestId"],
                        "response": {"canHandle": False}})
        if (msg.get("type") == "broadcast" and msg.get("method") == "thread-stream-state-changed"
                and msg.get("sourceClientId") == self.owner_client_id):
            data = msg.get("params", {})
            if data.get("hostId") == "local" and data.get("conversationId") == self.thread_id:
                change = data.get("change", {})
                if change.get("type") == "snapshot":
                    if msg.get("version") != 11:
                        raise RPCError("Unsupported Desktop conversation protocol version")
                    state = change.get("conversationState", {})
                    if state.get("id") != self.thread_id:
                        raise RPCError("Desktop chat identity changed")
                    self.state = state
        return msg

    def _request(self, method, params, version, target=False, mutating=False):
        ident = str(uuid.uuid4())
        request = {"type": "request", "requestId": ident, "sourceClientId": self.client_id,
                   "version": version, "method": method, "params": params,
                   "timeoutMs": int(self.timeout * 1000)}
        if target:
            request.update(hostId="local", targetClientId=self.owner_client_id)
        try:
            self._send(request)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                reply = self._receive(deadline)
                if reply.get("type") != "response" or reply.get("requestId") != ident:
                    continue
                if reply.get("resultType") != "success":
                    # An application error during delivery can occur after admission.
                    raise RPCError("Desktop rejected " + method + ": " + str(reply.get("error", ""))[:300],
                                   uncertain=mutating)
                if target and reply.get("handledByClientId") != self.owner_client_id:
                    raise RPCError("Desktop owner changed", uncertain=mutating)
                return reply
            raise RPCError("Desktop IPC request timed out", uncertain=mutating)
        except RPCError as error:
            if mutating:
                raise RPCError(str(error), uncertain=True) from error
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RPCError("Desktop IPC transport failed", uncertain=mutating) from error

    def refresh(self):
        self.state = None
        self.model_settings = None
        self._send({"type": "broadcast", "method": "thread-stream-following-changed", "version": 1,
                    "sourceClientId": self.client_id, "targetClientIds": [self.owner_client_id],
                    "params": {"hostId": "local", "conversationId": self.thread_id, "following": True}})
        # Read-only barrier on the same owner connection. This keeps the original
        # chat subscribed while the desktop publishes its current snapshot.
        reply = self._request("thread-follower-read-model-settings", {"conversationId": self.thread_id},
                              version=2, target=True)
        settings = reply.get('result', {}).get('settings')
        if not isinstance(settings, dict) or any(not isinstance(settings.get(k), str) or not settings[k]
                                                 for k in ('model', 'modelProvider')):
            raise RPCError('Desktop owner did not provide a complete model identity')
        # The targeted owner response is authoritative; do not infer a provider
        # from a model name, global defaults, or a historical conversation turn.
        self.model_settings = {k: settings[k] for k in ('model', 'modelProvider')}
        deadline = time.monotonic() + self.timeout
        while self.state is None and time.monotonic() < deadline:
            self._receive(deadline)
        if self.state is None:
            raise RPCError("Desktop did not publish the original chat state")

    def _turns(self):
        history = self.state.get("turnHistory", {})
        if history.get("kind") == "canonical":
            turns = list(history.get("history", {}).get("entitiesByKey", {}).values())
        else:
            turns = self.state.get("turns", [])
        result = []
        for turn in sorted(turns, key=lambda t: t.get("turnStartedAtMs") or 0, reverse=True):
            params = turn.get("params", {})
            # The admitted input is authoritative even when UI items are paged out.
            items = [{"type": "userMessage", "content": params.get("input", [])}]
            result.append({"id": turn.get("turnId"), "status": turn.get("status"), "items": items})
        return result

    def call(self, method, params):
        if params.get("threadId") != self.thread_id:
            raise RPCError("Desktop adapter cannot target another chat")
        if method == "thread/read":
            status = self.state.get("threadRuntimeStatus", {})
            if status.get("type") not in ("active", "idle"):
                raise RPCError("Original Desktop chat is not loaded")
            if not getattr(self, 'model_settings', None):
                raise RPCError('Desktop model identity has not been read from the owner')
            return {"thread": {"id": self.thread_id, "cwd": self.state.get("cwd"), "status": status,
                               **self.model_settings}}
        if method == "thread/turns/list":
            return {"data": self._turns()[:params.get("limit", 20)]}
        if method == "thread/queue/list":
            # Desktop callbacks wait in TELOS's durable outbox until idle.
            return {"data": [], "nextCursor": None}
        if method == "thread/queue/add":
            original_cwd = self.state.get("cwd")
            self.refresh()
            if self.state.get("cwd") != original_cwd:
                raise RPCError("Desktop chat working directory changed; return deferred")
            if self.state.get("threadRuntimeStatus", {}).get("type") != "idle":
                raise RPCError("Desktop chat became busy; return deferred")
            reply = self._request("thread-follower-start-turn", {
                "conversationId": self.thread_id,
                "turnStart": {"request": {"threadId": self.thread_id, "input": params["input"],
                              "clientUserMessageId": params["clientUserMessageId"]},
                              "context": {"inheritThreadSettings": True}}}, version=3, target=True, mutating=True)
            return {"queuedSubmission": {"id": params["clientUserMessageId"]},
                    "desktopStarted": True, "result": reply["result"]}
        raise RPCError("Unsupported Desktop callback operation: " + method)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

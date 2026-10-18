"""JSON-RPC over the local Codex Unix WebSocket. Standard library only."""
import base64
import hashlib
import json
import os
import socket
from identity import VERSION
import struct
import time


class RPCError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class CodexRPC:
    MAX_MESSAGE = 8 * 1024 * 1024

    def __init__(self, socket_path, timeout=10):
        self.timeout, self.seq, self.events = timeout, 0, []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(socket_path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                               f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
            header = b""
            while not header.endswith(b"\r\n\r\n") and len(header) < 8192:
                header += self._read(1)
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            lines = header.decode().split("\r\n")
            fields = {k.lower(): v.strip() for x in lines[1:] if ":" in x for k, v in [x.split(":", 1)]}
            if not lines[0].startswith("HTTP/1.1 101 ") or fields.get("sec-websocket-accept") != expected:
                raise RPCError("Invalid Codex control handshake")
            self.call("initialize", {"clientInfo": {"name": "telos_return", "version": VERSION},
                                     "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
        except Exception as error:
            self.close()
            raise RPCError("Cannot connect to local Codex control service") from error

    def _read(self, size):
        data = b""
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise RPCError("Codex control connection ended", uncertain=True)
            data += part
        return data

    def _frame(self, body, opcode=1):
        length = len(body)
        if length < 126:
            prefix = bytes([128 | opcode, 128 | length])
        elif length < 65536:
            prefix = bytes([128 | opcode, 128 | 126]) + struct.pack("!H", length)
        else:
            prefix = bytes([128 | opcode, 128 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        self.sock.sendall(prefix + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(body)))

    def _send(self, value):
        self._frame(json.dumps(value).encode())

    def _receive(self, until):
        payload = b""
        while True:
            self.sock.settimeout(max(.001, until - time.monotonic()))
            first, second = self._read(2)
            opcode, size = first & 15, second & 127
            if first & 112 or second & 128:
                raise RPCError("Unsupported Codex control frame")
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            if size + len(payload) > self.MAX_MESSAGE:
                raise RPCError("Codex control message too large")
            body = self._read(size)
            if opcode == 8:
                raise RPCError("Codex control connection closed", uncertain=True)
            if opcode == 9:
                self._frame(body, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RPCError("Unsupported Codex control message")
            payload += body
            if first & 128:
                return json.loads(payload)

    def call(self, method, params):
        self.seq += 1
        ident = self.seq
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            until = time.monotonic() + self.timeout
            while time.monotonic() < until:
                msg = self._receive(until)
                if msg.get("id") == ident and "method" not in msg:
                    if "error" in msg:
                        raise RPCError(f"Codex rejected {method}: {str(msg['error'].get('message', ''))[:400]}")
                    return msg.get("result", {})
                if "method" in msg and "id" in msg:
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "Event client cannot approve requests"}})
                else:
                    self.events = (self.events + [msg])[-100:]
            raise RPCError("Codex control request timed out", uncertain=True)
        except (OSError, ValueError) as error:
            raise RPCError("Codex control transport failed", uncertain=True) from error

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

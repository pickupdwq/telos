"""Loopback browser observer. Task state is never updated by HTTP requests."""
import argparse
import contextlib
import errno
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import webbrowser

from dashboard_data import DashboardData

PORT = 47831
HOST = '127.0.0.1'
ASSETS = Path(__file__).with_name('dashboard_web')
ROUTES = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8'),
          '/access.js': ('access.js', 'text/javascript; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8')}


def access_key(state_dir, port, *, create=False):
    """A private, per-state-root capability. Its presence opts in on this port."""
    directory = Path(state_dir) / 'dashboard'
    path = directory / f'access-{port}'
    if create and not path.exists():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, mode='w') as temporary:
            temporary.write(secrets.token_hex(32))
            temporary.flush()
            # Install a complete key or use the key a concurrent opener won.
            with contextlib.suppress(FileExistsError):
                os.link(temporary.name, path)
    try:
        key = path.read_text(encoding='ascii')
    except FileNotFoundError:
        return None
    if not re.fullmatch('[0-9a-f]{64}', key):
        raise ValueError('Invalid dashboard access file: ' + str(path))
    return key


def _without_clock_ticks(snapshot):
    # Timing accrues in the current phase even with no new Telos event. The
    # browser ticks its own clock; all non-timing fields still trigger a frame.
    projects = []
    for project in snapshot['projects']:
        row = {key: value for key, value in project.items() if key not in ('tasks', 'timing')}
        row['tasks'] = [{k: v for k, v in task.items() if k != 'timing'} for task in project['tasks']]
        projects.append(row)
    return projects, snapshot['warnings']


def _snapshot_frame(snapshot):
    data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    return b'event: snapshot\ndata: ' + data + b'\n\n'


class LiveFeed:
    """One on-demand sampler; slow readers coalesce to the latest full state."""
    interval = 0.45
    heartbeat = 3

    def __init__(self, data):
        self.data = data
        self.changed = threading.Condition()
        self.subscribers = 0
        self.worker = None
        self.closed = False
        self.failed = False
        self.revision = 0
        self.last_sample = 0.0
        self.latest = None
        self.frame = None
        self.stable = None

    def frames(self):
        with self.changed:
            if self.closed:
                return
            self.subscribers += 1
            if self.worker is None:
                self.latest = self.frame = self.stable = None
                self.failed = False
                self.worker = threading.Thread(target=self._sample, name='telos-dashboard-live', daemon=True)
                self.worker.start()
        previous = None
        connected = time.monotonic()
        try:
            while True:
                with self.changed:
                    self.changed.wait_for(lambda: self.closed or self.failed or
                                          (self.latest is not None and previous != self.revision),
                                          timeout=self.heartbeat)
                    if self.closed or self.failed:
                        return
                    # Don't keep claiming a healthy live connection if disk or
                    # process observation has stalled, including on first load.
                    reference = self.last_sample if self.latest is not None else connected
                    if time.monotonic() - reference > 3 * self.heartbeat:
                        return
                    if self.latest is None:
                        continue
                    if previous == self.revision:
                        frame = b': keepalive\n\n'
                    elif previous is None:
                        # Include up-to-date elapsed counters even on a late join.
                        frame = _snapshot_frame(self.latest)
                    else:
                        frame = self.frame
                    previous = self.revision
                yield frame
        finally:
            with self.changed:
                self.subscribers -= 1
                self.changed.notify_all()

    def _sample(self):
        while True:
            with self.changed:
                if self.closed or not self.subscribers:
                    self.worker = None
                    self.latest = self.frame = self.stable = None
                    self.changed.notify_all()
                    return
            try:
                snapshot = self.data.snapshot()
                stable = _without_clock_ticks(snapshot)
                frame = _snapshot_frame(snapshot) if stable != self.stable else None
            except Exception:
                # A background sampler must never strand waiters on an unhandled read error.
                # Close the stream instead of leaving clients believing stale
                # work is still running. A reconnect requests a fresh snapshot.
                with self.changed:
                    self.failed = True
                    self.changed.notify_all()
            else:
                with self.changed:
                    self.failed = False
                    self.last_sample = time.monotonic()
                    self.latest = snapshot
                    if frame is not None:
                        self.frame, self.stable = frame, stable
                        self.revision += 1
                        self.changed.notify_all()
            with self.changed:
                self.changed.wait_for(lambda: self.closed or not self.subscribers, timeout=self.interval)

    def close(self):
        with self.changed:
            self.closed = True
            self.changed.notify_all()


class Server(ThreadingHTTPServer):
    def __init__(self, state_dir, port, key):
        self.key = key
        self.data = DashboardData(state_dir)
        self.live = LiveFeed(self.data)
        super().__init__((HOST, port), Handler)

    def server_close(self):
        self.live.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Never mix browser output with the supervisor's MCP protocol.

    def response_headers(self, code, mime, length=None):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        if length is not None:
            self.send_header('Content-Length', str(length))
        else:
            self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'none'")
        self.end_headers()

    def reply(self, code, body=b'', mime='text/plain; charset=utf-8'):
        self.response_headers(code, mime, len(body))
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def stream_live(self):
        self.close_connection = True
        self.connection.settimeout(5)
        with contextlib.suppress(OSError), contextlib.closing(self.server.live.frames()) as frames:
            self.response_headers(200, 'text/event-stream; charset=utf-8')
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()

    def do_GET(self):
        port = self.server.server_port
        hosts = {f'{HOST}:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts:
            return self.reply(403)
        path = self.path.partition('?')[0]
        if path in ROUTES:
            filename, mime = ROUTES[path]
            try:
                content = (ASSETS / filename).read_bytes()
            except OSError:
                return self.reply(503, b'Dashboard assets unavailable')
            return self.reply(200, content, mime)
        if not path.startswith('/api/'):
            return self.reply(404)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers['Host']:
            return self.reply(403)
        if path == '/api/identity':
            challenge = self.headers.get('X-Telos-Challenge', '')
            if not re.fullmatch('[0-9a-f]{32}', challenge):
                return self.reply(400)
            return self.reply(200, identity_signature(self.server.key, challenge))
        auth = self.headers.get('Authorization', '').encode('latin-1')
        if not hmac.compare_digest(auth, ('Bearer ' + self.server.key).encode('ascii')):
            return self.reply(401)
        if path == '/api/live':
            return self.stream_live()
        detail = re.fullmatch(r'/api/tasks/([0-9a-f]{32})', path)
        if path != '/api/overview' and not detail:
            return self.reply(404)
        try:
            value = self.server.data.detail(detail[1]) if detail else self.server.data.snapshot()
            if value is not None:
                body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (OSError, ValueError, TypeError, KeyError):
            return self.reply(503, b'Dashboard state unavailable')
        if value is None:
            return self.reply(404)
        return self.reply(200, body, 'application/json; charset=utf-8')


def same_server(port, key):
    conn = HTTPConnection(HOST, port, timeout=2)
    try:
        # Challenge before sending a secret: a different application may own
        # this port. The identity probe uses a derived key, never the access key.
        challenge = secrets.token_hex(16)
        conn.request('GET', '/api/identity', headers={'X-Telos-Challenge': challenge})
        response = conn.getresponse()
        expected = identity_signature(key, challenge)
        return response.status == 200 and hmac.compare_digest(response.read(100), expected)
    except (OSError, ValueError, HTTPException):
        return False
    finally:
        conn.close()


def identity_signature(key, challenge):
    message = ('telos-dashboard-identity-v1:' + challenge).encode('ascii')
    return hmac.digest(key.encode('ascii'), message, 'sha256').hex().encode('ascii')


def host_when_enabled(state_dir, stop):
    """Only the first opted-in MCP process owns the port; others can take over."""
    try:
        key = access_key(state_dir, PORT)
    except (OSError, ValueError):
        return
    if key is None:
        return
    while not stop.is_set():
        try:
            with Server(state_dir, PORT, key) as server:
                server.timeout = 1
                while not stop.is_set():
                    server.handle_request()
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                return
            stop.wait(5)


def cli(state_dir, argv):
    parser = argparse.ArgumentParser(prog='telos dashboard', description='本机 Telos 运行看板')
    parser.add_argument('--port', type=int, default=PORT, help='本机监听端口')
    parser.add_argument('--open', action='store_true', help='在默认浏览器打开看板')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    key = access_key(state_dir, args.port, create=True)
    try:
        server = Server(state_dir, args.port, key)
    except OSError as error:
        if error.errno != errno.EADDRINUSE or not same_server(args.port, key):
            raise OSError(f'无法使用端口 {args.port}。可用 telos dashboard --port 47832 换一个端口。') from error
        server = None
    url = f'http://{HOST}:{args.port}/#access={key}'
    try:
        print(url, flush=True)
        if args.open:
            webbrowser.open(url)
        if server is not None:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        if server is not None:
            server.server_close()

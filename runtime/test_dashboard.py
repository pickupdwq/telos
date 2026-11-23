"""Browser observer transport, reuse and opt-in lifecycle."""
import contextlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import dashboard


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = 'a' * 64
        server = dashboard.Server(self.root, 0, self.key)
        self.server = server
        self.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

    def request(self, path, *, headers=None, method='GET'):
        conn = HTTPConnection(dashboard.HOST, self.port, timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            reply = conn.getresponse()
            return reply.status, reply.headers, reply.read()
        finally:
            conn.close()

    def test_static_files_and_scoped_read_only_api(self):
        for route, (filename, _) in dashboard.ROUTES.items():
            with self.subTest(route=route):
                status, headers, body = self.request(route)
                self.assertEqual(status, 200)
                self.assertEqual(body, (dashboard.ASSETS / filename).read_bytes())
                self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        auth = {'Authorization': 'Bearer ' + self.key}
        self.assertEqual(self.request('/api/overview')[0], 401)
        self.assertEqual(self.request('/api/live')[0], 401)
        self.assertEqual(self.request('/api/live', headers={**auth, 'Host': 'elsewhere.test'})[0], 403)
        self.assertEqual(self.request('/api/live', headers={**auth, 'Origin': 'https://elsewhere.test'})[0], 403)
        self.assertEqual(self.request('/api/overview', headers={**auth, 'Host': 'elsewhere.test'})[0], 403)
        self.assertEqual(self.request('/api/overview', headers={**auth, 'Origin': 'https://elsewhere.test'})[0], 403)
        status, _, body = self.request('/api/overview', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'projects': [], 'warnings': []})
        self.assertEqual(self.request('/api/tasks/' + 'b' * 32, headers=auth)[0], 404)
        self.assertEqual(self.request('/state.json', headers=auth)[0], 404)
        self.assertEqual(self.request('/api/overview', method='POST', headers=auth)[0], 501)
        self.assertEqual(list(self.root.iterdir()), [])

    def open_stream(self):
        connection = HTTPConnection(dashboard.HOST, self.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/live', headers={'Authorization': 'Bearer ' + self.key})
        response = connection.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), 'text/event-stream')
        self.assertNotIn('Content-Length', response.headers)
        return response

    def frame(self, response):
        line = response.readline()
        if line == b': keepalive\n':
            self.assertEqual(response.readline(), b'\n')
            return None
        self.assertEqual(line, b'event: snapshot\n')
        payload = response.readline()
        self.assertTrue(payload.startswith(b'data: '))
        self.assertEqual(response.readline(), b'\n')
        return json.loads(payload[6:])

    def next_snapshot(self, response):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = self.frame(response)
            if snapshot is not None:
                return snapshot
        self.fail('no changed snapshot')

    def wait_for_idle(self):
        feed = self.server.live
        with feed.changed:
            self.assertTrue(feed.changed.wait_for(lambda: not feed.subscribers and feed.worker is None, timeout=3))

    def test_shared_stream_replays_last_snapshot_and_ignores_ticking_counters(self):
        feed = self.server.live
        feed.interval, feed.heartbeat = .025, .11
        calls = []
        phase = 'running'
        def snapshot():
            calls.append(threading.get_ident())
            elapsed = {'work': len(calls)}
            return {'warnings': [], 'projects': [{'id': 'p', 'timing': elapsed, 'tasks': [
                {'id': 't', 'phase': phase, 'timing': elapsed}]}]}
        with patch.object(self.server.data, 'snapshot', side_effect=snapshot):
            first = self.open_stream()
            self.assertEqual(self.frame(first)['projects'][0]['tasks'][0]['phase'], 'running')
            second = self.open_stream()
            self.assertEqual(self.frame(second)['projects'][0]['tasks'][0]['phase'], 'running')
            self.assertIsNone(self.frame(first))
            self.assertIsNone(self.frame(second))
            self.assertGreater(len(calls), 1)
            self.assertEqual(len(set(calls)), 1)
            phase = 'accepted'
            for stream in (first, second):
                self.assertEqual(self.next_snapshot(stream)['projects'][0]['tasks'][0]['phase'], phase)
                stream.close()
            self.wait_for_idle()
            self.assertIsNone(feed.latest)
            reconnected = self.open_stream()
            self.assertEqual(self.frame(reconnected)['projects'][0]['tasks'][0]['phase'], phase)
            reconnected.close()
            self.wait_for_idle()

    def test_stream_observes_real_state_and_ends_on_read_failure(self):
        feed = self.server.live
        feed.interval, feed.heartbeat = .025, .10
        stream = self.open_stream()
        self.assertEqual(self.frame(stream), {'projects': [], 'warnings': []})
        sid = 'b' * 32
        directory = self.root / 'runs' / sid
        directory.mkdir(parents=True)
        path = directory / 'state.json'
        path.write_text(json.dumps({'session_id': sid, 'phase': 'ready', 'task_title': '核对页面',
                                    'created_at': 123, 'repo': '/src'}))
        snapshot = self.next_snapshot(stream)
        self.assertEqual(snapshot['projects'][0]['tasks'][0]['display_title'], '核对页面')
        data = path.read_bytes()
        with patch.object(self.server.data, 'snapshot', side_effect=OSError('unavailable')):
            # It may have emitted a heartbeat before the failed read.
            self.assertTrue(all(line in (b': keepalive', b'') for line in stream.read().splitlines()))
            stream.close()
            self.wait_for_idle()
        self.assertEqual(path.read_bytes(), data)
        recovered = self.open_stream()
        self.assertEqual(self.frame(recovered)['projects'][0]['phase'], 'ready')
        recovered.close()

    def test_stalled_sampler_does_not_emit_healthy_heartbeats_indefinitely(self):
        feed = self.server.live
        feed.interval, feed.heartbeat = .015, .05
        started, blocked, release = threading.Event(), threading.Event(), threading.Event()
        def snapshot():
            if started.is_set():
                blocked.set()
                release.wait(2)
            started.set()
            return {'projects': [], 'warnings': []}
        with patch.object(self.server.data, 'snapshot', side_effect=snapshot):
            stream = self.open_stream()
            try:
                self.assertEqual(self.frame(stream), {'projects': [], 'warnings': []})
                self.assertTrue(blocked.wait(1))
                rest = stream.read()
                self.assertIn(b': keepalive', rest)
                self.assertNotIn(b'event:', rest)
            finally:
                release.set()
                stream.close()
            self.wait_for_idle()

    def test_only_reuse_the_server_for_the_requested_state(self):
        self.assertTrue(dashboard.same_server(self.port, self.key))
        self.assertFalse(dashboard.same_server(self.port, 'b' * 64))
        with self.assertRaisesRegex(OSError, '端口'):
            dashboard.cli(self.root, ['--port', str(self.port)])
        path = self.root / 'dashboard' / f'access-{self.port}'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text(self.key)
        with patch.object(dashboard.webbrowser, 'open') as browser, contextlib.redirect_stdout(io.StringIO()):
            dashboard.cli(self.root, ['--port', str(self.port), '--open'])
        browser.assert_called_once_with(f'http://127.0.0.1:{self.port}/#access={self.key}')

    def test_in_process_listener_requires_opt_in_and_stops_with_mcp(self):
        with socket.socket() as sock:
            sock.bind((dashboard.HOST, 0))
            port = sock.getsockname()[1]
        stop = threading.Event()
        with patch.object(dashboard, 'PORT', port):
            dashboard.host_when_enabled(self.root, stop)
            self.assertFalse((self.root / 'dashboard').exists())
            key = dashboard.access_key(self.root, port, create=True)
            thread = threading.Thread(target=dashboard.host_when_enabled, args=(self.root, stop))
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not dashboard.same_server(port, key):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.03)
            finally:
                stop.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(dashboard.same_server(port, key))

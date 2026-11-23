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
        self.assertEqual(self.request('/api/overview', headers={**auth, 'Host': 'elsewhere.test'})[0], 403)
        self.assertEqual(self.request('/api/overview', headers={**auth, 'Origin': 'https://elsewhere.test'})[0], 403)
        status, _, body = self.request('/api/overview', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'projects': [], 'warnings': []})
        self.assertEqual(self.request('/api/tasks/' + 'b' * 32, headers=auth)[0], 404)
        self.assertEqual(self.request('/state.json', headers=auth)[0], 404)
        self.assertEqual(self.request('/api/overview', method='POST', headers=auth)[0], 501)
        self.assertEqual(list(self.root.iterdir()), [])

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

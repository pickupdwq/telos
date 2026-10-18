"""Read allowlist and a per-job, authenticated, fixed-destination model relay."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import secrets
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
import sys
from identity import data_path


def runtime_paths(install, home):
    # No entire user home, /tmp, /var, source repository or sibling project.
    return [Path(x) for x in ('/System', '/usr', '/bin', '/sbin', '/opt/homebrew',
            '/Library/Apple', '/Library/Frameworks', '/private/var/db/dyld',
            '/private/etc/ssl', '/private/etc/hosts', '/private/etc/resolv.conf',
            '/private/etc/localtime', '/dev/null', '/dev/tty', '/dev/random', '/dev/urandom')] + [
            Path(sys.base_prefix), install / 'commands', data_path(home, '.local/share/{name}-cli')] + [
            Path(p).resolve() for name in ('node',) if (p := shutil.which(name))]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@contextlib.contextmanager
def model_relay(endpoint, key, timeout=180, policy=None):
    """Only the bridge holds upstream credentials. No arbitrary URL/proxy methods."""
    token = secrets.token_urlsafe(48)
    opener = urllib.request.build_opener(NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403); return
            allowed = ('/v1/responses',) if policy else ('/v1/responses', '/v1/responses/compact')
            if self.path not in allowed:
                self.send_error(404); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16 * 1024 * 1024:
                    self.send_error(413); return
                body = self.rfile.read(length)
                if policy:
                    body = policy.prepare(body)
                req = urllib.request.Request(endpoint.rstrip('/') + self.path.removeprefix('/v1'),
                    data=body, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
                try:
                    response = opener.open(req, timeout=timeout)
                except urllib.error.HTTPError as error:
                    # Never return arbitrary provider diagnostics or redirect locations.
                    if policy and (error.code in (408, 429) or error.code >= 500):
                        policy.transport_error = 'Exam endpoint HTTP ' + str(error.code)
                    self.send_error(error.code, 'Model endpoint rejected request'); return
                with response:
                    validated = None
                    if policy:
                        validated = response.read(policy.MAX_RESPONSE_BYTES + 1)
                        policy.validate_response(validated, response.headers.get('Content-Type', 'application/json'))
                    self.send_response(response.status)
                    self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if validated is not None:
                        self.wfile.write(validated); self.wfile.flush(); return
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk); self.wfile.flush()
            except Exception as error:
                if policy:
                    # The private policy error is consumed by the bridge, not by
                    # a model; provider output cannot become an executable event.
                    policy.failure = str(error)
                    if isinstance(error, (OSError, TimeoutError, urllib.error.URLError)):
                        policy.transport_error = 'Exam endpoint transport failed'
                    try:
                        self.send_error(400, 'Closed-book response rejected')
                    except OSError:
                        pass
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', token, server.server_port
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=2)

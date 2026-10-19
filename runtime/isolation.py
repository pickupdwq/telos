"""Read allowlist and a per-job, authenticated, fixed-destination model relay."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
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
def model_relay(endpoint, key, timeout=180, policy=None, binding=None, validate=None, diagnostics=None):
    """Only the bridge holds upstream credentials. No arbitrary URL/proxy methods."""
    token = secrets.token_urlsafe(48)
    local = binding is not None and binding.get('provider') == 'local'
    opener = urllib.request.build_opener(NoRedirect(), *([urllib.request.ProxyHandler({})] if local else []))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403); return
            allowed = ('/v1/responses',) if policy or local else ('/v1/responses', '/v1/responses/compact')
            if self.path not in allowed:
                self.send_error(404); return
            try:
                if validate:
                    validate()
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16 * 1024 * 1024:
                    self.send_error(413); return
                body = self.rfile.read(length)
                if policy:
                    body = policy.prepare(body)
                suffix = self.path.removeprefix('/v1')
                chat_request = None
                if local:
                    request = json.loads(body)
                    if request.get('model') != binding['model']:
                        raise ValueError('Local request changed the frozen model')
                    if binding['wire_api'] == 'chat':
                        from chat_adapter import prepare
                        chat_request, tool_types = prepare(request)
                        body, suffix = json.dumps(chat_request, ensure_ascii=False).encode(), '/chat/completions'
                    elif binding['reasoning_effort'] == 'none':
                        request.pop('reasoning', None)
                        body = json.dumps(request, ensure_ascii=False).encode()
                headers = {'Content-Type': 'application/json'}
                if key:
                    headers['Authorization'] = 'Bearer ' + key
                req = urllib.request.Request(endpoint.rstrip('/') + suffix, data=body, headers=headers)
                try:
                    response = opener.open(req, timeout=timeout)
                except urllib.error.HTTPError as error:
                    # Never return arbitrary provider diagnostics or redirect locations.
                    if policy and (error.code in (408, 429) or error.code >= 500):
                        policy.transport_error = 'Exam endpoint HTTP ' + str(error.code)
                    self.send_error(error.code, 'Model endpoint rejected request'); return
                with response:
                    validated = None
                    content_type = response.headers.get('Content-Type', 'application/json')
                    if chat_request is not None:
                        from chat_adapter import convert, encode
                        raw = response.read(16 * 1024 * 1024 + 1)
                        if len(raw) > 16 * 1024 * 1024:
                            raise ValueError('Local response exceeds size limit')
                        converted = convert(json.loads(raw), request['model'], tool_types)
                        if converted['status'] != 'completed':
                            raise ValueError('Local Chat response is incomplete; no partial tool call was forwarded')
                        validated, content_type = encode(converted, request.get('stream', False))
                    elif policy:
                        validated = response.read(policy.MAX_RESPONSE_BYTES + 1)
                    if policy:
                        policy.validate_response(validated, content_type)
                    self.send_response(response.status)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if validated is not None:
                        self.wfile.write(validated); self.wfile.flush(); return
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk); self.wfile.flush()
            except Exception as error:
                if diagnostics is not None:
                    detail = str(error).replace(token, '[REDACTED]')
                    diagnostics['failure'] = (detail.replace(key, '[REDACTED]') if key else detail)[:600]
                if policy:
                    # The private policy error is consumed by the bridge, not by
                    # a model; provider output cannot become an executable event.
                    policy.failure = str(error)
                    if isinstance(error, (OSError, TimeoutError, urllib.error.URLError)):
                        policy.transport_error = 'Exam endpoint transport failed'
                try:
                    self.send_error(400, 'Model protocol or frozen endpoint validation failed')
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

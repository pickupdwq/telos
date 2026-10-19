"""Read allowlist and a per-job, authenticated, fixed-destination model relay."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import sys
from identity import data_path


def runtime_paths(install, home):
    from preflight import git_runtime_paths
    # No entire user home, /tmp, /var, source repository or sibling project.
    return [Path(x) for x in ('/System', '/usr', '/bin', '/sbin', '/opt/homebrew',
            '/Library/Apple', '/Library/Frameworks', '/private/var/db/dyld',
            '/private/etc/ssl', '/private/etc/hosts', '/private/etc/resolv.conf',
            '/private/etc/localtime', '/dev/null', '/dev/tty', '/dev/random', '/dev/urandom')] + [
            Path(sys.base_prefix), install / 'commands', data_path(home, '.local/share/{name}-cli')] + [
            Path(p).resolve() for name in ('node',) if (p := shutil.which(name))] + git_runtime_paths()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def usage_from_payload(raw, content_type):
    """Extract one usage object from a JSON/SSE Responses or Chat payload."""
    if not raw:
        return None
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    if 'text/event-stream' in (content_type or ''):
        usage = None
        for block in text.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
            if not data or data == '[DONE]':
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') in ('response.completed', 'response.incomplete', 'response.failed',
                                     'response.cancelled', 'response.canceled'):
                response = event.get('response')
                candidate = response.get('usage') if isinstance(response, dict) else None
                if not isinstance(candidate, dict):
                    candidate = event.get('usage')
                if isinstance(candidate, dict):
                    # Duplicate terminal events replace, never accumulate.
                    usage = candidate
        return usage
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    response = payload.get('response')
    if isinstance(response, dict) and isinstance(response.get('usage'), dict):
        return response['usage']
    usage = payload.get('usage')
    return usage if isinstance(usage, dict) else None


def response_outcome(raw, content_type):
    """Return the terminal Responses status without retaining provider bodies.

    Malformed JSON and SSE streams that end before a terminal event are reported
    as failures so accounting never records them as completed successes.
    """
    stream = 'text/event-stream' in (content_type or '')
    json_body = 'application/json' in (content_type or '')
    if not raw:
        return 'missing_terminal' if stream else ('malformed' if json_body else None)
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    if stream:
        outcome = None
        for block in text.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
            if not data or data == '[DONE]':
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'response.completed':
                outcome = 'completed'
            elif kind == 'response.incomplete':
                outcome = 'incomplete'
            elif kind == 'response.failed':
                outcome = 'failed'
            elif kind in ('response.cancelled', 'response.canceled'):
                outcome = 'cancelled'
        return outcome or 'missing_terminal'
    try:
        payload = json.loads(text)
    except ValueError:
        return 'malformed' if json_body else None
    if not isinstance(payload, dict):
        return 'malformed' if json_body else None
    response = payload.get('response')
    status = response.get('status') if isinstance(response, dict) else payload.get('status')
    if not isinstance(status, str):
        return None
    status = status.lower()
    return status if status in ('completed', 'incomplete', 'failed', 'cancelled', 'canceled') else None


def _terminal(status, outcome):
    if outcome in ('failed', 'incomplete', 'cancelled', 'canceled', 'malformed', 'missing_terminal'):
        return 'failed', 'response_' + outcome
    return status, None


@contextlib.contextmanager
def model_relay(endpoint, key, timeout=180, policy=None, binding=None, validate=None, diagnostics=None,
                usage_ledger=None, usage_context=None, budget_failure=None):
    """Only the bridge holds upstream credentials. No arbitrary URL/proxy methods."""
    token = secrets.token_urlsafe(48)
    local = binding is not None and binding.get('provider') == 'local'
    opener = urllib.request.build_opener(NoRedirect(), *([urllib.request.ProxyHandler({})] if local else []))
    context = usage_context or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403); return
            allowed = ('/v1/responses',) if policy or local else ('/v1/responses', '/v1/responses/compact')
            if self.path not in allowed:
                self.send_error(404); return
            if budget_failure and budget_failure.get('message'):
                # A budget stop or accounting failure is terminal for this relay:
                # never forward another paid request in the same invocation.
                self.send_error(400, 'Task budget or usage accounting stop; no upstream request was sent')
                return
            started = time.monotonic()
            request_id = None
            finished = False
            seen_usage = None
            content_type = ''
            tail = bytearray()
            outcome = None

            def complete(status, raw_usage=None, error_kind=None):
                nonlocal finished
                if request_id is None or finished:
                    return
                finished = True
                try:
                    usage_ledger.finish(request_id, raw_usage, status=status,
                                        latency_ms=(time.monotonic() - started) * 1000,
                                        error_kind=error_kind)
                except Exception as error:
                    if diagnostics is not None:
                        diagnostics['usage_failure'] = str(error)[:300]
                    if budget_failure is not None:
                        budget_failure['message'] = ('Usage accounting failed; further paid requests are blocked: '
                                                     + str(error)[:300])
                        budget_failure['accounting'] = True
                        budget_failure.setdefault('snapshot', None)

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
                if usage_ledger is not None:
                    try:
                        request_id = usage_ledger.begin(context.get('phase', 'work'),
                                                        provider=context.get('provider'),
                                                        model=context.get('model'),
                                                        job=context.get('job'))
                    except Exception as error:
                        from usage import BudgetExceeded
                        if isinstance(error, BudgetExceeded):
                            if budget_failure is not None:
                                budget_failure['message'] = str(error)
                                budget_failure['snapshot'] = error.snapshot
                            self.send_error(400, 'Task budget exhausted; no upstream request was sent')
                            return
                        if budget_failure is not None:
                            budget_failure['message'] = ('Usage accounting failed before forwarding; '
                                                         'no upstream request was sent: ' + str(error)[:300])
                            budget_failure['accounting'] = True
                            budget_failure['snapshot'] = None
                        self.send_error(400, 'Usage accounting failed; no upstream request was sent')
                        return
                headers = {'Content-Type': 'application/json'}
                if key:
                    headers['Authorization'] = 'Bearer ' + key
                req = urllib.request.Request(endpoint.rstrip('/') + suffix, data=body, headers=headers)
                try:
                    response = opener.open(req, timeout=timeout)
                except urllib.error.HTTPError as error:
                    # Provider error bodies can still report the usage of the
                    # attempted request. Extract only bounded usage, never log
                    # or forward arbitrary provider diagnostics.
                    failed_usage = None
                    failed_type = ''
                    try:
                        failed_type = error.headers.get('Content-Type', '') if error.headers else ''
                        failed_usage = usage_from_payload(error.read(2 * 1024 * 1024), failed_type)
                    except Exception:
                        failed_usage = None
                    complete('failed', failed_usage, 'http_' + str(error.code))
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
                        try:
                            original = json.loads(raw)
                        except ValueError:
                            original = None
                        if isinstance(original, dict) and isinstance(original.get('usage'), dict):
                            seen_usage = original['usage']
                        converted = convert(original, request['model'], tool_types)
                        if converted['status'] != 'completed':
                            raise ValueError('Local Chat response is incomplete; no partial tool call was forwarded')
                        validated, content_type = encode(converted, request.get('stream', False))
                        if policy:
                            try:
                                policy.validate_response(validated, content_type)
                            except Exception:
                                complete('failed', seen_usage, 'protocol')
                                raise
                        complete('completed', seen_usage)
                    elif policy:
                        validated = response.read(policy.MAX_RESPONSE_BYTES + 1)
                        seen_usage = usage_from_payload(validated, content_type)
                        try:
                            policy.validate_response(validated, content_type)
                        except Exception:
                            complete('failed', seen_usage, 'protocol')
                            raise
                        complete('completed', seen_usage)
                    elif 'application/json' in content_type:
                        validated = response.read(16 * 1024 * 1024 + 1)
                        if len(validated) > 16 * 1024 * 1024:
                            raise ValueError('Model response exceeds size limit')
                        seen_usage = usage_from_payload(validated, content_type)
                        outcome = response_outcome(validated, content_type)
                    elif 'text/event-stream' not in content_type:
                        validated = response.read(16 * 1024 * 1024 + 1)
                        if len(validated) > 16 * 1024 * 1024:
                            raise ValueError('Model response exceeds size limit')
                        seen_usage = usage_from_payload(validated, content_type)
                        outcome = response_outcome(validated, content_type)
                    self.send_response(response.status)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if validated is not None:
                        self.wfile.write(validated); self.wfile.flush()
                        status, error_kind = _terminal('completed', outcome)
                        complete(status, seen_usage, error_kind)
                        return
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk); self.wfile.flush()
                        if len(tail) + len(chunk) > 2 * 1024 * 1024:
                            del tail[:len(tail) + len(chunk) - 2 * 1024 * 1024]
                        tail += chunk
                    seen_usage = usage_from_payload(bytes(tail), content_type)
                    outcome = response_outcome(bytes(tail), content_type)
                    status, error_kind = _terminal('completed', outcome)
                    complete(status, seen_usage, error_kind)
            except Exception as error:
                if seen_usage is None and tail:
                    seen_usage = usage_from_payload(bytes(tail), content_type)
                complete('failed', seen_usage, 'protocol' if seen_usage is not None else 'transport')
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

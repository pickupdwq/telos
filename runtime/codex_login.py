"""Executor provider backed by this Mac's own Codex ChatGPT login.

Only the bridge relay reads ``$CODEX_HOME/auth.json`` (default ``~/.codex``),
at request time. The executor sandbox receives the per-job relay token, never
the login. Telos never refreshes or writes the login: refresh tokens rotate on
use, so refreshing here could sign the user's own Codex out. When the access
token is missing or close to expiry, the provider is unavailable and profile
fallback (or the user reopening Codex) applies.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import time

from providers import ProviderError

PROVIDER = 'codex_login'
BASE_URL = 'https://chatgpt.com/backend-api/codex'
# Requests are rejected unless both are fixed this way (verified 2026-11-23).
FIXED_REQUEST = {'store': False, 'stream': True}
# A new task may run for hours; do not choose a token about to expire. A
# request in an existing job only needs a token that is still valid.
SELECT_MARGIN_SECONDS = 2 * 3600
REQUEST_MARGIN_SECONDS = 60
# After a usage-limit rejection without Retry-After, prefer the fallback for this long.
LIMIT_COOLDOWN_SECONDS = 1800
LIMIT_FILE = 'codex-login-limit.json'
# The login's model catalog, as Codex itself fetches it. Telos asks only when
# choosing a provider for a new task, and caches the answer briefly.
MODELS_URL = BASE_URL + '/models'
MODELS_CLIENT_VERSION = '0.171.0'
MODELS_FILE = 'codex-login-models.json'
MODELS_TTL_SECONDS = 600
MODELS_TIMEOUT = 15


def auth_path(home, environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get('CODEX_HOME') or Path(home) / '.codex') / 'auth.json'


def _expiry(token):
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        value = claims.get('exp')
        return float(value) if type(value) in (int, float) else None
    except (IndexError, ValueError, TypeError, AttributeError):
        return None


def login(home, environ=None, now=None, margin=REQUEST_MARGIN_SECONDS):
    """Return (access_token, account_id, expires_at) or raise ProviderError."""
    path = auth_path(home, environ)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ProviderError('No local Codex login; run codex login, or use the configured fallback') from None
    except (OSError, ValueError) as error:
        raise ProviderError('Local Codex login is unreadable: ' + type(error).__name__) from None
    tokens = data.get('tokens') if isinstance(data, dict) else None
    token = tokens.get('access_token') if isinstance(tokens, dict) else None
    if data.get('auth_mode') != 'chatgpt' or not isinstance(token, str) or not token.strip():
        raise ProviderError('Local Codex is not signed in with ChatGPT')
    expires = _expiry(token)
    now = time.time() if now is None else now
    if expires is None or expires - now < margin:
        raise ProviderError('Local Codex login has expired or expires soon; open Codex once so it refreshes its own login')
    account = tokens.get('account_id')
    return token.strip(), account if isinstance(account, str) and account else None, expires


def available(home, environ=None, now=None, state_dir=None, model=None, fetch=None):
    """Whether a new task should use this login rather than its fallback.

    With ``model``, the login's own model catalog must list it. An unknown
    catalog (network failure) counts as unavailable, so the task falls back.
    """
    now = time.time() if now is None else now
    try:
        token, account, _ = login(home, environ, now, margin=SELECT_MARGIN_SECONDS)
    except ProviderError:
        return False
    if limited_until(state_dir) > now:
        return False
    if model is None:
        return True
    models = catalog(token, account, state_dir, now=now, fetch=fetch)
    return models is not None and model in models


def catalog(token, account, state_dir=None, now=None, fetch=None):
    """Model slugs this login offers, or None when unknown."""
    now = time.time() if now is None else now
    identity = hashlib.sha256((account or token).encode()).hexdigest()[:16]
    path = Path(state_dir) / MODELS_FILE if state_dir is not None else None
    if path is not None:
        try:
            cached = json.loads(path.read_text())
            if (cached.get('identity') == identity and isinstance(cached.get('models'), list)
                    and 0 <= now - cached.get('at', 0) < MODELS_TTL_SECONDS):
                return set(cached['models'])
        except (OSError, ValueError, AttributeError):
            pass
    try:
        models = (fetch or fetch_models)(token, account)
    except Exception:  # noqa: BLE001 - any failure means "unknown", never a crash
        return None
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_name(path.name + '.tmp')
            tmp.write_text(json.dumps({'identity': identity, 'at': now, 'models': sorted(models)}))
            os.replace(tmp, path)
        except OSError:
            pass
    return set(models)


def fetch_models(token, account):
    import urllib.request
    headers = {'Authorization': 'Bearer ' + token}
    if account:
        headers['chatgpt-account-id'] = account
    request = urllib.request.Request(MODELS_URL + '?client_version=' + MODELS_CLIENT_VERSION, headers=headers)
    with urllib.request.urlopen(request, timeout=MODELS_TIMEOUT) as response:
        data = json.loads(response.read(4 << 20))
    items = data.get('models') if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError('Unexpected model catalog')
    return {str(m.get('slug') or m.get('id')) for m in items if isinstance(m, dict) and (m.get('slug') or m.get('id'))}


def limited_until(state_dir):
    if state_dir is None:
        return 0
    try:
        value = json.loads((Path(state_dir) / LIMIT_FILE).read_text()).get('until', 0)
    except (OSError, ValueError, AttributeError):
        return 0
    return float(value) if type(value) in (int, float) else 0


def mark_limited(state_dir, retry_after=None, now=None):
    """Record a usage-limit rejection so new tasks prefer the fallback for a while."""
    now = time.time() if now is None else now
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = LIMIT_COOLDOWN_SECONDS
    seconds = min(max(seconds, 60), 7 * 86400)
    path = Path(state_dir) / LIMIT_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({'until': now + seconds, 'recorded_at': now}))
    os.replace(tmp, path)


class Upstream:
    """Relay hook: fresh login headers per request, and usage-limit recording."""

    def __init__(self, home, state_dir, environ=None):
        self.home, self.state_dir, self.environ = home, state_dir, environ

    def __call__(self):
        return headers(self.home, self.environ)

    def limited(self, retry_after=None):
        try:
            mark_limited(self.state_dir, retry_after)
        except OSError:
            pass  # Never turns an upstream rejection into a different failure.


def headers(home, environ=None):
    """Upstream headers for one request; read fresh so Codex's own refreshes apply."""
    token, account, _ = login(home, environ)
    result = {'Authorization': 'Bearer ' + token}
    if account:
        result['chatgpt-account-id'] = account
    return result, token


def prepare(request):
    """Apply the backend's fixed request shape in place."""
    if not isinstance(request, dict):
        raise ValueError('Request must be a JSON object')
    request.update(FIXED_REQUEST)
    # Store is false, so earlier response IDs cannot be referenced; reasoning
    # must round-trip encrypted inside the CLI's own history instead.
    request.pop('previous_response_id', None)
    include = request.get('include')
    include = [x for x in include if isinstance(x, str)] if isinstance(include, list) else []
    if 'reasoning.encrypted_content' not in include:
        include.append('reasoning.encrypted_content')
    request['include'] = include
    return request

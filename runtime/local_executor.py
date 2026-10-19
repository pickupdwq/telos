"""Named loopback endpoints with one active selection and immutable task bindings."""
import contextlib
import copy
import fcntl
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from providers import ProviderError, EFFORTS


NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:-]{0,119}')


def endpoint(raw):
    if not isinstance(raw, dict) or set(raw) - {'base_url', 'model', 'wire_api', 'env', 'reasoning_effort'}:
        raise ProviderError('Local endpoint accepts base_url, model, wire_api, env and reasoning_effort only')
    value = copy.deepcopy(raw)
    address = value.get('base_url', '')
    try:
        url = urlsplit(address)
        port = url.port
        host = url.hostname
        local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        local, port = False, None
    if (not isinstance(address, str) or any(c.isspace() for c in address) or not local
            or url.scheme not in ('http', 'https') or not port or url.username or url.password
            or url.query or url.fragment or '%' in address or '\\' in address
            or any(p in ('.', '..') for p in url.path.split('/'))):
        raise ProviderError('Local endpoint needs http(s)://127.0.0.1:PORT/v1, localhost or [::1]; no URL credentials')
    # Pin localhost to a loopback literal; do not depend on DNS or system proxy settings.
    host = '127.0.0.1' if host == 'localhost' else host
    authority = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    value['base_url'] = urlunsplit((url.scheme, authority, url.path.rstrip('/'), '', ''))
    if not isinstance(value.get('model'), str) or not MODEL.fullmatch(value['model']):
        raise ProviderError('Local endpoint requires a concrete model ID')
    value.setdefault('wire_api', 'responses')
    if value['wire_api'] not in ('responses', 'chat'):
        raise ProviderError('Local protocol must be responses or chat')
    value.setdefault('env', None)
    if value['env'] is not None and (not isinstance(value['env'], str)
            or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,99}', value['env'])
            or value['env'] in {'PATH', 'HOME', 'CODEX_HOME', 'TMPDIR', 'LANG', 'LC_ALL',
                                'SSL_CERT_FILE', 'SSL_CERT_DIR', 'TELOS_RELAY_TOKEN', 'EXAM_LOOP_RELAY_TOKEN',
                                'ARK_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'CODEX_API_KEY'}):
        raise ProviderError('Use a dedicated local API-key environment variable, or omit env for no authentication')
    value.setdefault('reasoning_effort', 'none')
    if value['reasoning_effort'] not in EFFORTS:
        raise ProviderError('Invalid local reasoning_effort')
    return value


def configuration(cfg):
    raw = cfg.get('local_executor', {})
    if not isinstance(raw, dict) or set(raw) - {'active', 'endpoints'}:
        raise ProviderError('local_executor accepts one active name and an endpoints mapping')
    entries = raw.get('endpoints', {})
    if not isinstance(entries, dict) or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in entries):
        raise ProviderError('Local endpoints must have unique simple names')
    entries = {n: endpoint(v) for n, v in entries.items()}
    active = raw.get('active')
    if active is not None and (not isinstance(active, str) or active not in entries):
        raise ProviderError('Select exactly one configured local endpoint name, or null')
    return {'active': active, 'endpoints': entries}


def selected(cfg, required=True):
    config = configuration(cfg)
    name = config['active']
    if name is None:
        if required:
            raise ProviderError('No active local endpoint; configure one with telos local add and telos local select')
        return None
    return name, config['endpoints'][name]


def assert_active(b, binding):
    if binding.get('version') != 3 or binding.get('provider') != 'local':
        return
    name, spec = selected(b.config())
    if name != binding['local_endpoint'] or spec != binding['provider_spec']:
        raise ProviderError('This task is pinned to a different local endpoint/configuration. Select its original '
                            'endpoint before resuming, or create/reassign a task; no request was rerouted.')


@contextlib.contextmanager
def lease(b, binding):
    """Same-port jobs may run together; selection changes cannot overlap a local job."""
    b.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (b.STATE / 'local-executor.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        assert_active(b, binding)
        yield


def manage(b, action, name=None, spec=None, make_default=False):
    if action == 'list':
        return {**configuration(b.config()), 'default_executor_profile': b.config().get('default_executor_profile')}
    if action not in ('add', 'select') or not isinstance(name, str) or not NAME.fullmatch(name):
        raise ProviderError('Usage: telos local list | add NAME --url URL --model MODEL | select NAME [--default]')
    with b.project_manager().lock():
        with (b.STATE / 'local-executor.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProviderError('A local executor job is running; wait for it before changing local endpoints') from None
            # Also covers scheduled jobs whose process has not acquired its lease yet.
            for path in (b.STATE / 'runs').glob('*/state.json'):
                state = b.read_json(path)
                contract_path = path.with_name('contract.json')
                if state.get('phase') == 'running' and contract_path.exists():
                    binding = b.read_json(contract_path).get('executor', {})
                    if binding.get('provider') == 'local':
                        raise ProviderError('A local job is scheduled/running; wait or recover it before switching')
            cfg = b.read_json(b.CONFIG) if b.CONFIG.exists() else {}
            config = configuration(cfg)
            if action == 'add':
                if name in config['endpoints']:
                    raise ProviderError('Endpoint name already exists; use a new name to preserve frozen task configurations')
                config['endpoints'][name] = endpoint(spec)
            else:
                if name not in config['endpoints']:
                    raise ProviderError('Unknown local endpoint')
                config['active'] = name
            cfg['local_executor'] = config
            profiles = cfg.setdefault('profiles', {})
            if 'local' in profiles and profiles['local'] != {'provider': 'local'}:
                raise ProviderError('The local profile name is already in use; preserve it and resolve the conflict first')
            profiles['local'] = {'provider': 'local'}
            if make_default:
                if action != 'select':
                    raise ProviderError('--default applies to local select only')
                cfg['default_executor_profile'] = 'local'
            b.write_json(b.CONFIG, cfg)
            return {**config, 'default_executor_profile': cfg.get('default_executor_profile')}

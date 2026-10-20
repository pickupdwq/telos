"""Provider policy, immutable task bindings and host-bound credential resolution.

No model calls, session state or supervisor credentials belong in this module.
"""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from identity import credential_path


class ProviderError(Exception):
    pass


class ExecutionTimeout(ProviderError):
    """The local execution time limit fired, independently of account quota."""


ARK_MODELS = ('glm-5.3', 'glm-5.3-flash', 'deepseek-v4-1-flash-260910', 'deepseek-v4.1-flash')
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
PROVIDERS = {
    'ark': {'base_url': 'https://ark.cn-beijing.volces.com/api/coding/v3',
            'env': 'ARK_API_KEY', 'credential_provider': 'huoshancodingplan',
            'default_model': 'deepseek-v4-1-flash-260910', 'models': ARK_MODELS,
            'exam_wire_api': 'chat', 'billing': 'coding_plan'},
    'deepseek': {'base_url': 'https://api.deepseek.com', 'env': 'DEEPSEEK_API_KEY',
                 'credential_provider': 'deepseek', 'default_model': 'deepseek-flash',
                 'models': ('deepseek-flash', 'deepseek-v4-pro'),
                 'exam_wire_api': 'chat', 'billing': 'tokens'},
    'openai': {'base_url': 'https://api.openai.com/v1', 'env': 'OPENAI_API_KEY',
               'credential_provider': None, 'default_model': None, 'models': None,
               'exam_wire_api': 'responses', 'billing': 'tokens'},
    'bailian': {'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                'env': 'DASHSCOPE_API_KEY', 'credential_provider': None,
                'default_model': 'deepseek-v4.1-flash-1017',
                'models': ('deepseek-v4.1-flash-1017',),
                'exam_wire_api': 'responses', 'billing': 'tokens'},
}
LIMITS = {'http_timeout': (1, 3600, 180), 'job_timeout': (1, 86400, 600),
          'exam_max_tokens': (1, 131072, 8192), 'exam_format_attempts': (1, 3, 3)}
PROFILE_FIELDS = {'provider', 'model', 'reasoning_effort', 'credential_provider', *LIMITS}


def registry(cfg):
    """Extra providers are configured locally, never through a task's URL/key fields."""
    extra = cfg.get('providers', {})
    if not isinstance(extra, dict):
        raise ProviderError('providers must be a mapping')
    result = copy.deepcopy(PROVIDERS)
    for name, raw in extra.items():
        if name in (*PROVIDERS, 'local') or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', name):
            raise ProviderError('Custom providers need a distinct simple name')
        result[name] = validate_custom_spec(raw)
    from local_executor import selected
    local = selected(cfg, required=False)
    if local:
        name, spec = local
        result['local'] = {**spec, 'default_model': spec['model'], 'models': [spec['model']],
                           'credential_provider': None, 'local_endpoint': name}
    return result


def validate_custom_spec(raw):
    fields = {'base_url', 'env', 'default_model', 'models', 'credential_provider', 'wire_api'}
    if not isinstance(raw, dict) or set(raw) - fields:
        raise ProviderError('Custom provider accepts endpoint, credential references and Responses models only')
    spec = dict(raw)
    url = urlsplit(spec.get('base_url', ''))
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
            or spec['base_url'].endswith('/') or spec.get('wire_api', 'responses') != 'responses'):
        raise ProviderError('Custom executor requires a fixed HTTPS Responses API endpoint without URL credentials')
    if (not re.fullmatch(r'[A-Z][A-Z0-9_]{1,99}', spec.get('env', ''))
            or spec['env'] in ('PATH', 'HOME', 'CODEX_HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'TELOS_RELAY_TOKEN', 'EXAM_LOOP_RELAY_TOKEN')):
        raise ProviderError('Custom provider requires an API-key environment variable name')
    spec.setdefault('credential_provider', None)
    spec.setdefault('models', None)
    spec.setdefault('default_model', None)
    spec.setdefault('wire_api', 'responses')
    if spec['models'] is not None and (not isinstance(spec['models'], list) or not spec['models']
            or not all(isinstance(m, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,119}', m) for m in spec['models'])):
        raise ProviderError('Custom models must be concrete model IDs')
    return spec


def validate_backend(provider, model, specs=None):
    specs = PROVIDERS if specs is None else specs
    if not isinstance(provider, str) or provider not in specs:
        raise ProviderError('Provider must be configured locally')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,119}', model):
        raise ProviderError('Provide a concrete model ID, not a tool name')
    if model == 'codex':
        raise ProviderError('codex is a CLI, select an actual model ID')
    allowed = specs[provider]['models']
    if allowed is not None and model not in allowed:
        raise ProviderError(f'Supported {provider} models: ' + ', '.join(allowed))


def validate_binding(binding):
    if not isinstance(binding, dict) or binding.get('version') not in (1, 2, 3):
        raise ProviderError('Unsupported backend binding version')
    required = {'version', 'profile', 'provider', 'model', 'base_url', 'wire_api', 'exam_wire_api',
                'credential_ref', 'reasoning_effort', *LIMITS}
    if binding['version'] in (2, 3):
        required.add('provider_spec')
    if binding['version'] == 3:
        required.add('local_endpoint')
    if set(binding) != required:
        raise ProviderError('Invalid backend binding fields')
    if binding['version'] == 3:
        from local_executor import endpoint, NAME
        if (binding.get('provider') != 'local' or not isinstance(binding.get('local_endpoint'), str)
                or not NAME.fullmatch(binding['local_endpoint'])):
            raise ProviderError('Invalid local executor binding')
        raw = endpoint(binding['provider_spec'])
        if raw != binding['provider_spec'] or binding['model'] != raw['model']:
            raise ProviderError('Local binding must preserve its normalized endpoint and selected model')
        spec = {**raw, 'exam_wire_api': raw['wire_api']}
    elif binding['version'] == 2:
        if binding['provider'] in (*PROVIDERS, 'local') or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', binding['provider']):
            raise ProviderError('Invalid custom provider binding')
        spec = {**validate_custom_spec(binding['provider_spec']), 'exam_wire_api': 'responses'}
    else:
        spec = PROVIDERS.get(binding['provider'])
    if binding['version'] != 3:
        validate_backend(binding['provider'], binding['model'], {binding['provider']: spec} if spec else {})
    if (binding['base_url'] != spec['base_url'] or binding['wire_api'] != spec.get('wire_api', 'responses')
            or binding['exam_wire_api'] != spec['exam_wire_api']):
        raise ProviderError('Backend destination/protocol does not match its provider')
    if binding['reasoning_effort'] is not None and binding['reasoning_effort'] not in EFFORTS:
        raise ProviderError('Unsupported reasoning_effort')
    if binding['profile'] is not None and (not isinstance(binding['profile'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', binding['profile'])):
        raise ProviderError('Invalid profile name')
    ref = binding['credential_ref']
    if (not isinstance(ref, dict) or set(ref) != {'env', 'opencode_provider'} or ref['env'] != spec['env']
            or (binding['version'] == 3 and ref['opencode_provider'] is not None)
            or (ref['opencode_provider'] is not None and (not isinstance(ref['opencode_provider'], str)
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', ref['opencode_provider'])))):
        raise ProviderError('Invalid provider credential reference')
    for name, (low, high, _) in LIMITS.items():
        if type(binding[name]) is not int or not low <= binding[name] <= high:
            raise ProviderError(f'{name} must be between {low} and {high}')
    return copy.deepcopy(binding)


def resolve_binding(cfg, *, profile=None, provider=None, model=None, reasoning_effort=None,
                    legacy=False, exam=False):
    """Resolve once; subsequent jobs use the serialized binding, never profile defaults."""
    settings = {}
    if profile is not None:
        profiles = cfg.get('profiles', {})
        if not isinstance(profile, str) or not isinstance(profiles, dict) or profile not in profiles:
            raise ProviderError(f'Unknown backend profile: {profile}')
        settings = profiles[profile]
        if not isinstance(settings, dict) or set(settings) - PROFILE_FIELDS:
            raise ProviderError('Invalid profile fields; keys and arbitrary URLs do not belong in profiles')
        if provider is not None and provider != settings.get('provider'):
            raise ProviderError('Explicit provider conflicts with the selected profile')
    provider = provider or settings.get('provider') or 'ark'
    if provider == 'local':
        from local_executor import selected
        name, spec = selected(cfg)
        if model is not None and model != spec['model'] or settings.get('model', spec['model']) != spec['model']:
            raise ProviderError('Local tasks use the active endpoint model; select a configured endpoint to change it')
        if settings.get('credential_provider') is not None:
            raise ProviderError('Local tasks use their own endpoint credentials')
        effort = reasoning_effort if reasoning_effort is not None else settings.get('reasoning_effort', spec['reasoning_effort'])
        if effort not in EFFORTS:
            raise ProviderError('Unsupported local reasoning_effort')
        binding = {'version': 3, 'profile': profile, 'provider': 'local', 'model': spec['model'],
                   'local_endpoint': name, 'provider_spec': spec, 'base_url': spec['base_url'],
                   'wire_api': spec['wire_api'], 'exam_wire_api': spec['wire_api'],
                   'credential_ref': {'env': spec['env'], 'opencode_provider': None}, 'reasoning_effort': effort}
        binding.update({k: settings.get(k, cfg.get(k, limits[2])) for k, limits in LIMITS.items()})
        return validate_binding(binding)
    specs = registry(cfg)
    if not isinstance(provider, str) or provider not in specs:
        raise ProviderError('Unknown provider')
    spec = specs[provider]
    model = model or settings.get('model') or (cfg.get('model') if provider == 'ark' else spec['default_model'])
    validate_backend(provider, model, specs)
    effort = (None if exam else 'medium') if legacy else (
        reasoning_effort if reasoning_effort is not None else settings.get('reasoning_effort', 'max'))
    if not legacy and effort not in EFFORTS:
        raise ProviderError('Unsupported reasoning_effort')
    credential_provider = settings.get('credential_provider',
        cfg.get('credential_provider', spec['credential_provider']) if provider == 'ark' else spec['credential_provider'])
    binding = {'version': 1, 'profile': profile, 'provider': provider, 'model': model,
               'base_url': spec['base_url'], 'wire_api': 'responses', 'exam_wire_api': spec.get('exam_wire_api', 'responses'),
               'credential_ref': {'env': spec['env'], 'opencode_provider': credential_provider},
               'reasoning_effort': effort}
    if provider not in PROVIDERS:
        binding.update(version=2, provider_spec=spec)
    binding.update({k: settings.get(k, cfg.get(k, limits[2])) for k, limits in LIMITS.items()})
    return validate_binding(binding)


def resolve_pair(cfg, *, executor_profile=None, exam_profile=None, provider=None, model=None,
                 exam_provider=None, exam_model=None, reasoning_effort=None, exam_reasoning_effort=None):
    if executor_profile is None and provider is None:
        executor_profile = cfg.get('default_executor_profile')
    executor = resolve_binding(cfg, profile=executor_profile, provider=provider, model=model,
                               reasoning_effort=reasoning_effort)
    if exam_profile is None and exam_provider is None and exam_model is None:
        exam_profile = cfg.get('default_exam_profile', 'inherit_executor')
    if exam_profile == 'inherit_executor' or (exam_profile is None and exam_provider is None):
        examiner = copy.deepcopy(executor)
        if exam_provider is not None and exam_provider != examiner['provider']:
            raise ProviderError('exam_provider conflicts with inherit_executor')
        if exam_model is not None:
            examiner['model'] = exam_model
        if exam_reasoning_effort is not None:
            examiner['reasoning_effort'] = exam_reasoning_effort
        examiner = validate_binding(examiner)
    else:
        examiner = resolve_binding(cfg, profile=exam_profile, provider=exam_provider, model=exam_model,
                                   reasoning_effort=exam_reasoning_effort)
    return {'executor': executor, 'exam': examiner}


def contract_bindings(contract, cfg):
    if contract.get('workflow_version') == 3:
        executor = validate_binding(contract['executor'])
        # Internal compatibility view for old scheduling/timeout code. No second
        # actor or independently resolved binding exists in a v3 contract.
        return {'executor': executor, 'exam': copy.deepcopy(executor)}
    if 'backends' in contract:
        pair = contract['backends']
        if not isinstance(pair, dict) or set(pair) != {'executor', 'exam'}:
            raise ProviderError('Invalid task backend bindings')
        return {k: validate_binding(v) for k, v in pair.items()}
    # Historical contracts are immutable. Do not resolve them through new defaults.
    provider = contract.get('provider', 'ark')
    model = contract['model']
    return {'executor': resolve_binding(cfg, provider=provider, model=model, legacy=True),
            'exam': resolve_binding(cfg, provider=contract.get('exam_provider', provider),
                                    model=contract.get('exam_model', model), legacy=True, exam=True)}


def public_binding(binding):
    return {**{k: binding[k] for k in ('profile', 'provider', 'model', 'base_url', 'reasoning_effort')},
            **({'local_endpoint': binding['local_endpoint'], 'wire_api': binding['wire_api']} if binding['version'] == 3 else {}),
            'billing': PROVIDERS.get(binding['provider'], {}).get('billing', 'provider_defined')}


def resolve_executor(cfg, *, executor_profile=None, provider=None, model=None, reasoning_effort=None):
    if executor_profile is None and provider is None:
        executor_profile = cfg.get('default_executor_profile')
    return resolve_binding(cfg, profile=executor_profile, provider=provider, model=model,
                           reasoning_effort=reasoning_effort)


def public_contract(contract, cfg):
    bindings = contract_bindings(contract, cfg)
    if contract.get('workflow_version') == 3:
        return {'workflow': 'supervisor_executor_closed_book', 'executor': public_binding(bindings['executor'])}
    return {'workflow': 'legacy_independent_exam',
            'backends': {k: public_binding(v) for k, v in bindings.items()}}


def credential(binding, home, environ=None):
    """Each fallback must be explicitly bound to the selected provider's exact host/path."""
    binding = validate_binding(binding)
    environ = os.environ if environ is None else environ
    env_name = binding['credential_ref']['env']
    if binding['version'] == 3 and env_name is None:
        return '', 'local endpoint without authentication'
    key = environ.get(env_name, '').strip()
    if key:
        return key, env_name + ' environment'
    path = credential_path(home)
    if path.exists():
        storage_key = 'local:' + binding['local_endpoint'] if binding['version'] == 3 else binding['provider']
        entry = json.loads(path.read_text()).get(storage_key, {})
        if entry.get('base_url', '').rstrip('/') == binding['base_url']:
            key = entry.get('api_key')
            if isinstance(key, str) and key.strip():
                return key.strip(), 'telos local credential store'
    provider = binding['credential_ref']['opencode_provider']
    path = Path(home) / '.config/opencode/opencode.json'
    if provider and path.exists():
        options = json.loads(path.read_text()).get('provider', {}).get(provider, {}).get('options', {})
        if options.get('baseURL', '').rstrip('/') == binding['base_url']:
            key = options.get('apiKey')
            if isinstance(key, str) and key.startswith('{env:') and key.endswith('}'):
                key = environ.get(key[5:-1], '')
            if isinstance(key, str) and key.strip():
                return key.strip(), f'OpenCode provider {provider} (read at runtime)'
            # OpenCode /connect stores API keys separately. Require the matching
            # configured endpoint above before trusting an auth-store provider ID.
            auth_path = Path(home) / '.local/share/opencode/auth.json'
            if auth_path.exists():
                entry = json.loads(auth_path.read_text()).get(provider, {})
                key = entry.get('key')
                if entry.get('type') == 'api' and isinstance(key, str) and key.strip():
                    return key.strip(), f'OpenCode auth store provider {provider} (host verified)'
    raise ProviderError(f'No matching {binding["provider"]} credential. Run telos auth {binding["provider"]} '
                        f'locally or set {env_name} in the MCP server environment; never put keys in tool arguments.')

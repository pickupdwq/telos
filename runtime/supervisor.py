"""Identity policy for the existing GPT supervisor, never a second model client."""
from providers import ProviderError


def policy(cfg):
    value = cfg.get('supervisor')
    if value is None:  # Compatibility for pre-policy installations and manual CLI use.
        return None
    if (not isinstance(value, dict) or set(value) != {'provider', 'models'}
            or value['provider'] != 'openai' or not isinstance(value['models'], list)
            or not value['models'] or not all(isinstance(m, str) and m.startswith('gpt-') for m in value['models'])):
        raise ProviderError('Supervisor policy requires OpenAI and an explicit GPT flagship model allowlist')
    return value


def verify_thread(thread, cfg):
    selected = policy(cfg)
    if selected is not None and (thread.get('modelProvider') != selected['provider']
                                or thread.get('model') not in selected['models']):
        raise ProviderError('The original supervisor must use a configured GPT flagship model; executor profiles cannot replace it')
    return {k: thread.get(k) for k in ('model', 'modelProvider')}


def verify_owner(b, owner):
    if not owner or policy(b.config()) is None:
        return
    with b.owner_rpc(owner) as rpc:
        params = {'threadId': owner['thread_id']}
        if owner.get('transport') != 'desktop_ipc':
            params['includeTurns'] = False
        thread = rpc.call('thread/read', params)['thread']
    if thread.get('id') != owner['thread_id']:
        raise ProviderError('Supervisor thread identity changed')
    verify_thread(thread, b.config())

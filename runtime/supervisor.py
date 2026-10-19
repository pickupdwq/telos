"""Identity policy for the existing GPT supervisor, never a second model client."""
import re

from providers import ProviderError


def policy(cfg):
    value = cfg.get('supervisor')
    if value is None:  # Compatibility for pre-policy installations and manual CLI use.
        return None
    if (not isinstance(value, dict) or not {'provider', 'models'} <= set(value)
            or set(value) - {'provider', 'models', 'provider_aliases'}
            or value['provider'] != 'openai' or not isinstance(value['models'], list)
            or not value['models'] or not all(isinstance(m, str) and m.startswith('gpt-') for m in value['models'])):
        raise ProviderError('Supervisor policy requires OpenAI and an explicit GPT flagship model allowlist')
    aliases = value.get('provider_aliases', [])
    if (not isinstance(aliases, list)
            or not all(isinstance(alias, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', alias)
                       and alias != value['provider'] for alias in aliases)
            or len(set(aliases)) != len(aliases)):
        raise ProviderError('Supervisor provider_aliases must be distinct, explicitly trusted local OpenAI provider IDs')
    return value


def verify_thread(thread, cfg):
    selected = policy(cfg)
    if selected is not None:
        providers = {selected['provider'], *selected.get('provider_aliases', [])}
        if thread.get('modelProvider') not in providers or thread.get('model') not in selected['models']:
            raise ProviderError('The original supervisor must use a configured GPT flagship model and an explicitly trusted '
                                f'OpenAI provider ID; received provider={thread.get("modelProvider")!r}, '
                                f'model={thread.get("model")!r}; executor profiles cannot replace it')
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

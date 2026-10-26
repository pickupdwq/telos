"""Supervisor-selected, per-job reasoning within an immutable capability policy."""
import copy
from providers import EFFORTS, ProviderError


# Exact provider/model/protocol keys, never a model-name substring match. Other
# endpoints require an operator declaration; a CLI enum is not API capability.
CAPABILITIES = {
    'bailian:deepseek-v4.1-flash-1017:responses': {
        'supported_efforts': ['low', 'high', 'max'],
        'aliases': {'minimal': 'low', 'medium': 'high', 'xhigh': 'high'},
        'source': 'https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses'},
}


def capability_key(binding):
    return ':'.join(binding[k] for k in ('provider', 'model', 'wire_api'))


def capability(raw):
    if not isinstance(raw, dict) or set(raw) != {'supported_efforts', 'aliases', 'source'}:
        raise ProviderError('Reasoning capability requires supported_efforts, aliases and source')
    levels, aliases = raw['supported_efforts'], raw['aliases']
    if (not isinstance(levels, list) or not levels
            or any(not isinstance(x, str) or x not in EFFORTS for x in levels)
            or len(levels) != len(set(levels))):
        raise ProviderError('Invalid supported reasoning efforts')
    if (not isinstance(aliases, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                           or k not in EFFORTS or k in levels or v not in levels
                                           for k, v in aliases.items())):
        raise ProviderError('Reasoning aliases must map directly to supported efforts')
    if not isinstance(raw['source'], str) or not raw['source'].strip() or len(raw['source']) > 1000:
        raise ProviderError('Reasoning capability requires a bounded documentation/verification source')
    return copy.deepcopy(raw)


def canonical(value, spec):
    if not isinstance(value, str):
        raise ProviderError('Reasoning effort must be a supported string')
    result = spec['aliases'].get(value, value)
    if result not in spec['supported_efforts']:
        raise ProviderError('Unsupported reasoning effort for this model: ' + value)
    return result


def freeze(binding, cfg, requested=None):
    enabled = cfg.get('dynamic_reasoning_enabled', False)
    if type(enabled) is not bool:
        raise ProviderError('dynamic_reasoning_enabled must be boolean')
    if requested is None and not enabled:
        return None
    if requested is not None and (not isinstance(requested, dict) or set(requested) != {'allowed_efforts'}):
        raise ProviderError('reasoning_policy accepts allowed_efforts only')
    configured = cfg.get('reasoning_capabilities', {})
    if not isinstance(configured, dict):
        raise ProviderError('reasoning_capabilities must be a mapping')
    key = capability_key(binding)
    raw = configured.get(key, CAPABILITIES.get(key))
    if raw is None:
        if requested is not None:
            raise ProviderError('No verified reasoning capability for ' + key + '; configure it locally first')
        return None
    spec = capability(raw)
    values = spec['supported_efforts'] if requested is None else requested['allowed_efforts']
    if not isinstance(values, list) or not values or len(values) > len(EFFORTS):
        raise ProviderError('allowed_efforts must be a nonempty list of supported levels')
    allowed = list(dict.fromkeys(canonical(value, spec) for value in values))
    initial = canonical(binding['reasoning_effort'], spec)
    if initial not in allowed:
        raise ProviderError('Initial reasoning effort must be in the allowed range')
    return {'version': 1, 'capability_key': key, **spec, 'allowed_efforts': allowed,
            'initial_effort': initial, 'mode': 'supervisor'}


def validate(policy, binding):
    if (not isinstance(policy, dict) or set(policy) != {'version', 'capability_key', 'supported_efforts',
            'aliases', 'source', 'allowed_efforts', 'initial_effort', 'mode'}
            or type(policy['version']) is not int or policy['version'] != 1 or policy['mode'] != 'supervisor'
            or policy['capability_key'] != capability_key(binding)):
        raise ProviderError('Invalid frozen reasoning policy')
    spec = capability({k: policy[k] for k in ('supported_efforts', 'aliases', 'source')})
    allowed = policy['allowed_efforts']
    if (not isinstance(allowed, list) or not allowed
            or any(not isinstance(x, str) or x not in spec['supported_efforts'] for x in allowed)
            or len(allowed) != len(set(allowed))
            or policy['initial_effort'] != canonical(binding['reasoning_effort'], spec)
            or policy['initial_effort'] not in allowed):
        raise ProviderError('Invalid frozen reasoning range')
    return copy.deepcopy(policy)


def arguments(message, effort=None, reason=None):
    # Preserve historical action hashes when optional selection is absent.
    result = {'message': message}
    if effort is not None or reason is not None:
        result.update(reasoning_effort=effort, reasoning_reason=reason)
    return result


def select(contract, state, effort=None, reason=None):
    policy = contract.get('reasoning_policy')
    if policy is None:
        if effort is not None or reason is not None:
            raise ProviderError('This task has fixed reasoning; create a new task with a reasoning policy')
        return None
    policy = validate(policy, contract['executor'])
    prior = state.get('reasoning_decision')
    if prior is not None:
        validate_decision(prior, policy)
    previous = prior['effective_effort'] if prior else policy['initial_effort']
    if previous not in policy['allowed_efforts']:
        raise ProviderError('Previous reasoning decision is outside the frozen policy')
    if effort is None and reason is None:
        return {'requested_effort': previous, 'effective_effort': previous, 'previous_effort': previous,
                'reason': 'Retain the prior selection' if state.get('reasoning_decision') else 'Initial task selection',
                'source': 'inherit' if state.get('reasoning_decision') else 'initial'}
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ProviderError('Selecting a reasoning effort requires a nonempty reasoning_reason (at most 1000 characters)')
    effective = canonical(effort, policy)
    if effective not in policy['allowed_efforts']:
        raise ProviderError('Reasoning effort is outside this task\'s frozen allowed range')
    return {'requested_effort': effort, 'effective_effort': effective, 'previous_effort': previous,
            'reason': reason.strip(), 'source': 'supervisor'}


def validate_decision(decision, policy):
    if (not isinstance(decision, dict) or decision.get('effective_effort') not in policy['allowed_efforts']
            or canonical(decision.get('requested_effort'), policy) != decision['effective_effort']
            or decision.get('previous_effort') not in policy['allowed_efforts']
            or decision.get('source') not in ('initial', 'inherit', 'supervisor')
            or not isinstance(decision.get('reason'), str) or not decision['reason'].strip()
            or len(decision['reason']) > 1000):
        raise ProviderError('Invalid persisted reasoning decision')
    return decision

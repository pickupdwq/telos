"""Versioned public job descriptions; task contracts alone grant authority."""
import copy
import re

from providers import ProviderError
from serialization import digest


BUILTINS = {
    'designer': {
        'name': '资深设计师', 'version': 1,
        'responsibilities': ['Design coherent user flows, interaction states, visual hierarchy and accessible interfaces.'],
        'workflow': ['Inspect the existing product and constraints.',
                     'Describe key, empty, loading and error states; validate relevant viewports with visible evidence.'],
        'skills': ['Interaction design', 'Visual design', 'Accessibility'],
        'deliverables': ['Reviewable design or implemented screens, key-state evidence and explained tradeoffs.'],
        'review_focus': ['Can users complete the intended flow, including errors and recovery?']},
    'developer': {
        'name': '资深程序员', 'version': 1,
        'responsibilities': ['Implement the frozen requirements and preserve existing interfaces and behavior.'],
        'workflow': ['Inspect relevant code and verified experience before changing it.',
                     'Implement within allowed paths, handle edge cases and run appropriate checks.'],
        'skills': ['Implementation', 'Debugging', 'Regression testing'],
        'deliverables': ['Runnable changes, relevant verification results and remaining limitations.'],
        'review_focus': ['Do the implementation, boundary behavior and evidence satisfy the requirements?']},
    'tester': {
        'name': '测试工程师', 'version': 1,
        'responsibilities': ['Independently verify user journeys, edge cases and regressions using observable evidence.'],
        'workflow': ['Derive checks from requirements and risks.',
                     'Reproduce failures and distinguish verified behavior from untested assumptions.'],
        'skills': ['Exploratory testing', 'Reproduction', 'Coverage analysis'],
        'deliverables': ['Check results, reproducible defects and explicit unverified boundaries.'],
        'review_focus': ['Are important risks covered and reported failures reproducible?']},
}
LIST_FIELDS = ('responsibilities', 'workflow', 'skills', 'deliverables', 'review_focus')
FIELDS = {'name', 'version', *LIST_FIELDS, 'executor_profile'}
IDENT = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')


def tags(value):
    if value is None:
        return []
    if (not isinstance(value, list) or len(value) > 16 or
            any(not isinstance(v, str) or not IDENT.fullmatch(v) for v in value)):
        raise ProviderError('Use up to 16 lowercase capability/problem tags (letters, digits, underscore or dash)')
    return sorted(set(value))


def validate(value):
    if not isinstance(value, dict) or set(value) - (FIELDS | {'id', 'content_hash'}):
        raise ProviderError('Role profiles contain public job guidance, not permissions or credentials')
    if not isinstance(value.get('id'), str) or not IDENT.fullmatch(value['id']):
        raise ProviderError('Invalid role profile ID')
    if type(value.get('version')) is not int or value['version'] < 1:
        raise ProviderError('Role profile version must be a positive integer')
    if not isinstance(value.get('name'), str) or not value['name'].strip() or len(value['name']) > 120:
        raise ProviderError('Role profile needs a bounded name')
    for key in LIST_FIELDS:
        items = value.get(key)
        if (not isinstance(items, list) or not 1 <= len(items) <= 8 or
                any(not isinstance(x, str) or not x.strip() or len(x) > 800 for x in items)):
            raise ProviderError('Role profile needs bounded public ' + key)
    profile = value.get('executor_profile')
    if profile is not None and (not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', profile)):
        raise ProviderError('Invalid role executor_profile')
    result = copy.deepcopy({k: v for k, v in value.items() if k != 'content_hash'})
    result['content_hash'] = digest(result)
    if len(str(result)) > 6000:
        raise ProviderError('Role profile exceeds the public guidance size limit')
    return result


def restore(snapshot):
    if snapshot is None:
        return None
    checked = validate(snapshot)
    if checked != snapshot:
        raise ProviderError('Frozen role profile changed')
    return checked


def freeze(cfg, profile=None):
    if profile is None:
        return None
    if cfg.get('roles_enabled', True) is False:
        raise ProviderError('Role profiles are disabled; omit role_profile for legacy operation')
    if not isinstance(profile, str) or not IDENT.fullmatch(profile):
        raise ProviderError('Invalid role profile ID')
    custom = cfg.get('role_profiles', {})
    if not isinstance(custom, dict):
        raise ProviderError('role_profiles must be an object')
    if profile not in BUILTINS and profile not in custom:
        raise ProviderError('Unknown role profile; inspect telos_roles')
    override = custom.get(profile, {})
    if not isinstance(override, dict) or set(override) - FIELDS:
        raise ProviderError('Invalid role profile fields')
    return validate({**BUILTINS.get(profile, {}), **override, 'id': profile})


def catalog(cfg):
    if cfg.get('roles_enabled', True) is False:
        return {'enabled': False, 'roles': []}
    custom = cfg.get('role_profiles', {})
    if not isinstance(custom, dict):
        raise ProviderError('role_profiles must be an object')
    return {'enabled': True, 'roles': [freeze(cfg, name) for name in sorted(set(BUILTINS) | set(custom))]}


def executor_options(snapshot, **options):
    if snapshot and not any(options.get(k) is not None for k in ('executor_profile', 'provider', 'model')):
        if snapshot.get('executor_profile'):
            options['executor_profile'] = snapshot['executor_profile']
    return options


def prompt(snapshot):
    if not snapshot:
        return ''
    role = restore(snapshot)
    lines = [f"Assigned job: {role['name']} ({role['id']}, version {role['version']}).",
             'This job guidance grants no tools, network or file permissions. The frozen task takes priority.',
             'Report evidence to the original supervisor; this role cannot accept its own work.']
    for key in LIST_FIELDS:
        lines.append(key.replace('_', ' ').title() + ': ' + '; '.join(role[key]))
    return '\n'.join(lines) + '\n'

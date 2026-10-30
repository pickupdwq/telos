"""Explicitly reviewed general lessons, separate from unchanged repository records."""
import re
import time

import roles
from supervisor import verify_owner


TEXT_FIELDS = ('problem', 'cause', 'resolution', 'applicability', 'limitations')
LESSON_FIELDS = {*TEXT_FIELDS, 'required_tags', 'roles'}


def validate_lesson(value):
    if not isinstance(value, dict) or set(value) != LESSON_FIELDS:
        raise roles.ProviderError('A shared lesson requires problem, cause, resolution, applicability, limitations, required_tags and roles')
    for key in TEXT_FIELDS:
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 800:
            raise roles.ProviderError('Provide bounded public lesson text; use unknown for an unconfirmed cause')
    tags = roles.tags(value['required_tags'])
    if not tags:
        raise roles.ProviderError('Shared lessons need explicit applicability tags')
    return {**{k: value[k].strip() for k in TEXT_FIELDS}, 'required_tags': tags, 'roles': roles.tags(value['roles'])}


class SharedExperience:
    def __init__(self, memory):
        self.memory = memory
        self.b = memory.b

    def enabled(self):
        return getattr(self.b, 'config', lambda: {})().get('shared_experience_enabled', True) is not False

    def source(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{64}', ident):
            return None
        entry = self.memory.load_record(self.b.STATE / 'experience' / (ident + '.json'))
        if (entry is None or entry['id'] != ident or not self.memory.valid(entry) or
                entry.get('disabled') or not entry['reusable'] or entry.get('outcome') != 'accepted' or
                not self.memory.source_valid(entry)):
            return None
        return entry

    def load(self, path):
        try:
            entry = self.b.read_json(path)
            if (not isinstance(entry, dict) or entry.get('scope') != 'shared' or
                    type(entry.get('version')) is not int or entry['version'] != 1 or type(entry.get('disabled')) is not bool or
                    not self.memory.valid(entry) or not isinstance(entry.get('sources'), dict) or
                    not entry['sources'] or validate_lesson(entry.get('lesson')) != entry['lesson']):
                return None
            ident = self.b.digest({'scope': 'shared', 'version': 1, 'lesson': entry['lesson']})
            if entry.get('id') != ident or path.stem != ident:
                return None
            for source_id, receipt in entry['sources'].items():
                if (not re.fullmatch(r'[a-f0-9]{64}', source_id) or not isinstance(receipt, dict) or
                        any(not isinstance(receipt.get(k), str) for k in ('source_commit', 'source_packet_id', 'rationale'))):
                    return None
            return entry
        except (OSError, ValueError, TypeError, KeyError, self.b.GateError):
            return None

    def publish(self, experience_id, lesson, rationale, caller=None):
        if not self.enabled():
            raise self.b.GateError('Shared experience is disabled')
        lesson = validate_lesson(lesson)
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
            raise self.b.GateError('Explain why this reviewed lesson generalizes beyond its source project')
        # Same coordinator -> experience -> source observation order as task creation.
        # No source session lock is held while source_valid calls status().
        with self.b.project_manager().lock(), self.memory.locked() as folder:
            source = self.source(experience_id)
            if source is None:
                raise self.b.GateError('Publish only enabled, accepted and project-verified experience')
            state, contract = self.b.load_session(self.b.run_dir(source['session_id']))
            owner = state.get('owner')
            if owner and owner['thread_id'] != caller:
                raise self.b.GateError('Only the original supervisor can publish this experience', code='owner_mismatch')
            verify_owner(self.b, owner)
            contracts = self.memory.contracts(state, contract)
            public = self.b.encoded(lesson) + '\n' + rationale
            if not self.memory.safe_public(public, contracts):
                raise self.b.GateError('Shared lessons must exclude private acceptance material and credentials')
            # Generalization must be a summary, not an automatic export of paths.
            for location in (source['repo'], state.get('worktree', '')):
                if location and location in public:
                    raise self.b.GateError('Remove source-project paths from the general lesson')
            ident = self.b.digest({'scope': 'shared', 'version': 1, 'lesson': lesson})
            path = folder / 'shared' / (ident + '.json')
            existing = self.load(path) if path.exists() else None
            if path.exists() and existing is None:
                raise self.b.GateError('Existing shared experience is damaged; preserve and inspect it')
            entry = existing or {'id': ident, 'scope': 'shared', 'version': 1, 'lesson': lesson,
                                 'created_at': time.time(), 'disabled': False, 'sources': {}}
            duplicate = experience_id in entry['sources']
            if not duplicate:
                entry['sources'][experience_id] = {
                    'source_commit': source['source_commit'], 'source_packet_id': source['source_packet_id'],
                    'reviewer_thread_id': caller, 'reviewed_at': time.time(), 'rationale': rationale.strip()}
                entry['content_hash'] = self.b.digest({k: v for k, v in entry.items() if k != 'content_hash'})
                self.b.write_json(path, entry)
            return {'experience_id': ident, 'scope': 'shared', 'version': 1, 'duplicate': duplicate,
                    'disabled': entry['disabled'], 'source_count': len(entry['sources'])}

    def lookup(self, tags=None, role_profile=None, limit=3):
        tags = roles.tags(tags)
        if not tags or limit < 1 or not self.enabled():
            return []
        with self.memory.locked() as folder:
            candidates = []
            for path in (folder / 'shared').glob('*.json'):
                entry = self.load(path)
                if entry and not entry['disabled'] and set(entry['lesson']['required_tags']) <= set(tags):
                    candidates.append(entry)
            candidates.sort(key=lambda e: (role_profile in e['lesson']['roles'],
                                           len(e['lesson']['required_tags']), e['id']), reverse=True)
            result = []
            for entry in candidates:
                supported = 0
                for ident, receipt in entry['sources'].items():
                    source = self.source(ident)
                    if source and all(source[k] == receipt[k] for k in ('source_commit', 'source_packet_id')):
                        supported += 1
                if not supported:
                    continue
                # Cross-project callers receive ONLY the reviewed generalization.
                # Source paths, titles, sessions and private receipts remain local.
                result.append({'id': entry['id'], 'scope': 'shared', 'version': 1,
                               'lesson': entry['lesson'], 'source_count': supported})
                if len(result) >= limit:
                    break
            return result

    def forget(self, ident, caller=None):
        with self.memory.locked() as folder:
            path = folder / 'shared' / (ident + '.json')
            entry = self.load(path)
            if entry is None:
                raise self.b.GateError('Shared experience not found or damaged')
            # Same explicit local-user disable semantics as repository experience.
            entry['disabled'] = True
            entry['content_hash'] = self.b.digest({k: v for k, v in entry.items() if k != 'content_hash'})
            self.b.write_json(path, entry)
            return {'experience_id': ident, 'disabled': True, 'scope': 'shared'}

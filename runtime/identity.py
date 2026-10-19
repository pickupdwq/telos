"""TELOS identity and compatibility with existing Exam Loop installations."""
import os
from pathlib import Path

NAME = 'telos'
TITLE = 'TELOS'
VERSION = '3.3.0'

TOOL_PREFIXES = (
    ('exam_experience_', 'telos_experience_'),
    ('exam_project_', 'telos_project_'),
    ('exam_cache_', 'telos_cache_'),
    ('exam_loop_', 'telos_'),
)


def canonical_tool_name(name):
    for legacy, current in TOOL_PREFIXES:
        if name.startswith(legacy):
            return current + name[len(legacy):]
    return name


def legacy_callback_text(message):
    """Recognize already-delivered messages without rewriting historical events."""
    for legacy, current in TOOL_PREFIXES:
        message = message.replace(current, legacy)
    return message.replace('[TELOS_EVENT ', '[EXAM_LOOP_EVENT ').replace(
        '[TELOS_REMINDER ', '[EXAM_LOOP_REMINDER ')


def data_path(home, relative):
    """Prefer TELOS; reuse existing storage if the rename has not been installed."""
    home = Path(home)
    current = home / relative.replace('{name}', NAME)
    legacy = home / relative.replace('{name}', 'exam_loop')
    return current if current.exists() or not legacy.exists() else legacy


def runtime_path(home, kind, environ=None):
    environ = os.environ if environ is None else environ
    relative = {'STATE': '.local/state/{name}', 'CACHE': '.cache/{name}'}[kind]
    value = environ.get(f'TELOS_{kind}_DIR') or environ.get(f'EXAM_LOOP_{kind}_DIR')
    return Path(value or data_path(home, relative)).resolve()


def credential_path(home):
    # Choose the directory, including when a new auth file is being created.
    return data_path(home, '.config/{name}') / 'credentials.json'

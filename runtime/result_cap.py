"""Byte-bounded MCP results for the original supervisor conversation.

Every result is read by the one supervisor thread that all tasks share, and Codex
cuts long tool output in the middle (about 10k tokens), so identifiers could vanish
unnoticed. Oversized results are saved complete under STATE, which executors cannot
read, and the supervisor receives the same structure with the largest parts
removed, every identity/action field kept, and a list of the removed paths.
Executor channels (for example the audit probe service) are unaffected.
"""
import copy
import hashlib
import os
from pathlib import Path
import re
import secrets
import time

from serialization import encoded

DEFAULT_LIMIT = 32768
MINIMUM_LIMIT = 4096
# Headroom for the overflow notice itself.
NOTICE_RESERVE = 3072
MIN_CUT = 256
MAX_LISTED = 12
DIRECTORY = 'mcp-results'
RETAIN_SECONDS = 7 * 86400
RETAIN_FILES = 200
# Values under these keys are never removed, nor is any container holding one.
# Values shorter than MIN_CUT bytes are never removed either.
KEEP = frozenset((
    'id', 'task_id', 'session_id', 'project_id', 'meeting_id', 'event_id', 'job_id', 'request_id',
    'packet_id', 'contract_hash', 'phase', 'status', 'accepted', 'proceed', 'next_action', 'detail',
    'worktree', 'commit', 'base_commit', 'submitted_commit', 'accepted_commit', 'pins', 'blockers',
    'error', 'round', 'score', 'checks_passed', 'duplicate', 'screenshots', 'screenshot_paths',
    'identity', 'questions', 'answers', 'instruction', 'acceptance', 'final_review',
    'return_health', 'health', 'continuation'))
# Records in these lists/maps (and in any container with more than LONG_LIST
# entries) are history, not identity: an `id` or `status` inside them does not stop
# the whole container from being removed.
HISTORY = frozenset(('request_intervals', 'intervals', 'milestones', 'in_progress', 'requests',
                     'responses', 'attempts', 'history', 'reasoning_history', 'observed', 'plans',
                     'console', 'blocked_requests', 'steps', 'messages', 'rounds',
                     'auto_continuations', 'auto_check_history', 'actions', 'recent'))
LONG_LIST = 100


def limit_from(value):
    """0 disables the cap; invalid values use the default."""
    if type(value) is not int or value < 0:
        return DEFAULT_LIMIT
    return value and max(value, MINIMUM_LIMIT)


def _measure(node, path, protected, out):
    """Exact encoded byte size; records removable nodes in the same pass."""
    history = bool(path) and (path[-1] in HISTORY or len(node) > LONG_LIST) if isinstance(node, (dict, list)) else False
    if isinstance(node, dict):
        size, keep = 2 + max(0, len(node) - 1), protected
        for key, child in node.items():
            child_size, child_keep = _measure(child, path + (key,), protected or key in KEEP, out)
            size += len(encoded(str(key)).encode()) + 1 + child_size
            keep = keep or (child_keep and not history)
    elif isinstance(node, list):
        size, keep = 2 + max(0, len(node) - 1), protected
        for index, child in enumerate(node):
            child_size, child_keep = _measure(child, path + (index,), protected, out)
            size += child_size
            keep = keep or (child_keep and not history)
    else:
        size, keep = len(encoded(node).encode()), protected
    if path and not keep and size >= MIN_CUT:
        out.append((size, path))
    return size, keep


def _label(path):
    return ''.join(f'[{part}]' if isinstance(part, int) else '.' + str(part) for part in path).lstrip('.')


def _nested(a, b):
    return a[:len(b)] == b or b[:len(a)] == a


def _fit(data, budget):
    view = copy.deepcopy(data)
    omitted = []
    for _ in range(16):
        removable = []
        total, _ = _measure(view, (), False, removable)
        excess = total - budget
        if excess <= 0 or not removable:
            break
        # One part that clears the excess: the smallest such. Otherwise the
        # largest disjoint parts until the excess is cleared.
        fitting = [node for node in removable if node[0] >= excess]
        if fitting:
            chosen = [min(fitting, key=lambda node: node[0])]
        else:
            # Prefer a child that is most of its parent: drop usage.request_intervals,
            # not all of usage.
            largest_child = {}
            for size, path in removable:
                for depth in range(len(path)):
                    prefix = path[:depth]
                    largest_child[prefix] = max(largest_child.get(prefix, 0), size)
            chosen, freed = [], 0
            for size, path in sorted(removable, key=lambda node: -node[0]):
                if largest_child.get(path, 0) * 2 >= size:
                    continue
                if any(_nested(path, other) for _, other in chosen):
                    continue
                chosen.append((size, path))
                freed += size
                if freed >= excess:
                    break
        for size, path in chosen:
            parent = view
            for part in path[:-1]:
                parent = parent[part]
            if isinstance(path[-1], int):
                parent[path[-1]] = None  # keep list positions stable
            else:
                del parent[path[-1]]
            omitted.append({'path': _label(path)[:120], 'bytes': size})
    return view, omitted


def _save(state_root, tool, body):
    folder = Path(state_root) / DIRECTORY
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink() or not folder.is_dir():
        raise OSError('Unsafe MCP result directory')
    name = f'{int(time.time())}-{tool}-{hashlib.sha256(body).hexdigest()[:16]}.json'
    path = folder / name
    temp = folder / (name + '.' + secrets.token_hex(6) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(body + b'\n')
    os.replace(temp, path)
    _prune(folder, path)
    return path


def _prune(folder, keep):
    try:
        files = sorted(((p.stat().st_mtime, p) for p in folder.glob('*.json') if p != keep), reverse=True)
        stale = [p for p in folder.glob('*.tmp') if p.stat().st_mtime < time.time() - 3600]
    except OSError:
        return
    cutoff = time.time() - RETAIN_SECONDS
    stale += [path for index, (mtime, path) in enumerate(files) if index >= RETAIN_FILES - 1 or mtime < cutoff]
    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass


def bound(state_root, tool, data, limit=DEFAULT_LIMIT):
    """Return MCP text for data, normally at most `limit` bytes.

    Only removable parts are dropped, so a result whose protected fields alone
    exceed the limit is returned larger than the limit rather than incomplete.
    """
    text = encoded(data)
    body = text.encode()
    limit = limit_from(limit)
    if not limit or len(body) <= limit or not isinstance(data, dict):
        return text
    tool = re.sub(r'[^a-z0-9_]', '', str(tool).lower())[:48] or 'result'
    try:
        path = _save(state_root, tool, body)
    except OSError:
        # Never drop content that could not be saved elsewhere.
        return text
    view, omitted = _fit(data, limit - NOTICE_RESERVE)
    notice = {
        'bytes': len(body), 'limit': limit, 'full_result_path': str(path),
        'sha256': hashlib.sha256(body).hexdigest(), 'omitted_count': len(omitted),
        'hint': ('The full result is saved at full_result_path. Read only the omitted fields you need '
                 '(for example with jq); identifiers, status and next_action above are complete.')}
    # Long (for example CJK) paths must not push the notice past the limit.
    for listed in range(min(MAX_LISTED, len(omitted)), -1, -1):
        view['result_overflow'] = {**notice, 'omitted': omitted[:listed]}
        text = encoded(view)
        if len(text.encode()) <= limit:
            break
    return text

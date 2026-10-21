"""Best-effort work-phase reasoning telemetry; never an executor input or gate.

The relay only enqueues original bytes. Parsing, redaction and disk I/O happen
on a bounded, disposable observer thread, independently of usage accounting.
"""
import json
import queue
import re
import threading
import time

import activity
from isolation import SSEUsageTracker

INTERVAL = 0.15
MAX_PARTS = 32
MAX_STREAMS = 32
MAX_EVENT_BYTES = 256 * 1024
QUEUE_CHUNKS = 64
_WORD = re.compile(r'[A-Za-z0-9_./:+%=\-]+\Z')
_KINDS = {'response.reasoning_text': 'text', 'response.reasoning_summary_text': 'summary'}


def visible_prefix(raw, secrets, *, complete=False):
    """Keep unfinished credential-like words and exact secret prefixes private."""
    end = len(raw)
    word = _WORD.search(raw)
    if word and not complete:
        end = word.start()
    for secret in secrets:
        if not secret:
            continue
        for length in range(min(len(secret) - 1, len(raw)), 0, -1):
            if raw.endswith(secret[:length]):
                end = min(end, len(raw) - length)
                break
    return activity.clean(raw[:end], secrets)


class Decoder(SSEUsageTracker):
    """Reuse the bounded SSE framing parser without changing its usage path."""
    def __init__(self, recorder, stream_id, secrets=()):
        super().__init__(max_event_bytes=MAX_EVENT_BYTES)
        self.recorder, self.stream_id, self.secrets = recorder, stream_id, secrets
        self.parts = {}

    def _dispatch(self):
        if self.overflow:
            self._reset_event()
            return
        data = b'\n'.join(self.data_lines)
        self._reset_event()
        try:
            event = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if isinstance(event, dict):
            self.event(event)

    def event(self, event):
        kind = event.get('type', '')
        if not isinstance(kind, str):
            return
        prefix, _, suffix = kind.rpartition('.')
        if prefix in _KINDS and suffix in ('delta', 'done'):
            self.text(event, _KINDS[prefix], suffix == 'done')
        elif kind == 'response.output_item.done':
            self.item(event.get('item'), event.get('output_index', 0))
        elif kind in ('response.completed', 'response.failed', 'response.incomplete',
                      'response.cancelled', 'response.canceled'):
            response = event.get('response')
            if isinstance(response, dict) and isinstance(response.get('output'), list):
                for index, item in enumerate(response['output'][:MAX_PARTS]):
                    self.item(item, index)
            self.flush('completed' if kind == 'response.completed' else 'interrupted')

    def item(self, item, index):
        if not isinstance(item, dict) or item.get('type') != 'reasoning':
            return
        for field, mode in (('content', 'text'), ('summary', 'summary')):
            values = item.get(field)
            if not isinstance(values, list):
                continue
            for part_index, value in enumerate(values[:MAX_PARTS]):
                if isinstance(value, dict) and isinstance(value.get('text'), str):
                    self.text({'item_id': item.get('id'), 'output_index': index,
                               'content_index': part_index, 'summary_index': part_index,
                               'text': value['text']}, mode, True)

    def text(self, event, mode, done):
        text = event.get('text' if done else 'delta')
        if not isinstance(text, str):
            return
        index = event.get('summary_index' if mode == 'summary' else 'content_index', 0)
        output = event.get('output_index', 0)
        if type(index) is not int or not 0 <= index < MAX_PARTS or type(output) is not int:
            return
        item = event.get('item_id')
        item = item if isinstance(item, str) and 0 < len(item) <= 256 else str(output)
        identity = (item, mode, index)
        part = self.parts.get(identity)
        if part is None:
            if len(self.parts) >= MAX_PARTS:
                return
            part = self.parts[identity] = {'raw': '', 'sent': '', 'done': False, 'sequence': -1,
                                          'id': f'{self.stream_id}:{item}:{mode}:{index}'}
        if part['done']:
            return
        sequence = event.get('sequence_number')
        if type(sequence) is int:
            if sequence <= part['sequence']:
                return
            part['sequence'] = sequence
        raw = text if done else part['raw'] + text
        part['raw'] = raw[:activity.MAX_TEXT]
        if len(raw) > activity.MAX_TEXT:
            self.emit(part, 'truncated')
        elif done:
            self.emit(part, 'completed')

    def emit(self, part, status=None):
        if part['done']:
            return
        text = visible_prefix(part['raw'], self.secrets, complete=status == 'completed')
        if status == 'truncated':
            text += '\n[reasoning truncated]'
        snapshot = bool(status) or not text.startswith(part['sent'])
        payload = text if snapshot else text[len(part['sent']):]
        if payload or status:
            self.recorder.emit('reasoning', payload, item_id=activity.clean(part['id'], self.secrets),
                               event_type='reasoning.snapshot' if snapshot else 'reasoning.delta',
                               status=status or 'streaming', offset=0 if snapshot else len(part['sent']))
            part['sent'] = text
        part['done'] = bool(status)

    def flush(self, status=None):
        for part in self.parts.values():
            self.emit(part, status)


class Observer:
    def __init__(self, recorder, secrets=()):
        self.recorder = recorder
        self.secrets = tuple(recorder.secrets) + tuple(secrets)
        self.queue = queue.Queue(maxsize=QUEUE_CHUNKS)
        self.stopping, self.failed = threading.Event(), threading.Event()
        self.worker = threading.Thread(target=self.run, name='telos-reasoning', daemon=True)
        self.worker.start()

    def feed(self, stream_id, chunk):
        if self.stopping.is_set() or self.failed.is_set():
            return
        try:
            # The relay reads at most 64 KiB; reject larger external producers.
            if chunk is not None and len(chunk) > 65536:
                self.failed.set()
                return
            self.queue.put_nowait((stream_id, chunk))
        except Exception:
            self.failed.set()

    def end(self, stream_id):
        self.feed(stream_id, None)

    def close(self):
        self.stopping.set()
        self.worker.join(timeout=0.2)

    def run(self):
        streams = {}
        next_flush = time.monotonic() + INTERVAL
        try:
            while not self.failed.is_set():
                if self.stopping.is_set() and self.queue.empty():
                    break
                try:
                    stream_id, chunk = self.queue.get(timeout=max(0, next_flush - time.monotonic()))
                except queue.Empty:
                    chunk, stream_id = None, None
                if stream_id is not None:
                    if chunk is None:
                        decoder = streams.pop(stream_id, None)
                        if decoder:
                            decoder.finish()
                            decoder.flush('interrupted')
                    else:
                        if stream_id not in streams:
                            if len(streams) >= MAX_STREAMS:
                                self.failed.set()
                                break
                            streams[stream_id] = Decoder(self.recorder, stream_id, self.secrets)
                        streams[stream_id].feed(chunk)
                if time.monotonic() >= next_flush:
                    for decoder in streams.values():
                        decoder.flush()
                    next_flush = time.monotonic() + INTERVAL
            for decoder in streams.values():
                decoder.flush('interrupted')
            if self.failed.is_set():
                self.recorder.emit('diagnostic', 'Reasoning display stopped (observer backlog); task continues.')
        except Exception:
            self.failed.set()
            # A display failure must never become a model, budget or gate error.
            try:
                self.recorder.emit('diagnostic', 'Reasoning display unavailable; task continues.')
            except Exception:
                pass


def start(recorder, secrets=()):
    if recorder is None or recorder.phase != 'work':
        return None
    try:
        return Observer(recorder, secrets)
    except Exception:
        return None

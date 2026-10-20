"""Closed-book protocol for the executor's existing conversation.

This module has no model client or grader. The executor driver resumes the work
thread; the relay enforces this policy before any response reaches that driver.
"""
import hashlib
import json

from providers import ProviderError
from usage import normalize_usage


JSON_OBJECT_PROVIDER = 'bailian'
JSON_OBJECT_MODEL = 'deepseek-v4.1-flash-1017'
ANSWER_SCHEMA = '{"answers":[{"id":"<exact question id>","answer":"<nonempty answer text>"}]}'

# parse_answers owns the strict schema. These fixed translations keep attempt
# diagnostics useful without copying raw model text into evidence.
FORMAT_ERRORS = {
    'Exam returned invalid JSON': 'invalid JSON',
    'Exam must return only an answers array': 'root must contain only an answers array',
    'Each answer must contain exactly id and answer': 'each answer needs exactly id and answer',
    'Answer id and answer must be nonempty strings': 'id and answer must be nonempty strings',
    'Missing, duplicate, or unknown question ids': 'missing, duplicate, or unknown question ids',
}


def format_diagnostic(error):
    """Return a bounded schema diagnostic, never attacker- or model-controlled text."""
    return FORMAT_ERRORS.get(str(error), 'answer schema violation')[:160]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


class ExamPolicy:
    MAX_RESPONSE_BYTES = 16 * 1024 * 1024

    def __init__(self, binding):
        self.binding = binding
        self.requests = []
        self.responses = []
        self.failure = None
        self.transport_error = None

    def prepare(self, raw):
        request = json.loads(raw)
        if request.get('model') != self.binding['model']:
            raise ProviderError('Exam attempted to change the frozen executor model')
        scoped_json_object = (self.binding.get('provider') == JSON_OBJECT_PROVIDER
                              and self.binding.get('model') == JSON_OBJECT_MODEL)
        if scoped_json_object:
            text_options = request.get('text')
            request['text'] = {**(text_options if isinstance(text_options, dict) else {}),
                               'format': {'type': 'json_object'}}
        request.update(tools=[], tool_choice='none', store=False,
                       max_output_tokens=self.binding['exam_max_tokens'])
        request.pop('previous_response_id', None)
        request.pop('parallel_tool_calls', None)
        if isinstance(request.get('input'), list):
            # Recent CLI models declare tools inside history as well as at the
            # top level. Remove declarations, retaining past calls and results.
            request['input'] = [item for item in request['input']
                                if item.get('type') not in ('additional_tools', 'tool_search_output')]
        request['reasoning'] = {**request.get('reasoning', {}), 'effort': self.binding['reasoning_effort']}
        # The same conversation includes earlier tool results. These remain input
        # evidence, but no tools are advertised or executed during this turn.
        text = request.get('text')
        text_format = text.get('format') if isinstance(text, dict) else None
        self.requests.append({'request_hash': fingerprint(request),
                              'input_hash': fingerprint(request.get('input', [])),
                              'tool_choice': 'none', 'tools': [],
                              'model': request['model'], 'reasoning_effort': self.binding['reasoning_effort'],
                              'text_format': text_format.get('type') if isinstance(text_format, dict) else None})
        return json.dumps(request, ensure_ascii=False).encode()

    @staticmethod
    def check_item(item):
        if not isinstance(item, dict) or item.get('type') not in ('message', 'reasoning'):
            raise ProviderError('Closed-book response attempted a tool call or unsupported output item')
        if item['type'] == 'message':
            if item.get('role') != 'assistant' or any(
                    c.get('type') not in ('output_text', 'refusal') for c in item.get('content', [])):
                raise ProviderError('Closed-book response contains unsupported message content')

    def validate_response(self, raw, content_type):
        """Buffer and validate the entire response before forwarding even one event."""
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise ProviderError('Closed-book response exceeds the bounded response size')
        if 'text/event-stream' in content_type:
            events = []
            for block in raw.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
                data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
                if data and data != '[DONE]':
                    events.append(json.loads(data))
            completed = []
            for event in events:
                kind = event.get('type', '')
                if not (kind in ('response.created', 'response.in_progress', 'response.queued',
                                  'response.output_item.added', 'response.output_item.done', 'response.completed')
                        or kind.startswith(('response.output_text.', 'response.content_part.',
                                            'response.reasoning', 'response.refusal.'))):
                    raise ProviderError('Closed-book response contains a disallowed stream event')
                if 'part' in event and event['part'].get('type') not in ('output_text', 'refusal', 'summary_text', 'reasoning_text'):
                    raise ProviderError('Closed-book stream contains unsupported message content')
                if 'item' in event:
                    self.check_item(event['item'])
                for item in event.get('response', {}).get('output', []):
                    self.check_item(item)
                if kind == 'response.completed':
                    completed.append(event['response'])
            if len(completed) != 1:
                raise ProviderError('Closed-book response did not complete exactly once')
            response = completed[0]
        else:
            response = json.loads(raw)
        if response.get('status') != 'completed' or not response.get('output'):
            raise ProviderError('Closed-book response is incomplete')
        for item in response['output']:
            self.check_item(item)
        self.responses.append({'response_hash': hashlib.sha256(raw).hexdigest(),
                               'reported_model': response.get('model'), 'tool_call_count': 0,
                               'usage': response.get('usage')})


def question_payload(contract):
    # Deliberately contains neither source code, task context, test output,
    # references, scoring rules nor the supervisor's private feedback.
    return {'questions': [{'id': q['id'], 'prompt': q['prompt']} for q in contract['questions']]}


def exam_instructions(b, contract, payload, retry=False):
    ids = b.encoded([q['id'] for q in contract['questions']])
    schema_rules = (
        'The question objects in the examination message are INPUT data with the fields "id" and "prompt". '
        'Your outgoing answer objects are OUTPUT data with exactly the fields "id" and "answer"; '
        'never return or copy a "prompt" field. Return only one JSON object, with no Markdown fences, '
        'prose, comments or extra keys, in exactly this schema: ' + ANSWER_SCHEMA + ' '
        'The top-level object must contain only the single key "answers". '
        'Use the exact question IDs listed below, exactly one nonempty answer per ID, with no missing, '
        'duplicate or unknown IDs. Both id and answer must be JSON strings; escape double quotes as \\" '
        'and newlines as \\n. ')
    if retry:
        return ('Your previous response did not match the required closed-book JSON answer schema. '
                'Tools remain disabled. Do not read files, search, run commands or request another agent. '
                + schema_rules + 'Required question IDs: ' + ids + '.')
    return ('The work phase has ended. This is your closed-book examination in the SAME conversation. '
            'Use only what you already know from your work and this conversation. All tools are disabled. '
            'Do not read files, search, run commands, edit code or request another agent. '
            'If you do not know an answer, say so; the answer string must still be nonempty. '
            'Do not grade yourself or claim acceptance. ' + schema_rules
            + 'Required question IDs: ' + ids + '.\nQuestions:\n' + b.encoded(payload))


def response_usage(attempts):
    """API response usage is per request; Codex resume's usage can be cumulative."""
    totals = {}
    unknown = 0
    found = False
    for attempt in attempts:
        for response in attempt['responses']:
            usage = response.get('usage')
            if not isinstance(usage, dict):
                unknown += 1
                continue
            found = True
            current = normalize_usage(usage)
            if current is None:
                unknown += 1
                continue
            if current.get('input_tokens') is None or current.get('output_tokens') is None:
                unknown += 1
            for key, value in current.items():
                if type(value) is int:
                    totals[key] = totals.get(key, 0) + value
    if not found:
        return None
    totals['unknown_responses'] = unknown
    return totals


def answer(b, state, contract, commit):
    if not state.get('thread_id'):
        raise b.GateError('Closed-book exam requires the executor\'s existing work conversation')
    binding = b.backend_bindings(contract)['executor']
    payload = question_payload(contract)
    prompt = exam_instructions(b, contract, payload)
    attempts = []
    for number in range(1, binding['exam_format_attempts'] + 1):
        policy = ExamPolicy(binding)
        result = b.peer_turn(state, contract, prompt, phase='exam', policy=policy)
        if result['thread_id'] != state['thread_id'] or not policy.responses:
            raise b.GateError('Closed-book exam lost its executor conversation or transport evidence')
        format_valid = False
        format_error = None
        try:
            answers = b.parse_answers(result['summary'], contract['questions'])
        except b.ExamFormatError as error:
            format_error = format_diagnostic(error)
        else:
            format_valid = True
        attempt = {'attempt': number, 'cli_session_usage': result.get('usage'),
                   'requests': policy.requests, 'responses': policy.responses,
                   'format_valid': format_valid, 'format_error': format_error}
        attempts.append(attempt)
        if not format_valid:
            prompt = exam_instructions(b, contract, payload, retry=True)
            continue
        return {'answers': answers['answers'], 'mode': 'closed_book', 'executor_thread_id': state['thread_id'],
                'session_id': state['session_id'], 'commit': commit, 'backend': b.providers.public_binding(binding),
                'public_payload_hash': b.digest(payload), 'tool_call_count': 0, 'same_executor': True,
                'usage': response_usage(attempts), 'usage_scope': 'exam_requests', 'attempts': attempts}
    error = b.ExamFormatError('Executor failed the closed-book answer format; no acceptance')
    error.diagnostics = {'attempts': attempts, 'executor_thread_id': state['thread_id']}
    raise error


def validate_evidence(b, state, contract, packet):
    exam = packet.get('exam', {})
    expected = {'mode': 'closed_book', 'executor_thread_id': state.get('thread_id'),
                'session_id': state['session_id'], 'commit': packet['commit'], 'same_executor': True,
                'tool_call_count': 0, 'public_payload_hash': b.digest(question_payload(contract)),
                'backend': b.providers.public_binding(b.backend_bindings(contract)['executor'])}
    if not state.get('thread_id') or any(exam.get(k) != v for k, v in expected.items()):
        raise b.GateError('Exam evidence does not match the same executor, questions and submitted commit')
    if not exam.get('attempts') or any(not a.get('requests') or not a.get('responses')
                                      for a in exam['attempts']):
        raise b.GateError('Missing closed-book transport evidence')
    for attempt in exam['attempts']:
        for request in attempt['requests']:
            if (request.get('tools') != [] or request.get('tool_choice') != 'none'
                    or request.get('model') != expected['backend']['model']
                    or request.get('reasoning_effort') != expected['backend']['reasoning_effort']):
                raise b.GateError('Exam transport enabled tools or changed the executor binding')
        if any(r.get('tool_call_count') != 0 for r in attempt['responses']):
            raise b.GateError('Exam transport executed a tool')
    b.parse_answers(b.encoded({'answers': exam.get('answers')}), contract['questions'])

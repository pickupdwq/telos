"""Closed-book protocol for the executor's existing conversation.

This module has no model client or grader. The executor driver resumes the work
thread; the relay enforces this policy before any response reaches that driver.
"""
import hashlib
import json

from providers import ProviderError


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
        request.update(tools=[], tool_choice='none', store=False,
                       max_output_tokens=self.binding['exam_max_tokens'])
        request.pop('previous_response_id', None)
        request.pop('parallel_tool_calls', None)
        request['reasoning'] = {**request.get('reasoning', {}), 'effort': self.binding['reasoning_effort']}
        # The same conversation includes earlier tool results. These remain input
        # evidence, but no tools are advertised or executed during this turn.
        self.requests.append({'request_hash': fingerprint(request),
                              'input_hash': fingerprint(request.get('input', [])),
                              'tool_choice': 'none', 'tools': [],
                              'model': request['model'], 'reasoning_effort': self.binding['reasoning_effort']})
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


def response_usage(attempts):
    """API response usage is per request; Codex resume's usage can be cumulative."""
    totals = {}
    for attempt in attempts:
        for response in attempt['responses']:
            usage = response.get('usage')
            if not isinstance(usage, dict):
                return None
            current = {k: usage[k] for k in ('input_tokens', 'output_tokens', 'total_tokens') if k in usage}
            current['cached_input_tokens'] = (usage.get('input_tokens_details') or {}).get('cached_tokens', 0)
            current['reasoning_output_tokens'] = (usage.get('output_tokens_details') or {}).get('reasoning_tokens', 0)
            for key, value in current.items():
                if type(value) is int:
                    totals[key] = totals.get(key, 0) + value
    return totals


def answer(b, state, contract, commit):
    if not state.get('thread_id'):
        raise b.GateError('Closed-book exam requires the executor\'s existing work conversation')
    binding = b.backend_bindings(contract)['executor']
    payload = question_payload(contract)
    prompt = ('The work phase has ended. This is your closed-book examination in the SAME conversation. '
              'Use only what you already know from your work and this conversation. All tools are disabled. '
              'Do not read files, search, run commands, edit code or request another agent. '
              'If you do not know an answer, say so. Do not grade yourself or claim acceptance. '
              'Return only JSON: {"answers":[{"id":"question id","answer":"your answer"}]}, '
              'exactly one nonempty answer for each question.\n' + b.encoded(payload))
    attempts = []
    for number in range(1, binding['exam_format_attempts'] + 1):
        policy = ExamPolicy(binding)
        result = b.peer_turn(state, contract, prompt, phase='exam', policy=policy)
        if result['thread_id'] != state['thread_id'] or not policy.responses:
            raise b.GateError('Closed-book exam lost its executor conversation or transport evidence')
        attempt = {'attempt': number, 'cli_session_usage': result.get('usage'),
                   'requests': policy.requests, 'responses': policy.responses}
        attempts.append(attempt)
        try:
            answers = b.parse_answers(result['summary'], contract['questions'])
        except b.ExamFormatError:
            prompt = ('Your previous response did not match the required JSON answer schema. '
                      'Tools remain disabled. Return only the JSON object with exactly these question IDs: '
                      + b.encoded([q['id'] for q in contract['questions']]))
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

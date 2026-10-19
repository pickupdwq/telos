"""Bounded Responses <-> Chat Completions translation for local executors.

The CLI keeps the conversation. The adapter preserves call IDs and custom-tool
inputs; it never executes tools or invents a completed response from a truncation.
"""
import hashlib
import json
import re
import time
import uuid

from providers import ProviderError


def tool_name(name, namespace=None):
    if not isinstance(name, str) or not name or (namespace is not None and not isinstance(namespace, str)):
        raise ProviderError('Malformed local tool name/namespace')
    full = namespace + '__' + name if namespace else name
    if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', full):
        return full
    return 'tool_' + hashlib.sha256(json.dumps([namespace, name]).encode()).hexdigest()[:48]


def definitions(tools, namespace=None):
    for tool in tools:
        if tool.get('type') == 'namespace' and namespace is None:
            yield from definitions(tool['tools'], tool['name'])
        else:
            yield tool, namespace


def content(parts):
    if isinstance(parts, str):
        return parts
    result = []
    for part in parts or []:
        kind = part.get('type')
        if kind in ('input_text', 'output_text', 'text'):
            result.append({'type': 'text', 'text': part['text']})
        elif kind == 'input_image' and isinstance(part.get('image_url'), str):
            result.append({'type': 'image_url', 'image_url': {'url': part['image_url'], 'detail': part.get('detail', 'auto')}})
        elif kind == 'refusal':
            result.append({'type': 'text', 'text': part['refusal']})
        else:
            raise ProviderError('Unsupported local Chat message content: ' + str(kind))
    if all(p['type'] == 'text' for p in result):
        return '\n'.join(p['text'] for p in result)
    return result


def prepare(request):
    if request.get('previous_response_id'):
        raise ProviderError('Local Chat requires explicit conversation history, not previous_response_id')
    messages, tool_types = [], {}
    if request.get('instructions'):
        messages.append({'role': 'system', 'content': request['instructions']})
    items = request.get('input', [])
    if isinstance(items, str):
        items = [{'role': 'user', 'content': items}]
    available = list(request.get('tools', []))
    reasoning = ''
    for item in items:
        kind = item.get('type', 'message')
        if kind == 'additional_tools':
            available.extend(item.get('tools', []))
            continue
        if kind == 'reasoning':
            if item.get('encrypted_content'):
                raise ProviderError('Encrypted reasoning cannot be translated to Chat Completions')
            # CLI resumes serialize optional reasoning arrays as null.
            parts = (item.get('summary') or []) + (item.get('content') or [])
            reasoning += '\n'.join(p.get('text', '') for p in parts)
            continue
        if kind == 'message':
            role = item['role']
            if role not in ('system', 'developer', 'user', 'assistant'):
                raise ProviderError('Unsupported local Chat message role')
            message = {'role': 'system' if role == 'developer' else role, 'content': content(item.get('content'))}
        elif kind in ('function_call', 'custom_tool_call'):
            args = item['arguments'] if kind == 'function_call' else json.dumps({'input': item['input']}, ensure_ascii=False)
            call = {'id': item['call_id'], 'type': 'function',
                    'function': {'name': tool_name(item['name'], item.get('namespace')), 'arguments': args}}
            if messages and messages[-1]['role'] == 'assistant' and 'tool_calls' in messages[-1]:
                messages[-1]['tool_calls'].append(call)
                continue
            message = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            message = {'role': 'tool', 'tool_call_id': item['call_id'], 'content': content(item['output'])}
        else:
            raise ProviderError('Unsupported local Chat history item: ' + str(kind))
        if message['role'] == 'assistant' and reasoning:
            message['reasoning_content'], reasoning = reasoning, ''
        messages.append(message)
    tools = []
    # Historical declarations never re-enable tools when the exam disables them.
    if request.get('tool_choice') == 'none':
        available = []
    for tool, namespace in definitions(available):
        kind, name = tool.get('type'), tool.get('name')
        if kind not in ('function', 'custom'):
            raise ProviderError('Local Chat supports uniquely named function/custom tools only')
        alias = tool_name(name, namespace)
        if alias in tool_types:
            raise ProviderError('Ambiguous local Chat tool names')
        tool_types[alias] = {'type': kind, 'name': name, 'namespace': namespace}
        definition = {k: tool[k] for k in ('name', 'description') if k in tool}
        definition['name'] = alias
        if kind == 'function':
            definition.update(parameters=tool.get('parameters', {'type': 'object', 'properties': {}}))
            if 'strict' in tool:
                definition['strict'] = tool['strict']
        else:
            definition['parameters'] = {'type': 'object', 'properties': {'input': {'type': 'string'}},
                                        'required': ['input'], 'additionalProperties': False}
            if tool.get('format'):
                definition['description'] = definition.get('description', '') + '\nThe input string must follow: ' + json.dumps(tool['format'])
        tools.append({'type': 'function', 'function': definition})
    result = {'model': request['model'], 'messages': messages, 'stream': False}
    choice = request.get('tool_choice')
    if tools:
        result['tools'] = tools
    if choice is not None:
        if isinstance(choice, dict):
            if choice.get('type') not in ('function', 'custom'):
                raise ProviderError('Unsupported local Chat tool choice')
            choice = {'type': 'function', 'function': {'name': tool_name(choice['name'], choice.get('namespace'))}}
        result['tool_choice'] = choice
    for key in ('temperature', 'top_p', 'parallel_tool_calls'):
        if key in request:
            result[key] = request[key]
    if request.get('max_output_tokens') is not None:
        result['max_tokens'] = request['max_output_tokens']
    effort = request.get('reasoning', {}).get('effort')
    if effort and effort != 'none':
        result['reasoning_effort'] = effort
    fmt = request.get('text', {}).get('format', {})
    if fmt.get('type') in ('json_object', 'json_schema'):
        result['response_format'] = ({'type': 'json_object'} if fmt['type'] == 'json_object' else
                                     {'type': 'json_schema', 'json_schema': {k: v for k, v in fmt.items() if k != 'type'}})
    return result, tool_types


def convert(response, model, tool_types):
    choices = response.get('choices')
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderError('Local Chat must return exactly one choice')
    choice = choices[0]
    message = choice.get('message', {})
    if message.get('role', 'assistant') != 'assistant' or message.get('function_call'):
        raise ProviderError('Unsupported local Chat response')
    output = []
    reasoning = message.get('reasoning_content')
    if reasoning:
        if not isinstance(reasoning, str):
            raise ProviderError('Malformed local reasoning content')
        output.append({'type': 'reasoning', 'id': 'rs_' + uuid.uuid4().hex,
                       'summary': [{'type': 'summary_text', 'text': reasoning}]})
    text, refusal = message.get('content'), message.get('refusal')
    if text is not None and not isinstance(text, str):
        raise ProviderError('Local Chat output content must be text')
    if text or refusal:
        parts = ([{'type': 'output_text', 'text': text, 'annotations': []}] if text else [])
        if refusal:
            parts.append({'type': 'refusal', 'refusal': refusal})
        output.append({'type': 'message', 'id': 'msg_' + uuid.uuid4().hex, 'role': 'assistant',
                       'status': 'completed', 'content': parts})
    seen = set()
    for call in message.get('tool_calls') or []:
        function = call.get('function', {})
        name, args, call_id = function.get('name'), function.get('arguments'), call.get('id')
        if (call.get('type') != 'function' or name not in tool_types or not isinstance(args, str)
                or not isinstance(call_id, str) or not call_id or call_id in seen):
            raise ProviderError('Local Chat returned an unknown or malformed tool call')
        seen.add(call_id)
        metadata = tool_types[name]
        item = {'id': 'call_' + uuid.uuid4().hex, 'call_id': call_id, 'name': metadata['name'], 'status': 'completed'}
        if metadata['namespace']:
            item['namespace'] = metadata['namespace']
        if metadata['type'] == 'custom':
            value = json.loads(args)
            if not isinstance(value, dict) or set(value) != {'input'} or not isinstance(value['input'], str):
                raise ProviderError('Custom tool requires exactly one input string')
            item.update(type='custom_tool_call', input=value['input'])
        else:
            item.update(type='function_call', arguments=args)
        output.append(item)
    reason = choice.get('finish_reason')
    if reason not in ('stop', 'tool_calls', 'length', 'content_filter') or not output:
        raise ProviderError('Local Chat response did not finish with usable output')
    if all(item['type'] == 'reasoning' for item in output):
        raise ProviderError('Local Chat returned reasoning without a final answer or tool call')
    status = 'completed' if reason in ('stop', 'tool_calls') else 'incomplete'
    result = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'created_at': int(time.time()),
              'model': response.get('model', model), 'status': status, 'output': output}
    if status == 'incomplete':
        result['incomplete_details'] = {'reason': 'max_output_tokens' if reason == 'length' else 'content_filter'}
    usage = response.get('usage')
    if isinstance(usage, dict):
        result['usage'] = {'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': usage.get('completion_tokens', 0),
                           'total_tokens': usage.get('total_tokens', 0),
                           'input_tokens_details': {'cached_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)},
                           'output_tokens_details': {'reasoning_tokens': (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)}}
    return result


def encode(response, stream):
    if not stream:
        return json.dumps(response, ensure_ascii=False).encode(), 'application/json'
    events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}}]
    for index, item in enumerate(response['output']):
        events.append({'type': 'response.output_item.added', 'output_index': index, 'item': item})
        events.append({'type': 'response.output_item.done', 'output_index': index, 'item': item})
    events.append({'type': 'response.' + response['status'], 'response': response})
    for sequence, event in enumerate(events):
        event['sequence_number'] = sequence
    return ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e, ensure_ascii=False) + '\n\n' for e in events).encode(), 'text/event-stream'

#!/usr/bin/env python3
"""Opt-in real relay -> local Postman fixture check; never uses a provider key."""
import argparse
import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
import urllib.request

from isolation import model_relay, response_outcome
from providers import resolve_binding
from usage import UsageLedger
from activity import Recorder, Reader, FILENAME
from watch import EventBuffer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.query or url.fragment):
        parser.error('Use the local simulation URL at http://127.0.0.1:<port>/bailian/compatible-mode/v1')
    binding = resolve_binding({}, provider='bailian')
    payload = {'model': binding['model'], 'reasoning': {'effort': binding['reasoning_effort']},
               'parallel_tool_calls': True,
               'store': False, 'stream': False, 'input': [{'role': 'user', 'content': 'Run provider_probe.'}],
               'tools': [{'type': 'function', 'name': 'provider_probe',
                          'parameters': {'type': 'object', 'properties': {'value': {'type': 'integer'}}}}]}
    with tempfile.TemporaryDirectory(prefix='telos-bailian-simulation-') as folder:
        ledger = UsageLedger(Path(folder) / 'usage.json', {'max_requests': 2})
        recorder = Recorder(folder, 'simulation', 'work')
        with model_relay(args.base_url, 'SYNTHETIC_SIMULATION_ONLY', binding=binding, usage_ledger=ledger,
                         usage_context={'phase': 'work', 'provider': 'bailian', 'model': binding['model']},
                         activity_recorder=recorder) as (endpoint, token, _):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def send(data):
                req = urllib.request.Request(endpoint + '/responses', data=json.dumps(data).encode(),
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
                with opener.open(req, timeout=10) as response:
                    return response.read(), response.headers.get('Content-Type')
            raw, _ = send(payload)
            output = json.loads(raw)['output']
            assert len(output) == 2 and all(item['type'] == 'function_call' for item in output)
            payload['input'] += output + [
                {'type': 'function_call_output', 'call_id': item['call_id'],
                 'output': str(json.loads(item['arguments'])['value'])} for item in reversed(output)]
            raw, content_type = send({**payload, 'stream': True})
            assert response_outcome(raw, content_type) == 'completed'
        snapshot = ledger.snapshot()
        assert snapshot['totals']['total_tokens'] == 240, snapshot
        assert snapshot['totals']['reasoning_output_tokens'] == 24, snapshot
        displayed = [event for event in EventBuffer(Reader(Path(folder) / FILENAME).read())
                     if event.get('kind') == 'reasoning']
        assert len(displayed) == 1 and displayed[0]['text'] == 'Check tool results. Both values match.', displayed
        print(json.dumps({'passed': True, 'boundary': 'local Postman HTTP fixture, not live inference',
                          'reasoning_observed': True,
                          'json_and_sse': True, 'parallel_history_repaired': True,
                          'tool_round_trip': True, 'usage': snapshot}, indent=2))


if __name__ == '__main__':
    main()

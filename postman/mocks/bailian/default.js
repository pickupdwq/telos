// Local contract fixture, based on the Bailian Responses API documentation.
// It also reproduces the live provider's adjacent tool-result requirement.
const http = require('node:http');

http.createServer(async (req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  };
  if (req.method !== 'POST' || req.url !== '/compatible-mode/v1/responses') {
    return send(501, { error: { code: 'fixture_route_not_implemented' } });
  }
  try {
    let body = req.body;
    if (!body) {
      let raw = '';
      for await (const chunk of req) raw += chunk;
      body = JSON.parse(raw);
    }
    if (body.model !== 'deepseek-v4.1-flash-1017' || body.reasoning?.effort !== 'max'
        || body.store !== false || body.parallel_tool_calls !== false || !Array.isArray(body.input)) {
      return send(400, { error: { code: 'invalid_fixture_request' } });
    }
    const results = body.input.filter(x => x.type === 'function_call_output');
    const returned = results.length > 0;
    if (returned) {
      for (const [index, item] of body.input.entries()) {
        if (item.type === 'function_call') {
          const result = body.input[index + 1];
          if (result?.type !== 'function_call_output' || result.call_id !== item.call_id) {
            return send(400, { error: { code: 'tool_result_must_immediately_follow_call' } });
          }
        }
      }
      if (results.length !== 2 || results[0].call_id !== 'call_probe' || results[0].output !== '42'
          || results[1].call_id !== 'call_probe2' || results[1].output !== '84') {
        return send(400, { error: { code: 'invalid_tool_result' } });
      }
    }
    if (!returned && !body.tools?.some(t => t.type === 'function' && t.name === 'provider_probe')) {
      return send(400, { error: { code: 'missing_tool_definition' } });
    }
    const output = returned
      ? [{ type: 'message', id: 'msg_probe', role: 'assistant', status: 'completed',
           content: [{ type: 'output_text', text: '42, 84', annotations: [] }] }]
      : [{ type: 'function_call', id: 'fc_probe', call_id: 'call_probe',
           name: 'provider_probe', arguments: '{"value":42}' },
         { type: 'function_call', id: 'fc_probe2', call_id: 'call_probe2',
           name: 'provider_probe', arguments: '{"value":84}' }];
    const response = { id: returned ? 'resp_done' : 'resp_call', object: 'response',
      model: body.model, status: 'completed', output,
      usage: { input_tokens: 100, output_tokens: 20, total_tokens: 120,
        input_tokens_details: { cached_tokens: 50 }, output_tokens_details: { reasoning_tokens: 12 } } };
    if (!body.stream) return send(200, response);
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const events = [{ type: 'response.created', response: { ...response, status: 'in_progress', output: [] } }];
    events.push(
      { type: 'response.reasoning_text.delta', item_id: 'reason_probe', output_index: 1,
        content_index: 0, delta: 'Check tool results. ' },
      { type: 'response.reasoning_text.delta', item_id: 'reason_probe', output_index: 1,
        content_index: 0, delta: 'Both values match.' },
      { type: 'response.reasoning_text.done', item_id: 'reason_probe', output_index: 1,
        content_index: 0, text: 'Check tool results. Both values match.' }
    );
    output.forEach((item, output_index) => events.push(
      { type: 'response.output_item.added', output_index, item },
      { type: 'response.output_item.done', output_index, item }
    ));
    events.push({ type: 'response.completed', response });
    for (const event of events) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    res.end();
  } catch {
    send(400, { error: { code: 'invalid_json' } });
  }
}).listen(process.env.PORT);

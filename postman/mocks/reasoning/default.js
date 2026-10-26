// Local Responses contract fixture; inference answers are deterministic.
const http = require('node:http');
const runs = new Map();
http.createServer(async (req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  };
  if (req.method === 'GET' && req.url.startsWith('/fixture/evidence/')) {
    return send(200, { requests: runs.get(req.url.split('/').pop()) || [] });
  }
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
    const runId = JSON.stringify(body.input).match(/REASONING_FIXTURE_ID: ([a-f0-9]+)/)?.[1];
    if (!runId || body.model !== 'deepseek-v4.1-flash-1017' || body.store !== false
        || !['low', 'high', 'max'].includes(body.reasoning?.effort)) {
      return send(400, { error: { code: 'invalid_reasoning_fixture_request' } });
    }
    if (!runs.has(runId)) runs.set(runId, []);
    runs.get(runId).push(body);
    const exam = body.tool_choice === 'none';
    if ((exam && body.tools.length !== 0) || (!exam && body.parallel_tool_calls !== false)) {
      return send(400, { error: { code: 'invalid_tool_policy' } });
    }
    const text = exam ? JSON.stringify({ answers: [{ id: 'q1', answer: '5' }, { id: 'q2', answer: '3' }] })
      : JSON.stringify({ status: 'completed', summary: 'Addition verified.', clarification_questions: [],
          changed_files: [], checks_performed: [], remaining_issues: [] });
    const item = { type: 'message', id: 'msg_fixture', role: 'assistant', status: 'completed',
      content: [{ type: 'output_text', text, annotations: [] }] };
    const response = { id: 'resp_fixture', object: 'response', model: body.model, status: 'completed', output: [item],
      usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 } };
    if (!body.stream) return send(200, response);
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const events = [
      { type: 'response.created', response: { ...response, status: 'in_progress', output: [] } },
      { type: 'response.output_item.added', output_index: 0, item },
      { type: 'response.output_item.done', output_index: 0, item },
      { type: 'response.completed', response }
    ];
    for (const event of events) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    res.end();
  } catch {
    send(400, { error: { code: 'invalid_json' } });
  }
}).listen(process.env.PORT);

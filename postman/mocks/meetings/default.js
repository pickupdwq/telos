// Local Responses API fixture, using the repository's documented Bailian envelope.
// Native CLI tool execution and the Telos scheduler run outside this mock.
const http = require('node:http');
const runs = new Map();
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
const probe = `import json, os, pathlib, subprocess
source = pathlib.Path.cwd()
assert 'def add' in (source / 'calc.py').read_text()
blocked = []
gitdir = pathlib.Path(subprocess.check_output(['git','rev-parse','--git-common-dir'], text=True).strip())
for target in (source / 'calc.py', source / 'forbidden.txt', gitdir / 'meeting-write-probe'):
    try:
        with target.open('a') as stream: stream.write('FORBIDDEN')
    except PermissionError:
        blocked.append(str(target))
    else:
        raise AssertionError('read-only sandbox failed: ' + str(target))
scratch = pathlib.Path(os.environ['TELOS_DISCUSSION_SCRATCH']) / 'experiment.txt'
scratch.write_text('experiment passed')
assert scratch.read_text() == 'experiment passed'
assert not os.environ.get('TELOS_RELAY_TOKEN')
print('MEETING_SANDBOX_OK source_read git_read source_write_denied git_write_denied scratch_write_ok')
`;

const server = http.createServer(async (req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  };
  if (req.method === 'GET' && req.url.startsWith('/fixture/evidence/')) {
    const run = runs.get(req.url.split('/').pop());
    return send(200, run ? { requests: run.requests, parallelFirstRound: run.parallel } : {});
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
    const history = JSON.stringify(body.input);
    const runId = history.match(/MEETING_FIXTURE_ID: ([a-f0-9]+)/)?.[1];
    const python = history.match(/MEETING_FIXTURE_PYTHON: ([^\\\n]+)/)?.[1];
    if (!runId || !python || body.model !== 'deepseek-v4.1-flash-1017' || body.store !== false
        || (body.tool_choice !== 'none' && body.parallel_tool_calls !== false) || body.reasoning?.effort !== 'max') {
      return send(400, { error: { code: 'invalid_meeting_fixture_request' } });
    }
    if (!runs.has(runId)) runs.set(runId, { requests: [], pending: [], parallel: false });
    const run = runs.get(runId);
    const participant = history.match(/Native perspective (p[01])/ )?.[1];
    const followup = history.includes('FOCUSED_FOLLOWUP');
    const results = body.input.filter(x => x.type === 'function_call_output');
    const verified = results.some(x => JSON.stringify(x.output).includes('MEETING_SANDBOX_OK'));
    run.requests.push({ participant, followup, toolsDisabled: body.tool_choice === 'none',
      sandboxVerified: verified, input: body.input, at: Date.now() });
    const number = run.requests.length;
    let output;
    if (participant && !results.length) {
      // No first-round response is released until both independent requests arrive.
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('parallel first round missing')), 20000);
        run.pending.push(() => { clearTimeout(timer); resolve(); });
        if (run.pending.length === 2) {
          run.parallel = true;
          run.pending.forEach(release => release());
        }
      });
      const command = quote(python.trim()) + ' -I -c ' + quote(probe);
      const shell = body.tools.some(tool => tool.name === 'shell_command');
      output = [{ type: 'function_call', id: 'fc_' + number, call_id: 'call_' + number,
        name: shell ? 'shell_command' : 'exec_command',
        arguments: JSON.stringify(shell ? { command, timeout_ms: 10000 } : { cmd: command, yield_time_ms: 1000, max_output_tokens: 1000 }) }];
    } else {
      if (participant && !verified) return send(400, { error: { code: 'sandbox_probe_failed' } });
      const text = participant ? JSON.stringify({
        summary: followup ? 'Targeted disagreement resolved.' : 'Independent source inspection completed.',
        proposal: 'Preserve add(a, b); verify signed arithmetic.', risks: [], questions: [],
        evidence: ['MEETING_SANDBOX_OK: source and Git are read-only; scratch experiment passed.']
      }) : body.tool_choice === 'none' ? JSON.stringify({ answers: [{ id: 'q1', answer: '5' }, { id: 'q2', answer: '3' }] })
        : JSON.stringify({ status: 'completed', summary: 'Approved addition behavior verified.',
          clarification_questions: [], changed_files: [], checks_performed: [], remaining_issues: [] });
      output = [{ type: 'message', id: 'msg_' + number, role: 'assistant', status: 'completed',
        content: [{ type: 'output_text', text, annotations: [] }] }];
    }
    const response = { id: 'resp_' + number, object: 'response', model: body.model, status: 'completed', output,
      usage: { input_tokens: 100, output_tokens: 20, total_tokens: 120 } };
    if (!body.stream) return send(200, response);
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const events = [{ type: 'response.created', response: { ...response, status: 'in_progress', output: [] } }];
    output.forEach((item, output_index) => events.push(
      { type: 'response.output_item.added', output_index, item },
      { type: 'response.output_item.done', output_index, item }
    ));
    events.push({ type: 'response.completed', response });
    for (const event of events) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    res.end();
  } catch (error) {
    send(400, { error: { code: 'meeting_fixture_failure', message: error.message } });
  }
});
server.listen(Number(process.env.PORT || 0), '127.0.0.1', () => {
  console.log(JSON.stringify({ listening: server.address().port }));
});

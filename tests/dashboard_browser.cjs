// Optional: exercises the actual browser, including a chunked HTTP event stream.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { once } = require('node:events');
const { setTimeout: delay } = require('node:timers/promises');

const assets = path.join(__dirname, '../runtime/dashboard_web');
const types = {
  'index.html': 'text/html', 'style.css': 'text/css',
  'app.js': 'application/javascript', 'access.js': 'application/javascript',
};

async function main(modulePath, executablePath) {
  if (!modulePath || !executablePath) {
    throw new Error('Usage: node tests/dashboard_browser.cjs <playwright module path> <Chrome executable>');
  }
  const now = Math.floor(Date.now() / 1000);
  const tasks = Array.from({ length: 24 }, (_, i) => ({
    id: (i + 1).toString(16).padStart(32, '0'), key: 'raw_task_' + i,
    title: '检查订单接口 ' + (i + 1), display_title: '检查订单接口 ' + (i + 1),
    display_id: i ? '任务' + (i + 1) : '任务一', model: 'worker-test', role: '开发',
    tone: 'done', phase: 'accepted', status: '已通过', updated: now,
    run_started_at: now - 61, run_finished_at: now - 10,
  }));
  const task = tasks[0];
  const project = {
    id: 'f'.repeat(32), title: '订单项目', kind: 'project', phase: 'planned', status: '已规划', tone: 'wait',
    owner: { id: 'original-owner', label: 'Claude Code', model: 'supervisor-test' }, tasks, updated: now,
  };
  const snapshot = () => JSON.stringify({ projects: [project], warnings: [] });
  const frame = () => Buffer.from('event: snapshot\ndata: ' + snapshot() + '\n\n');
  const key = 'a'.repeat(64);
  const clients = new Set();
  let connects = 0;
  const server = http.createServer(async (request, response) => {
    if (request.url.startsWith('/api/') && request.headers.authorization !== 'Bearer ' + key) {
      response.writeHead(401).end();
      return;
    }
    if (request.url === '/api/live') {
      response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
      clients.add(response);
      connects++;
      response.write(frame());
      response.on('close', () => clients.delete(response));
      return;
    }
    if (request.url === '/api/overview') {
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(snapshot());
      return;
    }
    if (request.url.startsWith('/api/tasks/')) {
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({
        id: request.url.split('/').at(-1), events: [], callback: '等待回传', checks: '检查通过',
      }));
      return;
    }
    const file = request.url === '/' ? 'index.html' : request.url.slice(1);
    if (!Object.hasOwn(types, file)) return response.writeHead(404).end();
    response.writeHead(200, { 'Content-Type': types[file] }).end(await fs.readFile(path.join(assets, file)));
  });
  const heartbeat = setInterval(() => {
    for (const client of clients) client.write(': keepalive\n\n');
  }, 3000);
  async function publish() {
    assert.equal(clients.size, 1, 'one live subscription per visible dashboard');
    const bytes = frame();
    // Split one Chinese character across chunks, just as a socket can.
    const cut = bytes.indexOf(Buffer.from('订')) + 1;
    for (const client of clients) {
      client.write(bytes.subarray(0, cut));
      await delay(15);
      client.write(bytes.subarray(cut));
    }
  }
  let browser;
  try {
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const { chromium } = require(path.resolve(modulePath));
    browser = await chromium.launch({ executablePath, headless: true });
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 }, reducedMotion: 'no-preference' });
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto('http://127.0.0.1:' + server.address().port + '/#access=' + key, { waitUntil: 'domcontentloaded' });
    const cards = page.locator('#cards .card');
    await cards.first().waitFor();
    assert.equal(await cards.count(), tasks.length);
    assert.equal(await page.getByRole('button', { name: /预览.*动画/ }).count(), 0);
    assert.equal(await page.locator('#flows .moving').count(), 0);
    assert.equal(connects, 1);

    Object.assign(task, { phase: 'running', status: '执行中', tone: 'active', run_id: 'job-one',
      run_started_at: now - 2, run_finished_at: null });
    Object.assign(project, { phase: 'running', status: '执行中', tone: 'active' });
    await publish();
    const flow = page.locator('#flows .flow-row.moving');
    const card = page.locator('#cards .live-card');
    await flow.waitFor();
    const cardText = await card.innerText();
    for (const label of ['大总管', '执行者', 'supervisor-test', 'worker-test', '任务一']) assert.ok(cardText.includes(label), label);
    assert.ok(!cardText.includes(task.key));
    assert.ok(await flow.evaluate(n => n.getAnimations({ subtree: true }).length > 0));
    const time = await flow.locator('.clock strong').innerText();
    assert.match(time, /^\d+:\d{2}$/);
    await page.waitForFunction(old => document.querySelector('.moving .clock strong').textContent !== old, time);

    Object.assign(task, { activity_id: 'fresh-tool', activity_kind: 'tool', activity_at: Date.now() / 1000,
      activity: '读取订单文件' });
    await publish();
    const pulse = flow.locator('[data-activity-pulse="fresh-tool"]');
    await pulse.waitFor({ state: 'attached' });
    await pulse.waitFor({ state: 'detached' });
    await publish();
    assert.equal(await pulse.count(), 0, 'a repeated snapshot must not replay the event');

    // The reconnect snapshot contains an action that happened while disconnected.
    // It should restore current work without presenting that old action as new.
    for (const client of clients) client.end();
    Object.assign(task, { activity_id: 'missed-command', activity_kind: 'command', activity: '命令已完成' });
    await page.waitForFunction(text => document.querySelector('#flows').innerText.includes(text), task.activity);
    assert.ok(connects >= 2);
    assert.equal(await flow.locator('[data-activity-pulse="missed-command"]').count(), 0);
    await card.locator('.card-open').click();
    await page.locator('dialog[open]').waitFor();
    assert.ok((await page.locator('dialog').innerText()).includes('任务一'));
    await page.keyboard.press('Escape');

    Object.assign(task, { phase: 'awaiting_review', status: '待审核', tone: 'wait', run_finished_at: Date.now() / 1000 });
    await publish();
    await flow.waitFor({ state: 'detached' });
    assert.equal(await card.count(), 0);
    assert.equal(await cards.count(), tasks.length);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.deepEqual(errors, []);
    console.log('Live dashboard browser checks passed.');
  } finally {
    clearInterval(heartbeat);
    for (const client of clients) client.destroy();
    if (browser) await browser.close();
    server.close();
  }
}
main(...process.argv.slice(2)).catch(error => { console.error(error); process.exitCode = 1; });

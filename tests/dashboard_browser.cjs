// Optional real-browser regression. Uses an existing Playwright module and Chrome binary.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function main(modulePath, executablePath) {
  if (!modulePath || !executablePath) {
    throw new Error('Usage: node tests/dashboard_browser.cjs <playwright module path> <Chrome executable>');
  }
  const { chromium } = require(path.resolve(modulePath));
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 }, reducedMotion: 'no-preference' });
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(String(error)));
    const now = Math.floor(Date.now() / 1000);
    const tasks = Array.from({ length: 24 }, (_, i) => ({
      id: (i + 1).toString(16).padStart(32, '0'), key: 'raw_task_' + i,
      title: '检查订单接口 ' + (i + 1), display_title: '检查订单接口 ' + (i + 1),
      display_id: i === 0 ? '任务一' : '任务' + (i + 1), model: 'worker-test', role: '开发',
      tone: 'done', phase: 'accepted', status: '已通过', updated: now,
      run_started_at: now - 61, run_finished_at: now - 10,
    }));
    const first = tasks[0];
    Object.assign(first, { phase: 'running', status: '执行中', tone: 'active', run_finished_at: null });
    const project = {
      id: 'f'.repeat(32), title: '订单项目', kind: 'project', phase: 'running', status: '执行中', tone: 'active',
      owner: { id: 'original-owner', label: 'Claude Code', model: 'supervisor-test' }, tasks, updated: now,
    };
    const assets = path.join(__dirname, '../runtime/dashboard_web');
    await page.route('http://dashboard.test/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/api/overview') return route.fulfill({ json: { projects: [project], warnings: [] } });
      if (pathname.startsWith('/api/tasks/')) return route.fulfill({ json: {
        id: pathname.split('/').at(-1), events: [], callback: '等待回传', checks: '检查通过',
      } });
      const file = pathname === '/' ? 'index.html' : pathname.slice(1);
      const types = { 'index.html': 'text/html', 'style.css': 'text/css', 'app.js': 'application/javascript', 'access.js': 'application/javascript' };
      if (!Object.hasOwn(types, file)) return route.fulfill({ status: 404 });
      return route.fulfill({ contentType: types[file], body: await fs.readFile(path.join(assets, file)) });
    });
    await page.goto('http://dashboard.test/');
    await page.waitForSelector('.live-card');
    const cards = page.locator('#cards .card');
    assert.equal(await cards.count(), tasks.length, 'all tasks should be visible without expanding a project');
    const card = cards.filter({ hasText: '执行中' });
    const cardText = await card.innerText();
    for (const label of ['大总管', '执行者', 'supervisor-test', 'worker-test', '任务一']) assert.ok(cardText.includes(label), label);
    assert.ok(!cardText.includes(first.key));
    assert.ok(await page.locator('#flows .moving').evaluate(n => n.getAnimations({ subtree: true }).length > 0));
    const current = await page.locator('#flows .moving .clock strong').innerText();
    assert.match(current, /^\d+:\d{2}$/);
    await page.waitForFunction(value => document.querySelector('#flows .moving .clock strong').textContent !== value, current);
    await card.locator('.card-open').click();
    await page.locator('dialog[open]').waitFor();
    assert.ok((await page.locator('dialog').innerText()).includes('任务一'));
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('dialog[open]').count(), 0);

    Object.assign(first, { phase: 'awaiting_review', tone: 'wait', status: '待审核', run_finished_at: now });
    await page.locator('#refresh').click();
    await page.locator('#flows .moving').waitFor({ state: 'detached' });
    assert.equal(await cards.count(), tasks.length);
    await page.getByRole('button', { name: '预览运行动画' }).click();
    await page.locator('#flows .moving').waitFor();
    assert.ok((await page.locator('#flow-state').innerText()).includes('非实时'));
    assert.equal(await page.locator('#cards .live-card').count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.deepEqual(pageErrors, []);
    console.log('Dashboard browser checks passed.');
  } finally {
    await browser.close();
  }
}
main(...process.argv.slice(2)).catch(error => { console.error(error); process.exitCode = 1; });

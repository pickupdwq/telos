// Fixed browser adapter. Requires an existing Playwright; no package installation.
'use strict';
const fs = require('node:fs');
const path = require('node:path');

function allowed(policy, raw, method) {
  try {
    const u = new URL(raw);
    if (u.username || u.password || !['http:', 'https:'].includes(u.protocol)) return false;
    const decoded = decodeURIComponent(u.pathname);
    if (decodeURIComponent(decoded) !== decoded || decoded.includes('\\') || decoded.split('/').some(x => x === '.' || x === '..')) return false;
    return policy.targets.some(t => u.origin === t.origin && t.methods.includes(method) &&
      (t.path_prefix === '/' || decoded === t.path_prefix || decoded.startsWith(t.path_prefix + '/')));
  } catch { return false; }
}

async function main(input) {
  const { policy, request, folder } = input;
  const { chromium } = require(policy.browser.module_path);
  const viewport = policy.browser.viewports.find(v => v.name === request.viewport);
  const browser = await chromium.launch({ executablePath: policy.browser.executable_path, headless: true,
    downloadsPath: folder, args: ['--disable-background-networking', '--disable-extensions', '--no-proxy-server',
      '--disable-crashpad-for-testing', `--crash-dumps-dir=${folder}`] });
  const result = { outcome: 'passed', url: request.url, viewport, browser: browser.version(),
    steps: [], console: [], requests: [], blocked_requests: [], screenshots: [] };
  try {
    const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height },
      serviceWorkers: 'block', acceptDownloads: false });
    if (typeof context.routeWebSocket !== 'function') throw new Error('Playwright 1.48+ is required to block WebSockets');
    await context.routeWebSocket('**/*', socket => socket.close());
    await context.route('**/*', async route => {
      const r = route.request();
      if (!allowed(policy, r.url(), r.method())) {
        if (result.blocked_requests.length < 300) result.blocked_requests.push({ url: r.url(), method: r.method() });
        return route.abort('blockedbyclient');
      }
      // Playwright continue() can follow a redirect without re-running a route
      // handler. Fetch exactly once, reject redirects, then fulfill the page.
      try {
        const response = await route.fetch({ maxRedirects: 0, maxRetries: 0,
          timeout: Math.min(15000, policy.limits.probe_timeout_seconds * 1000) });
        if (response.status() >= 300 && response.status() < 400) {
          result.blocked_requests.push({ url: r.url(), method: r.method(), reason: 'redirect' });
          return route.fulfill({ status: 403, body: 'Redirect blocked by audit boundary' });
        }
        return route.fulfill({ response });
      } catch (error) {
        if (result.blocked_requests.length < 300) result.blocked_requests.push({ url: r.url(), method: r.method(), reason: String(error).slice(0, 300) });
        return route.abort('failed').catch(() => {});
      }
    });
    const page = await context.newPage();
    page.setDefaultTimeout(Math.min(8000, policy.limits.probe_timeout_seconds * 1000));
    page.on('console', message => { if (result.console.length < 100) result.console.push({ type: message.type(), text: message.text().slice(0, 2000) }); });
    page.on('pageerror', error => { if (result.console.length < 100) result.console.push({ type: 'pageerror', text: String(error).slice(0, 2000) }); });
    page.on('response', response => { if (result.requests.length < 300) result.requests.push({ url: response.url(), status: response.status() }); });
    const capture = async number => {
      const name = `step-${String(number).padStart(2, '0')}.png`;
      await page.screenshot({ path: path.join(folder, name), fullPage: false, animations: 'disabled' });
      result.screenshots.push(name);
    };
    try {
      await page.goto(request.url, { waitUntil: 'domcontentloaded' });
      await capture(0);
      for (let i = 0; i < request.steps.length; i++) {
        const step = request.steps[i];
        const locator = step.selector ? page.locator(step.selector) : null;
        if (step.action === 'click') await locator.click();
        else if (step.action === 'fill') await locator.fill(step.value);
        else if (step.action === 'select') await locator.selectOption(step.value);
        else if (step.action === 'press') await locator.press(step.value);
        else if (step.action === 'assert_visible') await locator.waitFor({ state: 'visible' });
        else if (step.action === 'assert_text') {
          await locator.waitFor({ state: 'visible' });
          const deadline = Date.now() + Math.min(8000, policy.limits.probe_timeout_seconds * 1000);
          while (!(await locator.innerText()).includes(step.value)) {
            if (Date.now() >= deadline) throw new Error('Expected text is absent');
            await new Promise(resolve => setTimeout(resolve, 100));
          }
        } else if (step.action === 'assert_url') {
          await page.waitForURL(step.value);
        } else if (step.action !== 'screenshot') throw new Error('Unsupported browser action');
        // Do not persist fill values (which can contain test-account passwords).
        result.steps.push({ action: step.action, selector: step.selector, status: 'passed' });
        await capture(i + 1);
      }
    } catch (error) {
      result.outcome = 'failed'; result.error = String(error).slice(0, 2000);
      await capture(request.steps.length + 1).catch(() => {});
    }
    result.final_url = page.url();
    result.title = await page.title().catch(() => '');
    result.visible_text = (await page.locator('body').innerText().catch(() => '')).slice(0, 16000);
    result.layout = await page.evaluate(() => ({
      viewport_width: innerWidth, document_width: document.documentElement.scrollWidth,
      horizontal_overflow: document.documentElement.scrollWidth > innerWidth,
      overflowing_elements: Array.from(document.querySelectorAll('body *')).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && (r.right > innerWidth + 1 || r.left < -1);
      }).slice(0, 30).map(el => ({ tag: el.tagName, id: el.id, class: String(el.className).slice(0, 150) }))
    })).catch(() => null);
    if (result.blocked_requests.length) result.boundary_note = 'Some requests were blocked by the frozen scope; assess coverage before acceptance.';
    return result;
  } finally { await browser.close(); }
}

let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { data += chunk; });
process.stdin.on('end', () => main(JSON.parse(data)).then(result => process.stdout.write(JSON.stringify(result)))
  .catch(error => { process.stderr.write(String(error)); process.exitCode = 1; }));

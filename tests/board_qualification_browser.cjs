/* Optional real-browser qualification; the unit suite uses the shipped Node harness.
 * Generate fixtures with board_qualification_fixtures.py, then run this script
 * with Playwright on NODE_PATH. Only synthetic metadata is served on loopback.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const directory = process.argv[2];
  assert(directory, 'fixture/output directory required');
  const html = fs.readFileSync(path.join(directory, 'board.html'));
  const fixture = JSON.parse(fs.readFileSync(path.join(directory, 'cases.json')));
  const server = http.createServer((req, res) => {
    res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
    res.end(html);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  let browser;
  const checks = [];
  try {
    browser = await chromium.launch({headless: true,
      ...(process.env.BOARD_BROWSER_CHANNEL ? {channel: process.env.BOARD_BROWSER_CHANNEL} : {})});
    for (const viewport of [{width: 1440, height: 1000}, {width: 390, height: 844}]) {
      const context = await browser.newContext({viewport});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', err => errors.push(err.message));
      page.on('console', msg => {if (['error', 'warning'].includes(msg.type())) errors.push(msg.text());});
      await page.addInitScript(now => {Date.now = () => now;}, Date.parse(fixture.now));
      let current = fixture.cases.fresh_without_pr;
      await page.route('**/api/status', route => route.fulfill({json: current}));
      await page.route('**/api/events', route => route.fulfill({json: {events: []}}));
      await page.goto(url);
      assert.match(await page.title(), /Code Mower Board/);
      assert.equal(new URL(page.url()).hostname, '127.0.0.1');
      for (const [name, snapshot] of Object.entries(fixture.cases)) {
        current = snapshot;
        const start = performance.now();
        await page.reload();
        const row = page.locator('.rowbtn').first();
        await row.waitFor({state: 'visible'});
        const text = await row.innerText();
        if (name === 'fresh_without_pr') {
          await page.screenshot({path: path.join(directory, `board-${viewport.width}.png`)});
        }
        for (const field of ['next:', 'responsible:', 'sources:']) assert(text.includes(field), `${name}: ${field}`);
        assert(!text.includes('PRIVATE_SESSION_PROVIDER_CONTEXT_SLACK_GRAPHIFY_SOURCE_PATH'));
        const width = await page.evaluate(() => ({page: document.documentElement.scrollWidth, viewport: innerWidth}));
        assert(width.page <= width.viewport, `${name}: horizontal clipping at ${viewport.width}`);
        assert(performance.now() - start < 10000, `${name}: first meaningful row exceeded 10s`);
        checks.push({case: name, width: viewport.width, status: 'pass'});
        if (name === 'fresh_without_pr') {
          assert(text.includes('observe implementation progress'));
          const bounds = await row.boundingBox();
          assert(bounds.y + bounds.height <= viewport.height, 'primary work facts require scrolling');
          await page.getByRole('tab', {name: /Health/}).click();
          await page.locator('#sources').waitFor({state: 'visible'});
          assert((await page.locator('#sources').innerText()).includes('local_runner'));
          await page.getByRole('tab', {name: 'Now', exact: true}).click();
          await row.waitFor({state: 'visible'});
        }
      }
      assert.deepEqual(errors, [], 'browser console/runtime errors');
      await context.close();
    }
    const scorecard = {
      schema: 'code_mower.boardQualificationBrowser.v1',
      head_sha: /^[a-f0-9]{40}$/.test(process.env.QUALIFICATION_HEAD_SHA || '') ? process.env.QUALIFICATION_HEAD_SHA : null,
      status: 'pass', checks,
      coverage: {observed: checks.length, total: Object.keys(fixture.cases).length * 2},
      discovery: {basis: 'automated_visible_render', human_seconds: null, human_coverage: 'unavailable'},
      hosted_execution: {state: 'not_run', coverage: 'unavailable'},
    };
    fs.writeFileSync(path.join(directory, 'browser-scorecard.json'), JSON.stringify(scorecard, null, 2) + '\n');
    console.log(JSON.stringify({status: scorecard.status, coverage: scorecard.coverage}));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});

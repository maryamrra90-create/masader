// Records a captioned walkthrough of the running Masader stack (simulated model mode, synthetic corpus).
// Usage: start api, worker and beat (README "Run"), save an admin and a reviewer key from `masader key create` into
// k_admin.txt and k_rev.txt, then: DEMO_DIR=<that folder> node perf/demo/record_demo.js  (needs Node Playwright).
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const S = process.env.DEMO_DIR || process.cwd();   // folder with k_admin.txt, k_rev.txt and repo/ (see header)
const BASE = 'http://127.0.0.1:8000';
const KEY = fs.readFileSync(`${S}/k_admin.txt`, 'utf8').match(/[a-z]+_[A-Za-z0-9_\-]{12,}/)[0];
const REV = fs.readFileSync(`${S}/k_rev.txt`, 'utf8').match(/[a-z]+_[A-Za-z0-9_\-]{12,}/)[0];
const ARCH = 'data:image/png;base64,' + fs.readFileSync(`${S}/repo/docs/architecture.png`).toString('base64');

const css = `body{margin:0;font-family:Carlito,Calibri,Arial,sans-serif;background:#0f2742;color:#fff;display:flex;
 align-items:center;justify-content:center;height:100vh} .c{max-width:1000px;padding:40px} h1{font-size:54px;margin:0 0 12px}
 h2{font-size:30px;font-weight:400;margin:0 0 28px;color:#cfe0f5} p{font-size:24px;line-height:1.45;color:#e8eef7}
 .tag{display:inline-block;background:#f5b041;color:#1b1b1b;padding:6px 14px;border-radius:6px;font-weight:700;font-size:20px}
 table{border-collapse:collapse;font-size:22px;width:100%} td,th{padding:9px 14px;border-bottom:1px solid #35557a;text-align:left}
 th{color:#9fc0e8;font-weight:400} .ok{color:#7ee2a8} .miss{color:#f5b041}`;
const card = (inner) => `<html><head><style>${css}</style></head><body><div class="c">${inner}</div></body></html>`;

async function caption(page, text) {
  await page.evaluate((t) => {
    let d = document.getElementById('__cap');
    if (!d) {
      d = document.createElement('div'); d.id = '__cap';
      d.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:22px;max-width:1120px;z-index:99999;' +
        'background:rgba(10,20,35,.88);color:#fff;font:22px/1.35 Carlito,Arial,sans-serif;padding:12px 20px;border-radius:8px;text-align:center';
      document.body.appendChild(d);
    }
    d.textContent = t;
  }, text);
}

async function api(method, url, key, body, extra) {
  const r = await fetch(BASE + url, { method, headers: Object.assign({ 'X-API-Key': key, 'Content-Type': 'application/json' }, extra || {}),
    body: body ? JSON.stringify(body) : undefined });
  return r.json();
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, recordVideo: { dir: `${S}/demo/raw`, size: { width: 1280, height: 720 } } });
  const page = await ctx.newPage();
  const wait = (ms) => page.waitForTimeout(ms);

  // 1. Title
  await page.setContent(card(`<h1>Masader</h1><h2>Evidence-labelled research briefs · implementation demo</h2>
    <p>Mariam Alshamsi · MAAI7102 Software Engineering and Programming · Fall 2026</p>
    <p><span class="tag">SIMULATED MODEL CALLS · SYNTHETIC 37-ITEM CORPUS</span></p>
    <p style="font-size:20px">No Anthropic API key was used for this recording. Every model role is answered by a labelled heuristic simulator;
    queues, leases, budgets, rules, validator, rendering and recovery run for real.</p>`));
  await wait(9000);

  // 2. Architecture
  await page.setContent(`<html><body style="margin:0;background:#fff;display:flex;justify-content:center;align-items:center;height:100vh">
    <img src="${ARCH}" style="max-width:100%;max-height:100%"></body></html>`);
  await caption(page, 'FastAPI service, eight Celery stages (collect → dedup → extract → verify → label → write → validate → render), PostgreSQL + pgvector, Redis. Dashed boxes are simulated.');
  await wait(10000);

  // 3. Request a brief in the UI
  await page.goto(BASE + '/');
  await caption(page, 'The minimal web form. An API key is required for every /v1 route; the request is pre-filled with the Strait of Hormuz example.');
  await wait(4000);
  await page.fill('#key', KEY);
  await wait(1200);
  await page.click('button[type=submit]');
  await page.waitForSelector('text=A few questions first');
  await caption(page, 'The planner checks the request first and asks at most three questions, each with a default.');
  await wait(5000);
  await page.click('text=Send answers');
  await caption(page, 'The report runs through eight stages; each stage takes a lease and commits fenced on it.');
  await page.waitForSelector('iframe', { timeout: 240000 });
  const reportId = (await page.textContent('#out')).match(/rep_[A-Za-z0-9]+/)[0];
  await wait(2500);
  await caption(page, 'Delivered. The brief opens in a sandboxed frame, with the simulated banner at the top.');
  const iframe = page.locator('iframe');
  await iframe.scrollIntoViewIfNeeded();
  await wait(4000);
  const box = await iframe.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  const steps = [
    'Each finding carries an evidence label and a one-line reason, and links to the passages it rests on.',
    'Corroborated needs at least two independent origins; disputed figures are given in both versions.',
    'A superseded figure is labelled insufficient and kept out of the key findings.',
    'Sources are listed with their reliability tier. The prompt-injection item in the corpus is never cited.',
  ];
  for (const s of steps) { await caption(page, s); for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, 140); await wait(450); } await wait(2500); }

  // 4. JSON record
  const rep = await api('GET', `/v1/reports/${reportId}`, KEY);
  const versions = JSON.stringify(rep.versions || {}, null, 1).replace(/</g, '&lt;');
  await page.setContent(card(`<h2>GET /v1/reports/${reportId}</h2>
    <table><tr><th>status</th><td>${rep.status}</td></tr><tr><th>model_mode</th><td>${rep.model_mode ?? 'simulated'}</td></tr>
    <tr><th>stages</th><td>${(rep.stages || []).map(s => s.name + ' ✓').join(' · ')}</td></tr></table>
    <p style="font-size:18px;font-family:DejaVu Sans Mono,monospace;white-space:pre-wrap;max-height:300px;overflow:hidden">${versions}</p>`));
  await caption(page, 'The JSON record names every model and prompt version. In simulated mode the model names start with "sim:".');
  await wait(9000);

  // 5. Publish and the public page
  const pub = await api('POST', `/v1/reports/${reportId}/publish`, REV);
  await page.setContent(card(`<h2>POST /v1/reports/${reportId}/publish  (reviewer key)</h2>
    <p style="font-family:DejaVu Sans Mono,monospace;font-size:20px">${JSON.stringify(pub).replace(/</g, '&lt;').slice(0, 400)}</p>`));
  await caption(page, 'Reports stay private until a reviewer publishes them. Corrections later create version n+1.');
  await wait(6000);
  await page.goto(`${BASE}/r/${reportId}`);
  await caption(page, `The published brief is now public at /r/${reportId}.`);
  await wait(3000);
  for (let i = 0; i < 5; i++) { await page.mouse.wheel(0, 200); await wait(500); }
  await wait(3000);

  // 6. Results
  await page.setContent(card(`<h2>Measured results (eval/results/, commit 3381183)</h2><table>
    <tr><th>Measure</th><th>Target</th><th>Result</th></tr>
    <tr><td>Automated tests</td><td>all pass</td><td class="ok">139 of 139 · 94% coverage</td></tr>
    <tr><td>Evidence rules (A15)</td><td>20/20</td><td class="ok">20/20</td></tr>
    <tr><td>Security scans (bandit, pip-audit)</td><td>no high findings</td><td class="ok">0 after one fix</td></tr>
    <tr><td>Read latency p95, 50 users</td><td>&lt; 2 s</td><td class="ok">11 ms</td></tr>
    <tr><td>Report time, admitted reports</td><td>p95 &lt; 10 min</td><td class="ok">340–373 s (simulated latency)</td></tr>
    <tr><td>Failure drills (worker kill, DB/Redis down, …)</td><td>recover</td><td class="ok">12 of 12</td></tr>
    <tr><td>Seeded errors caught by validator</td><td>≥ 95%</td><td class="miss">73% — not met (simulated support check)</td></tr>
    <tr><td>Cross-language duplicate recall</td><td>—</td><td class="miss">0.20 (offline embedder)</td></tr></table>`));
  await wait(14000);

  // 7. Close
  await page.setContent(card(`<h1 style="font-size:44px">Next: real model calls</h1>
    <p>Run the same evaluation scripts with MODEL_MODE=live, add the multilingual embedder, push to GitHub so CI runs, and deploy the Compose stack.</p>
    <p>Every deviation is listed in docs/deviations.md; every requirement links to code, tests and results in docs/traceability.md.</p>
    <p class="tag">Thank you</p>`));
  await wait(7000);

  const vid = page.video();
  await ctx.close(); await browser.close();
  const p = await vid.path();
  fs.writeFileSync(`${S}/demo/raw_path.txt`, p);
  console.log('video', p, 'report', reportId);
})().catch((e) => { console.error(e); process.exit(1); });

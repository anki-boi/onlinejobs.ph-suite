/**
 * tests/js/ui.test.mjs — frontend smoke tests (audit F15 / decision D3).
 *
 * The Python suite is green while the table lies, because none of these bugs are
 * reachable from Python: a missing import, a truthy "0", a count that reads the DOM.
 * This harness loads static/index.html into jsdom once, stubs fetch, and drives the
 * real ES modules. `node --test` is the runner; jsdom is the only dependency.
 *
 * Run: npm run test:js   (also wired into tools/gate.sh + CI)
 */
import test, { before, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const fileUrl = (p) => new URL('file:///' + join(ROOT, p).replace(/\\/g, '/')).href;

const PAGE = {
  items: [
    { id: 100, title: 'Reposted job', company: 'Reposted job', work_type: 'Full Time',
      posted_date: '2026-09-01 08:00:00', salary: '$800/mo', salary_monthly_min: 47000,
      salary_monthly_max: 47000, salary_currency: 'USD', hours_per_week: '40',
      skills: 'Excel, Data Entry, Email, Notepad, Extra', status: 'New',
      scrape_status: 'Open', filter_hidden: 0, repost_of: 90,
      ats_fit: 45, ats_total: 80, ats_profile: 'master' },
    ...Array.from({ length: 49 }, (_, i) => ({
      id: 200 + i, title: `Job ${i}`, company: 'Company', work_type: 'Any',
      posted_date: '2026-09-02 08:00:00', salary: '', hours_per_week: '',
      skills: 'Excel', status: 'New', scrape_status: '', repost_of: null,
      ats_fit: null, ats_total: null, ats_profile: null,
    })),
  ],
  page: 1, per_page: 50, total: 1232, next_cursor: '2',
};
const ROUTES = {
  '/api/jobs': PAGE,
  '/api/stats': { New: 5, Applied: 1, Interviewing: 0, Hired: 0, total: 1232, follow_ups_due: 2 },
  '/api/config': { config: {}, features_off: [], fx_metadata: { usd: 58, at: '2026-09-29 00:00:00' } },
  '/api/keywords': { positive: [], negative: [], still_filter_hidden: 0 },
  '/api/scrape-scope': { keyword: '', categories: [], skills: [] },
  '/api/skills': [{ id: 1, name: 'Excel', slug: 'virtual-assistant', category_path: 'Virtual Assistant' }],
  '/api/skills/categories': [{ name: 'Virtual Assistant', slug: 'virtual-assistant', count: 12 }],
  '/api/schedule': { enabled: false, interval_hours: 4, last_run: '', last_error: '',
                     last_status: '', next_run: '', running: false, instance_lock: null },
  '/api/resume': { basics: { name: 'Test', email: 'a@b.c', phone: '+63 900 000 0000',
                             location: 'PH', summary: 'x' }, skills: ['Excel'], work: [], education: [] },
  '/api/resume/profiles': { default: 'master', profiles: ['master'] },
  '/api/resume/built': { items: [] },
  '/api/resume/yamlcv-status': { available: false },
  '/api/resume/ats': { total: 61, profile: 'master',
                       breakdown: { skills: 30, keywords: 15, format: 10, completeness: 6 },
                       matched_skills: ['Excel'], missing_skills: ['Figma'], suggestions: [] },
  '/api/runs': { items: [{ started: '2026-09-29 03:00:00', kind: 'auto', status: 'completed',
                           inserted: 12, closed: 3, errors: 0, error: '' }] },
};

let fetched = [];
let M = null;                       // the loaded modules

// The route table every test fetches against. afterEach re-installs it because
// F16 swaps in a streaming stub that has no json().
function routeFetch(url) {
  const path = String(url).split('?')[0];
  fetched.push(String(url));
  const body = ROUTES[path]
    ?? (/^\/api\/jobs\/\d+$/.test(path)
          ? { ...PAGE.items.find(i => i.id === +path.split('/').pop()) ?? PAGE.items[0], history: [] }
          : { ok: true });
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
}

before(async () => {
  globalThis.fetch = routeFetch;
  // Record every EventSource so a test can fire a server event by hand (F18).
  globalThis.__ES = [];
  globalThis.EventSource = class {
    constructor(url) { this.url = url; this.handlers = {}; globalThis.__ES.push(this); }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() {}
  };
  const Notif = class {
    static permission = 'granted';
    static requestPermission() { return Promise.resolve('granted'); }
  };
  globalThis.Notification = Notif;
  const dom = new JSDOM(readFileSync(join(ROOT, 'static', 'index.html'), 'utf8'),
                        { url: 'http://127.0.0.1:8371/', pretendToBeVisual: true });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.localStorage = dom.window.localStorage;
  dom.window.Notification = Notif;      // app code checks `'Notification' in window`
  dom.window.EventSource = globalThis.EventSource;

  M = {
    core: await import(fileUrl('static/js/core.js')),
    jobs: await import(fileUrl('static/js/jobs.js')),
    filters: await import(fileUrl('static/js/filters.js')),
    resume: await import(fileUrl('static/js/resume.js')),
    run: await import(fileUrl('static/js/run.js')),
  };
  await import(fileUrl('static/app.js'));
  document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await tick();
});

afterEach(() => {
  globalThis.fetch = routeFetch;
  const s = M.core.state;
  s.search = ''; s.status = ''; s.skills = []; s.categories = [];
  s.includeHidden = false; s.hideReposts = false; s.minAts = false; s.minAtsValue = 20;
  s.colFilters = { status: [], work_type: [], scrape: [], title: '', company: '',
                   posted: { from: '', to: '' }, salary: '', hours: '' };
  s.hasSalaryOnly = false; s.sort = ''; s.order = 'desc';
  document.querySelector('#filter-salary-min').value = '';
  document.querySelector('#filter-salary-cur').value = '';
  fetched = [];
});

const tick = () => new Promise(r => setTimeout(r, 20));
const lastJobsUrl = () => [...fetched].reverse().find(u => u.startsWith('/api/jobs?')) || '';
const rows = () => [...document.querySelectorAll('#jobs-tbody tr[data-id]')];
const visible = () => rows().filter(r => r.style.display !== 'none');

test('F1 — the filter banner "clear" button works (it used to throw ReferenceError)', async () => {
  M.core.state.skills = ['Excel'];
  M.core.state.categories = ['virtual-assistant'];
  M.jobs.applyVisibleFilter();
  const clear = document.querySelector('#filter-clear-btn');
  assert.ok(clear, 'banner with a clear button is rendered');
  clear.click();                                  // used to throw: renderSkills/renderCats unimported
  assert.deepEqual(M.core.state.skills, [], 'skills cleared');
  assert.deepEqual(M.core.state.categories, [], 'categories cleared');
  await tick();
  assert.ok(lastJobsUrl(), 'and the table refetched');
});

test('F2 — "Hide reposts" hides only reposts, not every row', async () => {
  await M.jobs.loadJobs();
  assert.equal(visible().length, 50, 'all rows visible by default');
  M.core.state.hideReposts = true;
  M.jobs.applyVisibleFilter();
  const hidden = rows().filter(r => r.style.display === 'none');
  assert.equal(hidden.length, 1, 'exactly the one repost row is hidden');
  assert.equal(hidden[0]?.dataset.repost, '90', 'the hidden row is the repost');
});

test('F3 — the toolbar count is the global total, not the rows on the page', async () => {
  await M.jobs.loadJobs();
  assert.equal(document.querySelector('#result-count').textContent.trim(), '1,232 jobs');
});

test('F4 — changing a filter resets to page 1 instead of stranding page 5', async () => {
  M.core.state.page = 5;
  fetched = [];
  M.core.state.search = 'excel';
  await M.jobs.loadJobs();
  assert.match(lastJobsUrl(), /[?&]search=excel/, 'the search went out');
  assert.match(lastJobsUrl(), /[?&]page=1(&|$)/, 'and it asked for page 1');
});

test('F5 — selected categories reach the server', async () => {
  M.core.state.categories = ['virtual-assistant'];
  await M.jobs.loadJobs();
  assert.match(lastJobsUrl(), /[?&]categories=virtual-assistant/);
});

test('F6 — a job matching on its 5th skill tag is not hidden by the client', async () => {
  M.core.state.skills = ['Extra'];                 // 5th tag — never rendered in the row
  await M.jobs.loadJobs();
  assert.equal(visible().length, 50, 'every server-returned row stays visible');
});

test('F7 — the dead virtual scroll is gone and all rows render', async () => {
  await M.jobs.loadJobs();
  assert.equal(M.jobs.virtualRender, undefined, 'virtualRender is no longer exported');
  assert.equal(rows().length, 50);
  assert.equal(document.querySelector('tr.vs-spacer'), null, 'no spacer rows');
});

test('F8 — the drawer shows the job\'s current status', async () => {
  await M.resume.openDetail(100);
  assert.equal(document.querySelector('#detail-status-select').value, 'New',
    'the status select reflects the job');
});

test('F9 — a salary minimum of 0 is still sent', async () => {
  document.querySelector('#filter-salary-min').value = '0';
  await M.jobs.loadJobs();
  assert.match(lastJobsUrl(), /[?&]salary_min_monthly=0(&|$)/);
});

test('F11 — the alerts button reflects a permission the browser already granted', async () => {
  await M.run.syncAutoRunUI();
  assert.match(document.querySelector('#btn-notify').textContent, /on/, 'button says alerts are on');
});

test('F17 — toast types map to classes that exist in the stylesheet', async () => {
  M.core.toast('hi', 'ok');
  assert.match(document.querySelector('#toast').className, /toast-success/);
  M.core.toast('bad', 'err');
  assert.match(document.querySelector('#toast').className, /toast-error/);
});

test('F16 — a stream that ends without a done event still re-enables the buttons', async () => {
  M.run.setScraping(true);
  assert.equal(document.querySelector('#btn-harvest').disabled, true);
  // streamSSE's finally block must clear the flag even when no `done` arrives
  globalThis.fetch = async (url) => ({
    ok: true,
    body: { getReader: () => ({ read: async () => ({ done: true }), cancel: async () => {} }) },
  });
  await M.run.streamSSE('/api/pipeline/run', {});
  assert.equal(document.querySelector('#btn-harvest').disabled, false);
});
test('P1 — the fit slider sends min_fit, not a hard-coded ATS of 50', async () => {
  const box = document.querySelector('#filter-min-ats');
  const slider = document.querySelector('#filter-min-fit');
  assert.ok(box && slider, 'the ATS checkbox kept its slider');
  box.checked = true;
  box.dispatchEvent(new window.Event('change', { bubbles: true }));
  slider.value = '40';
  slider.dispatchEvent(new window.Event('input', { bubbles: true }));
  await M.jobs.loadJobs();
  assert.match(lastJobsUrl(), /[?&]min_fit=40(&|$)/, 'the floor travels as fit/60');
  assert.doesNotMatch(lastJobsUrl(), /min_ats=/, 'the 100-point total is no longer the filter');
  assert.equal(document.querySelector('#fit-value').textContent, '40', 'the number is echoed');
});

test('P1 — the table shows the score it filters on', async () => {
  await M.jobs.loadJobs();
  const scored = rows()[0].querySelector('.fit-chip');
  assert.ok(scored, 'a scored row renders its fit');
  assert.equal(scored.textContent.trim(), '45');
  assert.match(scored.title, /total 80\/100/, 'the tooltip carries the total it hides');
  const unscored = rows().find(r => r.dataset.id === '200').querySelector('.fit-chip, .text-muted');
  assert.equal(unscored.textContent.trim(), '—', 'an unscored job says so instead of 0');
});

test('P1 — sorting by Fit asks the server, not the page', async () => {
  document.querySelector('th[data-col="ats"]').click();
  await M.jobs.loadJobs();
  assert.match(lastJobsUrl(), /[?&]sort=ats(&|$)/);
});

test('P2 — the resume editor covers work[] and education[]', async () => {
  M.resume.renderWorkEditor([{ role: 'Ops VA', company: 'Acme', start: '2021',
                               end: '2024', bullets: ['Ran ledgers'] }]);
  M.resume.renderEduEditor([{ school: 'UP', degree: 'BS', year: '2020' }]);
  const work = M.resume.readWorkEditor();
  assert.equal(work.length, 1);
  assert.deepEqual(work[0], { role: 'Ops VA', company: 'Acme', start: '2021',
                              end: '2024', bullets: ['Ran ledgers'] });
  assert.deepEqual(M.resume.readEduEditor(),
                   [{ school: 'UP', degree: 'BS', year: '2020' }]);
});

test('P2 — add and remove buttons change what gets saved', async () => {
  M.resume.renderWorkEditor([]);
  M.resume.renderEduEditor([]);
  document.querySelector('#btn-add-work').click();
  document.querySelector('#btn-add-edu').click();
  assert.equal(document.querySelectorAll('#r-work .work-row').length, 1);
  assert.equal(document.querySelectorAll('#r-education .edu-row').length, 1);
  const box = document.querySelector('#r-work .w-role');
  box.value = 'New Role';
  document.querySelector('#r-work .w-del').click();
  assert.deepEqual(M.resume.readWorkEditor(), [], 'removed row is not saved');
  assert.deepEqual(M.resume.readEduEditor(),
                   [{ school: '', degree: '', year: '' }].slice(0, 0),
                   'an untouched education row saves nothing');
});

test('P2 — prefilling the form rebuilds both editors from the master', async () => {
  M.resume.prefillResumeForm({ basics: { name: 'J' }, skills: ['Excel'],
                               work: [{ role: 'A', company: 'B', start: '1', end: '2', bullets: [] }],
                               education: [{ school: 'S', degree: 'D', year: '3' }] }, 'master');
  assert.equal(document.querySelectorAll('#r-work .work-row').length, 1);
  assert.equal(document.querySelector('#r-work .w-role').value, 'A');
  assert.equal(document.querySelector('#r-education .e-school').value, 'S');
});

test('F12 — a request in flight looks like one, and a failure offers Retry', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  globalThis.fetch = async (url) => {
    fetched.push(String(url));
    await gate;
    return routeFetch(url);
  };
  const pending = M.jobs.loadJobs();
  assert.ok(document.querySelector('#jobs-tbody .loading-row'),
            'the table shows a loading state instead of the stale page');
  release();
  await pending;
  assert.ok(document.querySelector('#jobs-tbody tr[data-id]'), 'rows arrive');

  globalThis.fetch = async () => { throw new Error('boom'); };
  await M.jobs.loadJobs();
  const err = document.querySelector('#jobs-tbody .error-row');
  assert.ok(err, 'a failed request is an error row, not a raw string in the body');
  assert.match(err.textContent, /boom/);
  const retry = document.querySelector('#retry-jobs');
  assert.ok(retry, 'the error row carries a Retry button');
});

test('B11 — the CSV URL carries the filters on screen', async () => {
  // jsdom refuses navigation, so watch for the download anchor it clicks
  const hits = [];
  document.addEventListener('click', (e) => {
    if (e.target.tagName === 'A' && e.target.hasAttribute('download')) hits.push(e.target.href);
  });
  M.core.state.search = 'bookkeeper';
  M.core.state.hideReposts = true;
  M.core.state.minAts = true;
  M.core.state.minAtsValue = 35;
  document.querySelector('#btn-export').click();
  const url = hits.at(-1) || '';
  assert.match(url, /\/api\/jobs\/export\?.*search=bookkeeper/);
  assert.match(url, /hide_reposts=1/);
  assert.match(url, /min_fit=35/);
});

test('F14 — the funnel triggers are real buttons with names', () => {
  const funnels = [...document.querySelectorAll('.th-funnel')];
  assert.ok(funnels.length >= 5, 'every sortable column keeps its funnel');
  assert.ok(funnels.every(b => b.tagName === 'BUTTON'), 'spans cannot be tabbed to');
  assert.ok(funnels.every(b => b.getAttribute('aria-label')), 'each one says what it filters');
});

test('F14 — the drawer is a modal: focus goes in, and comes back out', async () => {
  await M.jobs.loadJobs();
  const row = rows().find(r => r.dataset.id === '100') || rows()[0];
  row.focus();                       // a keyboard user activates the row they're on
  await M.resume.openDetail(row.dataset.id);
  const panel = document.querySelector('#detail-panel');
  assert.equal(panel.getAttribute('aria-modal'), 'true');
  assert.equal(panel.getAttribute('role'), 'dialog');
  assert.equal(document.activeElement.id, 'detail-close', 'focus enters the dialog');
  M.resume.closeDetail();
  assert.equal(document.activeElement.dataset.id, row.dataset.id, 'focus returns to the row');
});

test('F14 — Escape closes the popup, not the drawer behind it', async () => {
  await M.resume.openDetail(100);
  const th = document.querySelector('th[data-col="status"]');
  M.jobs.openColFilter('status', th);
  assert.ok(M.jobs.colFilterOpen(), 'the filter popup is open');
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.notEqual(document.querySelector('#detail-panel').className.indexOf('open'), -1,
                  'the drawer survives one Escape');
});

test('F13 — the phone breakpoint exists and the sidebar collapses behind a button', () => {
  const css = readFileSync(join(ROOT, 'static', 'style.css'), 'utf8');
  assert.match(css, /@media\s*\(max-width:\s*900px\)/, 'there is a breakpoint now');
  assert.match(css, /100dvh/, 'dvh, not the mobile-viewport 100vh bug');
  assert.match(css, /position:\s*sticky;\s*left:\s*0/, 'the first column stays pinned while scrolling');
  document.querySelector('#btn-sidebar').click();
  assert.ok(document.querySelector('#sidebar').classList.contains('collapsed'));
  assert.equal(document.querySelector('#btn-sidebar').getAttribute('aria-expanded'), 'false');
  document.querySelector('#btn-sidebar').click();
  assert.equal(document.querySelector('#sidebar').classList.contains('collapsed'), false);
});

test('F10 — an empty activity log is not a 250px empty box', () => {
  const css = readFileSync(join(ROOT, 'static', 'style.css'), 'utf8');
  assert.match(css, /\.console:empty\s*\{\s*display:\s*none/, 'idle console collapses to a bar');
});

test('F18 — jobs_reset refreshes the table instead of leaving stale rows', async () => {
  const es = globalThis.__ES.at(-1);
  assert.ok(es.handlers.jobs_reset, 'the SSE event has a listener');
  fetched = [];
  es.handlers.jobs_reset({ data: JSON.stringify({ deleted_jobs: 1232 }) });
  await tick();
  assert.ok(fetched.some(u => u.startsWith('/api/jobs?')), 'the table reloaded');
  assert.match(document.querySelector('#toast').textContent, /cleared/i, 'and said so');
});

test('P8 — the sidebar answers "what happened last night?"', async () => {
  await M.run.loadRuns();
  const el = document.querySelector('#runs-list');
  assert.ok(el, 'the Recent runs panel exists');
  assert.match(el.textContent, /auto/, 'kind');
  assert.match(el.textContent, /\+12 new/, 'inserted count');
  assert.match(el.textContent, /completed/, 'status');
});

test('P6 — a Full reset is undoable, not a bonfire', async () => {
  globalThis.confirm = () => true;
  await M.filters.resetAll();
  const undo = document.querySelector('#btn-undo-reset');
  assert.ok(undo, 'the Undo button exists');
  assert.notEqual(undo.style.display, 'none', 'and appears after a reset');
  fetched = [];
  await M.filters.undoReset();
  assert.ok(fetched.includes('/api/jobs/reset/undo'), 'Undo posts to the undo endpoint');
  assert.equal(undo.style.display, 'none', 'then hides itself');
});

test('P5 — the drawer re-checks one job instead of running a whole-table Check', async () => {
  await M.resume.openDetail(100);
  const btn = document.querySelector('#detail-recheck-btn');
  assert.ok(btn, 'Re-check button is in the drawer');
  fetched = [];
  await M.resume.detailRecheckClick();
  assert.ok(fetched.includes('/api/jobs/100/recheck'), 'it posts for that job only');
});

test('B12 — the CV build reports each round instead of a silent spinner', async () => {
  const chunks = [
    'event: run_started\ndata: {"run":"resume/build","run_id":"b1"}\n\n',
    'event: log\ndata: "Rendering round 1…"\n\n',
    'event: done\ndata: {"ok":true,"rounds":1,"profile":"master","name":"x.pdf",'
    + '"url":"/api/resume/built/x.pdf","history":[{"round":1,"ok":true,"pages":1}]}\n\n',
  ];
  globalThis.fetch = () => ({
    ok: true, status: 200, json: async () => ({ items: [] }),
    body: { getReader: () => ({
      read: async () => chunks.length
        ? { done: false, value: new TextEncoder().encode(chunks.shift()) }
        : { done: true },
      cancel: async () => {},
    }) },
  });
  window.open = () => {};
  const btn = document.createElement('button');
  const status = document.createElement('div');
  await M.resume.buildCvClick(100, true, btn, status, null);
  assert.match(status.textContent, /Rendering round 1/, 'progress arrives live');
  assert.match(status.textContent, /Built in 1 round/, 'and the result lands');
  assert.equal(btn.disabled, false, 'the button comes back');
});

/* ── X-A: the salary chip may only claim what the listing supports ───────── */
const SAL = (over) => M.jobs.jobRowHtml({
  id: 900, title: 'VA', company: 'C', status: 'New', scrape_status: 'Open',
  posted_date: '2026-09-02 08:00:00', skills: 'Excel', repost_of: null,
  ats_fit: null, ats_total: null, ats_profile: null, ...over,
});

test('X-A — an hourly rate with no stated hours shows a rate, never a month', () => {
  const html = SAL({ salary: '$16/hour', salary_min: 16, salary_max: 16,
    salary_currency: 'USD', salary_unit: 'hour', salary_hours: null,
    salary_hours_basis: 'unstated', salary_rate_min: 928, salary_rate_max: 928,
    salary_monthly_min: null, salary_monthly_max: null,
    salary_assumed_currency: 0, salary_piece_rate: 0 });
  assert.match(html, /₱928\/hr/, 'the honest per-hour figure');
  assert.doesNotMatch(html, /\/mo/, 'no invented month');
});

test('X-A — a piece rate is shown per item, not per month', () => {
  const html = SAL({ salary: '$50-$150 per video', salary_min: 50, salary_max: 150,
    salary_currency: 'USD', salary_unit: 'month', salary_hours: 40,
    salary_hours_basis: 'full-time', salary_rate_min: 2900, salary_rate_max: 8700,
    salary_monthly_min: null, salary_monthly_max: null,
    salary_assumed_currency: 0, salary_piece_rate: 1 });
  assert.match(html, /₱2,900–8,700 each/, 'per video, in pesos');
  assert.doesNotMatch(html, /\/mo/, 'no month for pay-per-item');
});

test('X-A — a currency guessed from magnitude is marked as a guess', () => {
  const html = SAL({ salary: '42000', salary_min: 42000, salary_max: 42000,
    salary_currency: 'PHP', salary_unit: 'month', salary_hours: null,
    salary_hours_basis: 'unstated', salary_rate_min: null, salary_rate_max: null,
    salary_monthly_min: 42000, salary_monthly_max: 42000,
    salary_assumed_currency: 1, salary_piece_rate: 0 });
  assert.match(html, /≈₱42,000\/mo/, 'the ≈ says "we guessed the currency"');
});

test('X-A — a listing that states no money says so instead of showing nothing', () => {
  const html = SAL({ salary: 'Need to be discussed', salary_min: null, salary_max: null,
    salary_currency: null, salary_unit: null, salary_rate_min: null, salary_rate_max: null,
    salary_monthly_min: null, salary_monthly_max: null,
    salary_assumed_currency: 0, salary_piece_rate: 0 });
  assert.match(html, /no monthly figure/, 'an explicit absence beats a blank cell');
});

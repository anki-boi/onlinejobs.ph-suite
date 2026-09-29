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
      scrape_status: 'Open', filter_hidden: 0, repost_of: 90 },
    ...Array.from({ length: 49 }, (_, i) => ({
      id: 200 + i, title: `Job ${i}`, company: 'Company', work_type: 'Any',
      posted_date: '2026-09-02 08:00:00', salary: '', hours_per_week: '',
      skills: 'Excel', status: 'New', scrape_status: '', repost_of: null,
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
};

let fetched = [];
let M = null;                       // the loaded modules

before(async () => {
  globalThis.fetch = async (url) => {
    const path = String(url).split('?')[0];
    fetched.push(String(url));
    const body = ROUTES[path]
      ?? (/^\/api\/jobs\/\d+$/.test(path)
            ? { ...PAGE.items.find(i => i.id === +path.split('/').pop()) ?? PAGE.items[0], history: [] }
            : { ok: true });
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
  };
  globalThis.EventSource = class { addEventListener() {} close() {} };
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
  const s = M.core.state;
  s.search = ''; s.status = ''; s.skills = []; s.categories = [];
  s.includeHidden = false; s.hideReposts = false; s.minAts = false;
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
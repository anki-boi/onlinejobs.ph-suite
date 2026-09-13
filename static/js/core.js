/* W6.1: shared state, DOM refs, and utils */

/* Job Hunter — Frontend logic */

const state = {
  page: 1, perPage: 50,  // W6.2: paginated (server caps per_page at 500)
  status: '', search: '', workType: '',
  skills: [],          // selected skill checkboxes (OR scope)
  categories: [],      // selected category checkboxes (OR scope)
  keywords: [],        // search keywords
  posFilters: [],      // positive keyword filters
  negFilters: [],      // negative keyword filters
  includeHidden: false,
  hideReposts: false,
  minAts: false,
  total: 0,
  scraping: false,
  activeRun: null,        // 'harvest' | 'check' | null — drives button phase labels
  activeRunId: null,      // W2.8: run_id from run_started — the stop button targets it
  // Excel-style column controls
  sort: '', order: 'desc',
  hasSalaryOnly: false,   // "With salary" toggle (toolbar + Salary ▼ share this one state)
  stats: null,            // last /api/stats payload, feeds the next-step hint
  colFilters: { status: [], work_type: [], scrape: [],
                title: '', company: '', posted: {from:'', to:''}, salary: '', hours: '' },
};

// Known values for checkbox-style column filters
const COL_VALUES = {
  status: ['New','Interested','Applied','Interviewing','Offer','Hired','Rejected','Hidden'],
  work_type: ['Any','Gig','Part Time','Full Time'],
  scrape: ['Open','Closed'],
};
const SORT_DEFAULT_ORDER = { posted_date: 'desc', date_found: 'desc', date_updated: 'desc' };

const $ = (s) => document.querySelector(s);
const tbody = $('#jobs-tbody');
const consoleEl = $('#console');
const toastEl = $('#toast');
const detailPanel = $('#detail-panel');
const detailOverlay = $('#detail-overlay');
let currentJob = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: {'Content-Type':'application/json'}, ...opts });
  if (!res.ok) { const b = await res.json().catch(()=>({})); throw new Error(b.error?.message||res.statusText); }
  return res.json();
}
function toast(msg, type='info') {
  toastEl.textContent = msg; toastEl.className = `toast show toast-${type}`;
  setTimeout(() => toastEl.classList.remove('show'), 2500);
}
function log(msg, cls='') {
  const d = document.createElement('div'); d.className = `log-line ${cls}`; d.textContent = msg;
  consoleEl.appendChild(d); consoleEl.scrollTop = consoleEl.scrollHeight;
}
function fmtDate(s) {
  if (!s) return '';
  const d = new Date(s.replace(' ','T'));
  return isNaN(d) ? s : d.toLocaleDateString('en-PH',{month:'short',day:'numeric'});
}
function esc(s) { const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
const PHASE_RE = /^(Harvest:|Harvest done|Enriching|No new jobs|No jobs|\[STOPPED\]|⛔)/;

export function setCurrentJob(v) { currentJob = v; }
export { state, COL_VALUES, SORT_DEFAULT_ORDER, $, tbody, consoleEl, toastEl, detailPanel, detailOverlay, currentJob, api, toast, log, fmtDate, esc, PHASE_RE };

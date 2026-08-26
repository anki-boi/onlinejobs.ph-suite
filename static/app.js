/* ═══════════════════════════════════════════════════════════════
   Job Hunter — Frontend logic
   Vanilla JS, no dependencies.
   ═══════════════════════════════════════════════════════════════ */

// ── State ───────────────────────────────────────────────────────────────────
const state = {
  page: 1,
  perPage: 50,
  status: '',
  search: '',
  workType: '',
  skill: '',
  includeHidden: false,
  total: 0,
  scraping: false,
  keywords: { positive: [], negative: [] },
  categories: [],
  skills: [],
};

// ── DOM refs ────────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const tbody = $('#jobs-tbody');
const consoleEl = $('#console');
const toastEl = $('#toast');
const detailPanel = $('#detail-panel');
const detailOverlay = $('#detail-overlay');
const statsBar = $('#stats-bar');
const resultCount = $('#result-count');
const paginationEl = $('#pagination');

let currentJob = null;

// ── Utilities ───────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

function toast(msg, type = 'info') {
  toastEl.textContent = msg;
  toastEl.className = `toast show toast-${type}`;
  setTimeout(() => toastEl.classList.remove('show'), 2500);
}

function logLine(msg, cls = '') {
  const div = document.createElement('div');
  div.className = `log-line ${cls}`;
  div.textContent = msg;
  consoleEl.appendChild(div);
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

function clearConsole() {
  consoleEl.innerHTML = '';
}

function fmtDate(s) {
  if (!s) return '';
  const d = new Date(s.replace(' ', 'T'));
  if (isNaN(d)) return s;
  return d.toLocaleDateString('en-PH', { month: 'short', day: 'numeric' });
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// ── Stats ───────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await api('/api/stats');
    statsBar.innerHTML = `
      <div class="stat-pill new"><span>🆕</span><span class="stat-num">${s['New'] || 0}</span><span>New</span></div>
      <div class="stat-pill applied"><span>📮</span><span class="stat-num">${s['Applied'] || 0}</span><span>Applied</span></div>
      <div class="stat-pill interviewing"><span>💬</span><span class="stat-num">${s['Interviewing'] || 0}</span><span>Interview</span></div>
      <div class="stat-pill hired"><span>🎉</span><span class="stat-num">${s['Hired'] || 0}</span><span>Hired</span></div>
      <div class="stat-pill"><span>📊</span><span class="stat-num">${s.total || 0}</span><span>Total</span></div>
    `;
  } catch (e) { /* ignore */ }
}

// ── Jobs table ──────────────────────────────────────────────────────────────
async function loadJobs() {
  const params = new URLSearchParams({
    page: state.page,
    per_page: state.perPage,
  });
  if (state.status) params.set('status', state.status);
  if (state.search) params.set('search', state.search);
  if (state.workType) params.set('work_type', state.workType);
  if (state.skill) params.set('skill', state.skill);
  if (state.includeHidden) params.set('include_hidden', '1');

  try {
    const data = await api(`/api/jobs?${params}`);
    renderJobs(data.jobs);
    state.total = data.total;
    resultCount.textContent = `${data.total} job${data.total !== 1 ? 's' : ''}`;
    renderPagination(data.page, data.per_page, data.total);
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">⚠ ${esc(e.message)}</td></tr>`;
  }
}

function renderJobs(jobs) {
  if (!jobs.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">No jobs found</td></tr>`;
    return;
  }

  const rows = jobs.map((j) => {
    const scrapeDot = j.scrape_status === 'Open' ? 'open' : j.scrape_status === 'Closed' ? 'closed' : 'unk';
    const scrapeIcon = j.scrape_status === 'Open' ? '🟢' : j.scrape_status === 'Closed' ? '🔴' : '·';
    const wtClass = j.work_type ? j.work_type.replace(/ /g, '') : '';
    const shortWt = j.work_type ? j.work_type.split(' ')[0] : '';

    return `<tr data-id="${j.id}" class="${j.status === 'Hidden' ? 'hidden-row' : ''}">
      <td class="col-scrape"><span class="scrape-dot ${scrapeDot}" title="${j.scrape_status || 'Unknown'}"></span></td>
      <td class="col-status"><span class="status-badge status-${j.status}">${j.status}</span></td>
      <td class="col-title">
        <div class="cell-title">${esc(j.title) || '<em class="text-muted">untitled</em>'}</div>
        ${j.skills ? `<div class="mt-1">${j.skills.split(',').slice(0, 3).map(s => `<span class="skill-tag" data-skill="${esc(s.trim())}">${esc(s.trim())}</span>`).join('')}</div>` : ''}
      </td>
      <td class="col-company cell-mono">${esc(j.company || '')}</td>
      <td class="col-posted cell-date">${fmtDate(j.posted_date) || fmtDate(j.date_found)}</td>
      <td class="col-type"><span class="work-type ${wtClass}">${shortWt}</span></td>
      <td class="col-salary cell-mono">${esc(j.salary || '')}</td>
      <td class="col-actions"><button class="btn btn-ghost btn-sm" data-action="details" title="Details">↗</button></td>
    </tr>`;
  });

  tbody.innerHTML = rows.join('');
}

function renderPagination(page, perPage, total) {
  const pages = Math.ceil(total / perPage);
  if (pages <= 1) { paginationEl.innerHTML = ''; return; }

  let html = `<button class="page-btn" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>←</button>`;

  for (let i = 1; i <= pages; i++) {
    if (i === 1 || i === pages || (i >= page - 2 && i <= page + 2)) {
      html += `<button class="page-btn ${i === page ? 'active' : ''}" data-page="${i}">${i}</button>`;
    } else if (i === page - 3 || i === page + 3) {
      html += `<span class="text-muted" style="padding:0 4px">…</span>`;
    }
  }

  html += `<button class="page-btn" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>→</button>`;
  paginationEl.innerHTML = html;
}

// ── Detail panel ────────────────────────────────────────────────────────────
async function openDetail(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    currentJob = job;

    $('#detail-title').textContent = job.title || 'Untitled';
    $('#detail-company').textContent = job.company || '';
    $('#detail-status').textContent = job.status;
    $('#detail-status').className = `status-badge status-${job.status}`;
    $('#detail-work-type').textContent = job.work_type || '';
    $('#detail-work-type').className = `work-type ${(job.work_type || '').replace(/ /g, '')}`;
    $('#detail-salary').textContent = job.salary || '';
    $('#detail-hours').textContent = job.hours_per_week || '';
    $('#detail-location').textContent = job.location || '';
    $('#detail-posted').textContent = job.posted_date || '';
    $('#detail-updated').textContent = job.date_updated || '';
    $('#detail-source').textContent = job.search_keyword || job.search_category || '';
    $('#detail-desc').textContent = job.description || 'No description yet. Re-check to enrich.';
    $('#detail-desc').style.display = job.description ? '' : 'none';
    $('#detail-desc-section').style.display = job.description ? '' : 'none';

    // Skills
    const skillsEl = $('#detail-skills');
    if (job.skills) {
      skillsEl.innerHTML = job.skills.split(',').map(s => `<span class="skill-tag">${esc(s.trim())}</span>`).join('');
      $('#detail-skills-section').style.display = '';
    } else {
      skillsEl.innerHTML = '<span class="text-muted">No skills tagged</span>';
    }

    // Notes & follow-up
    $('#detail-notes').value = job.notes || '';
    $('#detail-followup').value = job.follow_up || '';
    $('#notes-saved-hint').textContent = '';

    // Status select
    const sel = $('#detail-status-select');
    sel.value = '';
    // Set apply link
    $('#detail-apply').href = job.job_url || '#';

    // Show
    detailPanel.classList.add('open');
    detailOverlay.classList.add('open');
  } catch (e) {
    toast(e.message, 'error');
  }
}

function closeDetail() {
  detailPanel.classList.remove('open');
  detailOverlay.classList.remove('open');
  currentJob = null;
}

// ── SSE streaming ───────────────────────────────────────────────────────────
async function streamSSE(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let currentEvent = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('event:')) {
        currentEvent = trimmed.slice(6).trim();
      } else if (trimmed.startsWith('data:')) {
        const dataStr = trimmed.slice(5).trim();
        let data;
        try { data = JSON.parse(dataStr); } catch { data = dataStr; }
        handleSSEEvent(currentEvent, data);
      }
    }
  }
}

function handleSSEEvent(event, data) {
  switch (event) {
    case 'log':
      logLine(`▸ ${typeof data === 'string' ? data : data.message || JSON.stringify(data)}`);
      break;
    case 'error':
      logLine(`⚠ ${typeof data === 'string' ? data : data.message}`, 'log-error');
      break;
    case 'harvest_done':
      logLine(`✅ ${data.inserted}/${data.total} jobs saved from this page`, 'log-done');
      break;
    case 'harvest_summary':
      logLine(`📦 Harvest: ${data.new} new / ${data.seen} seen`, 'log-done');
      break;
    case 'enrich_done': {
      const icon = data.is_closed ? '🔴' : '🟢';
      const status = data.is_closed ? 'Closed' : 'Open';
      const filled = [data.title, data.company, data.description, data.skills].filter(Boolean).length;
      logLine(`  ${icon} [${data.row_id}] ${status} — ${filled} fields`, data.is_closed ? 'log-closed' : 'log-open');
      break;
    }
    case 'enrich_summary':
      logLine(`📊 Enrich: 🟢 ${data.open} open, 🔴 ${data.closed} closed, ⚠ ${data.errors} errors`, 'log-done');
      break;
    case 'done':
      logLine(`✓ Done: ${typeof data === 'string' ? data : JSON.stringify(data)}`, 'log-done');
      setScraping(false);
      loadJobs();
      loadStats();
      break;
  }
}

function setScraping(active) {
  state.scraping = active;
  $('#btn-harvest').disabled = active;
  $('#btn-check').disabled = active;
  $('#btn-stop').disabled = !active;
  $('#harvest-label').innerHTML = active ? '<span class="spinner"></span> Scraping…' : 'Scrape new jobs';
  $('#check-label').innerHTML = active ? '<span class="spinner"></span> Checking…' : 'Check for updates';
}

// ── Pipeline actions ────────────────────────────────────────────────────────
async function runPipeline() {
  const kw = state.keywords.positive.join(', ').trim();
  const body = {};
  if (kw) body.keyword = kw;

  const since = $('#posted-since').value;
  if (since) body.posted_since = since;

  if (!kw) {
    toast('Add at least one keyword first', 'error');
    return;
  }

  clearConsole();
  setScraping(true);
  logLine(`Starting pipeline for: ${kw}`);

  try {
    await streamSSE('/api/pipeline/run', body);
  } catch (e) {
    logLine(`⚠ ${e.message}`, 'log-error');
    setScraping(false);
  }
}

async function runCheck() {
  const body = {
    workers: 3,
    recheck_all: $('#check-all').checked,
  };

  clearConsole();
  setScraping(true);
  logLine(body.recheck_all ? 'Checking ALL jobs…' : 'Checking New/Interested jobs…');

  try {
    await streamSSE('/api/pipeline/check', body);
  } catch (e) {
    logLine(`⚠ ${e.message}`, 'log-error');
    setScraping(false);
  }
}

async function stopPipeline() {
  try {
    await fetch('/api/pipeline/stop', { method: 'POST' });
    logLine('⏹ Stop signal sent', 'log-error');
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Keyword chips ───────────────────────────────────────────────────────────
function renderChips() {
  const el = $('#kw-chips');
  const chips = [];
  state.keywords.positive.forEach((kw, i) => {
    chips.push(`<span class="chip chip-pos">${esc(kw)}<button class="chip-remove" data-type="pos" data-i="${i}">×</button></span>`);
  });
  state.keywords.negative.forEach((kw, i) => {
    chips.push(`<span class="chip chip-neg">−${esc(kw)}<button class="chip-remove" data-type="neg" data-i="${i}">×</button></span>`);
  });
  el.innerHTML = chips.join(' ');
}

function addKeyword() {
  const input = $('#kw-input');
  const val = input.value.trim();
  if (!val) return;
  if (!state.keywords.positive.includes(val)) {
    state.keywords.positive.push(val);
  }
  input.value = '';
  renderChips();
}

function removeKeyword(type, i) {
  if (type === 'pos') state.keywords.positive.splice(i, 1);
  else state.keywords.negative.splice(i, 1);
  renderChips();
}

// ── Categories & Skills ─────────────────────────────────────────────────────
async function loadCategories() {
  try {
    const cats = await api('/api/skills/categories');
    state.categories = cats;
    renderCategories(cats, '');
  } catch (e) { /* ignore */ }
}

function renderCategories(cats, filter) {
  const el = $('#cat-list');
  const filtered = cats.filter(c => !filter || c.name.toLowerCase().includes(filter.toLowerCase()));
  el.innerHTML = filtered.map(c => `
    <div class="cat-item" data-cat="${esc(c.name)}" data-slug="${esc(c.slug || '')}">
      <span>${esc(c.name)}</span>
      <span class="cat-count">${c.count}</span>
    </div>
  `).join('') || '<div class="text-muted" style="padding:8px">No categories</div>';
}

async function loadSkills(filter = '') {
  try {
    const params = filter ? `?search=${encodeURIComponent(filter)}` : '';
    const skills = await api(`/api/skills${params}`);
    state.skills = skills;
    renderSkills(skills, filter);
  } catch (e) { /* ignore */ }
}

function renderSkills(skills, filter) {
  const el = $('#skill-list');
  const shown = skills.slice(0, 100);
  el.innerHTML = shown.map(s => `
    <div class="skill-item" data-skill="${esc(s.name)}">
      <span>
        <span class="skill-name">${esc(s.name)}</span>
        ${s.category_path ? `<br><span class="skill-cat">${esc(s.category_path)}</span>` : ''}
      </span>
    </div>
  `).join('') || '<div class="text-muted" style="padding:8px">No skills found</div>';
}

// ── Export CSV ──────────────────────────────────────────────────────────────
async function exportCSV() {
  try {
    const data = await api('/api/jobs?per_page=99999');
    const cols = ['id','job_id','title','company','work_type','salary','location',
                  'hours_per_week','skills','status','scrape_status','posted_date',
                  'date_found','date_updated','search_keyword','search_category',
                  'job_url','notes','follow_up'];
    const rows = [[cols.join(',')].concat(data.jobs.map(j =>
      cols.map(c => `"${String(j[c] ?? '').replace(/"/g, '""')}"`).join(',')
    ))];
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `jobs_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    toast('CSV exported', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Event handlers ──────────────────────────────────────────────────────────
function init() {
  // Harvest
  $('#btn-harvest').addEventListener('click', runPipeline);

  // Check
  $('#btn-check').addEventListener('click', runCheck);

  // Stop
  $('#btn-stop').addEventListener('click', stopPipeline);

  // Keyword input
  $('#kw-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addKeyword(); }
  });
  $('#kw-add').addEventListener('click', addKeyword);
  $('#kw-chips').addEventListener('click', (e) => {
    const btn = e.target.closest('.chip-remove');
    if (btn) removeKeyword(btn.dataset.type, parseInt(btn.dataset.i));
  });
  $('#kw-clear').addEventListener('click', () => {
    state.keywords = { positive: [], negative: [] };
    renderChips();
  });
  $('#kw-apply').addEventListener('click', async () => {
    try {
      const res = await api('/api/keywords/apply', {
        method: 'POST',
        body: JSON.stringify(state.keywords),
      });
      toast(`Applied filters: ${res.total_hidden} hidden`, 'success');
      loadJobs();
      loadStats();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // Console clear
  $('#console-close').addEventListener('click', clearConsole);

  // Search & filters
  let searchTimer;
  $('#search-box').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = e.target.value.trim();
      state.page = 1;
      loadJobs();
    }, 300);
  });
  $('#filter-status').addEventListener('change', (e) => {
    state.status = e.target.value;
    state.page = 1;
    loadJobs();
  });
  $('#filter-type').addEventListener('change', (e) => {
    state.workType = e.target.value;
    state.page = 1;
    loadJobs();
  });
  $('#filter-hidden').addEventListener('change', (e) => {
    state.includeHidden = e.target.checked;
    state.page = 1;
    loadJobs();
  });

  // Pagination
  paginationEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.page-btn');
    if (btn && !btn.disabled) {
      state.page = parseInt(btn.dataset.page);
      loadJobs();
    }
  });

  // Table row click → detail
  tbody.addEventListener('click', (e) => {
    const row = e.target.closest('tr[data-id]');
    if (row) openDetail(parseInt(row.dataset.id));
    const tag = e.target.closest('.skill-tag');
    if (tag) {
      state.skill = tag.dataset.skill || tag.textContent;
      state.page = 1;
      loadJobs();
      e.stopPropagation();
    }
  });

  // Detail panel
  $('#detail-close').addEventListener('click', closeDetail);
  detailOverlay.addEventListener('click', closeDetail);

  // Detail: status change
  $('#detail-status-select').addEventListener('change', async (e) => {
    if (!currentJob || !e.target.value) return;
    try {
      await api(`/api/jobs/${currentJob.id}/status`, {
        method: 'PATCH',
        body: JSON.stringify({ status: e.target.value }),
      });
      toast(`Status → ${e.target.value}`, 'success');
      closeDetail();
      loadJobs();
      loadStats();
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  // Detail: notes
  $('#detail-save-notes').addEventListener('click', async () => {
    if (!currentJob) return;
    const notes = $('#detail-notes').value;
    try {
      await api(`/api/jobs/${currentJob.id}/notes`, {
        method: 'PATCH',
        body: JSON.stringify({ notes }),
      });
      $('#notes-saved-hint').textContent = '✓ Saved';
      setTimeout(() => $('#notes-saved-hint').textContent = '', 2000);
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // Detail: follow-up
  $('#detail-save-followup').addEventListener('click', async () => {
    if (!currentJob) return;
    const follow_up = $('#detail-followup').value;
    try {
      await api(`/api/jobs/${currentJob.id}/follow-up`, {
        method: 'PATCH',
        body: JSON.stringify({ follow_up }),
      });
      toast('Follow-up saved', 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // Detail: re-check single job
  $('#detail-recheck').addEventListener('click', async () => {
    if (!currentJob) return;
    logLine(`Re-checking job ${currentJob.id}…`);
    setScraping(true);
    try {
      await streamSSE('/api/pipeline/check', { workers: 1, recheck_all: false });
    } catch (e) {
      logLine(`⚠ ${e.message}`, 'log-error');
    }
    setScraping(false);
    closeDetail();
    loadJobs();
  });

  // Categories
  $('#cat-search').addEventListener('input', (e) => {
    renderCategories(state.categories, e.target.value);
  });
  $('#cat-list').addEventListener('click', (e) => {
    const item = e.target.closest('.cat-item');
    if (!item) return;
    // Remove active from all
    document.querySelectorAll('.cat-item.active').forEach(el => el.classList.remove('active'));
    item.classList.add('active');

    const catName = item.dataset.cat;
    const slug = item.dataset.slug;
    // Trigger pipeline with this category
    clearConsole();
    setScraping(true);
    logLine(`Scraping category: ${catName} (${slug})`);
    streamSSE('/api/pipeline/run', { category: slug || catName.toLowerCase().replace(/ /g, '-') })
      .catch(e => logLine(`⚠ ${e.message}`, 'log-error'))
      .finally(() => setScraping(false));
  });

  // Skills
  let skillTimer;
  $('#skill-search').addEventListener('input', (e) => {
    clearTimeout(skillTimer);
    skillTimer = setTimeout(() => loadSkills(e.target.value), 300);
  });
  $('#skill-list').addEventListener('click', (e) => {
    const item = e.target.closest('.skill-item');
    if (item) {
      state.skill = item.dataset.skill;
      state.page = 1;
      loadJobs();
      toast(`Filtering by: ${state.skill}`, 'info');
    }
  });
  $('#btn-refresh-skills').addEventListener('click', async () => {
    $('#btn-refresh-skills').disabled = true;
    toast('Refreshing skills from API…', 'info');
    try {
      const res = await fetch('/api/skills/refresh', { method: 'POST' });
      const data = await res.json();
      toast(`Loaded ${data.count} skills`, 'success');
      loadSkills();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      $('#btn-refresh-skills').disabled = false;
    }
  });

  // Export
  $('#btn-export-csv').addEventListener('click', exportCSV);

  // Keyboard: Escape closes detail
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDetail();
  });
}

// ── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  init();
  loadStats();
  loadJobs();
  loadCategories();
  loadSkills();
});

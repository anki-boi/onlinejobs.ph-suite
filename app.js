// ── State ──────────────────────────────────────────────────────────────────

const state = {
  jobs: [],
  tags: [],
  posKeywords: [],
  negKeywords: [],
  selectedTags: new Set(),
  currentJobId: null,
  searchQuery: '',
  statusFilter: '',
  showHidden: false,
};

// ── DOM refs ───────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

const els = {
  headerStats:    $('headerStats'),
  tagCount:       $('tagCount'),
  tagSearchInput: $('tagSearchInput'),
  tagSelect:      $('tagSelect'),
  selectedTagsList: $('selectedTagsList'),
  maxPages:       $('maxPages'),
  posKeywordInput: $('posKeywordInput'),
  negKeywordInput: $('negKeywordInput'),
  btnAddPos:      $('btnAddPos'),
  btnAddNeg:      $('btnAddNeg'),
  posChips:       $('posChips'),
  negChips:       $('negChips'),
  btnApplyKeywords: $('btnApplyKeywords'),
  btnRefreshTags: $('btnRefreshTags'),
  btnRun:         $('btnRun'),
  btnCheckOnly:   $('btnCheckOnly'),
  consoleWrap:    $('consoleWrap'),
  btnCloseConsole: $('btnCloseConsole'),
  console:        $('console'),
  tableSearch:    $('tableSearch'),
  statusFilter:   $('statusFilter'),
  showHidden:     $('showHidden'),
  resultCount:    $('resultCount'),
  jobsBody:       $('jobsBody'),
  modalOverlay:   $('modalOverlay'),
  modalTitle:     $('modalTitle'),
  modalLink:      $('modalLink'),
  modalMeta:      $('modalMeta'),
  modalTags:      $('modalTags'),
  modalDesc:      $('modalDesc'),
  modalNotes:     $('modalNotes'),
  modalStatus:    $('modalStatus'),
  btnSaveNotes:   $('btnSaveNotes'),
  btnSaveStatus:  $('btnSaveStatus'),
  btnCloseModal:  $('btnCloseModal'),
};

// ── Toast ──────────────────────────────────────────────────────────────────

let toastContainer = null;

function toast(msg, type = 'info', duration = 3500) {
  if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.className = 'toast-container';
    document.body.appendChild(toastContainer);
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  toastContainer.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ── API helpers ────────────────────────────────────────────────────────────

async function api(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ── SSE stream helper ──────────────────────────────────────────────────────

function streamSSE(method, url, body, handlers) {
  // FastAPI StreamingResponse doesn't need EventSource for POST —
  // we use fetch + ReadableStream
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) opts.body = JSON.stringify(body);

  fetch(url, opts).then(async res => {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Parse SSE lines
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line

      let eventType = 'message';
      for (const line of lines) {
        if (line.startsWith('event:')) {
          eventType = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          const data = line.slice(5).trim();
          if (handlers[eventType]) handlers[eventType](data);
          eventType = 'message';
        }
      }
    }
    if (handlers['complete']) handlers['complete']();
  }).catch(err => {
    if (handlers['error']) handlers['error'](err.message);
  });
}

// ── Console logging ────────────────────────────────────────────────────────

function showConsole() {
  els.consoleWrap.style.display = 'block';
  els.console.innerHTML = '';
}

function logLine(text) {
  const line = document.createElement('div');
  line.className = 'log-line';

  // Colorise
  if (text.includes('🟢') || text.includes('Open')) line.classList.add('log-open');
  else if (text.includes('🔴') || text.includes('Closed')) line.classList.add('log-closed');
  else if (text.includes('🏷') || text.includes('Tag:')) line.classList.add('log-tag');
  else if (text.includes('✅') || text.includes('Done')) line.classList.add('log-done');

  line.textContent = text;
  els.console.appendChild(line);
  els.console.scrollTop = els.console.scrollHeight;
}

// ── Stats & header ─────────────────────────────────────────────────────────

async function refreshStats() {
  try {
    const stats = await api('GET', '/api/stats');
    const items = [
      { label: 'New',          val: stats.New || 0,          cls: 'new' },
      { label: 'Applied',      val: stats.Applied || 0,      cls: 'applied' },
      { label: 'Interviewing', val: stats.Interviewing || 0, cls: 'interviewing' },
      { label: 'Total',        val: stats.total || 0,        cls: '' },
    ];
    els.headerStats.innerHTML = items.map(i =>
      `<div class="stat-pill ${i.cls}"><span class="stat-num">${i.val}</span> ${i.label}</div>`
    ).join('');
  } catch {}
}

// ── Tags ───────────────────────────────────────────────────────────────────

async function loadTags() {
  try {
    const tags = await api('GET', '/api/tags');
    state.tags = tags;
    renderTagSelect(tags);
    els.tagCount.textContent = `${tags.length} tag${tags.length !== 1 ? 's' : ''} loaded`;
  } catch {
    els.tagCount.textContent = 'Failed to load tags';
  }
}

function renderTagSelect(tags) {
  const query = els.tagSearchInput.value.toLowerCase();
  const filtered = tags.filter(t => t.name.toLowerCase().includes(query));
  els.tagSelect.innerHTML = filtered.map(t =>
    `<option value="${t.id}" ${state.selectedTags.has(t.id) ? 'selected' : ''}>${t.name}</option>`
  ).join('');
}

function renderSelectedTags() {
  const tagMap = Object.fromEntries(state.tags.map(t => [t.id, t.name]));
  els.selectedTagsList.innerHTML = [...state.selectedTags].map(id =>
    `<span class="chip chip-tag">
      ${tagMap[id] || id}
      <button class="chip-remove" data-tag="${id}" title="Remove">×</button>
    </span>`
  ).join('');
}

els.tagSearchInput.addEventListener('input', () => renderTagSelect(state.tags));

els.tagSelect.addEventListener('change', () => {
  // Sync selected state from <select multiple>
  state.selectedTags.clear();
  for (const opt of els.tagSelect.selectedOptions) {
    state.selectedTags.add(opt.value);
  }
  renderSelectedTags();
});

els.selectedTagsList.addEventListener('click', e => {
  const btn = e.target.closest('.chip-remove');
  if (!btn) return;
  state.selectedTags.delete(btn.dataset.tag);
  renderTagSelect(state.tags);
  renderSelectedTags();
});

els.btnRefreshTags.addEventListener('click', () => {
  showConsole();
  setRunning(true);
  logLine('🌐 Refreshing tag catalogue…');

  streamSSE('GET', '/api/tags/refresh', null, {
    log:       d => logLine(d),
    tags_done: d => {
      const data = JSON.parse(d);
      if (data.error) { toast(data.error, 'error'); return; }
      toast(`✅ ${data.count} tags synced`, 'success');
      loadTags();
    },
    complete: () => setRunning(false),
    error:    e => { toast(`Error: ${e}`, 'error'); setRunning(false); },
  });
});

// ── Keywords ───────────────────────────────────────────────────────────────

function renderChips(arr, container, cls) {
  container.innerHTML = arr.map((kw, i) =>
    `<span class="chip ${cls}">${kw}<button class="chip-remove" data-i="${i}" title="Remove">×</button></span>`
  ).join('');
  els.btnApplyKeywords.disabled = state.posKeywords.length === 0 && state.negKeywords.length === 0;
}

function addKeyword(input, arr, container, cls) {
  const val = input.value.trim();
  if (!val || arr.includes(val)) { input.value = ''; return; }
  arr.push(val);
  input.value = '';
  renderChips(arr, container, cls);
}

els.btnAddPos.addEventListener('click', () =>
  addKeyword(els.posKeywordInput, state.posKeywords, els.posChips, 'chip-pos'));
els.btnAddNeg.addEventListener('click', () =>
  addKeyword(els.negKeywordInput, state.negKeywords, els.negChips, 'chip-neg'));

els.posKeywordInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') addKeyword(els.posKeywordInput, state.posKeywords, els.posChips, 'chip-pos');
});
els.negKeywordInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') addKeyword(els.negKeywordInput, state.negKeywords, els.negChips, 'chip-neg');
});

els.posChips.addEventListener('click', e => {
  const btn = e.target.closest('.chip-remove');
  if (!btn) return;
  state.posKeywords.splice(+btn.dataset.i, 1);
  renderChips(state.posKeywords, els.posChips, 'chip-pos');
});

els.negChips.addEventListener('click', e => {
  const btn = e.target.closest('.chip-remove');
  if (!btn) return;
  state.negKeywords.splice(+btn.dataset.i, 1);
  renderChips(state.negKeywords, els.negChips, 'chip-neg');
});

els.btnApplyKeywords.addEventListener('click', async () => {
  try {
    const res = await api('POST', '/api/keywords/apply', {
      positive: state.posKeywords,
      negative: state.negKeywords,
    });
    toast(`Filters applied — ${res.total_hidden} job(s) hidden`, 'success');
    await loadJobs();
    await refreshStats();
  } catch (e) {
    toast(`Error: ${e.message}`, 'error');
  }
});

// ── Pipeline ───────────────────────────────────────────────────────────────

function setRunning(running) {
  els.btnRun.disabled        = running;
  els.btnCheckOnly.disabled  = running;
  els.btnRefreshTags.disabled = running;
  if (running) {
    els.btnRun.classList.add('running');
  } else {
    els.btnRun.classList.remove('running');
    refreshStats();
    loadJobs();
  }
}

els.btnRun.addEventListener('click', () => {
  if (state.selectedTags.size === 0) {
    toast('Select at least one tag first', 'error');
    return;
  }

  showConsole();
  setRunning(true);
  logLine('▶ Starting pipeline…');

  streamSSE('POST', '/api/pipeline/run', {
    tag_ids:   [...state.selectedTags],
    max_pages: parseInt(els.maxPages.value) || 1,
    workers:   5,
  }, {
    log:          d => logLine(d),
    harvest_done: d => {
      const { inserted, total } = JSON.parse(d);
      logLine(`✅ Harvest: ${inserted} new links saved (${total} found)`);
    },
    check_done: d => {
      const s = JSON.parse(d);
      logLine(`✅ Check done — 🟢 ${s.open} open, 🔴 ${s.closed} closed`);
    },
    complete: () => {
      logLine('✅ Pipeline complete.');
      setRunning(false);
    },
    error: e => {
      logLine(`❌ Error: ${e}`);
      toast(`Pipeline error: ${e}`, 'error');
      setRunning(false);
    },
  });
});

els.btnCheckOnly.addEventListener('click', () => {
  showConsole();
  setRunning(true);
  logLine('↻ Re-checking open jobs…');

  streamSSE('POST', '/api/pipeline/check', { workers: 5 }, {
    log:        d => logLine(d),
    check_done: d => {
      const s = JSON.parse(d);
      logLine(`✅ Done — 🟢 ${s.open} open, 🔴 ${s.closed} closed`);
    },
    complete: () => {
      logLine('✅ Done.');
      setRunning(false);
    },
    error: e => {
      logLine(`❌ Error: ${e}`);
      setRunning(false);
    },
  });
});

els.btnCloseConsole.addEventListener('click', () => {
  els.consoleWrap.style.display = 'none';
});

// ── Jobs table ─────────────────────────────────────────────────────────────

async function loadJobs() {
  try {
    const params = new URLSearchParams();
    if (state.showHidden) params.set('include_hidden', 'true');
    if (state.statusFilter) params.set('status', state.statusFilter);
    state.jobs = await api('GET', `/api/jobs?${params}`);
    renderTable();
  } catch (e) {
    toast(`Failed to load jobs: ${e.message}`, 'error');
  }
}

function renderTable() {
  const query = state.searchQuery.toLowerCase();
  const jobs  = state.jobs.filter(j => {
    if (!query) return true;
    return (
      (j.job_title   || '').toLowerCase().includes(query) ||
      (j.company     || '').toLowerCase().includes(query) ||
      (j.description || '').toLowerCase().includes(query) ||
      (j.search_tag  || '').toLowerCase().includes(query)
    );
  });

  els.resultCount.textContent = `${jobs.length} job${jobs.length !== 1 ? 's' : ''}`;

  if (jobs.length === 0) {
    els.jobsBody.innerHTML = `<tr class="empty-row"><td colspan="7">No jobs match your filters.</td></tr>`;
    return;
  }

  els.jobsBody.innerHTML = jobs.map(j => {
    const isHidden = j.status === 'Hidden' || j.status === 'Closed';
    return `
    <tr class="${isHidden ? 'hidden-row' : ''}" data-id="${j.id}">
      <td><span class="status-badge status-${j.status}">${j.status}</span></td>
      <td class="cell-title">${esc(j.job_title || '—')}</td>
      <td class="cell-mono">${esc(j.company || '—')}</td>
      <td><span class="chip chip-tag" style="font-size:0.68rem">${esc(j.search_tag || '—')}</span></td>
      <td class="cell-mono">${esc(j.salary || '—')}</td>
      <td class="cell-date">${esc(j.date_found || '—')}</td>
      <td>
        <div class="row-actions">
          <button class="action-btn hide-btn" data-id="${j.id}" title="Hide">🙈</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Row click → open modal
els.jobsBody.addEventListener('click', async e => {
  // Hide button
  const hideBtn = e.target.closest('.hide-btn');
  if (hideBtn) {
    e.stopPropagation();
    const id = +hideBtn.dataset.id;
    try {
      await api('PATCH', `/api/jobs/${id}/status`, { status: 'Hidden' });
      await loadJobs();
      await refreshStats();
      toast('Job hidden', 'info');
    } catch (err) { toast(err.message, 'error'); }
    return;
  }

  const row = e.target.closest('tr[data-id]');
  if (!row) return;
  openModal(+row.dataset.id);
});

// Filters
els.tableSearch.addEventListener('input', e => {
  state.searchQuery = e.target.value;
  renderTable();
});

els.statusFilter.addEventListener('change', e => {
  state.statusFilter = e.target.value;
  loadJobs();
});

els.showHidden.addEventListener('change', e => {
  state.showHidden = e.target.checked;
  loadJobs();
});

// ── Modal ──────────────────────────────────────────────────────────────────

async function openModal(jobId) {
  state.currentJobId = jobId;
  try {
    const j = await api('GET', `/api/jobs/${jobId}`);

    els.modalTitle.textContent = j.job_title || '(No title)';
    els.modalLink.href = j.job_link;

    // Meta
    els.modalMeta.innerHTML = [
      j.company  && `<span class="meta-item"><span class="meta-label">Company</span> ${esc(j.company)}</span>`,
      j.salary   && `<span class="meta-item"><span class="meta-label">Salary</span> ${esc(j.salary)}</span>`,
      j.date_found && `<span class="meta-item"><span class="meta-label">Found</span> ${esc(j.date_found)}</span>`,
      j.search_tag && `<span class="meta-item"><span class="meta-label">Search Tag</span> ${esc(j.search_tag)}</span>`,
    ].filter(Boolean).join('');

    // Tags found
    if (j.tags_found) {
      els.modalTags.innerHTML = j.tags_found.split(',').map(t =>
        `<span class="chip chip-tag">${esc(t.trim())}</span>`
      ).join('');
    } else {
      els.modalTags.innerHTML = `<span style="font-size:0.75rem;color:var(--text3)">No tags scraped yet</span>`;
    }

    // Description
    els.modalDesc.textContent = j.description || '(No description yet — run check_jobs to fetch)';

    // Notes & status
    els.modalNotes.value = j.notes || '';
    els.modalStatus.value = j.status || 'New';

    els.modalOverlay.style.display = 'flex';
  } catch (e) {
    toast(`Could not load job: ${e.message}`, 'error');
  }
}

function closeModal() {
  els.modalOverlay.style.display = 'none';
  state.currentJobId = null;
}

els.btnCloseModal.addEventListener('click', closeModal);
els.modalOverlay.addEventListener('click', e => {
  if (e.target === els.modalOverlay) closeModal();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});

els.btnSaveStatus.addEventListener('click', async () => {
  if (!state.currentJobId) return;
  try {
    await api('PATCH', `/api/jobs/${state.currentJobId}/status`, {
      status: els.modalStatus.value,
    });
    toast('Status updated', 'success');
    await loadJobs();
    await refreshStats();
  } catch (e) { toast(e.message, 'error'); }
});

els.btnSaveNotes.addEventListener('click', async () => {
  if (!state.currentJobId) return;
  try {
    await api('PATCH', `/api/jobs/${state.currentJobId}/notes`, {
      notes: els.modalNotes.value,
    });
    toast('Notes saved', 'success');
  } catch (e) { toast(e.message, 'error'); }
});

// ── Init ───────────────────────────────────────────────────────────────────

async function init() {
  await Promise.all([loadTags(), loadJobs(), refreshStats()]);
}

init();

/* W6.1: jobs table — list, pagination (W6.2), sort, column filters.
   Audit 2026-09-29 wave W-A: F1 (missing imports), F2 (repost toggle), F3 (count),
   F4 (page reset), F5/F6 (server-side skills+categories), F7 (dead virtual scroll),
   F12-lite (retry on load failure). */
import { state, $, tbody, api, esc, fmtDate, COL_VALUES, SORT_DEFAULT_ORDER } from './core.js';
import { loadResumeStatus } from './resume.js';
import { loadStats, updateNextHint } from './stats.js';
import { renderCats, renderSkills } from './filters.js';

// Shared query builder so the table and the CSV export always see the same view.
// Page is deliberately NOT part of it: everything else is the view signature, and a
// changed view resets to page 1 (F4) instead of stranding "page 5 / 1".
function buildJobsParams(perPage) {
  const p = new URLSearchParams({per_page:String(perPage)});
  // status: header filter takes precedence over the toolbar select
  if (state.colFilters.status.length) p.set('status', state.colFilters.status.join(','));
  else if (state.status) p.set('status', state.status);
  if (state.search) p.set('search', state.search);
  if (state.includeHidden) p.set('include_hidden','1');
  if (state.skills.length) p.set('skills', state.skills.join(','));
  if (state.categories.length) p.set('categories', state.categories.join(','));  // F5
  for (const k of ['work_type','scrape'])
    if (state.colFilters[k].length) p.set(k === 'scrape' ? 'scrape_status' : k, state.colFilters[k].join(','));
  for (const k of ['title','company','salary','hours'])
    if (state.colFilters[k]) p.set(k, state.colFilters[k]);
  const pf = state.colFilters.posted || {};
  if (pf.from) p.set('posted_from', pf.from);
  if (pf.to) p.set('posted_to', pf.to);
  if (state.hasSalaryOnly) p.set('has_salary','1');
  // W4.2: money filters — normalized PHP/month (raw inputs live in the toolbar)
  const sMin = ($('#filter-salary-min')||{}).value;
  const sMax = ($('#filter-salary-max')||{}).value;
  const sCur = ($('#filter-salary-cur')||{}).value;
  if (sMin !== '') p.set('salary_min_monthly', sMin);   // F9: 0 is a real bound
  if (sMax !== '') p.set('salary_max_monthly', sMax);
  if (sCur) p.set('salary_currency', sCur);
  if (state.minAts) p.set('min_fit', state.minAtsValue ?? 20);  // P1: fit, not total
  if (state.sort) { p.set('sort', state.sort); p.set('order', state.order); }
  return p;
}

// F3: the toolbar count is the global total, never the rows on this page.
function renderResultCount() {
  const el = $('#result-count'); if (!el) return;
  const n = state.total;
  let txt = n === 1 ? '1 job' : `${n.toLocaleString()} jobs`;
  const shown = tbody.querySelectorAll('tr[data-id]');
  let hidden = 0;
  shown.forEach(r => { if (r.style.display === 'none') hidden++; });
  if (hidden) txt += ` · ${hidden} repost${hidden > 1 ? 's' : ''} hidden`;
  el.textContent = txt;
}

let lastView = null;

async function loadJobs() {
  const p = buildJobsParams(state.perPage);
  const view = p.toString();
  if (view !== lastView) { lastView = view; state.page = 1; }   // F4
  p.set('page', String(state.page));
  try {
    const data = await api(`/api/jobs?${p}`);
    renderJobs(data.items);
    state.total = data.total;
    renderPager();
    applyVisibleFilter();
    updateNextHint();
    loadResumeStatus();  // refresh the "target job" dropdown as the list changes
  } catch(e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">Couldn't load jobs — ${esc(e.message)}
      <button class="btn btn-ghost btn-sm" id="retry-jobs">Retry</button></td></tr>`;
    const b = $('#retry-jobs'); if (b) b.onclick = loadJobs;
  }
}

function jobRowHtml(j) {
  const dot = j.scrape_status==='Open'?'open':j.scrape_status==='Closed'?'closed':'unk';
  const dotTitle = j.scrape_status ? `Job is ${j.scrape_status} on OJ.ph` : 'Not checked yet';
  const wt = j.work_type ? j.work_type.split(' ')[0] : '';
  const wtCls = j.work_type ? j.work_type.replace(/ /g,'') : '';
  const skillsArr = j.skills ? (Array.isArray(j.skills) ? j.skills : String(j.skills).split(',')) : [];
  const skillsHtml = skillsArr.slice(0,3).map(s=>`<span class="skill-tag">${esc(String(s).trim())}</span>`).join('');
  const repostBadge = j.repost_of ? '<span class="repost-badge" title="Same title posted again by the same employer — see the original">↻ repost</span>' : '';
  // Salary chip = the PHP-normalized monthly value (W4.1). NULL = no live FX
  // rate was available → no chip: outdated money is worse than no money.
  // Tooltip shows the exact live rate + when it was checked, so
  // "US$800/mo = ₱50,103" is verifiable.
  const cur = j.salary_currency || ((j.salary||'').includes('$') ? 'USD' : 'PHP');
  const fxMeta = state.fx || {};
  const fxRate = fxMeta[cur.toLowerCase()];
  const fxTip = (cur !== 'PHP' && fxRate)
    ? ` normalized to ₱ at 1 ${cur} = ₱${fxRate}${fxMeta.at ? ` (rate checked ${fxMeta.at})` : ''}`
    : '';
  const salChip = (j.salary_monthly_min != null && j.salary_monthly_max != null)
    ? `<div class="salary-chip" title="PHP-normalized monthly${fxTip}">₱${j.salary_monthly_min===j.salary_monthly_max ? j.salary_monthly_min.toLocaleString() : j.salary_monthly_min.toLocaleString()+'–'+j.salary_monthly_max.toLocaleString()}/mo</div>`
    : '';
  // Two kinds of hidden: yours (solid) vs keyword auto-hide (dashed, remembers what it was)
  const badge = j.status === 'Hidden'
    ? (j.filter_hidden
        ? `<span class="status-badge status-Hidden hidden-by-filter" title="Auto-hidden by keyword rules — was ${esc(j.pre_filter_status||'New')}">Hidden · auto</span>`
        : `<span class="status-badge status-Hidden" title="Hidden by you">Hidden</span>`)
    : `<span class="status-badge status-${j.status}">${j.status}</span>`;
  // P1: the fit half of the ATS score (skills + keywords, /60). The 40 hygiene
  // points are the same number on every row, so they don't belong in a column
  // whose whole job is "is this worth applying to" — they stay in the tooltip.
  const fit = (j.ats_fit === null || j.ats_fit === undefined) ? null : j.ats_fit;
  const fitHtml = fit === null
    ? '<span class="text-muted" title="Not scored yet">—</span>'
    : `<span class="fit-chip ${fit >= 40 ? 'fit-hi' : fit >= 20 ? 'fit-mid' : 'fit-lo'}" title="Fit ${fit}/60 (skills+keywords) · total ${j.ats_total}/100 against ${esc(j.ats_profile || 'best profile')}">${fit}</span>`;
  // F2: empty string, not "0" — "0" is a truthy string and used to hide every row.
  return `<tr data-id="${j.id}" data-repost="${j.repost_of||''}" data-title="${esc(j.title||'')}" tabindex="0" class="${j.status==='Hidden'?'hidden-row':''}">
    <td><span class="scrape-dot ${dot}" title="${dotTitle}"></span></td>
    <td>${badge}</td>
    <td><div class="cell-title">${esc(j.title)||'<em class="text-muted">untitled</em>'} ${repostBadge}</div>${skillsHtml?`<div class="mt-1">${skillsHtml}</div>`:''}</td>
    <td class="cell-mono">${esc(j.company||'')||'<span class="text-muted">—</span>'}</td>
    <td class="cell-date">${fmtDate(j.posted_date)||fmtDate(j.date_found)}</td>
    <td><span class="work-type ${wtCls}">${wt}</span></td>
    <td class="cell-mono">${esc(j.salary||'')||'<span class="text-muted">—</span>'}${salChip}</td>
    <td class="cell-mono">${esc(j.hours_per_week||'')||'<span class="text-muted">—</span>'}</td>
    <td class="cell-mono">${fitHtml}</td>
  </tr>`;
}

// W6.2: pager (Prev / Next) — page state lives in state.page
function renderPager() {
  const el = $('#pager'); if (!el) return;
  const per = Math.max(1, state.perPage);
  const pages = Math.max(1, Math.ceil(state.total / per));
  if (pages <= 1) { el.innerHTML = ''; return; }
  el.innerHTML = `<button id="pg-prev" ${state.page<=1?'disabled':''} aria-label="Previous page">&#9664;</button>`
    + `<span>page ${state.page} / ${pages}</span>`
    + `<button id="pg-next" ${state.page>=pages?'disabled':''} aria-label="Next page">&#9654;</button>`;
  $('#pg-prev').onclick = () => { state.page = Math.max(1, state.page-1); loadJobs(); };
  $('#pg-next').onclick = () => { state.page = Math.min(pages, state.page+1); loadJobs(); };
}

// F7 (audit D2): the W6.3 virtual scroll was dead code — VS_MIN (80) was unreachable
// at per_page 50, and its windowed rows broke the visible-count math. Deleted.
let pageItems = [];

function renderJobs(jobs) {
  pageItems = jobs;
  if (!jobs.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No jobs match — run a scrape, or loosen a filter above.</td></tr>';
    return;
  }
  tbody.innerHTML = jobs.map(jobRowHtml).join('');
}

function insertStubRow(d) {
  // Remove empty row if present
  const empty = tbody.querySelector('.empty-row');
  if (empty) empty.remove();
  // Prepend the new row. The server sends skills as a JSON array —
  // normalise to the CSV string the row renderer expects (B3: the old code
  // called .split() on the array and threw, killing the whole run UI).
  const skillsStr = Array.isArray(d.skills) ? d.skills.join(', ') : (d.skills || null);
  const j = {
    id: d.row_id,
    title: d.title,
    company: d.company,
    work_type: d.work_type,
    posted_date: d.posted_date,
    salary: d.salary,
    skills: skillsStr,
    hours_per_week: d.hours || null,
    status: 'New',
    scrape_status: null,   // not checked yet — honest gray dot until enrich
    date_found: new Date().toISOString().slice(0,10),
  };
  pageItems.unshift(j);
  renderJobs(pageItems);
  // Update count
  state.total += 1;
  renderResultCount();
  // Update stats bar count
  loadStats();
}

// F5/F6: skills and categories are filtered by the server now — it can see the whole
// skills column and search_category, while the browser only ever saw the 3 rendered
// tags, so a job matching on its 4th tag vanished. What stays here is the one thing
// the server can't do per-row without a second query: repost hiding (F2).
function applyVisibleFilter() {
  const rows = tbody.querySelectorAll('tr[data-id]');
  rows.forEach(row => {
    row.style.display = (state.hideReposts && row.dataset.repost) ? 'none' : '';
  });
  renderResultCount();

  // Filter banner (F1: renderCats/renderSkills are imported now — they used to be
  // free identifiers here, so "clear" threw a ReferenceError and did nothing)
  let banner = $('#filter-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'filter-banner';
    banner.className = 'filter-banner';
    document.querySelector('.toolbar').before(banner);
  }
  const parts = [];
  if (state.skills.length) parts.push(`Skills: ${state.skills.join(', ')}`);
  if (state.categories.length) parts.push(`Categories: ${state.categories.join(', ')}`);
  if (parts.length) {
    banner.innerHTML = parts.join(' & ') + ' <button class="filter-clear" id="filter-clear-btn">clear</button>';
    banner.style.display = '';
    $('#filter-clear-btn').onclick = () => {
      state.skills = [];
      state.categories = [];
      document.querySelectorAll('#skill-list input, #cat-list input').forEach(cb => cb.checked = false);
      renderSkills($('#skill-search').value);
      renderCats($('#cat-search').value);
      applyVisibleFilter();
      loadJobs();
    };
  } else {
    banner.style.display = 'none';
  }
}

async function exportCSV() {
  // B11: the CSV is the view on screen, not the whole table.
  const p = buildJobsParams(0);
  window.location = `/api/jobs/export?${p}`;
}
function toggleSort(col) {
  const def = SORT_DEFAULT_ORDER[col] || 'asc';
  if (state.sort === col) {
    // Clicking the same header: flip direction until the default is reached
    // again, then off. (The old logic skipped one direction entirely for
    // date columns, whose default is 'desc'.)
    if (state.order === def) state.order = def === 'asc' ? 'desc' : 'asc';
    else { state.sort = ''; state.order = 'desc'; }
  } else {
    state.sort = col;
    state.order = def;
  }
  updateSortIndicators();
  loadJobs();
}

function updateSortIndicators() {
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.col === state.sort) {
      th.classList.add(state.order === 'asc' ? 'sorted-asc' : 'sorted-desc');
      th.setAttribute('aria-sort', state.order === 'asc' ? 'ascending' : 'descending');
    } else {
      th.setAttribute('aria-sort', 'none');
    }
  });
}

function updateFunnelIndicators() {
  document.querySelectorAll('.th-funnel').forEach(fn => {
    const k = fn.dataset.filter;
    if (k === 'salary') {
      fn.classList.toggle('active', state.hasSalaryOnly || !!state.colFilters.salary);
    } else {
      const v = state.colFilters[k];
      let active;
      if (Array.isArray(v)) active = v.length > 0;
      else if (k === 'posted') active = !!(v && (v.from || v.to));
      else active = !!v;
      fn.classList.toggle('active', active);
    }
  });
}

let filterPop = null;
function closeColFilter() {
  if (!filterPop) return;
  if (filterPop._outside) document.removeEventListener('mousedown', filterPop._outside);
  if (filterPop._onEsc) document.removeEventListener('keydown', filterPop._onEsc);
  filterPop.remove();
  filterPop = null;
}

function openColFilter(col, th) {
  closeColFilter();
  const values = COL_VALUES[col];
  const current = values ? [...state.colFilters[col]] : state.colFilters[col];

  filterPop = document.createElement('div');
  filterPop.className = 'th-filter-pop';
  filterPop.setAttribute('role', 'dialog');
  filterPop.setAttribute('aria-label', `Filter by ${col}`);
  if (values) {
    filterPop.innerHTML = `
      <div class="fp-title">Filter by ${col === 'scrape' ? 'scrape status' : col === 'work_type' ? 'work type' : col}</div>
      <div class="fp-list">${values.map(v =>
        `<label class="checkbox-label"><input type="checkbox" value="${esc(v)}" ${current.includes(v)?'checked':''}> ${esc(v)}</label>`).join('')}</div>
      <div class="fp-actions"><button class="btn btn-ghost btn-sm fp-clear">Clear</button><button class="btn btn-primary btn-sm fp-ok">OK</button></div>`;
  } else if (col === 'salary') {
    filterPop.innerHTML = `
      <div class="fp-title">Salary</div>
      <label class="checkbox-label" style="margin-bottom:2px"><input type="checkbox" class="fp-has-salary" ${state.hasSalaryOnly?'checked':''}> With salary only</label>
      <p class="panel-hint" style="margin:0 0 8px 22px">Hides jobs with no amount (TBD, N/A, Negotiable, …).</p>
      <div class="fp-title" style="margin-top:6px">Contains…</div>
      <input type="text" class="input input-sm fp-text" placeholder="Type to filter" value="${esc(current)}" style="width:100%;margin-bottom:8px">
      <div class="fp-actions"><button class="btn btn-ghost btn-sm fp-clear">Clear</button><button class="btn btn-primary btn-sm fp-ok">OK</button></div>`;
  } else if (col === 'posted') {
    const pf = state.colFilters.posted || {from:'', to:''};
    filterPop.innerHTML = `
      <div class="fp-title">Date posted</div>
      <div class="fp-daterange">
        <label>From<input type="date" class="input input-sm fp-date" data-k="from" value="${esc(pf.from)}"></label>
        <label>To<input type="date" class="input input-sm fp-date" data-k="to" value="${esc(pf.to)}"></label>
      </div>
      <div class="fp-actions"><button class="btn btn-ghost btn-sm fp-clear">Clear</button><button class="btn btn-primary btn-sm fp-ok">OK</button></div>`;
  } else {
    filterPop.innerHTML = `
      <div class="fp-title">Contains…</div>
      <input type="text" class="input input-sm fp-text" placeholder="Type to filter" value="${esc(current)}" style="width:100%;margin-bottom:8px">
      <div class="fp-actions"><button class="btn btn-ghost btn-sm fp-clear">Clear</button><button class="btn btn-primary btn-sm fp-ok">OK</button></div>`;
  }
  const r = th.getBoundingClientRect();
  const pw = 230;
  filterPop.style.top = (r.bottom + 6) + 'px';
  filterPop.style.left = Math.max(8, Math.min(r.left + 40, window.innerWidth - pw - 8)) + 'px';
  document.body.appendChild(filterPop);

  filterPop.querySelector('.fp-clear').onclick = () => {
    if (values) filterPop.querySelectorAll('input').forEach(i => i.checked = false);
    else {
      const t = filterPop.querySelector('.fp-text'); if (t) t.value = '';
      filterPop.querySelectorAll('.fp-date').forEach(i => i.value = '');
      const h = filterPop.querySelector('.fp-has-salary'); if (h) h.checked = false;
    }
  };
  filterPop.querySelector('.fp-ok').onclick = () => {
    if (values) state.colFilters[col] = [...filterPop.querySelectorAll('input:checked')].map(i => i.value);
    else if (col === 'posted') {
      state.colFilters.posted = {
        from: filterPop.querySelector('.fp-date[data-k=from]')?.value || '',
        to: filterPop.querySelector('.fp-date[data-k=to]')?.value || '',
      };
    }
    else state.colFilters[col] = (filterPop.querySelector('.fp-text')?.value || '').trim();
    if (col === 'salary') {
      state.hasSalaryOnly = !!filterPop.querySelector('.fp-has-salary')?.checked;
      $('#filter-has-salary').checked = state.hasSalaryOnly; // keep the toolbar toggle in sync
    }
    if (col === 'status') { state.status = ''; $('#filter-status').value = ''; }
    updateFunnelIndicators();
    closeColFilter();
    loadJobs();
  };
  const onEsc = e => { if (e.key === 'Escape') closeColFilter(); };
  document.addEventListener('keydown', onEsc);
  filterPop._onEsc = onEsc;
  const onDoc = e => {
    if (!filterPop.contains(e.target) && !e.target.closest('.th-funnel')) closeColFilter();
  };
  document.addEventListener('mousedown', onDoc);
  filterPop._outside = onDoc;
  filterPop.querySelector('.fp-text')?.focus();
}

export { buildJobsParams, loadJobs, renderJobs, renderPager, insertStubRow, applyVisibleFilter, exportCSV, toggleSort, updateSortIndicators, updateFunnelIndicators, closeColFilter, openColFilter, renderResultCount };
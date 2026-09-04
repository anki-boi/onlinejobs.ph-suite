/* Job Hunter — Frontend logic */

const state = {
  page: 1, perPage: 99999,
  status: '', search: '', workType: '',
  skills: [],          // selected skill checkboxes (OR scope)
  categories: [],      // selected category checkboxes (OR scope)
  keywords: [],        // search keywords
  posFilters: [],      // positive keyword filters
  negFilters: [],      // negative keyword filters
  includeHidden: false,
  total: 0,
  scraping: false,
  activeRun: null,        // 'harvest' | 'check' | null — drives button phase labels
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

// ── Utils ───────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: {'Content-Type':'application/json'}, ...opts });
  if (!res.ok) { const b = await res.json().catch(()=>({})); throw new Error(b.detail||res.statusText); }
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

// ── Stats ───────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await api('/api/stats');
    $('#stats-bar').innerHTML = `
      <div class="stat-pill new"><span class="stat-num">${s['New']||0}</span> New</div>
      <div class="stat-pill applied"><span class="stat-num">${s['Applied']||0}</span> Applied</div>
      <div class="stat-pill interviewing"><span class="stat-num">${s['Interviewing']||0}</span> Interview</div>
      <div class="stat-pill hired"><span class="stat-num">${s['Hired']||0}</span> Hired</div>
      <div class="stat-pill"><span class="stat-num">${s.total||0}</span> Total</div>${
        (s.follow_ups_due||0) > 0
          ? `<div class="stat-pill followup-due" title="Applied/Interviewing/Interested jobs past their follow-up date"><span class="stat-num">${s.follow_ups_due}</span> Follow-ups due</div>`
          : ''}`;
    state.stats = s;
    updateNextHint();
  } catch(e) {}
}

// Contextual "what to do next" hint — keeps the main loop visible at a glance
function updateNextHint() {
  const el = $('#next-hint');
  if (!el) return;
  let msg = '';
  if (state.scraping) {
    msg = 'Working — new jobs land in the table as they\'re found. Watch the activity feed.';
  } else if ((state.stats?.follow_ups_due||0) > 0) {
    const n = state.stats.follow_ups_due;
    msg = `⏰ ${n} follow-up${n>1?'s':''} due — open the job and nudge the employer.`;
  } else if (!state.stats || !state.stats.total) {
    msg = 'Start here: set a scope in "Find new jobs" and hit Scrape.';
  } else if (state.total === 0) {
    msg = 'Nothing matches this view — loosen a filter (or tick "Show hidden"), or run another scrape.';
  } else if ((state.stats['New']||0) > 0) {
    const n = state.stats['New'];
    msg = `${n} new job${n>1?'s':''} to review — click a row to open it, then set its status.`;
  }
  if (msg) { el.textContent = msg; el.style.display = ''; }
  else el.style.display = 'none';
}

// ── Jobs table ──────────────────────────────────────────────────────────────
// Shared query builder so the table and the CSV export always see the same view.
function buildJobsParams(perPage) {
  const p = new URLSearchParams({page:'1', per_page:String(perPage)});
  // status: header filter takes precedence over the toolbar select
  if (state.colFilters.status.length) p.set('status', state.colFilters.status.join(','));
  else if (state.status) p.set('status', state.status);
  if (state.search) p.set('search', state.search);
  if (state.includeHidden) p.set('include_hidden','1');
  if (state.skills.length) p.set('skills', state.skills.join(','));
  if (state.colFilters.work_type.length) p.set('work_type', state.colFilters.work_type.join(','));
  if (state.colFilters.scrape.length) p.set('scrape_status', state.colFilters.scrape.join(','));
  for (const k of ['title','company','salary','hours'])
    if (state.colFilters[k]) p.set(k, state.colFilters[k]);
  const pf = state.colFilters.posted || {};
  if (pf.from) p.set('posted_from', pf.from);
  if (pf.to) p.set('posted_to', pf.to);
  if (state.hasSalaryOnly) p.set('has_salary','1');
  if (state.sort) { p.set('sort', state.sort); p.set('order', state.order); }
  return p;
}

async function loadJobs() {
  const p = buildJobsParams(99999);
  try {
    const data = await api(`/api/jobs?${p}`);
    renderJobs(data.jobs);
    state.total = data.total;
    applyVisibleFilter();
    updateNextHint();
  } catch(e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">${esc(e.message)}</td></tr>`;
  }
}

function jobRowHtml(j) {
  const dot = j.scrape_status==='Open'?'open':j.scrape_status==='Closed'?'closed':'unk';
  const dotTitle = j.scrape_status ? `Job is ${j.scrape_status} on OJ.ph` : 'Not checked yet';
  const wt = j.work_type ? j.work_type.split(' ')[0] : '';
  const wtCls = j.work_type ? j.work_type.replace(/ /g,'') : '';
  const skillsArr = j.skills ? (Array.isArray(j.skills) ? j.skills : String(j.skills).split(',')) : [];
  const skillsHtml = skillsArr.slice(0,3).map(s=>`<span class="skill-tag">${esc(String(s).trim())}</span>`).join('');
  // Two kinds of hidden: yours (solid) vs keyword auto-hide (dashed, remembers what it was)
  const badge = j.status === 'Hidden'
    ? (j.filter_hidden
        ? `<span class="status-badge status-Hidden hidden-by-filter" title="Auto-hidden by keyword rules — was ${esc(j.pre_filter_status||'New')}">Hidden · auto</span>`
        : `<span class="status-badge status-Hidden" title="Hidden by you">Hidden</span>`)
    : `<span class="status-badge status-${j.status}">${j.status}</span>`;
  return `<tr data-id="${j.id}" class="${j.status==='Hidden'?'hidden-row':''}">
    <td><span class="scrape-dot ${dot}" title="${dotTitle}"></span></td>
    <td>${badge}</td>
    <td><div class="cell-title">${esc(j.title)||'<em class="text-muted">untitled</em>'}</div>${skillsHtml?`<div class="mt-1">${skillsHtml}</div>`:''}</td>
    <td class="cell-mono">${esc(j.company||'')}</td>
    <td class="cell-date">${fmtDate(j.posted_date)||fmtDate(j.date_found)}</td>
    <td><span class="work-type ${wtCls}">${wt}</span></td>
    <td class="cell-mono">${esc(j.salary||'')||'<span class="text-muted">—</span>'}</td>
    <td class="cell-mono">${esc(j.hours_per_week||'')}</td>
  </tr>`;
}

function renderJobs(jobs) {
  if (!jobs.length) { tbody.innerHTML='<tr class="empty-row"><td colspan="8">No jobs here — run a scrape, or loosen a filter above.</td></tr>'; return; }
  tbody.innerHTML = jobs.map(jobRowHtml).join('');
}

// ── Real-time stub insertion during scraping ───────────────────────────────
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
  tbody.insertAdjacentHTML('afterbegin', jobRowHtml(j));
  // Update count
  state.total += 1;
  $('#result-count').textContent = `${state.total} jobs`;
  // Update stats bar count
  loadStats();
}

// ── Instant client-side skill/category filter ─────────────────────────────
function applyVisibleFilter() {
  const rows = tbody.querySelectorAll('tr[data-id]');
  const activeSkills = state.skills.map(s => s.toLowerCase());
  const activeCats = state.categories.map(c => c.toLowerCase());
  
  let visible = 0;
  rows.forEach(row => {
    const title = (row.querySelector('.cell-title')?.textContent || '').toLowerCase();
    const company = (row.cells[3]?.textContent || '').toLowerCase();
    const skillsText = (row.querySelector('.mt-1')?.textContent || '').toLowerCase();
    const allText = title + ' ' + company + ' ' + skillsText;
    
    let show = true;
    // If skills selected, row must match at least one
    if (activeSkills.length) {
      show = activeSkills.some(sk => allText.includes(sk));
    }
    // If categories selected, row must match at least one (check title/company)
    if (activeCats.length && show) {
      show = activeCats.some(cat => allText.includes(cat));
    }
    
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  
  // Update count and filter banner
  const total = rows.length;
  $('#result-count').textContent = activeSkills.length || activeCats.length
    ? `${visible} of ${total} jobs (filtered)`
    : `${total} jobs`;
  
  // Show/hide filter banner
  let banner = $('#filter-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'filter-banner';
    banner.className = 'filter-banner';
    document.querySelector('.toolbar').before(banner);
  }
  const parts = [];
  if (activeSkills.length) parts.push(`Skills: ${state.skills.join(', ')}`);
  if (activeCats.length) parts.push(`Categories: ${state.categories.join(', ')}`);
  if (parts.length) {
    banner.innerHTML = parts.join(' & ') + ' <button class="filter-clear" id="filter-clear-btn">clear</button>';
    banner.style.display = '';
    $('#filter-clear-btn').onclick = () => {
      state.skills = [];
      state.categories = [];
      // Uncheck all
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

// ── Detail panel (full job overview) ───────────────────────────────────────
async function openDetail(id) {
  try {
    const j = await api(`/api/jobs/${id}`);
    currentJob = j;
    $('#detail-title').textContent = j.title||'Untitled';
    $('#detail-company').textContent = j.company||'';
    $('#detail-status').textContent = j.status;
    $('#detail-status').className = `status-badge status-${j.status}`;
    $('#detail-work-type').textContent = j.work_type||'';
    $('#detail-work-type').className = `work-type ${(j.work_type||'').replace(/ /g,'')}`;
    const hn = $('#detail-hidden-note');
    if (j.status === 'Hidden' && j.filter_hidden) {
      hn.textContent = `Auto-hidden by your keyword rules — it was "${j.pre_filter_status||'New'}" before. Restore it from the Auto-hide panel on the left.`;
      hn.style.display = '';
    } else hn.style.display = 'none';
    $('#detail-salary').textContent = j.salary||'\u2014';
    $('#detail-hours').textContent = j.hours_per_week||'\u2014';
    $('#detail-posted').textContent = j.posted_date||'';
    $('#detail-updated').textContent = j.date_updated||'';

    // Skills
    const sk = $('#detail-skills');
    if (j.skills) {
      sk.innerHTML = j.skills.split(',').map(s=>`<span class="skill-tag">${esc(s.trim())}</span>`).join('');
      $('#detail-skills-section').style.display='';
    } else { sk.innerHTML='<span class="text-muted">None tagged</span>'; }

    // FULL DESCRIPTION - the main content
    const descEl = $('#detail-desc');
    if (j.description) {
      descEl.textContent = j.description;
      descEl.style.display = '';
      descEl.style.opacity = '1';
    } else {
      descEl.textContent = 'No description yet. Run "Check" to enrich this job.';
      descEl.style.opacity = '0.5';
    }

    // Notes & follow-up
    $('#detail-notes').value = j.notes||'';
    $('#detail-followup').value = j.follow_up||'';
    $('#notes-saved-hint').textContent='';
    $('#detail-apply').href = j.job_url||'#';

    detailPanel.classList.add('open');
    detailOverlay.classList.add('open');
  } catch(e) { toast(e.message,'error'); }
}
function closeDetail() { detailPanel.classList.remove('open'); detailOverlay.classList.remove('open'); currentJob=null; }

// ── SSE ───────────────────────────────────────────────────────────────────
async function streamSSE(url, body) {
  const res = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  if (!res.ok) { const e=await res.json().catch(()=>({})); throw new Error(e.detail||res.statusText); }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf='', ev='';
  try {
    while(true) {
      const {done,value} = await reader.read();
      if (done) break;
      buf += dec.decode(value,{stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        const t = line.trim();
        if (t.startsWith('event:')) ev = t.slice(6).trim();
        else if (t.startsWith('data:')) {
          let d; try { d=JSON.parse(t.slice(5).trim()); } catch { d=t.slice(5).trim(); }
          handleSSE(ev, d);
        }
      }
    }
  } finally {
    // On any failure, cancel the body so the server-side run actually stops
    // (an abandoned-but-open stream would keep scraping in the background
    // while the UI already shows the run as dead).
    try { await reader.cancel(); } catch {}
  }
}
// Phase-transition log lines get the ▸ accent treatment so the run's
// current phase is visible at a glance in the activity feed.
const PHASE_RE = /^(Harvest:|Harvest done|Enriching|No new jobs|No jobs|\[STOPPED\]|⛔)/;
function handleSSE(ev, d) {
  switch(ev) {
    case 'log': {
      const msg = typeof d==='string'?d:(d.message||'');
      const phase = PHASE_RE.test(msg);
      log(phase ? `▸ ${msg}` : `> ${msg}`, phase ? 'log-phase' : '');
      break;
    }
    case 'error': log(`! ${typeof d==='string'?d:d.message||''}`, 'log-error'); break;
    case 'harvest_stub':
      insertStubRow(d);
      break;
    case 'harvest_done': log(`  +${d.inserted} saved`, 'log-done'); break;
    case 'harvest_summary':
      log(`▸ Harvest: ${d.new} new / ${d.seen} seen`, 'log-phase log-done');
      if (state.activeRun === 'harvest') $('#harvest-label').textContent = d.new ? 'Enriching…' : 'Scraping…';
      loadJobs();
      break;
    case 'enrich_done': {
      if (d.error) { log(`  ${d.progress||''} [err] ${d.error}`.trim(), 'log-error'); break; }
      const icon = d.is_closed?'[X]':'[ok]';
      const filled = ['title','company','description','salary','skills'].filter(k=> Array.isArray(d[k]) ? d[k].length : !!d[k]);
      let line = `  ${d.progress||''} ${icon} ${d.title||d.row_id}`.trim();
      if (filled.length) line += `  (${filled.join(', ')})`;
      log(line, d.is_closed?'log-closed':'log-open');
      break;
    }
    case 'enrich_summary':
      log(`▸ Enrich: ${d.open} open, ${d.closed} closed, ${d.errors} err${d.total?` (of ${d.total})`:''}`, 'log-phase log-done');
      loadJobs();
      break;
    case 'done': {
      log(`Done: ${typeof d==='string'?d:''}`, 'log-done');
      setScraping(false);
      loadJobs(); loadStats();
      // If skills were selected, auto-filter the table to show matching jobs
      if (state.skills.length) {
        toast(`Showing ${state.skills.length} skill filter(s) applied`, 'info');
      }
      break;
    }
  }
}
function setScraping(on) {
  state.scraping = on;
  state.activeRun = null;
  $('#btn-harvest').disabled = on;
  $('#btn-check').disabled = on;
  $('#btn-stop').disabled = !on;
  $('#harvest-label').textContent = on?'Scraping...':'Scrape jobs';
  $('#check-label').textContent = on?'Checking...':'Check for updates';
  updateNextHint();
}

// ── Actions ─────────────────────────────────────────────────────────────────
async function runPipeline() {
  const kw = state.keywords.join(', ').trim();
  const cats = state.categories;
  const skills = state.skills;
  // No requirement — can run with just keywords, just categories, just skills, or nothing
  consoleEl.innerHTML=''; setScraping(true); state.activeRun='harvest';
  log(`Scrape: kw="${kw}" cats=[${cats.length}] skills=[${skills.length}]`);
  try {
    await streamSSE('/api/pipeline/run', {
      keyword: kw,
      categories: cats,
      skills: skills,
      posted_since: null,
    });
  } catch(e) { log(`! ${e.message}`,'log-error'); setScraping(false); }
}

async function runCheck() {
  consoleEl.innerHTML=''; setScraping(true); state.activeRun='check';
  log($('#check-all').checked?'Checking ALL jobs...':'Checking New/Interested...');
  try {
    await streamSSE('/api/pipeline/check', {workers:null, recheck_all:$('#check-all').checked});
  } catch(e) { log(`! ${e.message}`,'log-error'); setScraping(false); }
}

async function applyFilters() {
  const pos = state.posFilters;
  const neg = state.negFilters;
  if (!pos.length && !neg.length) { toast('No filters set','info'); return; }
  try {
    const res = await api('/api/keywords/apply', {method:'POST', body:JSON.stringify({positive:pos, negative:neg})});
    const bits = [`Hidden: ${res.total_hidden}`];
    if (res.restored) bits.push(`restored: ${res.restored}`);
    toast(bits.join(' '), 'success');
    updateFilterHiddenUI(res.still_filter_hidden);
    loadJobs(); loadStats();
  } catch(e) { toast(e.message,'error'); }
}

async function restoreFilters() {
  try {
    const res = await api('/api/keywords/apply', {method:'POST', body:JSON.stringify({positive:[], negative:[], restore:true})});
    toast(`Restored ${res.restored} job(s) hidden by filters`, 'success');
    updateFilterHiddenUI(res.still_filter_hidden);
    loadJobs(); loadStats();
  } catch(e) { toast(e.message,'error'); }
}

function updateFilterHiddenUI(n) {
  const hint = $('#filter-hidden-hint');
  const btn = $('#btn-restore-filters');
  if (n > 0) {
    hint.style.display = '';
    hint.textContent = `${n} job(s) auto-hidden by these keywords — change or remove keywords and re-apply (or use the button) to bring them back. Jobs you hid yourself are never auto-restored.`;
    btn.style.display = '';
    btn.textContent = `Restore auto-hidden jobs (${n})`;
  } else {
    hint.style.display = 'none';
    btn.style.display = 'none';
  }
}

// ── Chip management ─────────────────────────────────────────────────────────
function addChip(inputId, arr) {
  const input = $(inputId);
  const val = input.value.trim();
  if (!val) return;
  if (!arr.includes(val)) arr.push(val);
  input.value='';
  renderChips();
}
function renderChips() {
  $('#kw-chips').innerHTML = state.keywords.map((k,i)=>
    `<span class="chip chip-tag">${esc(k)}<button class="chip-remove" data-t="kw" data-i="${i}">x</button></span>`).join('');
  $('#pos-chips').innerHTML = state.posFilters.map((k,i)=>
    `<span class="chip chip-pos">${esc(k)}<button class="chip-remove" data-t="pos" data-i="${i}">x</button></span>`).join('');
  $('#neg-chips').innerHTML = state.negFilters.map((k,i)=>
    `<span class="chip chip-neg">${esc(k)}<button class="chip-remove" data-t="neg" data-i="${i}">x</button></span>`).join('');
}
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.chip-remove');
  if (!btn) return;
  const i = parseInt(btn.dataset.i);
  if (btn.dataset.t==='kw') state.keywords.splice(i,1);
  else if (btn.dataset.t==='pos') state.posFilters.splice(i,1);
  else state.negFilters.splice(i,1);
  renderChips();
});

// ── Categories & Skills (checkbox lists) ───────────────────────────────────
let allCats = [], allSkills = [];

async function loadCategories() {
  try {
    allCats = await api('/api/skills/categories');
    renderCats('');
  } catch(e) {}
}
function renderCats(filter) {
  const f = (filter||'').toLowerCase();
  const el = $('#cat-list');
  const shown = allCats.filter(c=>!f||(c.name||'').toLowerCase().includes(f));
  el.innerHTML = shown.map(c=>{
    const slug = c.slug|| (c.name||'').toLowerCase().replace(/ /g,'-');
    const checked = state.categories.includes(slug);
    return `<label class="check-item"><input type="checkbox" data-cat="${esc(slug)}" ${checked?'checked':''}> <span>${esc(c.name||slug)}</span> <span class="cat-count">${c.count||''}</span></label>`;
  }).join('')||'<div class="text-muted" style="padding:4px">None found</div>';
}
async function loadSkills(filter='') {
  try {
    const p = filter?`?search=${encodeURIComponent(filter)}`:'';
    const skills = await api(`/api/skills${p}`);
    allSkills = skills;
    renderSkills(filter);
  } catch(e) {}
}
function renderSkills(filter) {
  const f = (filter||'').toLowerCase();
  const el = $('#skill-list');
  const shown = allSkills.filter(s=>!f||(s.name||'').toLowerCase().includes(f));
  el.innerHTML = shown.map(s=>{
    const checked = state.skills.includes(s.name);
    return `<label class="check-item"><input type="checkbox" data-skill="${esc(s.name)}" ${checked?'checked':''}> <span>${esc(s.name)}</span></label>`;
  }).join('')||'<div class="text-muted" style="padding:4px">None found</div>';
}

// ── Export ──────────────────────────────────────────────────────────────────
async function exportCSV() {
  try {
    const data = await api(`/api/jobs?${buildJobsParams(99999)}`);
    const cols=['id','job_id','title','company','work_type','salary','skills','status','scrape_status','posted_date','date_found','job_url','notes'];
    const csv=[cols.join(',')].concat(data.jobs.map(j=>cols.map(c=>`"${String(j[c]??'').replace(/"/g,'""')}"`).join(',')));
    const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([csv.join('\n')],{type:'text/csv'}));
    a.download=`jobs_${new Date().toISOString().slice(0,10)}.csv`; a.click();
    toast('Exported','success');
  } catch(e) { toast(e.message,'error'); }
}

// ── Event wiring ────────────────────────────────────────────────────────────
function init() {
  // Search keywords
  $('#kw-input').addEventListener('keydown', e=>{ if(e.key==='Enter'){e.preventDefault(); addChip('#kw-input',state.keywords);} });
  $('#kw-add').addEventListener('click', ()=>addChip('#kw-input',state.keywords));

  // Positive filters
  $('#pos-input').addEventListener('keydown', e=>{ if(e.key==='Enter'){e.preventDefault(); addChip('#pos-input',state.posFilters);} });
  $('#pos-add').addEventListener('click', ()=>addChip('#pos-input',state.posFilters));

  // Negative filters
  $('#neg-input').addEventListener('keydown', e=>{ if(e.key==='Enter'){e.preventDefault(); addChip('#neg-input',state.negFilters);} });
  $('#neg-add').addEventListener('click', ()=>addChip('#neg-input',state.negFilters));

  // Apply filters
  $('#btn-apply-filters').addEventListener('click', applyFilters);
  $('#btn-restore-filters').addEventListener('click', restoreFilters);

  // Harvest
  $('#btn-harvest').addEventListener('click', runPipeline);
  $('#btn-check').addEventListener('click', runCheck);
  $('#btn-stop').addEventListener('click', ()=>{ fetch('/api/pipeline/stop',{method:'POST'}); log('Stop sent','log-error'); });

  // Console
  $('#console-close').addEventListener('click', ()=>consoleEl.innerHTML='');

  // Toolbar
  let st;
  $('#search-box').addEventListener('input', e=>{ clearTimeout(st); st=setTimeout(()=>{state.search=e.target.value.trim();loadJobs();},300); });
  $('#filter-status').addEventListener('change', e=>{
    state.status = e.target.value;
    state.colFilters.status = [];
    // Picking "Hidden" while "Show hidden" is off would show nothing — enable it
    if (e.target.value === 'Hidden') { state.includeHidden = true; $('#filter-hidden').checked = true; }
    updateFunnelIndicators();
    loadJobs();
  });
  $('#filter-hidden').addEventListener('change', e=>{state.includeHidden=e.target.checked;loadJobs();});
  $('#filter-has-salary').addEventListener('change', e=>{
    state.hasSalaryOnly = e.target.checked;
    updateFunnelIndicators();
    loadJobs();
  });

  // ── Excel-style column headers: click = sort, ▼ = filter ─────────────────
  document.querySelectorAll('th.sortable').forEach(th => {
    const col = th.dataset.col;
    th.addEventListener('click', e => {
      if (e.target.closest('.th-funnel')) return; // funnel handled separately
      toggleSort(col);
    });
  });
  document.querySelectorAll('.th-funnel').forEach(fn => {
    fn.addEventListener('click', e => {
      e.stopPropagation();
      openColFilter(fn.dataset.filter, fn.closest('th'));
    });
  });

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
    if (th.dataset.col === state.sort)
      th.classList.add(state.order === 'asc' ? 'sorted-asc' : 'sorted-desc');
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

  // Table click
  tbody.addEventListener('click', e=>{
    const row=e.target.closest('tr[data-id]'); if(row) openDetail(+row.dataset.id);
  });

  // Detail
  $('#detail-close').addEventListener('click', closeDetail);
  detailOverlay.addEventListener('click', closeDetail);
  document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeDetail(); });

  $('#detail-status-select').addEventListener('change', async e=>{
    if(!currentJob||!e.target.value) return;
    await api(`/api/jobs/${currentJob.id}/status`,{method:'PATCH',body:JSON.stringify({status:e.target.value})});
    toast(`\u2192 ${e.target.value}`,'success'); closeDetail(); loadJobs(); loadStats();
  });
  $('#detail-save-notes').addEventListener('click', async ()=>{
    if(!currentJob) return;
    await api(`/api/jobs/${currentJob.id}/notes`,{method:'PATCH',body:JSON.stringify({notes:$('#detail-notes').value})});
    $('#notes-saved-hint').textContent='Saved'; setTimeout(()=>$('#notes-saved-hint').textContent='',2000);
  });
  $('#detail-save-followup').addEventListener('click', async ()=>{
    if(!currentJob) return;
    await api(`/api/jobs/${currentJob.id}/follow-up`,{method:'PATCH',body:JSON.stringify({follow_up:$('#detail-followup').value})});
    toast('Follow-up saved','success');
  });

  // Skills checkboxes — instant filter
  let skt;
  $('#skill-search').addEventListener('input', e=>{clearTimeout(skt); skt=setTimeout(()=>loadSkills(e.target.value),300);});
  $('#skill-list').addEventListener('change', e=>{
    const cb=e.target; if(cb.tagName!=='INPUT') return;
    const sk=cb.dataset.skill;
    if(cb.checked) { if(!state.skills.includes(sk)) state.skills.push(sk); }
    else state.skills = state.skills.filter(s=>s!==sk);
    applyVisibleFilter();
    loadJobs();
  });

  // Categories checkboxes — instant filter
  $('#cat-search').addEventListener('input', e=>renderCats(e.target.value));
  $('#cat-list').addEventListener('change', e=>{
    const cb=e.target; if(cb.tagName!=='INPUT') return;
    const cat=cb.dataset.cat;
    if(cb.checked) { if(!state.categories.includes(cat)) state.categories.push(cat); }
    else state.categories = state.categories.filter(c=>c!==cat);
    applyVisibleFilter();
  });

  // Export
  $('#btn-export').addEventListener('click', exportCSV);

  // Auto-run
  $('#auto-run-enabled').addEventListener('change', async (e) => {
    try { await api('/api/schedule', { method:'POST', body: JSON.stringify({ enabled: e.target.checked }) }); }
    catch (err) { toast(err.message, 'error'); }
  });
  $('#auto-run-interval').addEventListener('change', async (e) => {
    try { await api('/api/schedule', { method:'POST', body: JSON.stringify({ interval_hours: +e.target.value }) }); }
    catch (err) { toast(err.message, 'error'); }
  });
  $('#btn-notify').addEventListener('click', async () => {
    if (!('Notification' in window)) { toast('Notifications not supported in this browser', 'error'); return; }
    const p = await Notification.requestPermission();
    if (p === 'granted') {
      toast('Desktop alerts on', 'success');
      new Notification('Job Hunter', { body: 'You’ll get desktop alerts for new jobs, closures, and follow-ups.', tag: 'jobhunter' });
      $('#btn-notify').textContent = 'Desktop alerts: on ✓';
    } else {
      toast(p === 'denied' ? 'Notifications blocked — allow them in site settings' : 'Notification permission not granted', 'error');
    }
  });
  syncAutoRunUI();
}

// ── Auto-run status + server-pushed events ─────────────────────────────
async function syncAutoRunUI() {
  try {
    const s = await api('/api/schedule');
    $('#auto-run-enabled').checked = s.enabled;
    $('#auto-run-interval').value = String(s.interval_hours || 4);
    const st = $('#auto-run-status');
    if (st) st.textContent = s.last_run
      ? `last run ${s.last_run}${s.last_error ? ` — ${s.last_error}` : ''}`
      : 'never run yet';
  } catch (_) { /* server not up yet */ }
}

function desktopNotify(title, body) {
  if ('Notification' in window && Notification.permission === 'granted') {
    try { new Notification(title, { body: body || '', tag: 'jobhunter' }); } catch (_) {}
  }
}

function connectEvents() {
  const es = new EventSource('/api/events');
  es.addEventListener('new_jobs', (e) => {
    let d = {}; try { d = JSON.parse(e.data); } catch (_) {}
    toast(`🆕 ${d.count} new job(s)`, 'success');
    desktopNotify(`🆕 ${d.count} new job(s) on OJ.ph`, (d.titles || []).slice(0, 3).join(' · '));
    loadStats(); loadJobs();
  });
  es.addEventListener('alert', (e) => {
    let d = {}; try { d = JSON.parse(e.data); } catch (_) {}
    const msg = d.message || d.type || 'alert';
    toast(msg, d.type === 'error' ? 'error' : 'info');
    desktopNotify(`Job Hunter: ${d.type || 'alert'}`, msg);
    log(`⚠ ${msg}`, 'log-error');
  });
  es.addEventListener('schedule_done', (e) => {
    let d = {}; try { d = JSON.parse(e.data); } catch (_) {}
    const st = $('#auto-run-status');
    if (st) st.textContent = `last run: +${d.inserted||0} new · ${d.closed||0} closed · ${d.errors||0} err`;
    loadStats(); loadJobs();
  });
  es.onerror = () => { syncAutoRunUI(); };
}

document.addEventListener('DOMContentLoaded', ()=>{
  init();
  connectEvents();
  loadStats();
  loadJobs();
  loadCategories();
  loadSkills();
});

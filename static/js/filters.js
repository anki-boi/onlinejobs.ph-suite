/* W6.1: keyword/skill/category filters, chips, scrape scope, reset */
import { state, $, api, toast, esc } from './core.js';
import { loadJobs } from './jobs.js';
import { loadStats } from './stats.js';

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
async function loadSavedKeywords() {
  try {
    const b = await (await fetch('/api/keywords')).json();
    if (b.positive.length || b.negative.length) {
      state.posFilters = b.positive;
      state.negFilters = b.negative;
      renderChips();
    }
    if (b.still_filter_hidden) {
      const h = $('#filter-hidden-hint');
      h.style.display = 'block';
      h.textContent = `${b.still_filter_hidden} job(s) auto-hidden by the saved rules`;
    }
  } catch {}
}
async function loadScrapeScope() {
  try {
    const s = await (await fetch('/api/scrape-scope')).json();
    const parts = [s.keyword && `keywords: ${s.keyword}`,
                   s.categories.length && `categories: ${s.categories.join(', ')}`,
                   s.skills.length && `skills: ${s.skills.join(', ')}`].filter(Boolean);
    $('#scope-hint').textContent = parts.length
      ? `Auto-run scope: ${parts.join(' · ')}`
      : 'Auto-run is set to scrape everything.';
  } catch {}
}
async function saveScrapeScope() {
  const r = await (await fetch('/api/scrape-scope', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      keyword: state.keywords.join(', '),
      categories: state.categories,
      skills: state.skills,
    }),
  })).json();
  const what = r.keyword || r.categories.join(', ') || r.skills.join(', ') || 'everything';
  toast(`Auto-run will now scrape: ${what}`, 'success');
  loadScrapeScope();
}
async function resetAll() {
  if (!confirm('Delete ALL job listings?\n\nKept: saved keyword rules, scrape scope, scheduler settings, resume masters, backups.')) return;
  const b = await (await fetch('/api/jobs/reset', {method: 'POST'})).json();
  toast(`Deleted ${b.deleted_jobs} job(s). Settings were kept.`, 'success');
  loadStats(); loadJobs();
}

export { applyFilters, restoreFilters, updateFilterHiddenUI, addChip, renderChips, loadCategories, renderCats, loadSkills, renderSkills, loadSavedKeywords, loadScrapeScope, saveScrapeScope, resetAll };

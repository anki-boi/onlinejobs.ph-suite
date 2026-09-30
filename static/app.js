/* W6.1: entry point — wires events, boots the app (index.html loads this as type="module") */
import { state, $, api, toast, log, consoleEl, currentJob, detailOverlay, tbody } from './js/core.js';
import { loadJobs, exportCSV, toggleSort, openColFilter, updateFunnelIndicators, colFilterOpen } from './js/jobs.js';
import { loadStats, loadConfigBanner } from './js/stats.js';
import { runPipeline, runCheck, syncAutoRunUI, connectEvents, loadRuns } from './js/run.js';
import { addChip, applyFilters, restoreFilters, saveScrapeScope, resetAll, undoReset, loadSavedKeywords, loadScrapeScope, loadCategories, loadSkills, renderCats } from './js/filters.js';
import { initResumePanel, loadResumeStatus, loadBuiltCvs, buildCvClick, detailTailorClick, openDetail, closeDetail } from './js/resume.js';

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

  // Auto-run scrape scope + full reset
  $('#btn-save-scope').addEventListener('click', saveScrapeScope);
  $('#btn-reset').addEventListener('click', resetAll);
  $('#btn-undo-reset').addEventListener('click', undoReset);   // P6

  // Harvest
  $('#btn-harvest').addEventListener('click', runPipeline);
  $('#btn-check').addEventListener('click', runCheck);
  $('#btn-stop').addEventListener('click', ()=>{ fetch('/api/pipeline/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id: state.activeRunId||null})}); log('Stop sent','log-error'); });

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
  // B11: "Hide reposts" is a server-side filter now, so the count, the pager and
  // the CSV all agree with the screen instead of just blanking rows on this page.
  $('#filter-reposts').addEventListener('change', e=>{state.hideReposts=e.target.checked;loadJobs();});
  $('#filter-min-ats').addEventListener('change', e=>{state.minAts=e.target.checked;loadJobs();});
  // P1: the ATS floor is a slider on `fit` (0-60), not a hard-coded 50/100 that
  // hid nearly every job. Dragging re-queries; the number is echoed next to it.
  const fitSlider = $('#filter-min-fit'), fitValue = $('#fit-value');
  const syncFit = ()=>{ state.minAtsValue = Number(fitSlider.value)||0;
                        if (fitValue) fitValue.textContent = state.minAtsValue; };
  fitSlider.addEventListener('input', ()=>{ syncFit(); if (state.minAts) loadJobs(); });
  syncFit();
  // F9: money filters react while typing (debounced like the search box) — the old
  // `change` listener only fired on blur/Enter, so typing a bound did nothing.
  let svt;
  for (const id of ['filter-salary-min','filter-salary-max'])
    $(`#${id}`).addEventListener('input', ()=>{ clearTimeout(svt); svt=setTimeout(loadJobs,300); });
  $('#filter-salary-cur').addEventListener('change', loadJobs);  // W4.2: money filters
  $('#filter-has-salary').addEventListener('change', e=>{
    state.hasSalaryOnly = e.target.checked;
    updateFunnelIndicators();
    loadJobs();
  });

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

  // Table click / keyboard (F14: rows are focusable and Enter/Space opens them)
  tbody.addEventListener('click', e=>{
    const row=e.target.closest('tr[data-id]'); if(row) openDetail(+row.dataset.id);
  });
  tbody.addEventListener('keydown', e=>{
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row=e.target.closest('tr[data-id]');
    if (row) { e.preventDefault(); openDetail(+row.dataset.id); }
  });

  // Detail
  $('#detail-close').addEventListener('click', closeDetail);
  detailOverlay.addEventListener('click', closeDetail);
  // F14: Escape closes the topmost layer only. A column-filter popup is on top of
  // the drawer, and its own handler deals with it — the old global listener closed
  // both at once, losing the filter you were mid-edit on.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (colFilterOpen()) return;          // the popup's own handler closes it
    closeDetail();
  });

  // F13: on a phone the sidebar is a drawer behind this button.
  const sidebar = $('#sidebar'), sbToggle = $('#btn-sidebar');
  if (sbToggle) {
    sbToggle.addEventListener('click', () => {
      const collapsed = sidebar.classList.toggle('collapsed');
      sbToggle.setAttribute('aria-expanded', String(!collapsed));
    });
  }

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

  // Skills checkboxes — server-side filter (F6: no second client-side pass)
  let skt;
  $('#skill-search').addEventListener('input', e=>{clearTimeout(skt); skt=setTimeout(()=>loadSkills(e.target.value),300);});
  $('#skill-list').addEventListener('change', e=>{
    const cb=e.target; if(cb.tagName!=='INPUT') return;
    const sk=cb.dataset.skill;
    if(cb.checked) { if(!state.skills.includes(sk)) state.skills.push(sk); }
    else state.skills = state.skills.filter(s=>s!==sk);
    loadJobs();
  });

  // Categories checkboxes — server-side (F5: they used to be a client-side substring
  // match on title/company text, which hid rows for reasons nobody could name)
  $('#cat-search').addEventListener('input', e=>renderCats(e.target.value));
  $('#cat-list').addEventListener('change', e=>{
    const cb=e.target; if(cb.tagName!=='INPUT') return;
    const cat=cb.dataset.cat;
    if(cb.checked) { if(!state.categories.includes(cat)) state.categories.push(cat); }
    else state.categories = state.categories.filter(c=>c!==cat);
    loadJobs();
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

document.addEventListener('DOMContentLoaded', ()=>{
  init();
  connectEvents();
  loadStats();
  loadJobs();
  loadSavedKeywords();
  loadScrapeScope();
  loadCategories();
  loadSkills();
  loadConfigBanner();
  initResumePanel();
  loadResumeStatus();
  loadBuiltCvs();
  loadRuns();
  api('/api/resume/yamlcv-status').then(r=>{
    for (const id of ['btn-build-cv','detail-build-cv-btn']) {
      const b = $(`#${id}`);
      if (!b) continue;
      if (!r.available) {
        b.style.display = 'none';
      } else if (id === 'btn-build-cv') {
        b.addEventListener('click', ()=>{
          const sel = $('#resume-job-sel');
          if (!sel.value) { $('#resume-result').textContent = 'Pick a job to target first.'; return; }
          buildCvClick(+sel.value, 0, b, $('#resume-result'), sel);
        });
      } else {
        b.addEventListener('click', ()=>{
          if (!currentJob?.id) return;
          buildCvClick(currentJob.id, 1, b, $('#detail-ats'), null);
        });
      }
    }
  });
  $('#detail-tailor-btn')?.addEventListener('click', detailTailorClick);
});

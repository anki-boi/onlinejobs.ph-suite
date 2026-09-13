/* W6.1: pipeline actions (scrape/check SSE), auto-run status, event hub wiring */
import { state, $, api, toast, log, consoleEl, PHASE_RE } from './core.js';
import { loadJobs, insertStubRow } from './jobs.js';
import { loadStats, updateNextHint } from './stats.js';
import { loadResumeStatus } from './resume.js';

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
function handleSSE(ev, d) {
  if (ev === 'run_started' && d && d.run_id) state.activeRunId = d.run_id;
  // W5.4: identical run already in progress — attach its id, don't look like a failure
  if (ev === 'run_id' && d && d.status === 'already_running')
    toast('Same run already in progress — id ' + String(d.run_id).slice(0, 8), 'info');
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
  state.activeRunId = null;
  $('#btn-harvest').disabled = on;
  $('#btn-check').disabled = on;
  $('#btn-stop').disabled = !on;
  $('#harvest-label').textContent = on?'Scraping...':'Scrape jobs';
  $('#check-label').textContent = on?'Checking...':'Check for updates';
  updateNextHint();
}

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
async function syncAutoRunUI() {
  try {
    const s = await api('/api/schedule');
    $('#auto-run-enabled').checked = s.enabled;
    $('#auto-run-interval').value = String(s.interval_hours || 4);
    const st = $('#auto-run-status');
    if (st) {
      let txt = s.last_run
        ? `last run ${s.last_run}${s.last_status === 'stopped' ? ' — stopped' : s.last_error ? ` — ${s.last_error}` : ''}`
        : 'never run yet';
      // W2.6: another Job Hunter instance is running the pipeline on this DB
      if (s.instance_lock) {
        txt += s.instance_lock.stale
          ? ` · pipeline lock left over from pid ${s.instance_lock.pid} (auto-released)`
          : ` · pipeline running in another instance (pid ${s.instance_lock.pid})`;
      }
      st.textContent = txt;
    }
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


export { streamSSE, handleSSE, setScraping, runPipeline, runCheck, syncAutoRunUI, connectEvents };

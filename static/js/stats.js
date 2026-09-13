/* W6.1: stats bar, config banner, next-step hint */
import { state, $, api, toast, esc, fmtDate } from './core.js';

// W1.4: optional-features-off banner (e.g. LLM tailoring not configured).
// Dismissal is remembered per message — a different note shows again.
async function loadConfigBanner() {
  const el = $('#config-banner');
  if (!el) return;
  let r;
  try { r = await api('/api/config'); } catch (e) { return; }
  state.fx = (r.config||{}).fx_to_php || null;  // W4.2: rate for the salary-chip tooltip
  const msgs = r.features_off || [];
  if (!msgs.length) { el.hidden = true; el.innerHTML = ''; return; }
  const sig = JSON.stringify(msgs);
  if (localStorage.getItem('cb-dismissed') === sig) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `<div class="cb-msg">${msgs.map(esc).join('<br>')}</div>
    <button id="cb-reload" title="Re-read config.json + config.local.json">Reload config</button>
    <button id="cb-dismiss" title="Hide this note">✕</button>`;
  $('#cb-reload').onclick = async (b) => {
    b.textContent = 'Reloading…';
    try {
      const res = await api('/api/config/reload', { method: 'POST' });
      toast(res.changed ? 'Config reloaded — new values are live' : 'Config reloaded — no changes found');
      localStorage.removeItem('cb-dismissed');
    } catch (e) { toast('Reload failed: ' + e.message, 'err'); }
    loadConfigBanner();
  };
  $('#cb-dismiss').onclick = () => {
    localStorage.setItem('cb-dismissed', sig);
    el.hidden = true;
  };
}

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
    msg = 'Your database is empty. Click Scrape jobs to pull the latest OnlineJobs.ph listings (or set a keyword / category scope first). The auto-run also scrapes on its own schedule.';
  } else if (state.total === 0) {
    msg = 'Nothing matches this view — loosen a filter (or tick "Show hidden"), or run another scrape.';
  } else if ((state.stats['New']||0) > 0) {
    const n = state.stats['New'];
    msg = `${n} new job${n>1?'s':''} to review — click a row to open it, then set its status.`;
  }
  if (msg) { el.textContent = msg; el.style.display = ''; }
  else el.style.display = 'none';
}


export { loadConfigBanner, loadStats, updateNextHint };

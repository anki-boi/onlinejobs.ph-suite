/* W6.1: resume tailoring, ATS, built CVs, detail panel */
import { state, $, tbody, api, toast, esc, currentJob, detailPanel, detailOverlay, setCurrentJob } from './core.js';

async function openDetail(id) {
  try {
    const j = await api(`/api/jobs/${id}`);
    setCurrentJob(j);
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
    $('#detail-dl-docx').style.display = 'none';
    $('#detail-dl-docx').href = '';
    $('#detail-tailor-btn').disabled = false;
    $('#detail-tailor-btn').textContent = 'Tailor resume for this job';

    // ATS score of your best-fitting resume profile vs this job (deterministic, instant)
    const atsEl = $('#detail-ats');
    atsEl.textContent = 'Scoring…';
    try {
      const r = await api(`/api/resume/ats?job_id=${id}&auto=1`);
      $('#detail-ats-section').style.display = '';
      atsEl.textContent = formatAts(r);
    } catch(e) {
      atsEl.textContent = e.message;
    }

    detailPanel.classList.add('open');
    detailOverlay.classList.add('open');
  } catch(e) { toast(e.message,'error'); }
}
function closeDetail() { detailPanel.classList.remove('open'); detailOverlay.classList.remove('open'); setCurrentJob(null); }

function prefillResumeForm(doc, profileName) {
  const el = $('#resume-edit'); if (!el) return;
  const b = (doc && doc.basics) || {};
  $('#r-name').value = b.name || '';
  $('#r-email').value = b.email || '';
  $('#r-phone').value = b.phone || '';
  $('#r-location').value = b.location || '';
  $('#r-summary').value = b.summary || '';
  $('#r-skills').value = ((doc && doc.skills) || []).join('\n');
  $('#resume-edit-profile').textContent = 'Editing: ' + (profileName ? profileName : 'default profile (auto-pick)');
}

function formatAts(r) {
  const b = r.breakdown || {};
  const prof = r.profile ? `Best-fit profile: ${r.profile}\n` : '';
  const lines = [prof + `ATS score: ${r.total}/100`,
    `  skills ${b.skills||0}/40 · keywords ${b.keywords||0}/20 · format ${b.format||0}/25 · completeness ${b.completeness||0}/15`];
  if (r.matched_skills?.length) lines.push(`  matched: ${r.matched_skills.join(', ')}`);
  if (r.missing_skills?.length) lines.push(`  missing: ${r.missing_skills.join(', ')}`);
  for (const s of (r.suggestions||[])) lines.push(`  • ${s}`);
  return lines.join('\n');
}

async function loadResumeStatus() {
  try {
    const p = $('#resume-profile-sel')?.value || '';
    resumeMaster = await api(`/api/resume${p ? `?profile=${encodeURIComponent(p)}` : ''}`);
    $('#resume-status').textContent =
      `Master resume: ${resumeMaster.basics?.name||'?'} (${(resumeMaster.skills||[]).length} skills)`;
    prefillResumeForm(resumeMaster, p);
    // profile list
    try {
      const profs = await api('/api/resume/profiles');
      const psel = $('#resume-profile-sel');
      const curP = psel.value;
      psel.innerHTML = '<option value="">Auto-pick best profile</option>' +
        (profs.profiles||[]).map(p=>`<option value="${esc(p)}"${p===profs.default?' data-default="1"':''}>${esc(p)}${p===profs.default?' (default)':''}</option>`).join('');
      if ([...psel.options].some(o=>o.value===curP)) psel.value = curP;
    } catch(_)
    {}
    const sel = $('#resume-job-sel');
    const cur = sel.value;
    const opts = [...tbody.querySelectorAll('tr[data-id]')].map(row=>{
      const id = row.dataset.id;
      const title = row.querySelector('.cell-title')?.textContent || 'Untitled';
      const company = row.cells[3]?.textContent || '';
      return `<option value="${id}">${esc(title)}${company?' — '+esc(company):''}</option>`;
    }).join('');
    sel.innerHTML = '<option value="">Pick a job to target…</option>' + opts;
    if ([...sel.options].some(o=>o.value===cur)) sel.value = cur;
    const has = !!cur;
    $('#btn-ats').disabled = !has;
    $('#btn-tailor').disabled = !has;
    const bc = $('#btn-build-cv'); if (bc) bc.disabled = !has;
  } catch(e) {
    $('#resume-status').textContent = 'Master resume: ' + e.message;
  }
}

// profile param for API calls: explicit selection or auto-pick
function profileParam() {
  const p = $('#resume-profile-sel').value;
  return p ? `&profile=${encodeURIComponent(p)}` : '&auto=1';
}

function initResumePanel() {
  $('#resume-profile-sel').addEventListener('change', () => loadResumeStatus());
  $('#btn-save-resume').addEventListener('click', async () => {
    const b = {
      name: $('#r-name').value.trim(),
      email: $('#r-email').value.trim(),
      phone: $('#r-phone').value.trim(),
      location: $('#r-location').value.trim(),
      summary: $('#r-summary').value.trim(),
    };
    for (const k of ['name', 'email', 'phone', 'summary'])
      if (!b[k]) { toast('Add your ' + k, 'err'); return; }
    const skills = $('#r-skills').value.split('\n').map(s => s.trim()).filter(Boolean);
    const base = resumeMaster || {};
    const master = { basics: b, skills, work: base.work || [], education: base.education || [] };
    const p = $('#resume-profile-sel').value;
    const btn = $('#btn-save-resume');
    btn.disabled = true;
    try {
      await api('/api/resume', { method: 'PUT', body: JSON.stringify({ master, profile: p }) });
      toast('Resume saved', 'ok');
      await loadResumeStatus();
    } catch (e) { toast(e.message, 'err'); }
    btn.disabled = false;
  });
  $('#resume-job-sel').addEventListener('change', () => {
    const has = !!$('#resume-job-sel').value;
    $('#btn-ats').disabled = !has;
    $('#btn-tailor').disabled = !has;
    const bc = $('#btn-build-cv'); if (bc) bc.disabled = !has;
  });
  $('#btn-ats').addEventListener('click', async () => {
    const out = $('#resume-result');
    out.textContent = 'Scoring…';
    try { out.textContent = formatAts(await api(`/api/resume/ats?job_id=${$('#resume-job-sel').value}${profileParam()}`)); }
    catch(e) { out.textContent = e.message; }
  });
  $('#btn-tailor').addEventListener('click', async () => {
    const btn = $('#btn-tailor');
    btn.disabled = true;
    const out = $('#resume-result');
    out.textContent = 'Asking the LLM (can take a minute on local models)…';
    const p = $('#resume-profile-sel').value;
    const body = {job_id: Number($('#resume-job-sel').value)};
    if (p) body.profile = p; else body.auto = 1;
    try {
      const r = await api('/api/resume/tailor', {method:'POST', body:JSON.stringify(body)});
      out.textContent = (r.changed ? '' : '(LLM returned the master unchanged — check llm config)\n') + formatAts(r.score);
      $('#resume-dl-row').style.display = '';
      const pp = p ? `profile=${encodeURIComponent(p)}` : 'auto=1';
      $('#dl-docx').href = `/api/resume/export?job_id=${$('#resume-job-sel').value}&tailored=1&fmt=docx&${pp}`;
      $('#dl-txt').href = `/api/resume/export?job_id=${$('#resume-job-sel').value}&tailored=1&fmt=txt&${pp}`;
    } catch(e) { out.textContent = e.message; }
    btn.disabled = false;
  });
}

async function detailTailorClick() {
  if (!currentJob?.id) return;
  const btn = $('#detail-tailor-btn');
  btn.disabled = true;
  btn.textContent = 'Tailoring…';
  $('#detail-ats').textContent = 'Asking the LLM for the best-fitting profile (can take a minute on local models)…';
  try {
    const r = await api('/api/resume/tailor', {method:'POST', body:JSON.stringify({job_id: currentJob.id, auto: 1})});
    $('#detail-ats').textContent =
      (r.changed ? '' : '(LLM returned the master unchanged — check llm config)\n') + formatAts(r.score) +
      `\nTailored with: ${r.profile}`;
    const dl = $('#detail-dl-docx');
    dl.href = `/api/resume/export?job_id=${currentJob.id}&tailored=1&fmt=docx&auto=1`;
    dl.style.display = '';
  } catch(e) { $('#detail-ats').textContent = e.message; }
  btn.disabled = false;
  btn.textContent = 'Tailor resume for this job';
}

async function loadBuiltCvs() {
  const r = await api('/api/resume/built');
  const list = $('#built-cv-list');
  $('#built-cv-details').style.display = r.items.length ? '' : 'none';
  list.textContent = r.items.length ? '' : 'Nothing built yet.';
  for (const it of r.items) {
    const a = document.createElement('a');
    a.href = it.url; a.target = '_blank'; a.download = '';
    a.textContent = `${it.name} (${new Date(it.mtime*1000).toLocaleDateString()})`;
    a.style.display = 'block'; a.style.color = 'var(--blue)';
    a.addEventListener('click', ()=>window.open(it.url, '_blank'));
    list.appendChild(a);
  }
  return r;
}

async function buildCvClick(jobId, auto, btn, statusEl, jobSel) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = 'Building…';
  statusEl.textContent = 'Digesting your resume sources, drafting YAML, rendering… the 1-page loop can take a few minutes on local models.';
  try {
    const body = auto ? {job_id: jobId, auto: 1}
      : {job_id: jobId, profile: jobSel ? jobSel.value : ''};
    const r = await api('/api/resume/build', {method:'POST', body: JSON.stringify(body)});
    statusEl.textContent = `Built in ${r.rounds} round(s) — exactly 1 page. Profile: ${r.profile}. Opening PDF…`;
    const rounds = r.history.map(h => `  round ${h.round}: ${h.ok ? h.pages + ' page(s)' : 'FAIL ' + (h.error||'').slice(0,120)}`).join('\n');
    statusEl.appendChild(document.createTextNode('\n' + rounds));
    window.open(r.url, '_blank');
    loadBuiltCvs();
  } catch(e) { statusEl.textContent = e.message; }
  btn.disabled = false;
  btn.textContent = old;
}


export { openDetail, closeDetail, prefillResumeForm, formatAts, loadResumeStatus, profileParam, initResumePanel, detailTailorClick, loadBuiltCvs, buildCvClick };

/* Compliance Partner - live capture wiring.
 *
 * The page it loads into is a prototype: every control simulates its evidence.
 * This file replaces that simulation for exactly one control -
 *
 *     Workato  ->  Change management (CM-02)  ->  Prepare
 *
 * - which instead runs workato_sox_capture.py in the runner container and shows
 * what it is really doing. Every other system and control keeps the simulation,
 * and says so, because a page that looks identical whether or not it touched a
 * real system is a page that will eventually be believed when it should not be.
 *
 * Loaded after the prototype script, so it overrides by reassignment.
 */
'use strict';

(function () {
  const POLL_MS = 1500;
  const LOG_LINES = 14;

  // Filled in from /api/capabilities on load. Empty until then, so a page served
  // without a runner behind it simply stays a prototype.
  let LIVE = [];

  // What the Workspace and Project boxes offer, from /api/scope. Both are part of
  // the run's scope, so both are recorded on the job and in the manifest.
  let SCOPE = { workspaces: [], defaultWorkspace: '', projects: [] };
  let chosen = { workspace: '', project: 'all' };

  const liveFor = (system, controlId) =>
    LIVE.some(c =>
      c.system.toLowerCase() === String(system).toLowerCase() &&
      c.controlId.toLowerCase() === String(controlId).toLowerCase());

  const isLive = () => liveFor(app.system, controls[app.control].id);

  const bytes = n =>
    n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
      : n >= 1024 ? Math.round(n / 1024) + ' KB'
        : n + ' B';

  // What a run covered, in one line. Shown while it runs and on the finished
  // evidence, so a workbook is never read without knowing what was in scope.
  const scopeLine = j =>
    `${j.workspace || 'workspace ?'} · ${!j.project || j.project === 'all' ? 'all projects' : j.project}`;

  const PHASE_TEXT = {
    starting: 'Starting the capture session',
    awaiting_login: 'Waiting for sign-in',
    checking_workspace: 'Confirming the Workato workspace',
    capturing: 'Navigating Workato and capturing screens',
    building_workbook: 'Building the evidence workbook',
    done: 'Capture complete'
  };

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    let body = null;
    try { body = await res.json(); } catch (e) { /* empty or non-JSON error body */ }
    return { ok: res.ok, status: res.status, body };
  }

  // ---------------------------------------------------------------- rendering

  function steps(n) {
    return `<div class="steps">${['Prepare', 'Human validation', 'Management review', 'Sign off']
      .map((s, i) => `<div class="step ${i < n ? 'done' : i === n ? 'current' : ''}">${i + 1}. ${s}</div>`)
      .join('')}</div>`;
  }

  function logBox(lines) {
    if (!lines || !lines.length) return '';
    const tail = lines.slice(-LOG_LINES).join('\n');
    return `<pre class="cp-log" aria-label="Capture log">${esc(tail)}</pre>`;
  }

  function runningPanel(r) {
    const j = r.live;
    const phaseText = PHASE_TEXT[j.phase] || j.phase;
    const login = j.phase === 'awaiting_login'
      ? `<div class="notice amber" style="margin-top:14px">
           <strong>Sign in to Workato to continue</strong><br>
           The capture opened a browser in the runner. Sign in there once with SSO and it
           carries on by itself; the session is remembered for later runs.
           <div class="actions" style="margin-top:12px">
             <a class="btn primary" href="${esc(j.sessionUrl || '/session/vnc.html?autoconnect=1&resize=remote')}"
                target="_blank" rel="noopener">Open the browser session →</a>
           </div>
         </div>`
      : '';

    return steps(0) + `
      <div class="cp-live" role="status">
        <div class="cp-livehead">
          <span class="spinner"></span>
          <div>
            <strong>${esc(phaseText)}</strong>
            <small>${esc(scopeLine(j))} · ${esc(j.screenshots || 0)} screenshot(s) so far${j.phaseDetail ? ' · ' + esc(j.phaseDetail) : ''}</small>
          </div>
          <button class="textbtn" onclick="cpCancel()">Stop</button>
        </div>
        ${login}
        ${logBox(j.log)}
        <p class="sub">Reads Workato and takes screenshots. It never starts, stops, edits or
        deletes anything there.</p>
      </div>`;
  }

  function failedPanel(r) {
    const j = r.live;
    return steps(0) + `
      <div class="notice amber" style="margin-top:18px">
        <strong>${j.status === 'cancelled' ? 'Capture stopped' : 'Capture did not finish'}</strong><br>
        ${esc(j.error || 'The capture exited before producing a workbook.')}
      </div>
      ${logBox(j.log)}
      <div class="actions">
        <button class="btn primary" onclick="cpRetry()">Try again</button>
        <a class="textbtn" href="/api/jobs/${esc(j.id)}/log" target="_blank" rel="noopener">Full log</a>
      </div>
      <p class="sub">No workpaper was produced, so there is nothing to validate. Nothing was
      changed in Workato.</p>`;
  }

  function artifactCard(j) {
    const rows = (j.artifacts || []).map(a => {
      const label = a.kind === 'workbook' ? 'Excel workbook · one tab per control area'
        : a.kind === 'manifest' ? 'Capture manifest · URL and timestamp per screenshot'
          : a.kind === 'screenshots' ? `${a.count} full-screen captures` : a.kind;
      return `<div class="attachment">
                <span class="circle">${a.kind === 'workbook' ? '▤' : a.kind === 'screenshots' ? '▦' : '⋯'}</span>
                <div style="flex:1"><strong>${esc(a.name)}</strong><small>${esc(label)} · ${bytes(a.bytes)}</small></div>
                <a class="textbtn" href="/api/jobs/${esc(j.id)}/artifacts/${encodeURIComponent(a.name)}">Download</a>
              </div>`;
    }).join('');

    const warn = (j.warnings && j.warnings.length)
      ? `<div class="notice amber" style="margin-top:14px">
           <strong>${j.warnings.length} capture warning(s) - check these before signing off</strong>
           <div class="cp-warnlist">${j.warnings.map(w => `<div>${esc(w)}</div>`).join('')}</div>
         </div>`
      : '';

    return `<div class="cp-evidence">
              <div class="eyebrow">Captured evidence · ${esc(j.screenshots)} screenshots · ${esc(j.month)} · ${esc(scopeLine(j))}</div>
              ${rows || '<p class="sub">The run finished but produced no files.</p>'}
              <a class="textbtn" href="/api/jobs/${esc(j.id)}/log" target="_blank" rel="noopener">Capture log</a>
            </div>${warn}`;
  }

  // The validation / review / sign-off body, driven by the same handlers the
  // prototype uses, so a live run continues through the identical human workflow.
  function reviewBody(r) {
    if (r.stage === 'generated' || r.stage === 'returned') {
      const checks = ['Source evidence and population are complete',
        'Reconciliation and identified exceptions are accurate',
        'Conclusions are supported and ready for review'];
      return `${r.stage === 'returned'
        ? `<div class="notice amber" style="margin-top:18px"><strong>Management requested changes</strong><br>${esc(r.review)}</div>` : ''}
        <div class="validation">
          <h3>Preparer validation</h3>
          <p class="sub">Open the workbook and check the screenshots before completing these.</p>
          ${checks.map((t, i) => `<label><input type="checkbox" ${r.validated[i] ? 'checked' : ''} onchange="toggleValidation(${i},this.checked)">${t}</label>`).join('')}
          <label class="fieldlabel" for="prepComment">Validation notes <span style="font-weight:400">(optional)</span></label>
          <textarea id="prepComment" class="field" placeholder="Explain an exception or document your validation." oninput="rec().comment=this.value">${esc(r.comment)}</textarea>
          <div class="actions">
            <button id="submitBtn" class="btn primary" ${r.validated.every(Boolean) ? '' : 'disabled'} onclick="submitReview()">Submit for management review →</button>
          </div>
        </div>`;
    }
    if (r.stage === 'submitted') {
      return `<div class="notice" style="margin-top:20px"><strong>Ready for management review</strong><br>
                Preparer validation is complete. Management must review the evidence and record a decision.</div>
              <button class="btn primary" style="margin-top:17px" onclick="managementReview()">Open management review</button>
              <p class="sub">Prototype role switch · No notification is sent.</p>`;
    }
    return `<div class="notice" style="margin-top:20px;border-color:#cde9dd;background:#eff9f3;color:#286d52">
              <strong>✓ Management sign-off recorded</strong><br>Captured evidence for ${esc(app.period)} is complete.</div>
            <p class="sub" style="margin-top:10px">Reviewer: Demo management reviewer</p>
            ${r.review ? `<p class="sub">Decision note: ${esc(r.review)}</p>` : ''}`;
  }

  // The scope chosen before a run: which workspace to capture, and which projects.
  // Shown in place of the prototype's "Prepare workpaper" empty state, because for
  // a real capture these two answers decide what the evidence actually covers.
  function scopePanel() {
    const ws = SCOPE.workspaces.length ? SCOPE.workspaces : [SCOPE.defaultWorkspace || 'Production'];
    const wsOpts = ws.map(w =>
      `<option value="${esc(w)}" ${chosen.workspace === w ? 'selected' : ''}>${esc(w)}</option>`).join('');
    const prOpts = ['<option value="all"' + (chosen.project === 'all' ? ' selected' : '') +
      '>All projects</option>'].concat(
        SCOPE.projects.map(p =>
          `<option value="${esc(p)}" ${chosen.project === p ? 'selected' : ''}>${esc(p)}</option>`)).join('');

    return steps(0) + `
      <div class="cp-scope">
        <div class="eyebrow">Capture scope</div>
        <div class="cp-scopegrid">
          <div>
            <label class="fieldlabel" for="cpWorkspace">Workspace</label>
            <select id="cpWorkspace" class="select" onchange="cpSetWorkspace(this.value)">${wsOpts}</select>
          </div>
          <div>
            <label class="fieldlabel" for="cpProject">Project</label>
            <select id="cpProject" class="select" onchange="cpSetProject(this.value)">${prOpts}</select>
          </div>
        </div>
        <p class="sub">The capture confirms the workspace is visible on every page before
        it shoots, and stops rather than collect evidence from a different one. Only the
        chosen project gets a tab in the workbook, so a scoped run cannot be read as
        &ldquo;nothing changed&rdquo; elsewhere.</p>
        <div class="actions">
          <button class="btn primary" onclick="startPrepare()">Prepare workpaper</button>
        </div>
      </div>`;
  }

  function livePanel(r) {
    const j = r.live;
    if (j.status === 'running') return runningPanel(r);
    if (j.status !== 'succeeded') return failedPanel(r);
    const n = { generated: 1, returned: 1, submitted: 2, approved: 3 }[r.stage] ?? 1;
    return steps(n) + artifactCard(j) + reviewBody(r) +
      `<details class="audit"><summary style="font-size:11px;color:var(--muted);cursor:pointer">Activity history (${r.events.length})</summary>${r.events.map(e => `<div>${esc(e)}</div>`).join('')}</details>`;
  }

  // ------------------------------------------------------------------ polling

  let poller = null;

  function stopPolling() {
    if (poller) { clearInterval(poller); poller = null; }
  }

  function poll(recordKey, jobId) {
    stopPolling();
    poller = setInterval(async () => {
      const { ok, body } = await api(`/api/jobs/${jobId}`);
      if (!ok || !body) return;

      const r = app.records[recordKey];
      if (!r) { stopPolling(); return; }
      r.live = body;

      if (body.status !== 'running') {
        stopPolling();
        if (body.status === 'succeeded') {
          r.stage = 'generated';
          log(r, `Capture complete - ${body.screenshots} screenshots, workbook built`);
          if (body.warnings && body.warnings.length) {
            log(r, `${body.warnings.length} capture warning(s) recorded`);
          }
        } else {
          r.stage = 'idle';
          log(r, body.status === 'cancelled' ? 'Capture stopped by the operator'
            : `Capture failed: ${body.error || 'unknown error'}`);
        }
      }
      if (key() === recordKey && app.tab === 'prepare') render();
    }, POLL_MS);
  }

  // ------------------------------------------------------------- the overrides

  const protoWorkflow = workflow;
  workflow = function (r) {
    if (r.live) return livePanel(r);
    // A live control that has not been run yet asks for its scope first.
    if (isLive() && r.stage === 'idle') return scopePanel();
    return protoWorkflow(r);
  };

  window.cpSetWorkspace = function (v) { chosen.workspace = v; };
  window.cpSetProject = function (v) { chosen.project = v; };

  const protoStartPrepare = startPrepare;
  startPrepare = async function () {
    if (!isLive()) return protoStartPrepare();

    const r = rec();
    if (r.stage !== 'idle') return;
    const k = key();

    r.stage = 'preparing';
    r.live = { id: '', status: 'running', phase: 'starting', phaseDetail: '', log: [], screenshots: 0 };
    log(r, 'Live capture requested');
    render();

    const { ok, status, body } = await api('/api/prepare', {
      method: 'POST',
      body: JSON.stringify({
        system: app.system,
        controlId: controls[app.control].id,
        period: app.period,
        month: captureMonth(),
        workspace: chosen.workspace || SCOPE.defaultWorkspace,
        project: chosen.project || 'all'
      })
    });

    if (!ok) {
      r.stage = 'idle';
      r.live = {
        id: '', status: 'failed', log: [],
        error: (body && body.message) || `The runner answered ${status}.`
      };
      log(r, 'Capture could not start');
      if (key() === k) render();
      return;
    }

    r.live = body;
    log(r, `Capture started (job ${body.id})`);
    if (key() === k) render();
    poll(k, body.id);
  };

  // CM-02 is a MONTHLY control, and the capture labels its workbook and sheet
  // headers with a month. The page's period selector is quarterly, and mapping a
  // quarter onto a month would mean guessing a fiscal calendar - so the run is
  // labelled with the current month and the chosen period is recorded alongside
  // it on the job. To capture a different month, use the CLI:
  //   docker compose run --rm runner capture --month "August 2026"
  function captureMonth() {
    return new Date().toLocaleString('en-US', { month: 'long', year: 'numeric' });
  }

  window.cpCancel = async function () {
    const r = rec();
    if (!r.live || !r.live.id) return;
    await api(`/api/jobs/${r.live.id}/cancel`, { method: 'POST' });
    toast('Stopping the capture…');
  };

  window.cpRetry = function () {
    const r = rec();
    delete r.live;
    r.stage = 'idle';
    render();
    startPrepare();
  };

  // ------------------------------------------------------- badge on live rows

  // Badge every control row that is backed by a real capture, not just the
  // selected one - the point is to show at a glance which of the three are real.
  function markLiveRows() {
    if (app.tab !== 'prepare') return;
    document.querySelectorAll('#workspace .row').forEach(row => {
      const meta = row.querySelector('.meta');
      if (!meta || meta.querySelector('.cp-badge')) return;
      const control = controls.find(c => meta.textContent.includes(c.id));
      if (!control || !liveFor(app.system, control.id)) return;
      const b = document.createElement('span');
      b.className = 'cp-badge';
      b.textContent = 'Live capture';
      meta.appendChild(b);
    });
  }

  const protoRender = render;
  render = function () {
    protoRender();
    markLiveRows();
  };

  // ------------------------------------------------------------------ startup

  Promise.all([api('/api/capabilities'), api('/api/scope')])
    .then(([caps, sc]) => {
      if (!caps.ok || !caps.body || !caps.body.live) return;
      LIVE = caps.body.live;
      if (sc.ok && sc.body) {
        SCOPE = sc.body;
        chosen.workspace = SCOPE.defaultWorkspace || (SCOPE.workspaces[0] || '');
      }
      render();
    })
    .catch(() => { /* no runner: the page stays a prototype */ });
})();

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
  // evidence, so a workbook is never read without knowing what was in scope. The
  // second half depends on the control: a change review is scoped by project, an
  // access review by the period it is labelled with.
  const scopeLine = (j) => {
    const ws = j.workspace || 'workspace ?';
    if (String(j.controlId || '').toUpperCase() === 'UA-04') {
      return `${ws} · ${j.period || 'period ?'}`;
    }
    return `${ws} · ${!j.project || j.project === 'all' ? 'all projects' : j.project}`;
  };

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

  // The user access review's deliverable is the listing itself, so it is shown on
  // the page rather than left inside a download. Counts first, then the roster.
  const USER_ROWS_SHOWN = 60;

  function userCard(j) {
    const u = j.users;
    if (!u) return '';

    const tiles = [
      ['Total collaborators', u.total, ''],
      ['Active', u.active, 'good'],
      ['Inactive / suspended', u.inactive, ''],
      ['Pending invitation', u.pending, ''],
    ].concat(u.unknown ? [['Status unrecognised', u.unknown, 'warn']] : []);

    const shown = (u.rows || []).slice(0, USER_ROWS_SHOWN);
    const more = (u.rows || []).length - shown.length;

    const body = shown.map(r => {
      const cls = r.active === true ? 'active' : r.status === 'pending' ? 'pending'
        : r.active === false ? 'inactive' : 'unknown';
      const verdict = r.active === true ? 'Active' : r.status === 'pending' ? 'Pending'
        : r.active === false ? 'Inactive' : 'Unrecognised';
      return `<tr class="cp-u-${cls}">
                <td>${esc(r.name || '')}</td>
                <td>${esc(r.email || '')}</td>
                <td>${esc(r.role || '')}</td>
                <td>${esc(r.status_raw || '—')}</td>
                <td><span class="cp-pill cp-${cls}">${verdict}</span></td>
              </tr>`;
    }).join('');

    return `<div class="cp-users">
        <div class="eyebrow">Collaborators · ${esc(u.workspace || '')} · listed for ${esc(u.period || j.period || '')}</div>
        <div class="cp-tiles">
          ${tiles.map(([k, v, c]) => `<div class="cp-tile ${c}"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join('')}
        </div>
        ${u.total ? `<div class="cp-tablewrap"><table class="cp-utable">
            <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status as shown</th><th>Verdict</th></tr></thead>
            <tbody>${body}</tbody></table></div>
          ${more > 0 ? `<p class="sub">${more} more in the workbook and users.csv.</p>` : ''}`
        : `<div class="notice amber" style="margin-top:14px"><strong>No collaborators could be read</strong><br>
             The page opened but no rows were recognised. Check the screenshots - this is
             not evidence that the workspace has no users.</div>`}
        <p class="sub">Point-in-time listing taken ${esc((u.capturedAt || '').replace('T', ' ').slice(0, 19))}
        and labelled ${esc(u.period || '')}. Workato publishes no historical roster, so this
        evidences access as it stood when the capture ran.</p>
      </div>`;
  }

  function artifactCard(j) {
    const rows = (j.artifacts || []).map(a => {
      const label = a.kind === 'workbook' ? 'Excel workbook'
        : a.kind === 'manifest' ? 'Capture manifest · URL and timestamp per screenshot'
          : a.kind === 'screenshots' ? `${a.count} full-screen captures`
            : a.kind === 'users' ? 'Collaborator listing · full detail, JSON'
              : a.kind === 'userscsv' ? 'Collaborator listing · spreadsheet'
                : a.kind;
      return `<div class="attachment">
                <span class="circle">${a.kind === 'workbook' ? '▤' : a.kind === 'screenshots' ? '▦'
                  : (a.kind === 'users' || a.kind === 'userscsv') ? '◎' : '⋯'}</span>
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
  const scopeOf = () => {
    const c = LIVE.find(x =>
      x.system.toLowerCase() === app.system.toLowerCase() &&
      x.controlId.toLowerCase() === controls[app.control].id.toLowerCase());
    return (c && c.scope) || ['workspace'];
  };

  function scopePanel() {
    const needsProject = scopeOf().includes('project');
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
            <label class="fieldlabel" for="${needsProject ? 'cpProject' : 'cpPeriodShown'}">${needsProject ? 'Project' : 'Review period'}</label>
            ${needsProject
              ? `<select id="cpProject" class="select" onchange="cpSetProject(this.value)">${prOpts}</select>`
              : `<output id="cpPeriodShown" class="cp-periodout">${esc(chosenPeriod())}</output>`}
          </div>
        </div>
        <p class="sub" id="cpPeriodHint">The capture confirms the workspace is visible on every page
        before it reads anything, and stops rather than collect evidence from a different one.
        ${needsProject
          ? 'Only the chosen project gets a tab in the workbook, so a scoped run cannot be read as &ldquo;nothing changed&rdquo; elsewhere.'
          : 'The review period is the one set in Preparation Period above, shown here so there is no doubt which period a run will be labelled with. The listing is a point-in-time snapshot - Workato publishes no historical roster.'}</p>
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
    return steps(n) + userCard(j) + artifactCard(j) + reviewBody(r) +
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
    // Let the prototype's own validation speak: it toasts and returns.
    if (!periodIsValid()) return protoStartPrepare();
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
        period: chosenPeriod(),
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

  // The page now configures a real period per control - a quarter+year for the
  // access review, a start/end pair for change management - so use that instead
  // of app.period, which is only the initial default.
  function chosenPeriod() {
    try {
      if (typeof periodLabel === 'function') return periodLabel();
    } catch (e) { /* fall through */ }
    return app.period || '';
  }

  // Mirrors the prototype's own guard in startPrepare. A live run must not be
  // easier to start than a simulated one: beginning a capture with no period
  // would produce a workbook nobody could file against a review.
  function periodIsValid() {
    if (typeof periodConfig !== 'function') return true;
    let p;
    try { p = periodConfig(); } catch (e) { return true; }
    if (app.control === 0) return !!p.year && +p.year >= 2020 && +p.year <= 2100;
    if (app.control === 1) return !!p.start && !!p.end && !(p.end < p.start);
    return !!p.asof;
  }

  // CM-02 is monthly and the capture labels its workbook with a month. Derive it
  // from the chosen start date where there is one rather than guessing.
  function captureMonth() {
    try {
      if (typeof periodConfig === 'function') {
        const p = periodConfig();
        const d = p && p.start ? new Date(p.start) : null;
        if (d && !isNaN(d)) {
          return d.toLocaleString('en-US', { month: 'long', year: 'numeric' });
        }
      }
    } catch (e) { /* fall through */ }
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

  // Badge every control that is backed by a real capture, not just the selected
  // one - the point is to show at a glance which are real and which simulate.
  //
  // Found by text rather than by class: this used to look for ".row .meta",
  // which the page no longer uses, so the badges silently stopped appearing
  // while everything else still worked. Matching the control id where it is
  // actually written survives the next restyle too.
  function markLiveRows() {
    if (app.tab !== 'prepare') return;
    const ws = document.getElementById('workspace');
    if (!ws) return;
    ws.querySelectorAll('*').forEach(el => {
      if (el.children.length || el.querySelector('.cp-badge')) return;
      const text = (el.textContent || '').trim();
      // The control's own entry starts with its id ("UA-04 · Quarterly").
      // Elsewhere the id appears mid-sentence ("Selected Control · UA-04"),
      // which is a label, not a row to badge.
      const control = controls.find(c => text.startsWith(c.id));
      if (!control || !liveFor(app.system, control.id)) return;
      const b = document.createElement('span');
      b.className = 'cp-badge';
      b.textContent = 'Live capture';
      el.appendChild(b);
    });
  }

  const protoRender = render;
  render = function () {
    protoRender();
    markLiveRows();
  };

  // ------------------------------------------------------------------ startup

  // ----------------------------------------------------- who is signed in

  // The picker sends people here as /?system=Workato. Honour that, but only for a
  // system they are actually entitled to - the query string is the user's to edit,
  // so it decides what is SHOWN, never what is ALLOWED. /api/prepare re-checks
  // entitlement server-side on every run.
  function applyIdentity(me) {
    if (!me || !me.authRequired) return;

    // The page now has its own visibility model (visibleSystems(), scopes per
    // demo user), so rewriting the sidebar list here would put two systems of
    // entitlement in conflict and neither would be trustworthy. Okta decides
    // what /api/prepare will ALLOW - which it re-checks server-side on every
    // run - and the page decides what it shows.
    const wanted = new URLSearchParams(location.search).get('system');
    if (wanted) {
      const pool = (typeof visibleSystems === 'function' ? visibleSystems() : systems) || systems;
      const hit = pool.find(s => s.toLowerCase() === wanted.toLowerCase());
      if (hit) app.system = hit;
    }

    const right = document.querySelector('.topright');
    if (right && !document.getElementById('cpWho')) {
      const who = document.createElement('span');
      who.id = 'cpWho';
      who.className = 'cp-who';
      who.innerHTML =
        `<a class="textbtn" href="/choose" title="Work on a different system">Switch system</a>
         <span class="cp-whoname" title="${esc(me.email || '')}">${esc(me.name || me.email || '')}</span>
         <a class="textbtn" href="/auth/logout">Sign out</a>`;
      right.insertBefore(who, right.firstChild);
    }
    render();
  }

  api('/auth/me')
    .then(({ ok, body }) => { if (ok) applyIdentity(body); })
    .catch(() => { /* sign-in off or runner down: the page is unchanged */ });

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

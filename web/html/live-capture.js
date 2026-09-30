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

  // The administration page gains the inventory, above the user list: you assign
  // people to systems, so the systems have to be the thing you can change first.
  if (typeof adminView === 'function') {
    const protoAdminView = adminView;
    adminView = function () {
      const html = protoAdminView();
      const panel = systemsPanel();
      // The directory is only readable by an admin, and only this view needs it.
      if (!DIRECTORY.length) refreshDirectory().then(render);
      // adminView ends with the page footer. Appending put the inventory BELOW
      // it, which reads as something bolted on after the page had finished.
      // Insert before the trailing explanatory block instead, so it sits with
      // the other administration cards.
      const both = assignmentPanel() + panel;
      const anchors = ['<details class="audit"', '<details', '<div class="footer"'];
      for (const a of anchors) {
        const i = html.lastIndexOf(a);
        if (i > 0) return html.slice(0, i) + both + html.slice(i);
      }
      return html + both;
    };
  }

  const protoRender = render;
  render = function () {
    protoRender();
    markLiveRows();
    ensureSystemPicker();
    syncSystemPicker();
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

  // --------------------------------------------- the SOX system inventory
  //
  // The list lived in three places - a sidebar array, a longer inventory beside
  // it, and a constant in Python - so adding a system meant editing code in two
  // languages and rebuilding. It is reference data an administrator maintains,
  // so it comes from the runner and is edited on the Administration page.
  let INVENTORY = { systems: [], wired: [] };

  function applyInventory(d) {
    if (!d || !Array.isArray(d.systems) || !d.systems.length) return;
    INVENTORY = { systems: d.systems.slice(), wired: (d.wired || []).slice() };

    // These are const arrays; replace their CONTENTS so every existing reader
    // (visibleSystems, the sidebar, the scope picker) sees the change.
    const swap = (arr, next) => {
      if (!Array.isArray(arr)) return;
      arr.length = 0;
      next.forEach(x => arr.push(x));
    };
    if (typeof allSoxSystems !== 'undefined') swap(allSoxSystems, INVENTORY.systems);
    // accessScopes is derived from the inventory at load; it would otherwise
    // still offer systems that have been removed.
    if (typeof accessScopes !== 'undefined') {
      const extra = (typeof auditCatalog !== 'undefined' ? auditCatalog : []).map(c => c.scope);
      swap(accessScopes, [...new Set([...INVENTORY.systems, ...extra])].sort());
    }
    // The sidebar shortlist: what this person may actually reach.
    // `systems` still backs the page's own helpers, so keep it to what this
    // person may reach - it is simply no longer drawn as a sidebar list.
    if (typeof systems !== 'undefined' && typeof visibleSystems === 'function') {
      swap(systems, visibleSystems());
      if (!systems.includes(app.system) && systems.length) app.system = systems[0];
    }
  }

  function refreshInventory() {
    return api('/api/sox-systems')
      .then(({ ok, body }) => { if (ok) applyInventory(body); })
      .catch(() => {});
  }

  // ---------------------------------------- assigning systems to people
  //
  // The page's Edit Access modal does have scopes, but it mixes the SOX systems
  // into one 41-item list with audit process scopes, so assigning someone two
  // systems means finding them among things that are not systems at all. This
  // does the one job plainly, and writes straight to the directory.
  let DIRECTORY = [];

  function refreshDirectory() {
    return api('/api/users')
      .then(({ ok, body }) => { if (ok && body.users) DIRECTORY = body.users; })
      .catch(() => {});
  }

  function assignmentPanel() {
    if (!DIRECTORY.length) {
      return `<section class="card cp-assign"><div class="cardhead"><div>
          <h2>SOX system assignment</h2>
          <div class="sub">Loading the directory…</div></div></div></section>`;
    }

    const rows = DIRECTORY.map(u => {
      if (u.admin) {
        return `<tr>
            <td><strong>${esc(u.name)}</strong><small>${esc(u.role)}</small></td>
            <td class="cp-allsys">Every system, because the role is ${esc(u.role)}.
              Change the role to assign individually.</td>
          </tr>`;
      }
      const have = new Set((u.scopes || []).map(x => x.toLowerCase()));
      const chips = INVENTORY.systems.map(name => {
        const on = have.has(name.toLowerCase());
        return `<button type="button" class="cp-chip ${on ? 'on' : ''}"
                   data-uid="${esc(u.id)}" data-system="${esc(name)}"
                   aria-pressed="${on}">${esc(name)}</button>`;
      }).join('');
      return `<tr>
          <td><strong>${esc(u.name)}</strong><small>${esc(u.role)}</small></td>
          <td><div class="cp-chips">${chips}</div>
              <div class="cp-count">${(u.scopes || []).length} assigned</div></td>
        </tr>`;
    }).join('');

    return `<section class="card cp-assign">
        <div class="cardhead"><div>
          <h2>SOX system assignment</h2>
          <div class="sub">Click a system to assign or remove it. Saved immediately;
            the person sees it in their Working on list next time they load the page.</div>
        </div></div>
        <div class="pad"><table class="cp-assigntable"><tbody>${rows}</tbody></table></div>
      </section>`;
  }

  // Delegated once: the panel is rebuilt on every render, so per-button
  // listeners would be re-attached (or leak) each time.
  document.addEventListener('click', async (ev) => {
    const chip = ev.target.closest && ev.target.closest('.cp-assign .cp-chip');
    if (!chip) return;
    ev.preventDefault();
    const uid = chip.dataset.uid;
    const system = chip.dataset.system;
    const user = DIRECTORY.find(u => u.id === uid);
    if (!user) return;

    const have = (user.scopes || []).slice();
    const at = have.findIndex(x => x.toLowerCase() === system.toLowerCase());
    if (at >= 0) have.splice(at, 1); else have.push(system);

    chip.disabled = true;
    const { ok, body } = await api('/api/users/' + encodeURIComponent(uid), {
      method: 'PUT',
      body: JSON.stringify({ name: user.name, role: user.role, scopes: have })
    });
    chip.disabled = false;
    if (!ok) {
      toast((body && body.message) || 'That assignment was not saved.');
      return;
    }
    user.scopes = body.user.scopes;
    render();
    toast(`${user.name}: ${at >= 0 ? 'removed' : 'assigned'} ${system}`);
  });

  function systemsPanel() {
    const wired = new Set(INVENTORY.wired.map(w => w.toLowerCase()));
    const rows = INVENTORY.systems.map(name => {
      const locked = wired.has(name.toLowerCase());
      return `<li class="cp-sysrow">
          <span class="cp-sysname">${esc(name)}</span>
          ${locked
            ? '<span class="cp-badge">Capture wired</span>'
            : `<button class="textbtn cp-sysdel" onclick="cpRemoveSystem(${JSON.stringify(name).replace(/"/g, '&quot;')})"
                       aria-label="Remove ${esc(name)}">Remove</button>`}
        </li>`;
    }).join('');

    return `<section class="card cp-systems">
        <div class="cardhead">
          <div>
            <h2>SOX systems</h2>
            <div class="sub">The inventory people can be assigned to. ${INVENTORY.systems.length} systems.</div>
          </div>
        </div>
        <div class="pad">
          <div class="cp-sysadd">
            <label class="fieldlabel" for="cpNewSystem">Add a system</label>
            <div class="cp-sysaddrow">
              <input id="cpNewSystem" class="select" placeholder="e.g. Snowflake"
                     onkeydown="if(event.key==='Enter'){event.preventDefault();cpAddSystem()}">
              <button class="btn primary" onclick="cpAddSystem()">Add</button>
            </div>
            <p id="cpSysErr" class="err" hidden></p>
          </div>
          <ul class="cp-syslist">${rows}</ul>
          <p class="sub">Removing a system also removes it from everyone assigned to it &mdash;
          an assignment to something that no longer exists is one nobody can see or revoke.
          A system a capture reads cannot be removed: the control would still be offered and
          could not be run.</p>
        </div>
      </section>`;
  }

  window.cpAddSystem = async function () {
    const input = document.getElementById('cpNewSystem');
    const err = document.getElementById('cpSysErr');
    if (!input) return;
    const name = input.value.trim();
    if (!name) return;
    const { ok, body } = await api('/api/sox-systems',
      { method: 'POST', body: JSON.stringify({ name }) });
    if (!ok) {
      if (err) { err.textContent = (body && body.message) || 'Could not add that.'; err.hidden = false; }
      return;
    }
    input.value = '';
    if (err) err.hidden = true;
    applyInventory(body);
    render();
    toast(name + ' added to the inventory.');
  };

  window.cpRemoveSystem = async function (name) {
    const { ok, body } = await api('/api/sox-systems/' + encodeURIComponent(name),
      { method: 'DELETE' });
    if (!ok) {
      toast((body && body.message) || 'Could not remove that.');
      return;
    }
    applyInventory(body);
    render();
    toast(name + ' removed, and unassigned from anyone who had it.');
  };

  // ------------------------------------------- choosing what to work on
  //
  // The sidebar listed every system in the inventory, which put a 24-item
  // catalogue in front of someone entitled to two. What a person needs is the
  // one they are working on now, so the choice moved next to their name and
  // offers only what they are assigned.

  function ensureSystemPicker() {
    const right = document.querySelector('.topright');
    if (!right || document.getElementById('cpSystemPick')) return;

    const wrap = document.createElement('label');
    wrap.className = 'cp-syspick';
    wrap.htmlFor = 'cpSystemPick';
    wrap.innerHTML = '<span class="cp-syspick-label">Working on</span>';

    const sel = document.createElement('select');
    sel.id = 'cpSystemPick';
    sel.className = 'cp-syspick-select';
    sel.addEventListener('change', () => {
      if (typeof selectSystem === 'function') selectSystem(sel.value);
      else { app.system = sel.value; render(); }
    });
    wrap.appendChild(sel);
    right.insertBefore(wrap, right.firstChild);
  }

  function syncSystemPicker() {
    const sel = document.getElementById('cpSystemPick');
    if (!sel) return;
    const mine = (typeof visibleSystems === 'function' ? visibleSystems() : []) || [];

    if (!mine.length) {
      sel.replaceChildren(new Option('No systems assigned', ''));
      sel.disabled = true;
      sel.title = 'An administrator assigns systems under User Administration';
      return;
    }
    sel.disabled = false;
    sel.title = mine.length + ' system' + (mine.length > 1 ? 's' : '') + ' assigned to you';

    // Rebuild only when the set changed, so an open dropdown is not yanked shut.
    const current = [...sel.options].map(o => o.value).join('\u0000');
    if (current !== mine.join('\u0000')) {
      sel.replaceChildren(...mine.map(s => new Option(s, s)));
    }
    if (!mine.includes(app.system)) {
      app.system = mine[0];
      if (typeof selectSystem === 'function') { /* caller re-renders */ }
    }
    sel.value = app.system;
  }

  // ------------------------------------------------- identity from the server
  //
  // The page shipped with a "Preview As" dropdown over an in-memory user list:
  // identity was self-selected and an administrator's assignment vanished on
  // reload. Both are replaced here. activeUser() answers from the server, so
  // isAdmin(), canModule() and visibleSystems() - which the page already gets
  // right - start describing the person who actually signed in.
  function adoptServerUser(me) {
    if (!me || !me.user) return;
    const u = me.user;

    // Shape it exactly as the page builds its own records, or its helpers break.
    const record = {
      id: u.id, name: u.name, role: u.role,
      modules: (u.modules || []).slice(),
      scopes: u.admin
        ? (typeof accessScopes !== 'undefined' ? accessScopes.slice() : (u.scopes || []))
        : (u.scopes || []),
      prep: [0, 1, 2], monitor: [0, 1, 2], audit: '*'
    };

    if (typeof demoUsers !== 'undefined' && Array.isArray(demoUsers)) {
      const i = demoUsers.findIndex(x => x.id === record.id);
      if (i < 0) demoUsers.push(record); else demoUsers[i] = record;
      try { activeUserId = record.id; } catch (e) { /* const: overridden below */ }
    }
    // Authoritative regardless of how the page stored it.
    activeUser = () => record;

    // A non-admin must not be able to become someone else from a dropdown.
    if (!u.admin) {
      const sel = document.getElementById('demoUser');
      if (sel) {
        const only = document.createElement('option');
        only.value = record.id;
        only.textContent = record.name + ' · ' + record.role;
        sel.replaceChildren(only);
        sel.disabled = true;
        sel.title = 'You are signed in as ' + record.name;
      }
    }
    if (u.unlisted) {
      toast('You are signed in but not in the user directory - no systems are assigned yet.');
    }
    render();
  }

  // Persist what the administration screen changes. The page's own save updated
  // an array in memory, so a grant was invisible to the person it was granted to
  // and gone on reload.
  function wrapAdminSave() {
    if (typeof saveDemoUser !== 'function') return;
    const protoSave = saveDemoUser;
    saveDemoUser = function () {
      const draft = (typeof userDraft !== 'undefined' && userDraft)
        ? { id: userDraft.id, name: userDraft.name, role: userDraft.role,
            scopes: (userDraft.scopes || []).slice() }
        : null;
      protoSave();                       // keeps the page's own validation and toast
      if (!draft) return;
      api('/api/users/' + encodeURIComponent(draft.id),
          { method: 'PUT', body: JSON.stringify(draft) })
        .then(({ ok, body }) => {
          if (!ok) toast((body && body.message) || 'Saved on screen, but not stored.');
        })
        .catch(() => toast('Saved on screen, but the directory could not be written.'));
    };

    if (typeof confirmRemoveDemoUser === 'function') {
      const protoRemove = confirmRemoveDemoUser;
      confirmRemoveDemoUser = function (id) {
        protoRemove(id);
        api('/api/users/' + encodeURIComponent(id), { method: 'DELETE' })
          .then(({ ok, body }) => {
            if (!ok) toast((body && body.message) || 'Removed on screen only.');
          }).catch(() => {});
      };
    }
  }

  Promise.all([api('/api/whoami'), api('/api/sox-systems')])
    .then(([me, inv]) => {
      if (inv.ok) applyInventory(inv.body);
      if (me.ok) { adoptServerUser(me.body); wrapAdminSave(); }
    })
    .catch(() => { /* runner down: the page keeps its own demo identity */ });

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

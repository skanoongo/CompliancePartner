/* Chart-first Workday/Okta termination monitoring from an imported Workato CSV. */
'use strict';
(function () {
  const endpoint = '/api/termination-reconciliation';
  const state = { report: null, loading: false, loaded: false, error: '', selected: null, filter: null, query: '', timelineId: null, canImport: false };
  const categories = {
    OK: { color: '#139879', title: 'OK per recipe', note: 'No follow-up under the recipe classification' },
    MISSED: { color: '#d04b54', title: 'Missing Okta evidence', note: 'Open these records for investigation' },
    LATE: { color: '#b67418', title: 'Late per recipe', note: 'Review the deactivation timing' },
    REVIEW: { color: '#b67418', title: 'Unclassified results', note: 'Review these results before drawing a conclusion' },
    REHIRED: { color: '#245ac3', title: 'Rehired / converted', note: 'Account retained per recipe; view context' }
  };
  const dateLabel = value => value ? new Date(value + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : 'No event in log';
  const controlsHTML = () => `<div class="tm-control-toolbar"><div class="tm-controls" aria-label="Monitoring controls">${controlEntries('monitor').map(({ c, i }) => `<button type="button" aria-pressed="${app.check === i}" onclick="selectCheck(${i})">${esc(c.name)}</button>`).join('')}</div>${state.report ? `<div class="tm-actions">${importHTML()}${reloadHTML()}</div>` : ''}</div>`;
  const importHTML = () => isAdmin() ? `<label class="btn primary tm-file">Import CSV<input id="tmImport" type="file" accept=".csv,text/csv" aria-label="Import Workato termination reconciliation CSV" onchange="tmImportCSV(this)"></label>` : '';
  const reloadHTML = () => `<button class="btn" onclick="tmReload()" ${state.loading ? 'disabled' : ''}>${state.loading ? 'Loading…' : 'Reload snapshot'}</button>`;
  const headerHTML = () => `<div class="tm-header"><div><div class="eyebrow">Compliance Monitoring / Workday + Okta</div><h1>Employee &amp; Contractor Termination</h1><p class="sub">Workday vs Okta · Reconciliation overview</p></div><div class="tm-actions">${state.report?.source?.url ? `<a class="btn" href="${esc(state.report.source.url)}" target="_blank" rel="noopener noreferrer">Open source CSV ↗</a>` : ''}${state.report ? '<button class="btn primary" onclick="tmExport()">Export review</button>' : importHTML() + reloadHTML()}</div></div>`;

  async function load() {
    if (state.loading) return;
    state.loading = true;
    state.error = '';
    state.report = null;
    state.selected = null;
    state.filter = null;
    state.query = '';
    state.timelineId = null;
    try {
      const response = await fetch(endpoint, { cache: 'no-store' });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || 'The reconciliation snapshot could not be loaded.');
      state.canImport = body.canImport;
      state.report = body.available ? body : null;
    } catch (error) {
      state.error = error.message;
    } finally {
      state.loading = false;
      state.loaded = true;
      if (app.tab === 'monitor' && app.check === 0) render();
    }
  }

  function chartHTML(summary) {
    let angle = 0;
    const point = degrees => { const a = (degrees - 90) * Math.PI / 180; return [180 + 139 * Math.cos(a), 180 + 139 * Math.sin(a)]; };
    const paths = Object.entries(categories).map(([bucket, category]) => {
      const count = summary.counts[bucket];
      if (!count) return '';
      const sweep = count / summary.total * 360;
      let geometry;
      if (sweep >= 359.999) geometry = '<circle cx="180" cy="180" r="139"';
      else {
        const a = point(angle), b = point(angle + sweep);
        geometry = `<path d="M ${a[0]} ${a[1]} A 139 139 0 ${sweep > 180 ? 1 : 0} 1 ${b[0]} ${b[1]}"`;
      }
      angle += sweep;
      const interaction = bucket === 'OK' ? 'role="img"' : `role="button" tabindex="0" aria-controls="tmDetails" aria-expanded="${state.selected === bucket}" onclick="tmOpen('${bucket}')" onkeydown="tmChartKey(event,'${bucket}')"`;
      return `${geometry} fill="none" stroke="${category.color}" stroke-width="43" class="tm-segment" data-bucket="${bucket}" ${interaction} aria-label="${count} workers: ${category.title}"><title>${category.title}: ${count} (${(count / summary.total * 100).toFixed(1)}%)</title></${sweep >= 359.999 ? 'circle' : 'path'}>`;
    }).join('');
    return `<div class="tm-chart"><svg viewBox="0 0 360 360" aria-label="Reconciliation outcome chart">${paths}</svg><div class="tm-chart-center"><strong>${summary.total}</strong><span>workers</span></div></div>`;
  }

  function legendHTML(summary) {
    return Object.entries(categories).filter(([key]) => summary.counts[key]).map(([key, category]) => {
      const count = summary.counts[key], tag = key === 'OK' ? 'div' : 'button';
      const interaction = key === 'OK' ? '' : `type="button" data-tm-category="${key}" aria-controls="tmDetails" aria-expanded="${state.selected === key}" onclick="tmOpen('${key}')"`;
      return `<${tag} ${interaction} class="tm-outcome ${state.selected === key && !state.filter ? 'selected' : ''}"><span class="tm-dot" style="background:${category.color}"></span><span class="tm-outcome-text"><strong>${key === 'OK' ? 'OK' : category.title}</strong></span><span class="tm-count">${count}</span><span class="tm-legend-percent">${(count / summary.total * 100).toFixed(1)}%</span></${tag}>`;
    }).join('');
  }

  const typeLabel = (type, count) => `${count} ${(type || 'worker').toLowerCase()}${count === 1 ? '' : 's'}`;
  const timingKey = r => {
    const days = r.days === '' ? NaN : Number(r.days);
    return !Number.isFinite(days) ? 'unknown' : days < 0 ? 'before' : days === 0 ? 'same' : days === 1 ? 'one' : days === 2 ? 'two' : days === 3 ? 'three' : days <= 5 ? 'fourfive' : 'long';
  };
  function timingSeries() {
    const records = state.report.records.filter(r => r.bucket === 'OK');
    const long = records.filter(r => timingKey(r) === 'long').map(r => Number(r.days));
    const min = Math.min(...long), max = Math.max(...long);
    const labels = { before: 'Before term', same: 'Same day', one: '1 day', two: '2 days', three: '3 days', fourfive: '4–5 days', long: long.length ? (min === max ? `${min} days` : `${min}–${max} days`) : 'Over 5 days', unknown: 'Not provided' };
    return Object.entries(labels).map(([key, label]) => ({ key, label, count: records.filter(r => timingKey(r) === key).length })).filter(row => row.count);
  }

  function kpisHTML(summary) {
    const types = Object.keys(summary.workerTypes).sort((a, b) => (a === 'Employee' ? -1 : b === 'Employee' ? 1 : a.localeCompare(b)));
    const missingTypes = [...new Set(state.report.records.filter(r => r.bucket === 'MISSED').map(r => r.type))];
    const missingNote = summary.counts.MISSED === 2 && missingTypes.length === 1 && missingTypes[0] === 'Contractor' ? 'Both contractors' : summary.counts.MISSED ? 'Requires evidence follow-up' : 'No missing events flagged';
    return `<div class="tm-kpis"><section class="tm-kpi"><h2>Workers reviewed</h2><strong id="tmKpiTotal">${summary.total}</strong><p>${esc(types.map(t => typeLabel(t, summary.workerTypes[t])).join(' · '))}</p></section><section class="tm-kpi"><h2>Recipe status: OK</h2><strong id="tmKpiOK" class="tm-teal">${(summary.counts.OK / summary.total * 100).toFixed(1)}%</strong><p>${summary.counts.OK} of ${summary.total} workers</p></section><section class="tm-kpi"><h2>Missing Okta evidence</h2><strong id="tmKpiMissing" class="tm-red">${summary.counts.MISSED}</strong><p>${missingNote}</p></section><section class="tm-kpi"><h2>Entry-adjusted cases</h2><strong id="tmKpiAdjusted" class="tm-amber">${summary.entryAdjusted}</strong><p>Included in the ${summary.counts.OK} OK results</p></section></div>`;
  }

  function workersHTML() {
    const groups = [...new Set(state.report.records.map(r => r.type))].sort((a, b) => (a === 'Employee' ? -1 : b === 'Employee' ? 1 : a.localeCompare(b)));
    const order = ['OK', 'REHIRED', 'MISSED', 'LATE', 'REVIEW'];
    const rows = groups.map(type => {
      const records = state.report.records.filter(r => r.type === type);
      const counts = Object.fromEntries(order.map(key => [key, records.filter(r => r.bucket === key).length]));
      const segments = order.filter(key => counts[key]).map(key => {
        const width = counts[key] / records.length * 100;
        const tag = key === 'OK' ? 'span' : 'button';
        const action = key === 'OK' ? '' : `type="button" data-worker-type="${esc(type)}" data-bucket="${key}" onclick="tmOpen(this.dataset.bucket,this.dataset.workerType)" aria-label="${counts[key]} ${esc(type || 'worker')} workers: ${categories[key].title}"`;
        return `<${tag} ${action} class="tm-stack-segment" data-stack-bucket="${key}" style="width:${width}%;background:${categories[key].color}" title="${counts[key]} ${categories[key].title}">${key === 'OK' && width > 40 ? `<span>${width === 100 ? '100' : width.toFixed(1)}%</span>` : ''}</${tag}>`;
      }).join('');
      const caption = `${counts.OK} OK · ${counts.MISSED} missing · ${counts.REHIRED} rehired${counts.LATE ? ` · ${counts.LATE} late` : ''}${counts.REVIEW ? ` · ${counts.REVIEW} unclassified` : ''}`;
      return `<div class="tm-worker-group"><div class="tm-worker-label"><strong>${esc(type ? type + 's' : 'Unspecified worker type')}</strong><span>${records.length}</span></div><div class="tm-stacked" aria-label="${esc(type || 'Unspecified')} outcome percentages">${segments}</div><p class="tm-group-caption">${caption}</p></div>`;
    }).join('');
    const missing = state.report.records.filter(r => r.bucket === 'MISSED');
    const allContractors = missing.length && missing.every(r => r.type === 'Contractor');
    return `<section class="tm-panel" aria-labelledby="tmWorkerTitle"><h2 id="tmWorkerTitle">Employees vs contractors</h2><p class="tm-panel-sub">Each bar = 100% of worker type</p><div class="tm-worker-groups">${rows}</div><p class="tm-insight ${missing.length ? 'tm-insight-red' : ''}">${allContractors ? `${missing.length === 2 ? 'Both' : 'All'} missing-evidence cases are contractors.` : missing.length ? `${missing.length} missing-evidence cases require follow-up.` : 'No missing-evidence cases in this snapshot.'}</p></section>`;
  }

  function timingHTML() {
    const rows = timingSeries();
    const maximum = Math.max(1, ...rows.map(row => row.count));
    const rough = maximum / 6, power = Math.pow(10, Math.floor(Math.log10(rough)));
    const step = Math.max(1, ([1, 2, 2.5, 5, 10].find(n => n * power >= rough) || 10) * power);
    const axisMax = Math.ceil(maximum / step) * step;
    const ticks = Array.from({ length: Math.round(axisMax / step) + 1 }, (_, i) => i * step);
    return `<section class="tm-panel" aria-labelledby="tmTimingTitle"><h2 id="tmTimingTitle">Time to deactivation</h2><p class="tm-panel-sub">${state.report.summary.counts.OK} records marked OK · From effective termination date</p><div class="tm-timing-chart" style="--tm-grid-step:${step / axisMax * 100}%"><div class="tm-axis-label">Workers</div>${rows.length ? rows.map(row => `<button class="tm-timing-row" type="button" data-timing="${row.key}" data-count="${row.count}" onclick="tmTiming(this.dataset.timing)" aria-label="${row.label}: ${row.count} workers"><span class="tm-timing-label">${row.label}</span><span class="tm-timing-track"><span class="tm-timing-fill ${['long', 'unknown'].includes(row.key) ? 'tm-bar-amber' : ''}" style="width:${row.count / axisMax * 100}%"></span><span class="tm-timing-count" style="left:${row.count / axisMax * 100}%">${row.count}</span></span></button>`).join('') : '<p class="sub">No OK records to plot.</p>'}<div class="tm-axis-ticks">${ticks.map(n => `<span>${n}</span>`).join('')}</div></div><p class="tm-panel-foot">${state.report.summary.entryAdjusted ? `${state.report.summary.entryAdjusted} entry-adjusted cases; recipe result remains OK.` : 'Timing values come from the imported CSV.'}</p></section>`;
  }

  function timelineHTML() {
    const cases = state.report.records.filter(r => r.entryAdjusted && r.disabled).sort((a, b) => Number(b.days) - Number(a.days));
    if (!cases.length) return `<section class="tm-panel tm-timeline-panel"><h2>Where does the delay happen?</h2><p class="tm-panel-sub">Workday entry and Okta processing</p><div class="tm-no-delay"><span aria-hidden="true">✓</span><strong>No entry-adjusted cases in this snapshot</strong><p>The recipe has not flagged any results as accepted after allowing for late Workday entry.</p></div></section>`;
    const r = cases.find(item => item.id === state.timelineId) || cases[0];
    const dayDifference = (a, b) => Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000);
    const lag = dayDifference(r.term, r.entered), process = dayDifference(r.entered, r.disabled), total = dayDifference(r.term, r.disabled);
    const share = total > 0 && lag >= 0 && process >= 0 ? lag / total * 100 : 50;
    const shortDate = value => new Date(value + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    return `<section class="tm-panel tm-timeline-panel" aria-labelledby="tmTimelineTitle"><div class="tm-panel-heading"><h2 id="tmTimelineTitle">Where does the delay happen?</h2><span class="tm-tag tm-amber-tag">${cases.length} entry-adjusted cases</span></div><div class="tm-case-header"><strong>${esc(r.name)} <span>· ${esc(r.id)} · ${esc(r.type)}</span></strong><span class="tm-tag tm-green-tag">Recipe: ${esc(r.status)}</span></div><div class="tm-milestones"><div><strong>${shortDate(r.term)}</strong><span>Termination effective</span></div><div><strong>${shortDate(r.entered)}</strong><span>Entered in Workday</span></div><div><strong>${shortDate(r.disabled)}</strong><span>Okta disabled</span></div></div><div class="tm-elapsed" role="img" aria-label="${lag} days Workday entry lag and ${process} days Okta processing"><span class="tm-elapsed-lag" style="width:${share}%"></span><span class="tm-elapsed-process" style="width:${100 - share}%"></span></div><div class="tm-elapsed-labels"><span>${lag} days · Workday entry lag</span><span>${process} day${process === 1 ? '' : 's'} · Okta processing</span></div><div class="tm-equation"><div class="tm-equation-value tm-amber-value"><strong>${lag}d</strong><span>entry lag</span></div><b aria-hidden="true">+</b><div class="tm-equation-value tm-teal-value"><strong>${process}d</strong><span>processing</span></div><b aria-hidden="true">=</b><div class="tm-equation-value tm-blue-value"><strong>${total}d</strong><span>from termination</span></div><p>The recipe accepts this result after allowing for late Workday entry.</p></div><div class="tm-case-picker"><label for="tmTimelineWorker">Compare another worker</label><select id="tmTimelineWorker" class="select" onchange="tmSelectTimeline(this.value)">${cases.map(item => `<option value="${esc(item.id)}" ${item.id === r.id ? 'selected' : ''}>${esc(item.name)} · ${esc(item.days)} days from termination</option>`).join('')}</select></div></section>`;
  }

  function missingHTML() {
    const rows = state.report.records.filter(r => r.bucket === 'MISSED');
    return `<section class="tm-panel tm-missing-panel" aria-labelledby="tmMissingTitle"><div class="tm-panel-heading"><h2 id="tmMissingTitle">Investigate missing evidence</h2><span class="tm-tag ${rows.length ? 'tm-red-tag' : 'tm-green-tag'}">${rows.length}</span></div>${rows.length ? rows.slice(0, 3).map(r => `<button class="tm-missing-worker" type="button" data-worker-id="${esc(r.id)}" onclick="tmMissing(this.dataset.workerId)"><span><strong>${esc(r.name)}</strong><small>${esc(r.id)} · ${esc(r.type)} · Terminated ${dateLabel(r.term)}</small></span><span class="tm-tag tm-red-tag">No event</span></button>`).join('') : '<div class="tm-no-missing"><strong>No missing-evidence cases</strong><p>There is nothing to investigate in this category.</p></div>'}<div class="tm-missing-foot"><p>${rows.length ? 'No matching deactivation event in the supplied Okta log.' : 'Source recipe classifications are preserved.'}</p>${rows.length ? `<button class="textbtn" onclick="tmOpen('MISSED')">View ${rows.length > 3 ? 'all ' + rows.length + ' ' : ''}records →</button>` : ''}</div></section>`;
  }

  function detailsHTML() {
    const title = state.filter?.title || (state.selected ? categories[state.selected].title : '');
    return `<section id="tmDetails" class="card tm-details tm-card" aria-labelledby="tmDetailsTitle" ${state.selected ? '' : 'hidden'}><div class="tm-detail-head"><div><div class="eyebrow">Reconciliation details</div><h2 id="tmDetailsTitle" tabindex="-1">${esc(title)}</h2><p class="sub" id="tmDetailsDescription"></p></div><button class="btn" onclick="tmClose()">Close details ×</button></div><div class="tm-search"><input id="tmSearch" class="select" type="search" aria-label="Search workers in selected category" placeholder="Search worker or employee ID" value="${esc(state.query)}" oninput="tmSearch(this.value)"><span class="sub" id="tmRowCount"></span><button class="textbtn" onclick="tmExport()">Export these records</button></div><div class="table-scroll"><table class="tm-table"><thead><tr><th>Worker</th><th>Type</th><th>Termination</th><th>Workday entry</th><th>Okta disabled</th><th>Days from term</th><th>Days from entry</th><th>Recipe result</th></tr></thead><tbody id="tmRows"></tbody></table></div><div class="tm-detail-foot" id="tmDetailsFooter"></div></section>`;
  }

  function view() {
    if (!state.loaded && !state.loading) load();
    const header = headerHTML() + controlsHTML();
    if (state.error) return header + `<div class="notice amber tm-alert" role="alert">${esc(state.error)}</div><p class="sub">No chart is shown until a valid snapshot is available.</p>`;
    if (!state.report) return header + `<div class="card tm-card tm-empty"><h2>${state.loading ? 'Loading reconciliation…' : 'No reconciliation snapshot yet'}</h2><p class="sub">${state.loading ? 'Reading the saved Workato CSV.' : 'Import the Workato CSV that compares Workday terminations with Okta deactivation events. An empty or missing file is never treated as an all-clear result.'}</p></div>`;
    const summary = state.report.summary, source = state.report.source;
    const followups = summary.counts.MISSED + summary.counts.LATE + summary.counts.REVIEW;
    const allGreen = summary.counts.OK === summary.total;
    return header + `<div class="tm-meta"><span title="${esc(source.filename)}">${source.url ? 'Google Drive CSV snapshot' : 'Imported CSV'} · ${esc(source.filename)}</span><span>Effective dates: ${dateLabel(summary.effectiveStart)} – ${dateLabel(summary.effectiveEnd)}</span></div><div class="tm-dashboard">${kpisHTML(summary)}<div class="tm-chart-grid"><section class="tm-panel tm-outcomes-panel" aria-labelledby="tmOverviewTitle"><h2 id="tmOverviewTitle">Reconciliation outcomes</h2><p class="tm-panel-sub">Recipe classifications</p><div class="tm-outcomes-body">${chartHTML(summary)}<div class="tm-compact-legend">${legendHTML(summary)}</div></div><p class="tm-panel-foot">${followups ? `${followups} worker${followups === 1 ? '' : 's'} need a closer look. Click a non-green segment to view records.` : 'No follow-up flagged by the recipe.'}</p></section>${workersHTML()}${timingHTML()}</div>${allGreen ? '<div id="tmAllClear" class="tm-clear"><strong>All green. No follow-up needed.</strong><p>Every worker is marked OK in this snapshot.</p></div>' : ''}<div class="tm-bottom-grid">${timelineHTML()}${missingHTML()}</div></div>${detailsHTML()}<div id="tmAnnouncement" class="tm-announcement" role="status" aria-live="polite"></div><p class="tm-note">Entry-adjusted cases overlap the OK category. Imported ${esc(uiDate(source.importedAt))}. Missing log evidence does not establish current account status. Reload reads the saved snapshot; run your Workato recipe and import its new CSV to update the results.</p>`;
  }

  function selectedRecords() {
    return state.report.records.filter(r => (!state.selected || r.bucket === state.selected) && (!state.filter || (state.filter.kind === 'type' ? r.type === state.filter.key : timingKey(r) === state.filter.key)));
  }

  function renderRows() {
    if (!state.selected || !state.report || !$('tmRows')) return;
    const query = state.query.toLowerCase().trim();
    const rows = selectedRecords().filter(r => !query || `${r.name} ${r.id}`.toLowerCase().includes(query));
    $('tmRowCount').textContent = `${rows.length} worker${rows.length === 1 ? '' : 's'}`;
    $('tmRows').innerHTML = rows.map(r => `<tr><td><strong>${esc(r.name)}</strong><small>Worker ${esc(r.id)}</small></td><td>${esc(r.type)}</td><td>${dateLabel(r.term)}</td><td>${dateLabel(r.entered)}</td><td class="${r.disabled ? '' : 'tm-missing'}">${dateLabel(r.disabled)}</td><td>${r.days === '' ? '—' : esc(r.days) + ' days'}</td><td>${r.entryDays === '' ? '—' : esc(r.entryDays) + ' days'}</td><td><span class="pill ${r.bucket === 'MISSED' ? 'danger' : r.bucket === 'REHIRED' ? 'neutral' : r.bucket === 'OK' ? 'good' : 'warn'}">${esc(r.status || 'Unclassified')}</span><small>${esc(r.result)}</small></td></tr>`).join('') || '<tr><td colspan="8">No workers match your search.</td></tr>';
    $('tmDetailsDescription').textContent = state.selected === 'OK' ? 'Timing context for records marked OK by the recipe. Compare elapsed time from termination with elapsed time from Workday entry.' : state.selected === 'MISSED' ? 'Workday shows a termination, but the supplied Okta log has no matching deactivation event. Check the source evidence and current account status before drawing a conclusion.' : state.selected === 'REHIRED' ? 'The recipe classified these workers as rehired or converted and the account as correctly retained. These records are shown for context.' : 'Review the recipe result and date comparison for these workers. Unrecognized classifications are never counted as OK.';
    $('tmDetailsFooter').textContent = 'Source classifications are preserved. Dates and timing values come from the imported CSV.';
  }

  function focusDetails() {
    render();
    $('tmAnnouncement').textContent = `Showing ${selectedRecords().length} records below the charts.`;
    $('tmDetailsTitle').focus({ preventScroll: true });
    $('tmDetails').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  }
  window.tmOpen = (bucket, workerType) => {
    if (!state.report || bucket === 'OK' || !categories[bucket] || !state.report.summary.counts[bucket]) return;
    state.selected = bucket; state.query = '';
    state.filter = workerType === undefined ? null : { kind: 'type', key: workerType, title: `${workerType || 'Unspecified workers'} · ${categories[bucket].title}` };
    focusDetails();
  };
  window.tmTiming = key => {
    const row = state.report && timingSeries().find(item => item.key === key);
    if (!row) return;
    state.selected = 'OK'; state.query = ''; state.filter = { kind: 'timing', key, title: `Time to deactivation · ${row.label}` }; focusDetails();
  };
  window.tmMissing = id => { window.tmOpen('MISSED'); state.query = id; $('tmSearch').value = id; renderRows(); };
  window.tmSelectTimeline = id => { state.timelineId = id; render(); };
  window.tmChartKey = (event, bucket) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); window.tmOpen(bucket); } };
  window.tmClose = () => { const previous = state.selected, timing = state.filter?.kind === 'timing' && state.filter.key; state.selected = null; state.filter = null; state.query = ''; render(); (timing ? document.querySelector(`[data-timing="${timing}"]`) : document.querySelector(`[data-tm-category="${previous}"]`))?.focus(); };
  window.tmSearch = value => { state.query = value; renderRows(); };
  window.tmReload = () => { load(); render(); };
  window.tmImportCSV = async input => {
    const file = input.files[0];
    if (!file || state.loading) return;
    if (file.size > 2 * 1024 * 1024) { toast('The CSV must be no larger than 2 MB.'); input.value = ''; return; }
    const body = new FormData(); body.append('file', file);
    state.loading = true; render();
    try {
      const response = await fetch(endpoint, { method: 'POST', body });
      const report = await response.json();
      if (!response.ok) throw new Error(report.message || 'The CSV could not be imported.');
      state.report = report; state.loaded = true; state.error = ''; state.selected = null; state.filter = null; state.query = ''; state.timelineId = null;
      toast(`Imported ${report.summary.total} workers from ${file.name}.`);
    } catch (error) { toast(error.message); }
    finally { state.loading = false; render(); }
  };

  window.tmExport = () => {
    if (!state.report) return;
    const query = state.query.toLowerCase().trim();
    const records = selectedRecords().filter(r => !query || `${r.name} ${r.id}`.toLowerCase().includes(query));
    const cells = value => {
      let text = String(value ?? '');
      if (typeof value !== 'number' && /^[=+@\-\t\r]/.test(text)) text = "'" + text;
      return '"' + text.replaceAll('"', '""') + '"';
    };
    const headers = ['Full name', 'Employee ID', 'Worker Type', 'Termination Date', 'Term Entered On', 'Okta Disable Date', 'Term>Disable Days', 'Entry>Disable Days', 'Status', 'Result', 'Source file', 'Imported at UTC'];
    const rows = records.map(r => [r.name, r.id, r.sourceWorkerType, r.term, r.entered, r.disabled, r.days === '' ? '' : Number(r.days), r.entryDays === '' ? '' : Number(r.entryDays), r.status, r.result, state.report.source.filename, state.report.source.importedAt]);
    const csv = '\uFEFF' + [headers, ...rows].map(row => row.map(cells).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = 'Termination-review.csv'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${records.length} worker records.`);
  };

  const originalView = monitorView;
  monitorView = function () { return app.check === 0 ? view() : originalView(); };
  const originalRender = render;
  render = function () {
    originalRender();
    const active = app.tab === 'monitor' && app.check === 0 && canModule('monitor');
    const picker = $('cpSystemPick')?.closest('.cp-syspick');
    if (picker) picker.hidden = active;
    let scope = $('tmSystemScope');
    if (!scope && document.querySelector('.topright')) {
      scope = document.createElement('span');
      scope.id = 'tmSystemScope';
      scope.className = 'cp-syspick tm-system-scope';
      scope.innerHTML = '<span class="cp-syspick-label">Comparing</span><strong>Workday + Okta</strong>';
      document.querySelector('.topright').prepend(scope);
    }
    if (scope) scope.hidden = !active;
    if (active) {
      if ($('locationLabel')) $('locationLabel').textContent = 'Compliance Monitoring / Workday + Okta';
      renderRows();
    }
  };
  const originalIssues = issueList;
  issueList = function (index = app.check) {
    if (index !== 0) return originalIssues(index);
    if (!state.report) return [];
    return state.report.records.filter(r => ['MISSED', 'LATE', 'REVIEW'].includes(r.bucket)).map(r => ({
      id: 'TERM-' + r.id, title: `${categories[r.bucket].title}: ${r.name}`, account: r.id,
      expected: 'Deactivation evidence consistent with the termination policy',
      observed: r.result || r.status || 'Unclassified result', severity: r.bucket === 'REVIEW' ? 'Medium' : 'High', detected: r.term
    }));
  };
  const originalRun = runMonitoring;
  runMonitoring = function (index = null) {
    if (index === 0) { window.tmReload(); toast('Reloading the saved CSV snapshot.'); return; }
    return originalRun(index);
  };
  const params = new URLSearchParams(location.search);
  if (params.get('tab') === 'monitor') { app.tab = 'monitor'; app.check = 0; }
  if (['Workday', 'Okta'].includes(params.get('system'))) app.system = params.get('system');
  render();
})();

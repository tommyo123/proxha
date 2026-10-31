const insights = {view: 'overview', resource: null, detail: null, telemetry: null, serial: 0, detailBusy: false, dcBusy: false, historySerial: {dc: 0, resource: 0}};
const chartColors = ['#2c9973', '#7688cf'];
const finite = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value) : null;
const ratio = (value, total) => finite(value) !== null && finite(total) > 0 ? Number(value) / Number(total) : null;
const rateText = value => finite(value) === null ? '—' : `${bytes(Number(value))}/s`;
const dateSeconds = value => value ? time(new Date(value * 1000).toISOString()) : 'Never';

function resetInsights() {
  resetGuestPreference();
  insights.serial++; insights.resource = null; insights.detail = null; insights.telemetry = null;
  insights.outcome = null;
  insights.detailBusy = false;
  insights.historySerial.dc++; insights.historySerial.resource++;
  if (insights.view === 'resource') showView('overview');
}
function resourceGauge(label, value, fraction, caption = '') {
  const known = finite(fraction) !== null;
  const width = known ? Math.max(0, Math.min(100, fraction * 100)) : 0;
  return `<article class="resource-gauge"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><div class="usage-track ${known && fraction > .9 ? 'high' : ''}" ${known ? `role="progressbar" aria-label="${escapeHtml(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${width.toFixed(1)}"` : ''}><i style="width:${width}%"></i></div><small>${escapeHtml(caption)}</small></article>`;
}
function memoryGauge(label, used, total) {
  return resourceGauge(label, pct(ratio(used, total)), ratio(used, total), `${bytes(finite(used))} of ${bytes(finite(total))}`);
}
function factsList(entries) {
  return `<dl class="resource-facts">${entries.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? '—')}</dd>`).join('')}</dl>`;
}
function repositoryText(repo) {
  if (!repo) return 'Unavailable';
  const enabled = (repo.standard || []).filter(r => r.status === 1 || r.status === true);
  const pve = enabled.filter(r => ['enterprise', 'no-subscription', 'test'].includes(r.handle));
  const warning = enabled.some(r => r.handle.includes('no-subscription') || r.handle === 'test' || r.handle.endsWith('-test'));
  const label = pve.length ? `Proxmox VE updates: ${pve.map(r => r.name).join(', ')}` : 'No enabled standard Proxmox VE repository detected';
  return [label, warning ? 'Non-production repository enabled.' : '', ...(repo.errors || []).map(r => r.error || r.message || 'Repository error'), ...(repo.warnings || [])].filter(Boolean).join(' · ');
}
async function openResource(kind, identifier) {
  if (!state.selected) return;
  insights.serial++; insights.historySerial.resource++;
  insights.resource = {kind, identifier: String(identifier), key: `${kind}/${identifier}`};
  insights.detail = null;
  insights.outcome = null;
  showView('resource');
  resourceTab('summary');
  $('#evacuation-result').textContent = '';
  $('#resource-kind').textContent = kind === 'node' ? 'HOST' : kind === 'lxc' ? 'LXC CONTAINER' : 'VIRTUAL MACHINE';
  const guest = state.cluster?.guests.find(g => String(g.vmid) === String(identifier) && g.type === kind);
  $('#resource-title').textContent = guest ? `${guest.vmid} · ${guest.name || 'Unnamed'}` : String(identifier);
  $('#breadcrumb').textContent = $('#resource-title').textContent;
  updateWorkspace();
  $('#resource-subtitle').textContent = 'Loading current status …';
  $('#resource-summary').innerHTML = '';
  $('#resource-facts').innerHTML = '<p class="guest-empty">Loading resource details …</p>';
  $('#resource-notice').hidden = true;
  loadGuestPreference(kind, identifier);
  refreshResource(true);
  loadHistory('resource');
}

const guestPreference = {serial: 0, connection: null, resource: null, saved: null, draft: 5, saving: false, error: '', conflict: false};

function resetGuestPreference() {
  guestPreference.serial++;
  Object.assign(guestPreference, {connection: null, resource: null, saved: null, draft: 5, saving: false, error: '', conflict: false});
  document.getElementById('guest-policy-panel').hidden = true;
}

function renderGuestPreference() {
  const p = guestPreference, value = p.draft;
  $('#guest-stickiness').value = value;
  $('#guest-stickiness').disabled = !p.saved || p.conflict;
  $('#guest-stickiness').setAttribute('aria-valuetext', `${value} of 10${value === 10 ? ', protected from routine automatic migration' : value === 5 ? ', default' : ''}`);
  $('#stickiness-value').textContent = `${value} / 10`;
  $('#stickiness-description').textContent = value === 10 ? 'Stay put during routine balancing. Placement rule repair, host maintenance or a verified emergency may override this preference; compatibility and capacity checks still apply.' :
    value < 5 ? 'Prefer moving. This guest is a more willing candidate when a migration would help. History, cooldowns and safety checks still take priority.' :
    value === 5 ? 'Balanced preference. The default willingness to move when automatic balancing would help.' :
    'Prefer staying. Moving this guest needs a stronger benefit than moving a guest with lower stickiness.';
  $('#stickiness-scope').textContent = `${p.resource?.startsWith('lxc/') ? 'Saved for this container. Containers require restart migration with downtime. ' : ''}Applies to continuous and scheduled balancing. Placement rules take priority over this preference. Manual guest migrations remain available.`;
  $('#stickiness-status').textContent = p.error ? `Not saved: ${p.error}` : !p.saved ? 'Loading preference …' : p.saving ? 'Saving …' : value !== p.saved.stickiness ? 'Release the slider to save.' : p.saved.revision === 0 ? 'Default preference · 5' : `Saved · ${time(p.saved.updated_at)}`;
  $('#stickiness-status').classList.toggle('preference-error', !!p.error);
  $('#stickiness-reset').disabled = !p.saved || p.conflict || value === 5;
  $('#stickiness-reload').disabled = p.saving;
  $('#stickiness-retry').hidden = !p.error || p.conflict || !p.saved || p.draft === p.saved.stickiness;
}

async function loadGuestPreference(kind, identifier) {
  resetGuestPreference();
  if (kind === 'node' || !state.selected) return;
  const serial = guestPreference.serial, connection = state.selected, resource = `${kind}/${identifier}`;
  Object.assign(guestPreference, {connection, resource});
  $('#guest-policy-panel').hidden = false; renderGuestPreference();
  try {
    const policy = await api(`/connections/${connection}/guests/${resource}/policy`);
    if (serial !== guestPreference.serial || connection !== state.selected) return;
    guestPreference.saved = policy; guestPreference.draft = policy.stickiness;
  } catch (error) {
    if (serial !== guestPreference.serial || connection !== state.selected) return;
    guestPreference.error = `Could not load preference. ${error.message}`;
  }
  renderGuestPreference();
}

async function saveGuestPreference() {
  const p = guestPreference;
  if (!p.saved || p.saving || p.conflict || p.draft === p.saved.stickiness) return;
  const serial = p.serial, connection = p.connection, resource = p.resource;
  p.saving = true; p.error = ''; renderGuestPreference();
  try {
    while (serial === p.serial && p.draft !== p.saved.stickiness) {
      const policy = await api(`/connections/${connection}/guests/${resource}/policy`, {
        method: 'PUT', body: JSON.stringify({stickiness: p.draft, revision: p.saved.revision})
      });
      if (serial !== p.serial || connection !== state.selected) return;
      p.saved = policy;
    }
  } catch (error) {
    if (serial !== p.serial || connection !== state.selected) return;
    p.error = error.message; p.conflict = error.status === 409;
  } finally {
    if (serial === p.serial) {p.saving = false; renderGuestPreference();}
  }
}

document.getElementById('guest-stickiness').addEventListener('input', event => {
  guestPreference.draft = Number(event.target.value); renderGuestPreference();
});
document.getElementById('guest-stickiness').addEventListener('change', saveGuestPreference);
document.getElementById('stickiness-reset').addEventListener('click', () => {
  guestPreference.draft = 5; renderGuestPreference(); saveGuestPreference();
});
document.getElementById('stickiness-retry').addEventListener('click', saveGuestPreference);
document.getElementById('stickiness-reload').addEventListener('click', () => {
  if (insights.resource && !guestPreference.saving) loadGuestPreference(insights.resource.kind, insights.resource.identifier);
});
async function refreshResource(force = false) {
  if (!insights.resource || !state.selected || (insights.detailBusy && !force)) return;
  const serial = insights.serial, connection = state.selected, resource = {...insights.resource};
  insights.detailBusy = true;
  if (resource.kind === 'node') refreshNodeMaintenance();
  try {
    const data = await api(`/connections/${connection}/resources/${resource.kind}/${encodeURIComponent(resource.identifier)}`);
    if (serial !== insights.serial || connection !== state.selected) return;
    insights.detail = data; renderResource(data);
  } catch (error) {
    if (serial !== insights.serial || connection !== state.selected) return;
    $('#resource-notice').hidden = false;
    $('#resource-notice').textContent = `Could not refresh resource: ${error.message}${insights.detail ? ' Showing the last successful response.' : ''}`;
    if (!insights.detail) $('#resource-facts').innerHTML = '<p class="guest-empty">No current resource data available.</p>';
  } finally {if (serial === insights.serial) insights.detailBusy = false;}
}
function renderResource(data) {
  const d = data.detail, r = data.resource, node = r.type === 'node';
  $('#resource-subtitle').textContent = `${d.qmpstatus || d.status || r.status || 'Unknown'} · ${r.node} · Updated ${dateSeconds(data.observed_at)} · Live refresh 15s`;
  $('#resource-notice').hidden = !data.errors.length;
  $('#resource-notice').textContent = data.errors.join(' ');
  if (node) {
    const memory = d.memory || {}, swap = d.swap || {}, root = d.rootfs || {}, cpu = d.cpuinfo || {};
    $('#resource-summary').innerHTML = resourceGauge('CPU usage', pct(finite(d.cpu)), finite(d.cpu), `of ${cpu.cpus ?? '—'} CPUs`) +
      memoryGauge('RAM usage', memory.used, memory.total) + memoryGauge('/ HD space', root.used, root.total) + memoryGauge('SWAP usage', swap.used, swap.total);
    const kernel = d['current-kernel'];
    $('#resource-facts').innerHTML = '<div class="panel-heading"><h2>Host status</h2><span class="badge online">Live data</span></div>' + factsList([
      ['Status', r.status], ['IO delay', pct(finite(d.wait))], ['Load average', (d.loadavg || []).join(', ') || '—'],
      ['KSM sharing', bytes(finite(d.ksm?.shared))], ['CPU(s)', `${cpu.cpus ?? '—'} × ${cpu.model || 'Unknown'} (${cpu.sockets ?? '—'} socket${cpu.sockets === 1 ? '' : 's'})`],
      ['Kernel version', kernel ? [kernel.sysname, kernel.release, kernel.version].filter(Boolean).join(' ') : d.kversion],
      ['Boot mode', d['boot-info']?.mode?.toUpperCase()], ['Manager version', d.pveversion],
      ['Repository status', repositoryText(data.extra.repositories)], ['Uptime', d.uptime == null ? '—' : `${(d.uptime / 86400).toFixed(1)} days`]
    ]) + `<div class="detail-links"><button class="secondary" data-node-guests="${escapeHtml(r.node)}">View guests on this host</button></div>`;
    $('#resource-facts').insertAdjacentHTML('beforeend', '<h2>Physical I/O and network</h2>' + infrastructureMarkup(data.extra.infrastructure ? {[r.node]: data.extra.infrastructure} : {}));
  } else {
    const reclaimed = finite(d.maxmem) !== null && finite(d.balloon) > 0 ? Math.max(0, d.maxmem - d.balloon) : null;
    $('#resource-summary').innerHTML = resourceGauge('CPU usage', pct(finite(d.cpu)), finite(d.cpu), `of ${d.cpus ?? r.maxcpu ?? '—'} CPUs`) +
      memoryGauge('Memory usage', d.mem, d.maxmem) + resourceGauge('Host memory usage', bytes(finite(d.memhost)), ratio(d.memhost, d.maxmem), r.type === 'lxc' ? 'Not separately reported for containers' : 'Memory used by the VM on its host') +
      resourceGauge('Bootdisk size', bytes(finite(d.maxdisk)), null, r.type === 'lxc' ? `${bytes(finite(d.disk))} used` : 'Provisioned capacity');
    $('#resource-facts').innerHTML = '<div class="panel-heading"><h2>Guest status</h2><span class="badge online">Live data</span></div>' + factsList([
      ['Status', d.qmpstatus || d.status], ['HA state', d.ha?.state || (d.ha?.managed ? 'Managed' : 'none')], ['Node', r.node],
      ['Balloon allocation', bytes(finite(d.balloon))], ['Ballooned memory', bytes(reclaimed)],
      ['Uptime', d.uptime == null ? '—' : `${(d.uptime / 3600).toFixed(1)} hours`],
      ['IPs', data.extra.ips === null ? 'Unavailable' : (data.extra.ips || []).map(ip => `${ip.address} (${ip.interface})`).join('\n') || 'No non-local addresses reported']
    ]) + `<div class="detail-links"><button class="secondary" data-open-node="${escapeHtml(r.node)}">Open host ${escapeHtml(r.node)}</button><button class="secondary" data-guest-actions="${r.vmid}">Guest actions</button></div>`;
    $('#resource-facts').insertAdjacentHTML('beforeend', automaticMigrationsHtml(data.automatic_migrations));
  }
}

async function refreshDatacenter() {
  if (!state.selected || insights.dcBusy) return;
  const connection = state.selected, generation = state.generation;
  insights.dcBusy = true;
  try {
    const data = await api(`/connections/${connection}/telemetry`);
    if (connection !== state.selected || generation !== state.generation) return;
    insights.telemetry = data; renderDatacenter();
  } catch (error) {
    if (generation === state.generation) $('#collector-status').textContent = `Could not load telemetry: ${error.message}`;
  } finally {insights.dcBusy = false;}
}
function renderDatacenter() {
  const c = state.cluster, telemetry = insights.telemetry;
  $('#insights-dc-name').textContent = c?.name || 'Datacenter';
  if (!state.selected) {
    $('#collector-status').textContent = 'Connect a cluster to start collecting metrics.';
    $('#dc-capacity').innerHTML = ''; $('#dc-inventory').innerHTML = ''; return;
  }
  const status = telemetry?.collector;
  const old = !telemetry?.newest_sample || Date.now() / 1000 - telemetry.newest_sample > 45;
  $('#collector-status').classList.toggle('delayed', old || !!status?.last_error);
  $('#collector-status').textContent = status ? `${old ? 'Collection delayed' : 'Background collection active'} · Every 15s · Raw observations 24h · Minute summaries 7 days · ${telemetry.stored_samples.toLocaleString('en-US')} summaries · Last cycle ${dateSeconds(status.finished_at)}${status.last_error ? ' · ' + status.last_error : ''}` : 'Waiting for the first background collection. The collector runs every 15 seconds while the server is running.';
  const s = telemetry?.samples.find(s => s.resource === 'datacenter');
  $('#dc-capacity').innerHTML = resourceGauge('Host CPU usage', pct(s?.cpu), s?.cpu, `${s?.cpu_count ?? '—'} CPUs across ${c?.nodes.length || 0} hosts`) +
    memoryGauge('Host memory', s?.mem_used, s?.mem_total) + resourceGauge('Network traffic', `${rateText(s?.net_in_bps)} ↓`, null, `${rateText(s?.net_out_bps)} ↑ · Host network totals`) +
    resourceGauge('Guests', String(c?.guests.length || 0), null, `${c?.guests.filter(g => g.status === 'running').length || 0} running · ${c?.guests.filter(g => g.type === 'lxc').length || 0} containers`);
  const query = $('#dc-search').value.toLowerCase().trim();
  const resources = [...(c?.nodes || []), ...(c?.guests || [])].filter(r => `${r.name || ''} ${r.node} ${r.vmid || ''}`.toLowerCase().includes(query));
  $('#dc-inventory').innerHTML = resources.length ? `<div class="table-wrap inventory-scroll"><table><thead><tr><th>RESOURCE</th><th>TYPE</th><th>HOST</th><th>STATUS</th><th>CPU</th><th>MEMORY</th><th>DISK READ / WRITE</th><th>NETWORK IN / OUT</th><th>CPU / IO / MEMORY PSI</th></tr></thead><tbody>${resources.map(r => {
    const sample = telemetry?.samples.find(s => s.resource === r.id);
    const stale = sample && Date.now() / 1000 - sample.sampled_at > 45;
    const m = stale ? null : sample;
    const id = r.type === 'node' ? r.node : r.vmid;
    return `<tr class="${r.type === 'node' && r.status !== 'online' ? 'is-offline' : ''}"><td><button class="${r.type === 'node' && maintenanceIdle(r) ? 'maintenance-idle' : ''}" data-resource-kind="${r.type}" data-resource-id="${escapeHtml(id)}">${r.type === 'node' ? hostIcon(r) : guestIcon(r)}<span class="host-name">${escapeHtml(r.name || r.node)}</span>${r.vmid ? ' · ' + r.vmid : ''}</button></td><td>${r.type === 'node' ? 'Host' : r.type === 'lxc' ? 'LXC' : 'VM'}</td><td>${escapeHtml(r.node)}</td><td>${escapeHtml(r.status)}${stale ? ' · Stale metrics' : sample?.quality === 'partial' ? ' · Partial' : ''}</td><td>${pct(m?.cpu)}</td><td>${bytes(m?.mem_used)}</td><td>${rateText(m?.disk_read_bps)} / ${rateText(m?.disk_write_bps)}</td><td>${rateText(m?.net_in_bps)} / ${rateText(m?.net_out_bps)}</td><td>${[m?.pressurecpusome, m?.pressureiosome, m?.pressurememorysome].map(v => finite(v) === null ? '—' : Number(v).toFixed(2) + '%').join(' / ')}</td></tr>`;
  }).join('')}</tbody></table></div><p class="metrics-footnote">Disk I/O on hosts is the sum of guest I/O. PSI columns show “some”. Missing values are not zeros.</p>` : '<p class="guest-empty">No matching resources.</p>';
}

function chartSeries(rows, key, maxPoints = 360, statistic = 'mean') {
  const step = Math.max(1, Math.ceil(rows.length / maxPoints));
  const values = [];
  for (let index = 0; index < rows.length; index += step) {
    const bucket = rows.slice(index, index + step);
    const valueOf = r => statistic === 'max' ? finite(r.metadata?.summary?.max?.[key]) : finite(r[key]);
    const known = bucket.filter(r => valueOf(r) !== null);
    const continuous = bucket.every((row, i) => i === 0 || row.sampled_at - bucket[i - 1].sampled_at <= 90);
    const weight = r => r.metadata?.summary?.weights?.[key] ?? r.metadata?.summary?.counts?.[key] ?? 1;
    const count = known.reduce((sum, r) => sum + weight(r), 0);
    const value = statistic === 'max' ? Math.max(...known.map(valueOf)) : count ? known.reduce((sum, r) => sum + valueOf(r) * weight(r), 0) / count : null;
    values.push({time: bucket[0].sampled_at, end: bucket[bucket.length - 1].sampled_at, value: known.length === bucket.length && continuous ? value : null});
  }
  return {values, step};
}
function historyChart(title, series, rows, format, fixedMax = null) {
  const datasets = series.flatMap(([key, label], color) => ['mean', 'max'].map(statistic => ({key, label, color, statistic, ...chartSeries(rows, key, 360, statistic)})));
  const known = datasets.flatMap(s => s.values.filter(p => p.value !== null).map(p => p.value));
  if (!known.length) return `<article class="history-chart"><h3>${escapeHtml(title)}</h3><div class="chart-empty">No reported data yet</div></article>`;
  const max = fixedMax || Math.max(...known, .0001) * 1.1;
  const first = rows[0].sampled_at, last = Math.max(first + 60, rows[rows.length - 1].sampled_at);
  const x = t => 48 + (t - first) / (last - first) * 484, y = value => 130 - Math.min(max, Math.max(0, value)) / max * 108;
  const paths = datasets.map(s => {
    let path = '', previous = null;
    for (const point of s.values) {
      if (point.value === null) {previous = null; continue;}
      const continuous = previous && point.time - previous.end <= 90;
      path += `${continuous ? 'L' : 'M'}${x(point.time).toFixed(1)},${y(point.value).toFixed(1)} `;
      previous = point;
    }
    const points = s.values.filter(p => p.value !== null);
    return `<path d="${path}" fill="none" stroke="${chartColors[s.color]}" stroke-width="${s.statistic === 'max' ? 1 : 2}" ${s.statistic === 'max' ? 'stroke-dasharray="4 3" opacity=".65"' : ''}/>${points.length === 1 ? `<circle cx="${x(points[0].time)}" cy="${y(points[0].value)}" r="${s.statistic === 'max' ? 2 : 3}" fill="${chartColors[s.color]}"/>` : ''}`;
  }).join('');
  const latest = datasets.map(s => {
    const row = rows.at(-1), value = s.statistic === 'max' ? row?.metadata?.summary?.max?.[s.key] : row?.[s.key];
    return `<span><i style="background:${chartColors[s.color]}"></i>${escapeHtml(s.label)} ${s.statistic === 'max' ? 'peak' : 'mean'} <strong>${escapeHtml(format(value))}</strong></span>`;
  }).join('');
  return `<article class="history-chart"><h3>${escapeHtml(title)}</h3><div class="chart-legend">${latest}</div><svg viewBox="0 0 550 160" role="img" aria-label="${escapeHtml(title)} history"><title>${escapeHtml(title)}. Hover over the time labels for the sampled range.</title>${[0, .5, 1].map(f => `<line x1="48" y1="${130 - f * 108}" x2="532" y2="${130 - f * 108}" stroke="#e7eee9"/><text x="42" y="${134 - f * 108}" text-anchor="end">${escapeHtml(format(max * f))}</text>`).join('')}${paths}<text x="48" y="151">${escapeHtml(new Date(first * 1000).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}))}</text><text x="532" y="151" text-anchor="end">${escapeHtml(new Date(last * 1000).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}))}</text></svg></article>`;
}
function renderHistory(target, data, isNode) {
  const rows = data.samples;
  if (!rows.length) {target.innerHTML = '<p class="history-empty">No summaries in this time range yet. Collection runs every 15 seconds in the background.</p>'; return;}
  const pressure = value => finite(value) === null ? '—' : `${Number(value).toFixed(2)}%`;
  const summaries = rows.filter(row => row.metadata?.summary);
  const observations = summaries.reduce((sum, row) => sum + row.metadata.summary.sample_count, 0);
  const partial = summaries.filter(row => row.metadata.summary.sample_count < row.metadata.summary.expected_samples).length;
  target.innerHTML = historyChart('CPU usage', [['cpu', 'CPU']], rows, pct, 1) +
    historyChart('Memory usage', [['mem_used', 'Used'], ['mem_total', 'Total']], rows, bytes) +
    historyChart(isNode ? 'Guest disk I/O (aggregate)' : 'Disk I/O', [['disk_read_bps', 'Read'], ['disk_write_bps', 'Write']], rows, rateText) +
    (data.resource?.startsWith('qemu/') ? historyChart('Virtual disk IOPS', [['disk_read_iops', 'Read'], ['disk_write_iops', 'Write']], rows, value => finite(value) === null ? '—' : `${Number(value).toFixed(1)} ops/s`) +
      historyChart('Virtual disk latency', [['disk_read_latency_ms', 'Read'], ['disk_write_latency_ms', 'Write']], rows, value => finite(value) === null ? '—' : `${Number(value).toFixed(2)} ms`) +
      '<p class="metrics-footnote">QEMU virtual block devices, including attached media. Latency means are weighted by completed operations; peaks are interval averages, not individual request percentiles. Idle latency and unsupported counters remain unknown.</p>' : '') +
    historyChart('Network traffic', [['net_in_bps', 'In'], ['net_out_bps', 'Out']], rows, rateText) +
    historyChart('CPU pressure stall', [['pressurecpusome', 'Some'], ['pressurecpufull', 'Full']], rows, pressure) +
    historyChart('IO pressure stall', [['pressureiosome', 'Some'], ['pressureiofull', 'Full']], rows, pressure) +
    historyChart('Memory pressure stall', [['pressurememorysome', 'Some'], ['pressurememoryfull', 'Full']], rows, pressure) +
    `<div class="history-description"><strong>${rows.length.toLocaleString('en-US')} minute records · ${observations.toLocaleString('en-US')} fast observations · ${partial} partial minutes</strong><p>${dateSeconds(rows[0].sampled_at)} to ${dateSeconds(rows[rows.length - 1].sampled_at)}</p><p>Every 15 seconds; up to four observations per minute. Solid lines show means; dashed lines preserve observed peaks, including in long ranges. Capacity values use the last observation. The current minute is provisional. Older single-sample records have no peak or coverage estimate.</p><p>Raw observations expire after 24 hours; summaries after 7 days. Missing metrics remain unknown. Host network and PSI values come from minute RRD data, so their peaks do not represent 15-second host measurements. Guest PSI uses host cgroup averages over 10 seconds; peaks are observed values, not guaranteed instantaneous maxima. Some = at least one task stalled; full = all non-idle tasks stalled. Host CPU full PSI is not reported.</p></div>`;
}
async function loadHistory(scope) {
  if (!state.selected) return;
  const target = document.getElementById(scope === 'dc' ? 'dc-history' : 'resource-history');
  const resource = scope === 'dc' ? 'datacenter' : insights.resource?.key;
  if (!resource) return;
  const connection = state.selected, serial = ++insights.historySerial[scope];
  const hours = document.getElementById(scope === 'dc' ? 'dc-range' : 'resource-range').value;
  target.innerHTML = '<p class="history-empty">Loading history …</p>';
  try {
    const result = await api(`/connections/${connection}/history?resource=${encodeURIComponent(resource)}&hours=${hours}`);
    if (connection !== state.selected || serial !== insights.historySerial[scope]) return;
    renderHistory(target, result, resource === 'datacenter' || resource.startsWith('node/'));
  } catch (error) {
    if (connection === state.selected && serial === insights.historySerial[scope]) target.textContent = `Could not load history: ${error.message}`;
  }
}
document.addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.dataset.resourceKind) openResource(button.dataset.resourceKind, button.dataset.resourceId);
  if (button.dataset.openNode) openResource('node', button.dataset.openNode);
  if (button.dataset.nodeGuests) {$('#dc-search').value = button.dataset.nodeGuests; showView('datacenter');}
  if (button.dataset.guestActions) {
    selectGuest(Number(button.dataset.guestActions));
    const rect = button.getBoundingClientRect(); showContextMenu(rect.left, rect.bottom);
  }
});
document.getElementById('refresh-resource').addEventListener('click', () => {refreshResource(); loadHistory('resource');});
document.getElementById('refresh-datacenter').addEventListener('click', () => {refreshDatacenter(); loadHistory('dc'); sync();});
document.getElementById('resource-range').addEventListener('change', () => loadHistory('resource'));
document.getElementById('dc-range').addEventListener('change', () => loadHistory('dc'));
document.getElementById('dc-search').addEventListener('input', renderDatacenter);
setInterval(() => {if (!document.hidden && insights.view === 'resource') refreshResource();}, 15000);
setInterval(() => {
  if (document.hidden) return;
  if (insights.view === 'resource') loadHistory('resource');
  if (insights.view === 'datacenter') {refreshDatacenter(); loadHistory('dc');}
}, 60000);

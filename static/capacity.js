const capacityMonitor = {data: null, settings: null, error: '', serial: 0, busy: false, formRevision: null};
const capacityLabels = {fits: 'Capacity available', at_risk: 'Failover reserve not met', unknown: 'Capacity unverified', disabled: 'Monitoring disabled'};

function resetCapacityMonitor() {
  capacityMonitor.serial++; capacityMonitor.data = null; capacityMonitor.settings = null; capacityMonitor.error = ''; capacityMonitor.busy = false; capacityMonitor.formRevision = null;
  $('#capacity-banner').hidden = true;
  $('#capacity-monitor-status').textContent = 'Waiting for capacity monitoring.';
  $('#capacity-monitor-report').innerHTML = ''; $('#capacity-monitor-history').innerHTML = '';
  $('#capacity-monitor-error').textContent = ''; $('#capacity-monitor-form').reset();
}

function updateCapacityMarkup(selector, html) {
  const element = $(selector);
  if (element.innerHTML === html) return;
  const opened = new Set(Array.from(element.querySelectorAll('details')).filter(d => d.open).map(d => d.dataset.capacityHost || 'outer'));
  element.innerHTML = html;
  element.querySelectorAll('details').forEach(d => {d.open = opened.has(d.dataset.capacityHost || 'outer');});
}

function renderCapacityMonitor() {
  const data = capacityMonitor.data, banner = $('#capacity-banner');
  const config = data?.settings || capacityMonitor.settings;
  const needsAttention = capacityMonitor.error || ['at_risk', 'unknown'].includes(data?.status);
  banner.hidden = !(config?.enabled && config.show_cluster_warning && needsAttention && state.selected && insights.view !== 'connections');
  if (capacityMonitor.error) {
    banner.dataset.status = 'unknown';
    $('#capacity-banner-text').textContent = `N+1: Capacity unverified. Status refresh failed: ${capacityMonitor.error}`;
    $('#capacity-monitor-status').textContent = `Capacity status unavailable: ${capacityMonitor.error}`;
  }
  if (!data) return;
  const label = capacityLabels[data.status] || 'Capacity unverified';
  banner.dataset.status = data.status;
  $('#capacity-banner-text').textContent = `N+1: ${label}. ${data.message}`;
  $('#capacity-monitor-status').textContent = `${label}. ${data.message} Last check: ${dateSeconds(data.checked_at)}. ${config.enabled ? `Next check: ${data.next_at ? dateSeconds(data.next_at) : 'Pending'}.` : ''} RAM: ${config.memory_mode === 'configured' ? 'configured' : 'observed + margin'}.`;
  updateCapacityMarkup('#capacity-monitor-report', data.report ? `<details><summary>Last assessed failure scenarios${data.stale ? ' (stale)' : ''}</summary>${capacityReportMarkup(data.report)}</details>` : '');
  updateCapacityMarkup('#capacity-monitor-history', `<details><summary>Status changes / last seven days</summary><div class="table-scroll"><table class="policy-table"><thead><tr><th>Time</th><th>Status</th><th>RAM mode</th><th>Details</th></tr></thead><tbody>${data.history.map(e => `<tr><td>${dateSeconds(e.at)}</td><td>${escapeHtml(capacityLabels[e.status] || e.status)}</td><td>${escapeHtml(e.memory_mode)}</td><td>${escapeHtml(e.message)}${e.unplaced_guests.length ? `<small>Guests without a placement in at least one scenario: ${escapeHtml(e.unplaced_guests.join(', '))}</small>` : ''}</td></tr>`).join('') || '<tr><td colspan="4">No status changes recorded yet.</td></tr>'}</tbody></table></div><p class="group-note">Latest 30 changes. Repeated identical results do not create new entries.</p></details>`);
  const form = $('#capacity-monitor-form'), revision = `${state.selected}/${config.revision}`;
  if (capacityMonitor.formRevision !== revision) {
    form.elements.enabled.checked = config.enabled; form.elements.memory_mode.value = config.memory_mode;
    form.elements.show_cluster_warning.checked = !!config.show_cluster_warning;
    form.elements.interval_seconds.value = config.interval_seconds; capacityMonitor.formRevision = revision;
  }
  const role = access.me?.auth_enabled ? access.me?.user?.role : 'admin';
  for (const element of form.elements) element.disabled = capacityMonitor.busy || role !== 'admin';
  $('#refresh-capacity-monitor').disabled = capacityMonitor.busy || !config.enabled || !['admin', 'operator'].includes(role);
}

async function loadCapacityMonitor() {
  if (!state.selected || capacityMonitor.busy) return;
  const connection = state.selected, serial = ++capacityMonitor.serial;
  try {
    const data = await api(`/connections/${connection}/capacity-monitor`);
    if (connection !== state.selected || serial !== capacityMonitor.serial) return;
    capacityMonitor.data = data; capacityMonitor.settings = data.settings; capacityMonitor.error = ''; renderCapacityMonitor();
  } catch (error) {
    if (connection === state.selected && serial === capacityMonitor.serial) {
      capacityMonitor.data = null; capacityMonitor.error = error.message; renderCapacityMonitor();
    }
  }
}

async function changeCapacityMonitor(refresh = false) {
  if (!state.selected || !capacityMonitor.data || capacityMonitor.busy) return;
  const connection = state.selected, serial = ++capacityMonitor.serial, form = $('#capacity-monitor-form');
  const body = {enabled: form.elements.enabled.checked, memory_mode: form.elements.memory_mode.value,
    show_cluster_warning: form.elements.show_cluster_warning.checked,
    interval_seconds: Number(form.elements.interval_seconds.value), revision: capacityMonitor.data.settings.revision};
  capacityMonitor.busy = true; $('#capacity-monitor-error').textContent = ''; renderCapacityMonitor();
  try {
    const data = await api(`/connections/${connection}/capacity-monitor${refresh ? '/refresh' : ''}`,
      refresh ? {method: 'POST'} : {method: 'PUT', body: JSON.stringify(body)});
    if (connection !== state.selected || serial !== capacityMonitor.serial) return;
    capacityMonitor.data = data; capacityMonitor.settings = data.settings; capacityMonitor.error = '';
    $('#capacity-monitor-error').textContent = refresh ? 'Fresh assessment requested.' : 'Capacity monitoring settings saved.';
  } catch (error) {
    if (connection === state.selected && serial === capacityMonitor.serial) $('#capacity-monitor-error').textContent = error.message;
  } finally {
    if (connection === state.selected && serial === capacityMonitor.serial) {capacityMonitor.busy = false; renderCapacityMonitor();}
  }
}

document.getElementById('capacity-monitor-form').addEventListener('submit', event => {event.preventDefault(); changeCapacityMonitor();});
document.getElementById('refresh-capacity-monitor').addEventListener('click', () => changeCapacityMonitor(true));
document.getElementById('open-capacity-monitor').addEventListener('click', () => {
  showView('balancing'); drsTab('overview'); $('#capacity-panel').scrollIntoView?.({block: 'start'});
});
setInterval(() => {if (!document.hidden) loadCapacityMonitor();}, 30000);

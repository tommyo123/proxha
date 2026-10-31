const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
function newRequestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const value = crypto.getRandomValues(new Uint8Array(16));
  value[6] = (value[6] & 15) | 64;
  value[8] = (value[8] & 63) | 128;
  const hex = Array.from(value, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
const escapeHtml = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
// Map operation states to labels and status colors.
const stateLabels = {
  completed: ['Completed', 'good'], satisfied: ['Satisfied', 'good'], healthy: ['Healthy', 'good'], normal: ['Healthy', 'good'], online: ['Online', 'good'], review_ready: ['Ready to move', 'good'], acknowledged: ['Reviewed', 'good'], proposals: ['Moves proposed', 'good'],
  running: ['Running', 'active'], migrating: ['Migrating', 'active'], submitting: ['Submitting', 'active'], queued: ['Queued', 'active'], status_retry: ['Checking status', 'active'], verifying: ['Confirming placement', 'active'], retrying: ['Retrying', 'active'], stopping: ['Stopping', 'active'],
  observing: ['Monitoring', 'waiting'], waiting: ['Waiting', 'waiting'], settling: ['Settling', 'waiting'], scheduled: ['Scheduled', 'waiting'], scheduled_paused: ['Paused for schedule', 'waiting'], backup_paused: ['Paused for backup', 'waiting'], insufficient_data: ['Collecting data', 'waiting'], pending: ['Pending', 'waiting'], routine: ['Routine', 'waiting'], maintenance: ['Maintenance', 'waiting'], native_ha: ['Native HA', 'waiting'],
  failed: ['Failed', 'attention'], unknown: ['Needs review', 'attention'], completed_with_errors: ['Completed with errors', 'attention'], violated: ['Violated', 'attention'], degraded: ['Degraded', 'attention'], critical: ['Critical', 'attention'], interrupted: ['Interrupted', 'attention'], offline: ['Offline', 'attention'], policy_blocked: ['Blocked by policy', 'attention'],
  blocked: ['Blocked', 'idle'], no_feasible_move: ['No safe move', 'idle'], paused: ['Paused', 'idle'], disabled: ['Disabled', 'idle'], schedule_disabled: ['Schedule off', 'idle'], stopped: ['Stopped', 'idle'], cancelled_backup: ['Cancelled for backup', 'idle'], skipped: ['Skipped', 'idle'], report_only: ['Report only', 'idle']};
const stateLabel = value => stateLabels[value]?.[0] || String(value || 'Pending').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const stateBadge = (value, fallback = 'pending') => `<span class="task-state tone-${stateLabels[value || fallback]?.[1] || 'idle'} ${escapeHtml(value || fallback)}" title="${escapeHtml(value || fallback)}">${escapeHtml(stateLabel(value || fallback))}</span>`;
const icon = name => `<img class="icon" src="/static/icons/${name}.svg" alt="" draggable="false">`;
const icons = () => $$('[data-icon]').forEach(el => {el.innerHTML = icon(el.dataset.icon); el.removeAttribute('data-icon');});
const state = {connections: [], selected: null, cluster: null, connection: null, layout: 'topology', query: '', editing: null, syncing: false, generation: 0};
const bytes = n => !Number.isFinite(n) ? '—' : n === 0 ? '0 B' : n < 1073741824 ? `${(n / 1048576).toLocaleString('en-US', {maximumFractionDigits: 1})} MiB` : `${(n / 1073741824).toLocaleString('en-US', {maximumFractionDigits: 2})} GiB`;
const pct = n => !Number.isFinite(n) ? '—' : `${(n * 100).toLocaleString('en-US', {maximumFractionDigits: 1})}%`;
const time = value => value ? new Date(value).toLocaleString('en-GB', {dateStyle: 'short', timeStyle: 'medium', hourCycle: 'h23'}) : 'Never';

async function api(path, options = {}) {
  const res = await fetch('/api' + path, {...options, headers: {'Content-Type': 'application/json', 'X-ProxHA-Request': '1', ...options.headers}});
  if (res.status === 204) return null;
  const body = await res.json();
  if (res.status === 401 && path !== '/auth/login') showLogin();
  if (!res.ok) {const error = new Error(body.detail || 'Request failed.'); error.status = res.status; throw error;}
  return body;
}

const access = {me: null, users: []};

function showLogin() {
  const dialog = $('#login-dialog');
  if (!dialog.open) {$('#login-error').hidden = true; dialog.showModal(); dialog.querySelector('[name=username]').focus();}
}

async function loadAccess() {
  try {access.me = await api('/auth/me');} catch {return;}
  const user = access.me.user;
  $('#session-user').textContent = user ? user.username.slice(0, 2).toUpperCase() : '—';
  $('#session-user').title = user ? `${user.username} · ${user.role}` : 'Not signed in';
  $('#sign-out').hidden = !user;
  $('#change-password').hidden = !user;
  renderReplayControls();
  if (access.me.auth_enabled && !user) showLogin();
}

async function refreshAccessPanel() {
  const canManage = access.me?.user?.role === 'admin';
  $('#access-panel').hidden = !canManage;
  if (!canManage) return;
  $('#access-summary').textContent = `Signed in as ${access.me.user.username}. Administrators manage users and roles. Every user can change their own password from the top bar.`;
  try {
    const [{users}, {entries}] = await Promise.all([api('/users'), api('/audit?limit=100')]);
    access.users = users;
    $('#user-list').innerHTML = users.map(u => `<tr><td>${escapeHtml(u.username)}</td><td>${escapeHtml(u.role)}</td><td>${dateSeconds(u.created_at)}</td><td><button class="secondary" data-edit-user="${u.id}">Edit</button> <button class="secondary" data-delete-user="${u.id}">Remove</button></td></tr>`).join('');
    $('#audit-list').innerHTML = entries.map(e => `<tr><td>${dateSeconds(e.at)}</td><td>${escapeHtml(e.username || 'No sign-in')}${e.role ? `<small>${escapeHtml(e.role)}</small>` : ''}</td><td>${escapeHtml(e.method)} ${escapeHtml(e.path)}</td><td>${e.status}</td></tr>`).join('') || '<tr><td colspan="4">No changes recorded yet.</td></tr>';
  } catch (error) {$('#access-error').textContent = error.message; $('#access-error').hidden = false;}
}

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  try {
    const result = await api('/auth/login', {method: 'POST', body: JSON.stringify({username: form.elements.username.value, password: form.elements.password.value})});
    access.me = {auth_enabled: true, user: result.user};
    form.reset(); $('#login-dialog').close();
    location.reload();
  } catch (error) {$('#login-error').textContent = error.message; $('#login-error').hidden = false;}
});
$('#login-dialog').addEventListener('cancel', event => {if (access.me?.auth_enabled && !access.me.user) event.preventDefault();});
$('#sign-out').addEventListener('click', async () => {await api('/auth/logout', {method: 'POST'}); location.reload();});
$('#user-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target; $('#access-error').hidden = true;
  try {
    await api('/users', {method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(form)))});
    form.reset();
    await refreshAccessPanel();
  } catch (error) {$('#access-error').textContent = error.message; $('#access-error').hidden = false;}
});

function showPasswordChange() {
  const dialog = $('#password-dialog');
  $('#password-description').textContent = 'Choose any non-empty password. Other sessions will be signed out.';
  if (!dialog.open) {$('#password-form').reset(); $('#password-error').hidden = true; dialog.showModal();}
}
$('#change-password').addEventListener('click', () => showPasswordChange());
$('#cancel-password').addEventListener('click', () => $('#password-dialog').close());
$('#password-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, button = form.querySelector('[type=submit]');
  $('#password-error').hidden = true; button.disabled = true;
  try {
    if (form.elements.new_password.value !== form.elements.confirm_password.value) throw new Error('The new passwords do not match.');
    const {user} = await api('/auth/password', {method: 'POST', body: JSON.stringify({current_password: form.elements.current_password.value, new_password: form.elements.new_password.value})});
    access.me = {auth_enabled: true, user}; form.reset(); $('#password-dialog').close(); location.reload();
  } catch (error) {$('#password-error').textContent = error.message; $('#password-error').hidden = false;}
  finally {button.disabled = false;}
});

document.addEventListener('click', event => {
  const id = event.target.closest('[data-edit-user]')?.dataset.editUser;
  if (!id) return;
  const user = access.users.find(u => String(u.id) === id);
  if (!user) return;
  const form = $('#edit-user-form'); form.reset(); form.dataset.userId = id;
  form.elements.role.value = user.role; $('#edit-user-title').textContent = `Edit user: ${user.username}`;
  $('#edit-user-error').hidden = true; $('#edit-user-dialog').showModal();
});
$('#cancel-edit-user').addEventListener('click', () => $('#edit-user-dialog').close());
$('#edit-user-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, button = form.querySelector('[type=submit]'), value = {role: form.elements.role.value};
  if (form.elements.password.value) value.password = form.elements.password.value;
  button.disabled = true; $('#edit-user-error').hidden = true;
  try {
    await api(`/users/${form.dataset.userId}`, {method: 'PUT', body: JSON.stringify(value)});
    form.reset(); $('#edit-user-dialog').close(); await loadAccess(); await refreshAccessPanel();
  } catch (error) {$('#edit-user-error').textContent = error.message; $('#edit-user-error').hidden = false;}
  finally {button.disabled = false;}
});
document.addEventListener('click', async event => {
  const id = event.target.closest('[data-delete-user]')?.dataset.deleteUser;
  if (!id || !confirm('Remove this user? Their sessions end immediately.')) return;
  try {await api(`/users/${id}`, {method: 'DELETE'}); await loadAccess(); await refreshAccessPanel();} catch (error) {toast(error.message);}
});

let toastTimer;
function toast(message) {$('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 6000);}
function showView(view) {
  insights.view = view;
  ['overview', 'connections', 'balancing', 'datacenter', 'resource', 'activity'].forEach(v => $(`#${v}-view`).hidden = v !== view);
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#breadcrumb').textContent = {overview: 'Cluster overview', connections: 'Connections', balancing: 'Load balancing', datacenter: 'Datacenter', resource: 'Resource details'}[view];
  if (view === 'connections') {renderConnections(); refreshAccessPanel();}
  if (view === 'datacenter') {renderDatacenter(); refreshDatacenter(); loadHistory('dc');}
  if (view === 'balancing') {renderGroups(); refreshGroups();}
  if (view === 'activity') {renderActivity(); refreshActivity();}
  updateWorkspace();
}
function openConnect(id = null) {
  state.editing = id;
  const form = $('#connect-form'); form.reset();
  const connection = state.connections.find(c => c.id === id);
  if (connection) {['name', 'url', 'token_id'].forEach(k => form.elements[k].value = connection[k]); form.elements.verify_tls.checked = !!connection.verify_tls;}
  form.elements.token_secret.required = !connection;
  form.elements.token_secret.placeholder = connection ? 'Leave blank to keep the saved token' : 'The secret provided when the token was created';
  $('#dialog-title').textContent = connection ? 'Edit connection' : 'Connect cluster';
  resetTrust(connection);
  $('#form-error').hidden = true; $('#connect-dialog').showModal();
}
// Track saved, confirmed, pending and removed cluster certificates.
const trust = {saved: null, pending: null, removed: false, review: null, session: 0};
const fingerprintHtml = value => `<code class="fingerprint">${escapeHtml(value)}</code>`;
const caDate = value => value ? new Date(value).toLocaleDateString('en-GB', {dateStyle: 'medium'}) : '—';
function resetTrust(connection) {
  Object.assign(trust, {saved: connection?.trusted_ca || null, pending: null, removed: false, review: null, session: trust.session + 1});
  $('#cluster-ca-pem').value = ''; $('#cluster-ca-paste').hidden = true; $('#paste-cluster-ca').setAttribute('aria-expanded', 'false');
  showTrustError(''); renderTrust();
}
function trustedCa() {return trust.pending || (!trust.removed && trust.saved) || null;}
function renderTrust() {
  const current = trustedCa(), verify = $('#connect-form').elements.verify_tls;
  const validity = ca => `valid until ${escapeHtml(caDate(ca.not_after))}${ca.valid_now === false ? ', but not valid now, so verification will fail' : ''}`;
  $('#cluster-ca-status').innerHTML = trust.pending ? `This CA will be trusted when you save. SHA-256 ${fingerprintHtml(current.fingerprint)}, ${validity(current)}.`
    : current ? `Trusted: ${escapeHtml(current.subject)}. SHA-256 ${fingerprintHtml(current.fingerprint)}, ${validity(current)}. Every node certificate is verified with this CA.`
    : trust.removed ? 'The trusted CA will be removed when you save. The TLS setting above applies again.'
    : 'Not trusted. Proxmox signs every node certificate with the cluster CA, so trusting it verifies all nodes, including failover endpoints, with TLS verification on.';
  if (current) verify.checked = true;
  verify.disabled = !!current;
  $('#verify-tls-note').textContent = current ? 'Verified with the trusted cluster CA.' : 'Uses the system trust store, for example ACME certificates. For self-signed Proxmox certificates, trust the cluster CA below.';
  $('#trust-cluster-ca').hidden = $('#paste-cluster-ca').hidden = !!current;
  $('#remove-cluster-ca').hidden = !current;
  if (current) $('#cluster-ca-paste').hidden = true;
  $('#cluster-ca-review').hidden = !trust.review;
  const ca = trust.review;
  $('#cluster-ca-details').innerHTML = ca ? [ca.url ? ['Read from', `${ca.url} (${ca.node})`] : ['Source', 'Pasted certificate'], ['Subject', ca.subject],
    ['Valid', `${caDate(ca.not_before)} to ${caDate(ca.not_after)}${ca.valid_now ? '' : ' (not valid now)'}`]]
    .map(([label, value]) => `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>`).join('') + `<dt>SHA-256</dt><dd>${fingerprintHtml(ca.fingerprint)}</dd>` : '';
}
function showTrustError(message) {$('#cluster-ca-error').textContent = message; $('#cluster-ca-error').hidden = !message;}
async function reviewCa(button, path, body) {
  showTrustError(''); trust.review = null; renderTrust();
  const label = button.textContent, session = trust.session; button.disabled = true; button.textContent = 'Checking …';
  // A response for a dialog that was closed or reopened since is ignored.
  try {const result = await api(path, {method: 'POST', body: JSON.stringify(body)}); if (session === trust.session) {trust.review = result; renderTrust(); $('#confirm-cluster-ca').focus();}}
  catch (error) {if (session === trust.session) showTrustError(error.message);}
  finally {button.disabled = false; button.textContent = label;}
}
function connectionPayload(form) {
  const payload = Object.fromEntries(new FormData(form)); payload.verify_tls = form.elements.verify_tls.checked;
  // Omitting trusted_ca keeps the saved CA; an empty value removes it.
  if (trust.pending) Object.assign(payload, {trusted_ca: trust.pending.pem, trusted_ca_fingerprint: trust.pending.fingerprint});
  else if (trust.removed) payload.trusted_ca = '';
  return payload;
}
$('#trust-cluster-ca').addEventListener('click', () => {
  const form = $('#connect-form');
  if (!['name', 'url', 'token_id', 'token_secret'].every(name => form.elements[name].checkValidity())) {form.reportValidity(); return;}
  // The CA is read over the configured URL with the dialog's TLS setting; nothing is saved yet.
  reviewCa($('#trust-cluster-ca'), state.editing ? `/connections/${state.editing}/cluster-ca/fetch` : '/connections/cluster-ca/fetch', {...connectionPayload(form), trusted_ca: ''});
});
$('#paste-cluster-ca').addEventListener('click', () => {
  const panel = $('#cluster-ca-paste'); panel.hidden = !panel.hidden;
  $('#paste-cluster-ca').setAttribute('aria-expanded', String(!panel.hidden));
  if (!panel.hidden) $('#cluster-ca-pem').focus();
});
$('#check-cluster-ca').addEventListener('click', () => reviewCa($('#check-cluster-ca'), '/connections/cluster-ca/describe', {pem: $('#cluster-ca-pem').value}));
$('#confirm-cluster-ca').addEventListener('click', () => {trust.pending = trust.review; trust.review = null; renderTrust(); $('#remove-cluster-ca').focus();});
$('#reject-cluster-ca').addEventListener('click', () => {trust.review = null; renderTrust();});
$('#remove-cluster-ca').addEventListener('click', () => {if (trust.pending) trust.pending = null; else trust.removed = true; renderTrust();});
async function loadConnections(preferred) {
  state.connections = await api('/connections');
  $('#connection-count').textContent = state.connections.length;
  const selected = preferred ?? Number(localStorage.getItem('proxha.selected'));
  state.selected = (state.connections.find(c => c.id === selected) || state.connections[0])?.id ?? null;
  $('#cluster-select').innerHTML = state.connections.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');
  $('#cluster-select').hidden = state.connections.length < 2;
  if (state.selected) $('#cluster-select').value = state.selected;
  await loadCluster(); renderConnections();
}
async function loadCluster() {
  const generation = ++state.generation;
  resetInsights();
  resetGroups();
  workspace.activitySerial++; workspace.activity = null; workspace.resourceActivity = null; workspace.logSerial++; drs.historySerial++; drs.settingsRevision = null;
  $('#evacuation-result').textContent = ''; $('#task-log-dialog').close(); renderActivity();
  resetManagement(); state.cluster = null; state.connection = null; render();
  if (!state.selected) return;
  localStorage.setItem('proxha.selected', state.selected);
  const data = await api(`/connections/${state.selected}/cluster`);
  if (generation !== state.generation) return;
  state.cluster = data.cluster; state.connection = data.connection; render();
  loadCapacityMonitor();
  refreshHostStatus();
  refreshTasks();
  refreshActivity(); updateWorkspace();
  if (insights.view === 'balancing') refreshGroups();
}
async function sync() {
  if (!state.selected || state.syncing) return;
  const selected = state.selected, generation = state.generation;
  state.syncing = true; $('#refresh').disabled = true; $('#sync-status').textContent = 'Fetching fresh data …';
  try {
    const data = await api(`/connections/${selected}/sync`, {method: 'POST'});
    if (generation === state.generation) {state.cluster = data.cluster; state.connection = data.connection; await refreshHostStatus();}
  } catch (error) {
    if (generation === state.generation && state.connection) state.connection.last_error = error.message;
  } finally {state.syncing = false; render(); if (insights.view === 'balancing') refreshGroups();}
}
function metric(label, fraction) {
  const width = Number.isFinite(fraction) ? Math.max(0, Math.min(100, fraction * 100)) : 0;
  return `<div class="metric">${label}<strong>${pct(fraction)}</strong><div class="bar"><i style="width:${width}%"></i></div></div>`;
}
function guestRunning(g) {
  return g.status === 'running' && !['paused', 'suspended'].includes(g.qmpstatus);
}
function guestIcon(g) {
  const running = guestRunning(g);
  return `<span class="guest-icon" role="img" aria-label="${escapeHtml(g.qmpstatus || g.status || 'Unknown')}" title="${escapeHtml(g.qmpstatus || g.status || 'Unknown')}">${icon(g.type === 'lxc' ? 'cube' : 'computer-desktop')}${running ? '<span class="guest-play" aria-hidden="true">▶</span>' : ''}</span>`;
}
function guestButton(g) {
  return `<button class="guest ${guestRunning(g) ? 'is-running' : 'is-muted'}" data-machine="${Number(g.vmid)}">${guestIcon(g)}<span class="guest-name">${escapeHtml(g.name || `VM ${g.vmid}`)}<small>${g.vmid} · ${g.type === 'lxc' ? 'LXC' : 'QEMU'}${g.template ? ' · Template' : ''}</small></span></button>`;
}
function filteredGuests() {return (state.cluster?.guests || []).filter(g => `${g.name} ${g.vmid} ${g.node} ${g.type}`.toLowerCase().includes(state.query));}
function renderContent() {
  const target = $('#cluster-content');
  if (!state.cluster) {
    target.innerHTML = `<div class="empty-state"><div class="empty-graphic"><div class="ghost-dc">${icon('building-office-2')}</div><div class="tree-stem"></div><div class="tree-branch"></div><div class="ghost-nodes"><span>${icon('server')}</span><span>${icon('server')}</span><span>${icon('server')}</span></div></div><h2>Your cluster starts here</h2><p>Connect Proxmox to bring your hosts, virtual machines<br>and containers into one datacenter view.</p><button class="primary connect-button">${icon('plus')}Connect your first cluster</button><small>API token · Encrypted credentials · Local storage</small></div>`;
    return;
  }
  const guests = filteredGuests();
  if (state.layout === 'list') {
    target.innerHTML = guests.length ? `<div class="table-wrap"><table><thead><tr><th>GUEST</th><th>ID</th><th>TYPE</th><th>HOST</th><th>STATUS</th><th>CPU</th><th>MEMORY</th><th>BALLOONED</th></tr></thead><tbody>${guests.map(g => `<tr class="${guestRunning(g) ? 'is-running' : 'is-muted'}"><td><button data-machine="${Number(g.vmid)}">${guestIcon(g)}${escapeHtml(g.name || 'Unnamed')}</button></td><td>${g.vmid}</td><td>${g.type === 'lxc' ? 'LXC' : 'QEMU'}${g.template ? ' · Template' : ''}</td><td>${escapeHtml(g.node)}</td><td>${escapeHtml(g.status)}</td><td>${pct(g.cpu)}</td><td>${bytes(g.mem)}</td><td title="Maximum memory minus current balloon allocation">${bytes(g.balloon_reclaimed)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="guest-empty">No matching guests.</div>';
  } else {
    const nodes = state.cluster.nodes.filter(n => n.node.toLowerCase().includes(state.query) || guests.some(g => g.node === n.node));
    target.innerHTML = nodes.length ? `<div class="node-grid">${nodes.map(n => {
      const members = guests.filter(g => g.node === n.node);
      const balloon = ProxHASelection.summary(state.cluster.guests.filter(g => g.node === n.node && g.type === 'qemu' && g.status === 'running'), 'balloon_reclaimed');
      return `<article data-host-node="${escapeHtml(n.node)}" class="node-card ${maintenanceIdle(n) ? 'maintenance-idle' : ''} ${n.status !== 'online' ? 'is-offline' : ''}"><div class="node-header">${hostIcon(n, 'server-stack')}<div><h3><button class="node-detail-link" data-open-node="${escapeHtml(n.node)}">${escapeHtml(n.node)}</button></h3><small>${n.maxcpu ?? '—'} CPU · ${bytes(n.maxmem)}</small></div><span class="badge ${n.status === 'online' ? 'online' : 'offline'}">${n.status === 'online' ? 'Online' : 'Offline'}</span></div><div class="node-metrics">${metric('CPU', n.cpu)}${metric('Memory', n.maxmem ? n.mem / n.maxmem : null)}</div><div class="node-memory"><span title="Host KSM memory savings">Shared <strong>${bytes(n.ksm_shared)}</strong></span><span title="${balloon.known}/${balloon.total} running VMs reporting">Ballooned <strong>${bytes(balloon.value)}${balloon.known < balloon.total ? ' *' : ''}</strong></span></div><div class="guest-list">${members.length ? members.map(guestButton).join('') : '<div class="guest-empty">No visible guests</div>'}</div></article>`;
    }).join('')}</div>` : '<div class="guest-empty">No matches. Try another search.</div>';
    const orphans = guests.filter(g => !state.cluster.nodes.some(n => n.node === g.node));
    if (orphans.length) target.innerHTML += `<article class="node-card"><div class="node-header"><h3>Guests without visible host data</h3></div><div class="guest-list">${orphans.map(guestButton).join('')}</div></article>`;
  }
}
function render() {
  const c = state.cluster, conn = state.connection;
  const nodes = c?.nodes || [], guests = (c?.guests || []).filter(g => !g.template);
  const online = nodes.filter(n => n.status === 'online');
  const totalCpu = online.reduce((s, n) => s + (n.maxcpu || 0), 0), totalMem = online.reduce((s, n) => s + (n.maxmem || 0), 0);
  $('#node-stat').textContent = nodes.length; $('#node-caption').textContent = c ? `${online.length} online · ${nodes.length - online.length} offline` : 'No hosts connected';
  $('#vm-stat').textContent = guests.length; $('#vm-caption').textContent = c ? `${guests.filter(g => g.status === 'running').length} running · ${guests.filter(g => g.type === 'lxc').length} containers` : 'Waiting for a cluster';
  $('#cpu-stat').textContent = totalCpu ? pct(online.reduce((s, n) => s + (n.cpu || 0) * (n.maxcpu || 0), 0) / totalCpu) : '—';
  $('#memory-stat').textContent = totalMem ? pct(online.reduce((s, n) => s + (n.mem || 0), 0) / totalMem) : '—';
  $('#memory-caption').textContent = totalMem ? `${bytes(online.reduce((s, n) => s + (n.mem || 0), 0))} of ${bytes(totalMem)}` : 'No metrics yet';
  const shared = ProxHASelection.summary(nodes, 'ksm_shared');
  const balloon = ProxHASelection.summary(guests.filter(g => g.type === 'qemu' && g.status === 'running'), 'balloon_reclaimed');
  $('#shared-stat').textContent = bytes(shared.value);
  $('#shared-caption').textContent = c ? `${shared.known}/${shared.total} hosts reporting${shared.known < shared.total ? ' · Partial data' : ' · KSM memory savings'}` : 'Host memory saved through page sharing';
  $('#balloon-stat').textContent = bytes(balloon.value);
  $('#balloon-caption').textContent = c ? `${balloon.known}/${balloon.total} running VMs reporting${balloon.known < balloon.total ? ' · Partial data' : ''} · Max RAM minus balloon allocation` : 'Reclaimed from running VMs';
  $('#dc-name').textContent = c?.name || 'Datacenter';
  $('#dc-subtitle').textContent = c ? `${nodes.length} hosts · ${guests.length} guests${c.quorate === 0 ? ' · No quorum' : ''}` : 'Hosts, virtual machines and containers';
  const stale = conn?.last_error || (conn?.synced_at && Date.now() - Date.parse(conn.synced_at) > 90000);
  $('#cluster-status').textContent = stale ? 'Cached data' : c ? 'Connected' : 'Disconnected';
  $('#cluster-status').className = 'badge ' + (stale ? 'offline' : c ? 'online' : '');
  $('#warning').hidden = !stale && c?.quorate !== 0;
  $('#warning').textContent = stale ? `${conn.last_error || 'Metrics are older than 90 seconds.'} Showing cached data from ${time(conn.synced_at)}.` : 'The cluster reports that it has no quorum.';
  $('#sync-status').textContent = conn ? `Last updated ${time(conn.synced_at)}` : 'Waiting for the first connection';
  $('#footer-dot').className = c && !stale ? 'live' : '';
  $('#refresh').disabled = !state.selected || state.syncing;
  $('#pve-version').textContent = c ? c.version : '9.2 · API integration';
  renderContent(); renderTree(); updateWorkspace();
  if (insights.view === 'datacenter') renderDatacenter();
  if (insights.view === 'balancing') renderGroups();
}
function renderConnections() {
  $('#connections-list').innerHTML = state.connections.length ? state.connections.map(c => `<article class="connection-card"><span class="info-icon">${icon('link')}</span><div><h3>${escapeHtml(c.name)}</h3><p>${escapeHtml(c.url)} · ${c.trusted_ca ? 'TLS verified with the cluster CA' : c.verify_tls ? 'TLS verified' : 'TLS verification disabled'}<br>${escapeHtml(c.token_id)}${c.api_routing?.active ? `<br>Active API: ${escapeHtml(c.api_routing.active)} / ${c.api_routing.endpoints.length} known endpoints` : ''}</p></div><div class="actions"><button class="secondary" data-open="${c.id}">Open</button><button class="secondary" data-edit="${c.id}">Edit</button><button class="secondary" data-delete="${c.id}">Remove</button></div></article>`).join('') : '<article class="roadmap"><h2>No saved connections</h2><p>Add your first Proxmox cluster to get started.</p><button class="primary connect-button">Connect cluster</button></article>';
}
function machineDetails(id) {
  const guest = state.cluster?.guests.find(g => g.vmid === id);
  if (guest) openResource(guest.type, id);
}
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button) return;
  try {
    if (button.classList.contains('connect-button')) openConnect();
    if (button.classList.contains('close-dialog')) $('#connect-dialog').close();
    if (button.dataset.view) showView(button.dataset.view);
    if (button.dataset.layout) {state.layout = button.dataset.layout; $$('[data-layout]').forEach(b => b.classList.toggle('selected', b === button)); renderContent();}
    if (button.dataset.machine) machineDetails(Number(button.dataset.machine));
    if (button.dataset.edit) openConnect(Number(button.dataset.edit));
    if (button.dataset.open) {state.selected = Number(button.dataset.open); $('#cluster-select').value = state.selected; await loadCluster(); showView('overview'); await sync();}
    if (button.dataset.delete) {
      const id = Number(button.dataset.delete), conn = state.connections.find(c => c.id === id);
      if (confirm(`Remove “${conn.name}” and its locally saved data? The Proxmox cluster will not change.`)) {await api(`/connections/${id}`, {method: 'DELETE'}); await loadConnections(); toast('Connection removed.');}
    }
  } catch (e) {toast(e.message);}
});
$('#connect-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.target, submit = $('#connect-submit'); submit.disabled = true; submit.textContent = 'Testing connection …'; $('#form-error').hidden = true;
  const payload = connectionPayload(form);
  try {
    const result = await api(state.editing ? `/connections/${state.editing}` : '/connections', {method: state.editing ? 'PUT' : 'POST', body: JSON.stringify(payload)});
    form.elements.token_secret.value = ''; $('#connect-dialog').close(); await loadConnections(result.id); showView('overview'); toast('Connection saved. Your cluster is ready.');
  } catch (error) {$('#form-error').textContent = error.message; $('#form-error').hidden = false;}
  finally {submit.disabled = false; submit.textContent = 'Test and save connection';}
});
$('#connect-dialog').addEventListener('close', () => {$('#connect-form').elements.token_secret.value = ''; trust.session += 1;});
$('#close-details').addEventListener('click', () => $('#details-dialog').close());
$('#refresh').addEventListener('click', sync);
$('#search').addEventListener('input', e => {state.query = e.target.value.toLowerCase().trim(); renderContent();});
$('#cluster-select').addEventListener('change', async e => {state.selected = Number(e.target.value); try {await loadCluster(); await sync();} catch (error) {toast(error.message);}});
initializeWorkspace(); icons(); render(); loadAccess();
loadConnections().then(async () => {await restoreWorkspaceRoute(workspace.initialHash); await sync();}).catch(e => {$('#warning').textContent = `Could not load connections: ${e.message}`; $('#warning').hidden = false;});
setInterval(() => {if (!document.hidden) sync();}, 30000);
document.addEventListener('visibilitychange', () => {if (!document.hidden) {sync(); refreshTasks();}});

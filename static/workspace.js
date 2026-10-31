const workspace = {tab: 'summary', activity: null, resourceActivity: null, activitySerial: 0, logSerial: 0, restoring: false, initialHash: location.hash, searchSerial: 0, hostKey: null, hostState: null, hostSerial: 0, hostBusy: false, hostRequest: null};

function initializeWorkspace() {
  $('.top-right').insertAdjacentHTML('afterbegin', '<label class="global-search"><input id="global-search" type="search" placeholder="Search inventory · Ctrl+K" aria-label="Search all connected inventory"><div id="global-results" hidden></div></label><button class="secondary" id="workspace-health" data-view="activity">Tasks & Events</button>');
  const context = document.createElement('div'); context.id = 'workspace-context';
  context.innerHTML = `<div class="workspace-object"><span id="workspace-object-icon">${icon('building-office-2')}</span><h1 id="workspace-title">Infrastructure</h1></div>`;
  context.append($('.breadcrumb')); $('main').prepend(context);
  const toggle = document.createElement('button'); toggle.id = 'toggle-inventory'; toggle.className = 'icon-button';
  toggle.setAttribute('aria-controls', 'inventory-panel'); toggle.setAttribute('aria-label', 'Toggle inventory'); toggle.title = 'Toggle inventory';
  toggle.innerHTML = icon('list-bullet');
  $('.topbar').prepend(toggle, $('.brand'), $('.global-search'));
  $('.top-right').prepend($('#refresh'));
  $('.avatar').className = 'workspace-session'; $('.workspace-session').textContent = 'Local workspace';
  const setInventory = collapsed => {
    document.body.classList.toggle('inventory-collapsed', collapsed);
    $('#inventory-panel').hidden = collapsed; toggle.setAttribute('aria-expanded', String(!collapsed));
    localStorage.setItem('proxha.inventory-collapsed', String(collapsed));
  };
  toggle.addEventListener('click', () => setInventory(!$('#inventory-panel').hidden));
  const savedInventory = localStorage.getItem('proxha.inventory-collapsed');
  setInventory(savedInventory === null ? window.innerWidth <= 600 : savedInventory === 'true');
  $$('.sidebar .nav-item').forEach(button => {button.title = button.textContent.trim(); button.setAttribute('aria-label', button.title);});
  $('.tree-heading').insertAdjacentHTML('beforebegin', '<label class="sidebar-cluster">Cluster<select id="sidebar-cluster" aria-label="Inventory cluster"></select></label>');
  context.insertAdjacentHTML('afterend', '<nav id="cluster-tabs" class="object-tabs" aria-label="Cluster sections"><button data-view="overview">Summary</button><button data-view="datacenter">Monitor</button><button data-view="balancing">DRS</button><button data-view="activity">Tasks & Events</button></nav>');
  const view = $('#resource-view');
  view.querySelector('.page-heading').insertAdjacentHTML('afterend', '<div class="object-tabs" role="tablist" aria-label="Resource sections"><button data-resource-tab="summary" role="tab">Summary</button><button data-resource-tab="monitor" role="tab">Monitor</button><button data-resource-tab="configure" role="tab">Configure</button><button data-resource-tab="activity" role="tab">Tasks & Events</button></div>');
  for (const [tab, selectors] of Object.entries({summary: ['#resource-summary', '#resource-facts'], monitor: ['#resource-view .history-panel'], configure: ['#guest-policy-panel']})) {
    const panel = document.createElement('section'); panel.dataset.resourcePanel = tab;
    selectors.forEach(selector => panel.append(document.querySelector(selector)));
    view.append(panel);
  }
  view.querySelector('[data-resource-panel="configure"]').insertAdjacentHTML('beforeend', `<div id="node-configure" class="detail-panel host-maintenance" hidden><h2>Host maintenance</h2><p id="host-maintenance-status" role="status">Loading host state…</p><p>Enter maintenance excludes this host from placement and evacuates its guests using the parallel migration limit in DRS Settings. Submitted migrations finish when you stop evacuation. Local disks remain blocked. When HA resources are configured, native HA evacuates first; its parallelism is controlled by Proxmox. Optional SSH enables native maintenance, otherwise enable it in Proxmox and resume here. Exiting native maintenance can return HA guests to this host.</p><label class="checkbox"><input type="checkbox" id="maintenance-restart-containers" checked><span>Include containers (restart migration)<small>Enabled by default. Running containers shut down and restart on the target, causing downtime. Uncheck to leave running containers on this host.</small></span></label><div class="host-actions"><button class="primary" data-host-action="enter" disabled>Enter maintenance</button><button class="secondary" data-host-action="exit" disabled>Exit maintenance mode</button><button class="danger" data-host-action="stop" hidden>Stop evacuation</button><button class="secondary" data-host-action="reboot" disabled>Reboot host</button><button class="danger" data-host-action="shutdown" disabled>Shutdown host</button></div><p id="host-action-error" role="alert" hidden></p><p id="host-native-ha-status" role="status"></p><div id="host-maintenance-guests"></div><div id="host-power-status"></div><button class="secondary" data-host-action="acknowledge" hidden>I verified the host state</button><button class="secondary" id="preview-evacuation">Preview evacuation</button><div id="evacuation-result"></div></div>`);
  view.querySelector('.resource-actions').insertAdjacentHTML('afterbegin', '<button class="secondary" id="host-maintenance-shortcut" hidden>Maintenance</button>');
  document.body.insertAdjacentHTML('beforeend', '<dialog id="host-power-dialog"><form id="host-power-form"><h2 id="host-power-title"></h2><p id="host-power-description"></p><label>Type the host name to confirm<input name="confirm_node" required autocomplete="off"></label><p id="host-power-error" role="alert" hidden></p><div class="dialog-actions"><button class="secondary" type="button" id="cancel-host-power">Cancel</button><button class="danger" type="submit">Confirm host action</button></div></form></dialog>');
  $('#host-maintenance-shortcut').addEventListener('click', () => resourceTab('configure'));
  $$('[data-host-action]').forEach(button => button.addEventListener('click', () => hostAction(button.dataset.hostAction)));
  $('#host-power-form').addEventListener('submit', submitHostPower);
  $('#cancel-host-power').addEventListener('click', () => {if (!workspace.hostBusy) $('#host-power-dialog').close();});
  $('#host-power-dialog').addEventListener('cancel', event => {if (workspace.hostBusy) event.preventDefault();});
  view.insertAdjacentHTML('beforeend', '<section data-resource-panel="activity" hidden><div id="resource-activity"></div></section>');
  const dock = document.createElement('details'); dock.id = 'task-dock';
  dock.innerHTML = '<summary><span>Recent tasks</span><span id="dock-status">Background tracking active</span></summary><div id="dock-native"></div>';
  dock.append($('.task-panel')); $('.shell').append(dock);
  dock.open = localStorage.getItem('proxha.task-dock-open') !== 'false';
  document.body.classList.toggle('task-dock-collapsed', !dock.open);
  dock.addEventListener('toggle', () => {document.body.classList.toggle('task-dock-collapsed', !dock.open); localStorage.setItem('proxha.task-dock-open', String(dock.open));});
  document.body.insertAdjacentHTML('beforeend', '<dialog id="task-log-dialog"><div class="panel-heading"><h2>Task log</h2><button class="secondary" id="close-task-log">Close</button></div><pre id="task-log-content"></pre></dialog>');
  $('#close-task-log').addEventListener('click', () => {workspace.logSerial++; $('#task-log-dialog').close();});
  $('#preview-evacuation').addEventListener('click', previewEvacuation);
  $('#refresh-activity').addEventListener('click', refreshActivity);
  $('#sidebar-cluster').addEventListener('change', async event => {state.selected = Number(event.target.value); await loadCluster(); showView('overview'); await sync();});
  $('#global-search').addEventListener('input', searchInventory);
  document.addEventListener('keydown', event => {if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {event.preventDefault(); $('#global-search').focus();}});
  document.addEventListener('click', async event => {
    const tab = event.target.closest('[data-resource-tab]');
    if (tab) resourceTab(tab.dataset.resourceTab);
    const object = event.target.closest('[data-open-resource]');
    if (object) {
      const connection = Number(object.dataset.connection) || state.selected;
      if (connection !== state.selected) {state.selected = connection; await loadCluster();}
      const [kind, ...parts] = object.dataset.openResource.split('/');
      await openResource(kind, parts.join('/')); $('#global-results').hidden = true;
    }
    const log = event.target.closest('[data-task-log]');
    if (log) {
      const connection = state.selected, serial = ++workspace.logSerial;
      $('#task-log-content').textContent = 'Loading …'; $('#task-log-dialog').showModal();
      try {const data = await api(`/connections/${connection}/activity/log?upid=${encodeURIComponent(log.dataset.taskLog)}`); if (connection === state.selected && serial === workspace.logSerial) $('#task-log-content').textContent = data.lines.join('\n');}
      catch (error) {if (connection === state.selected && serial === workspace.logSerial) $('#task-log-content').textContent = error.message;}
    }
    if (!event.target.closest('.global-search')) $('#global-results').hidden = true;
  });
  window.addEventListener('hashchange', () => restoreWorkspaceRoute(location.hash));
  resourceTab('summary'); drsTab('overview');
}

function resourceTab(tab) {
  if (!['summary', 'monitor', 'configure', 'activity'].includes(tab)) return;
  workspace.tab = tab;
  $$('[data-resource-panel]').forEach(panel => panel.hidden = panel.dataset.resourcePanel !== tab);
  $$('[data-resource-tab]').forEach(button => {const active = button.dataset.resourceTab === tab; button.classList.toggle('selected', active); button.setAttribute('aria-selected', active);});
  $('#node-configure').hidden = insights.resource?.kind !== 'node';
  $('#host-maintenance-shortcut').hidden = insights.resource?.kind !== 'node';
  if (tab === 'configure' && insights.resource?.kind === 'node') refreshNodeMaintenance();
  if (tab === 'activity') {renderActivity(); refreshActivity();}
  saveWorkspaceRoute();
}

function updateWorkspace() {
  renderCapacityMonitor();
  $('#sidebar-cluster').innerHTML = state.connections.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');
  $('#sidebar-cluster').value = state.selected ?? '';
  $('#sidebar-cluster').disabled = !state.connections.length;
  $('#cluster-tabs').hidden = insights.view === 'connections' || insights.view === 'resource';
  $$('#cluster-tabs button').forEach(button => button.classList.toggle('selected', button.dataset.view === insights.view));
  const name = state.connection?.name || 'Workspace';
  const object = insights.view === 'resource' ? $('#resource-title').textContent : ({overview: 'Summary', datacenter: 'Monitor', balancing: 'DRS', activity: 'Tasks & Events', connections: 'Connections'}[insights.view] || 'Summary');
  $('#workspace-title').textContent = insights.view === 'resource' ? object : insights.view === 'connections' ? 'Connections' : state.cluster?.name || state.connection?.name || 'Infrastructure';
  $('#workspace-object-icon').innerHTML = icon(insights.view === 'resource' ? insights.resource?.kind === 'node' ? 'server' : 'computer-desktop' : 'building-office-2');
  document.body.dataset.view = insights.view;
  const selectedHost = insights.view === 'resource' && insights.resource?.kind === 'node' ? state.cluster?.nodes.find(n => n.node === insights.resource.identifier) : null;
  $('#workspace-title').classList.toggle('maintenance-idle', maintenanceIdle(selectedHost));
  $('.workspace-object').classList.toggle('is-offline', !!selectedHost && selectedHost.status !== 'online');
  if (selectedHost) $('.workspace-object').dataset.hostNode = selectedHost.node;
  else delete $('.workspace-object').dataset.hostNode;
  if (selectedHost) $('#workspace-object-icon').innerHTML = hostIcon(selectedHost);
  $('#breadcrumb').textContent = `${name} / ${object}`;
  saveWorkspaceRoute();
}

function saveWorkspaceRoute() {
  if (workspace.restoring || !state.selected) return;
  const route = new URLSearchParams({connection: state.selected, view: insights.view});
  if (insights.view === 'resource' && insights.resource) {route.set('resource', insights.resource.key); route.set('tab', workspace.tab);}
  if (insights.view === 'balancing') route.set('tab', drs.tab);
  history.replaceState(null, '', '#' + route.toString());
}

async function restoreWorkspaceRoute(hash) {
  if (!hash) return;
  const route = new URLSearchParams(hash.slice(1)), connection = Number(route.get('connection'));
  if (!state.connections.some(c => c.id === connection)) return;
  workspace.restoring = true;
  try {
    if (connection !== state.selected) {state.selected = connection; await loadCluster();}
    const view = route.get('view'), resource = route.get('resource');
    if (view === 'resource' && /^(node\/[A-Za-z0-9.-]+|(?:qemu|lxc)\/[0-9]+)$/.test(resource || '')) {
      const [kind, identifier] = resource.split('/'); await openResource(kind, identifier); resourceTab(route.get('tab') || 'summary');
    } else if (['overview', 'datacenter', 'balancing', 'activity', 'connections'].includes(view)) {
      showView(view); if (view === 'balancing') drsTab(route.get('tab') || 'overview');
    }
  } finally {workspace.restoring = false; updateWorkspace();}
}

async function searchInventory() {
  const query = $('#global-search').value.trim(), serial = ++workspace.searchSerial;
  if (!query) {$('#global-results').hidden = true; return;}
  try {
    const data = await api(`/inventory?q=${encodeURIComponent(query)}`);
    if (serial !== workspace.searchSerial) return;
    $('#global-results').innerHTML = data.items.length ? data.items.map(item => `<button data-open-resource="${escapeHtml(item.resource)}" data-connection="${item.connection_id}"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.connection_name)} · ${escapeHtml(item.resource)} · ${escapeHtml(item.node)}</small></button>`).join('') : '<p>No matching hosts or guests.</p>';
    $('#global-results').hidden = false;
  } catch (error) {if (serial === workspace.searchSerial) {$('#global-results').textContent = error.message; $('#global-results').hidden = false;}}
}

function activityMarkup(data, resource = null, compact = false) {
  const tasks = (data?.tasks || []).filter(task => !resource || task.resource === resource || resource === `node/${task.node}` || resource === `node/${task.target}`);
  const events = (data?.placements || []).filter(event => !resource || event.resource === resource || resource === `node/${event.source}` || resource === `node/${event.target}`);
  return `${!compact ? `<p class="collection-status">${escapeHtml(data?.coverage_note || 'Waiting for background task collection.')} · Retention 7 days${data?.collector?.archive_complete ? ' · Archive scan caught up' : ' · Archive scan incomplete'}</p>${data?.collector?.last_error ? `<p class="warning">${escapeHtml(data.collector.last_error)}</p>` : ''}` : ''}
    ${tasks.length ? `<div class="table-scroll"><table class="policy-table activity-table"><thead><tr><th>Started</th><th>Task / guest</th><th>Source → target</th><th>Origin</th><th>State</th><th>Details</th></tr></thead><tbody>${tasks.slice(0, compact ? 5 : 200).map(task => `<tr><td>${dateSeconds(task.started_at)}<small>${task.finished_at ? 'Finished ' + dateSeconds(task.finished_at) : ['completed', 'failed'].includes(task.status) ? 'Finish time unavailable' : 'In progress / unresolved'}</small></td><td>${escapeHtml(task.kind)} ${task.resource ? `<button class="text-button" data-open-resource="${escapeHtml(task.resource)}">${escapeHtml(task.resource)}</button>` : ''}</td><td>${escapeHtml(task.source || task.node)}${task.source ? ' → ' + escapeHtml(task.target || 'Unknown') : ''}</td><td>${escapeHtml(task.origin)}<small>${escapeHtml(task.reason || 'Trigger unknown')}</small></td><td>${stateBadge(task.status)}<small>${escapeHtml(task.message || '')}</small></td><td><button class="secondary" data-task-log="${escapeHtml(task.upid)}">Log</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="guest-empty">No visible tasks in this scope yet.</p>'}
    ${!compact && events.length ? `<h3>Observed placement changes</h3><ul>${events.map(event => `<li>${dateSeconds(event.observed_at)} · ${escapeHtml(event.resource)} · ${escapeHtml(event.source)} → ${escapeHtml(event.target)} <small>(observed between ${dateSeconds(event.previous_at)} and ${dateSeconds(event.observed_at)})</small></li>`).join('')}</ul>` : ''}`;
}

function renderActivity() {
  const data = workspace.activity;
  $('#activity-main').innerHTML = activityMarkup(data);
  const scoped = workspace.resourceActivity;
  $('#resource-activity').innerHTML = activityMarkup(scoped?.connection === state.selected && scoped?.resource === insights.resource?.key ? scoped.data : data, insights.resource?.key);
  $('#dock-native').innerHTML = activityMarkup(data, null, true);
  const running = (data?.tasks || []).filter(task => task.status === 'running').length;
  const stale = !data?.collector?.checked_at || Date.now() / 1000 - data.collector.checked_at > 180;
  $('#dock-status').textContent = `${running} running · Background reconciliation · Seven-day history`;
  const policy = stale ? null : data?.collector?.native_policy;
  const crs = policy?.options?.crs;
  const crsLabel = crs && typeof crs === 'object' ? Object.entries(crs).map(([key, value]) => `${key}=${value}`).join(' · ') : crs;
  $('#workspace-health').textContent = stale || data?.collector?.last_error ? 'Tasks & Events ? Check collection' : 'Tasks & Events';
  const native = data?.collector?.native_ha;
  $('#drs-ownership').innerHTML = policy ? `<p><strong>Native HA:</strong> ${Array.isArray(policy.ha_resources) ? policy.ha_resources.length + ' managed resources' : 'Unavailable'}${policy.ha_resources?.length === 0 ? ' / No guests are protected by native HA.' : ''}</p><p>State: ${escapeHtml(native?.state || 'Unknown')} / Native affinity rules: ${Array.isArray(policy.ha_rules) ? policy.ha_rules.length : 'Unavailable'} / Maintenance SSH: ${native?.ssh_configured ? 'Configured' : 'Not configured'}</p><p>CRS configuration: <code>${escapeHtml(crsLabel || 'Not explicitly configured or unavailable')}</code></p><p class="group-note">Enable HA guest balancing in DRS Settings to include eligible resources. Native affinity and compatibility groups are checked before submission. ProxHA does not register guests with HA.</p>` : '<p>Native HA/CRS status is unavailable or stale. Check Tasks &amp; Events collection.</p>';
}

async function refreshActivity() {
  const connection = state.selected, serial = ++workspace.activitySerial;
  if (!connection) return;
  try {
    const resource = insights.view === 'resource' && workspace.tab === 'activity' ? insights.resource?.key : null;
    const [data, scoped] = await Promise.all([api(`/connections/${connection}/activity`), resource ? api(`/connections/${connection}/activity?resource=${encodeURIComponent(resource)}`) : null]);
    if (connection !== state.selected || serial !== workspace.activitySerial) return;
    workspace.activity = data;
    if (resource === insights.resource?.key && scoped) workspace.resourceActivity = {connection, resource, data: scoped};
    renderActivity();
  }
  catch (error) {if (connection === state.selected && serial === workspace.activitySerial) $('#activity-main').textContent = error.message;}
}

function hostActionUnavailable(action, data) {
  if (['reset', 'force-stop'].includes(action)) return 'Not supported by the Proxmox host API. BMC/IPMI integration is required.';
  if (workspace.hostBusy) return 'A host action is being submitted.';
  if (!data) return 'Refresh the host state before running actions.';
  const pendingPower = ['submitting', 'accepted', 'unknown'].includes(data.power_action?.status);
  if (action === 'stop') return !data.run?.active ? 'No active evacuation to stop.' : data.run.stop_requested ? 'Evacuation is already stopping.' : null;
  if (action === 'acknowledge') return data.power_action?.status === 'unknown' ? null : 'No unknown power action to acknowledge.';
  if (!data.online) return 'The host is offline.';
  if (pendingPower) return 'Wait for or resolve the current host power action.';
  if (data.state === 'exiting') return 'Waiting for native HA to acknowledge maintenance exit.';
  if (action === 'enter' && data.enabled) return 'The host is already in maintenance mode.';
  if (action === 'resume' && (!data.enabled || data.state === 'maintenance')) return 'No incomplete evacuation to resume.';
  if (['enter', 'resume'].includes(action)) return data.queue_active ? 'A migration queue is already active.' : null;
  if (action === 'exit') return !data.enabled ? 'The host is not in maintenance mode.' : data.run?.active ? 'Stop or finish evacuation before exiting maintenance.' : null;
  if (['reboot', 'shutdown'].includes(action)) return data.state !== 'maintenance' || data.remaining.length ? 'Enter maintenance and evacuate all guests first.' : data.queue_active ? 'Stop or finish the migration queue first.' : null;
  return 'Unsupported host action.';
}

function renderNodeMaintenance() {
  const data = workspace.hostState, busy = workspace.hostBusy, run = data?.run;
  $('#host-maintenance-status').textContent = data ? `${stateLabel(data.state)} · ${data.remaining.length} guest${data.remaining.length === 1 ? ' remains' : 's remain'}. Parallel limit: ${data.concurrency || 3}${run?.active ? ` (${run.in_flight || 0} in flight)` : ''}. ${data.message}${run?.active && run.message !== data.message ? ' ' + run.message : ''}` : 'Loading host state…';
  $('#host-native-ha-status').textContent = data?.native_ha ? `Native HA: ${stateLabel(data.native_ha.state)} / ${data.native_ha.owned ? 'Managed by ProxHA' : 'Enabled outside ProxHA'}. ${data.native_ha.message}` : '';
  $('#host-maintenance-shortcut').textContent = data?.enabled ? `Maintenance: ${data.state}` : 'Maintenance';
  $$('[data-host-action]').forEach(button => {
    const action = button.dataset.hostAction;
    const reason = hostActionUnavailable(action === 'enter' && data?.enabled ? 'resume' : action, data);
    button.disabled = !!reason; button.title = reason || '';
    if (action === 'enter') button.textContent = data?.enabled ? 'Resume evacuation' : 'Enter maintenance mode';
    if (action === 'stop') button.hidden = !run?.active;
    if (action === 'acknowledge') button.hidden = data?.power_action?.status !== 'unknown';
  });
  if (run?.active) $('#maintenance-restart-containers').checked = !!run.restart_containers;
  $('#maintenance-restart-containers').disabled = busy || !!run?.active;
  $('#host-maintenance-guests').innerHTML = data?.blockers?.length ? `<h3>Evacuation admission details</h3><ul class="host-admission-details">${data.blockers.map(item => `<li><strong>${escapeHtml(item.resource)}${item.name ? ' · ' + escapeHtml(item.name) : ''}</strong> ${escapeHtml(item.reason)}<small>${item.retryable ? 'Temporary constraint: checked automatically while evacuation is active.' : 'Manual action required.'}</small></li>`).join('')}</ul>` : data?.remaining.length ? `<p>Remaining: ${data.remaining.map(item => `${escapeHtml(item.resource)} (${escapeHtml(item.status)})`).join(', ')}</p>` : '';
  $('#host-power-status').textContent = data?.power_action ? `${data.power_action.command}: ${data.power_action.status}. ${data.power_action.message}` : '';
}

async function refreshNodeMaintenance() {
  if (!state.selected || insights.resource?.kind !== 'node' || workspace.hostBusy) return;
  const connection = state.selected, node = insights.resource.identifier, key = `${connection}/${node}`, serial = ++workspace.hostSerial;
  if (workspace.hostKey !== key) {workspace.hostKey = key; workspace.hostState = null; workspace.hostRequest = null; $('#maintenance-restart-containers').checked = true; $('#host-action-error').hidden = true;}
  renderNodeMaintenance();
  try {
    const data = await api(`/connections/${connection}/nodes/${encodeURIComponent(node)}/maintenance`);
    if (serial !== workspace.hostSerial || key !== `${state.selected}/${insights.resource?.identifier}` || insights.resource?.kind !== 'node') return;
    workspace.hostState = data;
    const inventoryNode = state.cluster?.nodes.find(n => n.node === node);
    if (inventoryNode) {if (data.enabled) inventoryNode.maintenance = data.state; else delete inventoryNode.maintenance; inventoryNode.status = data.online ? 'online' : 'offline'; inventoryNode.maintenance_idle = data.enabled && data.remaining.every(g => g.status === 'stopped'); renderHostIndicators();}
    renderNodeMaintenance();
  } catch (error) {
    if (serial === workspace.hostSerial && key === `${state.selected}/${insights.resource?.identifier}`) {workspace.hostState = null; renderNodeMaintenance(); $('#host-maintenance-status').textContent = 'Host state unavailable.'; $('#host-action-error').textContent = `Could not refresh host state: ${error.message}`; $('#host-action-error').hidden = false;}
  }
}

async function hostAction(action) {
  const data = workspace.hostState;
  if (!data || workspace.hostBusy || insights.resource?.kind !== 'node') return;
  const connection = state.selected, node = insights.resource.identifier, key = `${connection}/${node}`;
  if (workspace.hostKey !== key) return;
  const reason = hostActionUnavailable(action === 'enter' && data.enabled ? 'resume' : action, data);
  if (reason) {toast(reason); return;}
  if (['reboot', 'shutdown'].includes(action)) {
    workspace.powerDialog = {connection, node, action, request: newRequestId()};
    $('#host-power-title').textContent = `${action === 'reboot' ? 'Reboot' : 'Shutdown'} ${node}`;
    $('#host-power-description').textContent = `This sends a ${action} command to the physical host ${node}. The host must be empty and in maintenance. Maintenance stays enabled afterward.`;
    $('#host-power-form').reset(); $('#host-power-error').hidden = true; $('#host-power-dialog').showModal(); return;
  }
  if (action === 'acknowledge' && !confirm(`Confirm you have checked ${node} directly and verified the outcome of the last power action. This permits a new command.`)) return;
  workspace.hostBusy = true; workspace.hostSerial++; $('#host-action-error').hidden = true; renderNodeMaintenance();
  try {
    const base = `/connections/${connection}/nodes/${encodeURIComponent(node)}`;
    if (action === 'enter') {
      workspace.hostRequest ||= newRequestId();
      const response = await api(base + '/maintenance/enter', {method: 'POST', body: JSON.stringify({request_id: workspace.hostRequest, restart_containers: $('#maintenance-restart-containers').checked})});
      if (key === `${state.selected}/${insights.resource?.identifier}`) {workspace.hostRequest = null; drs.run = response.run; renderDrs();}
    } else if (action === 'exit') await api(base + '/maintenance/exit', {method: 'POST'});
    else if (action === 'stop') await api(`/connections/${connection}/balancing/runs/${encodeURIComponent(data.run.id)}/stop`, {method: 'POST'});
    else if (action === 'acknowledge') await api(base + `/power/${encodeURIComponent(data.power_action.id)}/acknowledge`, {method: 'POST'});
  } catch (error) {
    if (key === `${state.selected}/${insights.resource?.identifier}`) {$('#host-action-error').textContent = error.message; $('#host-action-error').hidden = false; if (error.status) workspace.hostRequest = null;}
  } finally {workspace.hostBusy = false; await refreshNodeMaintenance();}
}

async function submitHostPower(event) {
  event.preventDefault();
  const action = workspace.powerDialog, form = event.target;
  if (!action || workspace.hostBusy) return;
  const confirmed = form.elements.confirm_node.value;
  if (confirmed !== action.node || action.connection !== state.selected || action.node !== insights.resource?.identifier || insights.resource?.kind !== 'node') {
    $('#host-power-error').textContent = 'Select the intended host and type its exact name to confirm.'; $('#host-power-error').hidden = false; return;
  }
  workspace.hostBusy = true; form.querySelector('[type=submit]').disabled = true; renderNodeMaintenance();
  try {
    const result = await api(`/connections/${action.connection}/nodes/${encodeURIComponent(action.node)}/power`, {method: 'POST', body: JSON.stringify({request_id: action.request, command: action.action, confirm_node: confirmed})});
    if (action.connection === state.selected && action.node === insights.resource?.identifier) {workspace.hostState.power_action = result; renderNodeMaintenance();}
    $('#host-power-dialog').close();
  } catch (error) {$('#host-power-error').textContent = error.message; $('#host-power-error').hidden = false;}
  finally {workspace.hostBusy = false; form.querySelector('[type=submit]').disabled = false; await refreshNodeMaintenance();}
}

async function previewEvacuation() {
  if (insights.resource?.kind !== 'node') return;
  const connection = state.selected, resource = insights.resource.key, node = insights.resource.identifier;
  $('#evacuation-result').textContent = 'Calculating evacuation targets …';
  try {
    const data = await api(`/connections/${connection}/nodes/${encodeURIComponent(node)}/evacuation-preview`);
    if (connection !== state.selected || resource !== insights.resource?.key) return;
    $('#evacuation-result').innerHTML = `<p class="collection-status">${escapeHtml(data.maintenance.state)} · ${data.maintenance.planned_guests} of ${data.maintenance.running_guests} running guests have a tentative target${data.maintenance.native_ha_guests ? `; native HA evacuates ${data.maintenance.native_ha_guests} HA ${data.maintenance.native_ha_guests === 1 ? 'resource' : 'resources'} first` : ''}. Host maintenance state is unchanged.</p>${planMarkup(data)}`;
  } catch (error) {if (connection === state.selected && resource === insights.resource?.key) $('#evacuation-result').textContent = error.message;}
}

setInterval(() => {if (!document.hidden && state.selected) refreshActivity();}, 15000);

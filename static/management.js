const management = {selected: new Set(), anchor: null, collapsed: new Set(), query: '', dragging: false, dragConnection: null, pending: null, tasks: [], polling: false, submitting: false, hostSerial: 0, hostPolling: false, hostMenu: null};
const actionLabels = {start: 'Start', shutdown: 'Shutdown', stop: 'Stop', reset: 'Reset', reboot: 'Reboot', hibernate: 'Hibernate', 'free-balloon': 'Free balloon', migrate: 'Migrate'};

function maintenanceIdle(node) {
  return !!node?.maintenance && (node.maintenance_idle ?? (state.cluster?.guests || []).filter(g => g.node === node.node).every(g => g.status === 'stopped'));
}
function hostIcon(node, name = 'server') {
  const idle = maintenanceIdle(node);
  return `<span class="host-symbol ${idle ? 'maintenance-idle' : ''}" ${idle ? 'title="Maintenance: no running guests"' : ''}>${icon(name)}${idle ? '<svg class="host-maintenance-strike" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 22L22 2" fill="none" stroke="#c52222" stroke-width="3" stroke-linecap="round"/></svg>' : ''}</span>`;
}

function renderHostIndicators() {
  renderTree(); renderContent(); updateWorkspace();
  if (insights.view === 'datacenter') renderDatacenter();
}
async function refreshHostStatus() {
  if (!state.selected || !state.cluster || management.hostPolling) return;
  const connection = state.selected, serial = management.hostSerial;
  management.hostPolling = true;
  try {
    const data = await api(`/connections/${connection}/host-status`);
    if (connection !== state.selected || serial !== management.hostSerial) return;
    for (const node of state.cluster?.nodes || []) {
      const fresh = data.nodes.find(n => n.node === node.node);
      if (fresh) Object.assign(node, fresh);
    }
    renderHostIndicators();
  } catch (_) {
    // Keep the last known state until a successful refresh.
  } finally {management.hostPolling = false;}
}

function resetManagement() {
  management.hostSerial++; management.hostMenu = null;
  management.selected.clear(); management.anchor = null; management.collapsed.clear(); management.tasks = [];
  management.pending = null; management.query = ''; management.dragging = false;
  document.querySelector('#tree-search').value = '';
  document.querySelector('#context-menu').hidden = true;
  document.querySelector('#action-dialog').close();
  renderTasks();
}
function treeGroups() {
  const c = state.cluster;
  if (!c) return [];
  const names = [...new Set([...c.nodes.map(n => n.node), ...c.guests.map(g => g.node)])].sort();
  return names.map(name => {
    const node = c.nodes.find(n => n.node === name) || {node: name, status: 'unknown'};
    const guests = c.guests.filter(g => g.node === name && `${g.name} ${g.vmid} ${g.node}`.toLowerCase().includes(management.query)).sort((a, b) => a.vmid - b.vmid);
    return {node, guests};
  }).filter(group => group.guests.length || group.node.node.toLowerCase().includes(management.query));
}
function visibleGuestIds() {
  return treeGroups().flatMap(({node, guests}) => management.collapsed.has(node.node) && !management.query ? [] : guests.map(g => g.vmid));
}
function selectedGuests() {return (state.cluster?.guests || []).filter(g => management.selected.has(g.vmid));}
function selectionChanged() {
  document.querySelectorAll('[data-tree-guest]').forEach(el => {
    const selected = management.selected.has(Number(el.dataset.treeGuest));
    el.classList.toggle('selected', selected); el.setAttribute('aria-selected', String(selected));
  });
  document.querySelector('#selection-count').textContent = `${management.selected.size} selected`;
  document.querySelector('#selection-actions').disabled = !management.selected.size;
}
function renderTree() {
  if (management.dragging) return;
  const tree = $('#resource-tree');
  const focusKey = tree.contains(document.activeElement) ? document.activeElement.dataset.treeGuest || document.activeElement.dataset.treeNode : null;
  const guests = state.cluster?.guests || [];
  management.selected = new Set([...management.selected].filter(id => guests.some(g => g.vmid === id)));
  if (!state.cluster) {
    tree.innerHTML = '<p class="tree-empty">Connect a cluster to browse its resources.</p>'; selectionChanged(); return;
  }
  tree.innerHTML = `<button class="tree-root" data-view="datacenter" role="treeitem" aria-expanded="true" aria-level="1">${icon('building-office-2')}<span>${escapeHtml(state.cluster.name)}</span></button><div role="group">${treeGroups().map(({node, guests}) => {
    const expanded = !management.collapsed.has(node.node) || !!management.query;
    return `<div class="tree-host-group"><button class="tree-node ${maintenanceIdle(node) ? 'maintenance-idle' : ''} ${node.status !== 'online' ? 'is-offline' : ''}" role="treeitem" aria-expanded="${expanded}" aria-level="2" data-tree-node="${escapeHtml(node.node)}" title="${escapeHtml(node.node)} · ${escapeHtml(node.status)}"><span class="tree-chevron">${expanded ? '▾' : '▸'}</span>${hostIcon(node)}<span class="host-name">${escapeHtml(node.node)}</span><small>${guests.length}</small>${node.maintenance ? `<span class="maintenance-badge" title="ProxHA maintenance: ${escapeHtml(node.maintenance)}">Maintenance</span>` : ''}</button><div role="group" ${expanded ? '' : 'hidden'}>${guests.map(g => `<button class="tree-guest ${guestRunning(g) ? 'is-running' : 'is-muted'}" role="treeitem" aria-level="3" aria-selected="${management.selected.has(g.vmid)}" data-tree-guest="${g.vmid}" draggable="${!g.template}" title="${escapeHtml(g.name || g.vmid)} · ${escapeHtml(g.status)}${g.template ? ' · Template' : ''}">${guestIcon(g)}<span><b>${g.vmid}</b> ${escapeHtml(g.name || 'Unnamed')}</span></button>`).join('')}</div></div>`;
  }).join('')}</div>`;
  selectionChanged();
  if (focusKey) [...tree.querySelectorAll('button')].find(el => (el.dataset.treeGuest || el.dataset.treeNode) === focusKey)?.focus({preventScroll: true});
}
function selectGuest(id, event = {}) {
  const result = ProxHASelection.update(management.selected, management.anchor, id, visibleGuestIds(), event);
  management.selected = result.selected; management.anchor = result.anchor; selectionChanged();
}
function unavailable(action, guests = selectedGuests()) {
  if (!state.connection || state.connection.last_error || Date.now() - Date.parse(state.connection.synced_at) > 90000) return 'Refresh the cluster before running actions.';
  if (state.cluster?.quorate === 0) return 'Cluster has no quorum.';
  if (management.submitting) return 'A request is being submitted.';
  if (management.tasks.some(op => op.tasks.some(t => ['running', 'submitting', 'unknown'].includes(t.status) && guests.some(g => g.vmid === t.vmid)))) return 'A selected guest has an active or unresolved task.';
  return ProxHASelection.unavailable(action, guests, state.cluster?.nodes || []);
}
function showContextMenu(x, y) {
  const menu = $('#context-menu'), guests = selectedGuests();
  management.hostMenu = null; menu.setAttribute('aria-label', 'Guest actions');
  menu.innerHTML = `<div class="menu-heading">${guests.length} guest${guests.length === 1 ? '' : 's'} selected</div>${Object.entries(actionLabels).map(([action, label]) => {
    const reason = unavailable(action, guests);
    return `<button role="menuitem" data-action="${action}" ${reason ? 'disabled' : ''} title="${escapeHtml(reason || label)}">${label}<small>${action === 'hibernate' ? 'Save to disk' : action === 'stop' ? 'Force off' : action === 'reset' ? 'Hard reset' : action === 'free-balloon' ? 'Return full RAM to VM' : ''}</small></button>`;
  }).join('')}${guests.length === 1 && guests[0].status === 'stopped' ? `<button role="menuitem" data-start-best="${escapeHtml(guests[0].type)}/${guests[0].vmid}">Start on best host<small>Move offline first if needed</small></button>` : ''}`;
  positionContextMenu(x, y);
}
function positionContextMenu(x, y) {
  const menu = $('#context-menu');
  menu.hidden = false;
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
  menu.querySelector('button:not(:disabled)')?.focus();
}
async function showHostContextMenu(node, x, y) {
  const menu = $('#context-menu'), request = {connection: state.selected, node, x, y};
  management.hostMenu = request;
  menu.setAttribute('aria-label', 'Host actions');
  menu.innerHTML = `<div class="menu-heading">${escapeHtml(node)}</div><p class="menu-note">Loading host actions…</p>`;
  positionContextMenu(x, y);
  try {
    const data = await api(`/connections/${request.connection}/nodes/${encodeURIComponent(node)}/maintenance`);
    if (management.hostMenu !== request || request.connection !== state.selected || menu.hidden) return;
    request.data = data;
    const actions = [['enter', 'Enter maintenance mode'], ['resume', 'Resume evacuation'], ['exit', 'Exit maintenance mode'], ['stop', 'Stop evacuation'], ['reboot', 'Reboot'], ['shutdown', 'Shutdown'], ['reset', 'Reset (hard)'], ['force-stop', 'Stop (force power off)']];
    menu.innerHTML = `<div class="menu-heading">${escapeHtml(node)} · ${data.online ? 'Online' : 'Offline'}</div>${actions.map(([action, label]) => {
      const reason = hostActionUnavailable(action, data);
      return `<button role="menuitem" data-host-menu-action="${action}" ${reason ? 'disabled' : ''} title="${escapeHtml(reason || label)}">${label}</button>`;
    }).join('')}<p class="menu-note">Container evacuation shuts down and restarts containers on the target. Hard reset and force power off require BMC/IPMI integration.</p>`;
    positionContextMenu(x, y);
  } catch (error) {
    if (management.hostMenu === request && !menu.hidden) menu.innerHTML = `<div class="menu-heading">${escapeHtml(node)}</div><p class="menu-note">${escapeHtml(error.message)}</p>`;
  }
}
async function beginHostMenuAction(action) {
  const request = management.hostMenu;
  $('#context-menu').hidden = true;
  if (!request || request.connection !== state.selected || hostActionUnavailable(action, request.data)) return;
  await openResource('node', request.node);
  resourceTab('configure');
  await refreshNodeMaintenance();
  if (request.connection !== state.selected || insights.resource?.kind !== 'node' || insights.resource.identifier !== request.node) return;
  const reason = hostActionUnavailable(action, workspace.hostState);
  if (reason) {toast(reason); return;}
  await hostAction(action === 'resume' ? 'enter' : action);
}
function beginAction(action, target = null) {
  $('#context-menu').hidden = true;
  const guests = selectedGuests();
  const reason = unavailable(action, guests);
  if (reason) {toast(reason); return;}
  management.pending = {connection: state.selected, action, vmids: guests.map(g => g.vmid), request_id: newRequestId(), submitted: false};
  $('#action-form').reset();
  ['migration-target', 'local-disks', 'restart-containers'].forEach(id => document.getElementById(id).disabled = false);
  $('#action-title').textContent = action === 'free-balloon' ? `Free balloon · VM ${guests[0].vmid}` : `${actionLabels[action]} ${guests.length} guest${guests.length === 1 ? '' : 's'}`;
  const descriptions = {
    start: 'Start the selected guests. Hibernated VMs resume from their saved state.',
    shutdown: 'Ask the guest operating systems to shut down cleanly.',
    stop: 'Immediately power off these guests. Unsaved data may be lost.',
    reset: 'Hard reset these VMs without a clean operating system shutdown. Unsaved data may be lost.',
    reboot: 'Ask the guest operating systems to reboot cleanly.',
    hibernate: 'Save VM memory to disk and stop the VMs. They resume on the next start. Requires suitable storage and additional permissions.',
    'free-balloon': 'Request full RAM immediately from the VM balloon driver. The driver may take a few seconds to respond. Proxmox can reclaim RAM again under host memory pressure.',
    migrate: 'Move the selected guests to another host. Running VMs use live migration; stopped guests remain stopped.'
  };
  $('#action-description').textContent = descriptions[action];
  $('#action-guests').innerHTML = guests.map(g => `<div><strong>${g.vmid} · ${escapeHtml(g.name || 'Unnamed')}</strong><span>${escapeHtml(g.node)} · ${g.type === 'lxc' ? 'LXC' : 'VM'} · ${escapeHtml(g.status)}${action === 'free-balloon' ? ` · RAM ${bytes(g.balloon)} → ${bytes(g.maxmem)}` : ''}</span></div>`).join('');
  $('#migration-options').hidden = action !== 'migrate';
  const targets = (state.cluster?.nodes || []).filter(n => n.status === 'online' && guests.every(g => g.node !== n.node));
  $('#migration-target').innerHTML = targets.map(n => `<option value="${escapeHtml(n.node)}">${escapeHtml(n.node)}</option>`).join('');
  if (target) {
    if (!targets.some(n => n.node === target)) {management.pending = null; toast('Choose an online host different from every selected guest’s source host.'); return;}
    $('#migration-target').value = target;
  }
  $('#restart-option').hidden = !guests.some(g => g.type === 'lxc' && g.status === 'running');
  $('#restart-containers').required = action === 'migrate' && !$('#restart-option').hidden;
  $('#action-note').textContent = action === 'free-balloon' ? 'Requires available host RAM and Sys.Modify on /. VM memory settings stay unchanged. Recent tasks confirms when full RAM is reported; the command is sent only once.' : 'Proxmox validates permissions, locks and resource compatibility. Each guest is tracked separately; a batch is not atomic.';
  $('#action-error').hidden = true;
  $('#submit-action').textContent = actionLabels[action];
  $('#submit-action').disabled = false;
  $('#action-dialog').showModal();
}
function renderTasks() {
  const target = document.querySelector('#task-list');
  if (!management.tasks.length) {target.innerHTML = '<p class="guest-empty">No tasks yet.</p>'; return;}
  target.innerHTML = management.tasks.map(op => `<article class="task-batch"><div class="task-batch-heading"><strong>${escapeHtml(actionLabels[op.payload.action] || op.payload.action)}${op.payload.target ? ' → ' + escapeHtml(op.payload.target) : ''}</strong><span>${time(op.created_at)}</span></div>${op.tasks.map(t => `<div class="task-row"><span><strong>${t.vmid} · ${escapeHtml(t.name || 'Unnamed')}</strong><small>${escapeHtml(t.node)}${t.upid ? ' · ' + escapeHtml(t.upid) : ''}</small></span>${stateBadge(t.status)}<p>${escapeHtml(t.message)}</p></div>`).join('')}${op.tasks.some(t => t.status === 'unknown') ? `<p class="task-unknown">The request outcome is unknown. Check the task log and guest state in Proxmox before retrying.</p><button class="secondary" data-acknowledge="${escapeHtml(op.id)}">I checked Proxmox, acknowledge unknown outcome</button>` : ''}</article>`).join('');
}
async function refreshTasks() {
  if (!state.selected || management.polling) return;
  const connection = state.selected, generation = state.generation;
  management.polling = true;
  try {
    const tasks = await api(`/connections/${connection}/operations`);
    if (generation !== state.generation) return;
    const completed = tasks.some(op => op.tasks.some(t => ['completed', 'failed'].includes(t.status) && management.tasks.some(old => old.tasks.some(ot => ot.id === t.id && ot.status === 'running'))));
    management.tasks = tasks; renderTasks();
    if (completed) sync();
  } catch (error) {
    if (generation === state.generation) $('#task-list').textContent = `Could not refresh task status: ${error.message}`;
  } finally {management.polling = false;}
}

document.querySelector('#resource-tree').addEventListener('click', event => {
  const guest = event.target.closest('[data-tree-guest]');
  if (guest) {
    selectGuest(Number(guest.dataset.treeGuest), event);
    if (!event.shiftKey && !event.ctrlKey && !event.metaKey) machineDetails(Number(guest.dataset.treeGuest));
  }
  const node = event.target.closest('[data-tree-node]');
  if (node) {
    const name = node.dataset.treeNode;
    if (event.target.closest('.tree-chevron')) {management.collapsed.has(name) ? management.collapsed.delete(name) : management.collapsed.add(name); renderTree();}
    else openResource('node', name);
  }
});
document.querySelector('#resource-tree').addEventListener('dblclick', event => {
  const guest = event.target.closest('[data-tree-guest]');
  if (guest) machineDetails(Number(guest.dataset.treeGuest));
});
document.querySelector('#resource-tree').addEventListener('contextmenu', event => {
  const guest = event.target.closest('[data-tree-guest]');
  if (!guest) return;
  event.preventDefault();
  const id = Number(guest.dataset.treeGuest);
  if (!management.selected.has(id)) selectGuest(id);
  showContextMenu(event.clientX, event.clientY);
});
document.querySelector('#resource-tree').addEventListener('keydown', event => {
  const row = event.target.closest('button'); if (!row) return;
  const id = Number(row.dataset.treeGuest);
  if ((event.key === 'F10' && event.shiftKey) || event.key === 'ContextMenu') {
    event.preventDefault(); if (id && !management.selected.has(id)) selectGuest(id);
    const rect = row.getBoundingClientRect();
    if (row.dataset.treeNode) showHostContextMenu(row.dataset.treeNode, rect.right, rect.top);
    else if (id) showContextMenu(rect.right, rect.top);
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {event.preventDefault(); management.selected = new Set(visibleGuestIds()); selectionChanged(); return;}
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault(); const rows = [...document.querySelectorAll('#resource-tree button')].filter(r => !r.closest('[hidden]'));
    const index = rows.indexOf(row), nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
    const next = rows[nextIndex]; next?.focus();
    if (next?.dataset.treeGuest && (!event.ctrlKey || event.shiftKey)) selectGuest(Number(next.dataset.treeGuest), event);
  }
  if (row.dataset.treeNode && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {event.preventDefault(); event.key === 'ArrowLeft' ? management.collapsed.add(row.dataset.treeNode) : management.collapsed.delete(row.dataset.treeNode); renderTree();}
});
document.querySelector('#resource-tree').addEventListener('dragstart', event => {
  const guest = event.target.closest('[data-tree-guest]');
  if (!guest || guest.draggable === false) {event.preventDefault(); return;}
  const id = Number(guest.dataset.treeGuest);
  if (!management.selected.has(id)) selectGuest(id);
  const reason = unavailable('migrate');
  if (reason) {event.preventDefault(); toast(reason); return;}
  management.dragging = true; management.dragConnection = state.selected;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('application/x-proxha-guests', JSON.stringify({connection: state.selected, ids: [...management.selected]}));
  event.dataTransfer.setData('text/plain', `${management.selected.size} guests`);
});
document.querySelector('#resource-tree').addEventListener('dragover', event => {
  const node = event.target.closest('[data-tree-node]');
  if (!node || !management.dragging || management.dragConnection !== state.selected) return;
  const name = node.dataset.treeNode;
  if (!state.cluster.nodes.some(n => n.node === name && n.status === 'online') || selectedGuests().some(g => g.node === name)) return;
  event.preventDefault(); event.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.drop-target').forEach(el => el.classList.remove('drop-target')); node.classList.add('drop-target');
});
document.querySelector('#resource-tree').addEventListener('dragleave', event => {
  const node = event.target.closest('[data-tree-node]'); if (node && !node.contains(event.relatedTarget)) node.classList.remove('drop-target');
});
document.querySelector('#resource-tree').addEventListener('drop', event => {
  const node = event.target.closest('[data-tree-node]');
  if (!node || !management.dragging || management.dragConnection !== state.selected) return;
  event.preventDefault(); management.dragging = false;
  beginAction('migrate', node.dataset.treeNode); renderTree();
});
document.querySelector('#resource-tree').addEventListener('dragend', () => {management.dragging = false; renderTree();});
document.querySelector('#tree-search').addEventListener('input', event => {management.query = event.target.value.trim().toLowerCase(); renderTree();});
document.querySelector('#clear-selection').addEventListener('click', () => {management.selected.clear(); management.anchor = null; selectionChanged();});
document.querySelector('#selection-actions').addEventListener('click', event => {const rect = event.currentTarget.getBoundingClientRect(); showContextMenu(rect.left, rect.top);});
document.querySelector('#context-menu').addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  if (button.dataset.hostMenuAction) beginHostMenuAction(button.dataset.hostMenuAction);
  else if (button.dataset.action) beginAction(button.dataset.action);
});
document.querySelector('#context-menu').addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    event.preventDefault(); $('#context-menu').hidden = true;
    const host = [...$$('[data-tree-node]')].find(el => el.dataset.treeNode === management.hostMenu?.node);
    (host || $('#selection-actions')).focus();
  }
  if (['ArrowDown', 'ArrowUp'].includes(event.key)) {event.preventDefault(); const buttons = [...event.currentTarget.querySelectorAll('button:not(:disabled)')], index = buttons.indexOf(document.activeElement); buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length]?.focus();}
});
document.addEventListener('click', event => {if (!event.target.closest('#context-menu, #selection-actions, [data-guest-actions]')) document.querySelector('#context-menu').hidden = true;});
document.addEventListener('keydown', event => {if (event.key === 'Escape') document.querySelector('#context-menu').hidden = true;});
document.addEventListener('contextmenu', event => {
  const guest = event.target.closest('[data-machine]');
  if (guest) {
    event.preventDefault();
    const id = Number(guest.dataset.machine);
    if (!management.selected.has(id)) selectGuest(id);
    showContextMenu(event.clientX, event.clientY);
    return;
  }
  const host = event.target.closest('[data-tree-node], [data-open-node], [data-host-node], [data-resource-kind="node"]');
  if (!host || event.target.closest('[data-machine], [data-tree-guest]')) return;
  event.preventDefault();
  showHostContextMenu(host.dataset.treeNode || host.dataset.openNode || host.dataset.hostNode || host.dataset.resourceId, event.clientX, event.clientY);
});
setInterval(() => {if (!document.hidden) refreshHostStatus();}, 15000);
['cancel-action', 'cancel-action-top'].forEach(id => document.getElementById(id).addEventListener('click', () => {if (!management.submitting) document.querySelector('#action-dialog').close();}));
document.querySelector('#action-dialog').addEventListener('cancel', event => {if (management.submitting) event.preventDefault();});
document.querySelector('#action-form').addEventListener('submit', async event => {
  event.preventDefault(); const pending = management.pending;
  if (!pending || management.submitting || pending.connection !== state.selected) return;
  management.submitting = true; $('#submit-action').disabled = true; $('#action-error').hidden = true;
  $('#submit-action').textContent = 'Submitting …';
  if (!pending.payload) pending.payload = {request_id: pending.request_id, action: pending.action, vmids: pending.vmids, target: pending.action === 'migrate' ? $('#migration-target').value : null, with_local_disks: $('#local-disks').checked, restart_containers: $('#restart-containers').checked};
  ['migration-target', 'local-disks', 'restart-containers'].forEach(id => document.getElementById(id).disabled = true);
  try {
    const result = await api(`/connections/${pending.connection}/operations`, {method: 'POST', body: JSON.stringify(pending.payload)});
    if (pending.connection === state.selected) {
      management.tasks = [result, ...management.tasks.filter(op => op.id !== result.id)]; renderTasks();
      $('#action-dialog').close(); showView('overview'); toast('Request recorded. See Recent tasks for each guest’s result.');
      refreshTasks(); sync();
    }
  } catch (error) {
    if (pending.connection !== state.selected) return;
    $('#action-error').textContent = error.status && error.status < 500 ? error.message : 'The request outcome could not be confirmed. Check Recent tasks or retry this same request; its request ID prevents duplicate submission.';
    $('#action-error').hidden = false;
    if (error.status && error.status < 500) {
      pending.payload = null; pending.request_id = newRequestId();
      ['migration-target', 'local-disks', 'restart-containers'].forEach(id => document.getElementById(id).disabled = false);
    }
    refreshTasks();
  } finally {management.submitting = false; $('#submit-action').disabled = false; $('#submit-action').textContent = 'Submit request';}
});
document.querySelector('#refresh-tasks').addEventListener('click', refreshTasks);
document.querySelector('#task-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-acknowledge]'); if (!button) return;
  if (!confirm('Have you checked Proxmox and confirmed this guest has no pending operation? Acknowledging releases the local action lock.')) return;
  try {await api(`/connections/${state.selected}/operations/${button.dataset.acknowledge}/acknowledge`, {method: 'POST'}); refreshTasks();} catch (error) {toast(error.message);}
});
setInterval(() => {if (!document.hidden) refreshTasks();}, 5000);

// Reuse the request ID for ten minutes to resume incomplete start operations.
const startRequests = new Map();

async function startOnBestHost(resource) {
  const connection = state.selected; if (!connection) return;
  const key = `${connection}/${resource}`, saved = startRequests.get(key);
  const request = saved && Date.now() - saved.at < 600000 ? saved : {id: newRequestId(), at: Date.now()};
  $('#context-menu').hidden = true;
  try {
    const plan = await api(`/connections/${connection}/guests/${resource}/start-placement`);
    if (!plan.target) {toast(`No host can start ${resource}: ${plan.options.map(o => `${o.node}: ${o.reason}`).join('; ')}`); return;}
    const question = plan.migrate ? `Move ${resource} offline from ${plan.current} to ${plan.target}, then start it there?` : `${plan.current} is already the best host. Start ${resource} there?`;
    if (!confirm(question)) return;
    toast(plan.migrate ? `Moving ${resource} to ${plan.target} before starting…` : `Starting ${resource}…`);
    startRequests.set(key, request);
    await api(`/connections/${connection}/guests/${resource}/start-on-best`, {method: 'POST', body: JSON.stringify({request_id: request.id})});
    startRequests.delete(key);
    toast(`${resource} start submitted on ${plan.target}.`); refreshTasks();
  } catch (error) {toast(error.message);}
}
document.addEventListener('click', event => {
  const resource = event.target.closest('[data-start-best]')?.dataset.startBest;
  if (resource) startOnBestHost(resource);
});

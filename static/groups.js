const migrationGroups = {data: null, error: '', serial: 0, editing: null, saving: false, evaluation: null, evaluationError: ''};

function resetGroups() {
  resetResourceGroups(); resetPlacementRules();
  clearDrsRun(); resetDrsDiagnostics();
  drs.run = null; drs.queueSerial++; drs.startRequest = null; drs.stopping = false;
  drs.continuous = null; drs.continuousBusy = false; drs.continuousDesired = null; drs.aggressivenessBusy = false; drs.aggressivenessSerial++; drs.continuousSerial++; $('#continuous-error').hidden = true;
  migrationGroups.serial++;
  migrationGroups.data = null;
  migrationGroups.error = '';
  migrationGroups.evaluation = null; migrationGroups.evaluationError = '';
  migrationGroups.editing = null;
  document.getElementById('group-dialog').close();
}

async function refreshGroups() {
  const serial = ++migrationGroups.serial, connection = state.selected;
  if (!connection) {migrationGroups.data = null; renderGroups(); return;}
  refreshResourceGroups(); refreshPlacementRules();
  refreshMigrationQueue();
  refreshBalancing(connection, serial);
  try {
    const data = await api(`/connections/${connection}/migration-groups`);
    if (serial !== migrationGroups.serial || connection !== state.selected) return;
    migrationGroups.data = data; migrationGroups.error = '';
  } catch (error) {
    if (serial !== migrationGroups.serial || connection !== state.selected) return;
    migrationGroups.data = null; migrationGroups.error = error.message;
  }
  renderGroups();
}

function renderGroups() {
  renderBalancing();
  const data = migrationGroups.data;
  $('#group-cluster').innerHTML = state.connections.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');
  $('#group-cluster').value = state.selected ?? '';
  $('#group-cluster').disabled = !state.selected;
  $('#new-group').disabled = !data?.nodes.length;
  $('#refresh-groups').disabled = !state.selected;
  $('#group-error').hidden = !migrationGroups.error;
  $('#group-error').textContent = migrationGroups.error;
  if (!state.selected || !data) {
    $('#group-summary').textContent = !state.selected ? 'Connect a cluster to define migration domains.' : migrationGroups.error ? 'Migration domains are unavailable.' : 'Loading migration domains …';
    $('#group-list').innerHTML = !state.selected ? '<article class="roadmap"><h2>No cluster connected</h2><p>Groups are saved separately for each connection.</p><button class="primary connect-button">Connect cluster</button></article>' : '';
    $('#group-matrix').innerHTML = ''; return;
  }
  const enabled = data.groups.filter(g => g.enabled).length;
  $('#group-summary').textContent = `${data.groups.length} migration domains · ${enabled} enabled · ${data.unassigned.length} unassigned hosts. Automatic migrations stay within each group.`;
  const nodes = new Map(data.nodes.map(n => [n.node, n]));
  $('#group-list').innerHTML = data.groups.length ? data.groups.map(group => {
    const online = group.nodes.filter(name => nodes.get(name)?.status === 'online').length;
    return `<article class="migration-group ${group.enabled ? '' : 'group-disabled'}"><div class="group-card-heading"><span class="info-icon">${icon('server-stack')}</span><div><h2>${escapeHtml(group.name)}</h2><p>${group.nodes.length} hosts · ${online} online</p></div><span class="badge ${group.enabled ? 'online' : ''}">${group.enabled ? 'Enabled' : 'Disabled'}</span></div>
      ${group.notes ? `<p class="group-notes">${escapeHtml(group.notes)}</p>` : ''}
      <div class="group-members">${group.nodes.map(name => `<span class="group-host ${nodes.get(name)?.status === 'online' ? '' : 'host-unavailable'}">${icon('server')}<strong>${escapeHtml(name)}</strong><small>${escapeHtml(nodes.get(name)?.status || 'Missing from inventory')}</small></span>`).join('')}</div>
      <p class="group-note">${!group.enabled ? 'Automatic live migration is blocked for this group.' : online < 2 ? 'At least two online hosts are needed for an automatic migration target.' : 'Automatic live migration may use hosts in this group after guest checks pass.'}</p>
      <div class="group-card-actions"><button class="secondary" data-edit-group="${group.id}">Edit group</button><button class="secondary" data-delete-group="${group.id}">Remove</button></div></article>`;
  }).join('') : '<article class="roadmap"><h2>No migration domains yet</h2><p>Create a group for hosts you have verified can live-migrate VMs between each other. Unassigned hosts are excluded from automatic live migration.</p><button class="primary" data-new-group>Create first group</button></article>';
  if (data.unassigned.length) $('#group-list').insertAdjacentHTML('beforeend', `<article class="unassigned-hosts"><h3>Unassigned hosts</h3><p>${data.unassigned.map(escapeHtml).join(' · ')}</p><small>Excluded from automatic live migration.</small></article>`);
  const pairs = new Map(data.pairs.map(pair => [`${pair.source}/${pair.target}`, pair]));
  $('#group-matrix').innerHTML = `${!data.inventory_fresh ? '<p class="warning">Inventory is stale or unavailable. Refresh to evaluate online hosts.</p>' : ''}<div class="table-scroll"><table class="policy-table"><caption>Group policy preview · Source host → target host</caption><thead><tr><th scope="col">Source / target</th>${data.nodes.map(n => `<th scope="col">${escapeHtml(n.node)}</th>`).join('')}</tr></thead><tbody>${data.nodes.map(source => `<tr><th scope="row">${escapeHtml(source.node)}</th>${data.nodes.map(target => {const p = pairs.get(`${source.node}/${target.node}`); return `<td><span class="policy-cell ${p.allowed ? 'policy-allowed' : 'policy-blocked'}" title="${escapeHtml(p.reason)}" aria-label="${escapeHtml(`${source.node} to ${target.node}: ${p.reason}`)}">${source.node === target.node ? '—' : p.allowed ? 'Allowed' : 'Blocked'}</span></td>`;}).join('')}</tr>`).join('')}</tbody></table></div><p class="group-note">Allowed means the group boundary permits this pair. CPU features, VM configuration, HA rules, capacity, storage and networking still need validation. LXC restart migration is outside this live-only policy.</p>`;
}

async function refreshBalancing(connection, serial) {
  try {
    const data = await api(`/connections/${connection}/balancing`);
    if (serial !== migrationGroups.serial || connection !== state.selected) return;
    if (data.settings?.revision < migrationGroups.evaluation?.settings?.revision) data.settings = migrationGroups.evaluation.settings;
    migrationGroups.evaluation = data; migrationGroups.evaluationError = '';
  } catch (error) {
    if (serial !== migrationGroups.serial || connection !== state.selected) return;
    migrationGroups.evaluation = null; migrationGroups.evaluationError = error.message;
  }
  renderBalancing();
}

function renderBalancing() { renderDrs(); }

function openGroup(id = null) {
  const data = migrationGroups.data;
  if (!data || migrationGroups.saving) return;
  const group = data.groups.find(g => g.id === id);
  migrationGroups.editing = {id, revision: group?.revision, connection: state.selected, generation: state.generation};
  const form = $('#group-form'); form.reset();
  form.elements.name.value = group?.name || '';
  form.elements.notes.value = group?.notes || '';
  form.elements.enabled.checked = group?.enabled ?? true;
  $('#group-dialog-title').textContent = group ? 'Edit migration domain' : 'New migration domain';
  const names = [...new Set([...data.nodes.map(n => n.node), ...(group?.nodes || [])])].sort();
  $('#group-host-options').innerHTML = names.map(name => {
    const owner = data.groups.find(g => g.id !== id && g.nodes.includes(name));
    const node = data.nodes.find(n => n.node === name);
    return `<label class="group-host-option"><input type="checkbox" name="nodes" value="${escapeHtml(name)}" ${group?.nodes.includes(name) ? 'checked' : ''} ${owner ? 'disabled' : ''}><span><strong>${escapeHtml(name)}</strong><small>${owner ? `Assigned to ${escapeHtml(owner.name)}` : escapeHtml(node?.status || 'Missing from inventory')}</small></span></label>`;
  }).join('');
  $('#group-form-error').hidden = true; $('#group-dialog').showModal();
  form.elements.name.focus();
}

document.getElementById('group-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (migrationGroups.saving || !migrationGroups.editing) return;
  const editing = {...migrationGroups.editing}, form = event.target;
  const payload = {name: form.elements.name.value, notes: form.elements.notes.value, enabled: form.elements.enabled.checked,
    nodes: [...form.querySelectorAll('input[name="nodes"]:checked')].map(input => input.value)};
  const errorBox = $('#group-form-error'); errorBox.hidden = true;
  if (editing.id !== null) payload.revision = editing.revision;
  migrationGroups.saving = true; $('#save-group').disabled = true;
  try {
    await api(`/connections/${editing.connection}/migration-groups${editing.id !== null ? `/${editing.id}` : ''}`, {method: editing.id !== null ? 'PUT' : 'POST', body: JSON.stringify(payload)});
    if (editing.generation !== state.generation) return;
    $('#group-dialog').close(); clearDrsRun(); await refreshGroups(); toast('Migration domain saved.');
  } catch (error) {
    if (editing.generation === state.generation) {errorBox.textContent = error.message; errorBox.hidden = false;}
  } finally {migrationGroups.saving = false; $('#save-group').disabled = false;}
});

document.getElementById('group-dialog').addEventListener('cancel', event => {if (migrationGroups.saving) event.preventDefault();});
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.id === 'new-group' || button.hasAttribute('data-new-group')) openGroup();
  if (button.dataset.editGroup) openGroup(Number(button.dataset.editGroup));
  if (button.hasAttribute('data-close-group') && !migrationGroups.saving) $('#group-dialog').close();
  if (button.id === 'refresh-groups') {await sync(); await refreshGroups();}
  if (button.dataset.deleteGroup) {
    const group = migrationGroups.data?.groups.find(g => g.id === Number(button.dataset.deleteGroup));
    if (!group || !confirm(`Remove “${group.name}”? Its hosts will be excluded from automatic live migration until assigned to another group.`)) return;
    const connection = state.selected, generation = state.generation;
    try {
      await api(`/connections/${connection}/migration-groups/${group.id}?revision=${group.revision}`, {method: 'DELETE'});
      if (generation === state.generation) {clearDrsRun(); await refreshGroups(); toast('Migration domain removed.');}
    } catch (error) {if (generation === state.generation) toast(error.message);}
  }
});
document.getElementById('group-cluster').addEventListener('change', async event => {
  state.selected = Number(event.target.value); $('#cluster-select').value = state.selected;
  try {await loadCluster(); await sync();} catch (error) {toast(error.message);}
});

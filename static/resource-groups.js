const resourceGroups = {data: null, error: '', serial: 0, mutation: 0, editing: null, saving: false};

function resetResourceGroups() {
  resourceGroups.serial++; resourceGroups.mutation++;
  resourceGroups.data = null; resourceGroups.error = ''; resourceGroups.editing = null; resourceGroups.saving = false;
  $('#resource-group-dialog').close();
  $('#resource-group-search').value = ''; $('#resource-group-filter').value = 'all';
  $('#save-resource-group').disabled = false;
  renderResourceGroups();
}

async function refreshResourceGroups() {
  const connection = state.selected, generation = state.generation, serial = ++resourceGroups.serial;
  if (!connection) {resourceGroups.data = null; renderResourceGroups(); return;}
  const current = () => connection === state.selected && generation === state.generation && serial === resourceGroups.serial;
  try {
    const data = await api(`/connections/${connection}/resource-groups`);
    if (!current()) return;
    resourceGroups.data = data; resourceGroups.error = '';
  } catch (error) {
    if (!current()) return;
    resourceGroups.data = null; resourceGroups.error = error.message;
  }
  renderResourceGroups();
}

function resourceMember(resource) {
  return resourceGroups.data?.inventory.find(item => item.resource === resource) || {
    resource, name: resource, type: resource.split('/')[0], kind: resource.startsWith('node/') ? 'host' : 'guest',
    missing: true, status: 'Missing from inventory', node: null};
}

function resourceMemberLabel(member) {
  return `${member.name} · ${{qemu: 'VM', lxc: 'Container', node: 'Host'}[member.type]} ${member.resource.split('/')[1]}`;
}

function renderResourceGroups() {
  const data = resourceGroups.data;
  $('#new-resource-group').disabled = !state.selected || !data || resourceGroups.saving;
  $('#resource-group-error').textContent = resourceGroups.error; $('#resource-group-error').hidden = !resourceGroups.error;
  if (!data) {
    $('#resource-group-summary').textContent = !state.selected ? 'Connect a cluster to create resource groups.' : resourceGroups.error ? 'Resource groups are unavailable. Refresh to retry.' : 'Loading resource groups…';
    $('#resource-group-list').innerHTML = ''; return;
  }
  const query = $('#resource-group-search').value.trim().toLocaleLowerCase(), kind = $('#resource-group-filter').value;
  const groups = data.groups.filter(group => (kind === 'all' || kind === group.kind) &&
    [group.name, group.notes, ...group.members.flatMap(resource => [resource, resourceMember(resource).name])].join(' ').toLocaleLowerCase().includes(query));
  $('#resource-group-summary').textContent = `${data.groups.length} groups · ${groups.length} shown. Membership is saved independently of live migration compatibility groups.${data.inventory_fresh ? '' : ' Inventory is stale or unavailable; refresh to find new members.'}`;
  $('#resource-group-list').innerHTML = groups.length ? groups.map(group => {
    const missing = group.members.filter(resource => resourceMember(resource).missing).length;
    return `<article class="migration-group resource-group"><div class="group-card-heading"><span class="info-icon">${icon(group.kind === 'host' ? 'server-stack' : 'squares-2x2')}</span><div><h3>${escapeHtml(group.name)}</h3><p>${group.kind === 'host' ? 'Host group' : 'Guest group'} · ${group.members.length} members</p></div></div>
      ${group.notes ? `<p class="group-notes">${escapeHtml(group.notes)}</p>` : ''}
      ${missing ? `<p class="warning">${missing} members missing from inventory. Membership is retained.</p>` : ''}
      ${group.members.length ? `<details class="resource-group-members"><summary>View members</summary><ul>${group.members.map(resource => {const member = resourceMember(resource); return `<li><strong>${escapeHtml(resourceMemberLabel(member))}</strong><small>${escapeHtml(member.node || '')}${member.node ? ' · ' : ''}${escapeHtml(member.status)}</small></li>`;}).join('')}</ul></details>` : '<p class="group-note">No members yet.</p>'}
      <div class="group-card-actions"><button class="secondary" type="button" data-edit-resource-group="${group.id}" ${resourceGroups.saving ? 'disabled' : ''}>Edit</button><button class="secondary" type="button" data-delete-resource-group="${group.id}" ${resourceGroups.saving ? 'disabled' : ''}>Remove</button></div></article>`;
  }).join('') : `<p class="group-note">${data.groups.length ? 'No matching groups.' : 'No resource groups yet. Create a guest group or a host group to organize placement rules.'}</p>`;
}

function visibleResourceMembers() {
  const editing = resourceGroups.editing;
  if (!editing || !resourceGroups.data) return [];
  const kind = $('#resource-group-form').elements.kind.value, query = $('#resource-member-search').value.trim().toLocaleLowerCase();
  const resources = new Set([...resourceGroups.data.inventory.filter(item => item.kind === kind).map(item => item.resource), ...editing.originalMembers]);
  return [...resources].map(resourceMember).filter(item => item.kind === kind &&
    `${item.name} ${item.resource} ${item.node || ''}`.toLocaleLowerCase().includes(query))
    .sort((a, b) => a.name.localeCompare(b.name, 'en', {numeric: true}) || a.resource.localeCompare(b.resource));
}

function renderResourceMembers() {
  const editing = resourceGroups.editing;
  if (!editing) return;
  const members = visibleResourceMembers();
  $('#resource-member-options').innerHTML = members.map(member => `<label class="group-host-option"><input type="checkbox" name="members" value="${escapeHtml(member.resource)}" ${editing.members.has(member.resource) ? 'checked' : ''}><span><strong>${escapeHtml(resourceMemberLabel(member))}</strong><small>${escapeHtml(member.node || '')}${member.node ? ' · ' : ''}${escapeHtml(member.status)}${member.missing ? ' (retained)' : ''}</small></span></label>`).join('');
  $('#resource-member-empty').hidden = members.length > 0;
  $('#resource-member-count').textContent = `${editing.members.size} selected · ${members.length} shown`;
}

function openResourceGroup(id = null) {
  if (!resourceGroups.data || resourceGroups.saving) return;
  const group = resourceGroups.data.groups.find(item => item.id === id);
  if (id !== null && !group) return;
  const form = $('#resource-group-form'); form.reset();
  resourceGroups.editing = {id, revision: group?.revision, connection: state.selected, generation: state.generation,
    originalMembers: group?.members || [], members: new Set(group?.members || [])};
  form.elements.name.value = group?.name || ''; form.elements.notes.value = group?.notes || '';
  form.elements.kind.value = group?.kind || 'guest'; form.elements.kind.disabled = !!group;
  $('#resource-group-dialog-title').textContent = group ? 'Edit resource group' : 'New resource group';
  $('#resource-group-form-error').hidden = true; $('#resource-member-search').value = '';
  renderResourceMembers(); $('#resource-group-dialog').showModal(); form.elements.name.focus();
}

document.getElementById('resource-group-search').addEventListener('input', renderResourceGroups);
document.getElementById('resource-group-filter').addEventListener('change', renderResourceGroups);
document.getElementById('resource-member-search').addEventListener('input', renderResourceMembers);
document.getElementById('resource-group-form').elements.kind.addEventListener('change', () => {
  if (!resourceGroups.editing || resourceGroups.saving) return;
  resourceGroups.editing.members.clear(); $('#resource-member-search').value = ''; renderResourceMembers();
});
document.getElementById('resource-member-options').addEventListener('change', event => {
  if (!event.target.matches('input[name="members"]') || !resourceGroups.editing) return;
  const members = resourceGroups.editing.members;
  event.target.checked ? members.add(event.target.value) : members.delete(event.target.value);
  $('#resource-member-count').textContent = `${members.size} selected · ${visibleResourceMembers().length} shown`;
});
for (const [id, select] of [['resource-select-visible', true], ['resource-clear-visible', false]]) {
  document.getElementById(id).addEventListener('click', () => {
    if (!resourceGroups.editing || resourceGroups.saving) return;
    visibleResourceMembers().forEach(member => select ? resourceGroups.editing.members.add(member.resource) : resourceGroups.editing.members.delete(member.resource));
    renderResourceMembers();
  });
}

document.getElementById('resource-group-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (resourceGroups.saving || !resourceGroups.editing) return;
  const editing = resourceGroups.editing, form = event.target, token = ++resourceGroups.mutation;
  const current = () => token === resourceGroups.mutation && editing.connection === state.selected && editing.generation === state.generation;
  const payload = {name: form.elements.name.value, kind: form.elements.kind.value, notes: form.elements.notes.value, members: [...editing.members]};
  if (editing.id !== null) payload.revision = editing.revision;
  resourceGroups.saving = true; $('#save-resource-group').disabled = true; $('#resource-group-form-error').hidden = true;
  try {
    await api(`/connections/${editing.connection}/resource-groups${editing.id !== null ? `/${editing.id}` : ''}`, {method: editing.id !== null ? 'PUT' : 'POST', body: JSON.stringify(payload)});
    if (!current()) return;
    $('#resource-group-dialog').close(); resourceGroups.editing = null;
    await refreshResourceGroups(); await refreshPlacementRules();
    if (current()) toast('Resource group saved.');
  } catch (error) {
    if (current()) {$('#resource-group-form-error').textContent = error.message; $('#resource-group-form-error').hidden = false;}
  } finally {
    if (current()) {resourceGroups.saving = false; $('#save-resource-group').disabled = false; renderResourceGroups();}
  }
});

document.getElementById('resource-group-dialog').addEventListener('cancel', event => {if (resourceGroups.saving) event.preventDefault();});
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.id === 'new-resource-group') openResourceGroup();
  if (button.dataset.editResourceGroup) openResourceGroup(Number(button.dataset.editResourceGroup));
  if (button.hasAttribute('data-close-resource-group') && !resourceGroups.saving) {$('#resource-group-dialog').close(); resourceGroups.editing = null;}
  if (!button.dataset.deleteResourceGroup || resourceGroups.saving) return;
  const group = resourceGroups.data?.groups.find(item => item.id === Number(button.dataset.deleteResourceGroup));
  if (!group || !confirm(`Remove resource group "${group.name}"? Its VMs, containers and hosts will remain unchanged.`)) return;
  const connection = state.selected, generation = state.generation, token = ++resourceGroups.mutation;
  const current = () => connection === state.selected && generation === state.generation && token === resourceGroups.mutation;
  resourceGroups.saving = true; renderResourceGroups();
  try {
    await api(`/connections/${connection}/resource-groups/${group.id}?revision=${group.revision}`, {method: 'DELETE'});
    if (current()) {await refreshResourceGroups(); if (current()) toast('Resource group removed.');}
  } catch (error) {if (current()) toast(error.message);}
  finally {if (current()) {resourceGroups.saving = false; renderResourceGroups();}}
});

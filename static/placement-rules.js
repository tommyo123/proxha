const placementRules = {data: null, serial: 0, mutation: 0, editing: null, saving: false, error: ''};
const placementRuleLabels = {together: 'Keep guests together', apart: 'Keep guests on separate hosts', require_hosts: 'Use only these host groups', avoid_hosts: 'Avoid these host groups'};
const placementEnforcementLabels = {must: 'Must / required with availability exceptions', should: 'Should / weighted preference', report: 'Report only / no automatic moves'};
const placementViolations = status => {
  const members = status?.violating_members || [];
  return members.length ? `<ul class="violation-list">${members.slice(0, 8).map(m => `<li><button class="text-button" data-open-resource="${escapeHtml(m.resource)}">${escapeHtml(m.name)}</button> · ${escapeHtml(m.reason)}</li>`).join('')}${members.length > 8 ? `<li>${members.length - 8} more</li>` : ''}</ul>` : '';
};

function resetPlacementRules() {
  placementRules.serial++; placementRules.mutation++;
  Object.assign(placementRules, {data: null, editing: null, saving: false, error: ''});
  $('#placement-rule-dialog').close(); $('#save-placement-rule').disabled = false;
  renderPlacementRules();
}

async function refreshPlacementRules() {
  const connection = state.selected, generation = state.generation, serial = ++placementRules.serial;
  if (!connection) return;
  const current = () => connection === state.selected && generation === state.generation && serial === placementRules.serial;
  try {
    const data = await api(`/connections/${connection}/placement-rules`);
    if (!current()) return;
    placementRules.data = data; placementRules.error = '';
  } catch (error) {
    if (!current()) return;
    placementRules.data = null; placementRules.error = error.message;
  }
  renderPlacementRules();
}

function renderPlacementRules() {
  const data = placementRules.data;
  $('#new-placement-rule').disabled = !data || placementRules.saving || !data.groups.some(g => g.kind === 'guest');
  $('#repair-placement-rules').disabled = !data || placementRules.saving || !data.statuses.some(s => s.violations && s.enforcement === 'must');
  $('#placement-rule-error').textContent = placementRules.error; $('#placement-rule-error').hidden = !placementRules.error;
  $('#placement-rule-status').textContent = !data ? 'Loading placement rules…' : `${data.rules.length} rules · Automatic repair ${data.repair_enabled ? 'enabled' : 'paused'} · Group parallel limit: ${data.parallel_limit}. Last inventory: ${data.synced_at ? time(data.synced_at) : 'Unavailable'}`;
  const names = ids => ids.map(id => data.groups.find(g => g.id === id)?.name || `Missing group ${id}`).join(', ');
  $('#placement-rule-list').innerHTML = !data ? '' : data.rules.length ? data.rules.map(rule => {
    const status = data.statuses.find(s => s.id === rule.id);
    return `<article class="migration-group"><div class="group-card-heading"><span class="info-icon">${icon('shield-check')}</span><div><h3>${escapeHtml(rule.name)}</h3><p>${escapeHtml(placementRuleLabels[rule.kind])}</p></div>${stateBadge(rule.enabled ? status?.state || 'unknown' : 'disabled')}</div>
      <p class="group-note">Guest groups: ${escapeHtml(names(rule.guest_group_ids))}</p>
      ${rule.host_group_ids.length ? `<p class="group-note">Host groups: ${escapeHtml(names(rule.host_group_ids))}</p>` : ''}
      <p class="group-note">Enforcement: ${escapeHtml(placementEnforcementLabels[rule.enforcement || 'should'])}${rule.enforcement === 'should' ? ' / Weight: ' + Number(rule.weight ?? 5) : ''}</p>
      ${status?.violations ? `<p class="warning">${status.violations} placement conflicts.${status.enforcement === 'report' ? ' Report-only rules do not move guests.' : status.enforcement === 'should' ? ' Weighted preference cost: ' + Number(status.preference_cost || 0).toFixed(2) + '. Balancing may accept this placement.' : ' Repairs must pass compatibility, capacity, storage and backup checks.'}</p>${placementViolations(status)}` : ''}
      ${(status?.warnings || []).map(warning => `<p class="warning">${escapeHtml(warning)}</p>`).join('')}
      ${status?.inactive_members.length ? `<p class="group-note">${status.inactive_members.length} members are not currently running or visible. Stopped guests are not automatically migrated.</p>` : ''}
      ${status?.unavailable_members.length ? `<p class="warning">${status.unavailable_members.length} members on unavailable or maintenance hosts. Availability exceptions apply.</p>` : ''}
      <div class="group-card-actions"><button class="secondary" data-edit-placement-rule="${rule.id}" ${placementRules.saving ? 'disabled' : ''}>Edit</button><button class="secondary" data-delete-placement-rule="${rule.id}" ${placementRules.saving ? 'disabled' : ''}>Remove</button></div></article>`;
  }).join('') : '<p class="group-note">No placement rules. Create resource groups, then bind them to a rule.</p>';
}

function placementRuleKind() {
  const form = $('#placement-rule-form'), hosts = form.elements.kind.value.endsWith('_hosts');
  $('#placement-host-groups-label').hidden = !hosts; $('#placement-host-groups-label').disabled = !hosts;
  form.elements.weight.disabled = form.elements.enforcement.value !== 'should';
}

const placementGroupIds = kind => $$(`#placement-rule-form [name=${kind}_group_ids]:checked`).map(input => Number(input.value));

function openPlacementRule(id = null) {
  if (!placementRules.data || placementRules.saving) return;
  const rule = placementRules.data.rules.find(r => r.id === id);
  if (id !== null && !rule) return;
  const form = $('#placement-rule-form'); form.reset();
  placementRules.editing = {id, revision: rule?.revision, connection: state.selected, generation: state.generation};
  form.elements.name.value = rule?.name || ''; form.elements.kind.value = rule?.kind || 'together';
  form.elements.enabled.checked = rule?.enabled ?? true; form.elements.enforcement.value = rule?.enforcement || 'should'; form.elements.weight.value = rule?.weight ?? 5;
  for (const kind of ['guest', 'host']) {
    const groups = placementRules.data.groups.filter(g => g.kind === kind);
    form.querySelector(`[data-choices=${kind}]`).innerHTML = groups.length ? groups.map(g =>
      `<label class="checkbox"><input type="checkbox" name="${kind}_group_ids" value="${g.id}" ${rule?.[kind + '_group_ids'].includes(g.id) ? 'checked' : ''}><span>${escapeHtml(g.name)} (${g.members.length} members)</span></label>`).join('') :
      `<p class="group-note">No ${kind} groups yet. Create one under Resource groups.</p>`;
  }
  $('#placement-rule-title').textContent = rule ? 'Edit placement rule' : 'New placement rule';
  $('#placement-rule-form-error').hidden = true; $('#placement-rule-preview').hidden = true; placementRuleKind();
  $('#placement-rule-dialog').showModal(); form.elements.name.focus();
}

function placementRulePayload(form) {
  const hosts = form.elements.kind.value.endsWith('_hosts');
  const payload = {name: form.elements.name.value, kind: form.elements.kind.value, enforcement: form.elements.enforcement.value, enabled: form.elements.enabled.checked,
    guest_group_ids: placementGroupIds('guest'), host_group_ids: hosts ? placementGroupIds('host') : []};
  // Only Should rules use a weight; the field is disabled otherwise.
  if (payload.enforcement === 'should') payload.weight = Number(form.elements.weight.value);
  if (!payload.guest_group_ids.length || hosts && !payload.host_group_ids.length) {
    $('#placement-rule-form-error').textContent = hosts && payload.guest_group_ids.length ? 'Select at least one host group.' : 'Select at least one guest group.';
    $('#placement-rule-form-error').hidden = false; return null;
  }
  return payload;
}

function placementPreviewHtml(data) {
  const status = data.status, moves = data.moves || [], blocked = data.blocked || [];
  const list = items => `<ul class="violation-list">${items.join('')}</ul>`;
  let outcome;
  if (!data.rule.enabled) outcome = 'Disabled rules are not checked and start no migrations.';
  else if (status?.enforcement === 'report') outcome = 'Report-only rule: violations are shown, and no guests move.';
  else if (status?.enforcement === 'should') outcome = 'Weighted preference: affects balancing scores without forcing a repair or blocking a manual move.';
  else if (moves.length) outcome = `${data.repair_enabled ? 'Saving starts' : 'Automatic repair is paused. Repair now would start'} these repair migrations:${list(moves.map(m => `<li>${escapeHtml(m.name)} · ${escapeHtml(m.source)} → ${escapeHtml(m.target)}${m.new ? '' : ' · already planned before this change'}</li>`))}`;
  else outcome = 'No repair migration is needed or currently possible.';
  return `<strong>Preview: ${status?.violations || 0} current conflicts</strong>${placementViolations(status)}
    ${(status?.warnings || []).map(w => `<p class="warning">${escapeHtml(w)}</p>`).join('')}<span>${outcome}</span>
    ${blocked.length ? `<span>Blocked repairs:</span>${list(blocked.map(b => `<li>${escapeHtml(b.name)} · ${escapeHtml(b.reasons.join(' '))}</li>`))}` : ''}`;
}

async function previewPlacementRule() {
  if (placementRules.saving || !placementRules.editing) return;
  const editing = placementRules.editing, form = $('#placement-rule-form'), token = ++placementRules.mutation;
  const current = () => token === placementRules.mutation && editing === placementRules.editing && editing.connection === state.selected && editing.generation === state.generation;
  $('#placement-rule-form-error').hidden = true;
  const payload = placementRulePayload(form); if (!payload) return;
  if (editing.id !== null) Object.assign(payload, {rule_id: editing.id, revision: editing.revision});
  const preview = $('#placement-rule-preview'); preview.hidden = false; preview.textContent = 'Calculating preview…';
  try {
    const data = await api(`/connections/${editing.connection}/placement-rules/preview`, {method: 'POST', body: JSON.stringify(payload)});
    if (current()) preview.innerHTML = placementPreviewHtml(data);
  } catch (error) {
    if (current()) {preview.hidden = true; $('#placement-rule-form-error').textContent = error.message; $('#placement-rule-form-error').hidden = false;}
  }
}

async function repairPlacementRules() {
  if (placementRules.saving || !state.selected) return;
  const connection = state.selected, generation = state.generation, token = ++placementRules.mutation;
  const current = () => token === placementRules.mutation && connection === state.selected && generation === state.generation;
  placementRules.saving = true; renderPlacementRules();
  try {
    const data = await api(`/connections/${connection}/placement-rules/repair`, {method: 'POST', body: JSON.stringify({request_id: newRequestId()})});
    if (!current()) return;
    toast(data.run ? `Rule repair started: ${data.moves.length} placement decision${data.moves.length === 1 ? '' : 's'}.` :
      `No repair migration can start. ${data.blocked.map(b => b.reasons.join(' ')).join(' ') || data.message || ''}`.trim());
    if (data.run) await refreshMigrationQueue();
  } catch (error) {if (current()) toast(error.message);}
  finally {if (current()) {placementRules.saving = false; await refreshPlacementRules();}}
}

document.getElementById('placement-rule-form').elements.enforcement.addEventListener('change', placementRuleKind);
document.getElementById('placement-rule-form').elements.kind.addEventListener('change', placementRuleKind);
document.getElementById('placement-rule-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (placementRules.saving || !placementRules.editing) return;
  const editing = placementRules.editing, token = ++placementRules.mutation, form = event.target;
  const current = () => token === placementRules.mutation && editing.connection === state.selected && editing.generation === state.generation;
  const payload = placementRulePayload(form); if (!payload) return;
  if (editing.id !== null) payload.revision = editing.revision;
  placementRules.saving = true; $('#save-placement-rule').disabled = true; $('#placement-rule-form-error').hidden = true;
  try {
    await api(`/connections/${editing.connection}/placement-rules${editing.id !== null ? `/${editing.id}` : ''}`, {method: editing.id !== null ? 'PUT' : 'POST', body: JSON.stringify(payload)});
    if (!current()) return;
    $('#placement-rule-dialog').close(); placementRules.editing = null; await refreshPlacementRules();
    if (current()) toast('Placement rule saved. Enabled rules are checked automatically.');
  } catch (error) {
    if (current()) {$('#placement-rule-form-error').textContent = error.message; $('#placement-rule-form-error').hidden = false;}
  } finally {if (current()) {placementRules.saving = false; $('#save-placement-rule').disabled = false; renderPlacementRules();}}
});
document.getElementById('placement-rule-dialog').addEventListener('cancel', event => {if (placementRules.saving) event.preventDefault();});
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.id === 'new-placement-rule') openPlacementRule();
  if (button.id === 'preview-placement-rule') previewPlacementRule();
  if (button.id === 'repair-placement-rules') repairPlacementRules();
  if (button.dataset.editPlacementRule) openPlacementRule(Number(button.dataset.editPlacementRule));
  if (button.hasAttribute('data-close-placement-rule') && !placementRules.saving) {$('#placement-rule-dialog').close(); placementRules.editing = null;}
  if (!button.dataset.deletePlacementRule || placementRules.saving) return;
  const rule = placementRules.data?.rules.find(r => r.id === Number(button.dataset.deletePlacementRule));
  if (!rule || !confirm(`Remove placement rule "${rule.name}"?`)) return;
  const connection = state.selected, generation = state.generation, token = ++placementRules.mutation;
  const current = () => token === placementRules.mutation && connection === state.selected && generation === state.generation;
  placementRules.saving = true; renderPlacementRules();
  try {
    await api(`/connections/${connection}/placement-rules/${rule.id}?revision=${rule.revision}`, {method: 'DELETE'});
    if (current()) await refreshPlacementRules();
  } catch (error) {if (current()) toast(error.message);}
  finally {if (current()) {placementRules.saving = false; renderPlacementRules();}}
});

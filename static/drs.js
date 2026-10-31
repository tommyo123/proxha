const drs = {tab: 'overview', busy: false, settingsRevision: null, historySerial: 0, candidateFilter: 'all', requestSerial: 0, preview: null, feedback: '', failed: false, run: null, queueSerial: 0, queuePolling: false, stopping: false, startRequest: null, continuous: null, continuousBusy: false, continuousDesired: null, continuousSerial: 0, aggressivenessBusy: false, aggressivenessSerial: 0};
const drsThresholdDefaults = {cpu_threshold_percent: 80, memory_threshold_percent: 80, overload_hold_seconds: 300, cpu_psi_threshold_percent: 5, continuous_migrations_per_hour: 10, migration_penalty_decay_minutes: 120, scheduled_minimum_score: .5, placement_concurrency: 2, migration_bandwidth_limit_mib: 0, migration_target_concurrency: 0};

function renderContinuous() {
  const data = drs.continuous || migrationGroups.evaluation?.continuous;
  $('#continuous-status').textContent = `Continuous balancing · ${data?.enabled ? 'enabled · ' + stateLabel(data.status || 'observing') : 'disabled'}${data?.enabled ? ` · Aggressiveness ${data.aggressiveness}/5` : ''}`;
  $('#continuous-message').textContent = data?.message || 'Enable continuous balancing in DRS Settings to migrate automatically.';
  $('#continuous-timing').textContent = data?.enabled ? `Check interval: ${data.interval_seconds}s · Last check: ${dateSeconds(data.last_checked_at)}${data.next_check_at ? ' · Next check: ' + dateSeconds(data.next_check_at) : ''}${data.next_move_at > Date.now() / 1000 ? ' · Settling until: ' + dateSeconds(data.next_move_at) : ''}${data.ready_at ? ' · Observation until: ' + dateSeconds(data.ready_at) : ''}` : '';
  const config = migrationGroups.evaluation?.settings;
  const enabled = drs.continuousBusy ? drs.continuousDesired : config?.revision > (data?.revision ?? -1) ? config.continuous_enabled : data?.enabled ?? config?.continuous_enabled;
  $$('[data-continuous-enabled]').forEach(input => {
    input.checked = enabled === true;
    input.disabled = !state.selected || !data && !config || drs.continuousBusy || drs.aggressivenessBusy;
  });
  if (data?.hourly_budget) $('#continuous-timing').textContent += ` · Continuous moves this hour: ${data.hourly_budget.used}/${data.hourly_budget.limit}`;
  syncAutomationMode();
}

function renderBalloonRecovery(data) {
  const recovery = data?.balloon_recovery;
  $('#recover-balloon').disabled = !state.selected || !data || drs.busy || !!drs.run?.active;
  if (!recovery) {$('#balloon-recovery-status').textContent = 'Waiting for balloon telemetry.'; return;}
  const labels = {native_wait: 'Capacity available; no recent recovery progress. Check pvestatd and the guest balloon driver.', unknown: 'Threshold unavailable', needs_capacity: 'Needs migration capacity', native_recovery: 'Capacity available for native recovery', restored: 'No verified recovery backlog', unverified: 'Check guest balloon driver, minimum and shares'};
  $('#balloon-recovery-status').innerHTML = `<p><strong>${bytes(recovery.recoverable_bytes)}</strong> verified RAM to return · <strong>${bytes(recovery.eligible_bytes)}</strong> can return with current capacity · <strong>${bytes(recovery.blocked_bytes)}</strong> needs capacity${recovery.enabled === false ? ' · Automatic recovery priority disabled' : ''}</p><div class="table-scroll"><table class="policy-table"><thead><tr><th>Host</th><th>Ballooning target</th><th>RAM to return</th><th>Capacity available</th><th>RAM returned (5 min)</th><th>Status</th></tr></thead><tbody>${(recovery.hosts || []).map(host => `<tr><td>${escapeHtml(host.node)}</td><td>${host.target_percent === null || host.target_percent === undefined ? 'Unknown' : `${pct(host.target_percent / 100)} (${escapeHtml(host.target_source || 'verified')})`}</td><td>${bytes(host.recoverable_bytes)}<small>${host.guests || 0} guests${host.unmanaged_bytes ? ` · ${bytes(host.unmanaged_bytes)} additional RAM needs balloon verification` : ''}</small></td><td>${bytes(host.eligible_bytes)}</td><td>${host.returned_bytes_5m == null ? 'Collecting history' : (host.returned_bytes_5m < 0 ? '−' : '') + bytes(Math.abs(host.returned_bytes_5m))}</td><td>${escapeHtml(labels[host.state] || host.state)}${host.error ? `<small>${escapeHtml(host.error)}</small>` : ''}</td></tr>`).join('')}</tbody></table></div><p class="group-note">${escapeHtml(recovery.progress_note || '')} Capacity figures are estimates, not completed memory returns. Refreshing measurements confirm the remaining backlog. Native ballooning follows each guest's minimum, maximum and shares.</p>`;
}
function continuousProfile() {
  const level = Number($('#drs-settings').elements.continuous_aggressiveness.value), index = level - 1;
  $('#continuous-level').textContent = `${level} / 5`;
  const saved = migrationGroups.evaluation?.settings?.continuous_aggressiveness ?? 3;
  $('#continuous-save-status').textContent = drs.aggressivenessBusy ? 'Saving aggressiveness...' : level === saved ? `Saved aggressiveness: ${saved} / 5` : `Selected: ${level} / 5. Saved: ${saved} / 5. Release the slider to save.`;
  $('#continuous-profile').textContent = `${['Conservative', 'Cautious', 'Balanced', 'Active', 'Aggressive'][index]} · Minimum score ${[12, 8, 5, 2.5, 1][index]} · Observe the same guest and target for ${[600, 300, 180, 120, 60][index]}s · Settle ${[600, 300, 180, 120, 60][index]}s after a move · Guest cooldown at least ${[4, 3, 2, 1, 1][index]}h, longer after repeated moves.`;
}
async function saveAggressiveness() {
  if (!state.selected || drs.aggressivenessBusy || drs.continuousBusy) return;
  const connection = state.selected, serial = ++drs.aggressivenessSerial;
  const current = () => connection === state.selected && serial === drs.aggressivenessSerial;
  drs.queueSerial++;
  drs.aggressivenessBusy = true; continuousProfile(); renderContinuous();
  try {
    while (current()) {
      const value = Number($('#drs-settings').elements.continuous_aggressiveness.value);
      const config = migrationGroups.evaluation?.settings;
      if (!config || value === config.continuous_aggressiveness) break;
      const data = await api(`/connections/${connection}/balancing/continuous/aggressiveness`, {method: 'PUT', body: JSON.stringify({aggressiveness: value, revision: config.revision})});
      if (!current()) return;
      drs.queueSerial++;
      migrationGroups.evaluation.settings = data.settings;
      drs.settingsRevision = `${connection}/${data.settings.revision}`;
      drs.continuous = data.continuous; renderContinuous();
    }
    if (current()) {drs.aggressivenessBusy = false; continuousProfile();}
  } catch (error) {
    if (current()) {drs.aggressivenessBusy = false; $('#continuous-save-status').textContent = `Not saved: ${error.message}`;}
  } finally {if (current()) {drs.aggressivenessBusy = false; renderContinuous();}}
}

async function changeContinuous(enabled) {
  if (!state.selected || drs.continuousBusy || drs.aggressivenessBusy) {renderContinuous(); return;}
  const connection = state.selected, serial = ++drs.continuousSerial;
  const revision = Math.max(drs.continuous?.revision ?? 0, migrationGroups.evaluation?.settings?.revision ?? 0);
  drs.queueSerial++; migrationGroups.serial++;
  drs.continuousBusy = true; drs.continuousDesired = enabled; $('#continuous-error').hidden = true; renderContinuous();
  try {
    const data = await api(`/connections/${connection}/balancing/continuous`, {method: 'PUT', body: JSON.stringify({enabled, revision})});
    if (connection !== state.selected || serial !== drs.continuousSerial) return;
    drs.queueSerial++; drs.continuous = data.continuous;
    migrationGroups.evaluation = {...migrationGroups.evaluation, settings: data.settings, continuous: data.continuous};
    drs.settingsRevision = null; await refreshGroups();
  } catch (error) {
    if (connection === state.selected && serial === drs.continuousSerial) {$('#continuous-error').textContent = error.message; $('#continuous-error').hidden = false;}
  } finally {if (connection === state.selected && serial === drs.continuousSerial) {drs.continuousBusy = false; drs.continuousDesired = null; renderContinuous();}}
}

function renderMigrationQueue() {
  const run = drs.run;
  $('#migration-queue').hidden = !run;
  $('#queue-shortcut').hidden = !run;
  $$('[data-stop-queue]').forEach(button => {button.hidden = !run?.active || run.trigger === 'continuous'; button.disabled = drs.stopping || !!run?.stop_requested; button.textContent = run?.stop_requested ? 'Stopping…' : 'Stop queue';});
  if (!run) return;
  $('#queue-status').textContent = `Migration queue · ${stateLabel(run.status)} · ${run.completed || 0} completed`;
  if (run.failed) $('#queue-status').textContent += ` · ${run.failed} failed / skipped`;
  if (run.trigger === 'continuous') $('#queue-status').textContent = 'Continuous ' + $('#queue-status').textContent;
  if (run.trigger === 'placement_rules') $('#queue-status').textContent = 'Placement rule repair | ' + $('#queue-status').textContent;
  if (run.trigger === 'scheduled') $('#queue-status').textContent = 'Scheduled ' + $('#queue-status').textContent;
  if (!run.maintenance_node) $('#queue-status').textContent = (run.guest_kind === 'lxc' ? 'VM + LXC ' : 'VM ') + $('#queue-status').textContent;
  if (run.trigger === 'aggressive_manual') $('#queue-status').textContent = 'Aggressive manual / ' + $('#queue-status').textContent;
  if (run.trigger === 'balloon_recovery') $('#queue-status').textContent = 'Balloon recovery · ' + $('#queue-status').textContent;
  $('#queue-message').textContent = run.message;
  if (run.maintenance_node || run.placement_batch) $('#queue-status').textContent += ` · ${run.in_flight || 0} in flight / limit ${run.concurrency || 3}`;
  $('#queue-shortcut').textContent = `${$('#queue-status').textContent} · View details`;
  $('#queue-next').textContent = run.next_at && run.active ? `Next evaluation: ${dateSeconds(run.next_at)}` : '';
  $('#queue-steps').innerHTML = (run.steps || []).map(step => `<li><strong>${escapeHtml(step.resource)}</strong> ${escapeHtml(step.source)} → ${escapeHtml(step.target)} ${stateBadge(step.status)}<small>${escapeHtml(step.message || '')}</small></li>`).join('');
  $('#queue-steps').innerHTML += (run.exclusions || []).map(item => `<li><strong>${escapeHtml(item.resource)}</strong> ${stateBadge('skipped')}<small>${escapeHtml(item.reason)}</small></li>`).join('');
}

async function refreshMigrationQueue() {
  if (!state.selected || drs.queuePolling) return;
  const connection = state.selected, serial = ++drs.queueSerial;
  drs.queuePolling = true;
  try {
    const data = await api(`/connections/${connection}/balancing/run`);
    if (connection !== state.selected || serial !== drs.queueSerial) return;
    drs.run = data.run;
    if (data.continuous && (data.continuous.revision ?? 0) >= (drs.continuous?.revision ?? 0)) drs.continuous = data.continuous;
    if (drs.run?.id === drs.startRequest) {drs.startRequest = null; drs.feedback = ''; drs.failed = false;}
    renderDrs();
  } catch (error) {
    if (connection === state.selected && serial === drs.queueSerial && drs.run?.active) $('#queue-message').textContent = `Status refresh failed: ${error.message} The queue runs on the server; use Stop queue to request a stop.`;
  } finally {drs.queuePolling = false;}
}

async function startMigrationQueue(goal = 'balance', guestKind = 'qemu') {
  if (!state.selected || drs.busy || drs.run?.active) return;
  const connection = state.selected, serial = ++drs.requestSerial;
  drs.queueSerial++;
  const aggressive = goal === 'balance' && $('#aggressive-run-now').checked;
  if (drs.startGoal !== goal || drs.startAggressive !== aggressive || drs.startGuestKind !== guestKind) drs.startRequest = null;
  drs.startGoal = goal; drs.startAggressive = aggressive; drs.startGuestKind = guestKind;
  drs.startRequest ||= newRequestId();
  drs.busy = true; drs.failed = false; drs.preview = null;
  drs.feedback = 'Starting migration queue. Together-groups use coordinated parallel migrations; other guests move one at a time.';
  renderDrs();
  try {
    const data = await api(`/connections/${connection}/balancing/run-now`, {method: 'POST', body: JSON.stringify({request_id: drs.startRequest, goal, aggressive, guest_kind: guestKind})});
    if (connection !== state.selected || serial !== drs.requestSerial) return;
    if (!data.run?.id) throw new Error('The server did not return a migration queue. Reload after the server update.');
    drs.run = data.run; drs.startRequest = null; drs.feedback = '';
    drs.queueSerial++; drs.candidateFilter = 'all'; drsTab('candidates');
    $('#migration-queue').hidden = false; $('#migration-queue').scrollIntoView?.({block: 'nearest'});
  } catch (error) {
    if (connection === state.selected && serial === drs.requestSerial) {
      drs.failed = true; drs.feedback = `Could not confirm queue start: ${error.message} Check the queue status before retrying.`;
      if (error.status) drs.startRequest = null;
      await refreshMigrationQueue();
    }
  } finally {if (connection === state.selected && serial === drs.requestSerial) {drs.busy = false; renderDrs();}}
}

async function stopMigrationQueue() {
  if (!state.selected || !drs.run?.active || drs.stopping) return;
  const connection = state.selected, runId = drs.run.id;
  drs.stopping = true; drs.queueSerial++; renderDrs();
  try {
    const data = await api(`/connections/${connection}/balancing/runs/${encodeURIComponent(runId)}/stop`, {method: 'POST'});
    if (connection !== state.selected || runId !== drs.run?.id) return;
    drs.queueSerial++; drs.run = data.run;
    drs.feedback = ''; drs.failed = false;
  } catch (error) {
    if (connection === state.selected) {drs.failed = true; drs.feedback = `Stop request failed: ${error.message} The queue may still be running; retry Stop queue.`;}
  } finally {if (connection === state.selected) {drs.stopping = false; renderDrs();}}
}

function clearDrsRun() {
  drs.requestSerial++;
  drs.busy = false; drs.preview = null; drs.feedback = ''; drs.failed = false;
}

function resetDrsDiagnostics() {
  resetReplay();
  resetCapacityMonitor();
  infraSerial++; infraData = null; $('#infrastructure-overview').textContent = 'No measurement sources loaded.'; $('#infrastructure-history').innerHTML = '';
  $('#infrastructure-form').reset(); $('#infrastructure-form').elements.node.innerHTML = ''; $('#infrastructure-source-status').textContent = '';
  capacitySerial++; $('#check-capacity').disabled = false; $('#capacity-result').textContent = 'Not assessed yet.';
  $('#aggressive-run-now').checked = false;
}

function drsTab(tab) {
  if (!['overview', 'candidates', 'rules', 'history', 'settings'].includes(tab)) return;
  drs.tab = tab;
  $$('[data-drs-tab]').forEach(button => {const active = button.dataset.drsTab === tab; button.classList.toggle('selected', active); button.setAttribute('aria-selected', active);});
  $$('[data-drs-panel]').forEach(panel => panel.hidden = panel.dataset.drsPanel !== tab);
  if (tab === 'history') loadDrsHistory();
  if (tab === 'settings') {loadCapacityMonitor(); loadCpuPerformance(); loadNotifications(); loadInfrastructure();}
  if (tab === 'overview' && !infraData) loadInfrastructure();
  saveWorkspaceRoute();
}

const notificationState = {revision: 0};

function renderNotifications(data) {
  const form = $('#notification-form');
  notificationState.revision = data.revision;
  form.elements.enabled.checked = data.enabled; form.elements.url.value = '';
  form.elements.url.placeholder = data.url_configured ? `Saved webhook on ${data.url_host}. Leave empty to keep it.` : 'https://hooks.example.com/...';
  form.querySelectorAll('[name=events]').forEach(input => input.checked = data.events.includes(input.value));
  $('#notification-recent').innerHTML = (data.recent || []).map(item => `<li>${dateSeconds(item.created_at)} · ${escapeHtml(item.event)} · ${item.delivered_at ? 'Delivered' : escapeHtml(item.error || 'Pending')}<small> ${escapeHtml(item.message)}</small></li>`).join('');
}

async function loadNotifications() {
  const connection = state.selected; if (!connection) return;
  try {
    const data = await api(`/connections/${connection}/notifications`);
    if (connection === state.selected) renderNotifications(data);
  } catch (error) {if (connection === state.selected) $('#notification-status').textContent = error.message;}
}

document.getElementById('notification-form').addEventListener('submit', async event => {
  event.preventDefault();
  const connection = state.selected, form = event.target; if (!connection) return;
  const payload = {enabled: form.elements.enabled.checked, revision: notificationState.revision,
    events: [...form.querySelectorAll('[name=events]:checked')].map(input => input.value), ...(form.elements.url.value.trim() ? {url: form.elements.url.value.trim()} : {})};
  try {
    const data = await api(`/connections/${connection}/notifications`, {method: 'PUT', body: JSON.stringify(payload)});
    if (connection === state.selected) {renderNotifications(data); $('#notification-status').textContent = 'Saved.';}
  } catch (error) {if (connection === state.selected) $('#notification-status').textContent = error.message;}
});

document.getElementById('test-notification').addEventListener('click', async () => {
  const connection = state.selected; if (!connection) return;
  $('#notification-status').textContent = 'Sending test…';
  try {
    const result = await api(`/connections/${connection}/notifications/test`, {method: 'POST'});
    if (connection === state.selected) {$('#notification-status').textContent = result.delivered ? 'Test delivered.' : `Test failed: ${result.error}`; loadNotifications();}
  } catch (error) {if (connection === state.selected) $('#notification-status').textContent = error.message;}
});

const replayState = {busy: false, serial: 0};
const replayNumber = value => value == null ? 'Not assessed' : Number(value).toFixed(Number.isInteger(Number(value)) ? 0 : 2);
function replayHtml(r) {
  return `<p>${r.evaluations} evaluations · ${r.with_move} with a recommended move · ${r.distinct_moves} distinct moves · ${r.recommendation_changes} changes of recommendation · <strong>${r.direction_reversals} direction reversals between hosts</strong> · ${r.guest_reverse_recommendations ?? 0} same-guest reverse suggestions</p>
    <p>Planner-ready steps: ${replayNumber(r.ready_evaluations)}. Full host measurements: ${r.complete_host_data_evaluations ?? 0} / ${r.evaluations} steps.</p>
    <ul class="violation-list">${r.top_moves.map(m => `<li>${escapeHtml(m.resource)} · ${escapeHtml(m.source)} → ${escapeHtml(m.target)} · ${m.evaluations} evaluations</li>`).join('')}</ul><p class="group-note">${escapeHtml(r.note)}</p>`;
}

function comparisonHtml(data) {
  const delta = value => value == null ? '' : `<small>${value > 0 ? '+' : ''}${replayNumber(value)} vs saved</small>`;
  const labels = {1: 'Conservative', 2: 'Cautious', 3: 'Balanced', 4: 'Active', 5: 'Aggressive'};
  return `${!data.has_observations ? '<p class="warning">No recorded observations are available for this comparison. This is not evidence of a balanced cluster.</p>' : ''}
    ${data.has_observations && data.profiles.every(r => !r.measured_evaluations) ? '<p class="warning">No evaluated step has fresh host measurements. This is not evidence of a balanced cluster.</p>' : ''}
    ${data.partial ? `<p class="warning">Partial comparison: ${escapeHtml(stateLabel(data.limit_reason))}. All profiles cover the same completed interval. Shorten the time range or increase the step to cover more history.</p>` : ''}
    ${!data.readiness_assessed ? '<p class="warning">This step exceeds the continuous observation window. Planner readiness is not assessed; use one-minute steps to compare persistence gates.</p>' : ''}
    <p>Requested: ${dateSeconds(data.requested_start)} to ${dateSeconds(data.requested_end)}. Evaluated: ${dateSeconds(data.started_at)} to ${dateSeconds(data.ended_at)}. Saved level: ${data.saved_level}/5.</p>
    <div class="table-scroll"><table class="policy-table replay-comparison"><thead><tr><th>Level</th><th>Steps with suggestions</th><th>Planner-ready steps</th><th>Guests in suggestions</th><th>Same-guest reverse suggestions</th><th>Median score</th></tr></thead><tbody>${data.profiles.map(r => `<tr class="${r.saved ? 'saved-profile' : ''}"><th scope="row">${r.level}/5 ${labels[r.level] || ''}${r.saved ? '<small>Saved setting / reference</small>' : ''}</th><td>${r.with_move} / ${r.evaluations}${delta(r.delta.with_move)}</td><td>${replayNumber(r.ready_evaluations)}${delta(r.delta.ready_evaluations)}</td><td>${r.guests.length}</td><td>${r.guest_reverse_recommendations}${delta(r.delta.guest_reverse_recommendations)}</td><td>${replayNumber(r.score_median)}${delta(r.delta.score_median)}</td></tr>`).join('')}</tbody></table></div>
    <p class="group-note">Suggestions repeat when recorded load stays unchanged. These are not migration counts. Same-guest reversals are opposite suggestions within six hours, not verified pingpong. Planner-ready counts exclude execution preflight and simulated post-move effects.</p>
    ${data.profiles.map(r => `<details class="replay-profile"><summary>Level ${r.level}/5: diagnostics</summary><p>Minimum score: ${r.policy.minimum_score}. Observe for ${r.policy.hold_seconds}s. Guest cooldown: ${r.policy.cooldown_seconds / 3600}h. Full host measurements: ${r.complete_host_data_evaluations} / ${r.evaluations} steps.</p><p>Planner statuses: ${escapeHtml(Object.entries(r.statuses).map(([s, n]) => `${stateLabel(s)}: ${n}`).join(' / '))}. Host-pair direction reversals: ${r.direction_reversals}.</p><p>Mean projected reduction in the source/target maximum CPU/RAM load: ${replayNumber(r.projected_relief_mean_pp)}${r.projected_relief_mean_pp == null ? '' : ' percentage points'}.</p>
      <h4>Frequent exclusions</h4><ul>${r.blocked_reasons.map(b => `<li>${escapeHtml(b.reason)} (${b.occurrences} occurrences across evaluated candidates)</li>`).join('') || '<li>No exclusions recorded.</li>'}</ul>
      <h4>Mean raw scoring terms</h4><p class="group-note">Costs and penalties are subtracted; final scores may be capped. These values describe repeated suggestions, not cumulative gains.</p><dl class="resource-facts">${Object.entries(r.score_breakdown_mean).map(([k, v]) => `<dt>${escapeHtml(stateLabel(k))}</dt><dd>${replayNumber(v)}</dd>`).join('') || '<dt>No scored suggestions</dt><dd>Not assessed</dd>'}</dl>
      <h4>Most frequent suggestions</h4><ul>${r.top_moves.map(m => `<li>${escapeHtml(m.resource)}: ${escapeHtml(m.source)} → ${escapeHtml(m.target)} (${m.evaluations} steps)</li>`).join('') || '<li>No suggestions.</li>'}</ul><p class="group-note">${escapeHtml(r.note)}</p></details>`).join('')}<p class="group-note">${escapeHtml(data.note)}</p>`;
}

// Replay and comparison require an administrator.
const replayAllowed = () => !access.me?.auth_enabled || access.me?.user?.role === 'admin';

function renderReplayControls() {
  const allowed = replayAllowed();
  $$('#replay-form button, #comparison-form button').forEach(b => b.disabled = replayState.busy || !allowed);
  $$('[data-replay-admin-note]').forEach(note => note.hidden = allowed);
}

function resetReplay() {
  replayState.serial++; replayState.busy = false;
  for (const prefix of ['replay', 'comparison']) { $(`#${prefix}-status`).textContent = ''; $(`#${prefix}-result`).innerHTML = ''; }
  renderReplayControls();
}

async function submitReplay(comparison) {
  if (!state.selected || replayState.busy || !replayAllowed()) return;
  const connection = state.selected, serial = ++replayState.serial, prefix = comparison ? 'comparison' : 'replay';
  const form = $(`#${prefix}-form`);
  const body = comparison ? {hours: Number(form.elements.hours.value), step_minutes: Number(form.elements.step_minutes.value),
    levels: Array.from(form.querySelectorAll('[name=level]:checked'), input => Number(input.value))} :
    {hours: 24, step_minutes: 5, mode: form.elements.mode.value, settings: form.elements.mode.value !== 'continuous' && form.elements.effort.value ? {distribution_effort: form.elements.effort.value} : {}};
  if (comparison && !body.levels.length) {$('#comparison-status').textContent = 'Select at least one level.'; return;}
  replayState.busy = true; renderReplayControls();
  $(`#${prefix}-status`).textContent = 'Evaluating recorded data. Larger comparisons can take up to about 90 seconds...'; $(`#${prefix}-result`).innerHTML = '';
  try {
    const result = await api(`/connections/${connection}/balancing/replay${comparison ? '/compare' : ''}`, {method: 'POST', body: JSON.stringify(body)});
    if (connection !== state.selected || serial !== replayState.serial) return;
    $(`#${prefix}-status`).textContent = ''; $(`#${prefix}-result`).innerHTML = comparison ? comparisonHtml(result) : replayHtml(result);
  } catch (error) {if (connection === state.selected && serial === replayState.serial) $(`#${prefix}-status`).textContent = error.message;}
  finally {if (connection === state.selected && serial === replayState.serial) {replayState.busy = false; renderReplayControls();}}
}

document.getElementById('replay-form').addEventListener('submit', event => {event.preventDefault(); submitReplay(false);});
document.getElementById('comparison-form').addEventListener('submit', event => {event.preventDefault(); submitReplay(true);});
document.getElementById('replay-form').elements.mode.addEventListener('change', event => {
  $('#replay-form').elements.effort.disabled = event.target.value === 'continuous';
});

async function loadCpuPerformance() {
  const connection = state.selected, target = $('#cpu-performance-hosts');
  if (!connection) return;
  try {
    const data = await api(`/connections/${connection}/balancing/cpu-performance`);
    if (connection !== state.selected) return;
    const nodes = (state.cluster?.nodes || []).map(n => n.node).sort();
    target.innerHTML = nodes.length ? nodes.map(node => `<label>${escapeHtml(node)}<input type="number" name="${escapeHtml(node)}" min="0.25" max="4" step="0.05" value="${data.factors[node] ?? 1}" required></label>`).join('') : '<p class="group-note">No hosts in the current inventory.</p>';
    $('#cpu-performance-status').textContent = '';
  } catch (error) {if (connection === state.selected) $('#cpu-performance-status').textContent = error.message;}
}

document.getElementById('cpu-performance-form').addEventListener('submit', async event => {
  event.preventDefault();
  const connection = state.selected; if (!connection) return;
  const factors = Object.fromEntries([...event.target.querySelectorAll('input[type=number]')].map(input => [input.name, Number(input.value)]));
  try {
    const data = await api(`/connections/${connection}/balancing/cpu-performance`, {method: 'PUT', body: JSON.stringify({factors})});
    if (connection === state.selected) $('#cpu-performance-status').textContent = `Saved. ${Object.keys(data.factors).length} hosts differ from the reference.`;
  } catch (error) {if (connection === state.selected) $('#cpu-performance-status').textContent = error.message;}
});

function trackLabel(track) {
  return {distribution: 'Scheduled balancing', dynamic: 'Dynamic relief', maintenance: 'Maintenance', placement_rules: 'Placement rules'}[track] || 'Placement';
}

function scheduleMarkup(schedule, saved = false) {
  if (!schedule) return '';
  if (schedule.continuous && !saved) return '<div class="plan-policy"><strong>Continuous balancing</strong><span>Independent of the calendar. One move at a time, followed by fresh measurements and a settling period.</span></div>';
  const format = value => new Date(value * 1000).toLocaleString('en-GB', {timeZone: schedule.timezone, weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
  const next = schedule.next_at ? format(schedule.next_at) : 'Disabled';
  const label = !saved && schedule.one_off ? 'Scheduled balancing / On-demand assessment' : !schedule.enabled ? 'Scheduled balancing schedule disabled; dynamic monitoring remains active' : saved ? 'Scheduled balancing schedule enabled' : schedule.manual_preview ? 'Manual preview of enabled tracks' : schedule.active ? 'Scheduled balancing start window is open' : 'Dynamic monitoring / scheduled balancing preview';
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const cadence = schedule.cadence === 'hourly' ? `Every ${schedule.interval_hours || 1} ${(schedule.interval_hours || 1) === 1 ? 'hour' : 'hours'}` : schedule.cadence === 'weekly' ? (schedule.weekly || []).map(entry => `${days[entry.day]} ${entry.time}`).join(', ') || 'No weekdays selected' : `Daily at ${schedule.time}`;
  const execution = schedule.execution;
  return `<div class="plan-policy"><strong>${label}</strong><span>${escapeHtml(cadence)} / ${escapeHtml(schedule.timezone)}</span><span>Next scheduled run: ${escapeHtml(next)}. Automatically balances ${schedule.guest_kind === 'lxc' ? 'VMs and LXC containers (live VM migration; container restart with downtime)' : 'VMs only (live migration)'}. Together-groups move as one decision. Continuous balancing waits and reassesses afterwards.</span>${execution ? `<small>Last scheduled run: ${escapeHtml(stateLabel(execution.status))}. ${escapeHtml(execution.message)}</small>` : ''}${(schedule.upcoming || []).length > 1 ? `<small>Following runs: ${schedule.upcoming.slice(1).map(value => escapeHtml(format(value))).join(' / ')}</small>` : ''}</div>`;
}

function updateScheduleFields() {
  const form = $('#drs-settings');
  for (const [prefix, fieldset, attribute, dayAttribute] of [
    ['distribution', 'perfect', 'data-schedule-fields', 'data-weekly-day'],
    ['lxc', 'lxc', 'data-lxc-schedule-fields', 'data-lxc-weekly-day']]) {
    const mode = form.elements[prefix + '_schedule'].value, enabled = form.elements[prefix + '_enabled'].checked;
    $('#' + fieldset + '-schedule-fields').disabled = !enabled;
    $$('[' + attribute + ']').forEach(section => {
      const active = mode === section.getAttribute(attribute);
      section.hidden = !active;
      section.querySelectorAll('input').forEach(input => input.disabled = !enabled || !active);
    });
    $$('[' + dayAttribute + ']').forEach(row => {
      row.querySelector('input[data-time24]').disabled = !enabled || mode !== 'weekly' || !row.querySelector('input[type=checkbox]').checked;
    });
  }
}

function automationMode(elements) {
  if (elements.paused.checked) return 'off';
  const scheduled = elements.distribution_enabled.checked, continuous = elements.continuous_enabled.checked;
  return scheduled && continuous ? 'both' : scheduled ? 'scheduled' : continuous ? 'continuous' : 'manual';
}

function syncAutomationMode() {
  $('#automation-mode').value = automationMode($('#drs-settings').elements);
}

document.getElementById('automation-mode').addEventListener('change', event => {
  const elements = $('#drs-settings').elements, mode = event.target.value;
  elements.paused.checked = mode === 'off';
  elements.distribution_enabled.checked = mode === 'scheduled' || mode === 'both';
  elements.distribution_enabled.dispatchEvent(new Event('change', {bubbles: true}));
  // The continuous switch saves through its own endpoint, exactly as when it is clicked.
  if (elements.continuous_enabled.checked !== (mode === 'continuous' || mode === 'both') && !elements.continuous_enabled.disabled) elements.continuous_enabled.click();
  syncAutomationMode();
});
document.getElementById('drs-settings').addEventListener('change', event => {if (event.target.id !== 'automation-mode') syncAutomationMode();});

function renderDrsSettings(config) {
  const form = $('#drs-settings');
  if (!config || drs.aggressivenessBusy || drs.continuousBusy || form.contains(document.activeElement) || drs.settingsRevision === `${state.selected}/${config.revision}`) return;
  form.elements.delay_seconds.value = config.delay_seconds;
  form.elements.hold_seconds.value = config.hold_seconds;
  form.elements.placement_repair_enabled.checked = config.placement_repair_enabled ?? true;
  form.elements.ha_guests_enabled.checked = config.ha_guests_enabled ?? false;
  form.elements.maintenance_concurrency.value = config.maintenance_concurrency ?? 3;
  form.elements.maintenance_memory_mode.value = config.maintenance_memory_mode || 'observed';
  form.elements.distribution_effort.value = config.distribution_effort || 'balanced';
  form.elements.continuous_enabled.checked = config.continuous_enabled === true;
  form.elements.balloon_recovery_enabled.checked = config.balloon_recovery_enabled !== false;
  const lxc = config.container_schedule || {};
  form.elements.lxc_enabled.checked = lxc.enabled === true;
  form.elements.lxc_schedule.value = lxc.cadence || 'daily';
  form.elements.lxc_time.value = lxc.time || '04:00';
  form.elements.lxc_interval_hours.value = lxc.interval_hours || 1;
  form.elements.lxc_timezone.value = lxc.timezone || 'Europe/Oslo';
  $$('[data-lxc-weekly-day]').forEach(row => {
    const entry = (lxc.weekly || []).find(item => item.day === Number(row.dataset.lxcWeeklyDay));
    row.querySelector('input[type=checkbox]').checked = !!entry;
    row.querySelector('input[data-time24]').value = entry?.time || lxc.time || '04:00';
  });
  form.elements.continuous_aggressiveness.value = config.continuous_aggressiveness ?? 3;
  form.elements.continuous_interval_seconds.value = config.continuous_interval_seconds ?? 30;
  for (const [name, fallback] of Object.entries(drsThresholdDefaults)) form.elements[name].value = config[name] ?? fallback;
  continuousProfile();
  form.elements.paused.checked = config.paused;
  form.elements.distribution_enabled.checked = config.distribution_enabled === true;
  syncAutomationMode();
  form.elements.distribution_schedule.value = config.distribution_schedule || 'daily';
  form.elements.distribution_time.value = config.distribution_time || '03:00';
  form.elements.distribution_interval_hours.value = config.distribution_interval_hours || 1;
  $$('[data-weekly-day]').forEach(row => {
    const entry = (config.distribution_weekly || []).find(item => item.day === Number(row.dataset.weeklyDay));
    row.querySelector('input[type=checkbox]').checked = !!entry;
    row.querySelector('input[data-time24]').value = entry?.time || config.distribution_time || '03:00';
  });
  form.elements.timezone.value = config.timezone || 'Europe/Oslo';
  updateScheduleFields();
  drs.settingsRevision = `${state.selected}/${config.revision}`;
}

function storageDetails(check) {
  if (!check) return '';
  return `<div class="storage-check"><strong>Disk locality: ${escapeHtml(check.state)}</strong><p class="group-note">Checked ${dateSeconds(check.checked_at)} on ${escapeHtml(check.node)}. Local or unverified disks are excluded from DRS.</p>${(check.disks || []).length ? `<div class="table-scroll"><table class="policy-table"><thead><tr><th>Disk / attachment</th><th>Volume</th><th>Location</th></tr></thead><tbody>${check.disks.map(disk => `<tr><td>${escapeHtml(disk.device)}</td><td>${escapeHtml(disk.volume)}</td><td>${escapeHtml(disk.state)}</td></tr>`).join('')}</tbody></table></div>` : ''}</div>`;
}

// Light fill is the load now; the dark marker is the projected load after the move.
function loadBar(label, before, after) {
  const now = Math.round(Math.min(1, Math.max(0, before || 0)) * 100), next = Math.round(Math.min(1, Math.max(0, after || 0)) * 100);
  return `<span class="load-row"><span>${label}</span><span class="load-bar" role="img" aria-label="${label} ${now}% to ${next}%"><i style="width:${now}%"></i><b style="left:${next}%"></b></span><span>${now}→${next}%</span></span>`;
}

function candidateBars(candidate) {
  const p = candidate.projection;
  if (!p?.source_before || !p?.target_before) return '';
  return `<span class="load-bars">${[[candidate.source, p.source_before, p.source_after], [candidate.target, p.target_before, p.target_after]].map(([host, before, after]) =>
    `<span class="load-host">${escapeHtml(host)}</span>${loadBar('CPU', before.cpu, after.cpu)}${loadBar('RAM', before.memory, after.memory)}`).join('')}</span>`;
}

function candidateDetails(candidate) {
  const failure = candidate.migration_failures;
  const failureNote = failure?.failures_7d ? `<p>Failed migrations: ${failure.failures_7d} in seven days. Failure penalty: ${failure.penalty} points. Each failure adds 200 points and loses one point per minute over 200 minutes.</p>` : '';
  if (candidate.score === null) return `${failureNote}<ul>${(candidate.reasons || []).map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>${storageDetails(candidate.storage)}`;
  const score = candidate.score_breakdown || {}, projection = candidate.projection;
  if (candidate.unit_moves) return `<p>${escapeHtml(candidate.reason)}</p>
    <p><strong>${candidate.unit_moves.length} coordinated migrations</strong> to ${escapeHtml(candidate.target)}. ${candidate.rule_repair ? 'Priority: rule repair. No history window, stickiness, cooldown, balancing score or CPU/balloon threshold is required.' : `Group net benefit: ${score.group_net_benefit} points; history/failure penalties: ${score.group_penalty}. The entire group must improve placement.`}</p>
    <p>Combined reservation: ${bytes(candidate.reserved_memory)} RAM · ${Number(candidate.reserved_cpu_cores).toFixed(2)} CPU cores.</p>
    <ul>${candidate.unit_moves.map(move => `<li>${escapeHtml(move.resource)}: ${escapeHtml(move.source)} → ${escapeHtml(move.target)} · ${move.requires_downtime ? 'Container restart (downtime)' : 'VM live migration'}</li>`).join('')}</ul>
    ${(candidate.placement_overrides || []).map(reason => `<p class="warning">${escapeHtml(reason)}</p>`).join('')}`;
  return `${candidate.migration_mode === "restart" ? '<p class="warning"><strong>Container restart migration:</strong> this guest shuts down and restarts on the target. Downtime is required.</p>' : ""}<p>${escapeHtml(candidate.reason)}</p>${candidate.policy_reason ? `<p class="warning">Policy blocked: ${escapeHtml(candidate.policy_reason)} No migration is permitted by this preview.</p>` : ''}<div class="score-parts"><span>Placement preference <b>${score.placement_preference ?? 0}</b></span><span>Resource relief <b>${score.resource_relief >= 0 ? '+' : ''}${score.resource_relief ?? '—'}</b></span><span>Balance change <b>${score.balance_improvement ?? '—'}</b></span><span>Commitment relief <b>${score.commitment_relief ?? 0}</b></span><span>Balloon recovery <b>${score.balloon_recovery ?? 0}</b></span><span>Allocation balance <b>${score.allocation_balance ?? 0}</b></span><span>Effort cost factor <b>${score.effort_multiplier ?? 1}×</b></span><span>I/O opportunity <b>${score.io_opportunity ?? 0}</b></span><span>CPU contention relief <b>${score.contention_relief ?? 0}</b></span><span>Workload selection <b>${score.workload_selection ?? 0}</b></span><span>Migration cost <b>−${score.migration_cost ?? '—'}</b></span><span>Stickiness multiplier <b>${score.stickiness_multiplier ?? '—'}×</b></span></div>
    ${projection ? `<div class="projection-grid">${[['Source', projection.source_before, projection.source_after], ['Target', projection.target_before, projection.target_after]].map(([label, before, after]) => `<div><strong>${label}</strong><p>CPU ${pct(before.cpu)} → ${pct(after.cpu)}</p><p>RAM ${pct(before.memory)} → ${pct(after.memory)}</p></div>`).join('')}</div>` : ''}
    <p>Reserved: ${bytes(candidate.reserved_memory)} RAM · ${Number(candidate.reserved_cpu_cores || 0).toFixed(2)} CPU cores · Current metrics: ${dateSeconds(candidate.metrics_observed_at)} · Stickiness ${candidate.stickiness}/10</p>
    ${candidate.workload ? `<p><strong>${categoryLabel(candidate.workload.category)}</strong>. Long-term distribution gain: ${Number(candidate.distribution_gain || 0).toFixed(2)} points.</p><div class="score-parts">${Object.entries(score.distribution_parts || {}).map(([key, value]) => `<span>${{count: 'Guest count', classes: 'Category spread', cpu: 'Current CPU', resident: 'Resident RAM', configured: 'Configured RAM'}[key] || escapeHtml(key)} <b>${Number(value).toFixed(2)}</b></span>`).join('')}</div>` : ''}
    ${failureNote}
    ${candidate.movement_history ? `<p>Migration history: ${candidate.movement_history.moves_24h} moves in 24 hours / ${candidate.movement_history.moves_7d} in seven days. History penalty: ${score.migration_history_penalty ?? 0} points. Guest cooldown: ${(candidate.movement_history.cooldown_seconds / 3600).toFixed(1)}h.</p>` : ''}
    ${candidate.expected_memory != null ? `<p>Expected target load increase: ${bytes(candidate.expected_memory)} resident RAM ? ${Number(candidate.expected_cpu_cores || 0).toFixed(2)} CPU cores. Reservations above are capacity checks, not predicted consumption.</p>` : ''}
    ${candidate.potential_balloon_recovery ? `<p>Potential balloon recovery: ${bytes(candidate.potential_balloon_recovery)} additional capacity beyond native recovery already possible. Projected return: ${bytes(candidate.source_balloon_refill)} on the source and ${bytes(candidate.target_balloon_refill)} on the target, limited by their configured thresholds. Actual return is gradual and verified by telemetry.</p>` : ''}
    ${candidate.io_assumption ? `<p class="warning">${escapeHtml(candidate.io_assumption)}</p>` : ''}
    ${candidate.override ? '<p class="warning">Protected guest: this preview would require a verified maintenance/emergency exception.</p>' : ''}
    <p class="group-note">${escapeHtml(candidate.cost_quality || '')} Score is a relative priority, not a success probability or VMware DRS score.</p>
    ${(candidate.alternatives || []).length ? `<p>Alternative targets: ${candidate.alternatives.map(a => `${escapeHtml(a.target)} (${a.score} points)`).join(' · ')}</p>` : ''}
    ${storageDetails(candidate.storage)}
    ${(candidate.reasons || []).length ? `<details><summary>Other target exclusions</summary><ul>${candidate.reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}</ul></details>` : ''}`;
}

function categoryLabel(category) {
  const [size, load] = category.split('/');
  return `${{small: 'Small', medium: 'Medium', large: 'Large'}[size] || 'Unknown size'} / ${{low: 'Low', medium: 'Medium', high: 'High'}[load] || 'Unknown'} demand`;
}

function distributionMarkup(distribution) {
  if (!distribution?.groups?.length) return '';
  const number = value => finite(value) === null ? 'Unknown' : Number(value).toFixed(1);
  return `<section class="detail-panel distribution-plan"><h2>Long-term distribution</h2><p>${escapeHtml(distribution.description)}</p><p>${distribution.conditional_moves} conditional moves forecast, up to ${distribution.horizon_limit} per assessment. Only one move executes before fresh validation. Pinned guests and cooldowns can prevent reaching every goal.</p>${distribution.groups.map(group => group.available ? `<article><h3>${escapeHtml(group.name)}</h3><p>Distribution cost: <strong>${number(group.cost_before)} &rarr; ${number(group.cost_after)}</strong> after the forecast (lower is better).</p><div class="table-scroll"><table class="policy-table"><thead><tr><th>Host</th><th>Running guests<br>Now / goal / forecast</th><th>Current CPU cores<br>Now / goal / forecast</th><th>Resident RAM<br>Now / goal / forecast</th><th>Configured RAM<br>Now / goal / forecast</th></tr></thead><tbody>${group.hosts.map(host => `<tr><td>${escapeHtml(host.node)}</td><td>${host.current.count} / ${number(host.target.count)} / <strong>${host.projected.count}</strong></td><td>${number(host.current.cpu)} / ${number(host.target.cpu)} / ${number(host.projected.cpu)}</td><td>${bytes(host.current.resident)} / ${bytes(host.target.resident)} / ${bytes(host.projected.resident)}</td><td>${bytes(host.current.configured)} / ${bytes(host.target.configured)} / ${bytes(host.projected.configured)}</td></tr>`).join('')}</tbody></table></div><details><summary>Workload categories by host</summary><p class="group-note">Size uses configured RAM and vCPUs. Demand uses current vCPU utilization and resident/configured RAM. Each cell shows now / goal / forecast. Goals are fractional capacity shares; exact equality may be impossible.</p><div class="table-scroll"><table class="policy-table"><thead><tr><th>Category</th>${group.hosts.map(h => `<th>${escapeHtml(h.node)}</th>`).join('')}</tr></thead><tbody>${Object.keys(group.classes).sort().map(category => `<tr><td>${categoryLabel(category)}</td>${group.hosts.map(h => `<td>${h.current.classes[category] || 0} / ${number(h.target.classes[category])} / ${h.projected.classes[category] || 0}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details></article>` : `<article><h3>${escapeHtml(group.name)}</h3><p>${escapeHtml(group.reason)}</p></article>`).join('')}</section>`;
}

function planMarkup(data) {
  const allCandidates = data.candidates || data.proposals || [];
  const isBlocked = c => c.score === null || c.status === 'policy_blocked';
  const filter = data.maintenance_source ? 'all' : drs.candidateFilter;
  const candidates = allCandidates.filter(c => filter === 'all' || (filter === 'blocked' ? isBlocked(c) : !isBlocked(c)));
  const diagnostic = !(data.plan || []).length && (data.diagnostic_plan || []).length > 0, plan = diagnostic ? data.diagnostic_plan : data.plan || [];
  const opened = new Set($$('#balancing-evaluation details[open][data-candidate]').map(el => el.dataset.candidate));
  const selected = new Map(plan.map(move => [move.resource, move]));
  return `${distributionMarkup(data.distribution)}${scheduleMarkup(data.schedule)}<p>${escapeHtml(data.message)}</p>${diagnostic ? '<p class="warning"><strong>Capacity preview only.</strong> These placements are blocked by missing migration domain membership. They are not permitted moves.</p>' : ''}${(data.blocked || []).length ? `<details><summary>Planning constraints</summary><ul>${data.blocked.map(item => `<li>${escapeHtml(item.reason)} (${item.count})</li>`).join('')}</ul></details>` : ''}<div class="plan-policy"><strong>${data.mode === 'continuous' ? 'Continuous balancing / One move per reassessment' : data.strategy === 'critical' ? 'Critical pressure · Bounded waves' : data.strategy === 'maintenance' ? 'Evacuation preview' : 'Gradual balancing'}</strong><span>${plan.length} ${diagnostic ? 'hypothetical placements' : 'proposed moves'} · Recheck after every move · Stop when further benefit disappears</span></div>
    ${plan.length ? `<ol class="plan-steps">${plan.map(move => `<li><span class="plan-order">${move.order}</span><div><strong>${escapeHtml(move.name)}</strong>${move.unit_moves ? `<small>Coordinated group | ${move.unit_moves.length} migrations | Fixed target ${escapeHtml(move.target)}</small>` : ''}<small>${trackLabel(move.evaluation_track)}${diagnostic ? ' / Policy blocked' : ''}</small><small>${escapeHtml(move.source)} → ${escapeHtml(move.target)} · ${move.score} points · Wave ${move.wave}</small><span>${move.conditional ? 'Conditional on earlier moves and fresh validation' : 'First move to review'} · Estimated peak load ${pct(move.before)} → ${pct(move.after)}</span></div></li>`).join('')}</ol>` : '<p class="guest-empty">No executable plan is implied. Candidates may be waiting, blocked, or no useful move exists.</p>'}
    <div class="panel-heading"><h3>Candidate ranking <span class="badge">${candidates.length} / ${allCandidates.length}</span></h3>${data.maintenance_source ? '' : `<label class="candidate-filter">Show <select data-candidate-filter aria-label="Filter DRS candidates">${[['all', 'All guests'], ['eligible', 'Scored candidates'], ['blocked', `Blocked (${allCandidates.filter(isBlocked).length})`]].map(([value, label]) => `<option value="${value}" ${filter === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label>`}</div>
    ${candidates.length ? `<div class="table-scroll"><table class="policy-table candidate-table"><thead><tr><th>Priority</th><th>Guest</th><th>Source → target</th><th>Score</th><th>State / earliest review</th><th>Plan</th></tr></thead><tbody>${candidates.map(c => `<tr><td>${c.rank || '—'}<small>${trackLabel(c.evaluation_track)} / ${escapeHtml(c.urgency || 'routine')}</small></td><td><button class="text-button" data-open-resource="${escapeHtml(c.resource)}">${escapeHtml(c.name)}</button><small>${escapeHtml(c.resource)}${c.resource?.startsWith("lxc/") ? " / Container restart migration" : " / VM live migration"}</small></td><td>${escapeHtml(c.source)} → ${escapeHtml(c.target || 'No eligible target')}${c.score !== null ? candidateBars(c) : ''}</td><td>${finite(c.score) !== null ? `<span class="score-badge">${c.score.toFixed(1)}</span>` : '—'}</td><td>${stateBadge(c.status, 'review_ready')}${c.policy_reason ? `<small>${escapeHtml(c.policy_reason)}</small>` : ''}${c.ready_at ? `<small data-ready-at="${c.ready_at}" data-plan-state="${escapeHtml(c.status)}" data-continuous="${data.mode === 'continuous'}"></small>` : ''}</td><td>${selected.has(c.resource) ? `Step ${selected.get(c.resource).order}` : c.status === 'native_ha' ? 'Native HA' : c.score === null ? 'Excluded' : 'Alternative'}</td></tr><tr class="candidate-detail-row"><td colspan="6"><details data-candidate="${escapeHtml(c.resource)}" ${opened.has(c.resource) ? 'open' : ''}><summary>${c.status === 'native_ha' ? 'Why ProxHA does not move this guest' : c.score === null ? 'Why this guest is blocked' : 'Score, evidence and target checks'}</summary>${candidateDetails(c)}</details></td></tr>`).join('')}</tbody></table></div>` : '<p class="guest-empty">No scored candidate is available. Check host imbalance, data coverage and planning constraints.</p>'}
    <p class="group-note">${escapeHtml(data.limitations)} Policies are re-read on evaluation. Evaluate now is a preview. Run now starts a separate migration queue shown above.</p>`;
}

function renderDrs() {
  renderMigrationQueue();
  renderContinuous();
  const data = drs.preview || migrationGroups.evaluation?.latest;
  if (data?.infrastructure) $('#infrastructure-overview').innerHTML = infrastructureMarkup(data.infrastructure);
  const guard = migrationGroups.evaluation?.latest?.backup_guard;
  $('#backup-guard').hidden = !guard?.blocked;
  $('#backup-guard').textContent = guard?.message || '';
  renderBalloonRecovery(data);
  const schedule = migrationGroups.evaluation?.schedule || migrationGroups.evaluation?.latest?.schedule || data?.schedule;
  $('#drs-run-result').hidden = !drs.feedback;
  $('#drs-run-result').classList.toggle('run-failed', drs.failed);
  $('#drs-run-message').textContent = drs.feedback;
  $('#drs-run-snapshot').hidden = !drs.preview;
  $('#show-live-drs').hidden = !drs.preview;
  $('#show-live-drs').disabled = drs.busy;
  $('#lxc-schedule-status').innerHTML = migrationGroups.evaluation?.container_schedule ? scheduleMarkup(migrationGroups.evaluation.container_schedule, true) : 'VM + LXC scheduling is disabled by default.';
  $('#evaluate-lxc').disabled = !state.selected || drs.busy;
  $('#perfect-schedule-status').innerHTML = schedule ? scheduleMarkup(schedule, true) : 'Schedule details will appear after an evaluation.';
  const target = $('#balancing-evaluation');
  $('#evaluate-drs').disabled = !state.selected || drs.busy;
  $$('[data-run-perfect]').forEach(button => {button.disabled = !state.selected || drs.busy || !!drs.run?.active; button.setAttribute('aria-busy', String(drs.busy)); button.textContent = drs.busy ? 'Working…' : button.dataset.runPerfect === 'lxc' ? 'Run VM + LXC now' : 'Run VM now';});
  if (!state.selected || !data) {
    if (state.selected) renderDrsSettings(migrationGroups.evaluation?.settings);
    target.textContent = migrationGroups.evaluationError ? `Evaluation unavailable: ${migrationGroups.evaluationError}` : 'Waiting for background metrics and the first evaluation. No migrations will be submitted.';
    $('#drs-overview').textContent = target.textContent;
    $('#drs-notice').textContent = `${(drs.continuous || migrationGroups.evaluation?.continuous)?.enabled ? 'Continuous balancing enabled' : 'Continuous balancing disabled'} · ${schedule?.enabled ? 'Scheduled balancing enabled' : 'Schedule disabled'} · Run now can migrate · Seven-day history`;
    return;
  }
  const expired = Date.now() / 1000 > (data.valid_until || data.evaluated_at + 360);
  $('#drs-notice').textContent = `${expired ? 'Plan expired, evaluate again' : 'Last evaluated ' + dateSeconds(data.evaluated_at)} · ${(drs.continuous || migrationGroups.evaluation?.continuous)?.enabled ? 'Continuous balancing enabled' : 'Continuous balancing disabled'} · ${schedule?.enabled ? 'Scheduled balancing enabled' : 'Schedule disabled'} · Run now can migrate · History 7 days`;
  $('#drs-overview').innerHTML = `<div class="detail-grid">${(data.hosts || []).map(host => `<article class="drs-host"><div><button class="text-button" data-open-resource="node/${escapeHtml(host.node)}">${escapeHtml(host.node)}</button>${stateBadge(host.state)}</div>${host.cpu !== undefined ? metric('CPU', host.cpu) + metric('RAM', host.memory) : '<p>Waiting for sufficient fresh measurements.</p>'}${host.ram ? `<p>Allocated: ${bytes(host.assigned_memory)} (${host.assigned_memory === null || host.assigned_memory === undefined ? 'unknown' : (host.assigned_memory / host.ram).toFixed(2) + 'x physical RAM'})</p><p>Ballooned: ${host.balloon_coverage < 1 ? 'at least ' : ''}${bytes(host.balloon_reclaimed)}${host.balloon_coverage < 1 ? ' / partial reporting' : ''}</p>` : ''}${(host.placement_reasons || []).length ? `<ul class="group-note">${host.placement_reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>` : ''}<small>Coverage ${pct(host.coverage)}${host.evidence ? ' · ' + escapeHtml(host.evidence) : ''}</small></article>`).join('')}</div><div class="plan-policy"><strong>Two planning tracks</strong><span>Scheduled distribution reduces sustained imbalance and memory commitment. Dynamic relief responds to CPU, RAM and I/O pressure. Normal moves are gradual and require fresh measurements.</span></div>`;
  target.innerHTML = planMarkup(data);
  $('#drs-overview').insertAdjacentHTML('afterbegin', distributionMarkup(data.distribution) + scheduleMarkup(data.schedule));
  renderDrsSettings(migrationGroups.evaluation.settings || data.settings);
  updatePlanTimers();
}

function updatePlanTimers() {
  $$('[data-ready-at]').forEach(el => {
    const remaining = Math.max(0, Math.ceil(Number(el.dataset.readyAt) - Date.now() / 1000));
    el.textContent = el.dataset.planState === 'scheduled' ? `Scheduled run: ${dateSeconds(Number(el.dataset.readyAt))}; fresh validation required` : el.dataset.planState === 'paused' ? 'Paused; reevaluation required' : remaining ? `${Math.floor(remaining / 60)}m ${remaining % 60}s remaining` : el.dataset.planState === 'waiting' ? 'Delay elapsed; awaiting fresh evaluation' : el.dataset.continuous === 'true' ? 'Observation complete; awaiting live validation' : 'Preview ready; execution requires a fresh assessment';
  });
}

async function loadDrsHistory() {
  const connection = state.selected, serial = ++drs.historySerial;
  if (!connection) return;
  try {
    const data = await api(`/connections/${connection}/balancing/history`);
    if (connection !== state.selected || serial !== drs.historySerial) return;
    $('#drs-history').innerHTML = data.evaluations.length ? `<div class="table-scroll"><table class="policy-table"><thead><tr><th>Evaluated</th><th>Strategy</th><th>Candidates</th><th>Plan</th><th>Top score</th></tr></thead><tbody>${data.evaluations.map(row => `<tr><td>${dateSeconds(row.evaluated_at)}</td><td>${escapeHtml(row.strategy)}<small>${escapeHtml(row.evaluation_kind === 'run_now' ? 'Run now' : row.evaluation_kind || 'legacy')}</small></td><td>${row.candidates}</td><td>${row.planned_moves} proposed moves${row.diagnostic_moves ? ` / ${row.diagnostic_moves} policy-blocked placements` : ''}</td><td>${row.top_score ?? '—'}</td></tr>`).join('')}</tbody></table></div>` : '<p>No evaluations yet.</p>';
  } catch (error) {if (connection === state.selected && serial === drs.historySerial) $('#drs-history').textContent = error.message;}
}

document.querySelectorAll('[data-drs-tab]').forEach(button => button.addEventListener('click', () => drsTab(button.dataset.drsTab)));
document.getElementById('queue-shortcut').addEventListener('click', () => drsTab('candidates'));
document.addEventListener('change', event => {
  if (event.target.matches('[data-candidate-filter]') && ['all', 'eligible', 'blocked'].includes(event.target.value)) {
    drs.candidateFilter = event.target.value; renderDrs();
  }
});
async function evaluateDrs(guestKind = 'qemu') {
  if (!state.selected || drs.busy) return;
  const connection = state.selected, serial = ++drs.requestSerial;
  const current = () => connection === state.selected && serial === drs.requestSerial;
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 30000);
  drs.busy = true; drs.failed = false;
  drs.feedback = 'Evaluating current placement…';
  renderDrs();
  try {
    const data = await api(`/connections/${connection}/balancing/evaluate${guestKind === 'lxc' ? '?guest_kind=lxc' : ''}`, {method: 'POST', signal: controller.signal});
    if (!current()) return;
    migrationGroups.evaluation = {...migrationGroups.evaluation, latest: data, settings: data.settings};
    migrationGroups.evaluationError = '';
    drs.preview = data;
    const count = (data.plan || []).length;
    drs.feedback = `Evaluation completed at ${dateSeconds(data.evaluated_at)}: ${count} proposed ${count === 1 ? 'move' : 'moves'}, ${(data.candidates || []).length} guests assessed. ${!count ? (data.message || 'No eligible move found.') + ' ' : ''}This evaluation does not start migrations.`;
    drs.candidateFilter = 'all';
    drsTab('candidates');
    $('#drs-run-result').scrollIntoView?.({block: 'nearest'});
  } catch (error) {
    if (current()) {
      drs.failed = true;
      drs.feedback = error.name === 'AbortError' ? 'Evaluation timed out. Check History for a completed result, or try again.' : `Evaluation failed: ${error.message}`;
      toast(drs.feedback);
    }
  } finally {
    clearTimeout(timeout);
    if (current()) {drs.busy = false; renderDrs();}
  }
}
document.getElementById('evaluate-drs').addEventListener('click', () => evaluateDrs());
document.querySelectorAll('[data-run-perfect]').forEach(button => button.addEventListener('click', () => startMigrationQueue('balance', button.dataset.runPerfect || 'qemu')));
document.getElementById('evaluate-lxc').addEventListener('click', () => evaluateDrs('lxc'));
document.getElementById('recover-balloon').addEventListener('click', () => startMigrationQueue('balloon_recovery'));
document.querySelectorAll('[data-stop-queue]').forEach(button => button.addEventListener('click', stopMigrationQueue));
document.getElementById('show-live-drs').addEventListener('click', () => {clearDrsRun(); renderDrs(); refreshGroups();});
document.getElementById('drs-settings').addEventListener('submit', async event => {
  event.preventDefault();
  const connection = state.selected, form = event.target, config = migrationGroups.evaluation?.settings || migrationGroups.evaluation?.latest?.settings;
  if (!connection || !config) return;
  if (drs.aggressivenessBusy || drs.continuousBusy) {$('#drs-settings-status').textContent = 'Wait for continuous balancing settings to finish saving.'; return;}
  const weekly = $$('[data-weekly-day]').filter(row => row.querySelector('input[type=checkbox]').checked).map(row => ({day: Number(row.dataset.weeklyDay), time: row.querySelector('input[data-time24]').value || '03:00'}));
  if (form.elements.distribution_enabled.checked && form.elements.distribution_schedule.value === 'weekly' && !weekly.length) {
    $('#drs-settings-status').textContent = 'Select at least one weekday and time for scheduled balancing.'; return;
  }
  const lxc = {enabled: form.elements.lxc_enabled.checked, cadence: form.elements.lxc_schedule.value,
    time: form.elements.lxc_time.value || '04:00', interval_hours: Number(form.elements.lxc_interval_hours.value || 1),
    timezone: form.elements.lxc_timezone.value,
    weekly: $$('[data-lxc-weekly-day]').filter(row => row.querySelector('input[type=checkbox]').checked)
      .map(row => ({day: Number(row.dataset.lxcWeeklyDay), time: row.querySelector('input[data-time24]').value || '04:00'}))};
  if (lxc.enabled && lxc.cadence === 'weekly' && !lxc.weekly.length) {
    $('#drs-settings-status').textContent = 'Select at least one weekday and time for VM + LXC balancing.'; return;
  }
  const button = form.querySelector('button[type=submit]'); button.disabled = true;
  try {
    await api(`/connections/${connection}/balancing/settings`, {method: 'PUT', body: JSON.stringify({...Object.fromEntries(Object.keys(drsThresholdDefaults).map(name => [name, Number(form.elements[name].value)])), paused: form.elements.paused.checked, placement_repair_enabled: form.elements.placement_repair_enabled.checked, ha_guests_enabled: form.elements.ha_guests_enabled.checked, balloon_recovery_enabled: form.elements.balloon_recovery_enabled.checked, container_schedule: lxc, continuous_enabled: form.elements.continuous_enabled.checked, continuous_aggressiveness: Number(form.elements.continuous_aggressiveness.value), continuous_interval_seconds: Number(form.elements.continuous_interval_seconds.value), delay_seconds: Number(form.elements.delay_seconds.value), hold_seconds: Number(form.elements.hold_seconds.value), maintenance_concurrency: Number(form.elements.maintenance_concurrency.value), maintenance_memory_mode: form.elements.maintenance_memory_mode.value, revision: config.revision, distribution_effort: form.elements.distribution_effort.value, distribution_enabled: form.elements.distribution_enabled.checked, distribution_schedule: form.elements.distribution_schedule.value, distribution_time: form.elements.distribution_time.value || '03:00', distribution_interval_hours: Number(form.elements.distribution_interval_hours.value || 1), distribution_weekly: weekly, timezone: form.elements.timezone.value})});
    if (connection !== state.selected) return;
    $('#drs-settings-status').textContent = 'Saved. Candidate waiting periods were reset.';
    clearDrsRun(); drs.settingsRevision = null; await refreshGroups();
  } catch (error) {if (connection === state.selected) $('#drs-settings-status').textContent = error.message;}
  finally {button.disabled = false;}
});
document.getElementById('drs-settings').addEventListener('change', updateScheduleFields);
document.querySelector('[name=continuous_aggressiveness]').addEventListener('input', continuousProfile);
document.querySelector('[name=continuous_aggressiveness]').addEventListener('change', saveAggressiveness);
document.querySelectorAll('[data-continuous-enabled]').forEach(input => input.addEventListener('change', () => changeContinuous(input.checked)));
setInterval(updatePlanTimers, 1000);
setInterval(() => {if (!document.hidden) refreshMigrationQueue();}, 3000);

function capacityReportMarkup(data) {
  return `<p><strong>${escapeHtml(stateLabel(data.status))}</strong> / ${data.running_guests} running guests / ${data.ha_managed} registered with native HA / ${dateSeconds(data.evaluated_at)}</p><p class="group-note">${escapeHtml(data.note)}</p>${(data.unassessed_guests || []).map(g => `<p class="warning">${escapeHtml(g.name)} (${escapeHtml(g.resource)}): running on unavailable or maintenance host ${escapeHtml(g.node)}; capacity is not assessed.</p>`).join('')}${data.scenarios.map(s => `<details data-capacity-host="${escapeHtml(s.failed_host)}"><summary>Loss of ${escapeHtml(s.failed_host)}: ${escapeHtml(stateLabel(s.status))} / ${s.placed} of ${s.affected} guests placed</summary>${Object.entries(s.host_errors || {}).map(([n, reason]) => `<p class="warning">${escapeHtml(n)}: ${escapeHtml(reason)}</p>`).join('')}${s.unplaced.map(g => `<p class="warning"><strong>${escapeHtml(g.name)} (${escapeHtml(g.resource)})</strong>: ${escapeHtml(g.reason)}</p><ul>${g.targets.map(t => `<li>${escapeHtml(t.target)}: ${escapeHtml(t.reasons.join(' '))}</li>`).join('')}</ul>`).join('')}<ul>${s.placements.map(g => `<li>${escapeHtml(g.name)} (${escapeHtml(g.resource)}) to ${escapeHtml(g.target)}</li>`).join('')}</ul>${s.exceptions.map(x => `<p class="warning">${escapeHtml(x)}</p>`).join('')}</details>`).join('')}`;
}

let capacitySerial = 0;
document.getElementById('check-capacity').addEventListener('click', async () => {
  const connection = state.selected, serial = ++capacitySerial, button = $('#check-capacity');
  if (!connection) return;
  button.disabled = true; $('#capacity-result').textContent = 'Assessing each host failure...';
  try {
    const data = await api(`/connections/${connection}/capacity?memory_mode=${$('#capacity-memory-mode').value}`);
    if (connection !== state.selected || serial !== capacitySerial) return;
    $('#capacity-result').innerHTML = capacityReportMarkup(data);
  } catch (error) {if (connection === state.selected && serial === capacitySerial) $('#capacity-result').textContent = `Capacity unknown: ${error.message}`;}
  finally {if (serial === capacitySerial) button.disabled = false;}
});

let infraData = null, infraSerial = 0;
const infraNumber = (n, suffix = '') => finite(n) === null ? 'Unknown' : Number(n).toFixed(2) + suffix;
function infrastructureMarkup(nodes) {
  if (!Object.keys(nodes || {}).length) return '<p>No physical measurement sources configured. Configure node_exporter in DRS Settings. These metrics are unknown.</p>';
  return Object.entries(nodes).map(([node, d]) => `<article class="detail-panel"><h3>${escapeHtml(node)} / ${escapeHtml(d.configured ? d.stale ? 'stale' : d.quality : 'disabled')}</h3><p>${dateSeconds(d.sampled_at)} ${escapeHtml(d.error || '')}</p>${d.block_reason ? `<p class="warning">${escapeHtml(d.block_reason)}</p>` : ''}<div class="table-scroll"><table class="policy-table"><thead><tr><th>Disk</th><th>Latency</th><th>Average queue</th><th>IOPS</th><th>Busy</th></tr></thead><tbody>${(d.disks || []).map(x => `<tr><td>${escapeHtml(x.device)}</td><td>${infraNumber(x.latency_ms, ' ms')}</td><td>${infraNumber(x.queue_depth)}</td><td>${infraNumber(x.iops)}</td><td>${infraNumber(x.busy_percent, '%')}</td></tr>`).join('')}</tbody></table><table class="policy-table"><thead><tr><th>Interface</th><th>Capacity</th><th>RX / TX</th><th>Utilization</th><th>Drops / errors</th></tr></thead><tbody>${(d.interfaces || []).map(x => `<tr><td>${escapeHtml(x.device)}</td><td>${finite(x.capacity_bps) === null ? 'Unknown' : infraNumber(x.capacity_bps * 8 / 1e9, ' Gbit/s')}</td><td>${rateText(x.rx_bps)} / ${rateText(x.tx_bps)}</td><td>${infraNumber(x.utilization_percent, '%')}</td><td>${infraNumber(x.drop_percent, '%')} / ${infraNumber(x.error_percent, '%')}</td></tr>`).join('')}</tbody></table></div><button class="text-button" data-infrastructure-history="${escapeHtml(node)}">24-hour history</button><button class="text-button" data-infrastructure-history="${escapeHtml(node)}" data-hours="168">Seven-day history</button></article>`).join('');
}
function fillInfrastructure() {
  const form = $('#infrastructure-form'), c = infraData?.sources?.[form.elements.node.value] || {};
  for (const key of ['enabled', 'guard_enabled']) form.elements[key].checked = c[key] ?? key === 'guard_enabled';
  form.elements.url.value = c.url || '';
  for (const key of ['disks', 'interfaces']) form.elements[key].value = (c[key] || []).join(',');
  for (const [key, value] of Object.entries({disk_latency_ms:20, disk_queue_depth:2, network_utilization_percent:90, network_drop_percent:1})) form.elements[key].value = c[key] ?? value;
}
async function loadInfrastructure() {
  const connection = state.selected, serial = ++infraSerial;
  try {
    const data = await api(`/connections/${connection}/infrastructure`);
    if (connection !== state.selected || serial !== infraSerial) return;
    infraData = data; const form = $('#infrastructure-form'), selected = form.elements.node.value;
    form.elements.node.innerHTML = data.nodes.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
    if (data.nodes.includes(selected)) form.elements.node.value = selected;
    fillInfrastructure(); $('#infrastructure-overview').innerHTML = infrastructureMarkup(data.latest);
  } catch (error) {if (connection === state.selected && serial === infraSerial) $('#infrastructure-source-status').textContent = error.message;}
}
document.getElementById('infrastructure-form').elements.node.addEventListener('change', fillInfrastructure);
document.getElementById('infrastructure-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.target, connection = state.selected, node = form.elements.node.value, serial = ++infraSerial;
  if (!infraData || !node) return;
  const payload = {url: form.elements.url.value.trim(), enabled: form.elements.enabled.checked, guard_enabled: form.elements.guard_enabled.checked, revision: infraData.sources[node]?.revision || 0};
  for (const key of ['disks', 'interfaces']) payload[key] = form.elements[key].value.split(',').map(x => x.trim()).filter(Boolean);
  for (const key of ['disk_latency_ms', 'disk_queue_depth', 'network_utilization_percent', 'network_drop_percent']) payload[key] = Number(form.elements[key].value);
  const button = form.querySelector('button[type=submit]'); button.disabled = true;
  try {
    const data = await api(`/connections/${connection}/infrastructure/${encodeURIComponent(node)}`, {method:'PUT', body:JSON.stringify(payload)});
    if (connection !== state.selected || serial !== infraSerial) return;
    infraData = data; $('#infrastructure-source-status').textContent = 'Saved. Waiting for fresh exporter samples.';
  } catch (error) {if (connection === state.selected && serial === infraSerial) $('#infrastructure-source-status').textContent = error.message;}
  finally {button.disabled = false;}
});
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-infrastructure-history]'); if (!button) return;
  const connection = state.selected, serial = ++infraSerial, node = button.dataset.infrastructureHistory, hours = Number(button.dataset.hours || 24);
  try {
    const data = await api(`/connections/${connection}/infrastructure/${encodeURIComponent(node)}/history?hours=${hours}`);
    if (connection !== state.selected || serial !== infraSerial) return;
    showView('balancing'); drsTab('overview');
    const rows = data.samples.map(s => ({sampled_at:s.sampled_at, ...s.mean, metadata:{summary:{max:s.max, counts:s.counts}}}));
    $('#infrastructure-history').innerHTML = `<h3>${escapeHtml(node)} / ${hours} hours / minute means and peaks</h3><div class="history-grid">${[['Physical disk latency', 'disk_latency_ms', ' ms'], ['Physical disk queue', 'disk_queue_depth', ''], ['Network utilization', 'network_utilization_percent', '%'], ['Interface drops', 'network_drop_percent', '%'], ['Interface errors', 'network_error_percent', '%']].map(([title, key, suffix]) => historyChart(title, [[key, title]], rows, v => infraNumber(v, suffix))).join('')}</div>`;
    $('#infrastructure-history').scrollIntoView?.({block:'nearest'});
  } catch (error) {if (connection === state.selected && serial === infraSerial) $('#infrastructure-history').textContent = error.message;}
});

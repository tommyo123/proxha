function automaticMigrationsHtml(moves) {
  if (!moves) return '<p class="group-note">Automatic migration history is unavailable.</p>';
  return `<section id="automatic-migrations"><div class="panel-heading"><h2>Automatic migrations</h2></div>${factsList([
    ['Last 24 hours', moves.migrations_24h], ['Last 7 days', moves.migrations_7d], ['Possible return moves (7 days)', moves.return_moves_7d]
  ])}<p class="group-note">${escapeHtml(moves.scope)}</p>${moves.migrations.length ? `<details ${insights.outcome ? 'open' : ''}><summary>Recent automatic moves (up to 50)</summary><div class="table-scroll"><table class="policy-table"><thead><tr><th>Completed</th><th>Source</th><th>Target</th><th>Score</th><th>Return</th><th>Measurements</th></tr></thead><tbody>${moves.migrations.map(move => `<tr><td>${dateSeconds(move.finished_at)}</td><td>${escapeHtml(move.source)}</td><td>${escapeHtml(move.target)}</td><td>${escapeHtml(move.score)}</td><td>${move.return_move ? 'Review return move' : '—'}</td><td>${move.operation_id ? `<button class="secondary" data-migration-outcome="${escapeHtml(move.operation_id)}">${insights.outcome?.id === move.operation_id ? 'Refresh outcome' : 'Before / after'}</button>` : '—'}</td></tr>`).join('')}</tbody></table></div><div id="migration-outcome" aria-live="polite">${migrationOutcomeHtml(insights.outcome)}</div></details>` : '<p>No completed automatic migrations in the last seven days.</p>'}</section>`;
}

function migrationOutcomeHtml(state) {
  if (!state) return '';
  if (state.loading) return '<p>Loading before/after measurements …</p>';
  if (state.error) return `<p class="warning">${escapeHtml(state.error)}</p>`;
  const data = state.data, number = value => finite(value) === null ? '—' : Number(value).toFixed(2);
  const labels = {pending: 'Waiting for measurements', measured: 'Measured change', confounded: 'Overlapping placement activity', insufficient_data: 'Insufficient data'};
  return `<h3>${escapeHtml(labels[data.status] || data.status)} · ${escapeHtml(data.source)} → ${escapeHtml(data.target)}</h3><p>${escapeHtml(data.message)}</p><p>Before: ${dateSeconds(data.before_window[0])} to ${dateSeconds(data.before_window[1])}<br>After: ${dateSeconds(data.after_window[0])} to ${dateSeconds(data.after_window[1])}</p><p>Migration score: ${escapeHtml(data.score)} · Submitted to verified: ${number(data.duration_seconds)}s · Group members: ${escapeHtml(data.cohort_members.join(', '))}</p>${data.comparisons.length ? `<div class="table-scroll"><table class="policy-table"><thead><tr><th>Resource</th><th>Metric</th><th>Before</th><th>After</th><th>Change</th><th>Coverage before / after</th></tr></thead><tbody>${data.comparisons.map(c => `<tr><td>${escapeHtml(c.entity)}</td><td>${escapeHtml(c.metric)} (${escapeHtml(c.unit)})</td><td>${number(c.before)}</td><td>${number(c.after)}</td><td>${finite(c.change) > 0 ? '+' : ''}${number(c.change)}${c.unit === '%' ? ' pp' : ''}</td><td>${pct(c.before_coverage)} / ${pct(c.after_coverage)}</td></tr>`).join('')}</tbody></table></div>` : ''}<p class="group-note">${escapeHtml(data.scope)}</p>`;
}

async function loadMigrationOutcome(id) {
  const resource = insights.resource, connection = state.selected, serial = insights.serial;
  if (!resource || !connection || insights.outcome?.loading) return;
  const request = insights.outcome = {id, loading: true};
  const render = () => {
    const panel = document.getElementById('migration-outcome');
    if (panel) panel.innerHTML = migrationOutcomeHtml(request);
  };
  render();
  try {
    request.data = await api(`/connections/${connection}/guests/${resource.key}/automatic-migrations/${encodeURIComponent(id)}/outcome`);
  } catch (error) {
    request.error = `Could not load migration outcome: ${error.message}`;
  }
  request.loading = false;
  if (state.selected === connection && insights.serial === serial && insights.outcome === request) render();
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-migration-outcome]');
  if (button) loadMigrationOutcome(button.dataset.migrationOutcome);
});

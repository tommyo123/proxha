import asyncio
import json
import logging
import time
from contextlib import suppress
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from . import adaptive, background, backup_guard, balancing, migration_queue, scheduled
from .proxmox import ProxmoxError

SCHEMA = '''CREATE TABLE IF NOT EXISTS continuous_state (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    document TEXT NOT NULL, updated_at REAL NOT NULL
);'''


class EnabledInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)
    revision: int = Field(ge=0, strict=True)


def read(db, connection_id):
    row = db.execute('SELECT document FROM continuous_state WHERE connection_id=?', (connection_id,)).fetchone()
    return json.loads(row['document']) if row else {'status': 'disabled', 'message': 'Continuous balancing is disabled.'}


def save(db, connection_id, state, now):
    db.execute('INSERT OR REPLACE INTO continuous_state VALUES(?,?,?)', (connection_id, json.dumps(state, separators=(',', ':')), now))


def document(db, connection_id):
    config = balancing.settings(db, connection_id)
    state = read(db, connection_id)
    return {**{k: v for k, v in state.items() if k != 'observation'},
            'revision': config['revision'], 'suspended': False,
            'enabled': config['continuous_enabled'], 'aggressiveness': config['continuous_aggressiveness'],
            'interval_seconds': config['continuous_interval_seconds'], 'policy': adaptive.profile(config),
            'hourly_budget': adaptive.hourly_budget(db, connection_id, time.time(), config['continuous_migrations_per_hour'])}


def change(db, connection_id, now, enabled=None, resume=False):
    config = balancing.settings(db, connection_id)
    if enabled is not None:
        revision = config.pop('revision')
        config['continuous_enabled'] = enabled
        db.execute('INSERT OR REPLACE INTO balancing_settings VALUES(?,?,?)', (connection_id, json.dumps(config), revision + 1))
    state = read(db, connection_id)
    state.update(observation=None, next_check_at=now, ready_at=None, candidate=None,
                 suspended=False, failure=None, error_count=0, retry_at=None)
    if not config['continuous_enabled']:
        state.update(status='disabled', message='Continuous balancing is disabled. Submitted migrations finish normally.')
        run = migration_queue.active(db, connection_id)
        if run and run['trigger'] == 'continuous':
            migration_queue.stop(db, connection_id, run['id'], now)
    else:
        state.update(status='observing', message='Waiting for fresh observations before the next migration.')
    save(db, connection_id, state, now)


def recover(db, now):
    for row in db.execute('SELECT connection_id,document FROM continuous_state').fetchall():
        state = json.loads(row['document'])
        enabled = balancing.settings(db, row['connection_id'])['continuous_enabled']
        state.update(observation=None, candidate=None, ready_at=None, suspended=False, failure=None, error_count=0, retry_at=None,
                     next_check_at=now, next_move_at=max(state.get('next_move_at') or 0, now + 60),
                     status='observing' if enabled else 'disabled',
                     message='Waiting for fresh observations after server startup.' if enabled else 'Continuous balancing is disabled.')
        save(db, row['connection_id'], state, now)


def observation(result):
    candidate_keys = ('resource', 'source', 'target', 'status', 'urgency', 'evaluation_track', 'first_seen_at', 'unit_members')
    return {**{k: result[k] for k in ('algorithm_version', 'policy_key', 'evaluated_at', 'pressure_since', 'pressure_streaks')},
            'hosts': [{k: h.get(k) for k in ('node', 'evaluation_track', 'relief')} for h in result['hosts']],
            'candidates': [{k: c.get(k) for k in candidate_keys} for c in result['candidates']]}


def eligible(result):
    return [m for m in result['candidates'] if m.get('score') is not None and m.get('policy_allowed')
            and not m.get('requires_storage_review') and m.get('stickiness', 10) < 10 and m['status'] == 'review_ready']


class Worker:
    def __init__(self, database, lock):
        self.database, self.lock = database, lock
        self.task = None
        self.wakeup = asyncio.Event()

    def start(self):
        self.task = asyncio.create_task(self.run(), name='continuous-balancing')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def review(self, connection_id, now):
        async with self.lock:
            await background.run(self.review_sync, connection_id, now)

    def review_sync(self, connection_id, now):
        with self.database() as db:
            config = balancing.settings(db, connection_id)
            state = read(db, connection_id)
            if not config['continuous_enabled']:
                return
            if scheduled.reserved(db, connection_id, config, now):
                state.update(status='scheduled_paused', message='Scheduled balancing has priority. Continuous balancing resumes with a fresh assessment afterwards.',
                             observation=None, candidate=None, ready_at=None, next_check_at=now,
                             history_reset_at=now)
                save(db, connection_id, state, now)
                return
            if (state.get('next_check_at') or 0) > now:
                return
            policy = adaptive.profile(config)
            state.update(last_checked_at=now, next_check_at=now + config['continuous_interval_seconds'],
                         suspended=False, failure=None, error_count=0, retry_at=None)

            def finish(status, message, reset=True):
                state.update(status=status, message=message)
                if reset:
                    state['observation'] = None
                    state['ready_at'] = None
                save(db, connection_id, state, now)

            if config['paused']:
                finish('paused', 'Planning is paused. No new automatic migrations will start.')
                return
            backup = backup_guard.read(db, connection_id, now)
            if backup['blocked']:
                finish('backup_paused', backup['message'])
                return
            previous_run = migration_queue.document(db, connection_id, state['run_id']) if state.get('run_id') else None
            if previous_run and not previous_run['active'] and state.get('handled_run_id') != previous_run['id']:
                state['handled_run_id'] = previous_run['id']
                state['observation'] = None
                if previous_run['status'] in ('failed', 'interrupted'):
                    state['last_queue_error'] = previous_run['message']
                if previous_run['status'] == 'completed_with_errors':
                    state['next_move_at'] = max(state.get('next_move_at') or 0, previous_run['finished_at'] + config['continuous_interval_seconds'])
                if previous_run['completed']:
                    state['next_move_at'] = max(state.get('next_move_at') or 0, previous_run['finished_at'] + policy['settle_seconds'])
            current_run = migration_queue.active(db, connection_id)
            if current_run:
                finish('busy', 'A migration or maintenance queue is active. Waiting before further placement decisions.', reset=current_run['trigger'] != 'continuous')
                return
            try:
                migration_queue.ensure_idle(db, connection_id)
            except ProxmoxError as exc:
                finish('waiting', str(exc) + ' Continuous balancing remains enabled and checks again automatically.')
                return
            last_placement = db.execute('SELECT MAX(observed_at) FROM placement_events WHERE connection_id=?', (connection_id,)).fetchone()[0]
            next_move = max(state.get('next_move_at') or 0, (last_placement or 0) + policy['settle_seconds'])
            state['next_move_at'] = next_move if next_move > now else None
            if next_move > now:
                finish('settling', 'Waiting for post-migration measurements before another automatic move.')
                return
            result = balancing.evaluate(db, connection_id, now, state.get('observation'), manual=False,
                                        continuous=True)
            state['observation'] = observation(result)
            state['candidate_count'] = sum(m.get('score') is not None for m in result['candidates'])
            ready = eligible(result)
            budget = adaptive.hourly_budget(db, connection_id, now, config['continuous_migrations_per_hour'])
            if budget['remaining']:
                ready = [m for m in ready if m.get('rule_repair') or m['urgency'] == 'critical' or len(m.get('unit_moves', [m])) <= budget['remaining']]
            if not budget['remaining']:
                ready = [m for m in ready if m.get('rule_repair') or m['urgency'] == 'critical']
            db.execute('BEGIN IMMEDIATE')
            if not balancing.settings(db, connection_id)['continuous_enabled']:
                return
            db.execute("DELETE FROM balancing_evaluations WHERE connection_id=? AND evaluated_at>=? AND json_extract(result,'$.evaluation_kind')='continuous'", (connection_id, int(now // 300) * 300))
            db.execute('INSERT OR REPLACE INTO balancing_evaluations VALUES(?,?,?)', (connection_id, int(now), json.dumps(result, separators=(',', ':'))))
            if not ready:
                if not budget['remaining']:
                    state.update(candidate=None, ready_at=budget['retry_at'])
                    finish('rate_limited', f'Continuous migration budget used ({budget["used"]}/{budget["limit"]} in one hour). Confirmed critical pressure can bypass this limit.', reset=False)
                    return
                waiting = [c for c in result['candidates'] if c.get('policy_allowed') and not c.get('requires_storage_review') and c.get('ready_at') and c['status'] == 'waiting']
                state['ready_at'] = min((c['ready_at'] for c in waiting), default=None)
                state['candidate'] = None
                needs_review = any(c.get('score') is not None for c in result['candidates']) and not waiting
                constrained = needs_review or result['status'] in ('blocked', 'policy_blocked', 'no_feasible_move')
                finish('observing' if waiting else 'waiting' if constrained else 'balanced',
                       'Waiting for sustained benefit on the same guest and target.' if waiting else
                       'No eligible move currently passes the constraints. Continuous balancing remains enabled and reassesses automatically. See Candidates for details.' if constrained else
                       'No move is needed now. Continuous balancing remains enabled and keeps checking.', reset=False)
                return
            move = ready[0]
            proposal = {**{k: move[k] for k in ('resource', 'source', 'target', 'score')}, 'policy_key': result['policy_key']}
            try:
                run = migration_queue.start(db, connection_id, str(uuid4()), now, trigger='continuous', proposal=proposal,
                                            restart_containers=config['container_balancing_enabled'])
            except ProxmoxError as exc:
                finish('waiting', str(exc) + ' Continuous balancing remains enabled and checks again automatically.')
                return
            state.update(run_id=run['id'], candidate=proposal, ready_at=None)
            finish('migrating', f'Queued {move["resource"]}: {move["source"]} → {move["target"]}, score {move["score"]}.', reset=False)

    async def tick(self, now=None):
        now = time.time() if now is None else now
        with self.database() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM connections')]
        for connection_id in ids:
            try:
                await self.review(connection_id, now)
            except Exception:
                logging.getLogger(__name__).exception('Continuous balancing failed for connection %s.', connection_id)
                with self.database() as db:
                    if not db.execute('SELECT 1 FROM connections WHERE id=?', (connection_id,)).fetchone():
                        continue
                    state = read(db, connection_id)
                    config = balancing.settings(db, connection_id)
                    errors = state.get('error_count', 0) + 1
                    delay = min(300, config['continuous_interval_seconds'] * 2 ** min(4, errors - 1))
                    enabled = config['continuous_enabled']
                    state.update(status='retrying' if enabled else 'disabled', suspended=False, observation=None,
                                 candidate=None, ready_at=None, last_checked_at=now, next_check_at=now + delay,
                                 retry_at=now + delay if enabled else None, error_count=errors,
                                 failure='Unexpected controller error. See server logs.' if enabled else None,
                                 message=f'Continuous balancing remains enabled. Assessment failed; retrying automatically in {delay} seconds.' if enabled else
                                         'Continuous balancing is disabled. Submitted migrations finish normally.')
                    save(db, connection_id, state, now)

    async def run(self):
        while True:
            self.wakeup.clear()
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).exception('Continuous controller cycle failed; retrying automatically.')
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

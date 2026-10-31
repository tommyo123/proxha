import json
import re
import threading
import time
from urllib.parse import urlsplit

SCHEMA = '''CREATE TABLE IF NOT EXISTS connection_routing (
    connection_id INTEGER PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    state TEXT NOT NULL
);'''
POOLS = {}
LOCK = threading.RLock()
# Pinned fingerprints are refreshed through a verified endpoint; missing ones are retried sooner.
PIN_REFRESH_SECONDS = 600
PIN_RETRY_SECONDS = 120
MISMATCH = 'API endpoint belongs to a different cluster. Save the connection again to re-validate it.'


def fingerprints(certificates):
    """SHA-256 fingerprints of the node certificates that the API proxy can serve."""
    result = set()
    for certificate in certificates if isinstance(certificates, list) else []:
        if not isinstance(certificate, dict) or certificate.get('filename') == 'pve-root-ca.pem':
            continue
        value = str(certificate.get('fingerprint', '')).replace(':', '').lower()
        if re.fullmatch(r'[0-9a-f]{64}', value):
            result.add(value)
    return sorted(result)


class Pool:
    def __init__(self, configured, saved=None):
        self.lock = threading.RLock()
        self.state = saved if saved and saved.get('configured') == configured else {
            'configured': configured, 'active': None, 'cluster': None, 'nodes': [], 'endpoints': {}}
        self.state['endpoints'].setdefault(configured, {'node': None, 'online': True})
        self.state.setdefault('certificates', {})

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def ordered(self):
        """Endpoints in preference order; endpoints of another cluster are never returned."""
        with self.lock:
            now = time.time()
            usable = [url for url, entry in self.state['endpoints'].items() if not entry.get('identity_mismatch')]
            return sorted(usable, key=lambda url: (
                self.state['endpoints'][url].get('retry_at', 0) > now,
                not self.state['endpoints'][url].get('online', True),
                url != self.state['active'], url != self.state['configured'],
                -self.state['endpoints'][url].get('last_success', 0), url))

    def blocked(self, url):
        with self.lock:
            return bool(self.state['endpoints'].get(url, {}).get('identity_mismatch'))

    def revalidate(self):
        """Let endpoints rejected for a cluster mismatch be checked again."""
        with self.lock:
            for entry in self.state['endpoints'].values():
                if entry.get('identity_mismatch'):
                    entry.update(identity_mismatch=False, retry_at=0, error=None)

    def discovery_due(self):
        with self.lock:
            now = time.time()
            if now - self.state.get('discovery_attempt', 0) < 60:
                return False
            self.state['discovery_attempt'] = now
            return True

    def success(self, url):
        with self.lock:
            if url in self.state['endpoints']:
                self.state['active'] = url
                self.state['endpoints'][url].update(last_success=time.time(), retry_at=0, error=None)

    def failed(self, url, identity_mismatch=False, error='API endpoint unavailable.'):
        with self.lock:
            if url in self.state['endpoints']:
                self.state['endpoints'][url].update(retry_at=time.time() + 60, error=error)
                if identity_mismatch:
                    # The token is withheld from this endpoint until the connection is saved again.
                    self.state['endpoints'][url].update(identity_mismatch=True, error=MISMATCH)
                    if self.state['active'] == url:
                        self.state['active'] = None

    def note(self, url, error):
        with self.lock:
            if url in self.state['endpoints']:
                self.state['endpoints'][url]['error'] = error

    def pins(self, url):
        with self.lock:
            node = self.state['endpoints'].get(url, {}).get('node')
            return list(self.state['certificates'].get(node, {}).get('fingerprints', [])) if node else []

    def pin(self, node, values):
        with self.lock:
            if values and node in self.state['nodes']:
                self.state['certificates'].setdefault(node, {})['fingerprints'] = sorted(set(values))

    def certificates_due(self):
        with self.lock:
            now, due = time.time(), []
            for node in self.state['nodes']:
                entry = self.state['certificates'].setdefault(node, {})
                if now - entry.get('attempt', 0) >= (PIN_REFRESH_SECONDS if entry.get('fingerprints') else PIN_RETRY_SECONDS):
                    entry['attempt'] = now
                    due.append(node)
            return due

    def recheck_certificate(self, url):
        with self.lock:
            node = self.state['endpoints'].get(url, {}).get('node')
            if node in self.state['certificates']:
                self.state['certificates'][node]['attempt'] = 0

    def learn(self, status, url):
        if not isinstance(status, list):
            raise ValueError('Invalid cluster status.')
        nodes = [n for n in status if n.get('type') == 'node' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', str(n.get('name', '')))]
        cluster = next((n.get('name') for n in status if n.get('type') == 'cluster'), None)
        names = {n['name'] for n in nodes}
        with self.lock:
            known = set(self.state['nodes'])
            if known and (cluster != self.state['cluster'] or not names.intersection(known)):
                raise ValueError('API endpoint belongs to a different cluster.')
            self.state['endpoints'].setdefault(url, {})['identity_mismatch'] = False
            self.state['discovery_attempt'] = time.time()
            if not nodes:
                return
            self.state.update(cluster=cluster, nodes=sorted(names), discovery_attempt=time.time())
            for node in nodes[:64]:
                addresses = [node['name']]
                if node.get('ip'):
                    addresses.append(str(node['ip']))
                for address in addresses:
                    # Only node names and IP addresses returned by this cluster are trusted.
                    if not re.fullmatch(r'[A-Za-z0-9.:-]+', address):
                        continue
                    host = f'[{address}]' if ':' in address else address
                    endpoint = f'https://{host}:8006'
                    entry = self.state['endpoints'].setdefault(endpoint, {})
                    entry.update(node=node['name'], online=bool(node.get('online', 0)))
                    if urlsplit(self.state['configured']).hostname == address:
                        self.state['endpoints'][self.state['configured']].update(node=node['name'], online=entry['online'])
                if node.get('local'):
                    self.state['endpoints'].setdefault(url, {}).update(node=node['name'], online=bool(node.get('online', 0)))
            self.state['endpoints'] = {u: e for u, e in self.state['endpoints'].items()
                                       if u == self.state['configured'] or e.get('node') in names}
            self.state['certificates'] = {n: c for n, c in self.state['certificates'].items() if n in names}


def configure(db, connection_id, config, revalidate=False):
    row = db.execute('SELECT url,token_id,secret,verify_tls,trusted_ca FROM connections WHERE id=?', (connection_id,)).fetchone()
    signature = tuple(row)
    with LOCK:
        pool = POOLS.get(connection_id)
        if pool is None or pool.signature != signature:
            saved = db.execute('SELECT state FROM connection_routing WHERE connection_id=?', (connection_id,)).fetchone()
            pool = POOLS[connection_id] = Pool(config.url, json.loads(saved['state']) if saved else None)
            pool.signature = signature
        if revalidate:
            # A private copy leaves the shared pool unchanged if validation fails.
            pool = Pool(config.url, pool.snapshot())
            pool.revalidate()
        config._endpoint_pool = pool


def remember(db, connection_id, state):
    with LOCK:
        POOLS.pop(connection_id, None)
        db.execute('DELETE FROM connection_routing WHERE connection_id=?', (connection_id,))
        if state:
            db.execute('INSERT INTO connection_routing VALUES(?,?)', (connection_id, json.dumps(state)))


def persist(database):
    with LOCK:
        saved = [(connection_id, pool.snapshot(), pool.signature) for connection_id, pool in POOLS.items()]
    with database() as db:
        for connection_id, state, signature in saved:
            db.execute('''INSERT OR REPLACE INTO connection_routing(connection_id,state)
                SELECT id,? FROM connections WHERE id=? AND url=? AND token_id=? AND secret=? AND verify_tls=? AND trusted_ca IS ?''',
                       (json.dumps(state), connection_id, *signature))


def summary(db, connection_id, configured):
    with LOCK:
        pool = POOLS.get(connection_id)
        if pool:
            state = pool.snapshot()
        else:
            row = db.execute('SELECT state FROM connection_routing WHERE connection_id=?', (connection_id,)).fetchone()
            state = json.loads(row['state']) if row else {}
    return {'configured': configured, 'active': state.get('active'),
            'endpoints': [{'url': url, **info} for url, info in state.get('endpoints', {}).items()]}

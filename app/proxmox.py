import asyncio
import hashlib
import logging
import re
import ssl
from urllib.parse import quote, urlsplit

import httpcore
import httpx
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from . import cluster_ca, endpoints

LOG = logging.getLogger(__name__)
UNPINNED = 'Certificate fingerprint not yet learned from the cluster; this endpoint is not used while TLS verification is disabled.'
PINNING_UNAVAILABLE = ('Certificate pinning is unavailable with the installed httpx/httpcore release; this endpoint is not used '
                       'while TLS verification is disabled. Trust the cluster CA to use it.')


class ConnectionInput(BaseModel):
    _endpoint_pool: object = PrivateAttr(default=None)
    name: str = Field(min_length=1, max_length=80)
    url: str
    token_id: str = Field(pattern=r'^[^\s=!]+@[^\s=!]+![^\s=!]+$')
    token_secret: str = Field(default='', max_length=512)
    verify_tls: bool = True
    # None keeps the saved CA when editing; an empty value removes it.
    trusted_ca: str | None = Field(default=None, max_length=cluster_ca.MAX_BYTES)
    # The fingerprint the user compared on a cluster host; required to save a new CA.
    trusted_ca_fingerprint: str | None = Field(default=None, max_length=200)

    @field_validator('name', 'token_id', 'token_secret', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('token_secret')
    @classmethod
    def valid_secret(cls, value):
        if any(c.isspace() for c in value):
            raise ValueError('Token must not contain whitespace.')
        return value

    @field_validator('url')
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip('/')
        p = urlsplit(value)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path:
            raise ValueError('Use https://hostname:8006 without a path or credentials.')
        if not p.port:
            value += ':8006'
        return value

    @field_validator('trusted_ca')
    @classmethod
    def valid_ca(cls, value):
        if value is None or not value.strip():
            return value and ''
        return cluster_ca.normalize(value)

    @model_validator(mode='after')
    def ca_verifies(self):
        # A trusted cluster CA always means verified TLS.
        if self.trusted_ca:
            self.verify_tls = True
        return self


class ProxmoxError(Exception):
    def __init__(self, message, uncertain=False, retryable=False, status_code=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.retryable = retryable
        self.status_code = status_code

    @property
    def unavailable(self):
        """Gateway errors mean the API could not serve the request, not that the request was refused."""
        return self.status_code in (502, 503, 504)


class ClusterMismatch(ProxmoxError):
    pass


class CertificateMismatch(ProxmoxError):
    """The endpoint presented a certificate the cluster did not report; nothing was sent."""


class CandidateRejected(ProxmoxError):
    """The selected guest or destination failed a placement check."""


def segment(value):
    return quote(str(value), safe='')


def verification_failure(exc):
    """The certificate verification message behind a connection error, if any."""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return (exc.verify_message or str(exc)).rstrip('.')
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return None


def origin(url):
    parts = urlsplit(url)
    return (parts.hostname or '').lower(), parts.port or 443


class CertificatePinError(Exception):
    pass


class PinnedStream(httpcore.AsyncNetworkStream):
    """Checks the peer certificate after the TLS handshake, before any request bytes are written."""

    def __init__(self, stream, address, expected):
        self.stream, self.address, self.expected = stream, address, expected

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self.stream.write(buffer, timeout)

    async def aclose(self):
        await self.stream.aclose()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        # Unknown origins fail closed; None marks the configured URL, which follows the user's TLS setting.
        allowed = self.expected.get(self.address, frozenset())
        if allowed is not None:
            ssl_object = stream.get_extra_info('ssl_object')
            certificate = ssl_object.getpeercert(binary_form=True) if ssl_object else None
            if not certificate or hashlib.sha256(certificate).hexdigest() not in allowed:
                await stream.aclose()
                raise CertificatePinError(self.address)
        return stream


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend, expected):
        self.backend, self.expected = backend, expected

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self.backend.connect_tcp(host, port, timeout, local_address, socket_options)
        return PinnedStream(stream, (host.lower(), port), self.expected)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise CertificatePinError(path)

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class ProxmoxClient:
    def __init__(self, connection):
        self.connection = connection
        self.pool = connection._endpoint_pool or endpoints.Pool(connection.url)
        self.validated = {connection.url}
        # Origin -> accepted certificate fingerprints, or None for the configured URL.
        self.expected = {}
        # A trusted cluster CA verifies every endpoint, so no fingerprint pins are needed.
        self.verified = bool(connection.trusted_ca) or connection.verify_tls
        self.client = httpx.AsyncClient(
            base_url=connection.url + '/api2/json/',
            headers={'Authorization': f'PVEAPIToken={connection.token_id}={connection.token_secret}'},
            verify=cluster_ca.context(connection.trusted_ca) if connection.trusted_ca else connection.verify_tls,
            timeout=httpx.Timeout(20, connect=3, pool=3), trust_env=False,
        )
        # Certificate pinning requires the pinned httpcore transport interface.
        pool = getattr(getattr(self.client, '_transport', None), '_pool', None)
        self.pinning = False
        if not self.verified:
            if isinstance(getattr(pool, '_network_backend', None), httpcore.AsyncNetworkBackend):
                pool._network_backend = PinnedBackend(pool._network_backend, self.expected)
                self.pinning = True
            else:
                # Without this hook, discovered endpoints stay unused while TLS verification is disabled.
                LOG.warning('Certificate pinning hook unavailable; API failover is disabled for unverified TLS connections.')

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.client.__aexit__(*args)

    def redact(self, message):
        for value in (self.connection.token_secret, self.connection.token_id):
            if value:
                message = message.replace(value, '[redacted]')
        return message[:600]

    def pinned(self, endpoint):
        """Discovered endpoints need a certificate pin when TLS verification is disabled."""
        return not self.verified and endpoint != self.connection.url

    def identify(self, status, endpoint):
        try:
            self.pool.learn(status, endpoint)
        except ValueError as exc:
            error = ClusterMismatch(str(exc), retryable=True)
            error.endpoint_failed = True
            raise error from None

    async def refresh_certificates(self):
        nodes = self.pool.certificates_due()
        semaphore = asyncio.Semaphore(8)

        async def read(node):
            async with semaphore:
                try:
                    self.pool.pin(node, endpoints.fingerprints(await self.get(f'nodes/{segment(node)}/certificates/info')))
                except ProxmoxError:
                    pass

        await asyncio.gather(*(read(node) for node in nodes))

    async def request(self, method, path, data=None):
        if path != 'cluster/status' and self.pool.discovery_due():
            try:
                await self.request('GET', 'cluster/status')
            except ClusterMismatch:
                raise
            except ProxmoxError:
                pass
        if self.pinning and not path.endswith('/certificates/info'):
            await self.refresh_certificates()
        last = mismatch = None
        for endpoint in self.pool.ordered():
            if self.pinned(endpoint) and not (self.pinning and self.pool.pins(endpoint)):
                self.pool.note(endpoint, UNPINNED if self.pinning else PINNING_UNAVAILABLE)
                continue
            try:
                if endpoint not in self.validated:
                    try:
                        self.identify(await self.exchange('GET', endpoint, 'cluster/status'), endpoint)
                    except ProxmoxError as exc:
                        # Nothing has been submitted yet, so a peer that fails validation is skipped.
                        exc.endpoint_failed = True
                        raise
                    self.validated.add(endpoint)
                result = await self.exchange(method, endpoint, path, data)
                if method == 'GET' and path == 'cluster/status':
                    self.identify(result, endpoint)
                self.pool.success(endpoint)
                return result
            except ProxmoxError as exc:
                last = exc
                if isinstance(exc, ClusterMismatch):
                    mismatch = exc
                    self.validated.discard(endpoint)
                if isinstance(exc, CertificateMismatch):
                    self.pool.recheck_certificate(endpoint)
                if getattr(exc, 'endpoint_failed', False) or exc.retryable and endpoint not in self.validated:
                    self.pool.failed(endpoint, identity_mismatch=isinstance(exc, ClusterMismatch), error=str(exc))
                    continue
                raise
        if self.pool.blocked(self.connection.url):
            mismatch = mismatch or ClusterMismatch(endpoints.MISMATCH, retryable=True)
        raise mismatch or last or ProxmoxError('No reachable Proxmox API endpoint.', retryable=method == 'GET')

    async def exchange(self, method, endpoint, path, data=None):
        address = origin(endpoint)
        self.expected[address] = frozenset(self.pool.pins(endpoint)) if self.pinned(endpoint) else None
        try:
            response = await self.client.request(method, endpoint + '/api2/json/' + path.lstrip('/'), data=data)
            response.raise_for_status()
            return response.json()['data']
        except CertificatePinError:
            # The handshake ended before the request, so even a write may use another endpoint.
            error = CertificateMismatch('Endpoint certificate does not match the fingerprint reported by the cluster; nothing was sent.',
                                        retryable=method == 'GET')
            error.endpoint_failed = True
            raise error from None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            message = {401: 'API token rejected. Check the token ID and secret.',
                       403: 'Permission denied. Check user and token privileges for this operation.'}.get(code)
            if not message:
                message = f'Proxmox returned HTTP {code}.'
                try:
                    body = exc.response.json()
                    detail = body.get('message') or body.get('errors')
                    if detail:
                        message += ' ' + str(detail)
                except (ValueError, AttributeError):
                    pass
            error = ProxmoxError(self.redact(message), uncertain=method != 'GET' and code >= 500,
                                 retryable=method == 'GET' and (code in (408, 429) or code >= 500), status_code=code)
            error.endpoint_failed = method == 'GET' and code in (502, 503, 504)
            raise error from None
        except httpx.RequestError as exc:
            not_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            reason = verification_failure(exc)
            error = ProxmoxError(f'TLS certificate verification failed ({reason}); nothing was sent.' if reason else
                                 'Could not reach Proxmox. Check the address, network and TLS certificate.',
                                 uncertain=method != 'GET' and not not_sent, retryable=method == 'GET')
            error.endpoint_failed = method == 'GET' or not_sent
            error.certificate_rejected = bool(reason)
            # A lost response to a write must never cause a duplicate submission.
            if not error.endpoint_failed:
                self.pool.failed(endpoint)
            raise error from None
        except (ValueError, KeyError, TypeError):
            raise ProxmoxError('Invalid Proxmox API response.', uncertain=method != 'GET') from None

    async def get(self, path):
        return await self.request('GET', path)

    async def post(self, path, data):
        return await self.request('POST', path, data)


async def fetch_cluster(connection: ConnectionInput):
    async with ProxmoxClient(connection) as client:
        status = await client.get('cluster/status')
        version, resources = await asyncio.gather(client.get('version'), client.get('cluster/resources'))
        if not isinstance(resources, list) or not isinstance(status, list) or not isinstance(version, dict):
            raise ProxmoxError('Invalid Proxmox inventory response.')
        nodes = sorted([r for r in resources if r.get('type') == 'node'], key=lambda n: n['node'])
        if not nodes:
            raise ProxmoxError('No visible nodes. Grant PVEAuditor on / to both the user and API token.')
        guests = sorted([r for r in resources if r.get('type') in ('qemu', 'lxc')], key=lambda r: r['vmid'])
        semaphore = asyncio.Semaphore(8)

        async def enrich(resource, path, node=False):
            async with semaphore:
                try:
                    detail = await client.get(path)
                    if not isinstance(detail, dict):
                        raise ProxmoxError('Invalid memory metrics response.')
                    if node:
                        resource['ksm_shared'] = detail.get('ksm', {}).get('shared')
                    else:
                        for key in ('balloon', 'balloon_min', 'ballooninfo', 'maxmem', 'qmpstatus', 'lock', 'ha'):
                            if key in detail:
                                resource[key] = detail[key]
                        actual, maximum = detail.get('balloon'), detail.get('maxmem')
                        resource['balloon_reclaimed'] = max(0, maximum - actual) if isinstance(actual, (int, float)) and actual > 0 and isinstance(maximum, (int, float)) else None
                except ProxmoxError as exc:
                    resource['metrics_error'] = str(exc)

        calls = [enrich(n, f'nodes/{segment(n["node"])}/status', node=True) for n in nodes if n.get('status') == 'online']
        calls += [enrich(g, f'nodes/{segment(g["node"])}/qemu/{g["vmid"]}/status/current') for g in guests if g['type'] == 'qemu' and g.get('status') == 'running']
        try:
            async with asyncio.timeout(25):
                await asyncio.gather(*calls)
        except TimeoutError:
            for r in nodes + guests:
                if (r['type'] == 'node' and 'ksm_shared' not in r) or (r['type'] == 'qemu' and r.get('status') == 'running' and 'balloon_reclaimed' not in r):
                    r.setdefault('metrics_error', 'Memory metrics timed out.')
        cluster = next((s for s in status if s.get('type') == 'cluster'), {})
        return {'name': cluster.get('name', connection.name), 'quorate': cluster.get('quorate'),
                'version': version.get('version', 'unknown'), 'nodes': nodes, 'guests': guests,
                **({'api_routing': client.pool.snapshot()} if hasattr(client, 'pool') else {})}


async def fetch_cluster_ca(connection: ConnectionInput):
    """Read the cluster CA through the configured URL only; nothing is saved."""
    async with ProxmoxClient(connection) as client:
        status = await client.exchange('GET', connection.url, 'cluster/status')
        node = next((n.get('name') for n in status if isinstance(n, dict) and n.get('type') == 'node' and n.get('local')), None) if isinstance(status, list) else None
        if not isinstance(node, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', node):
            raise ProxmoxError('Could not identify the Proxmox host behind the configured URL.')
        certificates = await client.exchange('GET', connection.url, f'nodes/{segment(node)}/certificates/info')
    entry = cluster_ca.from_certificates(certificates)
    if not entry:
        raise ProxmoxError(f'{node} did not report {cluster_ca.ROOT_CA_FILE}. Paste the CA certificate instead.')
    try:
        pem = cluster_ca.normalize(entry['pem'])
    except ValueError as exc:
        raise ProxmoxError(f'The cluster CA reported by {node} is not usable: {exc}') from None
    if entry.get('fingerprint') and not cluster_ca.confirmed(pem, str(entry['fingerprint'])):
        raise ProxmoxError(f'The fingerprint reported by {node} does not match its CA certificate.')
    return {**cluster_ca.describe(pem), 'node': node}

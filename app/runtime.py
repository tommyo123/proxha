"""Persistent server configuration and offline data migration."""
import json
import ipaddress
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent


def config_dir():
    return Path(os.environ.get('PROXHA_CONFIG_DIR') or os.environ.get('PROXHA_DATA_DIR') or ROOT / 'config').resolve()


def check_legacy_data(directory):
    if directory.resolve() == (ROOT / 'config').resolve() and not (directory / 'proxha.sqlite3').exists() and (ROOT / 'data/proxha.sqlite3').exists():
        raise RuntimeError('Existing data found in data/. Stop the old server, then run python -m app.server --migrate-from data.')


class TLSSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['self-signed', 'custom'] = 'self-signed'
    cert_file: str = 'tls/cert.pem'
    key_file: str = 'tls/key.pem'
    names: list[str] = Field(default_factory=lambda: ['localhost', '127.0.0.1', '::1'], min_length=1, max_length=50)

    @field_validator('names')
    @classmethod
    def valid_names(cls, values):
        result = []
        for name in values:
            try:
                result.append(str(ipaddress.ip_address(name)))
                continue
            except ValueError:
                pass
            name = name.encode('idna').decode('ascii')
            labels = name.removeprefix('*.').rstrip('.').split('.')
            if len(name) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
                raise ValueError('TLS names must be DNS names or IP addresses, without schemes or ports.')
            result.append(name)
        return list(dict.fromkeys(result))


class SSHSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user: str = Field(default='root', pattern=r'^[a-zA-Z_][a-zA-Z0-9_-]*$')
    key_file: str | None = None
    known_hosts_file: str | None = None


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str = '127.0.0.1'
    port: int = Field(default=8080, ge=1, le=65535)
    http_port: int = Field(default=8081, ge=1, le=65535)
    protocol: Literal['https', 'http', 'both'] = 'https'
    allowed_hosts: list[str] = Field(default_factory=lambda: ['localhost', '127.0.0.1', '[::1]'], min_length=1)
    tls: TLSSettings = Field(default_factory=TLSSettings)
    ssh: SSHSettings = Field(default_factory=SSHSettings)

    @model_validator(mode='after')
    def distinct_ports(self):
        if self.protocol == 'both' and self.port == self.http_port:
            raise ValueError('HTTP and HTTPS must listen on different ports.')
        return self

    def listeners(self):
        return [(scheme, port) for scheme, port in (('https', self.port), ('http', self.http_port))
                if self.protocol in (scheme, 'both')]

    @field_validator('allowed_hosts')
    @classmethod
    def host_patterns(cls, values):
        if any(not value or ',' in value or '/' in value or any(c.isspace() for c in value) for value in values):
            raise ValueError('Use host names or IP addresses without ports, spaces or URL schemes.')
        return values


def local_path(directory, value):
    path = (directory / value).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError('Configured file paths must stay inside the config directory.')
    return path


def tls_path(directory, settings, value):
    if settings.mode == 'custom' and Path(value).is_absolute():
        return Path(value).resolve()
    return local_path(directory, value)


def environment(settings):
    values = settings.model_dump()
    for variable, field in (('PROXHA_LISTEN_HOST', 'host'), ('PROXHA_LISTEN_PORT', 'port'),
                            ('PROXHA_HTTP_LISTEN_PORT', 'http_port'), ('PROXHA_PROTOCOL', 'protocol')):
        if variable in os.environ:
            values[field] = os.environ[variable]
    if os.environ.get('PROXHA_ALLOWED_HOSTS'):
        values['allowed_hosts'] = [name.strip() for name in os.environ['PROXHA_ALLOWED_HOSTS'].split(',')]
    cert, key = os.environ.get('PROXHA_TLS_CERT_FILE'), os.environ.get('PROXHA_TLS_KEY_FILE')
    if bool(cert) != bool(key):
        raise ValueError('Set both PROXHA_TLS_CERT_FILE and PROXHA_TLS_KEY_FILE.')
    if cert:
        values['tls'].update(mode='custom', cert_file=cert, key_file=key)
    public_host = os.environ.get('PROXHA_PUBLIC_HOST', '').strip()
    if public_host:
        if public_host.startswith('[') and public_host.endswith(']'):
            public_host = public_host[1:-1]
        values['tls']['names'].append(public_host)
        if values['allowed_hosts'] != ['*']:
            values['allowed_hosts'].append(f'[{public_host}]' if ':' in public_host else public_host)
    return Settings.model_validate(values)


def load(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'settings.json'
    if path.exists():
        return environment(Settings.model_validate_json(path.read_text(encoding='utf-8')))
    defaults = Settings(host='0.0.0.0', allowed_hosts=['*']) if os.environ.get('PROXHA_CONTAINER') == '1' else Settings()
    settings = environment(defaults)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(defaults.model_dump(), indent=2) + '\n')
    path.chmod(0o600)
    return settings


def migrate(source, target):
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise ValueError('Source and destination directories must differ.')
    if not (source / 'proxha.sqlite3').is_file() or not (source / 'secret.key').is_file():
        raise ValueError('The source must contain proxha.sqlite3 and secret.key.')
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [target / name for name in ('proxha.sqlite3', 'secret.key')]
    if any(path.exists() for path in paths):
        raise ValueError('The destination already contains a database or key; nothing was overwritten.')
    created = []
    try:
        for path in paths:
            with path.open('xb'):
                pass
            path.chmod(0o600)
            created.append(path)
        with sqlite3.connect((source / 'proxha.sqlite3').as_uri() + '?mode=ro', uri=True) as old:
            with sqlite3.connect(paths[0]) as new:
                old.backup(new)
                if new.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('The copied database failed its integrity check.')
        shutil.copyfile(source / 'secret.key', paths[1])
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def apply(directory, settings):
    os.environ['PROXHA_CONFIG_DIR'] = str(directory)
    os.environ['PROXHA_ALLOWED_HOSTS'] = ','.join(settings.allowed_hosts)
    ssh = settings.ssh
    if bool(ssh.key_file) != bool(ssh.known_hosts_file):
        raise ValueError('Set both SSH key_file and known_hosts_file.')
    if ssh.key_file:
        os.environ.setdefault('PROXHA_HA_SSH_KEY', str(local_path(directory, ssh.key_file)))
        os.environ.setdefault('PROXHA_HA_SSH_KNOWN_HOSTS', str(local_path(directory, ssh.known_hosts_file)))
        os.environ.setdefault('PROXHA_HA_SSH_USER', ssh.user)

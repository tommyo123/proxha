"""HTTP and HTTPS entry point with one shared application lifecycle."""
import argparse
import asyncio
import ipaddress
import logging
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

from . import runtime


def certificate(directory, settings):
    cert = runtime.tls_path(directory, settings, settings.cert_file)
    key = runtime.tls_path(directory, settings, settings.key_file)
    if cert == key:
        raise ValueError('TLS certificate and private key must use different files.')
    if settings.mode == 'custom':
        if not cert.is_file() or not key.is_file():
            raise ValueError('Custom TLS requires both the certificate chain and private key.')
    elif not cert.exists() and not key.exists():
        names = []
        for name in settings.names:
            try:
                names.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                if not name or any(c in name for c in '/:@ ') or len(name) > 253:
                    raise ValueError('TLS names must be DNS names or IP addresses.') from None
                names.append(x509.DNSName(name.encode('idna').decode('ascii')))
        private = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ProxHA')])
        now = datetime.now(timezone.utc)
        signed = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(private.public_key())
                  .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                  .not_valid_after(now + timedelta(days=365)).add_extension(x509.SubjectAlternativeName(names), critical=False)
                  .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                  .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                  .sign(private, hashes.SHA256()))
        for path, data in ((key, private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                           (cert, signed.public_bytes(serialization.Encoding.PEM))):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(data)
            path.chmod(0o600)
    elif not cert.is_file() or not key.is_file():
        raise ValueError('The TLS certificate or key is missing. Restore the pair; existing files were not replaced.')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    if not parsed.not_valid_before_utc <= datetime.now(timezone.utc) < parsed.not_valid_after_utc:
        raise ValueError('The TLS certificate is expired or not yet valid. Replace it before starting ProxHA.')
    return cert, key


class Server(uvicorn.Server):
    def __init__(self, config, http_config=None):
        super().__init__(config)
        self.http_config = http_config

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if not self.started or self.http_config is None:
            return
        config = self.http_config
        try:
            config.load()
            loop = asyncio.get_running_loop()

            def create_protocol():
                return config.http_protocol_class(config=config, server_state=self.server_state,
                                                  app_state=self.lifespan.state, _loop=loop)

            listener = await loop.create_server(create_protocol, host=config.host, port=config.port, backlog=config.backlog)
            self.servers.append(listener)
            logging.getLogger('uvicorn.error').info('HTTP also listening on %s:%s', config.host, config.port)
        except Exception:
            await self.shutdown(sockets=sockets)
            self.started = False
            raise


def configured_server(settings, cert=None, key=None):
    common = dict(host=settings.host, workers=1, proxy_headers=False, timeout_graceful_shutdown=30)
    scheme, port = settings.listeners()[0]
    tls = dict(ssl_certfile=str(cert), ssl_keyfile=str(key), ssl_version=ssl.PROTOCOL_TLS_SERVER) if scheme == 'https' else {}
    config = uvicorn.Config('app.main:app', port=port, **common, **tls)
    extra = uvicorn.Config('app.main:app', port=settings.http_port, lifespan='off', **common) if settings.protocol == 'both' else None
    return Server(config, extra)


def main():
    parser = argparse.ArgumentParser(description='Run ProxHA with HTTP/HTTPS and persistent configuration.')
    parser.add_argument('--config-dir', type=Path)
    parser.add_argument('--migrate-from', type=Path, help='Copy an offline database and encryption key into the config directory.')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int, help='Internal HTTPS port (default: 8080).')
    parser.add_argument('--http-port', type=int, help='Internal HTTP port (default: 8081).')
    parser.add_argument('--protocol', choices=['https', 'http', 'both'])
    args = parser.parse_args()
    os.umask(0o077)
    directory = (args.config_dir or runtime.config_dir()).resolve()
    try:
        if args.migrate_from:
            runtime.migrate(args.migrate_from, directory)
        runtime.check_legacy_data(directory)
        settings = runtime.load(directory)
        changes = {key: value for key, value in {'host': args.host, 'port': args.port, 'http_port': args.http_port,
                                               'protocol': args.protocol}.items() if value is not None}
        settings = runtime.Settings.model_validate({**settings.model_dump(), **changes})
        runtime.apply(directory, settings)
        cert, key = certificate(directory, settings.tls) if settings.protocol != 'http' else (None, None)
        configured_server(settings, cert, key).run()
    except ValidationError as exc:
        fields = ', '.join('.'.join(map(str, error['loc'])) for error in exc.errors())
        parser.exit(1, f'Cannot start ProxHA: invalid server settings ({fields}).\n')
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f'Cannot start ProxHA: {exc}\n')


if __name__ == '__main__':
    main()

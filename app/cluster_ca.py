"""Trusted cluster CA: validation, description and TLS contexts."""
import functools
import hashlib
import re
import ssl
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_BYTES = 16 * 1024
ROOT_CA_FILE = 'pve-root-ca.pem'
PEM_LABEL = re.compile(r'-----BEGIN ([A-Z0-9 ]+)-----')


class PemInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pem: str = Field(max_length=MAX_BYTES)

    @field_validator('pem')
    @classmethod
    def valid_pem(cls, value):
        return normalize(value)


def normalize(pem):
    """Canonical PEM of exactly one CA certificate; raises ValueError otherwise."""
    if not isinstance(pem, str) or not pem.strip():
        raise ValueError('Paste the cluster CA certificate in PEM format.')
    if len(pem.encode()) > MAX_BYTES:
        raise ValueError(f'The CA certificate must not exceed {MAX_BYTES // 1024} KiB.')
    labels = PEM_LABEL.findall(pem)
    if any('PRIVATE KEY' in label for label in labels):
        raise ValueError('Paste only the CA certificate, never a private key.')
    if labels != ['CERTIFICATE']:
        raise ValueError('Paste exactly one PEM certificate (-----BEGIN CERTIFICATE-----).')
    try:
        certificate = x509.load_pem_x509_certificate(pem.encode())
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        constraints = None
    except ValueError:
        raise ValueError('The CA certificate could not be parsed.') from None
    if not constraints or not constraints.ca:
        raise ValueError('The certificate is not a CA certificate (basicConstraints CA:TRUE is missing).')
    canonical = certificate.public_bytes(serialization.Encoding.PEM).decode()
    try:
        context(canonical)
    except ssl.SSLError:
        raise ValueError('The CA certificate cannot be used for TLS verification.') from None
    return canonical


def digest(pem):
    certificate = x509.load_pem_x509_certificate(pem.encode())
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


def colon_hex(value):
    return ':'.join(value[i:i + 2] for i in range(0, len(value), 2)).upper()


def confirmed(pem, fingerprint):
    """True when the fingerprint, in any common hex notation, is the SHA-256 of the certificate."""
    if not isinstance(fingerprint, str):
        return False
    value = re.sub(r'[\s:]', '', fingerprint.split('=')[-1]).lower()
    return value == digest(pem)


def describe(pem):
    certificate = x509.load_pem_x509_certificate(pem.encode())
    now = datetime.now(timezone.utc)
    return {'pem': pem, 'subject': certificate.subject.rfc4514_string(), 'issuer': certificate.issuer.rfc4514_string(),
            'not_before': certificate.not_valid_before_utc.isoformat(), 'not_after': certificate.not_valid_after_utc.isoformat(),
            'valid_now': certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc,
            'fingerprint': colon_hex(digest(pem))}


def summary(pem):
    """Public details of a saved CA; the certificate itself is not secret."""
    if not pem:
        return None
    details = describe(pem)
    return {key: details[key] for key in ('subject', 'not_before', 'not_after', 'valid_now', 'fingerprint')}


@functools.lru_cache(maxsize=32)
def context(pem):
    """TLS context that trusts only this CA, with hostname checks enabled."""
    result = ssl.create_default_context(cadata=pem)
    # Disable strict extension requirements; keep chain and hostname verification.
    result.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return result


def from_certificates(certificates):
    """The pve-root-ca.pem entry of nodes/{node}/certificates/info."""
    for entry in certificates if isinstance(certificates, list) else []:
        if isinstance(entry, dict) and entry.get('filename') == ROOT_CA_FILE and isinstance(entry.get('pem'), str):
            return entry
    return None

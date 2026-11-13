"""HTTPS without a domain (--lan): a self-signed certificate for this machine's addresses.

Browsers show a one-time warning for a self-signed certificate. Cloudora prints the certificate's SHA-256 fingerprint
at start-up, so the first visit can be checked against it (the browser's certificate viewer shows the same value);
after that, the browser warns if the certificate ever changes.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import os
import socket
import ssl
from pathlib import Path

from cloudora.config import Config


def local_addresses() -> list[str]:
    """This machine's non-loopback IPv4 addresses, found without sending any packet.

    A UDP connect() to a documentation address only asks the kernel which source address it would use."""
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in dict.fromkeys(found):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_loopback and not addr.is_unspecified and not addr.is_link_local:
            out.append(ip)
    return out


def portal_urls(cfg: Config) -> list[str]:
    scheme = "https" if cfg.tls else "http"
    try:
        unspecified = ipaddress.ip_address(cfg.host).is_unspecified
    except ValueError:
        unspecified = False
    hosts = local_addresses() if unspecified else [cfg.host]
    return [f"{scheme}://{h}:{cfg.port}" for h in hosts]


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def generate_self_signed(cert_path: Path, key_path: Path, ip_addresses: list[str], days: int = 397) -> str:
    """ECDSA P-256 key and a self-signed server certificate. Returns the SHA-256 fingerprint."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in dict.fromkeys(ip_addresses)]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cloudora")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5)).not_valid_after(now + _dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    _write_private(Path(key_path), key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    _write_private(Path(cert_path), cert.public_bytes(serialization.Encoding.PEM))
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def fingerprint(cert_path: Path) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(Path(cert_path).read_text())).hexdigest()


def ensure_certificate(cfg: Config, regenerate: bool = False) -> str:
    """Create the certificate if missing (or if this machine's address changed). Returns its fingerprint."""
    wanted = ["127.0.0.1", *local_addresses()]
    if not regenerate and cfg.cert_path.exists() and cfg.key_path.exists():
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cfg.cert_path.read_bytes())
        have = {str(ip) for ip in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                .value.get_values_for_type(x509.IPAddress)}
        if set(wanted) <= have and cert.not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc):
            return fingerprint(cfg.cert_path)
    return generate_self_signed(cfg.cert_path, cfg.key_path, wanted)


def pretty_fingerprint(fp: str) -> str:
    fp = fp.replace(":", "").upper()
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


def ssl_context(cfg: Config) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3  # every current browser speaks TLS 1.3
    ctx.load_cert_chain(str(cfg.cert_path), str(cfg.key_path))
    return ctx

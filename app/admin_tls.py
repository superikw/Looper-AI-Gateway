"""Native admin HTTPS configuration and local self-signed certificates."""

from __future__ import annotations

import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_certificate(hosts: list[str], directory: Path) -> tuple[Path, Path]:
    names = []
    for host in dict.fromkeys(hosts):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            name = host.encode("idna").decode("ascii")
            if (
                not name
                or len(name) > 253
                or any(
                    not label
                    or len(label) > 63
                    or label.startswith("-")
                    or label.endswith("-")
                    or not all(c.isalnum() or c == "-" for c in label)
                    for label in name.rstrip(".").split(".")
                )
            ):
                raise ValueError(
                    "--host must be an IP address or DNS name without scheme/port"
                )
            names.append(x509.DNSName(name))
    if not names:
        raise ValueError("at least one --host is required")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    if cert_path.exists() or key_path.exists():
        raise FileExistsError(
            "certificate/key already exists; use another output directory"
        )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Looper Admin")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    created = []
    try:
        for path, content, mode in (
            (
                key_path,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
                0o600,
            ),
            (cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644),
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            created.append(path)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return cert_path, key_path


def tls_options(certfile: str, keyfile: str) -> dict:
    if bool(certfile) != bool(keyfile):
        raise ValueError(
            "ADMIN_SSL_CERTFILE and ADMIN_SSL_KEYFILE must be configured together"
        )
    if not certfile:
        return {}
    for path in (certfile, keyfile):
        if not Path(path).is_file():
            raise ValueError(f"admin TLS file not found: {path}")
    return {"ssl_certfile": certfile, "ssl_keyfile": keyfile, "proxy_headers": False}

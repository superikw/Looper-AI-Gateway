import asyncio
import os
import socket
import ssl
import subprocess
import sys

import httpx
import pytest
from cryptography import x509

from app.admin_tls import generate_certificate, tls_options


def test_self_signed_cert_and_configuration(tmp_path):
    cert, key = generate_certificate(["127.0.0.1", "localhost"], tmp_path / "tls")
    names = (
        x509.load_pem_x509_certificate(cert.read_bytes())
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    assert names.get_values_for_type(x509.DNSName) == ["localhost"]
    assert key.stat().st_mode & 0o777 == 0o600
    original = key.read_bytes()
    with pytest.raises(FileExistsError):
        generate_certificate(["127.0.0.1"], cert.parent)
    assert key.read_bytes() == original
    assert tls_options("", "") == {}
    with pytest.raises(ValueError):
        tls_options(str(cert), "")
    assert tls_options(str(cert), str(key))["proxy_headers"] is False


async def test_native_https_without_reverse_proxy(tmp_path):
    cert, key = generate_certificate(["127.0.0.1"], tmp_path / "tls")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(
        os.environ,
        ADMIN_HOST="127.0.0.1",
        ADMIN_PORT=str(port),
        ADMIN_SSL_CERTFILE=str(cert),
        ADMIN_SSL_KEYFILE=str(key),
        ADMIN_REQUIRE_HTTPS="true",
        DATABASE_URL="sqlite+aiosqlite:///" + str(tmp_path / "native.db"),
        CREDENTIAL_KEY_FILE=str(tmp_path / "credential.key"),
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "app.admin_server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        context = ssl.create_default_context(cafile=str(cert))
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            for _ in range(100):
                assert server.poll() is None, "native HTTPS server failed to start"
                try:
                    health = await client.get(f"https://127.0.0.1:{port}/healthz")
                    assert health.status_code == 200
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("native HTTPS server did not start")
            response = await client.get(f"https://127.0.0.1:{port}/api/me")
            assert response.status_code == 401
            assert "strict-transport-security" in response.headers
    finally:
        server.terminate()
        server.wait(timeout=5)

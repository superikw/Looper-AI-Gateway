import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.crypto import cipher, decrypt, encrypt


def test_file_cipher_is_initialized_once_across_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "credential_encryption_key", "")
    monkeypatch.setattr(settings, "credential_key_file", str(tmp_path / "key"))
    with (
        patch("app.crypto.os.open", wraps=os.open) as opens,
        patch("app.crypto.Fernet", wraps=Fernet) as constructors,
    ):
        with ThreadPoolExecutor(max_workers=8) as pool:
            instances = list(pool.map(lambda _: cipher(), range(30)))
        for _ in range(30):
            assert decrypt(encrypt("secret")) == "secret"
        assert all(instance is instances[0] for instance in instances)
        assert opens.call_count == 1
        assert constructors.call_count == 1


def test_environment_cipher_is_cached_without_file_io(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "credential_encryption_key", key)
    with (
        patch(
            "app.crypto.os.open",
            side_effect=AssertionError("unexpected key file access"),
        ),
        patch("app.crypto.Fernet", wraps=Fernet) as constructors,
    ):
        assert cipher() is cipher()
        assert decrypt(encrypt("secret")) == "secret"
        assert constructors.call_count == 1


def test_configuration_changes_do_not_reuse_wrong_cipher(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "credential_encryption_key", "")
    monkeypatch.setattr(settings, "credential_key_file", str(tmp_path / "first.key"))
    first = cipher()
    ciphertext = encrypt("secret")
    monkeypatch.setattr(settings, "credential_key_file", str(tmp_path / "second.key"))
    assert cipher() is not first
    with pytest.raises(ValueError, match="original credential encryption key"):
        decrypt(ciphertext)
    monkeypatch.setattr(
        settings, "credential_encryption_key", Fernet.generate_key().decode()
    )
    assert cipher() is cipher()
    with pytest.raises(ValueError, match="original credential encryption key"):
        decrypt(ciphertext)

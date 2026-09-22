"""Keep shared tests away from the user's live LatticeShadow data."""

import tempfile
import os
from pathlib import Path

import pytest

_scratch = None
_patch = None


def pytest_configure(config):
    global _scratch, _patch
    _scratch = tempfile.TemporaryDirectory(prefix="latticeshadow-tests-")
    _patch = pytest.MonkeyPatch()
    _patch.setenv("LATTICEDB_MASTER_KEY", "temporary-test-key-not-for-production")
    _patch.setenv("LATTICEDB_SERVER_DB_ROOT", str(Path(_scratch.name) / "server"))
    try:
        from latticeshadow import config as client_config
    except ImportError:
        return  # The standalone DB does not need the macOS package.
    _patch.setattr(client_config, "LOG_DIR", _scratch.name)
    _patch.setattr(client_config, "CONFIG_PATH", str(Path(_scratch.name) / "config.toml"))


def pytest_unconfigure(config):
    if _patch:
        _patch.undo()
    if _scratch:
        _scratch.cleanup()


@pytest.fixture(autouse=True)
def isolated_client_keychain(request, monkeypatch):
    """Use an ephemeral key service except in explicitly selected hardware tests."""
    client_tests = Path(__file__).parent / "packages" / "cli" / "tests"
    if not request.node.path.is_relative_to(client_tests) or request.node.get_closest_marker("hardware"):
        return
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from latticeshadow import keychain, security

    stored = {}
    cipher = AESGCM(AESGCM.generate_key(bit_length=256))

    def encrypt(label, plaintext):
        nonce = os.urandom(12)
        return nonce + cipher.encrypt(nonce, plaintext, label.encode())

    monkeypatch.setattr(keychain, "retrieve_key", lambda: stored.get("key"))
    monkeypatch.setattr(keychain, "store_key", lambda value: stored.update(key=value))
    monkeypatch.setattr(keychain, "delete_key", lambda: stored.pop("key", None) is not None)
    monkeypatch.setattr(security, "encrypt_with_secure_enclave", encrypt)
    monkeypatch.setattr(
        security, "decrypt_with_secure_enclave",
        lambda label, data: cipher.decrypt(data[:12], data[12:], label.encode()),
    )

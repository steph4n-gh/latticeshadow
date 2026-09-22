import pytest
import os
from latticeshadow import security

pytestmark = pytest.mark.hardware

def test_memory_shield_ptrace():
    """Verify that process memory shielding executes without crashing."""
    # We may not be able to verify PT_DENY_ATTACH directly without killing the test runner,
    # but we can verify that the function executes and returns a boolean status.
    res = security.shield_process()
    assert isinstance(res, bool)

def test_memory_locking():
    """Verify that mlock/munlock can be called on buffers."""
    buf = bytearray(b"super_secret_test_key_material_12345")
    # Lock
    lock_res = security.lock_buffer(buf)
    assert lock_res is True
    # Unlock
    unlock_res = security.unlock_buffer(buf)
    assert unlock_res is True

def test_secure_enclave_crypto_flow():
    """Verify key generation, ECIES encryption, and decryption."""
    label = "test_run_security_spec"
    
    # Generate the key (will use software Keychain fallback if Enclave token is blocked/unavailable)
    gen_res = security.generate_secure_enclave_key(label)
    assert gen_res is True
    
    plaintext = b"top_secret_database_encryption_key"
    
    # Encrypt
    ciphertext = security.encrypt_with_secure_enclave(label, plaintext)
    assert isinstance(ciphertext, bytes)
    assert len(ciphertext) > 0
    assert ciphertext != plaintext
    
    # Decrypt
    decrypted = security.decrypt_with_secure_enclave(label, ciphertext)
    assert decrypted == plaintext
    
    # Cleanup
    security._delete_key(security.LABEL_PREFIX + label)

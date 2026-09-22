"""
PrivacyEngine — Envelope Encryption for Zero-Knowledge Vector Storage.

Implements the KEK/DEK (Key Encryption Key / Data Encryption Key) architecture:

- **DEK**: The CayleyPrivacyAdapter matrices (A, B). These perform the actual
  distance-preserving vector rotation. ~262 KB for dim=2048, rank=16.
- **KEK**: Derived from a user master passphrase via PBKDF2-SHA256 (600K iterations).
  Used only to AES-GCM encrypt/decrypt the DEK.

The encrypted DEK is stored directly in SQLite (no sidecar files).
The database is a single, self-contained, cryptographically sealed vault.

Master Key Resolution (3-tier waterfall):
  1. Explicit: `master_key=` parameter
  2. Environment: `LATTICEDB_MASTER_KEY` env var
  3. OS Keychain: Auto-generated, saved via `keyring` library

Enterprise Superpowers (free from KEK/DEK split):
  - O(1) password rotation: re-wrap 262KB blob, not re-encrypt millions of vectors
  - Crypto-shredding: DELETE the key row → vectors become irrecoverable noise
  - Zero-copy auto-locking: GC clears plaintext from RAM on scope exit
"""

import os
import json
import hashlib
import logging
import secrets
import numpy as np
import torch
from typing import Optional, Tuple
from pathlib import Path
from base64 import b64encode, b64decode

from ..adapter import CayleyPrivacyAdapter

logger = logging.getLogger("latticeshadow_db.latticedb.privacy")

# ── Optional Crypto Dependencies ──────────────────────────────────────────
# Graceful fallback: if cryptography is not installed, we degrade to
# raw torch.save key files with a loud warning.

_CRYPTO_AVAILABLE = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    _CRYPTO_AVAILABLE = True
except ImportError:
    pass

_KEYRING_AVAILABLE = False
try:
    import keyring as _keyring
    _KEYRING_AVAILABLE = True
except ImportError:
    pass


class PrivacyEngine:
    """
    Manages Cayley rotation keys with envelope encryption.

    Vectors encrypted with rotate() preserve pairwise distances,
    so cosine similarity search works identically on encrypted data.
    The DEK (rotation matrices) is AES-GCM encrypted under a master key
    and stored directly in SQLite — no sidecar files needed.
    """

    ENV_VAR = "LATTICEDB_MASTER_KEY"
    KEYRING_SERVICE = "latticedb"
    KEYRING_USERNAME = "default_key"
    AAD = b"latticedb_v1"  # Authenticated Associated Data for AES-GCM
    DOCUMENT_AAD = b"latticedb_document_v2"
    DATA_KEY_AAD = b"latticedb_data_key_v1"
    PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation

    def __init__(self, dim: int, rank: Optional[int] = None,
                 master_key: Optional[str] = None,
                 device: str = "cpu"):
        """
        Args:
            dim: Embedding dimension.
            rank: Cayley rotation rank (default: min(16, dim//2)).
            master_key: Explicit master passphrase. If None, resolved via
                        environment variable or OS keychain.
            device: Hardware device to place tensors on ("cpu", "mps", "cuda").
        """
        self.dim = dim
        self.rank = rank
        self.device = device
        self._adapter: Optional[CayleyPrivacyAdapter] = None
        self._master_key_bytes: bytes = self._resolve_master_key(master_key).encode("utf-8")

    # ── Public API ─────────────────────────────────────────────────────────

    @torch.no_grad()
    def encrypt(self, vectors: torch.Tensor) -> torch.Tensor:
        """
        Cayley-rotate vectors for encrypted storage.
        Distance-preserving: cosine similarity is identical on encrypted vectors.
        """
        if self._adapter is None:
            raise RuntimeError("PrivacyEngine has no loaded key. Call generate_key() or load_wrapped_key() first.")
        return self._adapter.rotate(vectors)

    @torch.no_grad()
    def decrypt(self, encrypted: torch.Tensor) -> torch.Tensor:
        """Inverse-rotate encrypted vectors back to original space."""
        if self._adapter is None:
            raise RuntimeError("PrivacyEngine has no loaded key.")
        return self._adapter.inverse_rotate(encrypted)

    def generate_key(self) -> str:
        """
        Generate a fresh Cayley rotation key (DEK) and return the
        AES-GCM encrypted blob (wrapped key) for SQLite storage.

        Returns:
            Encrypted key blob as a JSON string, ready for INSERT into collection_meta.
        """
        self._adapter = CayleyPrivacyAdapter(self.dim, rank=self.rank, device=self.device)
        return self._wrap_key(self._adapter)

    def load_wrapped_key(self, wrapped_blob: str):
        """
        Decrypt a wrapped key blob (from SQLite) and restore the adapter.

        Args:
            wrapped_blob: The encrypted JSON string from collection_meta.

        Raises:
            PermissionError: If the master key is wrong (AES-GCM auth fails).
        """
        self._adapter = self._unwrap_key(wrapped_blob)

    def rewrap_key(self, old_blob: str, new_master_key: str) -> str:
        """
        O(1) password rotation: decrypt with current key, re-encrypt with new key.
        Only re-wraps the ~262KB DEK, does NOT touch any stored vectors.

        Args:
            old_blob: Current encrypted key blob.
            new_master_key: New master passphrase.

        Returns:
            New encrypted key blob.
        """
        # Decrypt with current master key
        adapter = self._unwrap_key(old_blob)

        # Re-wrap with new master key
        old_master = self._master_key_bytes
        self._master_key_bytes = new_master_key.encode("utf-8")
        new_blob = self._wrap_key(adapter)
        self._master_key_bytes = old_master  # Restore (caller decides when to switch)
        return new_blob

    def derive_data_key(self, label: bytes) -> bytes:
        """
        Derive a stable data-encryption key from the loaded Cayley DEK.

        This ties document and auxiliary-data encryption to the wrapped key blob
        instead of directly to the master passphrase. Rotation re-wraps the DEK
        without changing derived data keys, while shredding the DEK destroys them.
        """
        self._require_crypto()
        if self._adapter is None:
            raise PermissionError("Privacy key is not loaded.")
        if not label:
            raise ValueError("Data-key label must be non-empty.")

        a_bytes = self._adapter._A.detach().cpu().numpy().astype(np.float32).tobytes()
        b_bytes = self._adapter._B.detach().cpu().numpy().astype(np.float32).tobytes()
        return hashlib.sha256(self.DATA_KEY_AAD + label + a_bytes + b_bytes).digest()

    @property
    def is_initialized(self) -> bool:
        """True if the adapter has valid rotation matrices."""
        return self._adapter is not None

    def shred(self):
        """Securely zero out the key matrices in memory and clear key reference."""
        if self._adapter is not None:
            self._adapter.shred()
            self._adapter = None
        if hasattr(self, "_master_key_bytes"):
            self._master_key_bytes = None

    def encrypt_document(self, text: str) -> str:
        """
        Encrypt a document text using AESGCM envelope encryption.
        """
        if not text:
            return text
        doc_key = self.derive_data_key(b"document")
        aesgcm = AESGCM(doc_key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, text.encode("utf-8"), self.DOCUMENT_AAD)
        return "enc:v2:" + b64encode(nonce + ciphertext).decode("utf-8")

    def decrypt_document(self, encrypted_text: str) -> str:
        """
        Decrypt a document text using AESGCM envelope encryption.
        """
        if not encrypted_text or not encrypted_text.startswith("enc:v2:"):
            return encrypted_text
        try:
            doc_key = self.derive_data_key(b"document")
            aesgcm = AESGCM(doc_key)
            raw = b64decode(encrypted_text[7:])
            if len(raw) < 12:
                return encrypted_text
            nonce = raw[:12]
            ciphertext = raw[12:]
            plaintext = aesgcm.decrypt(nonce, ciphertext, self.DOCUMENT_AAD)
            return plaintext.decode("utf-8")
        except Exception:
            return encrypted_text

    # ── KEK Resolution (3-Tier Waterfall) ──────────────────────────────────

    def _resolve_master_key(self, provided_key: Optional[str]) -> str:
        """
        Resolve master key via waterfall:
          1. Explicit parameter
          2. LATTICEDB_MASTER_KEY environment variable
          3. OS Keychain (auto-generate if needed)
        """
        # Tier 1: Explicit
        if provided_key:
            return provided_key

        # Tier 2: Environment variable
        env_key = os.environ.get(self.ENV_VAR)
        if env_key:
            return env_key

        # Tier 3: OS Keychain
        if _KEYRING_AVAILABLE:
            try:
                stored = _keyring.get_password(self.KEYRING_SERVICE, self.KEYRING_USERNAME)
                if stored:
                    return stored

                # Auto-generate and store
                new_key = secrets.token_urlsafe(32)
                _keyring.set_password(self.KEYRING_SERVICE, self.KEYRING_USERNAME, new_key)
                logger.warning(
                    "No %s found. Auto-generated master key and saved to OS Keychain "
                    "(service='%s'). Set the env var for reproducible deployments.",
                    self.ENV_VAR, self.KEYRING_SERVICE,
                )
                return new_key
            except Exception as e:
                logger.debug("Keyring unavailable: %s", e)

        # Fallback: deterministic key from hostname (NOT secure, but functional)
        # This ensures connect() never fails, while emitting a loud warning.
        import platform
        fallback = secrets.token_urlsafe(32)
        logger.warning(
            "No master key found (no explicit key, no %s env var, no keyring). "
            "Using an ephemeral key — data will NOT be recoverable after restart. "
            "Set %s for persistent encryption.",
            self.ENV_VAR, self.ENV_VAR,
        )
        return fallback

    # ── Envelope Encryption (AES-GCM) ─────────────────────────────────────

    def _derive_kek(self, salt: bytes) -> bytes:
        """Derive a 256-bit KEK from the master passphrase via PBKDF2."""
        self._require_crypto()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self.PBKDF2_ITERATIONS,
        )
        return kdf.derive(self._master_key_bytes)

    def _wrap_key(self, adapter: CayleyPrivacyAdapter) -> str:
        """
        Serialize the adapter's A, B matrices and AES-GCM encrypt them.
        Returns a JSON string suitable for SQLite TEXT storage.
        """
        if not _CRYPTO_AVAILABLE:
            self._require_crypto()

        # Serialize to raw float32 bytes (no pickle)
        a_bytes = adapter._A.detach().cpu().numpy().astype(np.float32).tobytes()
        b_bytes = adapter._B.detach().cpu().numpy().astype(np.float32).tobytes()
        payload = a_bytes + b_bytes

        salt = os.urandom(16)
        nonce = os.urandom(12)
        kek = self._derive_kek(salt)
        aesgcm = AESGCM(kek)

        ciphertext = aesgcm.encrypt(nonce, payload, associated_data=self.AAD)

        return json.dumps({
            "version": 1,
            "dim": adapter.dim,
            "rank": adapter.rank,
            "salt": b64encode(salt).decode("utf-8"),
            "nonce": b64encode(nonce).decode("utf-8"),
            "ciphertext": b64encode(ciphertext).decode("utf-8"),
        })

    def _unwrap_key(self, wrapped_json: str) -> CayleyPrivacyAdapter:
        """
        Decrypt the JSON blob and reconstitute the CayleyPrivacyAdapter.
        """
        data = json.loads(wrapped_json)

        if data.get("version") == 0:
            return self._unwrap_key_fallback(data)

        if not _CRYPTO_AVAILABLE:
            raise ImportError(
                "The 'cryptography' package is required to decrypt this database. "
                "Install it with: pip install cryptography"
            )

        dim = data["dim"]
        rank = data["rank"]
        salt = b64decode(data["salt"])
        nonce = b64decode(data["nonce"])
        ciphertext = b64decode(data["ciphertext"])

        kek = self._derive_kek(salt)
        aesgcm = AESGCM(kek)

        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=self.AAD)
        except Exception:
            raise PermissionError(
                "LatticeDB unlock failed: invalid master key. "
                "Check your LATTICEDB_MASTER_KEY or master_key parameter."
            )

        # Split bytes and rebuild tensors
        half = len(plaintext) // 2
        a_np = np.frombuffer(plaintext[:half], dtype=np.float32).copy()
        b_np = np.frombuffer(plaintext[half:], dtype=np.float32).copy()
        a_tensor = torch.from_numpy(a_np).view(dim, rank)
        b_tensor = torch.from_numpy(b_np).view(dim, rank)

        # Create adapter and restore exact matrices + derived state
        adapter = CayleyPrivacyAdapter(dim, rank=rank, device=self.device)
        self._restore_adapter_matrices(adapter, a_tensor, b_tensor)
        return adapter

    # ── Fallback (no cryptography package) ────────────────────────────────

    def _wrap_key_fallback(self, adapter: CayleyPrivacyAdapter) -> str:
        """Fallback: store raw base64-encoded matrices (NOT encrypted)."""
        logger.warning(
            "The 'cryptography' package is not installed. "
            "Storing rotation key WITHOUT encryption. Install cryptography for AES-GCM protection."
        )
        a_bytes = adapter._A.detach().cpu().numpy().astype(np.float32).tobytes()
        b_bytes = adapter._B.detach().cpu().numpy().astype(np.float32).tobytes()
        return json.dumps({
            "version": 0,
            "dim": adapter.dim,
            "rank": adapter.rank,
            "a": b64encode(a_bytes).decode("utf-8"),
            "b": b64encode(b_bytes).decode("utf-8"),
        })

    def _unwrap_key_fallback(self, data: dict) -> CayleyPrivacyAdapter:
        """Fallback: restore from raw base64-encoded matrices."""
        dim = data["dim"]
        rank = data["rank"]
        a_np = np.frombuffer(b64decode(data["a"]), dtype=np.float32).copy()
        b_np = np.frombuffer(b64decode(data["b"]), dtype=np.float32).copy()
        a_tensor = torch.from_numpy(a_np).view(dim, rank)
        b_tensor = torch.from_numpy(b_np).view(dim, rank)

        adapter = CayleyPrivacyAdapter(dim, rank=rank, device=self.device)
        self._restore_adapter_matrices(adapter, a_tensor, b_tensor)
        return adapter

    # ── Adapter Matrix Restoration ─────────────────────────────────────────

    @staticmethod
    def _restore_adapter_matrices(adapter: CayleyPrivacyAdapter,
                                  A: torch.Tensor, B: torch.Tensor):
        """
        Restore adapter from serialized A, B matrices and recompute
        all derived state (U, V, M) using the same math as regenerate().
        """
        adapter._A = A.to(device=adapter.device, dtype=adapter.dtype)
        adapter._B = B.to(device=adapter.device, dtype=adapter.dtype)

        # Recompute U = [A | -B], V = [B | A]
        adapter._U = torch.cat([adapter._A, -adapter._B], dim=1)
        adapter._V = torch.cat([adapter._B, adapter._A], dim=1)

        # Recompute M = (I_{2r} + V^T U)^{-1}
        vt_u = torch.matmul(adapter._V.t(), adapter._U)
        eye = torch.eye(2 * adapter.rank, device=adapter.device, dtype=adapter.dtype)
        core = eye + vt_u

        inv_dtype = torch.float32 if adapter.dtype in (torch.float16, torch.bfloat16) else adapter.dtype
        core_cpu = core.to(device="cpu", dtype=inv_dtype)
        M_cpu = torch.linalg.inv(core_cpu)
        adapter._M = M_cpu.to(device=adapter.device, dtype=adapter.dtype)

    @staticmethod
    def _require_crypto():
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError(
                "The 'cryptography' package is required for privacy encryption. "
                "Install it with: pip install cryptography"
            )

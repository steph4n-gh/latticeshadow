import sqlite3
import torch
import numpy as np
import logging
import hashlib
import os
from io import BytesIO
from typing import Any, Dict, Optional

from .routing import calculate_activation_entropy

logger = logging.getLogger("latticeshadow_db.cache")

# Attempt FAISS import — graceful fallback to brute-force if unavailable
import sys

_FAISS_AVAILABLE = False
if sys.version_info < (3, 14):
    try:
        import faiss
        _FAISS_AVAILABLE = True
    except Exception:
        pass

if not _FAISS_AVAILABLE:
    logger.debug("FAISS not available. Using brute-force cosine similarity for cache lookups.")

_CRYPTO_AVAILABLE = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_AVAILABLE = True
except ImportError:
    AESGCM = None


class ActivationCache:
    """
    Local Key-Value store mapping input prompt semantic embeddings to aligned cloud hidden states.
    Uses SQLite for persistent storage and either FAISS or brute-force PyTorch for fast lookups.
    Supports entropy-augmented cache keys to prevent false hits.
    """
    ENCRYPTED_BLOB_PREFIX = b"enc1:"
    AAD = b"latticeshadow_activation_cache_v1"

    def __init__(self, db_path: str = "activation_cache.db",
                 entropy_tolerance: float = 0.5,
                 encryption_key: Optional[str] = None,
                 allow_plaintext: bool = False):
        self.db_path = db_path
        self.entropy_tolerance = entropy_tolerance
        self._cache_aead = None
        effective_key = (
            encryption_key
            or os.environ.get("LATTICESHADOW_CACHE_KEY")
            or os.environ.get("LATTICEDB_CACHE_KEY")
        )
        if effective_key:
            if not _CRYPTO_AVAILABLE:
                raise RuntimeError("cryptography is required for encrypted ActivationCache storage.")
            key = hashlib.sha256(
                effective_key.encode("utf-8") + b"latticeshadow_activation_cache"
            ).digest()
            self._cache_aead = AESGCM(key)
        elif not allow_plaintext:
            raise ValueError(
                "ActivationCache requires encryption_key (or LATTICESHADOW_CACHE_KEY) "
                "unless allow_plaintext=True is explicitly set."
            )
        self._init_db()
        self.keys = []
        self.values = []
        self.entropies = []  # Per-entry entropy values for secondary discrimination
        self.prompt_hashes = set()
        self.last_hopfield_status: Dict[str, Any] = {"accepted": False, "reason": "not_run"}
        
        # FAISS index state
        self._use_faiss = False
        self._faiss_index = None
        self._faiss_dim = None
        
        self._load_from_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS activation_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_hash TEXT UNIQUE,
                    local_tensor_blob BLOB,
                    cloud_tensor_blob BLOB,
                    entropy REAL DEFAULT 0.0,
                    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

    def _protect_blob(self, blob: bytes) -> bytes:
        if self._cache_aead is None:
            return blob
        nonce = os.urandom(12)
        ciphertext = self._cache_aead.encrypt(nonce, blob, self.AAD)
        return self.ENCRYPTED_BLOB_PREFIX + nonce + ciphertext

    def _unprotect_blob(self, blob: bytes) -> bytes:
        blob = bytes(blob)
        if not blob.startswith(self.ENCRYPTED_BLOB_PREFIX):
            return blob
        if self._cache_aead is None:
            raise PermissionError("Activation cache row is encrypted; provide an encryption_key.")
        raw = blob[len(self.ENCRYPTED_BLOB_PREFIX):]
        if len(raw) < 12:
            raise PermissionError("Activation cache row is encrypted but malformed.")
        nonce = raw[:12]
        ciphertext = raw[12:]
        try:
            return self._cache_aead.decrypt(nonce, ciphertext, self.AAD)
        except Exception as exc:
            raise PermissionError("Failed to decrypt activation cache row.") from exc

    def _tensor_to_blob(self, tensor: torch.Tensor) -> bytes:
        # Convert to fp16 numpy array and save to bytes
        np_arr = tensor.detach().cpu().numpy().astype(np.float16)
        out = BytesIO()
        np.save(out, np_arr, allow_pickle=False)
        return self._protect_blob(out.getvalue())

    def _blob_to_tensor(self, blob: bytes, device: torch.device) -> torch.Tensor:
        out = BytesIO(self._unprotect_blob(blob))
        np_arr = np.load(out, allow_pickle=False).astype(np.float32)
        return torch.from_numpy(np_arr).to(device)

    def _init_faiss_index(self, dim: int):
        """Initialize a FAISS inner-product index for fast cosine similarity lookups."""
        if not _FAISS_AVAILABLE:
            return
        self._faiss_index = faiss.IndexFlatIP(dim)
        self._faiss_dim = dim
        self._use_faiss = True
        logger.debug("Initialized FAISS IndexFlatIP with dim=%d", dim)

    def _add_to_faiss(self, tensor: torch.Tensor):
        """Add a single L2-normalized vector to the FAISS index."""
        if not self._use_faiss:
            return
        vec = tensor.view(1, -1).float().numpy()
        if vec.shape[1] != self._faiss_dim:
            logger.warning("Dimension mismatch for FAISS insert: %d vs expected %d. Skipping FAISS add.", vec.shape[1], self._faiss_dim)
            return
        # L2 normalize for cosine similarity via inner product
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1e-8, norm)
        vec_normed = vec / norm
        self._faiss_index.add(vec_normed)

    def _load_from_db(self):
        """Loads all keys and values into memory from the database for fast checking."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Try to read entropy column — handle old schema gracefully
            try:
                cursor.execute("SELECT prompt_hash, local_tensor_blob, cloud_tensor_blob, entropy FROM activation_cache")
            except sqlite3.OperationalError:
                cursor.execute("SELECT prompt_hash, local_tensor_blob, cloud_tensor_blob FROM activation_cache")
            rows = cursor.fetchall()

            for row in rows:
                prompt_hash = row[0]
                local_blob = row[1]
                cloud_blob = row[2]
                entropy = row[3] if len(row) > 3 else 0.0
                
                if prompt_hash not in self.prompt_hashes:
                    key_tensor = self._blob_to_tensor(local_blob, torch.device('cpu'))
                    self.keys.append(key_tensor)
                    self.values.append(self._blob_to_tensor(cloud_blob, torch.device('cpu')))
                    self.entropies.append(entropy if entropy is not None else 0.0)
                    self.prompt_hashes.add(prompt_hash)

        # Initialize FAISS index if we have data
        if self.keys and _FAISS_AVAILABLE:
            dim = self.keys[0].view(-1).shape[0]
            self._init_faiss_index(dim)
            for key in self.keys:
                self._add_to_faiss(key.detach().cpu())

        logger.debug("Loaded %d cache entries into memory (FAISS: %s).", len(self.keys), self._use_faiss)

    def check_cache(self, local_tensor: torch.Tensor, threshold: float = 0.95) -> Optional[torch.Tensor]:
        """
        Finds the closest cached activation by cosine similarity, then validates
        with an entropy proximity check. Returns the aligned cloud tensor on hit.
        """
        if not self.keys:
            return None

        query_entropy = calculate_activation_entropy(local_tensor)

        if self._use_faiss and self._faiss_index is not None and self._faiss_index.ntotal > 0:
            return self._check_cache_faiss(local_tensor, threshold, query_entropy)
        else:
            return self._check_cache_bruteforce(local_tensor, threshold, query_entropy)

    def hopfield_readout(
        self,
        local_tensor: torch.Tensor,
        beta: float = 8.0,
        top_k: int = 64,
        min_confidence: float = 0.70,
        max_entropy_ratio: float = 0.65,
        min_similarity: float = 0.0,
    ) -> Optional[torch.Tensor]:
        """
        Soft associative cache readout using a modern-Hopfield/attention update.

        The method computes softmax(beta * cosine(query, keys)) over the top-k
        entropy-compatible cache keys, then returns the weighted sum of cached
        cloud values only when confidence and entropy gates pass.
        """
        self.last_hopfield_status = {"accepted": False, "reason": "not_run"}
        if not self.keys:
            self.last_hopfield_status = {"accepted": False, "reason": "empty_cache"}
            return None
        if top_k <= 0:
            raise ValueError("top_k must be strictly positive.")
        if beta <= 0.0:
            raise ValueError("beta must be strictly positive.")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0.")
        if not 0.0 <= max_entropy_ratio <= 1.0:
            raise ValueError("max_entropy_ratio must be between 0.0 and 1.0.")

        query_entropy = calculate_activation_entropy(local_tensor)
        flat_query = local_tensor.detach().reshape(-1).float()
        candidate_keys = []
        candidate_values = []
        for key, value, entropy in zip(self.keys, self.values, self.entropies):
            flat_key = key.reshape(-1).float()
            flat_value = value.reshape(-1).float()
            if flat_key.shape[0] != flat_query.shape[0] or flat_value.numel() != local_tensor.numel():
                continue
            if abs(query_entropy - entropy) > self.entropy_tolerance:
                continue
            candidate_keys.append(flat_key)
            candidate_values.append(flat_value)

        if not candidate_keys:
            self.last_hopfield_status = {"accepted": False, "reason": "no_entropy_compatible_candidates"}
            return None

        keys = torch.stack(candidate_keys).to(local_tensor.device)
        values = torch.stack(candidate_values).to(local_tensor.device)
        query = flat_query.to(local_tensor.device).unsqueeze(0)
        sims = torch.nn.functional.cosine_similarity(query, keys, dim=1)
        k_eff = min(top_k, sims.numel())
        top_sims, top_indices = torch.topk(sims, k=k_eff, largest=True)
        if top_sims[0].item() < min_similarity:
            self.last_hopfield_status = {
                "accepted": False,
                "reason": "low_similarity",
                "max_similarity": float(top_sims[0].item()),
            }
            return None

        weights = torch.softmax(top_sims * beta, dim=0)
        max_prob = float(weights.max().item())
        entropy = float((-(weights * torch.log(torch.clamp(weights, min=1e-12))).sum()).item())
        entropy_ratio = 0.0 if k_eff <= 1 else entropy / float(np.log(k_eff))

        status = {
            "accepted": False,
            "reason": "low_confidence",
            "max_probability": max_prob,
            "entropy_ratio": entropy_ratio,
            "top_k": k_eff,
            "max_similarity": float(top_sims[0].item()),
            "min_similarity": float(min_similarity),
        }
        if max_prob < min_confidence:
            self.last_hopfield_status = status
            return None
        if entropy_ratio > max_entropy_ratio:
            status["reason"] = "high_entropy"
            self.last_hopfield_status = status
            return None

        selected_values = values[top_indices]
        readout = (selected_values * weights.unsqueeze(1)).sum(dim=0)
        readout = readout.to(device=local_tensor.device, dtype=local_tensor.dtype)
        if readout.numel() == local_tensor.numel():
            readout = readout.reshape(local_tensor.shape)
        status["accepted"] = True
        status["reason"] = "accepted"
        self.last_hopfield_status = status
        return readout

    def _check_cache_faiss(self, local_tensor: torch.Tensor, threshold: float, query_entropy: float) -> Optional[torch.Tensor]:
        """FAISS-accelerated cache lookup with entropy validation."""
        vec = local_tensor.view(1, -1).float().cpu().numpy()
        if vec.shape[1] != self._faiss_dim:
            return None

        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1e-8, norm)
        vec_normed = vec / norm

        distances, indices = self._faiss_index.search(vec_normed, 1)
        sim = float(distances[0][0])
        idx = int(indices[0][0])

        if idx < 0 or sim < threshold:
            return None

        # Entropy proximity check
        cached_entropy = self.entropies[idx]
        if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
            logger.debug(
                "FAISS cosine hit (%.4f) but entropy mismatch: query=%.4f cached=%.4f tolerance=%.4f",
                sim, query_entropy, cached_entropy, self.entropy_tolerance
            )
            return None

        logger.debug("FAISS cache hit! Similarity: %.4f, entropy delta: %.4f", sim, abs(query_entropy - cached_entropy))
        hit_value = self.values[idx].to(device=local_tensor.device, dtype=local_tensor.dtype)
        if hit_value.numel() == local_tensor.numel():
            hit_value = hit_value.view(local_tensor.shape)
        return hit_value

    def _check_cache_bruteforce(self, local_tensor: torch.Tensor, threshold: float, query_entropy: float) -> Optional[torch.Tensor]:
        """Brute-force cosine similarity fallback with entropy validation."""
        flat_local = local_tensor.view(-1).unsqueeze(0)

        try:
            flat_keys = torch.stack([k.view(-1) for k in self.keys]).to(local_tensor.device)
        except Exception as e:
            logger.warning("Failed to stack cache keys: %s", e)
            return None

        # Dimension mismatch guard (different model dims across sessions)
        if flat_local.shape[1] != flat_keys.shape[1]:
            return None

        cos_sim = torch.nn.functional.cosine_similarity(flat_local, flat_keys, dim=1)
        max_sim, max_idx = torch.max(cos_sim, dim=0)

        if max_sim.item() < threshold:
            return None

        # Entropy proximity check
        idx = max_idx.item()
        cached_entropy = self.entropies[idx]
        if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
            logger.debug(
                "Cosine hit (%.4f) but entropy mismatch: query=%.4f cached=%.4f tolerance=%.4f",
                max_sim.item(), query_entropy, cached_entropy, self.entropy_tolerance
            )
            return None

        logger.debug("Cache hit! Similarity: %.4f, entropy delta: %.4f", max_sim.item(), abs(query_entropy - cached_entropy))
        hit_value = self.values[idx].to(device=local_tensor.device, dtype=local_tensor.dtype)
        if hit_value.numel() == local_tensor.numel():
            hit_value = hit_value.view(local_tensor.shape)
        return hit_value

    def insert(self, local_tensor: torch.Tensor, cloud_tensor: torch.Tensor, prompt_hash: str):
        """
        Inserts a new activation mapping into the in-memory cache, FAISS index, and SQLite database.
        """
        if prompt_hash in self.prompt_hashes:
            return

        entropy = calculate_activation_entropy(local_tensor)

        cpu_key = local_tensor.detach().cpu()
        self.keys.append(cpu_key)
        self.values.append(cloud_tensor.detach().cpu())
        self.entropies.append(entropy)
        self.prompt_hashes.add(prompt_hash)

        # Initialize FAISS on first insert if not yet done
        if self._faiss_index is None and _FAISS_AVAILABLE:
            dim = cpu_key.view(-1).shape[0]
            self._init_faiss_index(dim)
        self._add_to_faiss(cpu_key)

        local_blob = self._tensor_to_blob(local_tensor)
        cloud_blob = self._tensor_to_blob(cloud_tensor)

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO activation_cache (prompt_hash, local_tensor_blob, cloud_tensor_blob, entropy)
                    VALUES (?, ?, ?, ?)
                ''', (prompt_hash, local_blob, cloud_blob, entropy))
                conn.commit()
            logger.debug("Inserted prompt_hash %s (entropy: %.4f) into activation cache.", prompt_hash, entropy)
        except sqlite3.IntegrityError:
            logger.debug("Prompt hash %s already exists in cache DB.", prompt_hash)

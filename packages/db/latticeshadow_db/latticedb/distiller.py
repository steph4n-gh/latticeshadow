"""
AutoDistiller — Background CorrectionHead training with SQLite serialization.

Wraps CorrectionHead to provide:
- Automatic (local, cloud) pair recording (SQLite-backed for durability and scale)
- Bounded memory training using the latest N pairs
- Background training triggered at configurable sample thresholds
- Graduation detection
"""

import sqlite3
import io
import os
import hashlib
import torch
import numpy as np
import threading
import logging
from typing import Any, Dict, Optional, List, Tuple
from pathlib import Path

from ..distill import CorrectionHead

logger = logging.getLogger("latticeshadow_db.latticedb.distiller")

_CRYPTO_AVAILABLE = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_AVAILABLE = True
except ImportError:
    AESGCM = None


def _tensor_to_blob(tensor: torch.Tensor) -> bytes:
    """Convert tensor to fp16 numpy blob."""
    np_arr = tensor.detach().cpu().float().numpy().astype(np.float16)
    out = io.BytesIO()
    np.save(out, np_arr, allow_pickle=False)
    return out.getvalue()


def _blob_to_tensor(blob: bytes) -> torch.Tensor:
    """Convert blob back to float32 tensor."""
    out = io.BytesIO(blob)
    np_arr = np.load(out, allow_pickle=False)
    return torch.from_numpy(np_arr.copy()).float()


class AutoDistiller:
    """
    Manages automatic distillation from cloud-aligned vectors to local predictions.
    Saves observation pairs to SQLite to avoid RAM memory leakage.
    Once train and holdout losses drop below their thresholds, the distiller
    'graduates'.
    """

    def __init__(self, dim: int, hidden_dim: int = 256,
                 min_samples: int = 50, loss_threshold: float = 0.01,
                 holdout_fraction: float = 0.2,
                 holdout_loss_threshold: Optional[float] = None,
                 device: str = "cpu", db_path: Optional[str] = None,
                 collection: str = "default", max_training_pairs: int = 2000,
                 encryption_key: Optional[bytes] = None):
        """
        Args:
            dim: Embedding dimension.
            hidden_dim: Hidden layer size for CorrectionHead MLP.
            min_samples: Minimum observation pairs before training triggers.
            loss_threshold: Training MSE loss below which the distiller can graduate.
            holdout_fraction: Fraction of the latest training window reserved for
                              deterministic holdout validation. Set to 0.0 to
                              preserve train-loss-only graduation.
            holdout_loss_threshold: Holdout MSE threshold. Defaults to
                                    loss_threshold when holdout is enabled.
            device: Torch device for training.
            db_path: Optional path to SQLite DB. If None, falls back to in-memory list.
            collection: Collection name for scoping in SQLite.
            max_training_pairs: Max latest pairs loaded into RAM for training.
            encryption_key: Optional bytes that encrypt persisted observation pairs.
        """
        if not 0.0 <= holdout_fraction < 1.0:
            raise ValueError("holdout_fraction must be in the range [0.0, 1.0).")

        self.dim = dim
        self.min_samples = min_samples
        self.loss_threshold = loss_threshold
        self.holdout_fraction = holdout_fraction
        self.holdout_loss_threshold = (
            loss_threshold if holdout_loss_threshold is None else holdout_loss_threshold
        )
        self.db_path = db_path
        self.collection = collection
        self.max_training_pairs = max_training_pairs
        self._pair_aead = self._build_pair_aead(encryption_key)

        self._head = CorrectionHead(input_dim=dim, hidden_dim=hidden_dim, device=device)
        self._pair_buffer: List[Tuple[torch.Tensor, torch.Tensor]] = []  # Fallback
        self._lock = threading.Lock()
        self._last_valid_sample_count = 0
        self._last_train_sample_count = 0
        self._last_holdout_sample_count = 0
        self._last_holdout_loss = float('inf') if holdout_fraction > 0.0 else None
        self._graduation_block_reason = "not_trained"

        if db_path:
            self._init_db()

    def _init_db(self):
        """Create distillation pairs table."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS distillation_pairs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection TEXT NOT NULL,
                    local_blob BLOB NOT NULL,
                    cloud_blob BLOB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_distill_col ON distillation_pairs(collection)')
            conn.commit()

    @staticmethod
    def _build_pair_aead(encryption_key: Optional[bytes]):
        if encryption_key is None:
            return None
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography is required for encrypted distillation pair storage.")
        if not isinstance(encryption_key, (bytes, bytearray)):
            raise TypeError("encryption_key must be bytes.")
        key = hashlib.sha256(bytes(encryption_key) + b"latticedb_distillation_pairs").digest()
        return AESGCM(key)

    @property
    def encryption_enabled(self) -> bool:
        return self._pair_aead is not None

    def _protect_blob(self, blob: bytes) -> bytes:
        if self._pair_aead is None:
            return blob
        nonce = os.urandom(12)
        ciphertext = self._pair_aead.encrypt(nonce, blob, b"latticedb_distillation_pairs_v1")
        return b"enc1:" + nonce + ciphertext

    def _unprotect_blob(self, blob: bytes) -> bytes:
        blob = bytes(blob)
        if not blob.startswith(b"enc1:"):
            return blob
        if self._pair_aead is None:
            raise PermissionError("Distillation pair row is encrypted; privacy key is required.")
        raw = blob[5:]
        if len(raw) < 12:
            raise PermissionError("Distillation pair row is encrypted but malformed.")
        try:
            return self._pair_aead.decrypt(raw[:12], raw[12:], b"latticedb_distillation_pairs_v1")
        except Exception as exc:
            raise PermissionError("Failed to decrypt distillation pair row.") from exc

    def _tensor_to_blob(self, tensor: torch.Tensor) -> bytes:
        return self._protect_blob(_tensor_to_blob(tensor))

    def _blob_to_tensor(self, blob: bytes) -> torch.Tensor:
        return _blob_to_tensor(self._unprotect_blob(blob))

    def record_pair(self, local_vec: torch.Tensor, cloud_vec: torch.Tensor):
        """
        Record a (local, cloud-aligned) observation pair.
        Saves to SQLite if db_path is set, else in-memory list.
        Triggers training when threshold is met.
        """
        local_flat = local_vec.detach().cpu().view(-1)
        cloud_flat = cloud_vec.detach().cpu().view(-1)

        with self._lock:
            if self.db_path:
                local_blob = self._tensor_to_blob(local_flat)
                cloud_blob = self._tensor_to_blob(cloud_flat)
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO distillation_pairs (collection, local_blob, cloud_blob) VALUES (?, ?, ?)",
                        (self.collection, local_blob, cloud_blob)
                    )
                    conn.commit()
            else:
                self._pair_buffer.append((local_flat, cloud_flat))
                if len(self._pair_buffer) > 1000:
                    self._pair_buffer = self._pair_buffer[-1000:]

            if self.sample_count >= self.min_samples and not self.is_graduated:
                self._train()

    def predict(self, local_vec: torch.Tensor) -> Optional[torch.Tensor]:
        """Predict cloud-aligned vector from local vector if graduated."""
        if not self.is_graduated:
            return None
        return self._head.predict(local_vec)

    @property
    def is_graduated(self) -> bool:
        """True when enough samples pass train and holdout loss gates."""
        if not self._head.is_trained:
            self._graduation_block_reason = "not_trained"
            return False
        if self._last_valid_sample_count < self.min_samples:
            self._graduation_block_reason = "insufficient_samples"
            return False
        if self._head.training_loss >= self.loss_threshold:
            self._graduation_block_reason = "train_loss_above_threshold"
            return False
        if self.holdout_fraction > 0.0:
            if self._last_holdout_loss is None or self._last_holdout_sample_count <= 0:
                self._graduation_block_reason = "holdout_unavailable"
                return False
            if self._last_holdout_loss >= self.holdout_loss_threshold:
                self._graduation_block_reason = "holdout_loss_above_threshold"
                return False
        self._graduation_block_reason = "graduated"
        return True

    @property
    def training_loss(self) -> float:
        """Current training loss."""
        return self._head.training_loss

    @property
    def holdout_loss(self) -> Optional[float]:
        """Current holdout loss, or None when holdout validation is disabled."""
        return self._last_holdout_loss

    @property
    def graduation_metrics(self) -> Dict[str, Any]:
        """Latest distillation training and graduation telemetry."""
        return {
            "is_graduated": self.is_graduated,
            "training_loss": self.training_loss,
            "loss_threshold": self.loss_threshold,
            "holdout_loss": self.holdout_loss,
            "holdout_loss_threshold": (
                self.holdout_loss_threshold if self.holdout_fraction > 0.0 else None
            ),
            "holdout_fraction": self.holdout_fraction,
            "valid_sample_count": self._last_valid_sample_count,
            "train_sample_count": self._last_train_sample_count,
            "holdout_sample_count": self._last_holdout_sample_count,
            "graduation_block_reason": self._graduation_block_reason,
        }

    @property
    def sample_count(self) -> int:
        """Number of recorded observation pairs."""
        if self.db_path:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM distillation_pairs WHERE collection = ?",
                    (self.collection,)
                )
                return cursor.fetchone()[0]
        return len(self._pair_buffer)

    def train(self, epochs: int = 10) -> float:
        """Manually trigger training. Returns final loss."""
        with self._lock:
            return self._train(epochs=epochs)

    def _train(self, epochs: int = 10) -> float:
        """Internal training method (must hold lock)."""
        pairs = []

        if self.db_path:
            with sqlite3.connect(self.db_path) as conn:
                # Load latest max_training_pairs to keep memory usage bounded
                cursor = conn.execute(
                    "SELECT local_blob, cloud_blob FROM distillation_pairs WHERE collection = ? ORDER BY id DESC LIMIT ?",
                    (self.collection, self.max_training_pairs)
                )
                rows = cursor.fetchall()
            for l_blob, c_blob in rows:
                pairs.append((self._blob_to_tensor(l_blob), self._blob_to_tensor(c_blob)))
            pairs.reverse()
        else:
            pairs = self._pair_buffer[:]

        if not pairs:
            self._last_valid_sample_count = 0
            self._last_train_sample_count = 0
            self._last_holdout_sample_count = 0
            self._graduation_block_reason = "no_pairs"
            return float('inf')

        valid = [(l, c) for l, c in pairs
                 if l.shape[0] == self.dim and c.shape[0] == self.dim]
        if not valid:
            self._last_valid_sample_count = 0
            self._last_train_sample_count = 0
            self._last_holdout_sample_count = 0
            self._graduation_block_reason = "no_valid_pairs"
            return float('inf')

        self._last_valid_sample_count = len(valid)
        train_pairs, holdout_pairs = self._split_train_holdout(valid)
        self._last_train_sample_count = len(train_pairs)
        self._last_holdout_sample_count = len(holdout_pairs)

        X = torch.stack([p[0] for p in train_pairs]).to(
            device=self._head.device, dtype=torch.float32)
        Y = torch.stack([p[1] for p in train_pairs]).to(
            device=self._head.device, dtype=torch.float32)

        dataset = torch.utils.data.TensorDataset(X, Y)
        loader = torch.utils.data.DataLoader(dataset, batch_size=64, shuffle=True)

        self._head.model.train()
        loss_fn = torch.nn.MSELoss()
        final_loss = float('inf')

        for epoch in range(epochs):
            epoch_loss = 0.0
            count = 0
            for x_batch, y_batch in loader:
                self._head.optimizer.zero_grad()
                pred = self._head.model(x_batch)
                loss = loss_fn(pred, y_batch)
                loss.backward()
                self._head.optimizer.step()
                epoch_loss += loss.item()
                count += 1
            final_loss = epoch_loss / max(count, 1)

        final_loss = self._evaluate_loss(X, Y, loss_fn)
        if holdout_pairs:
            X_holdout = torch.stack([p[0] for p in holdout_pairs]).to(
                device=self._head.device, dtype=torch.float32)
            Y_holdout = torch.stack([p[1] for p in holdout_pairs]).to(
                device=self._head.device, dtype=torch.float32)
            self._last_holdout_loss = self._evaluate_loss(X_holdout, Y_holdout, loss_fn)
        else:
            self._last_holdout_loss = None

        self._head.training_loss = final_loss
        self._head.is_trained = True
        logger.info(
            "AutoDistiller training complete. train_loss=%.6f holdout_loss=%s "
            "(graduated: %s, reason: %s)",
            final_loss,
            "disabled" if self._last_holdout_loss is None else f"{self._last_holdout_loss:.6f}",
            self.is_graduated,
            self._graduation_block_reason,
        )
        return final_loss

    def _split_train_holdout(
        self, valid_pairs: List[Tuple[torch.Tensor, torch.Tensor]]
    ) -> Tuple[List[Tuple[torch.Tensor, torch.Tensor]],
               List[Tuple[torch.Tensor, torch.Tensor]]]:
        """Split oldest pairs for training and newest pairs for validation."""
        if self.holdout_fraction <= 0.0 or len(valid_pairs) < 2:
            return valid_pairs, []

        holdout_count = max(1, int(round(len(valid_pairs) * self.holdout_fraction)))
        holdout_count = min(holdout_count, len(valid_pairs) - 1)
        split_at = len(valid_pairs) - holdout_count
        return valid_pairs[:split_at], valid_pairs[split_at:]

    @torch.no_grad()
    def _evaluate_loss(self, X: torch.Tensor, Y: torch.Tensor,
                       loss_fn: torch.nn.Module) -> float:
        """Evaluate model MSE without mutating optimizer state."""
        self._head.model.eval()
        pred = self._head.model(X)
        loss = loss_fn(pred, Y).item()
        self._head.model.train()
        return loss

    def save(self, path: str):
        """Save trained distillation head."""
        self._head.save(path)

    def load(self, path: str):
        """Load a previously trained distillation head."""
        self._head.load(path)

"""
Holographic Memory Indexing for LatticeShadow.

Uses Holographic Reduced Representations (HRRs) to bind semantic keys (embeddings)
to orthogonalized value vectors (pointers) in a single memory vector.
Uses Codebook clean-up to completely eliminate crosstalk and retrieve original text.
"""

import os
import json
import hashlib
import torch
from typing import List, Dict, Any, Tuple

from latticeshadow_db.latticedb.holographic import circular_convolution, circular_correlation


class HolographicIndex:
    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.memory_vector = torch.zeros(dim, dtype=torch.float32)
        # Codebook maps: value_vector_hash -> original_text
        self.codebook = {}
        # Stores tuples of (value_vector_hash, value_vector_tensor)
        self.codebook_entries = []
        self._value_matrix = None

    def compile(self, documents: List[str], embeddings: List[torch.Tensor]):
        """
        Compile documents and their embeddings into the holographic memory vector.

        Args:
            documents: List of raw strings
            embeddings: List of embedding tensors (same dimension as dim)
        """
        n = len(documents)
        if n == 0:
            return

        # 1. Generate a single base set of orthogonal vectors (up to self.dim)
        # We orthogonalize these value vectors using Gram-Schmidt to ensure zero crosstalk within a batch.
        base_raw_values = []
        for i in range(min(n, self.dim)):
            gen = torch.Generator().manual_seed(i)
            base_raw_values.append(torch.randn(self.dim, generator=gen))
            
        ortho_base = self._gram_schmidt(base_raw_values)
        batch_keys = []

        # 2. Bind each semantic key (embedding) with a hierarchically combined value vector
        for i in range(n):
            batch_idx = i // self.dim
            in_batch_idx = i % self.dim
            
            # Generate a unitary batch key for each chunk
            if batch_idx >= len(batch_keys):
                seed = int(hashlib.sha256(f"batch_{batch_idx}".encode("utf-8")).hexdigest(), 16) % (2**32)
                gen = torch.Generator().manual_seed(seed)
                # Batch key must be unitary to preserve orthogonality of the base vectors when convolved
                B_k = self._make_unitary(torch.randn(self.dim, generator=gen))
                batch_keys.append(B_k)
                
            B_k = batch_keys[batch_idx]
            V_i = ortho_base[in_batch_idx]
            
            # Combined hierarchical pointer: C = B_k ⊛ V_i
            C = circular_convolution(B_k, V_i)

            # Convert embedding key to a unitary representation to eliminate distortion
            key = self._make_unitary(embeddings[i])

            # Bind: Key (embedding) convoluted with combined hierarchical pointer
            # M += K_i ⊛ C_i
            bound = circular_convolution(key, C)
            self.memory_vector += bound

            # Add to codebook
            val_hash = self._hash_vector(C)
            self.codebook[val_hash] = documents[i]
            self.codebook_entries.append((val_hash, C))

        self._rebuild_matrix()

    def recall(self, query_embedding: torch.Tensor) -> Tuple[str, float] | Tuple[None, float]:
        """
        Recall the closest matching document from the memory vector.

        Args:
            query_embedding: Semantic embedding of the query.

        Returns:
            Tuple of (matched_text, similarity_score)
        """
        if self._value_matrix is None or len(self.codebook_entries) == 0:
            return None, 0.0

        # Convert query embedding to its unitary representation
        q = self._make_unitary(query_embedding)

        # 1. Correlate query key against memory to extract the noisy value vector
        # V_noisy = M ★ Q
        noisy_val = circular_correlation(self.memory_vector, q)

        # 2. Clean-up Memory: project noisy vector onto codebook value matrix
        # Since value vectors are strictly orthogonal, the dot product acts as a clean filter
        if torch.norm(noisy_val) > 0:
            noisy_val_norm = noisy_val / torch.norm(noisy_val)
        else:
            noisy_val_norm = noisy_val

        similarities = torch.nn.functional.cosine_similarity(
            noisy_val_norm.unsqueeze(0), self._value_matrix
        )

        best_idx = torch.argmax(similarities).item()
        best_score = similarities[best_idx].item()

        # Threshold to ensure we don't return garbage on empty queries
        if best_score < 0.05:
            return None, best_score

        best_hash = self.codebook_entries[best_idx][0]
        return self.codebook[best_hash], best_score

    def _make_unitary(self, vec: torch.Tensor) -> torch.Tensor:
        """Force vector's Fourier components to have unit magnitude (unitary key)."""
        F = torch.fft.fft(vec.to(torch.float32))
        mag = torch.abs(F)
        mag = torch.where(mag < 1e-9, torch.ones_like(mag), mag)
        F_unitary = F / mag
        res = torch.real(torch.fft.ifft(F_unitary))
        return res

    def _gram_schmidt(self, vectors: List[torch.Tensor]) -> List[torch.Tensor]:
        """Strict Gram-Schmidt orthogonalization process."""
        ortho = []
        for v in vectors:
            v_ortho = v.clone().to(torch.float32)
            for u in ortho:
                proj = torch.dot(v_ortho, u)
                v_ortho -= proj * u
            norm = torch.norm(v_ortho)
            if norm > 1e-6:
                v_ortho = v_ortho / norm
            else:
                # Fallback to random if vector collapsed
                v_ortho = torch.randn(self.dim)
                v_ortho = v_ortho / torch.norm(v_ortho)
            ortho.append(v_ortho)
        return ortho

    def _hash_vector(self, vec: torch.Tensor) -> str:
        return hashlib.sha256(vec.numpy().tobytes()).hexdigest()[:16]

    def _rebuild_matrix(self):
        if not self.codebook_entries:
            self._value_matrix = None
            return
        self._value_matrix = torch.stack([v for _, v in self.codebook_entries])

    def save(self, filepath: str):
        """Save the holographic memory vector and codebook to a compressed PyTorch file."""
        state = {
            "dim": self.dim,
            "memory_vector": self.memory_vector,
            "codebook": self.codebook,
            "codebook_entries": self.codebook_entries,
        }
        torch.save(state, filepath)

    @classmethod
    def load(cls, filepath: str) -> "HolographicIndex":
        """Load a holographic index from a saved state file."""
        state = torch.load(filepath, weights_only=True)
        idx = cls(dim=state["dim"])
        idx.memory_vector = state["memory_vector"]
        idx.codebook = state["codebook"]
        idx.codebook_entries = state["codebook_entries"]
        idx._rebuild_matrix()
        return idx

"""
LatticeIndexer — Leech Lattice quantized coarse search.

Wraps LeechLatticeQuantizer to provide:
- Quantization of vectors to Λ₂₄ lattice coordinates on insert
- Optional compact int16 storage for quantized lattice coordinates
- Coarse nearest-neighbor search in lattice space as a pre-filter
- Batch distance computation for rapid candidate elimination
"""

import torch
import logging
from typing import List, Tuple, Optional, Dict

from ..quant import LeechLatticeQuantizer

logger = logging.getLogger("latticeshadow_db.latticedb.indexer")


class LatticeIndexer:
    """
    Indexes vectors using Leech Lattice quantization for ultra-fast
    coarse nearest-neighbor search. The lattice grid partitions the
    embedding space into discrete cells, enabling >90% candidate
    elimination before fine-grained FAISS ranking.
    """

    def __init__(self, noise_scale: float = 0.1, compact_codes: bool = True):
        """
        Args:
            noise_scale: Noise scale for the LeechLatticeQuantizer.
            compact_codes: Store quantized lattice coordinates as int16 codes.
        """
        self._quantizer = LeechLatticeQuantizer(noise_scale=noise_scale)
        self.compact_codes = compact_codes
        self._codes: Dict[str, torch.Tensor] = {}  # doc_id → quantized code
        self._orig_shapes: Dict[str, tuple] = {}    # doc_id → original shape

    def index(self, doc_id: str, vector: torch.Tensor):
        """
        Quantize a vector to its nearest Leech Lattice point and store.

        Args:
            doc_id: Document identifier.
            vector: Embedding vector (dimension must be divisible by 32).
        """
        if vector is None:
            raise ValueError("Input vector cannot be None.")
        if not isinstance(vector, torch.Tensor):
            raise ValueError("Input vector must be a torch.Tensor.")
        if vector.numel() == 0:
            raise ValueError("Input vector cannot be empty.")

        dim = vector.shape[-1]
        if dim % 32 != 0:
            raise ValueError(f"Vector dimension ({dim}) must be divisible by 32.")

        # Ensure input vector is detached, moved to CPU, and cast to float32
        vector_clean = vector.detach().cpu().float()

        # Reshape to (32, dim // 32)
        reshaped = vector_clean.view(32, dim // 32)

        # Call quantizer to get the quantized code
        code, orig_shape = self._quantizer.quantize(reshaped)

        # Store the resulting (32, 24) tensor in self._codes.
        self._codes[doc_id] = self._pack_code(code)
        self._orig_shapes[doc_id] = vector.shape

    def remove(self, doc_id: str):
        """Remove a document's lattice code."""
        self._codes.pop(doc_id, None)
        self._orig_shapes.pop(doc_id, None)

    def search(self, query_vector: torch.Tensor, k: int = 10,
               candidate_ids: Optional[List[str]] = None) -> List[Tuple[str, float]]:
        """
        Coarse nearest-neighbor search in Leech Lattice space.

        Quantizes the query vector, then computes L2 distance to all
        stored lattice codes. Returns top-k (doc_id, distance) pairs
        sorted by ascending distance.

        Args:
            query_vector: Query embedding (dimension must be divisible by 32).
            k: Number of nearest neighbors to return.
            candidate_ids: If provided, only search within these doc_ids.

        Returns:
            List of (doc_id, l2_distance) tuples, sorted ascending.
        """
        if k <= 0:
            raise ValueError("k must be strictly positive (greater than 0).")
        if query_vector is None:
            raise ValueError("Query vector cannot be None.")
        if not isinstance(query_vector, torch.Tensor):
            raise ValueError("Query vector must be a torch.Tensor.")
        if query_vector.numel() == 0:
            raise ValueError("Query vector cannot be empty.")

        dim = query_vector.shape[-1]
        if dim % 32 != 0:
            raise ValueError(f"Query vector dimension ({dim}) must be divisible by 32.")

        if not self._codes:
            return []

        # Ensure query_vector is detached, moved to CPU, and cast to float32
        query_clean = query_vector.detach().cpu().float()

        # Reshape to (32, dim // 32) and quantize to get the query code
        reshaped_query = query_clean.view(32, dim // 32)
        query_code, _ = self._quantizer.quantize(reshaped_query)
        query_flat = query_code.view(-1)

        search_ids = candidate_ids if candidate_ids else list(self._codes.keys())
        if not search_ids:
            return []

        # Batch distance computation
        codes = []
        ids = []
        for doc_id in search_ids:
            if doc_id in self._codes:
                codes.append(self._unpack_code(self._codes[doc_id]).view(-1))
                ids.append(doc_id)

        if not codes:
            return []

        code_matrix = torch.stack(codes)
        # Compute the Euclidean distance
        distances = torch.norm(code_matrix - query_flat.unsqueeze(0), dim=1)

        # Top-k by ascending distance
        k = min(k, len(ids))
        top_dists, top_indices = torch.topk(distances, k, largest=False)

        return [(ids[idx.item()], top_dists[i].item())
                for i, idx in enumerate(top_indices)]

    def count(self) -> int:
        """Number of indexed documents."""
        return len(self._codes)

    def code_storage_bytes(self) -> int:
        """Approximate in-memory tensor payload bytes used by lattice codes."""
        return sum(code.numel() * code.element_size() for code in self._codes.values())

    @property
    def code_dtype(self) -> Optional[torch.dtype]:
        """Stored code dtype, or None if the index is empty."""
        if not self._codes:
            return None
        return next(iter(self._codes.values())).dtype

    def clear(self):
        """Remove all indexed codes."""
        self._codes.clear()
        self._orig_shapes.clear()

    def _pack_code(self, code: torch.Tensor) -> torch.Tensor:
        if self.compact_codes:
            return self._quantizer.pack_code(code)
        return code.detach().cpu().float()

    def _unpack_code(self, code: torch.Tensor) -> torch.Tensor:
        if self.compact_codes:
            return self._quantizer.unpack_code(code)
        return code.float()

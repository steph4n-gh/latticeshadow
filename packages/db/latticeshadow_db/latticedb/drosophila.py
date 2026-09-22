import os
import numpy as np
import torch
from typing import Union

# 256-element popcount lookup table for quick popcounts in NumPy
POPCOUNT_TABLE = np.array([bin(i).count('1') for i in range(256)], dtype=np.uint8)


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


MAX_FLYHASH_INPUT_DIM = _int_env("LATTICEDB_MAX_FLYHASH_INPUT_DIM", 4096)
MAX_FLYHASH_OUTPUT_DIM = _int_env("LATTICEDB_MAX_FLYHASH_OUTPUT_DIM", 10000)
MAX_FLYHASH_MATRIX_ELEMENTS = _int_env("LATTICEDB_MAX_FLYHASH_MATRIX_ELEMENTS", 50_000_000)

class DrosophilaHasher:
    """
    Drosophila Hashing (FlyHash) implementation for projecting continuous vectors
    to sparse, high-dimensional binary codes, representing the biological olfactory system.
    """
    def __init__(self, input_dim: int, output_dim: int = 10000, seed: int = 42, sparsity: int = 6):
        """
        Args:
            input_dim: Input dimension (D).
            output_dim: Output projection dimension (typically 10,000).
            seed: Fixed seed for deterministic generation.
            sparsity: Number of non-zero connections per column.
        """
        input_dim = int(input_dim)
        output_dim = int(output_dim)
        sparsity = int(sparsity)
        if input_dim <= 0 or output_dim <= 0:
            raise ValueError("input_dim and output_dim must be positive.")
        if input_dim > MAX_FLYHASH_INPUT_DIM:
            raise ValueError(
                f"FlyHash input_dim {input_dim} exceeds configured limit {MAX_FLYHASH_INPUT_DIM}."
            )
        if output_dim > MAX_FLYHASH_OUTPUT_DIM:
            raise ValueError(
                f"FlyHash output_dim {output_dim} exceeds configured limit {MAX_FLYHASH_OUTPUT_DIM}."
            )
        if input_dim * output_dim > MAX_FLYHASH_MATRIX_ELEMENTS:
            raise ValueError(
                f"FlyHash projection matrix would allocate {input_dim * output_dim} elements; "
                f"the configured limit is {MAX_FLYHASH_MATRIX_ELEMENTS}."
            )
        if sparsity <= 0:
            raise ValueError("sparsity must be positive.")

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.seed = seed
        self.sparsity = min(sparsity, self.output_dim)
        
        # Deterministic generation using a fixed-seed generator
        rng = np.random.default_rng(seed=self.seed)
        self.projection_matrix = np.zeros((self.output_dim, self.input_dim), dtype=np.float32)
        
        # Ensure sparsity (exactly 6 non-zero standard normal values per column)
        # Note: if input_dim is larger than output_dim (unlikely), rng.choice would need replace=True.
        # But output_dim is typically 10,000, which is much larger than D.
        for j in range(self.input_dim):
            indices = rng.choice(self.output_dim, size=min(self.sparsity, self.output_dim), replace=False)
            values = rng.standard_normal(size=len(indices))
            self.projection_matrix[indices, j] = values

    def project(self, x: np.ndarray) -> np.ndarray:
        """
        Project continuous input vector(s) to higher-dimensional space.
        """
        x_clean = np.nan_to_num(x, nan=0.0, posinf=1.0e9, neginf=-1.0e9)
        
        if x_clean.ndim == 1:
            if x_clean.shape[0] != self.input_dim:
                raise ValueError(f"Input dimension {x_clean.shape[0]} does not match expected {self.input_dim}")
            if np.max(x_clean) == np.min(x_clean):
                return np.zeros(self.output_dim, dtype=np.float32)
            return np.dot(self.projection_matrix, x_clean)
        elif x_clean.ndim == 2:
            if x_clean.shape[1] != self.input_dim:
                raise ValueError(f"Input dimension {x_clean.shape[1]} does not match expected {self.input_dim}")
            out = np.zeros((x_clean.shape[0], self.output_dim), dtype=np.float32)
            for i in range(x_clean.shape[0]):
                if np.max(x_clean[i]) != np.min(x_clean[i]):
                    out[i] = np.dot(self.projection_matrix, x_clean[i])
            return out
        else:
            raise ValueError("Input must be 1D or 2D array")

    def wta_mask(self, projected: np.ndarray) -> np.ndarray:
        """
        Apply a Winner-Take-All (WTA) step mask: top 5% values set to 1, all others to 0.
        Handles ties stably.
        """
        k_top = max(1, int(self.output_dim * 0.05))
        
        if projected.ndim == 1:
            if np.max(projected) == np.min(projected):
                return np.zeros_like(projected, dtype=np.uint8)
            # Stable sort to handle ties
            sorted_indices = np.argsort(projected, kind='stable')
            top_indices = sorted_indices[-k_top:]
            mask = np.zeros_like(projected, dtype=np.uint8)
            mask[top_indices] = 1
            return mask
        elif projected.ndim == 2:
            mask = np.zeros_like(projected, dtype=np.uint8)
            for i in range(projected.shape[0]):
                if np.max(projected[i]) == np.min(projected[i]):
                    mask[i] = 0
                else:
                    sorted_indices = np.argsort(projected[i], kind='stable')
                    top_indices = sorted_indices[-k_top:]
                    mask[i, top_indices] = 1
            return mask
        else:
            raise ValueError("Projected array must be 1D or 2D")

    def pack(self, mask: np.ndarray) -> np.ndarray:
        """
        Pack the binary values into bytes using np.packbits.
        """
        return np.packbits(mask, axis=-1)

    def hash(self, x: Union[np.ndarray, torch.Tensor]) -> np.ndarray:
        """
        Hash continuous input vector(s) into packed binary representation.
        """
        if isinstance(x, torch.Tensor):
            x_np = x.detach().cpu().numpy()
        else:
            x_np = np.asarray(x)
            
        proj = self.project(x_np)
        mask = self.wta_mask(proj)
        packed = self.pack(mask)
        return packed

def compute_hamming_distance(
    stored: Union[np.ndarray, torch.Tensor],
    query: Union[np.ndarray, torch.Tensor]
) -> Union[np.ndarray, torch.Tensor]:
    """
    Compute binary Hamming distance using highly optimized, vectorized operations.
    Leverages PyTorch's native SIMD/GPU optimized bitwise popcount if available/applicable,
    or falls back to an optimized parallel bitwise count in NumPy to avoid expensive
    advanced-indexing allocations (e.g. POPCOUNT_TABLE[xor_result]).
    
    stored: shape (N, 1250) or (1250,)
    query: shape (1250,) or (N, 1250)
    """
    is_numpy = isinstance(stored, np.ndarray) or isinstance(query, np.ndarray)

    # 1. Convert to PyTorch tensors (with zero-copy torch.from_numpy if they are numpy arrays)
    stored_t = torch.from_numpy(stored) if isinstance(stored, np.ndarray) else stored
    query_t = torch.from_numpy(query) if isinstance(query, np.ndarray) else query

    # Ensure consistent device (e.g. keep on CPU or push to CUDA if query is on GPU)
    device = query_t.device
    stored_t = stored_t.to(device)

    # 2. Vectorized bitwise XOR
    xor_result = torch.bitwise_xor(stored_t, query_t)

    # 3. Vectorized popcount
    if hasattr(torch, "bitwise_count"):
        # Native PyTorch 2.0+ SIMD/CUDA implementation
        dist_t = torch.bitwise_count(xor_result).sum(dim=-1)
    else:
        # High-performance parallel bitwise arithmetic popcount fallback for older PyTorch versions.
        # This completely avoids memory-bound index lookups.
        c = xor_result.to(torch.int32)
        c = (c & 0x55) + ((c >> 1) & 0x55)
        c = (c & 0x33) + ((c >> 2) & 0x33)
        c = (c & 0x0F) + ((c >> 4) & 0x0F)
        dist_t = c.sum(dim=-1)

    # 4. Return matching format (NumPy array or PyTorch Tensor)
    return dist_t.cpu().numpy() if is_numpy else dist_t

import os
import torch
from typing import Dict, Optional, Tuple
from latticeshadow_db.latticedb.embedder import _default_hash_embedding


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


MAX_HOLOGRAPHIC_DIM = _int_env("LATTICEDB_MAX_HOLOGRAPHIC_DIM", 8192)
MAX_HOLOGRAPHIC_ELEMENTS = _int_env("LATTICEDB_MAX_HOLOGRAPHIC_ELEMENTS", 1_048_576)


def validate_holographic_dimension(dim: int) -> None:
    if dim <= 0:
        raise ValueError("Holographic vectors must have a positive dimension.")
    if dim > MAX_HOLOGRAPHIC_DIM:
        raise ValueError(
            f"Holographic dimension {dim} exceeds configured limit {MAX_HOLOGRAPHIC_DIM}."
        )


def _validate_holographic_tensor(name: str, tensor: torch.Tensor) -> None:
    if not isinstance(tensor, torch.Tensor):
        raise ValueError(f"{name} must be a torch.Tensor.")
    if tensor.ndim == 0:
        raise ValueError(f"{name} must have at least one dimension.")
    validate_holographic_dimension(int(tensor.shape[-1]))
    if tensor.numel() > MAX_HOLOGRAPHIC_ELEMENTS:
        raise ValueError(
            f"{name} contains {tensor.numel()} elements; "
            f"the configured limit is {MAX_HOLOGRAPHIC_ELEMENTS}."
        )


def circular_convolution(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    """
    Computes circular convolution using torch FFT:
    C_i = FFT^-1(FFT(a) * FFT(b))
    
    Handles float16/bfloat16 safely by casting to float32 internally
    and casting the output back to the original dtype.
    If inputs are complex, returns complex tensor, otherwise returns real part.
    """
    _validate_holographic_tensor("a", a)
    _validate_holographic_tensor("b", b)
    if a.shape[-1] != b.shape[-1]:
        raise ValueError("Inputs to circular_convolution must have matching final dimensions.")
    if torch.isnan(a).any() or torch.isinf(a).any() or torch.isnan(b).any() or torch.isinf(b).any():
        raise ValueError("Inputs to circular_convolution must not contain NaN or Inf values.")

    orig_dtype = a.dtype
    is_half = orig_dtype in (torch.float16, torch.bfloat16)
    
    if is_half:
        a_cast = a.to(torch.float32)
        b_cast = b.to(torch.float32)
    else:
        a_cast = a
        b_cast = b
        
    is_complex = torch.is_complex(a) or torch.is_complex(b)
    
    # PyTorch MPS often has spotty support for complex FFTs.
    # To be safe, if inputs are on MPS, move to CPU for the math, then back.
    device = a_cast.device
    if device.type == "mps" or b_cast.device.type == "mps":
        a_cast = a_cast.cpu()
        b_cast = b_cast.cpu()
    
    b_cast = b_cast.to(a_cast.device)

    fft_a = torch.fft.fft(a_cast, dim=-1)
    fft_b = torch.fft.fft(b_cast, dim=-1)
    
    fft_conv = fft_a * fft_b
    res = torch.fft.ifft(fft_conv, dim=-1).to(device)
    
    if not is_complex:
        res = torch.real(res)
        if is_half:
            res = res.to(orig_dtype)
    else:
        if is_half:
            res = res.to(torch.complex64)
            
    return res

def circular_correlation(c: torch.Tensor, a: torch.Tensor) -> torch.Tensor:
    """
    Computes circular correlation using torch FFT:
    V_i = FFT^-1(FFT(c) * conj(FFT(a)))
    
    Handles float16/bfloat16 safely by casting to float32 internally
    and casting the output back to the original dtype.
    If inputs are complex, returns complex tensor, otherwise returns real part.
    """
    _validate_holographic_tensor("c", c)
    _validate_holographic_tensor("a", a)
    if c.shape[-1] != a.shape[-1]:
        raise ValueError("Inputs to circular_correlation must have matching final dimensions.")
    if torch.isnan(c).any() or torch.isinf(c).any() or torch.isnan(a).any() or torch.isinf(a).any():
        raise ValueError("Inputs to circular_correlation must not contain NaN or Inf values.")

    orig_dtype = c.dtype
    is_half = orig_dtype in (torch.float16, torch.bfloat16)
    
    if is_half:
        c_cast = c.to(torch.float32)
        a_cast = a.to(torch.float32)
    else:
        c_cast = c
        a_cast = a
        
    is_complex = torch.is_complex(c) or torch.is_complex(a)
    
    # Fallback to CPU for FFT if on MPS
    device = c_cast.device
    if device.type == "mps" or a_cast.device.type == "mps":
        c_cast = c_cast.cpu()
        a_cast = a_cast.cpu()
        
    a_cast = a_cast.to(c_cast.device)
    
    fft_c = torch.fft.fft(c_cast, dim=-1)
    fft_a = torch.fft.fft(a_cast, dim=-1)
    
    fft_corr = fft_c * torch.conj(fft_a)
    res = torch.fft.ifft(fft_corr, dim=-1).to(device)
    
    if not is_complex:
        res = torch.real(res)
        if is_half:
            res = res.to(orig_dtype)
    else:
        if is_half:
            res = res.to(torch.complex64)
            
    return res

def generate_key_vector(doc_id: str, dim: int) -> torch.Tensor:
    """
    Deterministically returns a unit-normalized vector for a given doc_id.
    """
    validate_holographic_dimension(int(dim))
    return _default_hash_embedding(doc_id, dim=dim)


def cleanup_to_codebook(
    query: torch.Tensor,
    codebook: Dict[str, torch.Tensor],
    min_similarity: float = 0.0,
) -> Optional[Tuple[str, torch.Tensor, float]]:
    """
    Clean up a noisy HRR/VSA vector by snapping it to the nearest codebook item.

    Returns (label, vector, cosine_similarity) when the nearest item passes
    min_similarity, otherwise None.
    """
    if query is None or not isinstance(query, torch.Tensor):
        raise ValueError("query must be a torch.Tensor.")
    if torch.isnan(query).any() or torch.isinf(query).any():
        raise ValueError("query must not contain NaN or Inf values.")
    if not codebook:
        return None

    flat_query = query.detach().reshape(-1).float()
    q_norm = torch.linalg.norm(flat_query)
    if q_norm.item() <= 1e-8:
        return None
    q_unit = flat_query / q_norm

    best_label = None
    best_vector = None
    best_similarity = -float("inf")
    for label, vector in codebook.items():
        if not isinstance(vector, torch.Tensor):
            continue
        flat_vector = vector.detach().reshape(-1).float()
        if flat_vector.shape[0] != q_unit.shape[0]:
            continue
        v_norm = torch.linalg.norm(flat_vector)
        if v_norm.item() <= 1e-8:
            continue
        similarity = torch.dot(q_unit, flat_vector / v_norm).item()
        if similarity > best_similarity:
            best_label = label
            best_vector = vector
            best_similarity = similarity

    if best_label is None or best_similarity < min_similarity:
        return None
    return best_label, best_vector, float(best_similarity)

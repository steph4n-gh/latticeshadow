import torch

def poincare_project(vector: torch.Tensor) -> torch.Tensor:
    """
    Projects a vector to the open Poincaré unit ball:
    x_proj = tanh(||x||) * x / ||x||, with norm clamped to a maximum of 1 - 1e-5.
    If norm is 0, return zero vector.
    Supports both 1D and 2D/batch tensors using dim=-1 and keepdim=True.
    """
    if torch.isnan(vector).any():
        raise ValueError("Input vector contains NaN values.")

    # Compute L2 norm along the last dimension
    norm = torch.norm(vector, p=2, dim=-1, keepdim=True)
    
    # Avoid division by zero by using a small epsilon where norm is 0
    eps = 1e-15
    safe_norm = torch.where(norm > 0, norm, torch.ones_like(norm) * eps)
    
    # tanh(||x||) clamped to a maximum of 1 - 1e-5
    target_norm = torch.clamp(torch.tanh(norm), max=1.0 - 1e-5)
    
    # Project the vector
    x_proj = target_norm * (vector / safe_norm)
    
    # Force strictly zero vector where the original norm was zero
    x_proj = torch.where(norm > 0, x_proj, torch.zeros_like(vector))
    
    return x_proj

def poincare_distance(u: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    """
    Computes the Poincaré distance between two unit ball vectors:
    d(u, v) = arcosh(1 + 2 * ||u - v||^2 / ((1 - ||u||^2) * (1 - ||v||^2)))
    where arcosh(z) = ln(z + sqrt(z^2 - 1)).
    Clamps the denominator to min=1e-15, and clamps z to min=1.0 + 1e-15.
    Raises ValueError on dimension mismatch.
    """
    if u.ndim not in (1, 2) or v.ndim not in (1, 2):
        raise ValueError("Inputs u and v must be 1D or 2D tensors.")
        
    if u.shape[-1] != v.shape[-1]:
        raise ValueError(f"Dimension mismatch: u.shape[-1] ({u.shape[-1]}) must equal v.shape[-1] ({v.shape[-1]}).")
        
    if u.ndim == 2 and v.ndim == 2 and u.shape[0] != v.shape[0]:
        raise ValueError(f"Batch dimension mismatch: u.shape[0] ({u.shape[0]}) != v.shape[0] ({v.shape[0]}).")

    # Compute norms squared
    norm_u_sq = torch.sum(u**2, dim=-1)
    norm_v_sq = torch.sum(v**2, dim=-1)
    
    if (norm_u_sq >= 1.0).any() or (norm_v_sq >= 1.0).any():
        raise ValueError("Poincaré distance is undefined for vectors with norm >= 1.0.")

    # Compute diff norm squared
    diff_norm_sq = torch.sum((u - v)**2, dim=-1)
    
    # Denominator: (1 - ||u||^2) * (1 - ||v||^2)
    denom = (1.0 - norm_u_sq) * (1.0 - norm_v_sq)
    denom = torch.clamp(denom, min=1e-15)
    
    # z = 1 + 2 * ||u - v||^2 / denominator
    z = 1.0 + 2.0 * diff_norm_sq / denom
    z = torch.clamp(z, min=1.0 + 1e-15)
    
    # arcosh(z) = ln(z + sqrt(z^2 - 1))
    distance = torch.log(z + torch.sqrt(z**2 - 1.0))
    
    return distance

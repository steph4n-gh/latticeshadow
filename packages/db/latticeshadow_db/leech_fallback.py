import torch
import numpy as np

# Parity matrix B for the [24, 12, 8] extended binary Golay code G_24.
B = np.array([
    [1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1],
    [1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0],
    [0, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 1],
    [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1],
    [1, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 1],
    [1, 1, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0],
    [0, 1, 1, 1, 0, 1, 1, 1, 0, 1, 0, 0],
    [0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 1, 0],
    [0, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 1],
    [1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0],
    [0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0]
], dtype=int)

def generate_golay_codewords():
    codewords = np.zeros((4096, 24), dtype=int)
    for u in range(4096):
        # First 12 bits
        u_bits = np.array([(u >> i) & 1 for i in range(12)], dtype=int)
        codewords[u, :12] = u_bits
        # Last 12 bits are u * B mod 2
        codewords[u, 12:] = (u_bits @ B) % 2
    return codewords

G_codewords = generate_golay_codewords()

def decode_single_vector(y_arr):
    best_dist_sq = 1e30
    best_v = None

    v_cand = np.zeros(24)

    for u in range(4096):
        g = G_codewords[u]

        # Case A: Even coordinates
        # g[i] == 0 => v_cand[i] = 4 * round(yi / 4)
        # g[i] == 1 => v_cand[i] = 4 * round((yi - 2)/4) + 2
        mask_0 = (g == 0)
        mask_1 = ~mask_0
        
        v_cand[mask_0] = 4.0 * np.round(y_arr[mask_0] / 4.0)
        v_cand[mask_1] = 4.0 * np.round((y_arr[mask_1] - 2.0) / 4.0) + 2.0

        sum_v = int(np.round(np.sum(v_cand)))
        if (sum_v % 8) == 4:
            # Parity correction: find index i maximizing |yi - v_cand,i|
            err = np.abs(y_arr - v_cand)
            best_idx = np.argmax(err)
            diff = y_arr[best_idx] - v_cand[best_idx]
            sign = 1.0 if diff >= 0.0 else -1.0
            v_cand[best_idx] += 4.0 * sign

        dist_sq = np.sum((y_arr - v_cand) ** 2)
        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_v = v_cand.copy()

        # Case B: Odd coordinates
        # g[i] == 0 => v_cand[i] = 4 * round((yi - 1)/4) + 1
        # g[i] == 1 => v_cand[i] = 4 * round((yi - 3)/4) + 3
        v_cand[mask_0] = 4.0 * np.round((y_arr[mask_0] - 1.0) / 4.0) + 1.0
        v_cand[mask_1] = 4.0 * np.round((y_arr[mask_1] - 3.0) / 4.0) + 3.0

        sum_v = int(np.round(np.sum(v_cand)))
        if (sum_v % 8) == 0:
            err = np.abs(y_arr - v_cand)
            best_idx = np.argmax(err)
            diff = y_arr[best_idx] - v_cand[best_idx]
            sign = 1.0 if diff >= 0.0 else -1.0
            v_cand[best_idx] += 4.0 * sign

        dist_sq = np.sum((y_arr - v_cand) ** 2)
        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_v = v_cand.copy()

    return best_v

_G_codewords_cache = {}

def decode_leech_py(y: torch.Tensor) -> torch.Tensor:
    """Vectorized PyTorch implementation of Leech Lattice decoding."""
    device = y.device
    dtype = y.dtype
    N = y.shape[0]
    
    # Cache key based on device and dtype
    key = (device, dtype)
    if key not in _G_codewords_cache:
        _G_codewords_cache[key] = torch.from_numpy(G_codewords).to(device=device, dtype=dtype)
    G_tensor = _G_codewords_cache[key]
    two_G = 2.0 * G_tensor
    
    # Broadcast-ready input: (N, 1, 24)
    Y = y.unsqueeze(1)
    
    # =========================================================================
    # Case A: Even coordinates (shift = 2G)
    # =========================================================================
    V_A = 4.0 * torch.round((Y - two_G) / 4.0) + two_G  # (N, 4096, 24)
    
    # Sum coordinates and check parity: sum % 8 == 4 needs correction
    sum_A = torch.sum(V_A, dim=2)  # (N, 4096)
    sum_A_round = torch.round(sum_A).to(torch.int64)
    need_corr_A = (sum_A_round % 8) == 4  # (N, 4096)
    
    # Find coordinate with maximum error
    err_A = torch.abs(Y - V_A)  # (N, 4096, 24)
    best_idx_A = torch.argmax(err_A, dim=2)  # (N, 4096)
    
    # Gather the differences at the maximum error coordinates
    diff_A = Y - V_A
    diff_best_A = torch.gather(diff_A, dim=2, index=best_idx_A.unsqueeze(2)).squeeze(2)  # (N, 4096)
    sign_A = torch.where(diff_best_A >= 0.0, 4.0, -4.0).to(dtype)
    corr_val_A = torch.where(need_corr_A, sign_A, 0.0).to(dtype)
    
    # Apply correction using scatter
    delta_A = torch.zeros_like(V_A)
    delta_A.scatter_(dim=2, index=best_idx_A.unsqueeze(2), src=corr_val_A.unsqueeze(2))
    V_A = V_A + delta_A
    
    # =========================================================================
    # Case B: Odd coordinates (shift = 2G + 1)
    # =========================================================================
    two_G_plus_1 = two_G + 1.0  # (4096, 24)
    V_B = 4.0 * torch.round((Y - two_G_plus_1) / 4.0) + two_G_plus_1  # (N, 4096, 24)
    
    # Sum coordinates and check parity: sum % 8 == 0 needs correction
    sum_B = torch.sum(V_B, dim=2)  # (N, 4096)
    sum_B_round = torch.round(sum_B).to(torch.int64)
    need_corr_B = (sum_B_round % 8) == 0  # (N, 4096)
    
    # Find coordinate with maximum error
    err_B = torch.abs(Y - V_B)  # (N, 4096, 24)
    best_idx_B = torch.argmax(err_B, dim=2)  # (N, 4096)
    
    # Gather the differences at the maximum error coordinates
    diff_B = Y - V_B
    diff_best_B = torch.gather(diff_B, dim=2, index=best_idx_B.unsqueeze(2)).squeeze(2)  # (N, 4096)
    sign_B = torch.where(diff_best_B >= 0.0, 4.0, -4.0).to(dtype)
    corr_val_B = torch.where(need_corr_B, sign_B, 0.0).to(dtype)
    
    # Apply correction using scatter
    delta_B = torch.zeros_like(V_B)
    delta_B.scatter_(dim=2, index=best_idx_B.unsqueeze(2), src=corr_val_B.unsqueeze(2))
    V_B = V_B + delta_B
    
    # =========================================================================
    # Combine & Select Nearest Candidate
    # =========================================================================
    V_all = torch.cat([V_A, V_B], dim=1)  # (N, 8192, 24)
    dists = torch.sum((Y - V_all) ** 2, dim=2)  # (N, 8192)
    best_cand_idx = torch.argmin(dists, dim=1)  # (N,)
    
    # Gather the best candidates
    best_v = torch.gather(
        V_all, 
        dim=1, 
        index=best_cand_idx.unsqueeze(1).unsqueeze(2).expand(-1, -1, 24)
    ).squeeze(1)
    
    return best_v


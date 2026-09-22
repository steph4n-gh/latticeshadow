"""
Adversarial stress-testing suite for SVD-Procrustes alignment.
Exercises extreme mathematical boundaries, singularity, scaling discrepancies, and random noise.
"""

import pytest
import numpy as np
import torch
from latticeshadow_db.alignment import ProcrustesAligner
from latticeshadow_db.exceptions import CalibrationError


def test_zero_and_empty_inputs_numpy():
    """
    Test zero and empty inputs using NumPy arrays of float32 and float64.
    """
    local_dim = 128
    cloud_dim = 128
    n_samples = 50
    
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    
    # 1. Zero inputs
    local_zeros = np.zeros((n_samples, local_dim), dtype=np.float32)
    cloud_zeros = np.zeros((n_samples, cloud_dim), dtype=np.float32)
    
    metrics = aligner.calibrate(local_zeros, cloud_zeros)
    
    assert metrics["entropy"] == 0.0
    # Condition number under zero inputs: s_max is 0, s_min_clamped is 1e-9.
    # Therefore condition number is 0.0. Mathematically, a zero matrix has
    # undefined or infinite condition number. This is a mathematical anomaly.
    assert metrics["condition_number"] == 0.0
    assert metrics["mse"] == 0.0
    
    # 2. Empty inputs (should raise CalibrationError)
    local_empty = np.zeros((0, local_dim), dtype=np.float32)
    cloud_empty = np.zeros((0, cloud_dim), dtype=np.float32)
    with pytest.raises(CalibrationError):
        aligner.calibrate(local_empty, cloud_empty)


def test_zero_inputs_pytorch():
    """
    Test zero inputs using PyTorch Tensors.
    """
    local_dim = 128
    cloud_dim = 128
    n_samples = 50
    
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    
    local_zeros = torch.zeros((n_samples, local_dim), dtype=torch.float32)
    cloud_zeros = torch.zeros((n_samples, cloud_dim), dtype=torch.float32)
    
    metrics = aligner.calibrate(local_zeros, cloud_zeros)
    
    assert metrics["entropy"] == 0.0
    assert metrics["condition_number"] == 0.0
    assert metrics["mse"] == 0.0


def test_rank_deficient_inputs():
    """
    Test singularity and rank-deficient inputs (e.g., rank 1 data in 128-dim space).
    """
    local_dim = 128
    cloud_dim = 128
    n_samples = 100
    
    rng = np.random.default_rng(42)
    u_vec = rng.standard_normal(local_dim)
    v_vec = rng.standard_normal(cloud_dim)
    
    t = rng.standard_normal((n_samples, 1))
    local_states = t @ u_vec[np.newaxis, :]  # Rank 1
    cloud_states = t @ v_vec[np.newaxis, :]  # Rank 1
    
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    metrics = aligner.calibrate(local_states, cloud_states)
    
    assert metrics["entropy"] >= 0.0
    assert metrics["condition_number"] > 1.0
    
    test_cloud = rng.standard_normal((10, cloud_dim))
    aligned = aligner.align(test_cloud)
    assert aligned.shape == (10, local_dim)


def test_condition_number_anomaly():
    """
    Verify the condition number anomaly where s_max < 1e-9.
    This happens when inputs are extremely small, causing s_max to be less than 1e-9.
    As a result, condition_number = s_max / 1e-9 becomes < 1.0, which is mathematically impossible.
    """
    dim = 10
    n_samples = 20
    aligner = ProcrustesAligner(local_dim=dim, cloud_dim=dim)
    
    local_states = np.random.normal(0, 1e-12, size=(n_samples, dim))
    cloud_states = np.random.normal(0, 1e-12, size=(n_samples, dim))
    
    metrics = aligner.calibrate(local_states, cloud_states)
    
    # The condition number should be >= 1.0, but because of the clamp of s_min to 1e-9
    # while s_max is around 1e-22 (or so), condition_number becomes extremely small (< 1.0)
    print(f"DEBUG: s_max={np.max(np.linalg.svd((local_states - np.mean(local_states, axis=0)).T @ (cloud_states - np.mean(cloud_states, axis=0)), compute_uv=False))}")
    print(f"DEBUG: Condition number computed = {metrics['condition_number']}")
    assert metrics["condition_number"] < 1.0


def test_extreme_scaling_discrepancy():
    """
    Test alignment with extreme scaling discrepancies (e.g., local 1e-15, cloud 1e15).
    """
    local_dim = 64
    cloud_dim = 64
    n_samples = 50
    
    rng = np.random.default_rng(42)
    local_base = rng.standard_normal((n_samples, local_dim))
    cloud_base = rng.standard_normal((n_samples, cloud_dim))
    
    local_states = local_base * 1e-15
    cloud_states = cloud_base * 1e15
    
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    metrics = aligner.calibrate(local_states, cloud_states)
    
    assert not np.isnan(metrics["entropy"])
    assert not np.isnan(metrics["condition_number"])
    assert not np.isnan(metrics["mse"])
    assert not np.isnan(metrics["correlation"])
    
    test_cloud = cloud_states[:5]
    aligned = aligner.align(test_cloud)
    assert not np.any(np.isnan(aligned))
    assert not np.any(np.isinf(aligned))


def test_extreme_overflow_floats():
    """
    Test inputs that trigger floating point overflow (e.g. 1e20 for float32).
    Verify that this results in NaN correlation because intermediate products
    overflow the maximum representation limit of the data type.
    """
    local_dim = 16
    cloud_dim = 16
    n_samples = 10
    
    # 1. NumPy float32 overflow
    local_np = np.ones((n_samples, local_dim), dtype=np.float32) * 1e20
    cloud_np = np.ones((n_samples, cloud_dim), dtype=np.float32) * 1e20
    
    aligner_np = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    metrics_np = aligner_np.calibrate(local_np, cloud_np)
    
    # Correlation becomes NaN due to inf / inf division in cosine similarity
    assert np.isnan(metrics_np["correlation"])
    
    # 2. PyTorch float32 overflow
    local_pt = torch.ones((n_samples, local_dim), dtype=torch.float32) * 1e20
    cloud_pt = torch.ones((n_samples, cloud_dim), dtype=torch.float32) * 1e20
    
    aligner_pt = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    metrics_pt = aligner_pt.calibrate(local_pt, cloud_pt)
    
    assert np.isnan(metrics_pt["correlation"])


def test_random_noise_inputs_overfitting():
    """
    Verify random noise inputs and the discrepancy between in-sample and out-of-sample correlation.
    """
    local_dim = 64
    cloud_dim = 64
    n_samples = 30  # N < D to ensure overfitting
    
    rng = np.random.default_rng(100)
    local_train = rng.standard_normal((n_samples, local_dim))
    cloud_train = rng.standard_normal((n_samples, cloud_dim))
    
    local_test = rng.standard_normal((50, local_dim))
    cloud_test = rng.standard_normal((50, cloud_dim))
    
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    metrics = aligner.calibrate(local_train, cloud_train)
    
    in_sample_corr = metrics["correlation"]
    out_of_sample_corr = aligner.score(local_test, cloud_test)
    
    print(f"DEBUG: Random Noise: In-sample correlation = {in_sample_corr:.4f}, Holdout correlation = {out_of_sample_corr:.4f}")
    
    assert in_sample_corr > 0.5
    assert out_of_sample_corr < 0.3

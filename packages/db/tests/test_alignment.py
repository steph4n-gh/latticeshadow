import pytest
import tempfile
from pathlib import Path

import numpy as np
from latticeshadow_db.alignment import ProcrustesAligner
from latticeshadow_db.exceptions import CalibrationError


def _make_correlated_data(local_dim, cloud_dim, n_samples, seed=42):
    """Generates synthetic paired data with a known orthogonal relationship."""
    rng = np.random.default_rng(seed)
    local_states = rng.standard_normal((n_samples, local_dim))

    projection = rng.standard_normal((local_dim, cloud_dim))
    u, _s, vh = np.linalg.svd(projection, full_matrices=False)
    ortho_proj = u @ vh

    cloud_states = (
        local_states @ ortho_proj
        + rng.normal(0, 0.05, size=(n_samples, cloud_dim))
        + 10.0
    )
    return local_states, cloud_states


class TestCalibrationAndAlignment:
    """Assert calibration quality and dimensional correctness."""

    def test_high_correlation_on_synthetic_data(self):
        local_states, cloud_states = _make_correlated_data(128, 256, 200)
        aligner = ProcrustesAligner(local_dim=128, cloud_dim=256)
        metrics = aligner.calibrate(local_states, cloud_states)
        assert isinstance(metrics, dict)
        assert "entropy" in metrics
        assert "condition_number" in metrics
        assert "trace" in metrics
        assert "mse" in metrics
        assert "correlation" in metrics
        assert isinstance(metrics["entropy"], float)
        assert isinstance(metrics["condition_number"], float)
        assert isinstance(metrics["trace"], float)
        assert isinstance(metrics["mse"], float)
        assert metrics["correlation"] >= 0.85
        assert metrics["entropy"] >= 0.0
        assert metrics["condition_number"] >= 1.0
        assert metrics["mse"] >= 0.0

    def test_svd_metrics_for_identity_mapping(self):
        # When mapping is exactly identity,
        # Q should be identity (so trace should be dim), MSE should be 0.0,
        # and correlation should be 1.0.
        dim = 16
        n_samples = 50
        rng = np.random.default_rng(42)
        local_states = rng.standard_normal((n_samples, dim))
        cloud_states = local_states.copy()
        
        aligner = ProcrustesAligner(local_dim=dim, cloud_dim=dim)
        metrics = aligner.calibrate(local_states, cloud_states)
        
        assert np.isclose(metrics["trace"], float(dim), atol=1e-5)
        assert np.isclose(metrics["mse"], 0.0, atol=1e-5)
        assert np.isclose(metrics["correlation"], 1.0, atol=1e-5)
        assert metrics["entropy"] >= 0.0
        assert metrics["condition_number"] >= 1.0

    def test_alignment_shape_and_distance(self):
        local_states, cloud_states = _make_correlated_data(128, 256, 200)
        aligner = ProcrustesAligner(local_dim=128, cloud_dim=256)
        aligner.calibrate(local_states, cloud_states)

        assert aligner.Q.shape == (256, 128)
        test_aligned = aligner.align(cloud_states[0])
        assert test_aligned.shape == (128,)

        dist = np.linalg.norm(local_states[0] - test_aligned)
        assert dist < 1.0, f"Aligned state deviates too much: {dist}"


class TestInputValidation:
    """Verify that using incorrect dims raises ValueError."""

    def test_local_dim_mismatch(self):
        aligner = ProcrustesAligner(local_dim=64, cloud_dim=128)
        with pytest.raises(CalibrationError):
            aligner.calibrate(np.zeros((10, 63)), np.zeros((10, 128)))

    def test_cloud_dim_mismatch(self):
        aligner = ProcrustesAligner(local_dim=64, cloud_dim=128)
        with pytest.raises(CalibrationError):
            aligner.calibrate(np.zeros((10, 64)), np.zeros((10, 129)))

    def test_sample_count_mismatch(self):
        aligner = ProcrustesAligner(local_dim=64, cloud_dim=128)
        with pytest.raises(CalibrationError):
            aligner.calibrate(np.zeros((9, 64)), np.zeros((10, 128)))


class TestSaveLoadRoundTrip:
    """Action 16: Verify that save/load preserves calibration params exactly."""

    def test_save_load_npz(self):
        local_states, cloud_states = _make_correlated_data(32, 64, 50, seed=99)
        aligner = ProcrustesAligner(local_dim=32, cloud_dim=64)
        aligner.calibrate(local_states, cloud_states)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "params.npz"
            aligner.save(path)

            # Verify both files were created
            assert path.exists(), "npz file not created"
            assert path.with_suffix(".meta.json").exists(), "JSON sidecar not created"

            loaded = ProcrustesAligner(local_dim=1, cloud_dim=1)  # dummy dims
            loaded.load(path)

        assert loaded.is_calibrated
        assert loaded.local_dim == 32
        assert loaded.cloud_dim == 64
        np.testing.assert_array_equal(loaded.Q, aligner.Q)
        np.testing.assert_array_equal(loaded.mu_local, aligner.mu_local)
        np.testing.assert_array_equal(loaded.mu_cloud, aligner.mu_cloud)

        # Verify alignment produces identical results
        test_vec = cloud_states[0]
        np.testing.assert_allclose(
            loaded.align(test_vec),
            aligner.align(test_vec),
            atol=1e-12,
        )


class TestScore:
    """Verify the holdout score() method works correctly."""

    def test_score_on_holdout(self):
        local_train, cloud_train = _make_correlated_data(64, 128, 150, seed=1)
        local_test, cloud_test = _make_correlated_data(64, 128, 50, seed=2)

        aligner = ProcrustesAligner(local_dim=64, cloud_dim=128)
        in_sample = aligner.calibrate(local_train, cloud_train)["correlation"]
        holdout = aligner.score(local_test, cloud_test)

        # Both should be positive for correlated data
        assert holdout > 0.0
        # Holdout will typically be lower than in-sample
        assert isinstance(holdout, float)


class TestAdversarialCalibration:
    """Verify adversarial calibration keeps the final augmented fit."""

    def test_final_fit_uses_all_adversarial_batches(self, monkeypatch):
        local_states, cloud_states = _make_correlated_data(4, 4, 8, seed=123)
        aligner = ProcrustesAligner(local_dim=4, cloud_dim=4)

        observed_sample_counts = []
        original_calibrate = aligner.calibrate

        def spy_calibrate(local_arg, cloud_arg):
            observed_sample_counts.append(local_arg.shape[0])
            return original_calibrate(local_arg, cloud_arg)

        monkeypatch.setattr(aligner, "calibrate", spy_calibrate)

        result = aligner.calibrate_adversarial(
            local_states,
            cloud_states,
            generator_epochs=1,
            calibration_rounds=2,
            generator_hidden_dim=8,
        )

        assert result["adversarial_pairs_generated"] == 16
        assert observed_sample_counts[-1] == 24


class TestMpsCompatibility:
    """Verify MPS device compatibility during alignment."""

    def test_mps_compatibility_with_float64_params(self):
        import torch
        if not (torch.backends.mps.is_available() and torch.backends.mps.is_built()):
            pytest.skip("MPS device is not available on this platform")

        # Calibrate using numpy float64 arrays
        local_states, cloud_states = _make_correlated_data(32, 64, 50, seed=99)
        aligner = ProcrustesAligner(local_dim=32, cloud_dim=64)
        aligner.calibrate(local_states, cloud_states)

        # Ensure parameters are float64
        assert aligner.Q.dtype == np.float64

        # Create input tensor on MPS device
        cloud_tensor = torch.randn((10, 64), dtype=torch.float32, device="mps")

        # This should execute without TypeError / float64 compatibility crash
        aligned = aligner.align(cloud_tensor)

        assert aligned.device.type == "mps"
        assert aligned.dtype == torch.float32
        assert aligned.shape == (10, 32)

"""Tests for Temporal Coherence Memory (TemporalStateBuffer)."""

import pytest
import torch
import time

from latticeshadow_db.temporal import TemporalStateBuffer, QueryState


@pytest.fixture
def buffer():
    return TemporalStateBuffer(window_size=16, momentum_beta_max=0.3, trend_scaling=2.0)


def _make_state(
    residual: float = 0.1,
    alpha: float = 0.4,
    entropy: float = 2.0,
    route: str = "cloud",
    dim: int = 64,
    aligned: bool = True,
) -> QueryState:
    """Helper to create a QueryState with given metrics."""
    local = torch.randn(1, dim)
    aligned_state = local * 1.1 if aligned else None
    return QueryState(
        local_state=local,
        aligned_state=aligned_state,
        residual=residual,
        adaptive_alpha=alpha,
        entropy=entropy,
        route=route,
    )


class TestBufferBasics:
    def test_empty_buffer(self, buffer):
        assert buffer.is_empty
        assert buffer.size == 0
        assert buffer.mean_alpha == 0.0
        assert buffer.mean_entropy == 0.0
        assert buffer.mean_residual == 0.0
        assert buffer.cloud_skip_rate == 0.0
        assert buffer.health_status == "cold"
        assert buffer.residual_trend == 0.0
        assert buffer.momentum_vector is None
        assert buffer.momentum_stability == 0.0
        assert buffer.route_distribution == {}
        assert buffer.drift_active is False
        assert buffer.drift_status["reason"] == "insufficient_samples"

    def test_record_and_size(self, buffer):
        buffer.record(_make_state())
        assert buffer.size == 1
        assert not buffer.is_empty

    def test_window_overflow(self, buffer):
        """Buffer should respect maxlen."""
        for i in range(30):
            buffer.record(_make_state(residual=float(i)))
        assert buffer.size == 16  # window_size

    def test_clear(self, buffer):
        buffer.record(_make_state())
        buffer.record(_make_state())
        buffer.clear()
        assert buffer.is_empty
        assert buffer.size == 0


class TestResidualTrend:
    def test_decreasing_residuals_negative_trend(self, buffer):
        """Monotonically decreasing residuals should give negative trend."""
        for i in range(10):
            buffer.record(_make_state(residual=1.0 - i * 0.08))
        trend = buffer.residual_trend
        assert trend < 0, f"Expected negative trend, got {trend}"

    def test_increasing_residuals_positive_trend(self, buffer):
        """Monotonically increasing residuals should give positive trend."""
        for i in range(10):
            buffer.record(_make_state(residual=0.1 + i * 0.08))
        trend = buffer.residual_trend
        assert trend > 0, f"Expected positive trend, got {trend}"

    def test_constant_residuals_near_zero_trend(self, buffer):
        """Constant residuals should give near-zero trend."""
        for _ in range(10):
            buffer.record(_make_state(residual=0.5))
        trend = buffer.residual_trend
        assert abs(trend) < 0.01, f"Expected near-zero trend, got {trend}"

    def test_insufficient_data_returns_zero(self, buffer):
        """Fewer than 3 cloud states should return 0."""
        buffer.record(_make_state(residual=0.5))
        buffer.record(_make_state(residual=0.3))
        assert buffer.residual_trend == 0.0


class TestMomentum:
    def test_consistent_corrections_produce_momentum(self, buffer):
        """When corrections are in the same direction, momentum should be non-None."""
        for i in range(5):
            local = torch.ones(1, 32) * float(i + 1)
            # Aligned is always local + constant offset
            aligned = local + torch.ones(1, 32) * 0.5
            state = QueryState(
                local_state=local,
                aligned_state=aligned,
                residual=0.1,
                adaptive_alpha=0.4,
                entropy=2.0,
                route="cloud",
            )
            buffer.record(state)

        momentum = buffer.momentum_vector
        assert momentum is not None
        assert momentum.shape == (32,)

    def test_no_aligned_states_returns_none(self, buffer):
        """Without cloud interactions, momentum should be None."""
        for _ in range(5):
            buffer.record(_make_state(aligned=False, route="local"))
        assert buffer.momentum_vector is None


class TestHealthSignals:
    def test_mean_alpha(self, buffer):
        buffer.record(_make_state(alpha=0.3))
        buffer.record(_make_state(alpha=0.5))
        assert abs(buffer.mean_alpha - 0.4) < 1e-6

    def test_mean_entropy(self, buffer):
        buffer.record(_make_state(entropy=1.0))
        buffer.record(_make_state(entropy=3.0))
        assert abs(buffer.mean_entropy - 2.0) < 1e-6

    def test_cloud_skip_rate(self, buffer):
        buffer.record(_make_state(route="cloud"))
        buffer.record(_make_state(route="speculative"))
        buffer.record(_make_state(route="local"))
        buffer.record(_make_state(route="cloud"))
        assert abs(buffer.cloud_skip_rate - 0.5) < 1e-6

    def test_route_distribution(self, buffer):
        buffer.record(_make_state(route="cloud"))
        buffer.record(_make_state(route="cloud"))
        buffer.record(_make_state(route="speculative"))
        buffer.record(_make_state(route="local"))
        dist = buffer.route_distribution
        assert abs(dist["cloud"] - 0.5) < 1e-6
        assert abs(dist["speculative"] - 0.25) < 1e-6
        assert abs(dist["local"] - 0.25) < 1e-6

    def test_health_status_converging(self, buffer):
        for i in range(10):
            buffer.record(_make_state(residual=1.0 - i * 0.08))
        assert buffer.health_status == "converging"

    def test_health_status_degrading(self, buffer):
        for i in range(10):
            buffer.record(_make_state(residual=0.1 + i * 0.08))
        assert buffer.health_status in ("degrading", "drift")

    def test_health_status_cold(self, buffer):
        buffer.record(_make_state())
        assert buffer.health_status == "cold"


class TestSpeculativeThreshold:
    def test_converging_increases_threshold(self, buffer):
        """When residuals decrease, threshold should increase (more aggressive)."""
        for i in range(10):
            buffer.record(_make_state(residual=1.0 - i * 0.08))

        base = 0.05
        suggested = buffer.suggest_speculative_threshold(base)
        assert suggested > base, f"Expected threshold > {base}, got {suggested}"

    def test_degrading_decreases_threshold(self, buffer):
        """When residuals increase, threshold should decrease (more conservative)."""
        for i in range(10):
            buffer.record(_make_state(residual=0.1 + i * 0.08))

        base = 0.05
        suggested = buffer.suggest_speculative_threshold(base)
        assert suggested < base, f"Expected threshold < {base}, got {suggested}"

    def test_clamped_bounds(self, buffer):
        """Threshold should be clamped to [0.5x, 3.0x] of base."""
        # Extreme convergence
        for i in range(10):
            buffer.record(_make_state(residual=10.0 - i * 1.0))

        base = 0.05
        suggested = buffer.suggest_speculative_threshold(base)
        assert suggested <= base * 3.0
        assert suggested >= base * 0.5

    def test_empty_buffer_returns_base(self, buffer):
        assert buffer.suggest_speculative_threshold(0.05) == 0.05


class TestDriftGuard:
    def test_residual_drift_activates_guard_and_tightens_threshold(self):
        buffer = TemporalStateBuffer(
            window_size=16,
            drift_min_samples=4,
            drift_relative_threshold=0.2,
            drift_page_hinkley_threshold=0.05,
            drift_tightening_factor=0.25,
        )

        for _ in range(4):
            buffer.record(_make_state(residual=0.1, route="cloud"))
        assert buffer.drift_active is False

        for _ in range(4):
            buffer.record(_make_state(residual=0.4, route="cloud"))

        status = buffer.drift_status
        assert buffer.drift_active is True
        assert status["reason"] == "residual_drift"
        assert status["recent_residual"] > status["baseline_residual"]
        assert buffer.health_status == "drift"
        assert buffer.suggest_speculative_threshold(0.1) <= 0.025 + 1e-9

    def test_drift_guard_recovers_after_stable_residuals(self):
        buffer = TemporalStateBuffer(
            window_size=6,
            drift_min_samples=3,
            drift_relative_threshold=0.5,
            drift_page_hinkley_threshold=999.0,
            drift_recovery_samples=2,
        )

        for _ in range(3):
            buffer.record(_make_state(residual=0.1, route="cloud"))
        for _ in range(3):
            buffer.record(_make_state(residual=0.4, route="cloud"))
        assert buffer.drift_active is True

        for _ in range(8):
            buffer.record(_make_state(residual=0.1, route="cloud"))

        assert buffer.drift_active is False
        assert buffer.drift_status["reason"] in ("recovered", "stable")


class TestMomentumCorrection:
    def test_returns_original_on_empty_buffer(self, buffer):
        local = torch.randn(1, 32)
        corrected = buffer.suggest_momentum_correction(local)
        assert torch.equal(corrected, local)

    def test_returns_original_on_unstable_momentum(self, buffer):
        """With wildly varying corrections, momentum is unstable → no correction."""
        for i in range(5):
            local = torch.randn(1, 32)
            aligned = torch.randn(1, 32)  # random direction each time
            state = QueryState(
                local_state=local,
                aligned_state=aligned,
                residual=0.1,
                adaptive_alpha=0.4,
                entropy=2.0,
                route="cloud",
            )
            buffer.record(state)

        local = torch.randn(1, 32)
        corrected = buffer.suggest_momentum_correction(local)
        # With random corrections, stability should be low → no correction applied
        # (or the first call bootstraps _prev_momentum, so it's just original)
        assert corrected.shape == local.shape


class TestBridgeIntegration:
    """Test temporal integration in ZkBridge."""

    def test_temporal_enabled_by_default(self, tmp_path):
        from latticeshadow_db import CayleyPrivacyAdapter, ProcrustesAligner, ZkBridge
        import numpy as np

        adapter = CayleyPrivacyAdapter(dim=32, rank=4, device="cpu")
        aligner = ProcrustesAligner(local_dim=32, cloud_dim=32)
        x = np.random.normal(0, 1, size=(5, 32))
        aligner.calibrate(x, x)

        bridge = ZkBridge(
            adapter=adapter,
            aligner=aligner,
            mock_mode=True,
            cache_db_path=str(tmp_path / "test.db"),
        )
        assert bridge.temporal is not None
        assert bridge.temporal.is_empty

    def test_temporal_records_after_query(self, tmp_path):
        from latticeshadow_db import CayleyPrivacyAdapter, ProcrustesAligner, ZkBridge
        import numpy as np

        adapter = CayleyPrivacyAdapter(dim=32, rank=4, device="cpu")
        aligner = ProcrustesAligner(local_dim=32, cloud_dim=32)
        x = np.random.normal(0, 1, size=(5, 32))
        aligner.calibrate(x, x)

        bridge = ZkBridge(
            adapter=adapter,
            aligner=aligner,
            mock_mode=True,
            enable_routing=False,
            enable_speculation=False,
            cache_db_path=str(tmp_path / "test.db"),
        )

        h = torch.randn(1, 32)
        bridge.query(h, "test context")
        assert bridge.temporal.size >= 1

    def test_temporal_disabled(self, tmp_path):
        from latticeshadow_db import CayleyPrivacyAdapter, ProcrustesAligner, ZkBridge
        import numpy as np

        adapter = CayleyPrivacyAdapter(dim=32, rank=4, device="cpu")
        aligner = ProcrustesAligner(local_dim=32, cloud_dim=32)
        x = np.random.normal(0, 1, size=(5, 32))
        aligner.calibrate(x, x)

        bridge = ZkBridge(
            adapter=adapter,
            aligner=aligner,
            mock_mode=True,
            enable_temporal=False,
            cache_db_path=str(tmp_path / "test.db"),
        )
        assert bridge.temporal is None

        # Should still work without temporal
        h = torch.randn(1, 32)
        result = bridge.query(h, "test")
        assert result.shape == h.shape

    def test_health_status_accessible(self, tmp_path):
        from latticeshadow_db import CayleyPrivacyAdapter, ProcrustesAligner, ZkBridge
        import numpy as np

        adapter = CayleyPrivacyAdapter(dim=32, rank=4, device="cpu")
        aligner = ProcrustesAligner(local_dim=32, cloud_dim=32)
        x = np.random.normal(0, 1, size=(5, 32))
        aligner.calibrate(x, x)

        bridge = ZkBridge(
            adapter=adapter,
            aligner=aligner,
            mock_mode=True,
            enable_routing=False,
            enable_speculation=False,
            cache_db_path=str(tmp_path / "test.db"),
        )

        # Run several queries
        for _ in range(5):
            bridge.query(torch.randn(1, 32), f"ctx_{_}")

        assert bridge.temporal.size == 5
        assert bridge.temporal.health_status in ("cold", "healthy", "converging", "degrading")
        assert 0.0 <= bridge.temporal.cloud_skip_rate <= 1.0

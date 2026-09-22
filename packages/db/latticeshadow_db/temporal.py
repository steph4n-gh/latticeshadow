"""
Temporal Coherence Memory — Multi-turn conversation state tracking.

Maintains a rolling window of recent query execution snapshots to enable:
- Trajectory-based speculation: lower threshold when residuals are converging
- Momentum pre-correction: pre-apply consistent correction direction
- Conversation health signals: rolling averages for monitoring
- Drift guard: tighten cloud-skip paths when cloud residuals degrade

Usage:
    buffer = TemporalStateBuffer(window_size=16)
    buffer.record(QueryState(...))
    
    threshold = buffer.suggest_speculative_threshold(base_threshold=0.05)
    corrected = buffer.suggest_momentum_correction(local_state)
"""

import logging
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import torch

logger = logging.getLogger("latticeshadow_db.temporal")


@dataclass
class QueryState:
    """Snapshot of a single query's execution path."""

    local_state: torch.Tensor
    aligned_state: Optional[torch.Tensor]
    residual: float
    adaptive_alpha: float
    entropy: float
    route: str  # "local", "flash", "ultra", "speculative", "distilled", "cloud"
    timestamp: float = field(default_factory=time.time)


class TemporalStateBuffer:
    """Rolling window of recent query states for multi-turn coherence.

    Tracks the trajectory of residuals, correction vectors, and routing
    decisions across a conversation to enable adaptive speculation and
    health monitoring.

    Args:
        window_size: Maximum number of recent states to retain.
        momentum_beta_max: Maximum momentum blending coefficient (ramps up
            as momentum direction stabilizes).
        trend_scaling: Scaling constant for speculative threshold adjustment.
    """

    def __init__(
        self,
        window_size: int = 16,
        momentum_beta_max: float = 0.3,
        trend_scaling: float = 2.0,
        drift_min_samples: int = 8,
        drift_relative_threshold: float = 0.15,
        drift_page_hinkley_delta: float = 0.002,
        drift_page_hinkley_threshold: float = 0.05,
        drift_tightening_factor: float = 0.25,
        drift_recovery_samples: int = 3,
    ):
        self._buffer: deque[QueryState] = deque(maxlen=window_size)
        self.lock = threading.RLock()
        self._momentum_beta_max = momentum_beta_max
        self._trend_scaling = trend_scaling
        self._prev_momentum: Optional[torch.Tensor] = None
        self._drift_min_samples = max(2, drift_min_samples)
        self._drift_relative_threshold = max(0.0, drift_relative_threshold)
        self._drift_page_hinkley_delta = max(0.0, drift_page_hinkley_delta)
        self._drift_page_hinkley_threshold = max(0.0, drift_page_hinkley_threshold)
        self._drift_tightening_factor = max(0.05, min(1.0, drift_tightening_factor))
        self._drift_recovery_samples = max(1, drift_recovery_samples)
        self._drift_residuals: deque[float] = deque(maxlen=window_size)
        self._drift_baseline: Optional[float] = None
        self._drift_active = False
        self._drift_reason = "insufficient_samples"
        self._drift_score = 0.0
        self._drift_recent_mean = 0.0
        self._drift_window_relative_increase = 0.0
        self._drift_page_hinkley_score = 0.0
        self._drift_recovery_count = 0
        self._ph_samples = 0
        self._ph_mean = 0.0
        self._ph_cumulative = 0.0
        self._ph_min = 0.0

    def record(self, state: QueryState) -> None:
        """Record a query execution snapshot."""
        with self.lock:
            self._buffer.append(state)
            self._update_drift_guard_locked(state)

    def clear(self) -> None:
        """Clear all recorded states."""
        with self.lock:
            self._buffer.clear()
            self._prev_momentum = None
            self._drift_residuals.clear()
            self._drift_baseline = None
            self._drift_active = False
            self._drift_reason = "insufficient_samples"
            self._drift_score = 0.0
            self._drift_recent_mean = 0.0
            self._drift_window_relative_increase = 0.0
            self._drift_page_hinkley_score = 0.0
            self._drift_recovery_count = 0
            self._ph_samples = 0
            self._ph_mean = 0.0
            self._ph_cumulative = 0.0
            self._ph_min = 0.0

    @property
    def size(self) -> int:
        """Number of recorded states."""
        with self.lock:
            return len(self._buffer)

    @property
    def is_empty(self) -> bool:
        with self.lock:
            return len(self._buffer) == 0

    # ── Trajectory Analysis ─────────────────────────────────────────────

    @property
    def residual_trend(self) -> float:
        """Linear regression slope over recent residuals.

        Returns:
            Negative values indicate improving (decreasing) residuals.
            Positive values indicate degrading (increasing) residuals.
            Returns 0.0 if fewer than 3 states recorded.
        """
        with self.lock:
            cloud_states = [s for s in self._buffer if s.aligned_state is not None]
        if len(cloud_states) < 3:
            return 0.0

        residuals = [s.residual for s in cloud_states]
        n = len(residuals)

        # Simple linear regression: slope = (n*Σ(xy) - Σx*Σy) / (n*Σ(x²) - (Σx)²)
        x_sum = n * (n - 1) / 2  # sum of 0..n-1
        x2_sum = n * (n - 1) * (2 * n - 1) / 6  # sum of squares
        y_sum = sum(residuals)
        xy_sum = sum(i * r for i, r in enumerate(residuals))

        denom = n * x2_sum - x_sum * x_sum
        if abs(denom) < 1e-12:
            return 0.0

        slope = (n * xy_sum - x_sum * y_sum) / denom
        return slope

    @property
    def momentum_vector(self) -> Optional[torch.Tensor]:
        """Mean correction direction from recent cloud interactions.

        Returns the exponentially-weighted mean of (aligned - local)
        vectors, or None if no cloud interactions recorded.
        """
        with self.lock:
            corrections = []
            for s in self._buffer:
                if s.aligned_state is not None:
                    diff = s.aligned_state.detach() - s.local_state.detach()
                    corrections.append(diff.view(-1))

        if not corrections:
            return None

        # Exponential decay: recent corrections weighted higher
        # weight_i = decay^(n - 1 - i), so latest entry has weight 1.0
        decay = 0.85
        n = len(corrections)
        weights = torch.tensor(
            [decay ** (n - 1 - i) for i in range(n)],
            dtype=corrections[0].dtype,
            device=corrections[0].device,
        )
        weights = weights / weights.sum()

        # Check dimension compatibility
        dim = corrections[0].shape[0]
        compatible = [c for c in corrections if c.shape[0] == dim]
        if len(compatible) < 2:
            return None

        weights = weights[-len(compatible):]
        weights = weights / weights.sum()

        stacked = torch.stack(compatible)
        momentum = (stacked * weights.unsqueeze(1)).sum(dim=0)
        return momentum

    @property
    def momentum_stability(self) -> float:
        """Cosine similarity between current and previous momentum vectors.

        Returns 0.0 if insufficient data, values near 1.0 indicate
        the correction direction is stable across queries.
        """
        current = self.momentum_vector
        if current is None or self._prev_momentum is None:
            return 0.0

        if current.shape != self._prev_momentum.shape:
            return 0.0

        cos_sim = torch.nn.functional.cosine_similarity(
            current.unsqueeze(0),
            self._prev_momentum.unsqueeze(0),
            dim=1,
        )
        return max(0.0, cos_sim.item())

    # ── Health Signals ──────────────────────────────────────────────────

    @property
    def mean_alpha(self) -> float:
        """Rolling mean of adaptive alpha values."""
        with self.lock:
            if len(self._buffer) == 0:
                return 0.0
            return sum(s.adaptive_alpha for s in self._buffer) / len(self._buffer)

    @property
    def mean_entropy(self) -> float:
        """Rolling mean of activation entropy values."""
        with self.lock:
            if len(self._buffer) == 0:
                return 0.0
            return sum(s.entropy for s in self._buffer) / len(self._buffer)

    @property
    def mean_residual(self) -> float:
        """Rolling mean of residuals (cloud interactions only)."""
        with self.lock:
            cloud_states = [s for s in self._buffer if s.aligned_state is not None]
        if not cloud_states:
            return 0.0
        return sum(s.residual for s in cloud_states) / len(cloud_states)

    @property
    def cloud_skip_rate(self) -> float:
        """Fraction of recent queries that skipped the cloud call."""
        with self.lock:
            if len(self._buffer) == 0:
                return 0.0
            skipped = sum(
                1
                for s in self._buffer
                if s.route in ("local", "speculative", "distilled", "cache")
            )
            return skipped / len(self._buffer)

    @property
    def route_distribution(self) -> dict:
        """Distribution of routing decisions across the window."""
        with self.lock:
            if len(self._buffer) == 0:
                return {}
            counts: dict[str, int] = {}
            for s in self._buffer:
                counts[s.route] = counts.get(s.route, 0) + 1
            total = len(self._buffer)
        return {k: v / total for k, v in sorted(counts.items())}

    @property
    def drift_active(self) -> bool:
        """True when residual drift should tighten cloud-skip shortcuts."""
        with self.lock:
            return self._drift_active

    @property
    def drift_status(self) -> dict:
        """Latest residual drift guard telemetry."""
        with self.lock:
            return {
                "active": self._drift_active,
                "reason": self._drift_reason,
                "sample_count": len(self._drift_residuals),
                "baseline_residual": self._drift_baseline,
                "recent_residual": self._drift_recent_mean,
                "relative_increase": self._drift_score,
                "window_relative_increase": self._drift_window_relative_increase,
                "page_hinkley_score": self._drift_page_hinkley_score,
                "tightening_factor": self._drift_tightening_factor,
                "recovery_count": self._drift_recovery_count,
            }

    @property
    def health_status(self) -> str:
        """Human-readable health assessment.

        Returns one of:
            'healthy': Residuals stable or improving, alpha reasonable
            'converging': Residuals actively decreasing
            'degrading': Residuals increasing — consider recalibrating
            'drift': Residual drift guard is active
            'cold': Too few samples to assess
        """
        if self.drift_active:
            return "drift"
        if self.size < 3:
            return "cold"

        trend = self.residual_trend
        if trend < -0.01:
            return "converging"
        elif trend > 0.02:
            return "degrading"
        else:
            return "healthy"

    # ── Suggestion Methods ──────────────────────────────────────────────

    def suggest_speculative_threshold(self, base_threshold: float) -> float:
        """Suggest an adjusted speculative threshold based on residual trajectory.

        If residuals are trending down (converging), increase the threshold
        to be more aggressive about skipping cloud calls. If trending up
        (degrading), tighten the threshold.

        Args:
            base_threshold: The default speculative_threshold from ZkBridge.

        Returns:
            Adjusted threshold, clamped to [base_threshold * 0.5, base_threshold * 3.0].
        """
        if self.size < 3:
            return base_threshold

        trend = self.residual_trend
        # negative trend = improving → increase threshold (more aggressive)
        # positive trend = degrading → decrease threshold (more conservative)
        adjustment = 1.0 + max(-1.0, min(1.0, -trend * self._trend_scaling))

        adjusted = base_threshold * adjustment
        if self.drift_active:
            adjusted = min(adjusted, base_threshold * self._drift_tightening_factor)
            return max(base_threshold * 0.05, min(base_threshold * 3.0, adjusted))
        return max(base_threshold * 0.5, min(base_threshold * 3.0, adjusted))

    def _update_drift_guard_locked(self, state: QueryState) -> None:
        """Update residual drift state from cloud-anchored observations."""
        if state.route != "cloud" or state.aligned_state is None:
            return

        residual = max(0.0, float(state.residual))
        self._drift_residuals.append(residual)
        self._ph_samples += 1
        self._ph_mean += (residual - self._ph_mean) / float(self._ph_samples)
        self._ph_cumulative += residual - self._ph_mean - self._drift_page_hinkley_delta
        self._ph_min = min(self._ph_min, self._ph_cumulative)
        self._drift_page_hinkley_score = self._ph_cumulative - self._ph_min

        sample_count = len(self._drift_residuals)
        self._drift_recent_mean = sum(self._drift_residuals) / float(sample_count)
        if sample_count < self._drift_min_samples:
            self._drift_reason = "insufficient_samples"
            return

        if self._drift_baseline is None:
            self._drift_baseline = self._drift_recent_mean
            self._drift_reason = "baseline_established"
            return

        baseline = max(abs(self._drift_baseline), 1e-8)
        relative_increase = (self._drift_recent_mean - self._drift_baseline) / baseline
        self._drift_window_relative_increase = self._two_window_relative_increase_locked()
        self._drift_score = max(
            relative_increase,
            self._drift_window_relative_increase,
            self._drift_page_hinkley_score,
        )

        if self._drift_active:
            recovery_limit = self._drift_baseline * (1.0 + self._drift_relative_threshold * 0.5)
            if self._drift_recent_mean <= recovery_limit:
                self._drift_recovery_count += 1
            else:
                self._drift_recovery_count = 0
                self._drift_reason = "residual_drift"
                return

            if self._drift_recovery_count >= self._drift_recovery_samples:
                self._drift_active = False
                self._drift_baseline = self._drift_recent_mean
                self._reset_page_hinkley_locked()
                self._drift_reason = "recovered"
            else:
                self._drift_reason = "recovering"
            return

        triggered = (
            relative_increase >= self._drift_relative_threshold
            or self._drift_window_relative_increase >= self._drift_relative_threshold
            or self._drift_page_hinkley_score >= self._drift_page_hinkley_threshold
        )
        if triggered:
            self._drift_active = True
            self._drift_reason = "residual_drift"
            self._drift_recovery_count = 0
            return

        self._drift_baseline = 0.95 * self._drift_baseline + 0.05 * self._drift_recent_mean
        self._drift_reason = "stable"

    def _two_window_relative_increase_locked(self) -> float:
        """ADWIN-style recent-vs-old residual mean increase over the window."""
        values = list(self._drift_residuals)
        if len(values) < self._drift_min_samples * 2:
            return 0.0
        split = len(values) // 2
        old_mean = sum(values[:split]) / float(split)
        new_mean = sum(values[split:]) / float(len(values) - split)
        return (new_mean - old_mean) / max(abs(old_mean), 1e-8)

    def _reset_page_hinkley_locked(self) -> None:
        """Reset Page-Hinkley accumulators after recovery."""
        self._ph_samples = 0
        self._ph_mean = 0.0
        self._ph_cumulative = 0.0
        self._ph_min = 0.0
        self._drift_page_hinkley_score = 0.0

    def suggest_momentum_correction(
        self, local_state: torch.Tensor
    ) -> torch.Tensor:
        """Apply momentum-based pre-correction to a local state.

        If recent cloud corrections have been consistently in the same
        direction, pre-apply a fraction of that correction to the local
        state before speculation.

        Args:
            local_state: Raw local hidden state tensor.

        Returns:
            Pre-corrected tensor (or unchanged if insufficient data).
        """
        momentum = self.momentum_vector
        if momentum is None:
            return local_state

        stability = self.momentum_stability

        # Only apply momentum if direction is stable (cosine > 0.5)
        if stability < 0.5:
            # Update previous momentum for next stability check
            self._prev_momentum = momentum.clone()
            return local_state

        # Scale beta by stability: ramps from 0 at stability=0.5 to max at 1.0
        beta = self._momentum_beta_max * (stability - 0.5) / 0.5

        # Reshape momentum to match local_state
        correction = momentum.view(local_state.shape)
        corrected = local_state + beta * correction

        # Update previous momentum for next stability check
        self._prev_momentum = momentum.clone()

        logger.debug(
            "Momentum pre-correction applied: beta=%.3f, stability=%.3f",
            beta,
            stability,
        )
        return corrected
"""
    Temporal Coherence Memory for latticeshadow_db v0.2.0+
    
    This module provides multi-turn conversation awareness to the ZkBridge
    pipeline, enabling adaptive speculation, momentum-based pre-correction,
    and real-time health monitoring.
"""

import pytest
import json
import urllib.error
from unittest.mock import patch, MagicMock

import numpy as np
import torch

from latticeshadow_db.adapter import CayleyPrivacyAdapter
from latticeshadow_db.alignment import ProcrustesAligner
from latticeshadow_db.bridge import ZkBridge
from latticeshadow_db.temporal import QueryState

# Target module path for mocking (Action 8: patch at usage site)
_URLOPEN_PATH = "latticeshadow_db.bridge.urllib.request.urlopen"


def _activate_drift_guard(bridge: ZkBridge) -> None:
    local = torch.ones(1, bridge.adapter.dim)
    aligned = local * 1.1
    for _ in range(8):
        bridge.temporal.record(
            QueryState(
                local_state=local,
                aligned_state=aligned,
                residual=0.1,
                adaptive_alpha=bridge.alpha,
                entropy=0.0,
                route="cloud",
            )
        )
    for _ in range(8):
        bridge.temporal.record(
            QueryState(
                local_state=local,
                aligned_state=aligned,
                residual=0.4,
                adaptive_alpha=bridge.alpha,
                entropy=0.0,
                route="cloud",
            )
        )


@pytest.fixture
def setup_bridge(tmp_path):
    local_dim = 8
    cloud_dim = 16
    rank = 4

    np.random.seed(42)  # Action 4: Seed fixture RNG

    adapter = CayleyPrivacyAdapter(dim=local_dim, rank=rank, device="cpu")
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)

    local_states = np.random.normal(0, 1.0, size=(10, local_dim))
    cloud_states = np.random.normal(0, 1.0, size=(10, cloud_dim))
    aligner.calibrate(local_states, cloud_states)

    bridge = ZkBridge(
        adapter=adapter,
        aligner=aligner,
        api_endpoint="http://mock-api.com/correct",
        api_key="mock-key",
        pathway="custom",
        alpha=0.4,
        timeout=1.0,
        mock_mode=False,
        enable_speculation=False,
        adaptive_alpha=False,
        enable_routing=False,
        cache_db_path=str(tmp_path / "test_cache.db"),
    )

    return bridge


class TestShouldTrigger:
    """Verify divergence trigger logic, including boundary."""

    def test_below_threshold_triggers(self, setup_bridge):
        assert setup_bridge.should_trigger(0.5, soft_threshold=0.8) is True

    def test_above_threshold_does_not_trigger(self, setup_bridge):
        assert setup_bridge.should_trigger(0.9, soft_threshold=0.8) is False

    def test_at_boundary_does_not_trigger(self, setup_bridge):
        """Action 12: Verify cfi == threshold is a no-trigger."""
        assert setup_bridge.should_trigger(0.8, soft_threshold=0.8) is False


class TestQueryCustomPathway:
    """Test the custom split-layer API pathway."""

    def test_success(self, setup_bridge):
        bridge = setup_bridge
        cloud_dim = bridge.aligner.cloud_dim
        mock_cloud_state = [float(x) for x in range(cloud_dim)]

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"cloud_state": mock_cloud_state}
        ).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        h_local = torch.ones(1, bridge.adapter.dim)

        with patch(_URLOPEN_PATH, return_value=mock_response) as mock_urlopen:
            h_corrected = bridge.query(h_local, "test context")
            mock_urlopen.assert_called_once()
            assert h_corrected.shape == h_local.shape
            assert not torch.allclose(h_local, h_corrected)

    def test_send_text_context_false_omits_raw_context(self, setup_bridge):
        bridge = setup_bridge
        bridge.send_text_context = False
        cloud_dim = bridge.aligner.cloud_dim
        mock_cloud_state = [float(x) for x in range(cloud_dim)]

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"cloud_state": mock_cloud_state}
        ).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        h_local = torch.ones(1, bridge.adapter.dim)
        sensitive_context = "secret patient alice@example.com"

        with patch(_URLOPEN_PATH, return_value=mock_response) as mock_urlopen:
            h_corrected = bridge.query(h_local, sensitive_context)

        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["text_context"] == ""
        assert sensitive_context not in request.data.decode("utf-8")
        assert h_corrected.shape == h_local.shape

    def test_hopfield_cache_fast_path_is_opt_in(self, setup_bridge):
        bridge = setup_bridge
        bridge.cache.entropy_tolerance = 999.0
        h_local = torch.tensor([[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
        near_key = torch.tensor([[0.8, 0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
        bridge.cache.insert(near_key, h_local * 2.0, "near")
        assert bridge.cache.check_cache(h_local, threshold=0.95) is None

        cloud_state = np.ones(bridge.aligner.cloud_dim, dtype=np.float32)
        with patch.object(bridge, "_fetch_cloud_representation", return_value=cloud_state) as mock_fetch:
            result = bridge.query(h_local, "hopfield disabled")

        mock_fetch.assert_called_once()
        assert result.shape == h_local.shape
        assert bridge.cache.last_hopfield_status["reason"] == "not_run"

    def test_hopfield_cache_fast_path_bypasses_cloud_when_enabled(self, setup_bridge):
        bridge = setup_bridge
        bridge.enable_hopfield_cache = True
        bridge.hopfield_min_confidence = 0.9
        bridge.cache.entropy_tolerance = 999.0
        h_local = torch.tensor([[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
        near_key = torch.tensor([[0.8, 0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
        cached_cloud = h_local * 2.0
        bridge.cache.insert(near_key, cached_cloud, "near")
        assert bridge.cache.check_cache(h_local, threshold=0.95) is None

        with patch.object(bridge, "_fetch_cloud_representation") as mock_fetch:
            result = bridge.query(h_local, "hopfield enabled")

        mock_fetch.assert_not_called()
        assert torch.allclose(result, h_local * 1.4)
        assert bridge.cache.last_hopfield_status["accepted"] is True


class TestQueryGeminiPathway:
    """Test the Gemini embedding API pathway."""

    def test_success(self, setup_bridge):
        bridge = setup_bridge
        bridge.pathway = "gemini"
        cloud_dim = bridge.aligner.cloud_dim

        mock_resp_dict = {"embedding": {"values": [0.5] * cloud_dim}}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_resp_dict).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        h_local = torch.ones(1, bridge.adapter.dim)

        with patch(_URLOPEN_PATH, return_value=mock_response) as mock_urlopen:
            h_corrected = bridge.query(h_local, "test context")
            mock_urlopen.assert_called_once()
            assert h_corrected.shape == h_local.shape
            assert not torch.allclose(h_local, h_corrected)


class TestFallback:
    """Verify graceful degradation on network/parsing failures."""

    def test_timeout_fallback(self, setup_bridge):
        h_local = torch.ones(1, setup_bridge.adapter.dim)
        with patch(_URLOPEN_PATH, side_effect=urllib.error.URLError("timeout")):
            h_corrected = setup_bridge.query(h_local, "test context")
            assert torch.allclose(h_local, h_corrected)

    def test_invalid_json_fallback(self, setup_bridge):
        h_local = torch.ones(1, setup_bridge.adapter.dim)

        mock_response = MagicMock()
        mock_response.read.return_value = b"invalid json data"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch(_URLOPEN_PATH, return_value=mock_response):
            h_corrected = setup_bridge.query(h_local, "test context")
            assert torch.allclose(h_local, h_corrected)

    def test_connection_refused_fallback(self, setup_bridge):
        h_local = torch.ones(1, setup_bridge.adapter.dim)
        with patch(_URLOPEN_PATH, side_effect=ConnectionRefusedError("refused")):
            h_corrected = setup_bridge.query(h_local, "test context")
            assert torch.allclose(h_local, h_corrected)


class TestMockMode:
    """Verify offline mock mode produces deterministic corrections."""

    def test_mock_mode_produces_correction(self, setup_bridge):
        bridge = setup_bridge
        bridge.mock_mode = True
        h_local = torch.ones(1, bridge.adapter.dim)

        h_corrected = bridge.query(h_local, "test context")
        assert h_corrected.shape == h_local.shape
        assert not torch.allclose(h_local, h_corrected)

    def test_mock_mode_deterministic(self, setup_bridge):
        """Verify mock mode produces the same result across calls."""
        bridge = setup_bridge
        bridge.mock_mode = True
        h_local = torch.ones(1, bridge.adapter.dim)

        h1 = bridge.query(h_local, "test context")
        h2 = bridge.query(h_local, "test context")
        assert torch.allclose(h1, h2), "Mock mode should be deterministic"


class TestDriftGuardIntegration:
    def test_active_drift_guard_suppresses_cache_fast_path(self, setup_bridge):
        bridge = setup_bridge
        h_local = torch.ones(1, bridge.adapter.dim)
        bridge.cache.insert(h_local, h_local * 2.0, "cached")
        assert bridge.cache.check_cache(h_local) is not None

        _activate_drift_guard(bridge)
        assert bridge.drift_status["active"] is True

        cloud_state = np.ones(bridge.aligner.cloud_dim, dtype=np.float32)
        with patch.object(bridge, "_fetch_cloud_representation", return_value=cloud_state) as mock_fetch:
            result = bridge.query(h_local, "cached context")

        mock_fetch.assert_called_once()
        assert result.shape == h_local.shape

import json
import os
import socket
import urllib.error
import urllib.request
import logging
import hashlib
import threading

import numpy as np
import torch
import asyncio

from .adapter import CayleyPrivacyAdapter
from .alignment import ProcrustesAligner
from .exceptions import ConfigurationError
from .scrubber import PiiScrubber
from .cache import ActivationCache
from .routing import MoCRouter
from .distill import CorrectionHead
from .temporal import TemporalStateBuffer, QueryState
from .quant import LeechLatticeQuantizer
from .routing import calculate_activation_entropy

logger = logging.getLogger("latticeshadow_db")

class ZkBridge:
    """
    Zero-Knowledge Activation Privacy Proxy and Heterogeneous Cross-Model Alignment Bridge.

    Integrates the CayleyPrivacyAdapter and ProcrustesAligner with:
    - Speculative Procrustes prediction (predict cloud response before asking)
    - Adaptive alpha blending (residual-aware dynamic coefficients)
    - FAISS-backed activation caching with entropy-augmented keys
    - Cayley subspace compression for bandwidth reduction
    - Online distillation via a learned correction head
    """
    def __init__(
        self,
        adapter: CayleyPrivacyAdapter,
        aligner: ProcrustesAligner,
        api_endpoint: str = None,
        api_key: str = None,
        pathway: str = "gemini",  # "gemini" or "custom"
        alpha: float = 0.3,
        timeout: float = 2.0,
        mock_mode: bool = False,
        max_queries_per_session: int = 1000,
        scrub_pii: bool = False,
        # === New Feature Flags ===
        enable_speculation: bool = True,
        speculative_threshold: float = 0.05,
        adaptive_alpha: bool = True,
        compress_transit: bool = False,
        enable_distillation: bool = False,
        distill_min_samples: int = 500,
        distill_loss_threshold: float = 0.01,
        cache_db_path: str = "activation_cache.db",
        cache_encryption_key: str = None,
        allow_plaintext_cache: bool = False,
        enable_hopfield_cache: bool = False,
        hopfield_beta: float = 8.0,
        hopfield_min_confidence: float = 0.70,
        send_text_context: bool = True,
        enable_routing: bool = True,
        enable_temporal: bool = True,
        temporal_window: int = 16,
        use_leech_lattice: bool = False,
    ):
        if pathway not in ("gemini", "custom"):
            raise ConfigurationError(f"pathway must be 'gemini' or 'custom', got '{pathway}'")
        if not (0.0 <= alpha <= 1.0):
            raise ConfigurationError(f"alpha mixing coefficient must be between 0.0 and 1.0, got {alpha}")
        if pathway == "custom" and not mock_mode and not api_endpoint:
            raise ConfigurationError("api_endpoint must be specified when using 'custom' pathway")
        if max_queries_per_session <= 0:
            raise ConfigurationError(f"max_queries_per_session must be positive, got {max_queries_per_session}")
        if hopfield_beta <= 0.0:
            raise ConfigurationError(f"hopfield_beta must be positive, got {hopfield_beta}")
        if not (0.0 <= hopfield_min_confidence <= 1.0):
            raise ConfigurationError(
                f"hopfield_min_confidence must be between 0.0 and 1.0, got {hopfield_min_confidence}"
            )

        self.adapter = adapter
        self.aligner = aligner
        self.api_endpoint = api_endpoint
        self.api_key = api_key
        self.pathway = pathway
        self.alpha = alpha
        self.timeout = timeout
        self.mock_mode = mock_mode
        self.max_queries_per_session = max_queries_per_session
        self._query_count = 0
        self.scrub_pii = scrub_pii
        self.scrubber = PiiScrubber() if scrub_pii else None
        self.send_text_context = send_text_context

        # Feature: Activation Cache (FAISS + entropy-augmented)
        effective_cache_key = (
            cache_encryption_key
            or os.environ.get("LATTICESHADOW_CACHE_KEY")
            or os.environ.get("LATTICEDB_CACHE_KEY")
            or api_key
        )
        self.cache = ActivationCache(
            db_path=cache_db_path,
            encryption_key=effective_cache_key,
            allow_plaintext=allow_plaintext_cache or mock_mode,
        )
        self.enable_hopfield_cache = enable_hopfield_cache
        self.hopfield_beta = hopfield_beta
        self.hopfield_min_confidence = hopfield_min_confidence

        # Feature: Entropy Router
        self.enable_routing = enable_routing
        self.router = MoCRouter()

        # Feature: Speculative Procrustes
        self.enable_speculation = enable_speculation
        self.speculative_threshold = speculative_threshold

        # Feature: Adaptive Alpha
        self.adaptive_alpha = adaptive_alpha
        self._last_adaptive_alpha = alpha  # Observable health metric

        # Feature: Cayley Subspace Compression
        self.compress_transit = compress_transit

        # Feature: Leech Lattice Quantization
        self.use_leech_lattice = use_leech_lattice
        self.leech_quantizer = LeechLatticeQuantizer() if use_leech_lattice else None

        # Feature: Online Distillation
        self.enable_distillation = enable_distillation
        self.distill_min_samples = distill_min_samples
        self.distill_loss_threshold = distill_loss_threshold
        self._correction_head = None
        self._distill_insert_count = 0
        if enable_distillation:
            self._correction_head = CorrectionHead(input_dim=adapter.dim)

        # Feature: Temporal Coherence Memory
        self.enable_temporal = enable_temporal
        self._temporal = TemporalStateBuffer(window_size=temporal_window) if enable_temporal else None

        # Feature: Passive Drift Monitoring Sliding Window
        import collections
        self.query_similarities = collections.deque(maxlen=20)
        self._similarity_lock = threading.Lock()
        self._calibration_baseline = None
        self._last_drift_alert_time = 0.0
        self._drift_alert_cooldown = 3600.0  # 1 hour cooldown


        logger.debug(
            "Initialized ZkBridge with pathway=%s, alpha=%s, mock_mode=%s, "
            "max_queries_per_session=%d, scrub_pii=%s, speculation=%s, "
            "adaptive_alpha=%s, compress=%s, distillation=%s, temporal=%s, "
            "hopfield_cache=%s",
            pathway, alpha, mock_mode, max_queries_per_session, scrub_pii,
            enable_speculation, adaptive_alpha, compress_transit, enable_distillation,
            enable_temporal, enable_hopfield_cache
        )

    @property
    def temporal(self) -> 'TemporalStateBuffer':
        """Read-only access to the temporal state buffer for health monitoring."""
        return self._temporal

    @property
    def drift_status(self) -> dict:
        """Current residual drift guard status."""
        if not self.enable_temporal or self._temporal is None:
            return {"active": False, "reason": "disabled"}
        return self._temporal.drift_status

    def _drift_guard_active(self) -> bool:
        """True when local cloud-skip shortcuts should be suppressed."""
        return bool(self.enable_temporal and self._temporal is not None and self._temporal.drift_active)

    def _record_temporal(
        self,
        local_state: torch.Tensor,
        aligned_state: torch.Tensor,
        alpha: float,
        route: str,
    ) -> None:
        """Record a query snapshot in the temporal buffer."""
        if not self.enable_temporal or self._temporal is None:
            return

        try:
            entropy = calculate_activation_entropy(local_state)
            residual = 0.0
            if aligned_state is not None:
                norm = torch.norm(local_state)
                if norm.item() > 1e-8:
                    residual = (torch.norm(aligned_state - local_state) / norm).item()

            state = QueryState(
                local_state=local_state.detach(),
                aligned_state=aligned_state.detach() if aligned_state is not None else None,
                residual=residual,
                adaptive_alpha=alpha,
                entropy=entropy,
                route=route,
            )
            self._temporal.record(state)
        except Exception as e:
            logger.debug("Failed to record temporal state: %s", e)

    def _record_similarity_metric(self, local_state: torch.Tensor, aligned_state: torch.Tensor) -> None:
        """Maintain passive cosine similarity telemetry without external notifiers."""
        try:
            with torch.no_grad():
                flat_local = local_state.detach().flatten()
                flat_aligned = aligned_state.detach().flatten()
                dot_val = torch.dot(flat_local, flat_aligned)
                norm_local = torch.norm(flat_local)
                norm_aligned = torch.norm(flat_aligned)
                sim = (dot_val / (norm_local * norm_aligned + 1e-8)).item()

            with self._similarity_lock:
                self.query_similarities.append(sim)

                if len(self.query_similarities) == self.query_similarities.maxlen:
                    running_avg = sum(self.query_similarities) / len(self.query_similarities)
                    if self._calibration_baseline is None:
                        self._calibration_baseline = running_avg
                        logger.info(
                            "Established passive monitoring calibration baseline: %.4f",
                            self._calibration_baseline,
                        )
                    elif self._calibration_baseline > 1e-5 and running_avg < self._calibration_baseline * 0.85:
                        degrade_pct = (
                            (self._calibration_baseline - running_avg) / self._calibration_baseline
                        ) * 100
                        logger.warning(
                            "Passive similarity degraded below baseline: current=%.4f "
                            "baseline=%.4f drop=%.1f%%",
                            running_avg,
                            self._calibration_baseline,
                            degrade_pct,
                        )
        except Exception as ex:
            logger.debug("Failed to compute passive similarity drift: %s", ex)

    def should_trigger(self, cfi: float, soft_threshold: float = 0.8) -> bool:
        """
        Divergence check based on attention metrics/confidence score.
        Triggers correction if cfi (confidence score) is below soft_threshold.
        """
        return cfi < soft_threshold

    def _adaptive_blend(self, local: torch.Tensor, aligned: torch.Tensor) -> tuple:
        """
        Blend local and aligned tensors with residual-adaptive alpha.
        When the aligned vector is close to local, trust it more (higher alpha).
        When it diverges wildly, trust local more (lower alpha).

        Returns: (blended_tensor, effective_alpha)
        """
        if not self.adaptive_alpha:
            h_corrected = (1.0 - self.alpha) * local + self.alpha * aligned
            self._last_adaptive_alpha = self.alpha
            return h_corrected, self.alpha

        residual_norm = torch.norm(aligned - local) / (torch.norm(local) + 1e-8)
        effective_alpha = self.alpha * torch.sigmoid(1.0 - residual_norm).item()
        self._last_adaptive_alpha = effective_alpha

        h_corrected = (1.0 - effective_alpha) * local + effective_alpha * aligned
        logger.debug("Adaptive blend: residual_norm=%.4f, effective_alpha=%.4f (base=%.4f)",
                     residual_norm.item(), effective_alpha, self.alpha)
        return h_corrected, effective_alpha

    def query(self, local_hidden_state: torch.Tensor, text_context: str) -> torch.Tensor:
        """
        Executes the full obfuscated query cycle with all advanced features:

        0.  Cache check (FAISS + entropy-augmented)
        0.1 Distillation head fast-path (if trained)
        0.2 Entropy routing (local/flash/ultra)
        0.3 Speculative Procrustes prediction (early exit if validated)
        1.  Cayley obfuscation (with optional subspace compression)
        2.  Cloud API call
        3.  Procrustes alignment
        3.5 Cache insertion + distillation retraining trigger
        4.  Adaptive alpha blending

        Falls back gracefully to the uncorrected local state if any step fails.
        """
        try:
            logger.debug("Starting ZK Query. Input shape: %s", local_hidden_state.shape)
            drift_guard_active = self._drift_guard_active()
            if drift_guard_active:
                logger.debug("Residual drift guard active; suppressing cache/distill/local exits.")

            # ── Step 0: Cache Check ──────────────────────────────────────
            if not drift_guard_active:
                cached_aligned = self.cache.check_cache(local_hidden_state)
                if cached_aligned is None and self.enable_hopfield_cache:
                    cached_aligned = self.cache.hopfield_readout(
                        local_hidden_state,
                        beta=self.hopfield_beta,
                        min_confidence=self.hopfield_min_confidence,
                    )
                    if cached_aligned is not None:
                        logger.debug(
                            "Hopfield cache readout accepted. status=%s",
                            self.cache.last_hopfield_status,
                        )
                if cached_aligned is not None:
                    logger.debug("Cache hit for input tensor. Bypassing cloud transit.")
                    if cached_aligned.shape != local_hidden_state.shape:
                        if len(cached_aligned.shape) == 1 and len(local_hidden_state.shape) > 1:
                            cached_aligned = cached_aligned.expand_as(local_hidden_state)
                    h_corrected, eff_alpha = self._adaptive_blend(local_hidden_state, cached_aligned)
                    self._record_temporal(local_hidden_state, cached_aligned, eff_alpha, "cache")
                    return h_corrected

            # ── Step 0.1: Distillation Head Fast-Path ────────────────────
            if (not drift_guard_active
                    and self.enable_distillation
                    and self._correction_head is not None
                    and self._correction_head.is_trained
                    and self._correction_head.training_loss < self.distill_loss_threshold):
                predicted = self._correction_head.predict(local_hidden_state)
                if predicted.shape != local_hidden_state.shape:
                    if predicted.numel() == local_hidden_state.numel():
                        predicted = predicted.view(local_hidden_state.shape)
                h_corrected, eff_alpha = self._adaptive_blend(local_hidden_state, predicted)
                logger.debug("Distillation head fast-path. Loss: %.6f, alpha: %.4f",
                             self._correction_head.training_loss, eff_alpha)
                self._record_temporal(local_hidden_state, predicted, eff_alpha, "distilled")
                return h_corrected

            # ── Step 0.2: Entropy Routing ────────────────────────────────
            if self.enable_routing and not drift_guard_active:
                route = self.router.route(local_hidden_state)
                if route == "local":
                    logger.debug("Low entropy detected. Routing strictly locally.")
                    self._record_temporal(local_hidden_state, None, self.alpha, "local")
                    return local_hidden_state

            # ── Step 0.3: Temporal Momentum Pre-Correction ────────────────
            spec_input = local_hidden_state
            if self.enable_temporal and self._temporal is not None and self._temporal.size >= 3:
                spec_input = self._temporal.suggest_momentum_correction(local_hidden_state)

            # ── Step 0.4: Speculative Procrustes Prediction ──────────────
            if self.enable_speculation and self.aligner.is_calibrated:
                # Use temporally-adjusted threshold if available
                effective_threshold = self.speculative_threshold
                if self.enable_temporal and self._temporal is not None:
                    effective_threshold = self._temporal.suggest_speculative_threshold(
                        self.speculative_threshold
                    )

                predicted_cloud = self.aligner.predict_cloud(spec_input)
                predicted_aligned = self.aligner.align(predicted_cloud)

                if isinstance(predicted_aligned, np.ndarray):
                    predicted_aligned = torch.from_numpy(predicted_aligned).to(
                        device=local_hidden_state.device, dtype=local_hidden_state.dtype
                    )
                else:
                    predicted_aligned = predicted_aligned.to(
                        device=local_hidden_state.device, dtype=local_hidden_state.dtype
                    )

                local_norm = torch.norm(local_hidden_state)
                residual = torch.norm(predicted_aligned - local_hidden_state) / (local_norm + 1e-8)

                if residual.item() < effective_threshold:
                    logger.debug(
                        "Speculative validation passed (residual=%.4f < threshold=%.4f%s). "
                        "Cloud not needed.", residual.item(), effective_threshold,
                        " [temporal]" if effective_threshold != self.speculative_threshold else ""
                    )
                    self._record_temporal(local_hidden_state, predicted_aligned, self.alpha, "speculative")
                    return local_hidden_state
                else:
                    logger.debug(
                        "Speculative residual %.4f >= threshold %.4f. Proceeding to cloud.",
                        residual.item(), self.speculative_threshold
                    )

            # ── Step 1: Obfuscation ──────────────────────────────────────
            h_rotated = self.adapter.rotate(local_hidden_state)
            logger.debug("Obfuscated hidden state shape: %s", h_rotated.shape)

            # ── Step 1.5: Optional Subspace Compression ──────────────────
            if self.compress_transit and self.pathway == "custom":
                h_transit = self.adapter.compress(h_rotated)
                logger.debug("Compressed for transit: %s → %s", h_rotated.shape, h_transit.shape)
            else:
                h_transit = h_rotated

            # ── Step 1.7: Optional Leech Lattice Quantization ────────────
            if self.use_leech_lattice:
                h_transit, _ = self.leech_quantizer.quantize(h_transit)
                logger.debug("Leech Lattice quantized shape: %s", h_transit.shape)

            # ── Step 2: Offload to cloud ─────────────────────────────────
            transit_text_context = text_context if self.send_text_context else ""
            cloud_state = self._fetch_cloud_representation(h_transit, transit_text_context)
            if cloud_state is None:
                logger.warning(
                    "Cloud representation retrieval failed or timed out. "
                    "Falling back to uncorrected state."
                )
                return local_hidden_state

            logger.debug("Retrieved cloud representation shape: %s", cloud_state.shape)

            # ── Step 3: Alignment ────────────────────────────────────────
            h_aligned = self.aligner.align(cloud_state)
            logger.debug("Aligned representation shape: %s", h_aligned.shape)

            # Convert h_aligned to PyTorch tensor matching local state device/dtype
            if isinstance(h_aligned, torch.Tensor):
                h_aligned_torch = h_aligned.to(
                    device=local_hidden_state.device,
                    dtype=local_hidden_state.dtype,
                )
            else:
                h_aligned_torch = torch.from_numpy(h_aligned).to(
                    device=local_hidden_state.device,
                    dtype=local_hidden_state.dtype,
                )

            # ── Step 3.5: Cache insertion ────────────────────────────────
            prompt_hash = hashlib.sha256(text_context.encode('utf-8')).hexdigest() if text_context else "default"
            self.cache.insert(local_hidden_state, h_aligned_torch, prompt_hash)

            # ── Step 3.6: Distillation retraining trigger ────────────────
            if self.enable_distillation and self._correction_head is not None:
                self._distill_insert_count += 1
                if (self._distill_insert_count >= self.distill_min_samples
                        and self._distill_insert_count % self.distill_min_samples == 0):
                    logger.info("Triggering distillation head retraining (%d samples).",
                                len(self.cache.keys))
                    # Train in a background thread to avoid blocking inference
                    thread = threading.Thread(
                        target=self._correction_head.train_from_cache,
                        args=(self.cache,),
                        daemon=True
                    )
                    thread.start()

            # ── Step 4: Adaptive Blending ────────────────────────────────
            if h_aligned_torch.shape != local_hidden_state.shape:
                if len(h_aligned_torch.shape) == 1 and len(local_hidden_state.shape) > 1:
                    h_aligned_torch = h_aligned_torch.expand_as(local_hidden_state)

            h_corrected, eff_alpha = self._adaptive_blend(local_hidden_state, h_aligned_torch)
            logger.debug("Blending complete. Corrected output shape: %s (alpha=%.4f)",
                         h_corrected.shape, eff_alpha)

            # Record temporal state
            self._record_temporal(local_hidden_state, h_aligned_torch, eff_alpha, "cloud")
            self._record_similarity_metric(local_hidden_state, h_aligned_torch)

            # Increment and check key cycle count
            self._query_count += 1
            if self._query_count >= self.max_queries_per_session:
                logger.info(
                    "Session query limit reached (%d). Regenerating keys for forward secrecy.",
                    self.max_queries_per_session
                )
                self.adapter.regenerate()
                self._query_count = 0

            return h_corrected

        except (urllib.error.URLError, socket.timeout, json.JSONDecodeError,
                KeyError, OSError, ConnectionError) as e:
            logger.error(
                "Error in ZkBridge query pipeline: %s. Falling back to uncorrected state.", e
            )
            return local_hidden_state

    async def query_async(self, local_hidden_state: torch.Tensor, text_context: str) -> torch.Tensor:
        """
        Asynchronously executes the full query cycle using asyncio.to_thread to run
        blocking operations in a worker thread.
        """
        return await asyncio.to_thread(self.query, local_hidden_state, text_context)

    def _fetch_cloud_representation(self, h_rotated: torch.Tensor, text_context: str) -> np.ndarray:
        """
        Fetches the cloud model representation via Pathway 1 (Custom) or Pathway 2 (Gemini).
        Supports mock_mode for testing and validation.
        """
        if self.scrub_pii:
            orig = text_context
            text_context = self.scrubber.scrub(text_context)
            if text_context != orig:
                logger.debug("PII Redacted from context: '%s'", text_context)

        if self.mock_mode:
            if self.use_leech_lattice:
                target_dim = (2 * self.adapter.rank) if self.compress_transit else self.adapter.dim
                h_rotated = h_rotated[..., :target_dim]
            if self.compress_transit:
                h_rotated = self.adapter.decompress(h_rotated)
            return self._mock_cloud_response(h_rotated)

        if self.pathway == "custom":
            return self._fetch_custom_pathway(h_rotated, text_context)
        elif self.pathway == "gemini":
            return self._fetch_gemini_pathway(text_context)
        else:
            raise ValueError(f"Unknown pathway: {self.pathway}")

    def _fetch_custom_pathway(self, h_rotated: torch.Tensor, text_context: str) -> np.ndarray:
        """Pathway 1: Sends the scrambled tensor directly to a custom backend URL."""
        if not self.api_endpoint:
            raise ValueError("api_endpoint must be set for 'custom' pathway")

        # Convert rotated hidden state to a standard numpy/list format for JSON serialization
        h_rotated_np = h_rotated.detach().cpu().numpy()

        payload = {
            "obfuscated_state": h_rotated_np.tolist(),
            "text_context": text_context
        }

        headers = {
            "Content-Type": "application/json"
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(
            url=self.api_endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                resp_data = json.loads(response.read().decode("utf-8"))
                cloud_state_list = resp_data.get("cloud_state")
                if cloud_state_list is None:
                    raise KeyError("Response JSON missing 'cloud_state'")
                return np.array(cloud_state_list, dtype=np.float32)
        except Exception as e:
            logger.warning(f"Custom pathway request failed: {e}")
            return None

    def _fetch_gemini_pathway(self, text_context: str) -> np.ndarray:
        """Pathway 2: Queries the Gemini embedding API to fetch cloud representation."""
        endpoint = self.api_endpoint
        if not endpoint:
            # Default to public Google AI Studio Gemini API endpoint
            model = "gemini-embedding-001"
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"

        if self.api_key:
            url = f"{endpoint}?key={self.api_key}"
        else:
            url = endpoint

        payload = {
            "content": {
                "parts": [
                    {"text": text_context}
                ]
            }
        }

        headers = {
            "Content-Type": "application/json"
        }

        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                resp_data = json.loads(response.read().decode("utf-8"))
                # Standard Google Gemini embedContent JSON path: embedding.values
                embedding = resp_data.get("embedding", {})
                values = embedding.get("values")
                if values is None:
                    raise KeyError("Response JSON missing 'embedding.values'")
                return np.array(values, dtype=np.float32)
        except Exception as e:
            logger.warning(f"Gemini API request failed: {e}")
            return None

    def _mock_cloud_response(self, h_rotated: torch.Tensor) -> np.ndarray | torch.Tensor:
        """Generates a mock cloud state tensor of appropriate shape for testing/offline mode."""
        if isinstance(h_rotated, torch.Tensor):
            # Perform mock response generation natively in PyTorch to avoid host-device transfers
            mock_proj = torch.eye(self.adapter.dim, self.aligner.cloud_dim, device=h_rotated.device, dtype=h_rotated.dtype)
            mock_cloud = torch.matmul(h_rotated, mock_proj)
            try:
                gen = torch.Generator(device=h_rotated.device)
                gen.manual_seed(0)
                noise = torch.randn(mock_cloud.shape, device=h_rotated.device, dtype=h_rotated.dtype, generator=gen) * 0.01
            except Exception:
                gen = torch.Generator()
                gen.manual_seed(0)
                noise = torch.randn(mock_cloud.shape, generator=gen).to(device=h_rotated.device, dtype=h_rotated.dtype) * 0.01
            return mock_cloud + noise

        h_rotated_np = h_rotated.detach().cpu().numpy()

        # Deterministic identity-like projection to cloud_dim
        mock_proj = np.eye(self.adapter.dim, self.aligner.cloud_dim)
        mock_cloud = h_rotated_np @ mock_proj

        # Add small seeded noise for reproducibility
        rng = np.random.default_rng(seed=0)
        mock_cloud += rng.normal(0.0, 0.01, size=mock_cloud.shape)
        return mock_cloud.astype(np.float32)

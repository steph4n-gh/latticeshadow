"""Orthogonal Procrustes Space Translation for heterogeneous cross-model alignment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

import numpy as np
import torch

from .exceptions import CalibrationError


class ProcrustesAligner:
    """
    Orthogonal Procrustes Aligner to translate representation vectors between
    heterogeneous models (e.g., cloud space to local space) using SVD.
    """

    def __init__(self, local_dim: int, cloud_dim: int):
        self.local_dim = local_dim
        self.cloud_dim = cloud_dim

        self.mu_local: np.ndarray | None = None
        self.mu_cloud: np.ndarray | None = None
        self.Q: np.ndarray | None = None  # Alignment projection matrix: (cloud_dim, local_dim)
        self.is_calibrated: bool = False

    def calibrate(self, local_states: np.ndarray | torch.Tensor, cloud_states: np.ndarray | torch.Tensor) -> dict[str, float]:
        """
        Calibrates the aligner using paired local and cloud representations.
        Computes covariance matrix C = A^T B, performs SVD, calculates
        projection matrix Q = V U^T, saves translation biases, and returns
        SVD-Procrustes alignment quality and drift metrics.

        .. note::
            The returned correlation score is computed **in-sample** on the
            calibration data itself and will overestimate real-world quality.
            Use :meth:`score` with held-out data for an honest evaluation.

        Returns:
            A dictionary containing:
                "entropy": Shannon Entropy of singular values
                "condition_number": Condition Number of singular values
                "trace": Trace of alignment projection matrix Q
                "mse": Mean Squared Error (Geometric Residuals)
                "correlation": Average cosine similarity alignment correlation
        """
        if isinstance(local_states, torch.Tensor):
            if not torch.is_floating_point(local_states):
                local_states = local_states.to(torch.float32)
            max_val = 1e9
            if local_states.dtype in (torch.float16, torch.bfloat16):
                max_val = min(1e9, float(torch.finfo(local_states.dtype).max))
            local_states = torch.nan_to_num(local_states, nan=0.0, posinf=max_val, neginf=-max_val)
        else:
            local_states = np.asarray(local_states)
            if not np.issubdtype(local_states.dtype, np.floating):
                local_states = local_states.astype(np.float32)
            max_val = 1e9
            if np.issubdtype(local_states.dtype, np.floating):
                max_val = min(1e9, float(np.finfo(local_states.dtype).max))
            local_states = np.nan_to_num(local_states, nan=0.0, posinf=max_val, neginf=-max_val)

        if isinstance(cloud_states, torch.Tensor):
            if not torch.is_floating_point(cloud_states):
                cloud_states = cloud_states.to(torch.float32)
            max_val = 1e9
            if cloud_states.dtype in (torch.float16, torch.bfloat16):
                max_val = min(1e9, float(torch.finfo(cloud_states.dtype).max))
            cloud_states = torch.nan_to_num(cloud_states, nan=0.0, posinf=max_val, neginf=-max_val)
        else:
            cloud_states = np.asarray(cloud_states)
            if not np.issubdtype(cloud_states.dtype, np.floating):
                cloud_states = cloud_states.astype(np.float32)
            max_val = 1e9
            if np.issubdtype(cloud_states.dtype, np.floating):
                max_val = min(1e9, float(np.finfo(cloud_states.dtype).max))
            cloud_states = np.nan_to_num(cloud_states, nan=0.0, posinf=max_val, neginf=-max_val)

        if local_states.shape[0] == 0:
            raise CalibrationError("Number of calibration samples must be positive")

        if local_states.shape[0] != cloud_states.shape[0]:
            raise CalibrationError(
                f"Number of calibration samples must match: "
                f"{local_states.shape[0]} vs {cloud_states.shape[0]}"
            )

        # Ensure dimensions match
        if local_states.shape[1] != self.local_dim:
            raise CalibrationError(
                f"local_states dimension {local_states.shape[1]} "
                f"does not match local_dim {self.local_dim}"
            )
        if cloud_states.shape[1] != self.cloud_dim:
            raise CalibrationError(
                f"cloud_states dimension {cloud_states.shape[1]} "
                f"does not match cloud_dim {self.cloud_dim}"
            )

        if isinstance(local_states, torch.Tensor):
            target_device = local_states.device
            target_dtype = local_states.dtype
            upcast = target_dtype in (torch.float16, torch.bfloat16)

            local_up = local_states.to(dtype=torch.float32) if upcast else local_states
            cloud_up = cloud_states.to(dtype=torch.float32) if upcast else cloud_states

            # Compute means
            mu_local_up = torch.mean(local_up, dim=0)
            mu_cloud_up = torch.mean(cloud_up, dim=0)

            # Center both matrices
            A = local_up - mu_local_up
            B = cloud_up - mu_cloud_up

            # Compute cross-covariance matrix: C = A^T B
            C = torch.matmul(A.t(), B)

            # Singular Value Decomposition: C = U S V^T
            u, s, vh = torch.linalg.svd(C, full_matrices=False)

            # Q = V U^T
            Q_up = torch.matmul(vh.t(), u.t())

            # Cast parameters back to target precision
            self.mu_local = mu_local_up.to(dtype=target_dtype, device=target_device)
            self.mu_cloud = mu_cloud_up.to(dtype=target_dtype, device=target_device)
            self.Q = Q_up.to(dtype=target_dtype, device=target_device)

            # Calculate Shannon Entropy: H = -sum(p_i * log(p_i)) where p_i = s_i / sum(s_j)
            s_sum = torch.sum(s)
            if s_sum > 0.0:
                p_i = s / s_sum
                p_i_clamped = torch.clamp(p_i, min=1e-12)
                entropy_val = float(-torch.sum(p_i_clamped * torch.log(p_i_clamped)).item())
            else:
                entropy_val = 0.0

            # Calculate Condition Number: kappa = s_max / s_min. Clamp s_min to at least 1e-9.
            s_max = torch.max(s)
            s_min = torch.min(s)
            s_min_clamped = torch.clamp(s_min, min=1e-9)
            cond_val = float((s_max / s_min_clamped).item())

            # Calculate Q-Matrix Trace
            trace_val = float(torch.diagonal(Q_up).sum().item())
        else:
            local_states = np.asarray(local_states)
            cloud_states = np.asarray(cloud_states)
            target_dtype = local_states.dtype
            upcast = target_dtype == np.float16

            local_up = local_states.astype(np.float32) if upcast else local_states
            cloud_up = cloud_states.astype(np.float32) if upcast else cloud_states

            # Compute means
            mu_local_up = np.mean(local_up, axis=0)
            mu_cloud_up = np.mean(cloud_up, axis=0)

            # Center both matrices
            A = local_up - mu_local_up
            B = cloud_up - mu_cloud_up

            # Compute cross-covariance matrix: C = A^T B
            C = A.T @ B

            # Singular Value Decomposition: C = U S V^T
            u, s, vh = np.linalg.svd(C, full_matrices=False)

            # Q = V U^T
            Q_up = vh.T @ u.T

            # Cast parameters back to target precision
            self.mu_local = mu_local_up.astype(target_dtype)
            self.mu_cloud = mu_cloud_up.astype(target_dtype)
            self.Q = Q_up.astype(target_dtype)

            # Calculate Shannon Entropy: H = -sum(p_i * log(p_i)) where p_i = s_i / sum(s_j)
            s_sum = np.sum(s)
            if s_sum > 0.0:
                p_i = s / s_sum
                p_i_clamped = np.clip(p_i, a_min=1e-12, a_max=None)
                entropy_val = float(-np.sum(p_i_clamped * np.log(p_i_clamped)))
            else:
                entropy_val = 0.0

            # Calculate Condition Number: kappa = s_max / s_min. Clamp s_min to at least 1e-9.
            s_max = np.max(s)
            s_min = np.min(s)
            s_min_clamped = np.maximum(s_min, 1e-9)
            cond_val = float(s_max / s_min_clamped)

            # Calculate Q-Matrix Trace
            trace_val = float(np.trace(Q_up))

        self.is_calibrated = True

        # Calculate alignment metrics on calibration set (in-sample)
        aligned_states = self.align(cloud_states)
        correlation = self._cosine_similarity(local_states, aligned_states)

        # Calculate Geometric Residuals (MSE): MSE = mean((local_states - aligned_states) ** 2)
        if isinstance(local_states, torch.Tensor):
            local_states_aligned_device = local_states.to(device=aligned_states.device, dtype=aligned_states.dtype)
            mse_val = float(torch.mean((local_states_aligned_device - aligned_states) ** 2).item())
        else:
            mse_val = float(np.mean((local_states.astype(aligned_states.dtype) - aligned_states) ** 2))

        return {
            "entropy": entropy_val,
            "condition_number": cond_val,
            "trace": trace_val,
            "mse": mse_val,
            "correlation": correlation,
        }

    def align(self, cloud_state: np.ndarray | torch.Tensor) -> np.ndarray | torch.Tensor:
        """
        Translates incoming cloud-space vectors into local-space vectors.
        h_aligned = (h_cloud - mu_cloud) Q + mu_local
        Supports 1D (vector), 2D (batch), or 3D (sequence-batch) shapes.
        """
        if not self.is_calibrated:
            raise ValueError("Aligner has not been calibrated. Call calibrate() first.")

        if isinstance(cloud_state, torch.Tensor):
            if not torch.is_floating_point(cloud_state):
                cloud_state = cloud_state.to(torch.float32)
            max_val = 1e9
            if cloud_state.dtype in (torch.float16, torch.bfloat16):
                max_val = min(1e9, float(torch.finfo(cloud_state.dtype).max))
            cloud_state = torch.nan_to_num(cloud_state, nan=0.0, posinf=max_val, neginf=-max_val)
            if isinstance(self.Q, np.ndarray):
                Q_t = torch.from_numpy(self.Q)
                mu_local_t = torch.from_numpy(self.mu_local)
                mu_cloud_t = torch.from_numpy(self.mu_cloud)
            else:
                Q_t = self.Q
                mu_local_t = self.mu_local
                mu_cloud_t = self.mu_cloud

            # Move/cast to input device
            target_device = torch.device(cloud_state.device)
            param_dtype = Q_t.dtype
            if target_device.type == "mps" and param_dtype == torch.float64:
                param_dtype = torch.float32

            Q_t = Q_t.to(device=target_device, dtype=param_dtype)
            mu_local_t = mu_local_t.to(device=target_device, dtype=param_dtype)
            mu_cloud_t = mu_cloud_t.to(device=target_device, dtype=param_dtype)

            # Upcast to float32 for math if low precision
            upcast = cloud_state.dtype in (torch.float16, torch.bfloat16)
            if upcast:
                cloud_state_32 = cloud_state.to(dtype=torch.float32)
                Q_t_32 = Q_t.to(dtype=torch.float32)
                mu_cloud_t_32 = mu_cloud_t.to(dtype=torch.float32)
                mu_local_t_32 = mu_local_t.to(dtype=torch.float32)

                centered = cloud_state_32 - mu_cloud_t_32
                projected = centered @ Q_t_32
                aligned_32 = projected + mu_local_t_32

                # Clamp to avoid overflow when casting back
                clamp_val = max_val * 0.999
                aligned_32 = torch.nan_to_num(aligned_32, nan=0.0, posinf=clamp_val, neginf=-clamp_val)
                aligned = aligned_32.to(dtype=cloud_state.dtype)
            else:
                Q_t = Q_t.to(dtype=cloud_state.dtype)
                mu_local_t = mu_local_t.to(dtype=cloud_state.dtype)
                mu_cloud_t = mu_cloud_t.to(dtype=cloud_state.dtype)

                centered = cloud_state - mu_cloud_t
                projected = centered @ Q_t
                aligned = projected + mu_local_t

            aligned = torch.nan_to_num(aligned, nan=0.0, posinf=max_val, neginf=-max_val)
            return aligned
        else:
            cloud_state = np.asarray(cloud_state)
            if not np.issubdtype(cloud_state.dtype, np.floating):
                cloud_state = cloud_state.astype(np.float32)
            max_val = 1e9
            if np.issubdtype(cloud_state.dtype, np.floating):
                max_val = min(1e9, float(np.finfo(cloud_state.dtype).max))
            cloud_state = np.nan_to_num(cloud_state, nan=0.0, posinf=max_val, neginf=-max_val)
            if isinstance(self.Q, torch.Tensor):
                Q_np = self.Q.detach().cpu().numpy()
                mu_local_np = self.mu_local.detach().cpu().numpy()
                mu_cloud_np = self.mu_cloud.detach().cpu().numpy()
            else:
                Q_np = self.Q
                mu_local_np = self.mu_local
                mu_cloud_np = self.mu_cloud

            upcast = cloud_state.dtype == np.float16
            if upcast:
                cloud_state_32 = cloud_state.astype(np.float32)
                Q_np_32 = Q_np.astype(np.float32)
                mu_cloud_np_32 = mu_cloud_np.astype(np.float32)
                mu_local_np_32 = mu_local_np.astype(np.float32)

                centered = cloud_state_32 - mu_cloud_np_32
                projected = centered @ Q_np_32
                aligned_32 = projected + mu_local_np_32

                clamp_val = max_val * 0.999
                aligned_32 = np.nan_to_num(aligned_32, nan=0.0, posinf=clamp_val, neginf=-clamp_val)
                aligned = aligned_32.astype(cloud_state.dtype)
            else:
                Q_np = Q_np.astype(cloud_state.dtype)
                mu_local_np = mu_local_np.astype(cloud_state.dtype)
                mu_cloud_np = mu_cloud_np.astype(cloud_state.dtype)

                centered = cloud_state - mu_cloud_np
                projected = centered @ Q_np
                aligned = projected + mu_local_np

            aligned = np.nan_to_num(aligned, nan=0.0, posinf=max_val, neginf=-max_val)
            return aligned

    def predict_cloud(self, local_state: np.ndarray | torch.Tensor) -> np.ndarray | torch.Tensor:
        """
        Speculative projection: local_space → predicted cloud_space via Q^T.
        h_predicted_cloud = (h_local - mu_local) Q^T + mu_cloud

        This is the inverse direction of align(). Used for speculative decoding:
        predict what the cloud model would return before actually querying it.
        """
        if not self.is_calibrated:
            raise ValueError("Aligner has not been calibrated. Call calibrate() first.")

        if isinstance(local_state, torch.Tensor):
            if not torch.is_floating_point(local_state):
                local_state = local_state.to(torch.float32)
            max_val = 1e9
            if local_state.dtype in (torch.float16, torch.bfloat16):
                max_val = min(1e9, float(torch.finfo(local_state.dtype).max))
            local_state = torch.nan_to_num(local_state, nan=0.0, posinf=max_val, neginf=-max_val)
            if isinstance(self.Q, np.ndarray):
                Q_t = torch.from_numpy(self.Q)
                mu_local_t = torch.from_numpy(self.mu_local)
                mu_cloud_t = torch.from_numpy(self.mu_cloud)
            else:
                Q_t = self.Q
                mu_local_t = self.mu_local
                mu_cloud_t = self.mu_cloud

            target_device = torch.device(local_state.device)
            param_dtype = Q_t.dtype
            if target_device.type == "mps" and param_dtype == torch.float64:
                param_dtype = torch.float32

            Q_t = Q_t.to(device=target_device, dtype=param_dtype)
            mu_local_t = mu_local_t.to(device=target_device, dtype=param_dtype)
            mu_cloud_t = mu_cloud_t.to(device=target_device, dtype=param_dtype)

            upcast = local_state.dtype in (torch.float16, torch.bfloat16)
            if upcast:
                local_state_32 = local_state.to(dtype=torch.float32)
                Q_t_32 = Q_t.to(dtype=torch.float32)
                mu_local_t_32 = mu_local_t.to(dtype=torch.float32)
                mu_cloud_t_32 = mu_cloud_t.to(dtype=torch.float32)

                centered = local_state_32 - mu_local_t_32
                # Q is (cloud_dim, local_dim), Q^T is (local_dim, cloud_dim)
                projected = centered @ Q_t_32.t()
                predicted_32 = projected + mu_cloud_t_32

                clamp_val = max_val * 0.999
                predicted_32 = torch.nan_to_num(predicted_32, nan=0.0, posinf=clamp_val, neginf=-clamp_val)
                predicted = predicted_32.to(dtype=local_state.dtype)
            else:
                Q_t = Q_t.to(dtype=local_state.dtype)
                mu_local_t = mu_local_t.to(dtype=local_state.dtype)
                mu_cloud_t = mu_cloud_t.to(dtype=local_state.dtype)

                centered = local_state - mu_local_t
                projected = centered @ Q_t.t()
                predicted = projected + mu_cloud_t

            predicted = torch.nan_to_num(predicted, nan=0.0, posinf=max_val, neginf=-max_val)
            return predicted
        else:
            local_state = np.asarray(local_state)
            if not np.issubdtype(local_state.dtype, np.floating):
                local_state = local_state.astype(np.float32)
            max_val = 1e9
            if np.issubdtype(local_state.dtype, np.floating):
                max_val = min(1e9, float(np.finfo(local_state.dtype).max))
            local_state = np.nan_to_num(local_state, nan=0.0, posinf=max_val, neginf=-max_val)
            if isinstance(self.Q, torch.Tensor):
                Q_np = self.Q.detach().cpu().numpy()
                mu_local_np = self.mu_local.detach().cpu().numpy()
                mu_cloud_np = self.mu_cloud.detach().cpu().numpy()
            else:
                Q_np = self.Q
                mu_local_np = self.mu_local
                mu_cloud_np = self.mu_cloud

            upcast = local_state.dtype == np.float16
            if upcast:
                local_state_32 = local_state.astype(np.float32)
                Q_np_32 = Q_np.astype(np.float32)
                mu_local_np_32 = mu_local_np.astype(np.float32)
                mu_cloud_np_32 = mu_cloud_np.astype(np.float32)

                centered = local_state_32 - mu_local_np_32
                projected = centered @ Q_np_32.T
                predicted_32 = projected + mu_cloud_np_32

                clamp_val = max_val * 0.999
                predicted_32 = np.nan_to_num(predicted_32, nan=0.0, posinf=clamp_val, neginf=-clamp_val)
                predicted = predicted_32.astype(local_state.dtype)
            else:
                Q_np = Q_np.astype(local_state.dtype)
                mu_local_np = mu_local_np.astype(local_state.dtype)
                mu_cloud_np = mu_cloud_np.astype(local_state.dtype)

                centered = local_state - mu_local_np
                projected = centered @ Q_np.T
                predicted = projected + mu_cloud_np

            predicted = np.nan_to_num(predicted, nan=0.0, posinf=max_val, neginf=-max_val)
            return predicted

    def score(self, local_holdout: np.ndarray | torch.Tensor, cloud_holdout: np.ndarray | torch.Tensor) -> float:
        """
        Evaluates alignment quality on held-out data not used during calibration.

        Args:
            local_holdout: Ground-truth local representations, shape (N, local_dim).
            cloud_holdout: Corresponding cloud representations, shape (N, cloud_dim).

        Returns:
            Average cosine similarity between aligned cloud states and local ground truth.
        """
        if not self.is_calibrated:
            raise ValueError("Aligner has not been calibrated. Call calibrate() first.")
        aligned = self.align(cloud_holdout)
        return self._cosine_similarity(local_holdout, aligned)

    def save(self, path: Union[str, Path]) -> None:
        """
        Saves the calibration parameters to a ``.npz`` file with a JSON sidecar
        for scalar metadata.  Uses only NumPy serialization — no pickle.
        """
        if not self.is_calibrated:
            raise ValueError("Cannot save uncalibrated parameters.")

        path = Path(path)

        # Detach and convert PyTorch parameters to CPU NumPy arrays
        Q_np = self.Q.detach().cpu().numpy() if isinstance(self.Q, torch.Tensor) else self.Q
        mu_local_np = self.mu_local.detach().cpu().numpy() if isinstance(self.mu_local, torch.Tensor) else self.mu_local
        mu_cloud_np = self.mu_cloud.detach().cpu().numpy() if isinstance(self.mu_cloud, torch.Tensor) else self.mu_cloud

        # Save arrays
        np.savez(
            path,
            Q=Q_np,
            mu_local=mu_local_np,
            mu_cloud=mu_cloud_np,
        )

        # Save scalar metadata as JSON sidecar
        meta_path = path.with_suffix(".meta.json")
        meta = {"local_dim": self.local_dim, "cloud_dim": self.cloud_dim}
        meta_path.write_text(json.dumps(meta))

    def load(self, path: Union[str, Path]) -> None:
        """
        Loads calibration parameters from a ``.npz`` + JSON sidecar pair
        previously written by :meth:`save`.
        """
        path = Path(path)

        data = np.load(path)
        self.Q = data["Q"]
        self.mu_local = data["mu_local"]
        self.mu_cloud = data["mu_cloud"]

        meta_path = path.with_suffix(".meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            self.local_dim = meta["local_dim"]
            self.cloud_dim = meta["cloud_dim"]
        else:
            # Infer from loaded arrays
            self.cloud_dim, self.local_dim = self.Q.shape

        self.is_calibrated = True

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> ProcrustesAligner:
        """
        Creates and calibrates a ProcrustesAligner directly from saved parameters.

        Args:
            path: Path to the saved ``.npz`` file.

        Returns:
            An initialized and calibrated ProcrustesAligner instance.
        """
        # Create an instance with dummy dims; load() will overwrite them
        aligner = cls(local_dim=1, cloud_dim=1)
        aligner.load(path)
        return aligner

    @staticmethod
    def _cosine_similarity(X: np.ndarray | torch.Tensor, Y: np.ndarray | torch.Tensor) -> float:
        """Computes the average cosine similarity between X and Y along the last axis."""
        if isinstance(X, torch.Tensor):
            dot_product = torch.sum(X * Y, dim=-1)
            norm_X = torch.linalg.norm(X, dim=-1)
            norm_Y = torch.linalg.norm(Y, dim=-1)

            norm_X = torch.where(norm_X == 0.0, 1e-9, norm_X)
            norm_Y = torch.where(norm_Y == 0.0, 1e-9, norm_Y)

            cosine_sim = dot_product / (norm_X * norm_Y)
            return float(torch.mean(cosine_sim).item())
        else:
            dot_product = np.sum(X * Y, axis=-1)
            norm_X = np.linalg.norm(X, axis=-1)
            norm_Y = np.linalg.norm(Y, axis=-1)

            norm_X = np.where(norm_X == 0, 1e-9, norm_X)
            norm_Y = np.where(norm_Y == 0, 1e-9, norm_Y)

            cosine_sim = dot_product / (norm_X * norm_Y)
            return float(np.mean(cosine_sim))

    def calibrate_adversarial(
        self,
        local_states: np.ndarray | torch.Tensor,
        cloud_states: np.ndarray | torch.Tensor,
        generator_epochs: int = 100,
        calibration_rounds: int = 10,
        generator_hidden_dim: int = 64,
        regularization_lambda: float = 0.1,
        learning_rate: float = 1e-3,
    ) -> dict:
        """Adversarial calibration: GAN-train the Procrustes alignment for robustness.

        A small generator MLP produces perturbation vectors that maximize the
        Procrustes alignment residual. The aligner is then re-calibrated on the
        augmented dataset (original + adversarial), forcing it to handle worst-case
        inputs.

        Args:
            local_states: Real local activation samples (N, local_dim).
            cloud_states: Real cloud activation samples (N, cloud_dim).
            generator_epochs: Training epochs per calibration round for the generator.
            calibration_rounds: Number of generate→calibrate→repeat rounds.
            generator_hidden_dim: Hidden layer width of the generator MLP.
            regularization_lambda: L2 penalty on perturbation magnitude.
            learning_rate: Generator optimizer learning rate.

        Returns:
            Dict with keys:
                'initial_score': alignment score before hardening
                'final_score': alignment score after hardening
                'rounds': list of per-round metrics
                'adversarial_pairs_generated': count of synthetic pairs added
        """
        # Convert to numpy for calibration
        if isinstance(local_states, torch.Tensor):
            local_np = local_states.detach().cpu().numpy().astype(np.float32)
        else:
            local_np = np.asarray(local_states, dtype=np.float32)

        if isinstance(cloud_states, torch.Tensor):
            cloud_np = cloud_states.detach().cpu().numpy().astype(np.float32)
        else:
            cloud_np = np.asarray(cloud_states, dtype=np.float32)

        # Initial calibration to get baseline
        initial_score = self.calibrate(local_np, cloud_np)["correlation"]

        # Build generator network: takes (local, cloud) → (delta_local, delta_cloud)
        input_dim = self.local_dim + self.cloud_dim
        output_dim = self.local_dim + self.cloud_dim

        generator = torch.nn.Sequential(
            torch.nn.Linear(input_dim, generator_hidden_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(generator_hidden_dim, generator_hidden_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(generator_hidden_dim, output_dim),
            torch.nn.Tanh(),  # Bound perturbations to [-1, 1] before scaling
        )
        optimizer = torch.optim.Adam(generator.parameters(), lr=learning_rate)

        # Scale factor: perturbations proportional to data magnitude
        local_scale = float(np.std(local_np))
        cloud_scale = float(np.std(cloud_np))

        local_tensor = torch.from_numpy(local_np)
        cloud_tensor = torch.from_numpy(cloud_np)

        round_metrics = []
        adversarial_local_batches = []
        adversarial_cloud_batches = []
        total_adversarial = 0

        for round_idx in range(calibration_rounds):
            # Phase 1: Train generator to maximize Procrustes residual
            generator.train()
            gen_losses = []

            for epoch in range(generator_epochs):
                optimizer.zero_grad()

                # Concatenate inputs
                combined = torch.cat([local_tensor, cloud_tensor], dim=1)
                perturbations = generator(combined)

                # Split perturbations
                delta_local = perturbations[:, :self.local_dim] * local_scale
                delta_cloud = perturbations[:, self.local_dim:] * cloud_scale

                # Create adversarial pairs
                adv_local = local_tensor + delta_local
                adv_cloud = cloud_tensor + delta_cloud

                # Compute alignment residual using current Q matrix
                # h_aligned = (h_cloud - mu_cloud) @ Q + mu_local
                mu_local_t = torch.from_numpy(
                    self.mu_local if isinstance(self.mu_local, np.ndarray)
                    else self.mu_local.cpu().numpy()
                ).float()
                mu_cloud_t = torch.from_numpy(
                    self.mu_cloud if isinstance(self.mu_cloud, np.ndarray)
                    else self.mu_cloud.cpu().numpy()
                ).float()
                Q_t = torch.from_numpy(
                    self.Q if isinstance(self.Q, np.ndarray)
                    else self.Q.cpu().numpy()
                ).float()

                aligned_adv = (adv_cloud - mu_cloud_t) @ Q_t + mu_local_t

                # Generator loss: maximize residual, penalize perturbation size
                residual_loss = torch.mean(torch.norm(aligned_adv - adv_local, dim=1))
                perturbation_penalty = (
                    torch.mean(torch.norm(delta_local, dim=1))
                    + torch.mean(torch.norm(delta_cloud, dim=1))
                )

                # Negate residual (we want to maximize it)
                loss = -residual_loss + regularization_lambda * perturbation_penalty
                loss.backward()
                optimizer.step()
                gen_losses.append(-loss.item())  # Track positive residual

            # Phase 2: Generate adversarial pairs and augment dataset
            generator.eval()
            with torch.no_grad():
                combined = torch.cat([local_tensor, cloud_tensor], dim=1)
                perturbations = generator(combined)
                delta_local = perturbations[:, :self.local_dim] * local_scale
                delta_cloud = perturbations[:, self.local_dim:] * cloud_scale

                adv_local_np = (local_tensor + delta_local).numpy()
                adv_cloud_np = (cloud_tensor + delta_cloud).numpy()

            # Augment dataset with adversarial pairs
            augmented_local = np.vstack([local_np, adv_local_np])
            augmented_cloud = np.vstack([cloud_np, adv_cloud_np])
            adversarial_local_batches.append(adv_local_np)
            adversarial_cloud_batches.append(adv_cloud_np)
            total_adversarial += len(adv_local_np)

            # Phase 3: Re-calibrate on augmented dataset
            round_score = self.calibrate(augmented_local, augmented_cloud)["correlation"]

            mean_perturbation = float(
                np.mean(np.linalg.norm(adv_local_np - local_np, axis=1))
                + np.mean(np.linalg.norm(adv_cloud_np - cloud_np, axis=1))
            ) / 2

            round_metrics.append({
                'round': round_idx + 1,
                'score': round_score,
                'generator_loss_final': gen_losses[-1] if gen_losses else 0.0,
                'mean_perturbation': mean_perturbation,
            })

        # Final calibration on original + all adversarial data for clean Q
        if adversarial_local_batches:
            final_local = np.vstack([local_np, *adversarial_local_batches])
            final_cloud = np.vstack([cloud_np, *adversarial_cloud_batches])
        else:
            final_local = local_np
            final_cloud = cloud_np
        final_score = self.calibrate(final_local, final_cloud)["correlation"]

        return {
            'initial_score': initial_score,
            'final_score': final_score,
            'rounds': round_metrics,
            'adversarial_pairs_generated': total_adversarial,
        }

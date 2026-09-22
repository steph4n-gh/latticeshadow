import torch
import torch.nn as nn
import logging
import threading
from typing import Optional
from pathlib import Path

logger = logging.getLogger("latticeshadow_db.distill")


class CorrectionHead:
    """
    Lightweight MLP that learns to approximate the cloud correction locally.
    Trained on (local_activation, aligned_cloud_activation) pairs accumulated
    in the ActivationCache during normal operation.

    This enables online distillation: the cloud model teaches the local model
    how to self-correct through the cache, without the cloud knowing.
    """

    def __init__(self, input_dim: int, hidden_dim: int = 256, device: str = "cpu"):
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.device = device

        self.model = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, input_dim),
        ).to(device)

        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-3)
        self.is_trained = False
        self.training_loss = float('inf')
        self._training_lock = threading.Lock()

    @torch.no_grad()
    def predict(self, local_state: torch.Tensor) -> torch.Tensor:
        """
        Predict what the aligned cloud correction would be for a given local state.
        """
        self.model.eval()
        orig_device = local_state.device
        orig_dtype = local_state.dtype
        x = local_state.to(device=self.device, dtype=torch.float32)
        prediction = self.model(x)
        return prediction.to(device=orig_device, dtype=orig_dtype)

    def train_from_cache(self, cache, epochs: int = 10, batch_size: int = 64) -> float:
        """
        Train on all (local_tensor, cloud_tensor) pairs in the cache.

        Args:
            cache: An ActivationCache instance with populated keys/values.
            epochs: Number of training epochs.
            batch_size: Mini-batch size for training.

        Returns:
            Final training loss (MSE).
        """
        if not cache.keys:
            logger.warning("Cannot train CorrectionHead: cache is empty.")
            return float('inf')

        with self._training_lock:
            self.model.train()

            # Determine target dimension: use self.input_dim if valid, otherwise use first key's dim
            target_dim = self.input_dim
            if not cache.keys:
                return float('inf')
                
            first_dim = cache.keys[0].view(-1).shape[0]
            if target_dim is None or target_dim <= 0:
                target_dim = first_dim

            # Filter pairs that match target_dim
            valid_pairs = []
            for k, v in zip(cache.keys, cache.values):
                flat_k = k.view(-1)
                flat_v = v.view(-1)
                if flat_k.shape[0] == target_dim and flat_v.shape[0] == target_dim:
                    valid_pairs.append((flat_k, flat_v))

            if not valid_pairs:
                logger.warning("No cache entries match the target dimension %d.", target_dim)
                return float('inf')

            X = torch.stack([p[0] for p in valid_pairs]).to(
                device=self.device, dtype=torch.float32
            )
            Y = torch.stack([p[1] for p in valid_pairs]).to(
                device=self.device, dtype=torch.float32
            )

            # Ensure model input dim matches data
            if X.shape[1] != self.input_dim:
                logger.warning(
                    "CorrectionHead input_dim (%d) doesn't match cache tensor dim (%d). "
                    "Rebuilding model.", self.input_dim, X.shape[1]
                )
                self.input_dim = X.shape[1]
                self.model = nn.Sequential(
                    nn.Linear(self.input_dim, self.hidden_dim),
                    nn.ReLU(),
                    nn.Linear(self.hidden_dim, self.input_dim),
                ).to(self.device)
                self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-3)

            dataset = torch.utils.data.TensorDataset(X, Y)
            loader = torch.utils.data.DataLoader(
                dataset, batch_size=batch_size, shuffle=True
            )

            loss_fn = nn.MSELoss()
            final_loss = float('inf')

            for epoch in range(epochs):
                epoch_loss = 0.0
                count = 0
                for x_batch, y_batch in loader:
                    self.optimizer.zero_grad()
                    pred = self.model(x_batch)
                    loss = loss_fn(pred, y_batch)
                    loss.backward()
                    self.optimizer.step()
                    epoch_loss += loss.item()
                    count += 1
                final_loss = epoch_loss / max(count, 1)

            self.training_loss = final_loss
            self.is_trained = True
            logger.info(
                "CorrectionHead training complete. Final MSE: %.6f over %d samples.",
                final_loss, len(X)
            )
            return final_loss

    def save(self, path: str):
        """Save the trained model state dict."""
        path = Path(path)
        torch.save({
            'model_state_dict': self.model.state_dict(),
            'input_dim': self.input_dim,
            'hidden_dim': self.hidden_dim,
            'training_loss': self.training_loss,
            'is_trained': self.is_trained,
        }, path)
        logger.info("CorrectionHead saved to %s", path)

    def load(self, path: str):
        """Load model state dict from file."""
        path = Path(path)
        checkpoint = torch.load(path, map_location=self.device, weights_only=True)

        # Rebuild model if dimensions changed
        if checkpoint.get('input_dim', self.input_dim) != self.input_dim:
            self.input_dim = checkpoint['input_dim']
            self.hidden_dim = checkpoint.get('hidden_dim', self.hidden_dim)
            self.model = nn.Sequential(
                nn.Linear(self.input_dim, self.hidden_dim),
                nn.ReLU(),
                nn.Linear(self.hidden_dim, self.input_dim),
            ).to(self.device)

        self.model.load_state_dict(checkpoint['model_state_dict'])
        self.training_loss = checkpoint.get('training_loss', float('inf'))
        self.is_trained = checkpoint.get('is_trained', True)
        logger.info("CorrectionHead loaded from %s (loss: %.6f)", path, self.training_loss)

import torch
import math
import logging

logger = logging.getLogger("latticeshadow_db.routing")


def calculate_activation_entropy(tensor: torch.Tensor, bins: int = 64) -> float:
    """
    Approximates information entropy H(X) by constructing a probability density
    histogram over the activation values across the dimension.
    
    Standalone function for use across modules without importing MoCRouter.
    """
    if tensor.numel() == 0:
        return 0.0

    # Work on flattened tensor
    flat_tensor = tensor.view(-1).float()
    
    # Sanitize NaNs and Infinities for robustness
    if not torch.isfinite(flat_tensor).all():
        flat_tensor = torch.nan_to_num(flat_tensor, nan=0.0, posinf=1.0, neginf=-1.0)

    # Calculate histogram bounds
    min_val = flat_tensor.min().item()
    max_val = flat_tensor.max().item()

    # If all values are identical, entropy is 0
    if math.isclose(min_val, max_val, rel_tol=1e-5):
        return 0.0

    # Construct histogram using PyTorch
    hist = torch.histc(flat_tensor, bins=bins, min=min_val, max=max_val)

    # Calculate probabilities
    probs = hist / hist.sum()

    # Filter out zero probabilities to avoid log2(0)
    probs = probs[probs > 0]

    # Calculate Shannon entropy: -sum(P(x) * log2(P(x)))
    entropy = -torch.sum(probs * torch.log2(probs)).item()

    return entropy


class MoCRouter:
    """
    Mixture-of-Clouds Router based on Information Entropy.
    Dynamically routes queries based on the uncertainty (entropy) of the hidden activation state.
    """
    def __init__(self, low_thresh: float = 1.2, high_thresh: float = 2.5):
        self.low_thresh = low_thresh
        self.high_thresh = high_thresh

    def calculate_entropy(self, tensor: torch.Tensor, bins: int = 64) -> float:
        """Thin wrapper around the module-level function."""
        return calculate_activation_entropy(tensor, bins)

    def route(self, tensor: torch.Tensor) -> str:
        """
        Evaluates the entropy and returns the routing decision.
        Returns: "local", "flash", or "ultra"
        """
        entropy = self.calculate_entropy(tensor)
        logger.debug("Activation Entropy evaluated at: %.4f", entropy)

        if entropy < self.low_thresh:
            logger.debug("Entropy < %.4f: Routing LOCALLY", self.low_thresh)
            return "local"
        elif entropy < self.high_thresh:
            logger.debug("Entropy < %.4f: Routing to FLASH", self.high_thresh)
            return "flash"
        else:
            logger.debug("Entropy >= %.4f: Routing to ULTRA", self.high_thresh)
            return "ultra"


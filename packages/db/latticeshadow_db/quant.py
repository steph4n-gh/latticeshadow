import torch
import os
from typing import Tuple
import logging
from .leech import decode_leech

logger = logging.getLogger("latticeshadow_db.quant")


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


MAX_LEECH_BLOCKS = _int_env("LATTICEDB_MAX_LEECH_BLOCKS", 4096)

class Sparsifier:
    """
    Sparsifies activation tensors by applying a threshold mask to zero out coordinates below a magnitude.
    """
    @staticmethod
    def apply_mask(tensor: torch.Tensor, epsilon: float = 1e-3) -> torch.Tensor:
        """
        Zeroes out all coordinates where absolute value is strictly less than epsilon.
        """
        if tensor.numel() == 0:
            return tensor

        mask = torch.abs(tensor) >= epsilon
        sparsified = tensor * mask
        
        sparsity_ratio = 1.0 - (mask.sum().item() / tensor.numel())
        logger.debug("Sparsification complete. Epsilon: %.4f, Sparsity ratio: %.2f%%", 
                     epsilon, sparsity_ratio * 100)
        
        return sparsified

class Quantizer:
    """
    Implements symmetric uniform quantization for mapping Float activations to signed 8-bit integers.
    """
    @staticmethod
    def quantize_to_int8(tensor: torch.Tensor) -> Tuple[torch.Tensor, float, float]:
        """
        Maps a float tensor to int8 [-128, 127].
        Returns:
            - quantized_tensor (torch.Tensor of dtype torch.int8)
            - scale (float)
            - zero_point (float)
        """
        if tensor.numel() == 0:
            return tensor.to(torch.int8), 1.0, 0.0

        x_min = tensor.min().item()
        x_max = tensor.max().item()

        # Handle edge case where max == min
        if x_max == x_min:
            return torch.zeros_like(tensor, dtype=torch.int8), 1.0, 0.0

        q_min, q_max = -128, 127
        
        # Calculate Scale and Zero Point
        scale = (x_max - x_min) / (q_max - q_min)
        zero_point = round((-x_min / scale)) - 128
        
        # Quantize
        q_tensor = torch.round(tensor / scale) + zero_point
        q_tensor = torch.clamp(q_tensor, q_min, q_max).to(torch.int8)
        
        return q_tensor, scale, zero_point

    @staticmethod
    def dequantize_from_int8(q_tensor: torch.Tensor, scale: float, zero_point: float) -> torch.Tensor:
        """
        Reconstructs the float tensor from the int8 representation.
        Returns:
            - float_tensor (torch.Tensor of dtype torch.float32)
        """
        float_tensor = scale * (q_tensor.to(torch.float32) - zero_point)
        return float_tensor


class LeechLatticeQuantizer:
    """
    Implements Vector Quantization using the 24-dimensional Leech Lattice (Lambda_24).
    Uses a native C++ compiled extension (with a pure-Python fallback) to map 
    continuous 24D activation vectors to the closest points on the Leech Lattice.
    Supports structured random noise padding to handle arbitrary dimensions.
    """
    def __init__(self, noise_scale: float = 0.1, max_blocks: int = MAX_LEECH_BLOCKS):
        self.noise_scale = noise_scale
        self.lattice_scale = 8.0 ** 0.5
        self.max_blocks = max(1, int(max_blocks))

    def quantize(self, tensor: torch.Tensor) -> Tuple[torch.Tensor, Tuple[int, ...]]:
        """
        Pads the input tensor to a multiple of 24, reshapes it, and maps it to the Leech Lattice.
        
        Args:
            tensor: Input float tensor of shape (..., dim)
            
        Returns:
            Tuple of (quantized_tensor, original_shape)
        """
        orig_shape = tensor.shape
        if tensor.ndim == 0:
            raise ValueError("Leech quantization requires at least one dimension.")
        dim = orig_shape[-1]
        if dim <= 0:
            raise ValueError("Leech quantization requires a non-empty final dimension.")
        
        # 1. Padding with structured noise to a multiple of 24
        pad_len = (24 - (dim % 24)) % 24
        if pad_len > 0:
            # Calculate standard deviation and mean to construct structured noise
            if tensor.numel() > 1:
                std = tensor.std().item()
                mean = tensor.mean().item()
            else:
                std = 1.0
                mean = 0.0
            
            # Deterministic/reproducible generator on the correct device
            generator = torch.Generator(device=tensor.device).manual_seed(42)
            noise = torch.randn(tensor.shape[:-1] + (pad_len,), generator=generator, device=tensor.device, dtype=tensor.dtype)
            padded = torch.cat([tensor, mean + self.noise_scale * std * noise], dim=-1)
        else:
            padded = tensor
            
        # 2. Reshape to (N, 24)
        flat_shape = padded.shape
        block_count = padded.numel() // 24
        if block_count > self.max_blocks:
            raise ValueError(
                f"Leech lattice quantization would decode {block_count} blocks; "
                f"the configured limit is {self.max_blocks}."
            )
        padded_2d = padded.view(-1, 24)
        
        # 3. Scale by sqrt(8) to prepare for standard Leech Lattice decoding
        scaled = padded_2d * self.lattice_scale
        
        # 4. Decode to nearest lattice point
        decoded_scaled = decode_leech(scaled)
        
        # 5. Scale back by 1/sqrt(8)
        decoded = decoded_scaled / self.lattice_scale
        
        # 6. Reshape to original padded shape
        quantized = decoded.view(flat_shape)
        
        return quantized, orig_shape

    def dequantize(self, quantized: torch.Tensor, orig_shape: Tuple[int, ...]) -> torch.Tensor:
        """
        Strips the noise padding to restore the tensor to its original dimension.
        """
        dim = orig_shape[-1]
        return quantized[..., :dim]

    def pack_code(self, quantized: torch.Tensor) -> torch.Tensor:
        """
        Pack a quantized Leech code into int16 lattice coordinates.

        Quantized codes are stored after scaling by 1/sqrt(8). Multiplying by
        sqrt(8) recovers the integer lattice coordinate emitted by the decoder,
        so this compact representation is exact for normal decoded values.
        """
        scaled = torch.round(quantized.detach().cpu().float() * self.lattice_scale)
        info = torch.iinfo(torch.int16)
        if torch.any((scaled < info.min) | (scaled > info.max)):
            logger.warning("Leech code exceeded int16 range; clamping compact code.")
            scaled = torch.clamp(scaled, info.min, info.max)
        return scaled.to(torch.int16)

    def unpack_code(self, packed: torch.Tensor, dtype: torch.dtype = torch.float32) -> torch.Tensor:
        """Restore a packed int16 Leech code to the quantized float representation."""
        return packed.to(dtype=dtype) / self.lattice_scale

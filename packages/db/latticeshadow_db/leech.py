import logging
import torch

logger = logging.getLogger("latticeshadow_db.leech")

_CPP_DECODER_AVAILABLE = False
try:
    # Try importing the compiled C++ extension
    from . import _leech_decoder_cpp
    _CPP_DECODER_AVAILABLE = True
except ImportError:
    logger.warning("C++ Leech Lattice extension not found/built. Falling back to pure Python implementation.")

def decode_leech(y: torch.Tensor) -> torch.Tensor:
    """
    Decodes a batch of 24-dimensional vectors to their closest points on the Leech Lattice.
    
    Args:
        y: Tensor of shape (N, 24) with float coordinates.
        
    Returns:
        Tensor of shape (N, 24) containing the closest points on the Leech Lattice.
    """
    if _CPP_DECODER_AVAILABLE:
        device = y.device
        dtype = y.dtype
        # The C++ extension expects a contiguous float CPU tensor
        y_cpu = y.detach().cpu().float().contiguous()
        decoded = _leech_decoder_cpp.decode_leech(y_cpu)
        return decoded.to(device=device, dtype=dtype)
    else:
        from .leech_fallback import decode_leech_py
        return decode_leech_py(y)

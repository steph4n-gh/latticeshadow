import torch
import math
from .exceptions import ConfigurationError

class CayleyPrivacyAdapter:
    """
    Cayley Privacy Adapter implementing activation obfuscation using the
    Woodbury-optimized form of distance-preserving orthogonal Cayley rotations.
    """
    def __init__(self, dim: int, rank: int = None, device: str = "cpu", dtype: torch.dtype = torch.float32, noise_scale: float = 0.0):
        if dim <= 0:
            raise ConfigurationError(f"dim must be positive, got {dim}")
            
        if rank is None:
            rank = min(16, max(1, dim // 2))
            
        if rank <= 0:
            raise ConfigurationError(f"rank must be positive, got {rank}")
            
        self.dim = dim
        self.rank = rank
        self.device = device
        self.dtype = dtype
        
        if 2 * rank > dim:
            raise ConfigurationError(f"2 * rank ({2 * rank}) must be less than or equal to dim ({dim})")
        
        # Validate noise_scale
        if not isinstance(noise_scale, (int, float)) or isinstance(noise_scale, bool):
            raise ConfigurationError("noise_scale must be a float")
        import math
        if math.isnan(noise_scale) or math.isinf(noise_scale):
            raise ConfigurationError("noise_scale must be a finite float")
        if noise_scale < 0:
            raise ConfigurationError("noise_scale must be non-negative")
            
        self.noise_scale = float(noise_scale)
        
        self._A: torch.Tensor = None
        self._B: torch.Tensor = None
        self._U: torch.Tensor = None
        self._V: torch.Tensor = None
        self._M: torch.Tensor = None  # Precomputed (I_{2r} + V^T U)^{-1}
        
        self.regenerate()

    def _to_device(self, tensor: torch.Tensor) -> torch.Tensor:
        """Helper to move a tensor to the target device with a dynamic CPU fallback."""
        try:
            return tensor.to(device=self.device, dtype=self.dtype)
        except RuntimeError as e:
            if self.device == "mps":
                import logging
                logger = logging.getLogger("latticeshadow")
                logger.warning(f"MPS operation failed, falling back to CPU: {e}")
                self.device = "cpu"
                for attr in ["_A", "_B", "_U", "_V", "_M"]:
                    t = getattr(self, attr, None)
                    if isinstance(t, torch.Tensor):
                        setattr(self, attr, t.to("cpu"))
                return tensor.to(device="cpu", dtype=self.dtype)
            raise

    def regenerate(self):
        """
        Generates new random low-rank factors A, B, builds U and V,
        and precomputes the Woodbury scaling inverse matrix.
        Performs matrix inversion on the CPU for MPS/CUDA device safety.
        """
        try:
            self._regenerate_on_device(self.device)
        except RuntimeError as e:
            if self.device == "mps":
                import logging
                logger = logging.getLogger("latticeshadow")
                logger.warning(f"MPS allocation failed, falling back to CPU: {e}")
                self.device = "cpu"
                self._regenerate_on_device("cpu")
            else:
                raise

    def _regenerate_on_device(self, device):
        # Gaussian initialization N(0, 0.01)
        self._A = torch.randn(self.dim, self.rank, device=device, dtype=self.dtype) * 0.01
        self._B = torch.randn(self.dim, self.rank, device=device, dtype=self.dtype) * 0.01
        
        # U = [A | -B] in R^{d x 2r}
        self._U = torch.cat([self._A, -self._B], dim=1)
        # V = [B | A] in R^{d x 2r}
        self._V = torch.cat([self._B, self._A], dim=1)
        
        # Precompute (I_{2r} + V^T U)^{-1}
        # Compute V^T U
        vt_u = torch.matmul(self._V.t(), self._U)
        eye = torch.eye(2 * self.rank, device=device, dtype=self.dtype)
        core = eye + vt_u
        
        # Perform matrix inversion on CPU for device safety and dtype stability
        inv_dtype = torch.float32 if self.dtype in (torch.float16, torch.bfloat16) else self.dtype
        core_cpu = core.to(device="cpu", dtype=inv_dtype)
        M_cpu = torch.linalg.inv(core_cpu)
        self._M = M_cpu.to(device=device, dtype=self.dtype)

    @torch.no_grad()
    def rotate(self, h: torch.Tensor) -> torch.Tensor:
        """
        Applies forward rotation (row-vector convention: h @ W_L^T).
        Equivalent to W_L h in column-vector notation.
        Handles arbitrary leading batch/sequence dimensions via the last axis.
        """
        if h.numel() == 0:
            raise ValueError("Input tensor must not be empty")
        if not torch.isfinite(h).all():
            raise ValueError("Input contains non-finite values (NaN/Inf)")
        h_orig_device = h.device
        h_orig_dtype = h.dtype
        h = self._to_device(h)
        
        # Row-vector forward: h W_L^T = h (I - 2 V M^T U^T) = h - 2 (h V) M^T U^T
        v_t_h = torch.matmul(h, self._V)            # (..., 2r)
        temp = torch.matmul(v_t_h, self._M.t())      # (..., 2r)
        out = h - 2.0 * torch.matmul(temp, self._U.t())  # (..., d)
        
        if self.noise_scale > 0:
            std = h.std(dim=list(range(1, h.ndim)), keepdim=True) if h.ndim > 1 else h.std()
            std = torch.nan_to_num(std, nan=0.0)
            noise = torch.randn_like(out) * (std * self.noise_scale)
            out = out + noise
            
        return out.to(device=h_orig_device, dtype=h_orig_dtype)

    @torch.no_grad()
    def inverse_rotate(self, h: torch.Tensor) -> torch.Tensor:
        """
        Applies inverse rotation (row-vector convention: h @ W_L).
        Equivalent to W_L^T h in column-vector notation.
        Handles arbitrary leading batch/sequence dimensions via the last axis.
        """
        if h.numel() == 0:
            raise ValueError("Input tensor must not be empty")
        if not torch.isfinite(h).all():
            raise ValueError("Input contains non-finite values (NaN/Inf)")
        h_orig_device = h.device
        h_orig_dtype = h.dtype
        h = self._to_device(h)
        
        # Row-vector inverse: h W_L = h (I - 2 U M V^T) = h - 2 (h U) M V^T
        # Note: (I + U^T V)^{-T} = (I + V^T U)^{-1} = M
        u_t_h = torch.matmul(h, self._U)              # (..., 2r)
        temp = torch.matmul(u_t_h, self._M)            # (..., 2r)
        out = h - 2.0 * torch.matmul(temp, self._V.t())  # (..., d)
        
        return out.to(device=h_orig_device, dtype=h_orig_dtype)

    @torch.no_grad()
    def compress(self, h_rotated: torch.Tensor) -> torch.Tensor:
        """
        Sketch rotated activations onto the 2r-dimensional Cayley subspace.
        Returns shape (..., 2*rank) instead of (..., dim).
        Bandwidth reduction ratio: dim / (2*rank).
        This is a lossy transit sketch, not a reversible compression codec.
        """
        if h_rotated.numel() == 0:
            raise ValueError("Input tensor must not be empty")
        h_rotated = self._to_device(h_rotated)
        # Project onto V subspace: (..., d) @ (d, 2r) -> (..., 2r)
        return torch.matmul(h_rotated, self._V)

    @torch.no_grad()
    def decompress(self, h_compressed: torch.Tensor) -> torch.Tensor:
        """
        Project a 2r-dimensional Cayley subspace sketch back into full dimension.
        This returns the minimum-norm projection implied by V; it is not a
        faithful reconstruction of components outside the sketched subspace.
        V^+ = (V^T V)^{-1} V^T, so h_approx = h_compressed @ V^+^T = h_compressed @ V (V^T V)^{-1}
        """
        if h_compressed.numel() == 0:
            raise ValueError("Input tensor must not be empty")
        h_compressed = self._to_device(h_compressed)
        # Compute pseudo-inverse projection
        VtV = torch.matmul(self._V.t(), self._V)  # (2r, 2r)
        inv_dtype = torch.float32 if self.dtype in (torch.float16, torch.bfloat16) else self.dtype
        VtV_cpu = VtV.to(device="cpu", dtype=inv_dtype)
        VtV_inv_cpu = torch.linalg.inv(VtV_cpu)
        VtV_inv = self._to_device(VtV_inv_cpu)
        # h_compressed @ (V^T V)^{-1} @ V^T -> (..., d)
        temp = torch.matmul(h_compressed, VtV_inv)  # (..., 2r)
        return torch.matmul(temp, self._V.t())  # (..., d)

    @staticmethod
    def _next_power_of_two(value: int) -> int:
        return 1 << (value - 1).bit_length()

    @staticmethod
    def _normalized_hadamard(x: torch.Tensor) -> torch.Tensor:
        """Apply a normalized Walsh-Hadamard transform along the last axis."""
        n = x.shape[-1]
        if n <= 0 or n & (n - 1):
            raise ValueError("Hadamard dimension must be a positive power of two")

        original_shape = x.shape
        y = x.reshape(-1, n)
        width = 1
        while width < n:
            y = y.reshape(-1, n // (2 * width), 2 * width)
            left = y[:, :, :width]
            right = y[:, :, width:2 * width]
            y = torch.cat((left + right, left - right), dim=2)
            width *= 2
        y = y.reshape(original_shape)
        return y / math.sqrt(float(n))

    @torch.no_grad()
    def srht_sketch(self, h_rotated: torch.Tensor, sketch_dim: int,
                    seed: int = 0) -> torch.Tensor:
        """
        Sketch rotated activations with a deterministic SRHT-style transform.

        The transform is Phi = sqrt(n/m) R H D after zero-padding the last
        dimension to power-of-two length n. D is a seeded Rademacher sign
        diagonal, H is the normalized Walsh-Hadamard transform, and R samples
        m coordinates without replacement. This is intended for distance
        sketches and transit payload reduction, not full-state reconstruction.
        """
        if h_rotated.numel() == 0:
            raise ValueError("Input tensor must not be empty")
        if sketch_dim <= 0:
            raise ConfigurationError(f"sketch_dim must be positive, got {sketch_dim}")

        h_orig_device = h_rotated.device
        h_orig_dtype = h_rotated.dtype
        h = self._to_device(h_rotated)
        dim = h.shape[-1]
        padded_dim = self._next_power_of_two(dim)
        if sketch_dim > padded_dim:
            raise ConfigurationError(
                f"sketch_dim ({sketch_dim}) must be <= padded dimension ({padded_dim})"
            )

        if padded_dim != dim:
            pad_shape = (*h.shape[:-1], padded_dim - dim)
            h = torch.cat([h, torch.zeros(pad_shape, device=h.device, dtype=h.dtype)], dim=-1)

        generator = torch.Generator(device="cpu")
        generator.manual_seed(int(seed))
        signs = torch.randint(
            low=0,
            high=2,
            size=(padded_dim,),
            generator=generator,
            dtype=torch.int8,
        )
        signs = (signs.to(dtype=h.dtype) * 2.0 - 1.0).to(device=h.device)
        sampled = torch.randperm(padded_dim, generator=generator)[:sketch_dim].to(device=h.device)

        transformed = self._normalized_hadamard(h * signs)
        sketch = transformed.index_select(dim=-1, index=sampled)
        sketch = sketch * math.sqrt(float(padded_dim) / float(sketch_dim))
        return sketch.to(device=h_orig_device, dtype=h_orig_dtype)

    @torch.no_grad()
    def shred(self):
        """Securely zero out all secret rotation matrices in memory."""
        for attr in ["_A", "_B", "_U", "_V", "_M"]:
            tensor = getattr(self, attr, None)
            if isinstance(tensor, torch.Tensor):
                tensor.zero_()
                setattr(self, attr, None)

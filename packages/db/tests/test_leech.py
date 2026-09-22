import pytest
import torch
import numpy as np
from latticeshadow_db.leech import decode_leech, _CPP_DECODER_AVAILABLE
from latticeshadow_db.leech_fallback import decode_leech_py
from latticeshadow_db.quant import LeechLatticeQuantizer
from latticeshadow_db.bridge import ZkBridge
from latticeshadow_db.adapter import CayleyPrivacyAdapter
from latticeshadow_db.alignment import ProcrustesAligner

def test_decoder_equivalence():
    """Verify that C++ decoder and Python fallback decoder produce identical results."""
    # Generate some random 24D vectors
    y = torch.randn(5, 24)
    
    decoded_py = decode_leech_py(y)
    decoded_cpp = decode_leech(y)
    
    # Assert coordinate equality
    assert torch.allclose(decoded_py, decoded_cpp, atol=1e-5)

def test_leech_quantizer_shapes():
    """Verify that LeechLatticeQuantizer preserves shapes and handles padding correctly."""
    quantizer = LeechLatticeQuantizer(noise_scale=0.1)
    
    # Test multiple dimensions, some multiples of 24 and some not
    for dim in [24, 48, 128, 256, 384]:
        h = torch.randn(2, dim)
        
        quantized, orig_shape = quantizer.quantize(h)
        
        # Padded shape must be a multiple of 24
        assert quantized.shape[-1] % 24 == 0
        assert quantized.shape[-1] >= dim
        
        # Dequantize must restore original shape
        dequantized = quantizer.dequantize(quantized, orig_shape)
        assert dequantized.shape == h.shape

def test_leech_quantizer_noise_determinism():
    """Verify that structured noise padding is deterministic and reproducible."""
    quantizer1 = LeechLatticeQuantizer(noise_scale=0.1)
    quantizer2 = LeechLatticeQuantizer(noise_scale=0.1)
    
    h = torch.randn(2, 128)
    
    q1, _ = quantizer1.quantize(h)
    q2, _ = quantizer2.quantize(h)
    
    assert torch.equal(q1, q2)


def test_leech_quantizer_compact_code_roundtrip():
    """Verify int16 compact Leech codes reconstruct quantized values exactly."""
    quantizer = LeechLatticeQuantizer(noise_scale=0.1)
    h = torch.randn(2, 128)

    quantized, _ = quantizer.quantize(h)
    packed = quantizer.pack_code(quantized)
    unpacked = quantizer.unpack_code(packed)

    assert packed.dtype == torch.int16
    assert packed.numel() == quantized.numel()
    assert packed.element_size() == 2
    assert torch.allclose(unpacked, quantized.float(), atol=1e-6)

def test_zk_bridge_integration_with_leech():
    """Verify end-to-end ZkBridge query workflow with Leech Lattice quantization enabled."""
    local_dim = 128
    cloud_dim = 256
    rank = 16
    
    adapter = CayleyPrivacyAdapter(dim=local_dim, rank=rank)
    
    # Calibrate an aligner
    np.random.seed(42)
    local_synthetic = np.random.normal(0, 1.0, size=(100, local_dim))
    projection = np.random.normal(0, 1.0, size=(local_dim, cloud_dim))
    u_proj, _, vh_proj = np.linalg.svd(projection, full_matrices=False)
    ortho_proj = u_proj @ vh_proj
    cloud_synthetic = local_synthetic @ ortho_proj + np.random.normal(0, 0.01, size=(100, cloud_dim))
    
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    aligner.calibrate(local_synthetic, cloud_synthetic)
    
    # 1. Test standard pathway (no compression) + Leech
    bridge = ZkBridge(
        adapter=adapter,
        aligner=aligner,
        mock_mode=True,
        use_leech_lattice=True,
        enable_speculation=False,
        enable_routing=False,
        enable_temporal=False,
    )
    
    h = torch.randn(1, local_dim)
    result = bridge.query(h, "test context")
    
    assert result.shape == h.shape
    assert not torch.allclose(result, h)
    
    # 2. Test compressed pathway + Leech
    bridge_compressed = ZkBridge(
        adapter=adapter,
        aligner=aligner,
        mock_mode=True,
        use_leech_lattice=True,
        compress_transit=True,
        enable_speculation=False,
        enable_routing=False,
        enable_temporal=False,
    )
    
    h_c = torch.randn(1, local_dim)
    result_c = bridge_compressed.query(h_c, "test context")
    
    assert result_c.shape == h_c.shape
    assert not torch.allclose(result_c, h_c)


def test_vectorized_decoder_cache_and_dtypes():
    """Verify that the vectorized decoder caches tensors correctly and supports different dtypes."""
    from latticeshadow_db.leech_fallback import _G_codewords_cache, decode_leech_py
    
    # Clear the cache first to ensure test isolation
    _G_codewords_cache.clear()
    
    # Test float32
    y_f32 = torch.randn(4, 24, dtype=torch.float32)
    res_f32 = decode_leech_py(y_f32)
    assert res_f32.dtype == torch.float32
    
    # Check that cache has been populated
    key_f32 = (y_f32.device, torch.float32)
    assert key_f32 in _G_codewords_cache
    tensor_f32_first = _G_codewords_cache[key_f32]
    
    # Call again and verify the exact same tensor instance is used
    _ = decode_leech_py(y_f32)
    assert _G_codewords_cache[key_f32] is tensor_f32_first
    
    # Test float64 (double)
    y_f64 = torch.randn(4, 24, dtype=torch.float64)
    res_f64 = decode_leech_py(y_f64)
    assert res_f64.dtype == torch.float64
    
    key_f64 = (y_f64.device, torch.float64)
    assert key_f64 in _G_codewords_cache
    assert _G_codewords_cache[key_f64] is not tensor_f32_first
    assert _G_codewords_cache[key_f64].dtype == torch.float64

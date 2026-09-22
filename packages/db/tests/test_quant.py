"""Tests for Sparsifier and Quantizer (quant.py)."""

import pytest
import torch

from latticeshadow_db.quant import Sparsifier, Quantizer


class TestSparsifier:
    def test_apply_mask_basic(self):
        # Create tensor with values above and below threshold
        tensor = torch.tensor([0.0001, 0.002, -0.0005, -0.005, 0.0])
        epsilon = 0.001
        
        sparsified = Sparsifier.apply_mask(tensor, epsilon=epsilon)
        
        # Expected: values < 0.001 are zeroed out
        expected = torch.tensor([0.0, 0.002, 0.0, -0.005, 0.0])
        assert torch.equal(sparsified, expected)

    def test_apply_mask_empty(self):
        tensor = torch.tensor([])
        sparsified = Sparsifier.apply_mask(tensor)
        assert sparsified.numel() == 0

    def test_apply_mask_all_below(self):
        tensor = torch.tensor([0.0001, 0.0002])
        sparsified = Sparsifier.apply_mask(tensor, epsilon=0.01)
        assert torch.all(sparsified == 0.0)


class TestQuantizer:
    def test_quantize_dequantize_roundtrip(self):
        # Create a random tensor
        torch.manual_seed(42)
        tensor = torch.randn(10, 10) * 5.0 + 2.0  # arbitrary mean and std
        
        q_tensor, scale, zero_point = Quantizer.quantize_to_int8(tensor)
        
        assert q_tensor.dtype == torch.int8
        assert q_tensor.shape == tensor.shape
        
        # Dequantize
        reconstructed = Quantizer.dequantize_from_int8(q_tensor, scale, zero_point)
        
        assert reconstructed.dtype == torch.float32
        assert reconstructed.shape == tensor.shape
        
        # Reconstruction error should be bounded by half of the scale
        max_error = torch.max(torch.abs(tensor - reconstructed)).item()
        assert max_error <= (scale / 2.0) + 1e-4

    def test_quantize_empty(self):
        tensor = torch.tensor([])
        q_tensor, scale, zero_point = Quantizer.quantize_to_int8(tensor)
        assert q_tensor.numel() == 0
        assert q_tensor.dtype == torch.int8
        assert scale == 1.0
        assert zero_point == 0.0

    def test_quantize_constant(self):
        tensor = torch.ones(5, 5) * 4.2
        q_tensor, scale, zero_point = Quantizer.quantize_to_int8(tensor)
        assert q_tensor.dtype == torch.int8
        assert torch.all(q_tensor == 0)
        assert scale == 1.0
        assert zero_point == 0.0
        
        reconstructed = Quantizer.dequantize_from_int8(q_tensor, scale, zero_point)
        assert torch.all(reconstructed == 0.0)

    def test_quantize_single_element(self):
        tensor = torch.tensor([12.3])
        q_tensor, scale, zero_point = Quantizer.quantize_to_int8(tensor)
        assert q_tensor.dtype == torch.int8
        assert q_tensor.item() == 0
        assert scale == 1.0
        assert zero_point == 0.0

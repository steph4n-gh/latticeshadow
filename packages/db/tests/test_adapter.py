import pytest
import torch

from latticeshadow_db.adapter import CayleyPrivacyAdapter
from latticeshadow_db.exceptions import ConfigurationError


def test_srht_sketch_is_deterministic_for_seed():
    adapter = CayleyPrivacyAdapter(dim=6, rank=2)
    x = torch.randn(5, 6)
    rotated = adapter.rotate(x)

    sketch_a = adapter.srht_sketch(rotated, sketch_dim=4, seed=123)
    sketch_b = adapter.srht_sketch(rotated, sketch_dim=4, seed=123)
    sketch_c = adapter.srht_sketch(rotated, sketch_dim=4, seed=124)

    assert sketch_a.shape == (5, 4)
    assert torch.allclose(sketch_a, sketch_b)
    assert not torch.allclose(sketch_a, sketch_c)


def test_srht_sketch_handles_non_power_of_two_dimensions():
    adapter = CayleyPrivacyAdapter(dim=7, rank=2)
    x = torch.randn(3, 7)

    sketch = adapter.srht_sketch(x, sketch_dim=5, seed=7)

    assert sketch.shape == (3, 5)
    assert torch.isfinite(sketch).all()


def test_srht_sketch_rejects_invalid_dimensions():
    adapter = CayleyPrivacyAdapter(dim=6, rank=2)

    with pytest.raises(ConfigurationError):
        adapter.srht_sketch(torch.randn(6), sketch_dim=0)

    with pytest.raises(ConfigurationError):
        adapter.srht_sketch(torch.randn(6), sketch_dim=9)


def test_normalized_hadamard_preserves_norm():
    x = torch.randn(4, 8)

    transformed = CayleyPrivacyAdapter._normalized_hadamard(x)

    assert torch.allclose(
        torch.linalg.norm(transformed, dim=-1),
        torch.linalg.norm(x, dim=-1),
        atol=1e-5,
    )

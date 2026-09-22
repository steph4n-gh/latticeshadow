import pytest
import torch

from latticeshadow_db.latticedb.holographic import cleanup_to_codebook


def test_cleanup_to_codebook_returns_nearest_symbol():
    codebook = {
        "author": torch.tensor([1.0, 0.0, 0.0]),
        "title": torch.tensor([0.0, 1.0, 0.0]),
    }
    query = torch.tensor([0.95, 0.05, 0.0])

    result = cleanup_to_codebook(query, codebook, min_similarity=0.8)

    assert result is not None
    label, vector, similarity = result
    assert label == "author"
    assert vector is codebook["author"]
    assert similarity > 0.9


def test_cleanup_to_codebook_rejects_low_similarity():
    codebook = {"orthogonal": torch.tensor([0.0, 1.0])}

    result = cleanup_to_codebook(
        torch.tensor([1.0, 0.0]),
        codebook,
        min_similarity=0.5,
    )

    assert result is None


def test_cleanup_to_codebook_rejects_invalid_query():
    with pytest.raises(ValueError, match="must not contain NaN"):
        cleanup_to_codebook(torch.tensor([float("nan")]), {"x": torch.ones(1)})

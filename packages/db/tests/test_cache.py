import sqlite3

import pytest
import torch

from latticeshadow_db.cache import ActivationCache


def test_encrypted_activation_cache_hides_tensor_blobs(tmp_path):
    pytest.importorskip("cryptography")
    db_path = str(tmp_path / "activation_cache.sqlite")
    local = torch.tensor([1.0, 2.0, 3.0, 4.0])
    cloud = torch.tensor([4.0, 3.0, 2.0, 1.0])

    cache = ActivationCache(db_path=db_path, encryption_key="cache-secret")
    cache.insert(local, cloud, "prompt-hash")

    with sqlite3.connect(db_path) as conn:
        local_blob, cloud_blob = conn.execute(
            "SELECT local_tensor_blob, cloud_tensor_blob FROM activation_cache"
        ).fetchone()

    assert local_blob.startswith(ActivationCache.ENCRYPTED_BLOB_PREFIX)
    assert cloud_blob.startswith(ActivationCache.ENCRYPTED_BLOB_PREFIX)
    assert b"NUMPY" not in local_blob
    assert b"NUMPY" not in cloud_blob

    reloaded = ActivationCache(db_path=db_path, encryption_key="cache-secret")
    hit = reloaded.check_cache(local, threshold=0.99)
    assert hit is not None
    assert torch.allclose(hit, cloud)


def test_encrypted_activation_cache_requires_key(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.delenv("LATTICESHADOW_CACHE_KEY", raising=False)
    monkeypatch.delenv("LATTICEDB_CACHE_KEY", raising=False)
    db_path = str(tmp_path / "activation_cache.sqlite")
    cache = ActivationCache(db_path=db_path, encryption_key="cache-secret")
    cache.insert(torch.ones(4), torch.zeros(4), "prompt-hash")

    with pytest.raises(ValueError, match="requires encryption_key"):
        ActivationCache(db_path=db_path)
    with pytest.raises(PermissionError, match="encrypted"):
        ActivationCache(db_path=db_path, allow_plaintext=True)


def test_activation_cache_plaintext_requires_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("LATTICESHADOW_CACHE_KEY", raising=False)
    monkeypatch.delenv("LATTICEDB_CACHE_KEY", raising=False)
    db_path = str(tmp_path / "activation_cache.sqlite")

    with pytest.raises(ValueError, match="requires encryption_key"):
        ActivationCache(db_path=db_path)

    cache = ActivationCache(db_path=db_path, allow_plaintext=True)
    cache.insert(torch.ones(4), torch.zeros(4), "prompt-hash")
    assert cache.check_cache(torch.ones(4), threshold=0.99) is not None


def test_encrypted_activation_cache_reads_legacy_plaintext_rows(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.delenv("LATTICESHADOW_CACHE_KEY", raising=False)
    monkeypatch.delenv("LATTICEDB_CACHE_KEY", raising=False)
    db_path = str(tmp_path / "activation_cache.sqlite")
    local = torch.tensor([1.0, 1.0, 0.0, 0.0])
    cloud = torch.tensor([0.0, 0.0, 1.0, 1.0])
    legacy = ActivationCache(db_path=db_path, allow_plaintext=True)
    legacy.insert(local, cloud, "prompt-hash")

    encrypted_reader = ActivationCache(db_path=db_path, encryption_key="cache-secret")
    hit = encrypted_reader.check_cache(local, threshold=0.99)

    assert hit is not None
    assert torch.allclose(hit, cloud)


def test_hopfield_readout_accepts_near_neighbor_below_exact_threshold(tmp_path):
    db_path = str(tmp_path / "activation_cache.sqlite")
    cache = ActivationCache(db_path=db_path, entropy_tolerance=999.0, allow_plaintext=True)
    query = torch.tensor([1.0, 0.0, 0.0, 0.0])
    near_key = torch.tensor([0.8, 0.6, 0.0, 0.0])
    cloud_value = torch.tensor([2.0, 0.0, 0.0, 0.0])

    cache.insert(near_key, cloud_value, "near")

    assert cache.check_cache(query, threshold=0.95) is None
    readout = cache.hopfield_readout(
        query,
        beta=12.0,
        min_confidence=0.9,
        min_similarity=0.75,
    )

    assert readout is not None
    assert torch.allclose(readout, cloud_value)
    assert cache.last_hopfield_status["accepted"] is True
    assert cache.last_hopfield_status["reason"] == "accepted"
    assert cache.last_hopfield_status["max_similarity"] >= 0.75


def test_hopfield_readout_rejects_ambiguous_candidates(tmp_path):
    db_path = str(tmp_path / "activation_cache.sqlite")
    cache = ActivationCache(db_path=db_path, entropy_tolerance=999.0, allow_plaintext=True)
    query = torch.tensor([1.0, 1.0])

    cache.insert(torch.tensor([1.0, 0.0]), torch.tensor([10.0, 0.0]), "x")
    cache.insert(torch.tensor([0.0, 1.0]), torch.tensor([0.0, 10.0]), "y")

    readout = cache.hopfield_readout(
        query,
        beta=1.0,
        min_confidence=0.9,
        max_entropy_ratio=1.0,
    )

    assert readout is None
    assert cache.last_hopfield_status["accepted"] is False
    assert cache.last_hopfield_status["reason"] == "low_confidence"

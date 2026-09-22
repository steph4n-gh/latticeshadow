"""
Tests for scaling, HNSW indexing, and disk-backed distillation in LatticeDB.
"""

import sys
import json
import os
import sqlite3
import logging
import numpy as np
import torch
import pytest
from unittest.mock import MagicMock, patch

from latticeshadow_db.latticedb import connect, Collection, VectorStore, AutoDistiller


class _FakeHNSWState:
    def __init__(self):
        self.efSearch = None


class IndexHNSWFlat:
    """Small FAISS stand-in for tests that can return intentionally rough candidates."""

    def __init__(self, dim, m, metric):
        self.dim = dim
        self.m = m
        self.metric = metric
        self.hnsw = _FakeHNSWState()
        self.vectors = []
        self.search_calls = []

    def add(self, vectors):
        for row in np.asarray(vectors, dtype=np.float32):
            self.vectors.append(row.copy())

    def search(self, queries, k):
        self.search_calls.append(k)
        count = len(self.vectors)
        rough_order = [1, 0, 2] + list(range(3, count))
        rough_order = [idx for idx in rough_order if idx < count]
        rough_order = rough_order[:k]
        padding = max(0, k - len(rough_order))
        indices = np.array([rough_order + [-1] * padding], dtype=np.int64)
        scores = np.linspace(1.0, 0.0, num=k, dtype=np.float32).reshape(1, k)
        return scores, indices


class IndexFlatIP(IndexHNSWFlat):
    def __init__(self, dim):
        super().__init__(dim, m=0, metric="inner_product")


class IndexHNSWFlatWithInvalidCandidates(IndexHNSWFlat):
    def search(self, queries, k):
        self.search_calls.append(k)
        indices = np.array([[99, -1, 1, 1, 0]], dtype=np.int64)
        if k < indices.shape[1]:
            indices = indices[:, :k]
        elif k > indices.shape[1]:
            padding = np.full((1, k - indices.shape[1]), -1, dtype=np.int64)
            indices = np.concatenate([indices, padding], axis=1)
        scores = np.linspace(1.0, 0.0, num=k, dtype=np.float32).reshape(1, k)
        return scores, indices


class FakeHnswlibIndex:
    saved = {}

    def __init__(self, space, dim):
        self.space = space
        self.dim = dim
        self.max_elements = 0
        self.ef_construction = 0
        self.m = 0
        self.ef = 0
        self.vectors = {}

    def init_index(self, max_elements, ef_construction, M):
        self.max_elements = max_elements
        self.ef_construction = ef_construction
        self.m = M

    def set_ef(self, ef):
        self.ef = ef

    def add_items(self, rows, labels):
        for row, label in zip(np.asarray(rows, dtype=np.float32), labels):
            self.vectors[int(label)] = np.asarray(row, dtype=np.float32).copy()

    def save_index(self, path):
        FakeHnswlibIndex.saved[str(path)] = {
            "space": self.space,
            "dim": self.dim,
            "max_elements": self.max_elements,
            "ef_construction": self.ef_construction,
            "m": self.m,
            "ef": self.ef,
            "vectors": {idx: row.copy() for idx, row in self.vectors.items()},
        }
        with open(path, "wb") as handle:
            handle.write(b"fake-hnswlib-index")

    def load_index(self, path, max_elements=None):
        payload = FakeHnswlibIndex.saved[str(path)]
        self.space = payload["space"]
        self.dim = payload["dim"]
        self.max_elements = max_elements or payload["max_elements"]
        self.ef_construction = payload["ef_construction"]
        self.m = payload["m"]
        self.ef = payload["ef"]
        self.vectors = {idx: row.copy() for idx, row in payload["vectors"].items()}

    def knn_query(self, queries, k):
        query = np.asarray(queries[0], dtype=np.float32)
        q_norm = np.linalg.norm(query) or 1.0
        rows = []
        for idx, row in self.vectors.items():
            denom = (np.linalg.norm(row) or 1.0) * q_norm
            sim = float(np.dot(row, query) / denom)
            rows.append((idx, 1.0 - sim))
        rows.sort(key=lambda item: item[1])
        labels = [idx for idx, _ in rows[:k]]
        distances = [dist for _, dist in rows[:k]]
        while len(labels) < k:
            labels.append(-1)
            distances.append(float("inf"))
        return np.asarray([labels], dtype=np.int64), np.asarray([distances], dtype=np.float32)


class FakeHnswlibModule:
    Index = FakeHnswlibIndex


def _mock_faiss_module():
    mock_faiss = MagicMock()
    mock_faiss.IndexHNSWFlat = MagicMock(side_effect=IndexHNSWFlat)
    mock_faiss.IndexFlatIP = MagicMock(side_effect=IndexFlatIP)
    mock_faiss.METRIC_INNER_PRODUCT = "inner_product"
    return mock_faiss


def _mock_hnswlib():
    FakeHnswlibIndex.saved = {}
    return FakeHnswlibModule()


def test_hnsw_indexing_trigger(tmp_path):
    """Test that VectorStore initializes IndexHNSWFlat when vector count exceeds 10k."""
    db_file = str(tmp_path / "hnsw_test.sqlite")
    
    # Mock faiss module
    mock_faiss = _mock_faiss_module()

    # Patch sys.modules to mock faiss import
    with patch.dict("sys.modules", {"faiss": mock_faiss}), \
         patch("latticeshadow_db.latticedb.store._FAISS_AVAILABLE", True):
        
        store = VectorStore(db_path=db_file, collection="hnsw_col")
        
        # Pre-populate dummy vectors in the list to simulate large scale
        dummy_vecs = [torch.randn(128) for _ in range(10005)]
        store._vectors = dummy_vecs
        store._doc_ids = ["dummy"] * 10005
        
        # Trigger FAISS index initialization
        store._init_faiss_index(dim=128)
        
        # Verify IndexHNSWFlat was created with correct args
        assert store._use_faiss
        assert store._faiss_index.__class__.__name__ == "IndexHNSWFlat"
        mock_faiss.IndexHNSWFlat.assert_called_once_with(128, 32, "inner_product")


def test_experimental_hnsw_rerank_forces_hnsw_on_small_indexes(tmp_path):
    """The opt-in rerank mode uses HNSW from the first vector."""
    db_file = str(tmp_path / "hnsw_small.sqlite")
    mock_faiss = _mock_faiss_module()

    with patch.dict("sys.modules", {"faiss": mock_faiss}), \
         patch("latticeshadow_db.latticedb.store._FAISS_AVAILABLE", True):
        store = VectorStore(
            db_path=db_file,
            collection="hnsw_small",
            experimental_index="hnsw_rerank",
        )
        store._doc_ids = ["doc_0"]
        store._init_faiss_index(dim=4)

    assert store._hnsw_rerank_enabled
    assert store._faiss_index.__class__.__name__ == "IndexHNSWFlat"
    assert store._faiss_index.hnsw.efSearch == 128
    mock_faiss.IndexHNSWFlat.assert_called_once_with(4, 32, "inner_product")
    mock_faiss.IndexFlatIP.assert_not_called()


def test_experimental_hnsw_rerank_uses_exact_scores(tmp_path):
    """Approximate HNSW candidate order is exact-reranked before returning."""
    db_file = str(tmp_path / "hnsw_rerank.sqlite")
    mock_faiss = _mock_faiss_module()

    with patch.dict("sys.modules", {"faiss": mock_faiss}), \
         patch("latticeshadow_db.latticedb.store._FAISS_AVAILABLE", True):
        store = VectorStore(
            db_path=db_file,
            collection="hnsw_rerank",
            entropy_tolerance=999.0,
            experimental_index="hnsw_rerank",
        )
        vectors = [
            torch.tensor([1.0, 0.0, 0.0, 0.0]),
            torch.tensor([0.8, 0.2, 0.0, 0.0]),
            torch.tensor([-1.0, 0.0, 0.0, 0.0]),
        ]
        store.insert_batch(
            ["doc_0", "doc_1", "doc_2"],
            vectors,
            documents=["best", "second", "worst"],
        )

        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)

    assert result.ids == ["doc_0", "doc_1"]
    assert result.scores[0] > result.scores[1]
    assert store._faiss_index.search_calls[-1] == 3


def test_experimental_hnsw_rerank_ignores_invalid_candidates(tmp_path):
    db_file = str(tmp_path / "hnsw_invalid.sqlite")
    mock_faiss = _mock_faiss_module()
    mock_faiss.IndexHNSWFlat = MagicMock(side_effect=IndexHNSWFlatWithInvalidCandidates)

    with patch.dict("sys.modules", {"faiss": mock_faiss}), \
         patch("latticeshadow_db.latticedb.store._FAISS_AVAILABLE", True):
        store = VectorStore(
            db_path=db_file,
            collection="hnsw_invalid",
            entropy_tolerance=999.0,
            experimental_index="hnsw_rerank",
        )
        vectors = [
            torch.tensor([1.0, 0.0, 0.0, 0.0]),
            torch.tensor([0.9, 0.1, 0.0, 0.0]),
            torch.tensor([0.0, 1.0, 0.0, 0.0]),
            torch.tensor([0.0, 0.0, 1.0, 0.0]),
            torch.tensor([0.0, 0.0, 0.0, 1.0]),
        ]
        store.insert_batch([f"doc_{idx}" for idx in range(len(vectors))], vectors)

        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)

    assert result.ids == ["doc_0", "doc_1"]


def test_experimental_hnsw_rerank_falls_back_without_faiss(tmp_path):
    with patch("latticeshadow_db.latticedb.store._FAISS_AVAILABLE", False):
        store = VectorStore(
            db_path=str(tmp_path / "hnsw_no_faiss.sqlite"),
            collection="hnsw_no_faiss",
            entropy_tolerance=999.0,
            experimental_index="hnsw_rerank",
        )
        store.insert_batch(
            ["doc_0", "doc_1"],
            [
                torch.tensor([1.0, 0.0, 0.0, 0.0]),
                torch.tensor([0.0, 1.0, 0.0, 0.0]),
            ],
        )
        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=1)

    assert store._use_faiss is False
    assert result.ids == ["doc_0"]


def test_native_hnswlib_sidecar_reloads_and_exact_reranks(tmp_path):
    db_file = str(tmp_path / "native_hnsw.sqlite")
    collection = "native_hnsw"
    fake_hnswlib = _mock_hnswlib()
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.8, 0.2, 0.0, 0.0]),
        torch.tensor([-1.0, 0.0, 0.0, 0.0]),
    ]

    with patch("latticeshadow_db.latticedb.store._HNSWLIB_AVAILABLE", True), \
         patch("latticeshadow_db.latticedb.store.hnswlib", fake_hnswlib), \
         patch("latticeshadow_db.latticedb.store._FAISS_AVAILABLE", False):
        store = VectorStore(
            db_path=db_file,
            collection=collection,
            entropy_tolerance=999.0,
            experimental_index="hnsw_exact_rerank",
            hnsw_m=8,
            hnsw_ef_construction=64,
            hnsw_ef_search=32,
            hnsw_candidate_cap=3,
        )
        store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
        meta = store.build_hnswlib_sidecar(
            m=8,
            ef_construction=64,
            ef_search=32,
            candidate_limit=3,
        )

        assert meta["available"] is True
        assert os.path.exists(store._hnswlib_index_path())
        assert os.path.exists(store._hnswlib_metadata_path())
        assert store._use_faiss is False
        with patch.object(store, "_search_bruteforce", wraps=store._search_bruteforce) as brute_force:
            result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)
        assert result.ids == ["doc_0", "doc_1"]
        assert brute_force.call_count == 0
        del store

        reloaded = VectorStore(
            db_path=db_file,
            collection=collection,
            entropy_tolerance=999.0,
            experimental_index="hnsw_exact_rerank",
            hnsw_m=8,
            hnsw_ef_construction=64,
            hnsw_ef_search=32,
            hnsw_candidate_cap=3,
        )
        assert reloaded._hnswlib_index is not None
        result = reloaded.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)
        assert result.ids == ["doc_0", "doc_1"]

        assert reloaded.delete(["doc_0"]) == 1
        assert not os.path.exists(reloaded._hnswlib_index_path())
        result = reloaded.search(torch.tensor([0.8, 0.2, 0.0, 0.0]), n_results=1)
        assert result.ids == ["doc_1"]
        assert os.path.exists(reloaded._hnswlib_index_path())


def test_native_hnswlib_corrupt_metadata_falls_back_to_exact(tmp_path):
    db_file = str(tmp_path / "native_hnsw_corrupt.sqlite")
    collection = "native_hnsw_corrupt"
    fake_hnswlib = _mock_hnswlib()
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.0, 1.0, 0.0, 0.0]),
    ]

    with patch("latticeshadow_db.latticedb.store._HNSWLIB_AVAILABLE", True), \
         patch("latticeshadow_db.latticedb.store.hnswlib", fake_hnswlib), \
         patch("latticeshadow_db.latticedb.store._FAISS_AVAILABLE", False):
        store = VectorStore(
            db_path=db_file,
            collection=collection,
            entropy_tolerance=999.0,
            experimental_index="hnsw_exact_rerank",
        )
        store.insert_batch(["doc_0", "doc_1"], vectors)
        store.build_hnswlib_sidecar()
        with open(store._hnswlib_metadata_path(), "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        meta["count"] = 999
        with open(store._hnswlib_metadata_path(), "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        del store

        reloaded = VectorStore(
            db_path=db_file,
            collection=collection,
            entropy_tolerance=999.0,
            experimental_index="hnsw_exact_rerank",
        )
        assert reloaded._hnswlib_index is None
        result = reloaded.search(vectors[0], n_results=1)
        assert result.ids == ["doc_0"]


def test_experimental_index_rejects_unknown_values(tmp_path):
    with pytest.raises(ValueError, match="experimental_index"):
        VectorStore(
            db_path=str(tmp_path / "invalid.sqlite"),
            collection="invalid",
            experimental_index="not_a_strategy",
        )


def test_exact_rerank_aliases_set_expected_flags(tmp_path):
    cases = [
        ("hnsw_exact_rerank", False, "_hnsw_rerank_enabled"),
        ("flyhash_exact_rerank", True, "_flyhash_rerank_enabled"),
        ("pq_exact_rerank", False, "_pq_rerank_enabled"),
        ("flyhash_pq_rerank", True, "_flyhash_rerank_enabled"),
        ("cascade_auto", False, "_cascade_auto_enabled"),
    ]
    for strategy, drosophila_hash, flag in cases:
        store = VectorStore(
            db_path=str(tmp_path / f"{strategy}.sqlite"),
            collection=strategy,
            drosophila_hash=drosophila_hash,
            experimental_index=strategy,
        )

        assert getattr(store, flag) is True


def test_flyhash_rerank_requires_drosophila_hash(tmp_path):
    with pytest.raises(ValueError, match="requires drosophila_hash"):
        VectorStore(
            db_path=str(tmp_path / "flyhash_invalid.sqlite"),
            collection="flyhash_invalid",
            experimental_index="flyhash_rerank",
        )
    with pytest.raises(ValueError, match="requires drosophila_hash"):
        VectorStore(
            db_path=str(tmp_path / "flyhash_alias_invalid.sqlite"),
            collection="flyhash_alias_invalid",
            experimental_index="flyhash_exact_rerank",
        )


def test_flyhash_rerank_uses_fp16_sidecar_for_exact_scores(tmp_path):
    db_file = str(tmp_path / "flyhash_rerank.sqlite")
    collection = "flyhash_rerank"
    store = VectorStore(
        db_path=db_file,
        collection=collection,
        drosophila_hash=True,
        entropy_tolerance=999.0,
        experimental_index="flyhash_rerank",
    )
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.8, 0.2, 0.0, 0.0]),
        torch.tensor([-1.0, 0.0, 0.0, 0.0]),
    ]
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
    sidecar_path = f"{db_file}_{collection}_rerank_vectors.bin"
    assert os.path.exists(sidecar_path)
    assert os.path.getsize(sidecar_path) == 100 * 4 * 2

    with patch(
        "latticeshadow_db.latticedb.drosophila.compute_hamming_distance",
        return_value=np.array([1, 0, 2]),
    ):
        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)

    assert result.ids == ["doc_0", "doc_1"]
    assert result.scores[0] > result.scores[1]


def test_flyhash_rerank_sidecar_survives_reload_and_delete(tmp_path):
    db_file = str(tmp_path / "flyhash_reload.sqlite")
    collection = "flyhash_reload"
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.0, 1.0, 0.0, 0.0]),
        torch.tensor([0.0, 0.0, 1.0, 0.0]),
    ]
    store = VectorStore(
        db_path=db_file,
        collection=collection,
        drosophila_hash=True,
        entropy_tolerance=999.0,
        experimental_index="flyhash_rerank",
    )
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
    store._rerank_memmap.flush()
    del store

    reloaded = VectorStore(
        db_path=db_file,
        collection=collection,
        drosophila_hash=True,
        entropy_tolerance=999.0,
        experimental_index="flyhash_rerank",
    )
    with patch(
        "latticeshadow_db.latticedb.drosophila.compute_hamming_distance",
        return_value=np.array([0, 1, 2]),
    ):
        result = reloaded.search(vectors[0], n_results=1)
    assert result.ids == ["doc_0"]

    assert reloaded.delete(["doc_0"]) == 1
    with patch(
        "latticeshadow_db.latticedb.drosophila.compute_hamming_distance",
        return_value=np.array([0, 1]),
    ):
        result = reloaded.search(vectors[1], n_results=1)
    assert result.ids == ["doc_1"]


def test_pq_rerank_rejects_drosophila_hash(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        VectorStore(
            db_path=str(tmp_path / "pq_invalid.sqlite"),
            collection="pq_invalid",
            drosophila_hash=True,
            experimental_index="pq_rerank",
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        VectorStore(
            db_path=str(tmp_path / "pq_alias_invalid.sqlite"),
            collection="pq_alias_invalid",
            drosophila_hash=True,
            experimental_index="pq_exact_rerank",
        )


def test_pq_rerank_uses_compressed_candidates_then_exact_scores(tmp_path):
    db_file = str(tmp_path / "pq_rerank.sqlite")
    collection = "pq_rerank"
    store = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="pq_rerank",
    )
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.8, 0.2, 0.0, 0.0]),
        torch.tensor([-1.0, 0.0, 0.0, 0.0]),
    ]
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
    sidecar_path = f"{db_file}_{collection}_pq_codes.bin"
    assert os.path.exists(sidecar_path)
    assert os.path.getsize(sidecar_path) == 100 * 4

    with patch.object(store, "_search_bruteforce", wraps=store._search_bruteforce) as brute:
        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)

    assert result.ids == ["doc_0", "doc_1"]
    assert result.scores[0] > result.scores[1]
    assert brute.call_args.args[3] is not None


def test_pq_rerank_sidecar_survives_reload_and_delete(tmp_path):
    db_file = str(tmp_path / "pq_reload.sqlite")
    collection = "pq_reload"
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.0, 1.0, 0.0, 0.0]),
        torch.tensor([0.0, 0.0, 1.0, 0.0]),
    ]
    store = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="pq_rerank",
    )
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
    store._pq_memmap.flush()
    del store

    reloaded = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="pq_rerank",
    )
    result = reloaded.search(vectors[0], n_results=1)
    assert result.ids == ["doc_0"]

    assert reloaded.delete(["doc_0"]) == 1
    result = reloaded.search(vectors[1], n_results=1)
    assert result.ids == ["doc_1"]


def test_diskann_rerank_rejects_drosophila_hash(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        VectorStore(
            db_path=str(tmp_path / "diskann_invalid.sqlite"),
            collection="diskann_invalid",
            drosophila_hash=True,
            experimental_index="diskann_rerank",
        )


def test_diskann_rerank_uses_ssd_sidecars_then_exact_scores(tmp_path):
    db_file = str(tmp_path / "diskann_rerank.sqlite")
    collection = "diskann_rerank"
    store = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="diskann_rerank",
    )
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.8, 0.2, 0.0, 0.0]),
        torch.tensor([-1.0, 0.0, 0.0, 0.0]),
    ]
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)

    vector_path = f"{db_file}_{collection}_diskann_vectors.bin"
    graph_path = f"{db_file}_{collection}_diskann_graph.bin"
    assert os.path.exists(vector_path)
    assert os.path.getsize(vector_path) == 100 * 4 * 2
    assert os.path.exists(graph_path)
    assert os.path.getsize(graph_path) == 100 * store._diskann_graph_degree * 4

    with patch.object(store, "_search_bruteforce", wraps=store._search_bruteforce) as brute:
        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)

    assert result.ids == ["doc_0", "doc_1"]
    assert result.scores[0] > result.scores[1]
    assert brute.call_args.args[3] is not None
    assert store._use_faiss is False


def test_diskann_rerank_sidecars_survive_reload_and_delete(tmp_path):
    db_file = str(tmp_path / "diskann_reload.sqlite")
    collection = "diskann_reload"
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.0, 1.0, 0.0, 0.0]),
        torch.tensor([0.0, 0.0, 1.0, 0.0]),
    ]
    store = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="diskann_rerank",
    )
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
    store._diskann_vector_memmap.flush()
    store._diskann_graph_memmap.flush()
    del store

    reloaded = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="diskann_rerank",
    )
    result = reloaded.search(vectors[0], n_results=1)
    assert result.ids == ["doc_0"]

    assert reloaded.delete(["doc_0"]) == 1
    result = reloaded.search(vectors[1], n_results=1)
    assert result.ids == ["doc_1"]


def test_streaming_exact_rejects_drosophila_hash(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        VectorStore(
            db_path=str(tmp_path / "streaming_invalid.sqlite"),
            collection="streaming_invalid",
            drosophila_hash=True,
            experimental_index="streaming_exact",
        )


def test_streaming_exact_uses_memmap_without_faiss(tmp_path):
    db_file = str(tmp_path / "streaming_exact.sqlite")
    store = VectorStore(
        db_path=db_file,
        collection="streaming_exact",
        entropy_tolerance=999.0,
        experimental_index="streaming_exact",
    )
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.9, 0.1, 0.0, 0.0]),
        torch.tensor([-1.0, 0.0, 0.0, 0.0]),
    ]
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)

    with patch.object(store, "_search_bruteforce", wraps=store._search_bruteforce) as brute:
        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)

    assert result.ids == ["doc_0", "doc_1"]
    assert brute.call_count == 0
    assert store._use_faiss is False
    assert store._streaming_exact_inv_norms is not None


def test_cascade_auto_conservatively_uses_streaming_exact(tmp_path):
    db_file = str(tmp_path / "cascade_auto.sqlite")
    store = VectorStore(
        db_path=db_file,
        collection="cascade_auto",
        entropy_tolerance=999.0,
        experimental_index="cascade_auto",
    )
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.9, 0.1, 0.0, 0.0]),
        torch.tensor([-1.0, 0.0, 0.0, 0.0]),
    ]
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)

    with patch.object(store, "_search_bruteforce", wraps=store._search_bruteforce) as brute:
        result = store.search(torch.tensor([1.0, 0.0, 0.0, 0.0]), n_results=2)

    assert store._cascade_auto_enabled is True
    assert store._streaming_exact_enabled is True
    assert result.ids == ["doc_0", "doc_1"]
    assert brute.call_count == 0


def test_streaming_exact_survives_reload_delete_clear_and_candidate_filter(tmp_path):
    db_file = str(tmp_path / "streaming_reload.sqlite")
    collection = "streaming_reload"
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.0, 1.0, 0.0, 0.0]),
        torch.tensor([0.0, 0.0, 1.0, 0.0]),
    ]
    store = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="streaming_exact",
    )
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
    store._memmap.flush()
    del store

    reloaded = VectorStore(
        db_path=db_file,
        collection=collection,
        entropy_tolerance=999.0,
        experimental_index="streaming_exact",
    )
    assert reloaded._streaming_exact_inv_norms is not None
    norm_path = reloaded._streaming_norms_path()
    meta_path = reloaded._streaming_norms_metadata_path()
    assert os.path.exists(norm_path)
    assert os.path.exists(meta_path)
    with open(meta_path, "r", encoding="utf-8") as handle:
        norm_meta = json.load(handle)
    assert norm_meta["count"] == 3
    assert norm_meta["dim"] == 4
    result = reloaded.search(vectors[0], n_results=1)
    assert result.ids == ["doc_0"]

    filtered = reloaded.search(
        vectors[1],
        n_results=1,
        candidate_ids=["doc_1", "doc_2"],
    )
    assert filtered.ids == ["doc_1"]

    assert reloaded.delete(["doc_0"]) == 1
    assert reloaded._streaming_exact_inv_norms is None
    assert not os.path.exists(norm_path)
    assert not os.path.exists(meta_path)
    result = reloaded.search(vectors[1], n_results=1)
    assert result.ids == ["doc_1"]
    assert reloaded._streaming_exact_inv_norms is not None
    assert os.path.exists(norm_path)

    reloaded.clear()
    assert reloaded.count() == 0
    assert reloaded._streaming_exact_inv_norms is None
    assert not os.path.exists(norm_path)
    assert reloaded.search(vectors[1], n_results=1).ids == []


def test_cold_reload_preserves_grown_memmap_capacity(tmp_path, caplog):
    db_file = str(tmp_path / "cold_reload.sqlite")
    collection = "cold_reload"
    vectors = [torch.randn(4) for _ in range(101)]
    store = VectorStore(db_path=db_file, collection=collection, entropy_tolerance=999.0)
    store.insert_batch([f"doc_{idx}" for idx in range(len(vectors))], vectors)

    memmap_path = f"{db_file}_{collection}_vectors.bin"
    grown_size = os.path.getsize(memmap_path)
    assert store._memmap_capacity == 200
    assert grown_size == 200 * 4 * 4
    store._memmap.flush()
    del store

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="latticeshadow_db.latticedb.store"):
        reloaded = VectorStore(db_path=db_file, collection=collection, entropy_tolerance=999.0)

    assert reloaded._memmap_capacity == 200
    assert os.path.getsize(memmap_path) == grown_size
    assert "self-healing" not in caplog.text
    result = reloaded.search(vectors[0], n_results=1)
    assert result.ids == ["doc_0"]


def test_cold_reload_rebuilds_undersized_memmap(tmp_path, caplog):
    db_file = str(tmp_path / "cold_reload_corrupt.sqlite")
    collection = "cold_reload_corrupt"
    vectors = [torch.randn(4) for _ in range(3)]
    store = VectorStore(db_path=db_file, collection=collection, entropy_tolerance=999.0)
    store.insert_batch([f"doc_{idx}" for idx in range(len(vectors))], vectors)

    memmap_path = f"{db_file}_{collection}_vectors.bin"
    store._memmap.flush()
    del store
    with open(memmap_path, "r+b") as handle:
        handle.truncate(4 * 4)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="latticeshadow_db.latticedb.store"):
        reloaded = VectorStore(db_path=db_file, collection=collection, entropy_tolerance=999.0)

    assert reloaded._memmap_capacity >= len(vectors)
    assert "self-healing" in caplog.text
    result = reloaded.search(vectors[0], n_results=1)
    assert result.ids == ["doc_0"]


def test_disk_backed_distillation_recording(tmp_path):
    """Verify that AutoDistiller serializes and records pairs to SQLite database."""
    db_file = str(tmp_path / "distill_test.sqlite")
    
    distiller = AutoDistiller(
        dim=64,
        min_samples=100,
        db_path=db_file,
        collection="distill_col",
        max_training_pairs=5,
    )
    
    # Record pairs
    for i in range(10):
        distiller.record_pair(torch.ones(64) * i, torch.ones(64) * (i + 1))
        
    assert distiller.sample_count == 10
    
    # Verify records exist in SQLite
    with sqlite3.connect(db_file) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM distillation_pairs WHERE collection = 'distill_col'")
        assert cursor.fetchone()[0] == 10
        
    # Trigger manual training to test SQLite deserialization during train
    loss = distiller.train(epochs=2)
    assert loss < float('inf')

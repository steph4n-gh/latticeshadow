import json
import os

import numpy as np
import torch
from unittest.mock import patch

from latticeshadow_db.latticedb import VectorStore
from latticeshadow_db.scale_benchmarks import (
    ScaleHarnessConfig,
    corpus_path,
    generate_synthetic_memmaps,
    open_vector_memmap,
    query_path,
    run_scale_harness,
    streaming_exact_topk,
)


class FakeHnswlibIndex:
    saved = {}

    def __init__(self, space, dim):
        self.space = space
        self.dim = dim
        self.vectors = {}

    def init_index(self, max_elements, ef_construction, M):
        self.max_elements = max_elements
        self.ef_construction = ef_construction
        self.m = M

    def set_ef(self, ef):
        self.ef = ef

    def add_items(self, rows, labels):
        for row, label in zip(np.asarray(rows, dtype=np.float32), labels):
            self.vectors[int(label)] = row.copy()

    def save_index(self, path):
        FakeHnswlibIndex.saved[str(path)] = {
            "vectors": {idx: row.copy() for idx, row in self.vectors.items()},
            "max_elements": getattr(self, "max_elements", len(self.vectors)),
        }
        with open(path, "wb") as handle:
            handle.write(b"fake-hnswlib-index")

    def load_index(self, path, max_elements=None):
        payload = FakeHnswlibIndex.saved[str(path)]
        self.max_elements = max_elements or payload["max_elements"]
        self.vectors = {idx: row.copy() for idx, row in payload["vectors"].items()}

    def knn_query(self, queries, k):
        query = np.asarray(queries[0], dtype=np.float32)
        q_norm = np.linalg.norm(query) or 1.0
        scored = []
        for idx, row in self.vectors.items():
            denom = (np.linalg.norm(row) or 1.0) * q_norm
            scored.append((idx, 1.0 - float(np.dot(row, query) / denom)))
        scored.sort(key=lambda item: item[1])
        labels = [idx for idx, _ in scored[:k]]
        distances = [dist for _, dist in scored[:k]]
        while len(labels) < k:
            labels.append(-1)
            distances.append(float("inf"))
        return np.asarray([labels], dtype=np.int64), np.asarray([distances], dtype=np.float32)


class FakeHnswlibModule:
    Index = FakeHnswlibIndex


def test_synthetic_memmap_generation_is_stable(tmp_path):
    config = ScaleHarnessConfig(count=32, dim=8, queries=3, seed=123, chunk_size=8)

    generate_synthetic_memmaps(config, tmp_path)
    first = open_vector_memmap(corpus_path(tmp_path), config.count, config.dim, mode="r")
    first_row = np.asarray(first[7], dtype=np.float32).copy()
    del first

    generate_synthetic_memmaps(config, tmp_path)
    second = open_vector_memmap(corpus_path(tmp_path), config.count, config.dim, mode="r")

    assert np.allclose(first_row, np.asarray(second[7], dtype=np.float32))


def test_streaming_exact_topk_matches_in_memory_scores(tmp_path):
    config = ScaleHarnessConfig(count=48, dim=8, queries=4, k=5, seed=77, chunk_size=9)
    meta = generate_synthetic_memmaps(config, tmp_path)
    vectors = open_vector_memmap(corpus_path(tmp_path), config.count, config.dim, mode="r")
    queries = open_vector_memmap(query_path(tmp_path), config.queries, config.dim, mode="r")

    actual, _ = streaming_exact_topk(
        corpus_path(tmp_path),
        np.asarray(queries, dtype=np.float32),
        config.count,
        config.dim,
        config.k,
        config.chunk_size,
    )
    scores = np.asarray(vectors, dtype=np.float32) @ np.asarray(queries[0], dtype=np.float32)
    expected_idx = np.argsort(scores, kind="stable")[::-1][:config.k]
    expected = [f"doc_{int(idx)}" for idx in expected_idx]

    assert meta["count"] == config.count
    assert actual[0] == expected


def test_diskann_sidecar_metadata_rebuilds_after_corruption(tmp_path):
    db_file = str(tmp_path / "diskann_meta.sqlite")
    store = VectorStore(
        db_path=db_file,
        collection="diskann_meta",
        entropy_tolerance=999.0,
        experimental_index="diskann_rerank",
    )
    vectors = [
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.0, 1.0, 0.0, 0.0]),
        torch.tensor([0.0, 0.0, 1.0, 0.0]),
    ]
    store.insert_batch(["doc_0", "doc_1", "doc_2"], vectors)
    meta_path = store._diskann_metadata_path()
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    assert meta["count"] == 3
    meta["count"] = 2
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    del store

    reloaded = VectorStore(
        db_path=db_file,
        collection="diskann_meta",
        entropy_tolerance=999.0,
        experimental_index="diskann_rerank",
    )
    result = reloaded.search(vectors[0], n_results=1)

    assert result.ids == ["doc_0"]
    with open(reloaded._diskann_metadata_path(), "r", encoding="utf-8") as handle:
        repaired = json.load(handle)
    assert repaired["count"] == 3
    assert "config_hash" in repaired


def test_diskann_vectorized_scan_matches_sidecar_scores(tmp_path):
    db_file = str(tmp_path / "diskann_scan.sqlite")
    store = VectorStore(
        db_path=db_file,
        collection="diskann_scan",
        entropy_tolerance=999.0,
        experimental_index="diskann_rerank",
        diskann_candidate_cap=5,
    )
    generator = torch.Generator(device="cpu")
    generator.manual_seed(123)
    vectors = torch.nn.functional.normalize(
        torch.randn(16, 8, generator=generator, dtype=torch.float32),
        p=2,
        dim=1,
    )
    ids = [f"doc_{idx}" for idx in range(vectors.shape[0])]
    store.insert_batch(ids, [row for row in vectors])
    store.build_diskann_sidecars(partition_count=4, probe_count=4, candidate_limit=5)

    query = vectors[3]
    query_code = store._diskann_encode_vector(query).astype(np.float32, copy=False)
    rows = np.asarray(store._diskann_vector_memmap[: len(ids)], dtype=np.float32)
    scores = rows @ query_code
    expected_order = np.lexsort((np.arange(len(ids)), -scores))[:5]

    assert store._diskann_scan_candidates(query, 0.0, range(len(ids)), 5) == [
        int(idx) for idx in expected_order
    ]


def test_streaming_exact_index_matches_cosine_order_for_unnormalized_vectors(tmp_path):
    store = VectorStore(
        db_path=str(tmp_path / "streaming_exact.sqlite"),
        collection="streaming_exact",
        entropy_tolerance=999.0,
        experimental_index="streaming_exact",
    )
    vectors = [
        torch.tensor([2.0, 0.0]),
        torch.tensor([0.0, 3.0]),
        torch.tensor([1.0, 1.0]),
        torch.tensor([-1.0, 0.0]),
    ]
    ids = [f"doc_{idx}" for idx in range(len(vectors))]
    store.insert_batch(ids, vectors)

    result = store.search(torch.tensor([1.0, 0.0]), n_results=3)

    assert result.ids == ["doc_0", "doc_2", "doc_1"]
    assert np.allclose(result.scores, [1.0, 2.0 ** -0.5, 0.0], atol=1e-6)


def test_scale_harness_runs_tiny_end_to_end(tmp_path):
    config = ScaleHarnessConfig(
        count=64,
        dim=16,
        queries=3,
        k=5,
        seed=5,
        modes=("dense_exact_public", "dense_exact_streaming", "diskann_rerank"),
        chunk_size=16,
        insert_chunk_size=16,
        diskann_partitions=8,
        diskann_probes=8,
        diskann_candidates=64,
    )

    result = run_scale_harness(config, workspace=tmp_path)

    modes = {item["mode"]: item for item in result["modes"]}
    assert set(modes) == {"dense_exact_public", "dense_exact_streaming", "diskann_rerank"}
    assert modes["dense_exact_streaming"]["recall_at_k"] == 1.0
    assert modes["diskann_rerank"]["recall_at_k"] >= 0.95
    assert result["gates"]["best_mode"] == "diskann_rerank"
    assert os.path.exists(tmp_path / "scale_report.json")


def test_scale_harness_runs_new_cascade_modes(tmp_path):
    config = ScaleHarnessConfig(
        count=32,
        dim=8,
        queries=3,
        k=3,
        seed=5,
        modes=(
            "dense_exact_public",
            "streaming_exact_public",
            "hnsw_exact_rerank",
            "pq_exact_rerank",
            "cascade_auto",
        ),
        chunk_size=8,
        insert_chunk_size=8,
    )

    result = run_scale_harness(config, workspace=tmp_path)
    modes = {item["mode"]: item for item in result["modes"]}

    assert modes["hnsw_exact_rerank"]["experimental_index"] == "hnsw_exact_rerank"
    assert modes["pq_exact_rerank"]["experimental_index"] == "pq_exact_rerank"
    assert modes["cascade_auto"]["experimental_index"] == "cascade_auto"
    assert modes["cascade_auto"]["diskann_sidecar"]["selected_index"] == "streaming_exact"
    assert modes["cascade_auto"]["recall_at_k"] == 1.0
    assert "optional_accelerators" in result["environment"]
    assert "sidecar_audit" in modes["cascade_auto"]


def test_scale_harness_reports_native_hnswlib_sidecar(tmp_path):
    FakeHnswlibIndex.saved = {}
    config = ScaleHarnessConfig(
        count=32,
        dim=8,
        queries=3,
        k=3,
        seed=5,
        modes=("dense_exact_public", "hnsw_exact_rerank"),
        chunk_size=8,
        insert_chunk_size=8,
        hnsw_m=8,
        hnsw_ef_construction=32,
        hnsw_ef_search=16,
        hnsw_candidates=16,
    )

    with patch("latticeshadow_db.latticedb.store._HNSWLIB_AVAILABLE", True), \
         patch("latticeshadow_db.latticedb.store.hnswlib", FakeHnswlibModule()):
        result = run_scale_harness(config, workspace=tmp_path)

    hnsw = {item["mode"]: item for item in result["modes"]}["hnsw_exact_rerank"]
    assert hnsw["hnswlib_sidecar"]["available"] is True
    assert hnsw["hnswlib_sidecar"]["m"] == 8
    assert hnsw["hnswlib_sidecar"]["ef_search"] == 16
    assert hnsw["served_by_hnswlib"] is True
    assert hnsw["accelerator_missing"] is False
    assert hnsw["recall_at_k"] >= 0.95

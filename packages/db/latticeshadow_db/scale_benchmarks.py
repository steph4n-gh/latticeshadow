"""Scale benchmark harness for moonshot retrieval proof runs.

This module is intentionally separate from ``benchmarks.py``. It is built for
large synthetic corpora that must live on disk, and every expensive stage can
be resumed from files in a workspace directory.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import torch

from .latticedb.collection import Collection


SCHEMA_VERSION = 1
AVAILABLE_MODES = (
    "dense_exact_public",
    "dense_exact_streaming",
    "streaming_exact_public",
    "hnsw_exact_rerank",
    "flyhash_exact_rerank",
    "pq_exact_rerank",
    "flyhash_pq_rerank",
    "cascade_auto",
    "diskann_rerank",
    "pq_rerank",
)
EXPERIMENTAL_MODE_TO_INDEX = {
    "streaming_exact_public": "streaming_exact",
    "hnsw_exact_rerank": "hnsw_exact_rerank",
    "flyhash_exact_rerank": "flyhash_exact_rerank",
    "pq_exact_rerank": "pq_exact_rerank",
    "flyhash_pq_rerank": "flyhash_pq_rerank",
    "cascade_auto": "cascade_auto",
    "diskann_rerank": "diskann_rerank",
    "pq_rerank": "pq_rerank",
}
DROSOPHILA_MODES = {"flyhash_exact_rerank", "flyhash_pq_rerank"}


@dataclass(frozen=True)
class ScaleHarnessConfig:
    count: int = 10_000
    dim: int = 128
    queries: int = 10
    warmup_queries: int = 0
    k: int = 10
    seed: int = 42
    modes: tuple[str, ...] = ("dense_exact_public", "dense_exact_streaming", "diskann_rerank")
    chunk_size: int = 4096
    insert_chunk_size: int = 2048
    hnsw_m: int = 32
    hnsw_ef_construction: int = 200
    hnsw_ef_search: int = 128
    hnsw_candidates: int = 1000
    diskann_partitions: int | None = None
    diskann_probes: int | None = None
    diskann_candidates: int | None = None
    diskann_graph_degree: int | None = None
    diskann_full_scan_threshold: int | None = None
    diskann_kmeans_iterations: int = 1
    recall_gate: float = 0.95
    speedup_gate: float = 1000.0
    latency_gate_ms: float = 50.0
    storage_gate_bytes: int = 25 * 1024 * 1024 * 1024

    def validate(self) -> None:
        if self.count <= 1:
            raise ValueError("count must be greater than 1")
        if self.dim <= 0:
            raise ValueError("dim must be positive")
        if self.queries <= 0:
            raise ValueError("queries must be positive")
        if self.warmup_queries < 0:
            raise ValueError("warmup_queries cannot be negative")
        if self.k <= 0:
            raise ValueError("k must be positive")
        if self.chunk_size <= 0 or self.insert_chunk_size <= 0:
            raise ValueError("chunk sizes must be positive")
        if self.hnsw_m <= 0:
            raise ValueError("hnsw_m must be positive")
        if self.hnsw_ef_construction <= 0:
            raise ValueError("hnsw_ef_construction must be positive")
        if self.hnsw_ef_search <= 0:
            raise ValueError("hnsw_ef_search must be positive")
        if self.hnsw_candidates <= 0:
            raise ValueError("hnsw_candidates must be positive")
        if self.diskann_graph_degree is not None and self.diskann_graph_degree <= 0:
            raise ValueError("diskann_graph_degree must be positive")
        if self.diskann_full_scan_threshold is not None and self.diskann_full_scan_threshold <= 0:
            raise ValueError("diskann_full_scan_threshold must be positive")
        if self.diskann_kmeans_iterations <= 0:
            raise ValueError("diskann_kmeans_iterations must be positive")
        unknown = sorted(set(self.modes) - set(AVAILABLE_MODES))
        if unknown:
            raise ValueError(f"unknown scale benchmark modes: {unknown}")


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_values = sorted(float(v) for v in values)
    rank = (len(sorted_values) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def normalize_rows_np(rows: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return rows / norms


def deterministic_vector(index: int, dim: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed + int(index))
    row = rng.standard_normal(dim, dtype=np.float32).reshape(1, dim)
    return normalize_rows_np(row)[0].astype(np.float32, copy=False)


def corpus_path(workspace: Path) -> Path:
    return workspace / "synthetic_vectors_f32.bin"


def query_path(workspace: Path) -> Path:
    return workspace / "queries_f32.bin"


def result_path(workspace: Path) -> Path:
    return workspace / "scale_report.json"


def _metadata_path(workspace: Path) -> Path:
    return workspace / "synthetic_metadata.json"


def open_vector_memmap(path: Path, count: int, dim: int, mode: str = "r+") -> np.memmap:
    return np.memmap(path, dtype="float32", mode=mode, shape=(count, dim))


def generate_synthetic_memmaps(config: ScaleHarnessConfig, workspace: Path) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    vectors_path = corpus_path(workspace)
    queries_path = query_path(workspace)
    expected_size = config.count * config.dim * 4
    if vectors_path.exists() and vectors_path.stat().st_size == expected_size:
        vectors = open_vector_memmap(vectors_path, config.count, config.dim)
    else:
        generator = torch.Generator(device="cpu")
        generator.manual_seed(config.seed)
        vectors = open_vector_memmap(vectors_path, config.count, config.dim, mode="w+")
        for start in range(0, config.count, config.chunk_size):
            end = min(start + config.chunk_size, config.count)
            rows = torch.randn(end - start, config.dim, generator=generator, dtype=torch.float32)
            rows = torch.nn.functional.normalize(rows, p=2, dim=1)
            vectors[start:end] = rows.numpy()
        vectors.flush()

    query_count = min(config.queries, config.count)
    queries = open_vector_memmap(queries_path, query_count, config.dim, mode="w+")
    queries[:] = np.asarray(vectors[:query_count], dtype=np.float32)
    queries.flush()
    meta = {
        "schema_version": SCHEMA_VERSION,
        "count": config.count,
        "dim": config.dim,
        "queries": query_count,
        "seed": config.seed,
        "vectors_path": str(vectors_path),
        "queries_path": str(queries_path),
    }
    _metadata_path(workspace).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta


def streaming_exact_topk(
    vectors_path: Path,
    queries: np.ndarray,
    count: int,
    dim: int,
    k: int,
    chunk_size: int,
) -> tuple[list[list[str]], list[float]]:
    vectors = open_vector_memmap(vectors_path, count, dim, mode="r")
    all_ids: list[list[str]] = []
    latencies: list[float] = []
    for query in queries:
        start_time = time.perf_counter_ns()
        best_scores = np.full(k, -np.inf, dtype=np.float32)
        best_indices = np.full(k, -1, dtype=np.int64)
        for start in range(0, count, chunk_size):
            end = min(start + chunk_size, count)
            scores = np.asarray(vectors[start:end], dtype=np.float32) @ query.astype(np.float32)
            take = min(k, scores.shape[0])
            local_idx = np.argpartition(scores, -take)[-take:]
            merged_scores = np.concatenate([best_scores, scores[local_idx]])
            merged_indices = np.concatenate([best_indices, local_idx.astype(np.int64) + start])
            keep = np.argpartition(merged_scores, -k)[-k:]
            best_scores = merged_scores[keep]
            best_indices = merged_indices[keep]
        order = np.argsort(best_scores, kind="stable")[::-1]
        all_ids.append([f"doc_{int(best_indices[idx])}" for idx in order if best_indices[idx] >= 0])
        latencies.append((time.perf_counter_ns() - start_time) / 1_000_000.0)
    return all_ids, latencies


def recall_at_k(expected: Sequence[str], actual: Sequence[str], k: int) -> float:
    expected_set = set(expected[:k])
    return len(expected_set & set(actual[:k])) / float(len(expected_set)) if expected_set else 0.0


def mrr_at_k(expected: Sequence[str], actual: Sequence[str], k: int) -> float:
    expected_set = set(expected[:k])
    for rank, doc_id in enumerate(actual[:k], start=1):
        if doc_id in expected_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(expected: Sequence[str], actual: Sequence[str], k: int) -> float:
    expected_set = set(expected[:k])
    dcg = 0.0
    for rank, doc_id in enumerate(actual[:k], start=1):
        if doc_id in expected_set:
            dcg += 1.0 / math.log2(rank + 1)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(k, len(expected_set)) + 1))
    return dcg / ideal if ideal > 0 else 0.0


def summarize_latencies(latencies: Sequence[float]) -> dict:
    return {
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99),
        "max_ms": max(latencies) if latencies else 0.0,
    }


def storage_bytes(paths: Iterable[Path]) -> int:
    return sum(path.stat().st_size for path in paths if path.exists())


def collection_storage_paths(db_path: Path, collection: str) -> list[Path]:
    suffixes = [
        "",
        f"_{collection}_vectors.bin",
        f"_{collection}_rerank_vectors.bin",
        f"_{collection}_pq_codes.bin",
        f"_{collection}_hnswlib.bin",
        f"_{collection}_hnswlib_meta.json",
        f"_{collection}_diskann_vectors.bin",
        f"_{collection}_diskann_graph.bin",
        f"_{collection}_diskann_meta.json",
        f"_{collection}_diskann_centroids.npy",
        f"_{collection}_diskann_partition_offsets.npy",
        f"_{collection}_diskann_partition_indices.bin",
        f"_{collection}_diskann_assignments.bin",
        f"_{collection}_streaming_norms.bin",
        f"_{collection}_streaming_norms_meta.json",
        "-wal",
        "-shm",
    ]
    return [Path(f"{db_path}{suffix}") for suffix in suffixes]


def collection_sidecar_audit(db_path: Path, collection: str, privacy_enabled: bool = False) -> dict:
    labels = {
        "": ("sqlite", False),
        f"_{collection}_vectors.bin": ("vector_memmap", True),
        f"_{collection}_rerank_vectors.bin": ("flyhash_rerank_fp16", True),
        f"_{collection}_pq_codes.bin": ("pq_codes_int8", True),
        f"_{collection}_hnswlib.bin": ("hnswlib_index", True),
        f"_{collection}_hnswlib_meta.json": ("hnswlib_metadata", True),
        f"_{collection}_diskann_vectors.bin": ("diskann_vectors_fp16", True),
        f"_{collection}_diskann_graph.bin": ("diskann_graph_uint32", True),
        f"_{collection}_diskann_meta.json": ("diskann_metadata", True),
        f"_{collection}_diskann_centroids.npy": ("diskann_centroids", True),
        f"_{collection}_diskann_partition_offsets.npy": ("diskann_partition_offsets", True),
        f"_{collection}_diskann_partition_indices.bin": ("diskann_partition_indices", True),
        f"_{collection}_diskann_assignments.bin": ("diskann_assignments", True),
        f"_{collection}_streaming_norms.bin": ("streaming_norms_f32", True),
        f"_{collection}_streaming_norms_meta.json": ("streaming_norms_metadata", True),
        "-wal": ("sqlite_wal", False),
        "-shm": ("sqlite_shm", False),
    }
    sidecars = []
    total_bytes = 0
    plaintext_bytes = 0
    for suffix, (label, plaintext) in labels.items():
        path = Path(f"{db_path}{suffix}")
        if not path.exists():
            continue
        size = path.stat().st_size
        total_bytes += size
        if plaintext:
            plaintext_bytes += size
        sidecars.append(
            {
                "label": label,
                "path": str(path),
                "bytes": size,
                "plaintext": plaintext,
            }
        )
    return {
        "total_bytes": total_bytes,
        "plaintext_sidecar_bytes": plaintext_bytes,
        "privacy_enabled": privacy_enabled,
        "privacy_warning": bool(privacy_enabled and plaintext_bytes > 0),
        "sidecars": sidecars,
    }


def optional_accelerator_status() -> dict:
    from .latticedb import store as store_module

    return {
        "faiss": bool(store_module._FAISS_AVAILABLE),
        "hnswlib": bool(store_module._HNSWLIB_AVAILABLE),
        "hnswlib_importable": importlib.util.find_spec("hnswlib") is not None,
    }


def build_embedding_fn(vectors_path: Path, count: int, dim: int):
    vectors = open_vector_memmap(vectors_path, count, dim, mode="r")

    def embed(text: str) -> torch.Tensor:
        try:
            idx = int(str(text).rsplit("_", 1)[1])
        except (IndexError, ValueError):
            idx = 0
        idx = max(0, min(idx, count - 1))
        return torch.from_numpy(np.asarray(vectors[idx], dtype=np.float32).copy())

    return embed


def build_collection_mode(
    mode: str,
    config: ScaleHarnessConfig,
    workspace: Path,
    vectors_path: Path,
) -> tuple[Collection, dict]:
    collection = f"scale_{mode}"
    db_path = workspace / f"{collection}.sqlite"
    embedding_fn = build_embedding_fn(vectors_path, config.count, config.dim)
    experimental_index = EXPERIMENTAL_MODE_TO_INDEX.get(mode)
    drosophila_hash = mode in DROSOPHILA_MODES

    build_start = time.perf_counter_ns()
    coll = Collection(
        name=collection,
        db_path=str(db_path),
        embedding_fn=embedding_fn,
        embedding_dim=config.dim,
        experimental_index=experimental_index,
        drosophila_hash=drosophila_hash,
        entropy_tolerance=999.0,
        hnsw_m=config.hnsw_m,
        hnsw_ef_construction=config.hnsw_ef_construction,
        hnsw_ef_search=config.hnsw_ef_search,
        hnsw_candidate_cap=config.hnsw_candidates,
        diskann_graph_degree=config.diskann_graph_degree or 16,
        diskann_probe_count=config.diskann_probes or 8,
        diskann_candidate_cap=config.diskann_candidates or 2000,
        diskann_full_scan_threshold=config.diskann_full_scan_threshold or 4096,
    )
    if coll._store.count() < config.count:
        start_idx = coll._store.count()
        if mode == "diskann_rerank":
            with coll._store.defer_diskann_sidecar_build():
                for start in range(start_idx, config.count, config.insert_chunk_size):
                    end = min(start + config.insert_chunk_size, config.count)
                    docs = [f"doc_{idx}" for idx in range(start, end)]
                    coll.add(documents=docs, ids=docs)
        else:
            for start in range(start_idx, config.count, config.insert_chunk_size):
                end = min(start + config.insert_chunk_size, config.count)
                docs = [f"doc_{idx}" for idx in range(start, end)]
                coll.add(documents=docs, ids=docs)

    if mode == "diskann_rerank":
        sidecar_meta = coll._store.build_diskann_sidecars(
            partition_count=config.diskann_partitions,
            probe_count=config.diskann_probes,
            candidate_limit=config.diskann_candidates,
            chunk_size=config.chunk_size,
            kmeans_iterations=config.diskann_kmeans_iterations,
        )
        hnsw_sidecar_meta = None
    elif mode == "hnsw_exact_rerank":
        sidecar_meta = None
        hnsw_sidecar_meta = coll._store.build_hnswlib_sidecar(
            m=config.hnsw_m,
            ef_construction=config.hnsw_ef_construction,
            ef_search=config.hnsw_ef_search,
            candidate_limit=config.hnsw_candidates,
            chunk_size=config.chunk_size,
        )
    else:
        sidecar_meta = None
        hnsw_sidecar_meta = None
        if mode in ("streaming_exact_public", "cascade_auto"):
            coll._store._ensure_streaming_exact_norms()
        if mode == "cascade_auto":
            sidecar_meta = {
                "selected_index": "streaming_exact",
                "selection_reason": "conservative exact-first fallback until autotuned cascade metadata exists",
            }
    build_ms = (time.perf_counter_ns() - build_start) / 1_000_000.0

    reload_start = time.perf_counter_ns()
    reloaded = Collection(
        name=collection,
        db_path=str(db_path),
        embedding_fn=embedding_fn,
        embedding_dim=config.dim,
        experimental_index=experimental_index,
        drosophila_hash=drosophila_hash,
        entropy_tolerance=999.0,
        hnsw_m=config.hnsw_m,
        hnsw_ef_construction=config.hnsw_ef_construction,
        hnsw_ef_search=config.hnsw_ef_search,
        hnsw_candidate_cap=config.hnsw_candidates,
        diskann_graph_degree=config.diskann_graph_degree or 16,
        diskann_probe_count=config.diskann_probes or 8,
        diskann_candidate_cap=config.diskann_candidates or 2000,
        diskann_full_scan_threshold=config.diskann_full_scan_threshold or 4096,
    )
    cold_reload_ms = (time.perf_counter_ns() - reload_start) / 1_000_000.0
    sidecar_audit = collection_sidecar_audit(db_path, collection)
    served_by_faiss = bool(getattr(reloaded._store, "_use_faiss", False))
    served_by_hnswlib = bool(getattr(reloaded._store, "_hnswlib_index", None) is not None)
    accelerator_missing = mode == "hnsw_exact_rerank" and not served_by_hnswlib
    return reloaded, {
        "build_ms": build_ms,
        "cold_reload_ms": cold_reload_ms,
        "storage_bytes": sidecar_audit["total_bytes"],
        "sidecar_audit": sidecar_audit,
        "diskann_sidecar": sidecar_meta,
        "hnswlib_sidecar": hnsw_sidecar_meta,
        "experimental_index": experimental_index,
        "served_by_faiss": served_by_faiss,
        "served_by_hnswlib": served_by_hnswlib,
        "accelerator_missing": accelerator_missing,
        "fallback_reason": (
            "hnswlib_unavailable"
            if accelerator_missing
            else None
        ),
    }


def run_collection_queries(
    coll: Collection,
    config: ScaleHarnessConfig,
    expected: Sequence[Sequence[str]],
) -> dict:
    actual = []
    latencies = []
    query_count = min(config.queries, config.count)
    for idx in range(min(config.warmup_queries, config.count)):
        coll.search(f"query_{idx % query_count}", n_results=config.k)
    for idx in range(query_count):
        query = f"query_{idx}"
        start = time.perf_counter_ns()
        result = coll.search(query, n_results=config.k)
        latencies.append((time.perf_counter_ns() - start) / 1_000_000.0)
        actual.append(result.ids)
    recalls = [recall_at_k(e, a, config.k) for e, a in zip(expected, actual)]
    mrrs = [mrr_at_k(e, a, config.k) for e, a in zip(expected, actual)]
    ndcgs = [ndcg_at_k(e, a, config.k) for e, a in zip(expected, actual)]
    return {
        **summarize_latencies(latencies),
        "latencies_ms": latencies,
        "warmup_queries": config.warmup_queries,
        "recall_at_k": sum(recalls) / float(len(recalls)) if recalls else 0.0,
        "mrr_at_k": sum(mrrs) / float(len(mrrs)) if mrrs else 0.0,
        "ndcg_at_k": sum(ndcgs) / float(len(ndcgs)) if ndcgs else 0.0,
    }


def run_scale_harness(config: ScaleHarnessConfig, workspace: Path | None = None) -> dict:
    config.validate()
    if workspace is None:
        with tempfile.TemporaryDirectory(prefix="latticeshadow-scale-") as tmp:
            return run_scale_harness(config, Path(tmp))
    workspace.mkdir(parents=True, exist_ok=True)
    meta = generate_synthetic_memmaps(config, workspace)
    vectors_path = Path(meta["vectors_path"])
    queries = open_vector_memmap(Path(meta["queries_path"]), meta["queries"], config.dim, mode="r")

    expected, exact_latencies = streaming_exact_topk(
        vectors_path,
        np.asarray(queries, dtype=np.float32),
        config.count,
        config.dim,
        config.k,
        config.chunk_size,
    )

    mode_results = {}
    if "dense_exact_streaming" in config.modes:
        mode_results["dense_exact_streaming"] = {
            "mode": "dense_exact_streaming",
            **summarize_latencies(exact_latencies),
            "latencies_ms": exact_latencies,
            "recall_at_k": 1.0,
            "mrr_at_k": 1.0,
            "ndcg_at_k": 1.0,
            "storage_bytes": vectors_path.stat().st_size + Path(meta["queries_path"]).stat().st_size,
        }

    for mode in config.modes:
        if mode == "dense_exact_streaming":
            continue
        coll, build_metrics = build_collection_mode(mode, config, workspace, vectors_path)
        query_metrics = run_collection_queries(coll, config, expected)
        mode_results[mode] = {
            "mode": mode,
            **build_metrics,
            **query_metrics,
        }

    baseline = mode_results.get("dense_exact_public")
    for item in mode_results.values():
        if baseline and item["mode"] != "dense_exact_public":
            item["p50_speedup_vs_dense_public"] = (
                baseline["p50_ms"] / item["p50_ms"] if item["p50_ms"] > 0 else 0.0
            )
            item["p95_speedup_vs_dense_public"] = (
                baseline["p95_ms"] / item["p95_ms"] if item["p95_ms"] > 0 else 0.0
            )
        else:
            item["p50_speedup_vs_dense_public"] = 1.0
            item["p95_speedup_vs_dense_public"] = 1.0

    best = None
    for item in mode_results.values():
        if item["mode"] in ("dense_exact_public", "dense_exact_streaming"):
            continue
        if best is None or item.get("p95_speedup_vs_dense_public", 0.0) > best.get("p95_speedup_vs_dense_public", 0.0):
            best = item
    gates = {
        "target_count": config.count,
        "target_dim": config.dim,
        "target_queries": config.queries,
        "warmup_queries": config.warmup_queries,
        "recall_gate": config.recall_gate,
        "speedup_gate": config.speedup_gate,
        "latency_gate_ms": config.latency_gate_ms,
        "storage_gate_bytes": config.storage_gate_bytes,
        "achieved": bool(
            best
            and best.get("recall_at_k", 0.0) >= config.recall_gate
            and best.get("p50_speedup_vs_dense_public", 0.0) >= config.speedup_gate
            and best.get("p95_speedup_vs_dense_public", 0.0) >= config.speedup_gate
            and best.get("p95_ms", float("inf")) <= config.latency_gate_ms
            and best.get("storage_bytes", config.storage_gate_bytes + 1) <= config.storage_gate_bytes
        ),
        "best_mode": best["mode"] if best else None,
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "config": asdict(config),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "torch": torch.__version__,
            "torch_num_threads": torch.get_num_threads(),
            "optional_accelerators": optional_accelerator_status(),
            "pid": os.getpid(),
        },
        "synthetic": meta,
        "modes": list(mode_results.values()),
        "gates": gates,
    }
    result_path(workspace).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--warmup-queries", type=int, default=0)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--chunk-size", type=int, default=4096)
    parser.add_argument("--insert-chunk-size", type=int, default=2048)
    parser.add_argument("--mode", dest="modes", choices=AVAILABLE_MODES, action="append")
    parser.add_argument("--hnsw-m", type=int, default=32)
    parser.add_argument("--hnsw-ef-construction", type=int, default=200)
    parser.add_argument("--hnsw-ef-search", type=int, default=128)
    parser.add_argument("--hnsw-candidates", type=int, default=1000)
    parser.add_argument("--diskann-partitions", type=int, default=None)
    parser.add_argument("--diskann-probes", type=int, default=None)
    parser.add_argument("--diskann-candidates", type=int, default=None)
    parser.add_argument("--diskann-graph-degree", type=int, default=None)
    parser.add_argument("--diskann-full-scan-threshold", type=int, default=None)
    parser.add_argument("--diskann-kmeans-iterations", type=int, default=1)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--torch-threads", type=int, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.torch_threads is not None:
        torch.set_num_threads(args.torch_threads)
    config = ScaleHarnessConfig(
        count=args.count,
        dim=args.dim,
        queries=args.queries,
        warmup_queries=args.warmup_queries,
        k=args.k,
        seed=args.seed,
        modes=tuple(args.modes or ("dense_exact_public", "dense_exact_streaming", "diskann_rerank")),
        chunk_size=args.chunk_size,
        insert_chunk_size=args.insert_chunk_size,
        hnsw_m=args.hnsw_m,
        hnsw_ef_construction=args.hnsw_ef_construction,
        hnsw_ef_search=args.hnsw_ef_search,
        hnsw_candidates=args.hnsw_candidates,
        diskann_partitions=args.diskann_partitions,
        diskann_probes=args.diskann_probes,
        diskann_candidates=args.diskann_candidates,
        diskann_graph_degree=args.diskann_graph_degree,
        diskann_full_scan_threshold=args.diskann_full_scan_threshold,
        diskann_kmeans_iterations=args.diskann_kmeans_iterations,
    )
    workspace = args.workspace or Path("benchmark_results") / f"scale_{config.count}_{config.dim}"
    result = run_scale_harness(config, workspace=workspace)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

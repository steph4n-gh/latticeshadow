"""Deterministic benchmark and privacy leakage harness for LatticeShadow.

The harness is intentionally side-effect-light: it creates temporary databases,
uses existing public storage APIs, and emits JSON-serializable metrics. It does
not change production search behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import torch

from .adapter import CayleyPrivacyAdapter
from .latticedb.collection import Collection
from .latticedb.distiller import AutoDistiller


DEFAULT_MODES = (
    "dense_exact",
    "privacy_dense",
    "cli_privacy_drosophila_hybrid",
    "lattice",
)
AVAILABLE_MODES = DEFAULT_MODES + (
    "hnsw_rerank",
    "hnsw_exact_rerank",
    "flyhash_rerank",
    "flyhash_exact_rerank",
    "pq_rerank",
    "pq_exact_rerank",
    "flyhash_pq_rerank",
    "diskann_rerank",
    "streaming_exact_public",
    "cascade_auto",
)
MODE_ALIASES = {
    "dense": "dense_exact",
    "drosophila_hash": "cli_privacy_drosophila_hybrid",
}
EXPERIMENTAL_MODE_TO_INDEX = {
    "hnsw_rerank": "hnsw_rerank",
    "hnsw_exact_rerank": "hnsw_exact_rerank",
    "flyhash_rerank": "flyhash_rerank",
    "flyhash_exact_rerank": "flyhash_exact_rerank",
    "pq_rerank": "pq_rerank",
    "pq_exact_rerank": "pq_exact_rerank",
    "flyhash_pq_rerank": "flyhash_pq_rerank",
    "diskann_rerank": "diskann_rerank",
    "streaming_exact_public": "streaming_exact",
    "cascade_auto": "cascade_auto",
}
DROSOPHILA_MODES = {
    "cli_privacy_drosophila_hybrid",
    "flyhash_rerank",
    "flyhash_exact_rerank",
    "flyhash_pq_rerank",
}
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class HarnessConfig:
    """Configuration for one deterministic harness run."""

    count: int = 1000
    dim: int = 128
    queries: int = 10
    k: int = 10
    seed: int = 42
    modes: tuple[str, ...] = DEFAULT_MODES
    privacy_known_pairs: int | None = None
    include_distillation_replay: bool = False

    def validate(self) -> None:
        if self.count <= 1:
            raise ValueError("count must be greater than 1")
        if self.dim <= 0:
            raise ValueError("dim must be positive")
        if self.dim < 2:
            raise ValueError("dim must be at least 2 for Cayley leakage metrics")
        if self.queries <= 0:
            raise ValueError("queries must be positive")
        if self.k <= 0:
            raise ValueError("k must be positive")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        unknown = sorted(set(self.modes) - set(AVAILABLE_MODES) - set(MODE_ALIASES))
        if unknown:
            raise ValueError(f"unknown benchmark modes: {unknown}")


def percentile(values: Sequence[float], pct: float) -> float:
    """Return a simple linear percentile for small benchmark samples."""
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


def normalize_rows(vectors: torch.Tensor) -> torch.Tensor:
    norms = torch.linalg.norm(vectors, dim=1, keepdim=True)
    norms = torch.where(norms == 0, torch.ones_like(norms), norms)
    return vectors / norms


def generate_vectors(count: int, dim: int, seed: int, normalize: bool = True) -> torch.Tensor:
    generator = torch.Generator(device="cpu")
    generator.manual_seed(seed)
    vectors = torch.randn(count, dim, generator=generator, dtype=torch.float32)
    return normalize_rows(vectors) if normalize else vectors


def topk_ids(vectors: torch.Tensor, query: torch.Tensor, k: int) -> list[str]:
    matrix = normalize_rows(vectors.float())
    q = query.float().view(1, -1)
    q = normalize_rows(q).view(-1)
    scores = torch.mv(matrix, q)
    top = torch.topk(scores, k=min(k, vectors.shape[0]), largest=True).indices.tolist()
    return [f"doc_{idx}" for idx in top]


def recall_at_k(expected_ids: Sequence[str], actual_ids: Sequence[str], k: int) -> float:
    expected = set(expected_ids[:k])
    if not expected:
        return 0.0
    actual = set(actual_ids[:k])
    return len(expected & actual) / float(len(expected))


def mean_recall_at_k(expected: Sequence[Sequence[str]], actual: Sequence[Sequence[str]], k: int) -> float:
    recalls = [recall_at_k(e, a, k) for e, a in zip(expected, actual)]
    return sum(recalls) / float(len(recalls)) if recalls else 0.0


def knn_indices(vectors: torch.Tensor, k: int) -> list[list[int]]:
    matrix = normalize_rows(vectors.float())
    scores = matrix @ matrix.T
    scores.fill_diagonal_(-float("inf"))
    k_eff = min(k, max(1, vectors.shape[0] - 1))
    return torch.topk(scores, k=k_eff, dim=1, largest=True).indices.tolist()


def mean_jaccard(left: Sequence[Sequence[int]], right: Sequence[Sequence[int]]) -> float:
    scores = []
    for a, b in zip(left, right):
        a_set = set(a)
        b_set = set(b)
        union = a_set | b_set
        scores.append((len(a_set & b_set) / float(len(union))) if union else 1.0)
    return sum(scores) / float(len(scores)) if scores else 0.0


def gram_relative_error(left: torch.Tensor, right: torch.Tensor) -> float:
    left_gram = left.float() @ left.float().T
    right_gram = right.float() @ right.float().T
    denom = torch.linalg.norm(left_gram).item()
    if denom == 0:
        return torch.linalg.norm(right_gram).item()
    return (torch.linalg.norm(left_gram - right_gram) / denom).item()


def pairwise_distance_relative_error(left: torch.Tensor, right: torch.Tensor) -> float:
    left_dist = torch.cdist(left.float(), left.float())
    right_dist = torch.cdist(right.float(), right.float())
    denom = torch.linalg.norm(left_dist).item()
    if denom == 0:
        return torch.linalg.norm(right_dist).item()
    return (torch.linalg.norm(left_dist - right_dist) / denom).item()


def pairwise_sketch_distortion_stats(left: torch.Tensor, sketch: torch.Tensor) -> dict:
    left_dist = torch.pdist(left.float())
    sketch_dist = torch.pdist(sketch.float())
    if left_dist.numel() == 0:
        return {"mean": 0.0, "p95": 0.0}
    denom = torch.dot(sketch_dist, sketch_dist).item()
    scale = 1.0 if denom == 0 else (torch.dot(left_dist, sketch_dist) / denom).item()
    rel = torch.abs((sketch_dist * scale) - left_dist) / torch.clamp(left_dist.abs(), min=1e-8)
    values = rel.tolist()
    return {
        "mean": sum(values) / float(len(values)) if values else 0.0,
        "p95": percentile(values, 0.95),
    }


def known_plaintext_recovery_error(raw: torch.Tensor, transformed: torch.Tensor, known_pairs: int) -> float:
    known_pairs = max(1, min(known_pairs, raw.shape[0] - 1))
    source_known = transformed[:known_pairs].float()
    target_known = raw[:known_pairs].float()
    source_holdout = transformed[known_pairs:].float()
    target_holdout = raw[known_pairs:].float()
    if source_holdout.numel() == 0:
        return 0.0
    solution = torch.linalg.lstsq(source_known, target_known).solution
    recovered = source_holdout @ solution
    denom = torch.linalg.norm(target_holdout).item()
    if denom == 0:
        return torch.linalg.norm(recovered).item()
    return (torch.linalg.norm(recovered - target_holdout) / denom).item()


def binary_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Compute rank-based AUC without sklearn."""
    pairs = sorted((float(score), int(label)) for score, label in zip(scores, labels))
    positives = sum(label for _, label in pairs)
    negatives = len(pairs) - positives
    if positives == 0 or negatives == 0:
        return 0.5

    rank_sum = 0.0
    rank = 1
    idx = 0
    while idx < len(pairs):
        end = idx + 1
        while end < len(pairs) and pairs[end][0] == pairs[idx][0]:
            end += 1
        avg_rank = (rank + (rank + end - idx - 1)) / 2.0
        rank_sum += avg_rank * sum(label for _, label in pairs[idx:end])
        rank += end - idx
        idx = end

    return (rank_sum - positives * (positives + 1) / 2.0) / float(positives * negatives)


def collection_sidecar_audit(db_path: Path, collection: str, privacy_enabled: bool = False) -> dict:
    candidates = [
        ("sqlite", db_path, False),
        ("vector_memmap", Path(f"{db_path}_{collection}_vectors.bin"), True),
        ("flyhash_rerank_fp16", Path(f"{db_path}_{collection}_rerank_vectors.bin"), True),
        ("pq_codes_int8", Path(f"{db_path}_{collection}_pq_codes.bin"), True),
        ("hnswlib_index", Path(f"{db_path}_{collection}_hnswlib.bin"), True),
        ("hnswlib_metadata", Path(f"{db_path}_{collection}_hnswlib_meta.json"), True),
        ("diskann_vectors_fp16", Path(f"{db_path}_{collection}_diskann_vectors.bin"), True),
        ("diskann_graph_uint32", Path(f"{db_path}_{collection}_diskann_graph.bin"), True),
        ("streaming_norms_f32", Path(f"{db_path}_{collection}_streaming_norms.bin"), True),
        ("sqlite_wal", Path(f"{db_path}-wal"), False),
        ("sqlite_shm", Path(f"{db_path}-shm"), False),
    ]
    sidecars = []
    total_bytes = 0
    plaintext_bytes = 0
    for label, path, plaintext in candidates:
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


def storage_bytes_for(db_path: Path, collection: str) -> int:
    return int(collection_sidecar_audit(db_path, collection)["total_bytes"])


def canonical_mode(mode: str) -> str:
    return MODE_ALIASES.get(mode, mode)


def stable_seed_offset(text: str) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 10_000


def cayley_rank(dim: int) -> int:
    return min(16, max(1, dim // 2))


def build_embedding_fn(vectors: torch.Tensor):
    """Return deterministic text->tensor mapping for corpus and query strings."""

    def embed(text: str) -> torch.Tensor:
        try:
            idx = int(str(text).rsplit("_", 1)[1])
        except (IndexError, ValueError):
            idx = 0
        idx = max(0, min(idx, vectors.shape[0] - 1))
        return vectors[idx].detach().clone()

    return embed


def environment_metadata() -> dict:
    try:
        from .latticedb import store as store_module

        faiss_available = bool(store_module._FAISS_AVAILABLE)
        hnswlib_available = bool(store_module._HNSWLIB_AVAILABLE)
    except Exception:
        faiss_available = False
        hnswlib_available = False
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "torch": torch.__version__,
        "faiss_available": faiss_available,
        "optional_accelerators": {
            "faiss": faiss_available,
            "hnswlib": hnswlib_available,
            "hnswlib_importable": importlib.util.find_spec("hnswlib") is not None,
        },
        "pid": os.getpid(),
    }


def run_collection_mode(
    mode: str,
    vectors: torch.Tensor,
    config: HarnessConfig,
    workspace: Path,
) -> dict:
    mode = canonical_mode(mode)
    if mode == "lattice" and config.dim % 32 != 0:
        return {
            "mode": mode,
            "skipped": True,
            "reason": "lattice mode requires dim divisible by 32",
        }

    collection = f"bench_{mode}"
    db_path = workspace / f"{collection}.sqlite"
    ids = [f"doc_{idx}" for idx in range(config.count)]
    docs = [f"doc_{idx}" for idx in range(config.count)]
    embedding_fn = build_embedding_fn(vectors)

    torch.manual_seed(config.seed + stable_seed_offset(mode))
    privacy_enabled = mode in ("privacy_dense", "cli_privacy_drosophila_hybrid")
    drosophila_enabled = mode in DROSOPHILA_MODES
    experimental_index = EXPERIMENTAL_MODE_TO_INDEX.get(mode)
    collection_obj = Collection(
        name=collection,
        db_path=str(db_path),
        embedding_fn=embedding_fn,
        embedding_dim=config.dim,
        privacy_enabled=privacy_enabled,
        lattice_index=mode == "lattice",
        master_key="latticeshadow-benchmark-master-key",
        drosophila_hash=drosophila_enabled,
        experimental_index=experimental_index,
    )
    start = time.perf_counter_ns()
    collection_obj.add(documents=docs, ids=ids)
    insert_ms = (time.perf_counter_ns() - start) / 1_000_000.0

    start = time.perf_counter_ns()
    torch.manual_seed(config.seed + stable_seed_offset(mode))
    reloaded = Collection(
        name=collection,
        db_path=str(db_path),
        embedding_fn=embedding_fn,
        embedding_dim=config.dim,
        privacy_enabled=privacy_enabled,
        lattice_index=mode == "lattice",
        master_key="latticeshadow-benchmark-master-key",
        drosophila_hash=drosophila_enabled,
        experimental_index=experimental_index,
    )
    cold_reload_ms = (time.perf_counter_ns() - start) / 1_000_000.0
    search_collection = collection_obj if mode == "lattice" else reloaded

    query_count = min(config.queries, config.count)
    expected = [topk_ids(vectors, vectors[idx], config.k) for idx in range(query_count)]
    actual = []
    search_times = []
    result_sizes = []
    served_locally = []
    for idx in range(query_count):
        query = f"query_{idx}"
        start = time.perf_counter_ns()
        result = search_collection.search(
            query,
            n_results=config.k,
            hybrid=mode == "cli_privacy_drosophila_hybrid",
        )
        search_times.append((time.perf_counter_ns() - start) / 1_000_000.0)
        actual.append(result.ids)
        result_sizes.append(len(result.ids))
        served_locally.append(bool(result.served_locally))

    sidecar_audit = collection_sidecar_audit(db_path, collection, privacy_enabled=privacy_enabled)
    served_by_faiss = bool(getattr(reloaded._store, "_use_faiss", False))
    served_by_hnswlib = bool(getattr(reloaded._store, "_hnswlib_index", None) is not None)
    accelerator_missing = (
        mode == "hnsw_exact_rerank"
        and not served_by_hnswlib
    ) or (
        mode == "hnsw_rerank"
        and not served_by_faiss
    )
    return {
        "mode": mode,
        "count": config.count,
        "dim": config.dim,
        "k": config.k,
        "insert_total_ms": insert_ms,
        "insert_ms_per_doc": insert_ms / float(config.count),
        "cold_reload_ms": cold_reload_ms,
        "search_p50_ms": percentile(search_times, 0.50),
        "search_p95_ms": percentile(search_times, 0.95),
        "search_p99_ms": percentile(search_times, 0.99),
        "search_max_ms": max(search_times) if search_times else 0.0,
        "recall_at_k": mean_recall_at_k(expected, actual, config.k),
        "min_result_size": min(result_sizes) if result_sizes else 0,
        "storage_bytes": sidecar_audit["total_bytes"],
        "sidecar_audit": sidecar_audit,
        "memmap_capacity": int(getattr(collection_obj._store, "_memmap_capacity", 0) or 0),
        "cold_memmap_capacity": int(getattr(reloaded._store, "_memmap_capacity", 0) or 0),
        "memmap_dim": int(getattr(reloaded._store, "_memmap_dim", 0) or 0),
        "served_locally_rate": sum(served_locally) / float(len(served_locally)) if served_locally else 0.0,
        "served_by_faiss": served_by_faiss,
        "served_by_hnswlib": served_by_hnswlib,
        "accelerator_missing": accelerator_missing,
        "fallback_reason": (
            "hnswlib_unavailable" if mode == "hnsw_exact_rerank" else "faiss_unavailable"
            if accelerator_missing
            else None
        ),
        "experimental_index": getattr(reloaded._store, "experimental_index", None),
        "hnsw_rerank_enabled": bool(getattr(reloaded._store, "_hnsw_rerank_enabled", False)),
        "hnsw_rerank_candidate_count": (
            reloaded._store._hnsw_rerank_candidate_count(config.k)
            if getattr(reloaded._store, "_hnsw_rerank_enabled", False)
            else 0
        ),
        "flyhash_rerank_enabled": bool(getattr(reloaded._store, "_flyhash_rerank_enabled", False)),
        "flyhash_rerank_candidate_count": (
            reloaded._store._flyhash_rerank_candidate_count(config.k, config.count)
            if getattr(reloaded._store, "_flyhash_rerank_enabled", False)
            else 0
        ),
        "pq_rerank_enabled": bool(getattr(reloaded._store, "_pq_rerank_enabled", False)),
        "pq_rerank_candidate_count": (
            reloaded._store._pq_rerank_candidate_count(config.k, config.count)
            if getattr(reloaded._store, "_pq_rerank_enabled", False)
            else 0
        ),
        "cascade_auto_enabled": bool(getattr(reloaded._store, "_cascade_auto_enabled", False)),
        "pq_code_storage_bytes": (
            Path(f"{db_path}_{collection}_pq_codes.bin").stat().st_size
            if Path(f"{db_path}_{collection}_pq_codes.bin").exists()
            else 0
        ),
        "diskann_rerank_enabled": bool(getattr(reloaded._store, "_diskann_rerank_enabled", False)),
        "diskann_rerank_candidate_count": (
            reloaded._store._diskann_rerank_candidate_count(config.k, config.count)
            if getattr(reloaded._store, "_diskann_rerank_enabled", False)
            else 0
        ),
        "diskann_vector_storage_bytes": (
            Path(f"{db_path}_{collection}_diskann_vectors.bin").stat().st_size
            if Path(f"{db_path}_{collection}_diskann_vectors.bin").exists()
            else 0
        ),
        "diskann_graph_storage_bytes": (
            Path(f"{db_path}_{collection}_diskann_graph.bin").stat().st_size
            if Path(f"{db_path}_{collection}_diskann_graph.bin").exists()
            else 0
        ),
        "faiss_index_class": (
            reloaded._store._faiss_index.__class__.__name__
            if getattr(reloaded._store, "_faiss_index", None) is not None
            else None
        ),
        "lattice_index_count": collection_obj._indexer.count() if collection_obj._indexer else 0,
        "cold_lattice_index_count": reloaded._indexer.count() if reloaded._indexer else 0,
        "lattice_code_storage_bytes": (
            collection_obj._indexer.code_storage_bytes() if collection_obj._indexer else 0
        ),
        "lattice_code_dtype": (
            str(collection_obj._indexer.code_dtype) if collection_obj._indexer and collection_obj._indexer.code_dtype else None
        ),
    }


def run_privacy_leakage_metrics(config: HarnessConfig) -> dict:
    vectors = generate_vectors(config.count, config.dim, config.seed + 10, normalize=True)
    torch.manual_seed(config.seed + 30)
    adapter = CayleyPrivacyAdapter(dim=config.dim, rank=cayley_rank(config.dim))
    rotated = adapter.rotate(vectors)
    known_pairs = config.privacy_known_pairs or min(max(config.dim, 4), max(1, config.count // 2))
    projected = adapter.decompress(adapter.compress(rotated))
    srht_sketch = adapter.srht_sketch(
        rotated,
        sketch_dim=min(2 * adapter.rank, adapter._next_power_of_two(config.dim)),
        seed=config.seed + 40,
    )
    srht_distortion = pairwise_sketch_distortion_stats(rotated, srht_sketch)
    rotated_norm = torch.linalg.norm(rotated).item()
    projection_energy = 0.0 if rotated_norm == 0 else (
        torch.linalg.norm(projected).pow(2) / torch.linalg.norm(rotated).pow(2)
    ).item()
    projection_error = 0.0 if rotated_norm == 0 else (
        torch.linalg.norm(rotated - projected) / torch.linalg.norm(rotated)
    ).item()

    canary = generate_vectors(config.count, config.dim, config.seed + 20, normalize=False)
    labels = torch.zeros(config.count, dtype=torch.int64)
    labels[config.count // 2 :] = 1
    canary[labels == 1] *= 1.25
    canary_rotated = adapter.rotate(canary)

    k_eff = min(config.k, max(1, config.count - 1))
    raw_knn = knn_indices(vectors, k_eff)
    rotated_knn = knn_indices(rotated, k_eff)
    return {
        "mode": "cayley_rotation",
        "count": config.count,
        "dim": config.dim,
        "rank": adapter.rank,
        "known_pairs": known_pairs,
        "gram_relative_error": gram_relative_error(vectors, rotated),
        "pairwise_distance_relative_error": pairwise_distance_relative_error(vectors, rotated),
        "knn_jaccard_at_k": mean_jaccard(raw_knn, rotated_knn),
        "known_plaintext_recovery_relative_error": known_plaintext_recovery_error(
            vectors, rotated, known_pairs
        ),
        "compression_projection_retained_energy": projection_energy,
        "compression_reconstruction_relative_error": projection_error,
        "srht_sketch_dim": srht_sketch.shape[-1],
        "srht_pairwise_distortion_mean": srht_distortion["mean"],
        "srht_pairwise_distortion_p95": srht_distortion["p95"],
        "norm_side_channel_auc": binary_auc(
            canary_rotated.norm(dim=1).tolist(), labels.tolist()
        ),
        "notes": [
            "Cayley rotation is expected to preserve Gram matrices and kNN neighborhoods.",
            "Compression metrics describe low-rank sketch projection exposure, not reversible hidden-state recovery.",
            "SRHT metrics describe distance-sketch distortion, not a reconstructive codec.",
            "norm_side_channel_auc is a synthetic canary metric; it measures norm leakage, not text inversion.",
        ],
    }


def _distillation_replay_case(
    config: HarnessConfig,
    workspace: Path,
    case: str,
    drift_tail: bool,
) -> dict:
    replay_count = min(max(config.count, 24), 256)
    holdout_fraction = 0.25
    holdout_count = max(1, int(round(replay_count * holdout_fraction)))
    train_count = replay_count - holdout_count
    db_path = workspace / f"distillation_replay_{case}.sqlite"
    if db_path.exists():
        db_path.unlink()

    generator = torch.Generator(device="cpu")
    generator.manual_seed(config.seed + stable_seed_offset(case))
    local_vectors = torch.randn(replay_count, config.dim, generator=generator, dtype=torch.float32)
    cloud_vectors = torch.zeros_like(local_vectors)
    if drift_tail:
        cloud_vectors[train_count:] = 5.0

    torch.manual_seed(config.seed + stable_seed_offset(case))
    distiller = AutoDistiller(
        dim=config.dim,
        hidden_dim=min(64, max(8, config.dim * 2)),
        min_samples=replay_count,
        loss_threshold=0.5,
        holdout_loss_threshold=0.5,
        holdout_fraction=holdout_fraction,
        db_path=str(db_path),
        collection=case,
        max_training_pairs=replay_count,
    )
    for local_vec, cloud_vec in zip(local_vectors, cloud_vectors):
        distiller.record_pair(local_vec, cloud_vec)

    start = time.perf_counter_ns()
    train_loss = distiller.train(epochs=30)
    train_ms = (time.perf_counter_ns() - start) / 1_000_000.0

    with torch.no_grad():
        predictions = torch.stack([
            distiller._head.predict(vec) for vec in local_vectors[train_count:]
        ])
        replay_mse = torch.mean((predictions - cloud_vectors[train_count:]) ** 2).item()

    metrics = distiller.graduation_metrics
    return {
        "case": case,
        "drift_tail": drift_tail,
        "count": replay_count,
        "dim": config.dim,
        "train_ms": train_ms,
        "train_loss": train_loss,
        "holdout_loss": metrics["holdout_loss"],
        "holdout_replay_mse": replay_mse,
        "is_graduated": metrics["is_graduated"],
        "cloud_skip_allowed_rate": 1.0 if metrics["is_graduated"] else 0.0,
        "graduation_block_reason": metrics["graduation_block_reason"],
        "train_sample_count": metrics["train_sample_count"],
        "holdout_sample_count": metrics["holdout_sample_count"],
        "pass_fail": (
            "pass" if (not drift_tail and metrics["is_graduated"])
            or (drift_tail and not metrics["is_graduated"])
            else "fail"
        ),
    }


def run_distillation_replay_metrics(config: HarnessConfig, workspace: Path | None = None) -> dict:
    """Run stable and drifted-tail distiller replays for cloud-skip safety."""
    config.validate()
    if workspace is None:
        with tempfile.TemporaryDirectory(prefix="latticeshadow-distill-replay-") as tmp:
            return run_distillation_replay_metrics(config, Path(tmp))

    workspace.mkdir(parents=True, exist_ok=True)
    stable = _distillation_replay_case(config, workspace, "stable", drift_tail=False)
    drifted_tail = _distillation_replay_case(config, workspace, "drifted_tail", drift_tail=True)
    return {
        "mode": "distillation_replay",
        "stable": stable,
        "drifted_tail": drifted_tail,
        "notes": [
            "Stable replay should graduate and allow local cloud-skip serving.",
            "Drifted-tail replay should fail holdout validation and block local serving.",
        ],
    }


def run_harness(config: HarnessConfig, workspace: Path | None = None) -> dict:
    config.validate()
    vectors = generate_vectors(config.count, config.dim, config.seed, normalize=True)
    if workspace is None:
        with tempfile.TemporaryDirectory(prefix="latticeshadow-bench-") as tmp:
            return run_harness(config, Path(tmp))

    workspace.mkdir(parents=True, exist_ok=True)
    vector_results = [
        run_collection_mode(mode, vectors, config, workspace)
        for mode in config.modes
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "config": asdict(config),
        "environment": environment_metadata(),
        "vector_store": vector_results,
        "privacy_leakage": run_privacy_leakage_metrics(config),
    }
    if config.include_distillation_replay:
        result["distillation_replay"] = run_distillation_replay_metrics(config, workspace)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", "--n", dest="count", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--mode",
        dest="modes",
        choices=("all",) + AVAILABLE_MODES + tuple(MODE_ALIASES),
        action="append",
        help="Benchmark mode. May be passed multiple times. Defaults to all modes.",
    )
    parser.add_argument("--privacy-known-pairs", type=int, default=None)
    parser.add_argument(
        "--include-distillation-replay",
        action="store_true",
        help="Include opt-in holdout-gated AutoDistiller replay metrics.",
    )
    parser.add_argument("--workspace", "--db-dir", dest="workspace", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--torch-threads", type=int, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.torch_threads is not None:
        torch.set_num_threads(args.torch_threads)
    selected_modes = tuple(args.modes or DEFAULT_MODES)
    if "all" in selected_modes:
        selected_modes = DEFAULT_MODES
    config = HarnessConfig(
        count=args.count,
        dim=args.dim,
        queries=args.queries,
        k=args.k,
        seed=args.seed,
        modes=selected_modes,
        privacy_known_pairs=args.privacy_known_pairs,
        include_distillation_replay=args.include_distillation_replay,
    )
    result = run_harness(config, workspace=args.workspace)
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

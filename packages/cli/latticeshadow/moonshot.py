"""Moonshot reporting and benchmark helpers for LatticeShadow CLI."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from latticeshadow import config
from latticeshadow.audit_log import SignedAuditLog
from latticeshadow.consent import consent_status
from latticeshadow.repair_queue import list_repairs
from latticeshadow.sensitivity import classify
from latticeshadow.trust import trust_status
from latticeshadow.vaults import (
    HOT_COLLECTION,
    MAIN_COLLECTION,
    chmod_collection_files,
    vault_file_paths,
)


def _file_record(path: str) -> dict[str, Any]:
    exists = os.path.exists(path)
    record: dict[str, Any] = {
        "path": path,
        "exists": exists,
    }
    if exists:
        st = os.stat(path)
        record.update(
            {
                "mode": oct(stat.S_IMODE(st.st_mode)),
                "owner_only": (stat.S_IMODE(st.st_mode) & 0o077) == 0,
                "bytes": int(st.st_size),
            }
        )
    return record


def _collection_exists(db_path: str, collection: str) -> bool:
    if not os.path.exists(db_path):
        return False
    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            row = conn.execute(
                "SELECT 1 FROM vectors WHERE collection = ? LIMIT 1",
                (collection,),
            ).fetchone()
            return bool(row)
    except sqlite3.Error:
        return False


def _plaintext_scan(db_path: str, collection: str, sample_limit: int = 50) -> dict[str, Any]:
    if not os.path.exists(db_path):
        return {"checked": 0, "sensitive_hits": 0, "status": "missing_db"}
    checked = 0
    sensitive_hits: list[str] = []
    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            cursor = conn.execute(
                "SELECT doc_id, document FROM vectors WHERE collection = ? LIMIT ?",
                (collection, sample_limit),
            )
            for doc_id, document in cursor.fetchall():
                checked += 1
                text = str(document or "")
                if text and not text.startswith("enc:") and classify(text) == "sensitive":
                    sensitive_hits.append(str(doc_id))
    except sqlite3.Error as exc:
        return {"checked": checked, "sensitive_hits": 0, "status": f"error: {exc}"}
    return {
        "checked": checked,
        "sensitive_hits": len(sensitive_hits),
        "hit_ids": sensitive_hits[:10],
        "status": "ok",
    }


def _sidecar_plaintext_scan(files: list[dict[str, Any]]) -> dict[str, Any]:
    canaries = [
        b"sk-proj-",
        b"AKIA",
        b"-----BEGIN",
        b"password=",
        b"api_key=",
    ]
    checked = 0
    hits: list[str] = []
    for record in files:
        path = record.get("path")
        if not path or not os.path.exists(path):
            continue
        if path.endswith((".sqlite", ".db", "-wal", "-shm")):
            continue
        checked += 1
        try:
            with open(path, "rb") as handle:
                blob = handle.read(256_000)
            if any(canary in blob for canary in canaries):
                hits.append(str(path))
        except OSError:
            continue
    return {
        "checked": checked,
        "plaintext_canary_hits": len(hits),
        "hit_paths": hits[:10],
        "status": "ok" if not hits else "warn",
    }


def _privacy_leakage_harness(db_path: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    sidecar_scan = _sidecar_plaintext_scan(files)
    return {
        "schema": "latticeshadow.privacy_leakage_harness.v1",
        "sidecar_plaintext_scan": sidecar_scan,
        "embedding_inversion_canaries": {
            "status": "not_run",
            "reason": "Requires adversarial reconstruction corpus; no zero-knowledge claim made.",
        },
        "known_plaintext_procrustes": {
            "status": "not_run",
            "reason": "Requires paired plaintext/vector probes; report blocks marketing claims until implemented.",
        },
        "knn_overlap": {
            "status": "not_run",
            "reason": "Use shadow bench moonshot with a labeled corpus for recall/leakage overlap gates.",
        },
    }


def _vectors_for_collection(vault: Any, collection: str) -> dict[str, np.ndarray]:
    vectors = {}
    with vault._store._connect() as conn:
        cursor = conn.execute(
            "SELECT doc_id, vector_blob FROM vectors WHERE collection = ? ORDER BY doc_id",
            (collection,),
        )
        for doc_id, blob in cursor.fetchall():
            vector = vault._store._blob_to_vector(blob)
            vectors[str(doc_id)] = vector.detach().cpu().float().numpy().reshape(-1)
    return vectors


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / denom)


def _procrustes_probe(plain_vectors: dict[str, np.ndarray], private_vectors: dict[str, np.ndarray]) -> dict[str, Any]:
    shared_ids = sorted(set(plain_vectors) & set(private_vectors))
    if len(shared_ids) < 4:
        return {"status": "skipped", "reason": "not enough paired vectors", "pairs": len(shared_ids)}

    split = max(2, len(shared_ids) // 2)
    train_ids = shared_ids[:split]
    test_ids = shared_ids[split:]

    x_train = np.stack([plain_vectors[doc_id] for doc_id in train_ids])
    y_train = np.stack([private_vectors[doc_id] for doc_id in train_ids])
    x_test = np.stack([plain_vectors[doc_id] for doc_id in test_ids])
    y_test = np.stack([private_vectors[doc_id] for doc_id in test_ids])

    x_mean = x_train.mean(axis=0, keepdims=True)
    y_mean = y_train.mean(axis=0, keepdims=True)
    matrix = (x_train - x_mean).T @ (y_train - y_mean)
    u, _, vt = np.linalg.svd(matrix, full_matrices=False)
    rotation = u @ vt
    predicted = (x_test - x_mean) @ rotation + y_mean
    cosines = [_cosine(predicted[i], y_test[i]) for i in range(len(test_ids))]
    mean_cosine = float(np.mean(cosines)) if cosines else 0.0
    return {
        "status": "ok",
        "pairs": len(shared_ids),
        "train_pairs": len(train_ids),
        "test_pairs": len(test_ids),
        "mean_cosine": mean_cosine,
        "leakage_flag": mean_cosine > 0.90,
        "interpretation": "High cosine means known plaintext pairs can recover the private vector transform.",
    }


def run_privacy_leakage_harness(
    count: int = 32,
    dim: int = 128,
    queries: int = 8,
    master_key: str = "moonshot-privacy",
) -> dict[str, Any]:
    count = max(8, int(count))
    dim = max(8, int(dim))
    queries = max(1, min(int(queries), count))

    with tempfile.TemporaryDirectory(prefix="latticeshadow_privacy_") as tmp:
        from latticeshadow_db.latticedb import connect

        db_path = os.path.join(tmp, "privacy.sqlite")
        documents = [
            f"privacy canary event {i:06d} project_{i % 5} token_marker_{i % 7}"
            for i in range(count)
        ]
        sensitive_docs = [
            "api_key=example_value",
            "password=privacy-harness-secret",
            "-----BEGIN PRIVATE KEY-----\nprivacy-harness\n-----END PRIVATE KEY-----",
        ]
        documents.extend(sensitive_docs)
        ids = [f"privacy_{i:06d}" for i in range(len(documents))]

        plain = connect(
            db_path=db_path,
            collection="privacy_plain",
            embedding_dim=dim,
            privacy=False,
            drosophila_hash=False,
            device="cpu",
        )
        plain.add(documents=documents, ids=ids, metadatas=[{"source": "privacy_harness"} for _ in documents])

        private_hot = connect(
            db_path=db_path,
            collection="privacy_private_hot",
            embedding_dim=dim,
            privacy=True,
            drosophila_hash=False,
            experimental_index="streaming_exact",
            master_key=master_key,
            device="cpu",
        )
        private_hot.add(documents=documents, ids=ids, metadatas=[{"source": "privacy_harness"} for _ in documents])
        chmod_collection_files(private_hot)

        files = [
            _file_record(path)
            for collection in ("privacy_plain", "privacy_private_hot")
            for path in vault_file_paths(db_path, collection=collection)
            if os.path.exists(path)
        ]
        sidecar_scan = _sidecar_plaintext_scan(files)
        plaintext_scan = {
            "privacy_plain": _plaintext_scan(db_path, "privacy_plain", sample_limit=len(documents)),
            "privacy_private_hot": _plaintext_scan(db_path, "privacy_private_hot", sample_limit=len(documents)),
        }

        query_indexes = [(i * max(1, count // queries)) % count for i in range(queries)]
        overlap_scores = []
        recall_hits = 0
        for idx in query_indexes:
            query = documents[idx]
            target_id = ids[idx]
            plain_ids = set(plain.search(query, n_results=10).ids)
            private_ids = set(private_hot.search(query, n_results=10).ids)
            if target_id in private_ids:
                recall_hits += 1
            union = plain_ids | private_ids
            overlap_scores.append((len(plain_ids & private_ids) / len(union)) if union else 0.0)

        plain_vectors = _vectors_for_collection(plain, "privacy_plain")
        private_vectors = _vectors_for_collection(private_hot, "privacy_private_hot")
        procrustes = _procrustes_probe(plain_vectors, private_vectors)

        issues = []
        if sidecar_scan.get("plaintext_canary_hits"):
            issues.append("sidecar_plaintext_canary")
        if plaintext_scan["privacy_private_hot"].get("sensitive_hits"):
            issues.append("private_sensitive_plaintext")
        if procrustes.get("leakage_flag"):
            issues.append("known_plaintext_procrustes_recovery")

        return {
            "schema": "latticeshadow.privacy_leakage_harness.v1",
            "count": len(documents),
            "dim": dim,
            "queries": queries,
            "sidecar_plaintext_scan": sidecar_scan,
            "plaintext_scan": plaintext_scan,
            "knn_overlap": {
                "status": "ok",
                "mean_jaccard_at_10": float(np.mean(overlap_scores)) if overlap_scores else 0.0,
                "private_recall_at_10": recall_hits / len(query_indexes),
                "interpretation": "Higher overlap preserves utility; it is not itself a privacy proof.",
            },
            "known_plaintext_procrustes": procrustes,
            "embedding_inversion_canaries": {
                "status": "canary_scan_only",
                "reason": "Full text reconstruction attack requires an adversarial decoder corpus.",
            },
            "issues": issues,
            "status": "pass" if not issues else "warn",
        }


def generate_privacy_report(db_path: str, data_dir: str | None = None) -> dict[str, Any]:
    data_dir = data_dir or config.get_data_dir()
    cfg = config.load_config()
    listeners = {
        "clipboard": bool(cfg.get("inputs", {}).get("clipboard")),
        "terminal_history": bool(cfg.get("inputs", {}).get("terminal_history")),
        "ambient_context": bool(cfg.get("inputs", {}).get("ambient_context")),
        "mobile_api": bool(cfg.get("mobile", {}).get("enabled")),
        "icloud_sync": bool(cfg.get("sync", {}).get("icloud_sync")),
        "mesh_sync": bool(cfg.get("sync", {}).get("mesh_sync")),
    }

    collections = [MAIN_COLLECTION]
    hot_collection = str(cfg.get("memory", {}).get("hot_index_collection") or HOT_COLLECTION)
    if hot_collection not in collections:
        collections.append(hot_collection)

    files: list[dict[str, Any]] = []
    for collection in collections:
        for path in vault_file_paths(db_path, collection=collection):
            record = _file_record(path)
            if record["exists"]:
                record["collection"] = collection
                files.append(record)

    pot_files = [
        _file_record(os.path.join(data_dir, ".pot_chain.jsonl")),
        _file_record(os.path.join(data_dir, ".pot_key.pem")),
    ]

    plaintext = {
        collection: _plaintext_scan(db_path, collection)
        for collection in collections
        if collection == MAIN_COLLECTION or _collection_exists(db_path, collection)
    }
    leakage = _privacy_leakage_harness(db_path, files)

    try:
        audit_ok, audit_message = SignedAuditLog(data_dir=data_dir).verify()
    except Exception as exc:
        audit_ok, audit_message = False, f"audit verification failed: {exc}"

    repairs = list_repairs(limit=100, data_dir=data_dir)
    repair_counts: dict[str, int] = {}
    for repair in repairs:
        status = str(repair.get("status", "pending"))
        repair_counts[status] = repair_counts.get(status, 0) + 1
    trust = trust_status(data_dir=data_dir)

    issues = []
    for record in files + pot_files:
        if record.get("exists") and not record.get("owner_only"):
            issues.append(f"insecure_mode:{record['path']}")
    for collection, scan in plaintext.items():
        if scan.get("sensitive_hits"):
            issues.append(f"sensitive_plaintext_sample:{collection}")
    if leakage["sidecar_plaintext_scan"].get("plaintext_canary_hits"):
        issues.append("sidecar_plaintext_canary")
    if not audit_ok:
        issues.append("audit_log_verification_failed")

    return {
        "schema": "latticeshadow.privacy_report.v1",
        "db_path": db_path,
        "data_dir": data_dir,
        "consent": consent_status(),
        "listeners": listeners,
        "hot_index_enabled": bool(cfg.get("memory", {}).get("hot_index_enabled")),
        "files": files,
        "pot_files": pot_files,
        "audit_log": {
            "path": os.path.join(data_dir, ".audit_log.jsonl"),
            "verified": audit_ok,
            "message": audit_message,
        },
        "repair_queue": {
            "path": os.path.join(data_dir, "repair_queue.jsonl"),
            "counts": repair_counts,
        },
        "device_trust": trust,
        "plaintext_scan": plaintext,
        "privacy_leakage": leakage,
        "issues": issues,
        "status": "pass" if not issues else "warn",
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return float(ordered[idx])


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _path_bytes(paths: list[str]) -> int:
    total = 0
    for path in paths:
        try:
            if os.path.exists(path):
                total += os.path.getsize(path)
        except OSError:
            pass
    return total


def _bench_search_lane(vault: Any, query_texts: list[str], target_ids: list[str]) -> dict[str, Any]:
    latencies = []
    nonempty_hits = 0
    recall_hits = 0
    for query, target_id in zip(query_texts, target_ids):
        start = time.perf_counter()
        result = vault.search(query, n_results=10)
        latencies.append((time.perf_counter() - start) * 1000.0)
        ids = list(getattr(result, "ids", []) or [])
        if ids:
            nonempty_hits += 1
        if target_id in ids:
            recall_hits += 1
    return {
        "p50_search_ms": _percentile(latencies, 50),
        "p95_search_ms": _percentile(latencies, 95),
        "hit_rate": nonempty_hits / len(query_texts),
        "recall_at_10": recall_hits / len(query_texts),
    }


def _gate_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "p95_search_under_100ms": metrics.get("p95_search_ms", float("inf")) < 100.0,
        "recall_at_10_gte_0_98": metrics.get("recall_at_10", 0.0) >= 0.98,
        "cold_open_under_5s": metrics.get("cold_open_ms", 0.0) < 5000.0,
        "insert_regression_under_25pct": metrics.get("insert_regression_ratio", 0.0) <= 1.25,
    }


def run_moonshot_bench(
    count: int = 250,
    dim: int = 128,
    queries: int = 10,
    master_key: str = "moonshot-bench",
    engines: list[str] | None = None,
) -> dict[str, Any]:
    count = max(1, int(count))
    dim = max(8, int(dim))
    queries = max(1, min(int(queries), count))
    engines = engines or ["streaming_exact"]
    if "all" in engines:
        engines = ["streaming_exact", "hnsw_rerank", "pq_rerank", "diskann_rerank", "matryoshka_64"]

    with tempfile.TemporaryDirectory(prefix="latticeshadow_moonshot_") as tmp:
        from latticeshadow_db.latticedb import connect

        db_path = os.path.join(tmp, "bench.sqlite")
        documents = [f"moonshot event {i:06d} topic_{i % 17}" for i in range(count)]
        ids = [f"bench_{i:06d}" for i in range(count)]
        query_indexes = [(i * max(1, count // queries)) % count for i in range(queries)]
        query_texts = [documents[idx] for idx in query_indexes]
        target_ids = [ids[idx] for idx in query_indexes]

        main = connect(
            db_path=db_path,
            collection=MAIN_COLLECTION,
            embedding_dim=dim,
            privacy=True,
            drosophila_hash=True,
            master_key=master_key,
            device="cpu",
        )
        chmod_collection_files(main)
        t0 = time.perf_counter()
        main.add(documents=documents, ids=ids, metadatas=[{"source": "bench"} for _ in documents])
        main_insert_ms = (time.perf_counter() - t0) * 1000.0
        chmod_collection_files(main)

        main_metrics = _bench_search_lane(main, query_texts, target_ids)
        main_metrics["insert_ms"] = main_insert_ms
        main_metrics["insert_ms_per_doc"] = main_insert_ms / count

        retrieval_lanes: dict[str, Any] = {}
        sidecar_bytes: dict[str, int] = {}
        engine_specs = {
            "streaming_exact": {"experimental_index": "streaming_exact", "embedding_dim": dim},
            "hnsw_rerank": {"experimental_index": "hnsw_rerank", "embedding_dim": dim},
            "pq_rerank": {"experimental_index": "pq_rerank", "embedding_dim": dim},
            "diskann_rerank": {"experimental_index": "diskann_rerank", "embedding_dim": dim},
            "matryoshka_64": {"experimental_index": "streaming_exact", "embedding_dim": min(dim, 64)},
        }

        for engine in engines:
            spec = engine_specs.get(engine)
            if not spec:
                retrieval_lanes[engine] = {"status": "skipped", "error": "unknown engine"}
                continue
            collection = HOT_COLLECTION if engine == "streaming_exact" else f"bench_{engine}"
            try:
                lane = connect(
                    db_path=db_path,
                    collection=collection,
                    embedding_dim=spec["embedding_dim"],
                    privacy=True,
                    drosophila_hash=False,
                    experimental_index=spec["experimental_index"],
                    master_key=master_key,
                    device="cpu",
                )
                chmod_collection_files(lane)
                t0 = time.perf_counter()
                lane.add(documents=documents, ids=ids, metadatas=[{"source": "bench", "engine": engine} for _ in documents])
                insert_ms = (time.perf_counter() - t0) * 1000.0
                chmod_collection_files(lane)

                metrics = _bench_search_lane(lane, query_texts, target_ids)
                cold_start = time.perf_counter()
                reloaded = connect(
                    db_path=db_path,
                    collection=collection,
                    embedding_dim=spec["embedding_dim"],
                    privacy=True,
                    drosophila_hash=False,
                    experimental_index=spec["experimental_index"],
                    master_key=master_key,
                    device="cpu",
                )
                chmod_collection_files(reloaded)
                metrics.update(
                    {
                        "status": "ok",
                        "insert_ms": insert_ms,
                        "insert_ms_per_doc": insert_ms / count,
                        "insert_regression_ratio": (
                            (insert_ms / count) / max(main_metrics["insert_ms_per_doc"], 1e-9)
                        ),
                        "cold_open_ms": (time.perf_counter() - cold_start) * 1000.0,
                        "cold_open_count": reloaded.count(),
                        "embedding_dim": spec["embedding_dim"],
                        "experimental_index": spec["experimental_index"],
                    }
                )
                metrics["gates"] = _gate_metrics(metrics)
                retrieval_lanes[engine] = metrics
                sidecar_bytes[engine] = _path_bytes(vault_file_paths(db_path, collection=collection))
            except Exception as exc:
                retrieval_lanes[engine] = {"status": "error", "error": str(exc)}
                sidecar_bytes[engine] = _path_bytes(vault_file_paths(db_path, collection=collection))

        ux_start = time.perf_counter()
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            conn.execute(
                "SELECT doc_id, document, metadata_json FROM vectors WHERE collection = ? ORDER BY created_at DESC LIMIT 20",
                (MAIN_COLLECTION,),
            ).fetchall()
        current_context_slice_ms = (time.perf_counter() - ux_start) * 1000.0

        privacy_leakage = run_privacy_leakage_harness(
            count=min(max(8, queries * 4), 32),
            dim=dim,
            queries=min(queries, 8),
            master_key=master_key + "-privacy",
        )

        return {
            "schema": "latticeshadow.moonshot_bench.v1",
            "count": count,
            "dim": dim,
            "queries": queries,
            "targets": {
                "p95_search_ms": 100.0,
                "recall_at_10": 0.98,
                "cold_open_ms": 5000.0,
            },
            "main_private_drosophila": main_metrics,
            "retrieval_lanes": retrieval_lanes,
            "hot_streaming_exact": retrieval_lanes.get("streaming_exact", {}),
            "sidecar_bytes": sidecar_bytes,
            "privacy_leakage": privacy_leakage,
            "ux_task_time_ms": {
                "current_context_slice": current_context_slice_ms,
            },
            "storage_bytes": _dir_size(tmp),
            "notes": [
                "Quick local smoke benchmark, not a 1M proof.",
                "Use latticeshadow-db moonshot proof workflow for publication-grade claims.",
            ],
        }


def write_json_report(report: dict[str, Any], output: str | None) -> str:
    payload = json.dumps(report, indent=2, sort_keys=True)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path)
    return payload

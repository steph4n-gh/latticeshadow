"""
Shared LatticeShadow vault openers.

The CLI keeps the Drosophila-hashed clipboard vault as the canonical store.
The optional hot vault is a dense streaming-exact sidecar for fast recall.
"""

import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from latticeshadow import config

MAIN_COLLECTION = "clipboard"
HOT_COLLECTION = "clipboard_hot"
HOT_INDEX_STRATEGY = "streaming_exact"
EMBEDDING_DIM = 128
EMBEDDING_MODEL = "sentence-transformers/static-retrieval-mrl-en-v1"
EMBEDDING_REVISION = "f60985c706f192d45d218078e49e5a8b6f15283a"


def embedding_model_id() -> str:
    if os.environ.get("LATTICESHADOW_EMBEDDING_MODEL") == "hash":
        return f"sha256-hash-v1:{EMBEDDING_DIM}"
    return f"{EMBEDDING_MODEL}@{EMBEDDING_REVISION}:{EMBEDDING_DIM}"


@lru_cache(maxsize=2)
def _load_model(bundle_path: str | None):
    from sentence_transformers import SentenceTransformer

    if bundle_path:
        return SentenceTransformer(
            bundle_path,
            local_files_only=True,
            truncate_dim=EMBEDDING_DIM,
            device="cpu",
        )
    return SentenceTransformer(
        EMBEDDING_MODEL,
        revision=EMBEDDING_REVISION,
        truncate_dim=EMBEDDING_DIM,
        device="cpu",
    )


def _local_model():
    return _load_model(os.environ.get("LATTICESHADOW_BUNDLED_MODEL") or None)


def embed_text(text: str):
    if os.environ.get("LATTICESHADOW_EMBEDDING_MODEL") == "hash":
        from latticeshadow_db.latticedb.embedder import _default_hash_embedding

        return _default_hash_embedding(text, EMBEDDING_DIM)
    import torch

    return torch.as_tensor(_local_model().encode(text, normalize_embeddings=True)).float()


def invalidate_holographic_indexes(data_dir: str) -> None:
    for path in Path(data_dir).glob("holographic_*.bin"):
        path.unlink()


def vault_file_paths(db_path: str, collection: str = MAIN_COLLECTION) -> list[str]:
    return [
        db_path,
        db_path + "-wal",
        db_path + "-shm",
        f"{db_path}_{collection}_vectors.bin",
        f"{db_path}_{collection}_vectors.bin.lock",
        f"{db_path}_{collection}_vectors.meta.json",
        f"{db_path}_{collection}_vectors.meta.json.tmp",
        f"{db_path}_{collection}_rerank_vectors.bin",
        f"{db_path}_{collection}_rerank_vectors.bin.lock",
        f"{db_path}_{collection}_pq_codes.bin",
        f"{db_path}_{collection}_pq_codes.bin.lock",
        f"{db_path}_{collection}_diskann_vectors.bin",
        f"{db_path}_{collection}_diskann_vectors.bin.lock",
        f"{db_path}_{collection}_diskann_graph.bin",
        f"{db_path}_{collection}_diskann_graph.bin.lock",
        f"{db_path}_{collection}_diskann_meta.json",
        f"{db_path}_{collection}_diskann_centroids.npy",
        f"{db_path}_{collection}_diskann_partition_offsets.npy",
        f"{db_path}_{collection}_diskann_partition_indices.bin",
        f"{db_path}_{collection}_diskann_assignments.bin",
        f"{db_path}_{collection}_streaming_norms.bin",
        f"{db_path}_{collection}_streaming_norms.bin.tmp",
        f"{db_path}_{collection}_streaming_norms_meta.json",
        f"{db_path}_{collection}_streaming_norms_meta.json.tmp",
    ]


def chmod_vault_files(db_path: str, collection: str = MAIN_COLLECTION) -> None:
    paths = vault_file_paths(db_path, collection=collection)
    for path in paths:
        if os.path.exists(path):
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass


def chmod_collection_files(vault: Any) -> None:
    store = getattr(vault, "_store", None)
    db_path = getattr(vault, "db_path", None) or getattr(store, "db_path", None)
    collection = getattr(vault, "name", None) or getattr(store, "collection", None)
    if isinstance(db_path, (str, os.PathLike)) and isinstance(collection, str):
        chmod_vault_files(str(db_path), collection=collection)


def _connect_vault(**kwargs: Any):
    from latticeshadow_db.latticedb import connect

    vault = connect(**kwargs)
    chmod_collection_files(vault)
    return vault


def open_main_vault(db_path: str, master_key: str, device: str | None = None):
    return _connect_vault(
        db_path=db_path,
        collection=MAIN_COLLECTION,
        embedding_fn=embed_text,
        embedding_dim=EMBEDDING_DIM,
        embedding_model=embedding_model_id(),
        privacy=True,
        drosophila_hash=True,
        master_key=master_key,
        device=device or config.get_device(),
    )


def open_hot_vault(
    db_path: str,
    master_key: str,
    device: str | None = None,
    collection: str | None = None,
    strategy: str | None = None,
):
    collection = collection or config.get("memory.hot_index_collection") or HOT_COLLECTION
    strategy = strategy or config.get("memory.hot_index_strategy") or HOT_INDEX_STRATEGY

    if collection == MAIN_COLLECTION:
        raise ValueError("memory.hot_index_collection must not be the canonical clipboard collection.")
    if strategy != HOT_INDEX_STRATEGY:
        raise ValueError("memory.hot_index_strategy must be 'streaming_exact' for the hot index.")

    return _connect_vault(
        db_path=db_path,
        collection=collection,
        embedding_fn=embed_text,
        embedding_dim=EMBEDDING_DIM,
        embedding_model=embedding_model_id(),
        privacy=True,
        drosophila_hash=False,
        experimental_index=strategy,
        master_key=master_key,
        device=device or config.get_device(),
    )


def hot_index_enabled() -> bool:
    return bool(config.get("memory.hot_index_enabled"))


def hot_collection_exists(db_path: str, collection: str | None = None) -> bool:
    collection = collection or config.get("memory.hot_index_collection") or HOT_COLLECTION
    if not os.path.exists(db_path):
        return False

    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "vectors" in tables:
                row = conn.execute(
                    "SELECT 1 FROM vectors WHERE collection = ? LIMIT 1",
                    (collection,),
                ).fetchone()
                if row:
                    return True
            if "collection_meta" in tables:
                row = conn.execute(
                    "SELECT 1 FROM collection_meta WHERE name = ? LIMIT 1",
                    (collection,),
                ).fetchone()
                if row:
                    return True
    except sqlite3.Error:
        return False

    return False

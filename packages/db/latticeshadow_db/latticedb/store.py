"""
VectorStore — Persistent vector storage with SQLite + FAISS indexing.

Extends the patterns from ActivationCache with:
- Document-level storage (doc_id, metadata, collections)
- Batch insert/search operations
- LRU eviction with configurable max_entries
- Metadata WHERE filtering
"""

import os
import sqlite3
import json
import hashlib
import time
import torch
import numpy as np
from typing import Optional, List, Tuple, Dict, Any, Sequence
from io import BytesIO
from dataclasses import dataclass, field
import logging
from base64 import b64encode, b64decode
from contextlib import contextmanager

from ..routing import calculate_activation_entropy

logger = logging.getLogger("latticeshadow_db.latticedb.store")

HNSW_EXACT_RERANK_INDEXES = {"hnsw_rerank", "hnsw_exact_rerank"}
FLYHASH_EXACT_RERANK_INDEXES = {"flyhash_rerank", "flyhash_exact_rerank", "flyhash_pq_rerank"}
PQ_EXACT_RERANK_INDEXES = {"pq_rerank", "pq_exact_rerank"}
DISKANN_EXACT_RERANK_INDEXES = {"diskann_rerank"}
STREAMING_EXACT_INDEXES = {"streaming_exact", "cascade_auto"}
EXPERIMENTAL_INDEXES = (
    HNSW_EXACT_RERANK_INDEXES
    | FLYHASH_EXACT_RERANK_INDEXES
    | PQ_EXACT_RERANK_INDEXES
    | DISKANN_EXACT_RERANK_INDEXES
    | STREAMING_EXACT_INDEXES
)

# Attempt FAISS import — graceful fallback to brute-force if unavailable
import sys

_FAISS_AVAILABLE = False
try:
    if not os.environ.get("ZK_NO_FAISS"):
        import faiss
        _FAISS_AVAILABLE = True
except ImportError:
    faiss = None

if not _FAISS_AVAILABLE:
    logger.debug("FAISS not available. Using brute-force cosine similarity for search.")

hnswlib = None
_HNSWLIB_AVAILABLE = False
try:
    if not os.environ.get("ZK_NO_HNSWLIB"):
        import hnswlib
        _HNSWLIB_AVAILABLE = True
except ImportError:
    hnswlib = None

if not _HNSWLIB_AVAILABLE:
    logger.debug("hnswlib not available. Native HNSW exact-rerank will fall back to exact search.")


@dataclass
class SearchResult:
    """Container for search results."""
    ids: List[str] = field(default_factory=list)
    documents: List[str] = field(default_factory=list)
    metadatas: List[Dict[str, Any]] = field(default_factory=list)
    scores: List[float] = field(default_factory=list)
    distances: List[float] = field(default_factory=list)
    served_locally: bool = False


class VectorStore:
    """
    Persistent vector store with SQLite backend and optional FAISS acceleration.
    Supports named collections, metadata filtering, batch operations, and LRU eviction.
    """

    def __init__(self, db_path: str = "latticedb.sqlite",
                 collection: str = "default",
                 max_entries: int = 0,
                 entropy_tolerance: float = 0.5,
                 drosophila_hash: bool = False,
                 engine: str = "default",
                 experimental_index: Optional[str] = None,
                 hnsw_m: int = 32,
                 hnsw_ef_construction: int = 200,
                 hnsw_ef_search: int = 128,
                 hnsw_candidate_cap: int = 1000,
                 diskann_graph_degree: int = 16,
                 diskann_probe_count: int = 8,
                 diskann_candidate_cap: int = 2000,
                 diskann_full_scan_threshold: int = 4096):
        """
        Args:
            db_path: Path to the SQLite database file.
            collection: Name of the vector collection.
            max_entries: Maximum entries before LRU eviction (0 = unlimited).
            entropy_tolerance: Max entropy delta for search validation.
            drosophila_hash: If True, enable Drosophila biological hashing & Hamming search.
            experimental_index: Optional experimental index strategy. Supports
                                exact-rerank HNSW/FlyHash/PQ aliases,
                                "diskann_rerank", "streaming_exact", and
                                conservative "cascade_auto".
        """
        if experimental_index not in (None, *sorted(EXPERIMENTAL_INDEXES)):
            raise ValueError(
                "experimental_index must be None or one of: "
                + ", ".join(repr(name) for name in sorted(EXPERIMENTAL_INDEXES))
            )
        if experimental_index in FLYHASH_EXACT_RERANK_INDEXES and not drosophila_hash:
            raise ValueError(f"experimental_index='{experimental_index}' requires drosophila_hash=True.")
        if experimental_index == "hnsw_exact_rerank" and drosophila_hash:
            raise ValueError("experimental_index='hnsw_exact_rerank' scans dense float vectors and cannot be combined with drosophila_hash=True.")
        if experimental_index in PQ_EXACT_RERANK_INDEXES and drosophila_hash:
            raise ValueError(f"experimental_index='{experimental_index}' stores dense int8 codes and cannot be combined with drosophila_hash=True.")
        if experimental_index in DISKANN_EXACT_RERANK_INDEXES and drosophila_hash:
            raise ValueError(f"experimental_index='{experimental_index}' stores dense fp16 sidecars and cannot be combined with drosophila_hash=True.")
        if experimental_index in STREAMING_EXACT_INDEXES and drosophila_hash:
            raise ValueError(f"experimental_index='{experimental_index}' scans dense float vectors and cannot be combined with drosophila_hash=True.")
        if hnsw_m <= 0:
            raise ValueError("hnsw_m must be positive.")
        if hnsw_ef_construction <= 0:
            raise ValueError("hnsw_ef_construction must be positive.")
        if hnsw_ef_search <= 0:
            raise ValueError("hnsw_ef_search must be positive.")
        if hnsw_candidate_cap <= 0:
            raise ValueError("hnsw_candidate_cap must be positive.")

        self.db_path = db_path
        self.collection = collection
        self.max_entries = max_entries
        self.entropy_tolerance = entropy_tolerance
        self.drosophila_hash = drosophila_hash
        self.engine = engine
        self.experimental_index = experimental_index
        self._hnsw_rerank_enabled = experimental_index in HNSW_EXACT_RERANK_INDEXES
        self._hnsw_rerank_candidate_min = 100
        self._hnsw_rerank_candidate_multiplier = 20
        self._hnsw_rerank_candidate_cap = int(hnsw_candidate_cap)
        self._native_hnsw_enabled = experimental_index == "hnsw_exact_rerank"
        self._hnsw_m = int(hnsw_m)
        self._hnsw_ef_construction = int(hnsw_ef_construction)
        self._hnsw_ef_search = int(hnsw_ef_search)
        self._hnswlib_index = None
        self._hnswlib_dim = None
        self._hnswlib_count = 0
        self._hnswlib_sidecar_meta = None
        self._hnswlib_sidecars_dirty = False
        self._flyhash_rerank_enabled = experimental_index in FLYHASH_EXACT_RERANK_INDEXES
        self._flyhash_rerank_candidate_min = 100
        self._flyhash_rerank_candidate_multiplier = 20
        self._flyhash_rerank_candidate_cap = 1000
        self._pq_rerank_enabled = experimental_index in PQ_EXACT_RERANK_INDEXES
        self._pq_rerank_candidate_min = 100
        self._pq_rerank_candidate_multiplier = 20
        self._pq_rerank_candidate_cap = 1000
        self._diskann_rerank_enabled = experimental_index in DISKANN_EXACT_RERANK_INDEXES
        self._streaming_exact_enabled = experimental_index in STREAMING_EXACT_INDEXES
        self._cascade_auto_enabled = experimental_index == "cascade_auto"
        self._diskann_rerank_candidate_min = 100
        self._diskann_rerank_candidate_multiplier = 20
        self._diskann_rerank_candidate_cap = int(diskann_candidate_cap)
        self._diskann_graph_degree = int(diskann_graph_degree)
        self._diskann_beam_width = 32
        self._diskann_visit_cap = 4096
        self._diskann_full_scan_threshold = int(diskann_full_scan_threshold)
        self._diskann_empty_neighbor = np.iinfo(np.uint32).max
        self._diskann_probe_count = int(diskann_probe_count)
        self._diskann_partition_count = 0
        self._diskann_centroids = None
        self._diskann_partition_offsets = None
        self._diskann_partition_indices_memmap = None
        self._diskann_assignments_memmap = None
        self._diskann_sidecar_meta = None
        self._streaming_exact_norms = None
        self._streaming_exact_inv_norms = None
        self._defer_diskann_sidecar_build = False
        self._diskann_sidecars_dirty = False
        self._store_drosophila = drosophila_hash and engine != "holographic"
        self._holographic_state = None

        # In-memory state
        self._vectors: List[torch.Tensor] = []
        self._doc_ids: List[str] = []
        self._entropies: List[float] = []
        self._doc_id_set: set = set()

        # Memmap state
        self._memmap = None
        self._memmap_capacity = 0
        self._memmap_dim = None
        self._rerank_memmap = None
        self._rerank_memmap_capacity = 0
        self._rerank_memmap_dim = None
        self._pq_memmap = None
        self._pq_memmap_capacity = 0
        self._pq_memmap_dim = None
        self._diskann_vector_memmap = None
        self._diskann_vector_memmap_capacity = 0
        self._diskann_vector_memmap_dim = None
        self._diskann_graph_memmap = None
        self._diskann_graph_memmap_capacity = 0

        # FAISS state
        self._faiss_index = None
        self._faiss_dim: Optional[int] = None
        self._use_faiss = False

        self._init_db()
        self._load_from_db()
        self._observed_revision = self.revision()

    def _get_drosophila_hasher(self, dim: int):
        if not hasattr(self, "_drosophila_hasher") or self._drosophila_hasher is None:
            from .drosophila import DrosophilaHasher
            self._drosophila_hasher = DrosophilaHasher(input_dim=dim)
        return self._drosophila_hasher


    @contextmanager
    def _lock_file(self, shared=False):
        """Acquire lock on vectors lock file with exponential backoff retry."""
        import fcntl
        import random
        filepath = f"{self.db_path}_{self.collection}_vectors.bin"
        lock_path = f"{filepath}.lock"
        f = None
        try:
            f = open(lock_path, "a+")
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            max_retries = 5
            base_delay = 0.02
            for attempt in range(max_retries + 1):
                try:
                    fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
                    break
                except (IOError, OSError) as e:
                    if attempt == max_retries:
                        raise IOError(f"Lock acquisition failed on {lock_path} after {max_retries} retries: {e}")
                    delay = min(0.5, base_delay * (2 ** attempt))
                    delay = delay * (0.5 + random.random())
                    time.sleep(delay)
            yield f
        finally:
            if f is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    f.close()
                except Exception:
                    pass

    def _ensure_memmap(self):
        """Sync memmap capacity with the physical file size on disk if modified externally."""
        if self._memmap_dim is None:
            return
        filepath = f"{self.db_path}_{self.collection}_vectors.bin"
        if not os.path.exists(filepath):
            return
        
        dtype = 'uint8' if self._store_drosophila else 'float32'
        bytes_per_elem = 1 if self._store_drosophila else 4
        
        try:
            actual_size = os.path.getsize(filepath)
            expected_size = self._memmap_capacity * self._memmap_dim * bytes_per_elem
            if actual_size != expected_size:
                row_bytes = self._memmap_dim * bytes_per_elem
                if row_bytes <= 0 or actual_size <= 0 or actual_size % row_bytes != 0:
                    logger.warning("Memmap file size mismatch. Keeping existing mapping.")
                    return
                # File size changed (e.g. by another process or grown)
                new_capacity = actual_size // row_bytes
                
                # Delete existing memmap reference to avoid locking the file
                if self._memmap is not None:
                    try:
                        self._memmap.flush()
                    except Exception:
                        pass
                    del self._memmap
                    self._memmap = None
                
                self._memmap_capacity = new_capacity
                self._memmap = np.memmap(
                    filepath,
                    dtype=dtype,
                    mode='r+',
                    shape=(self._memmap_capacity, self._memmap_dim)
                )
        except Exception as e:
            logger.warning(f"Error ensuring memmap alignment: {e}")

    def _init_memmap(self, dim: int, initial_capacity: Optional[int] = None):
        """Initialize memory-mapped storage file."""
        filepath = f"{self.db_path}_{self.collection}_vectors.bin"
        self._memmap_dim = dim
        if initial_capacity is None:
            initial_capacity = self.max_entries if self.max_entries > 0 else 100
        self._memmap_capacity = max(1, int(initial_capacity))
        
        dtype = 'uint8' if self._store_drosophila else 'float32'
        bytes_per_elem = 1 if self._store_drosophila else 4
        
        with self._lock_file(shared=False):
            if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
                # Create and write zeros
                m = np.memmap(filepath, dtype=dtype, mode='w+', shape=(self._memmap_capacity, self._memmap_dim))
                m[:] = 0
                m.flush()
                del m
                
            self._memmap = np.memmap(
                filepath,
                dtype=dtype,
                mode='r+',
                shape=(self._memmap_capacity, self._memmap_dim)
            )

    def _grow_memmap(self):
        """Double the memmap capacity on disk and re-map."""
        if self._memmap is None or self._memmap_dim is None:
            return
        
        dtype = 'uint8' if self._store_drosophila else 'float32'
        bytes_per_elem = 1 if self._store_drosophila else 4
        
        with self._lock_file(shared=False):
            new_capacity = self._memmap_capacity * 2
            filepath = f"{self.db_path}_{self.collection}_vectors.bin"
            
            # Flush and delete reference
            self._memmap.flush()
            del self._memmap
            self._memmap = None
            
            # Open file in "r+b" mode and truncate
            with open(filepath, "r+b") as f:
                f.truncate(new_capacity * self._memmap_dim * bytes_per_elem)
            
            # Re-open in "r+" mode
            self._memmap_capacity = new_capacity
            self._memmap = np.memmap(
                filepath,
                dtype=dtype,
                mode='r+',
                shape=(self._memmap_capacity, self._memmap_dim)
            )

    def _rerank_memmap_path(self) -> str:
        return f"{self.db_path}_{self.collection}_rerank_vectors.bin"

    def _pq_memmap_path(self) -> str:
        return f"{self.db_path}_{self.collection}_pq_codes.bin"

    def _hnswlib_index_path(self) -> str:
        return f"{self.db_path}_{self.collection}_hnswlib.bin"

    def _hnswlib_metadata_path(self) -> str:
        return f"{self.db_path}_{self.collection}_hnswlib_meta.json"

    def _diskann_vector_memmap_path(self) -> str:
        return f"{self.db_path}_{self.collection}_diskann_vectors.bin"

    def _diskann_graph_memmap_path(self) -> str:
        return f"{self.db_path}_{self.collection}_diskann_graph.bin"

    def _streaming_norms_path(self) -> str:
        return f"{self.db_path}_{self.collection}_streaming_norms.bin"

    def _streaming_norms_metadata_path(self) -> str:
        return f"{self.db_path}_{self.collection}_streaming_norms_meta.json"

    def _streaming_exact_vector_signature(self) -> Optional[dict]:
        filepath = f"{self.db_path}_{self.collection}_vectors.bin"
        try:
            stat = os.stat(filepath)
        except OSError:
            return None
        return {
            "vector_size": int(stat.st_size),
            "vector_mtime_ns": int(stat.st_mtime_ns),
        }

    def _invalidate_streaming_exact_norms(self, remove_sidecar: bool = False) -> None:
        self._streaming_exact_norms = None
        self._streaming_exact_inv_norms = None
        if remove_sidecar:
            for path in (
                self._streaming_norms_path(),
                self._streaming_norms_metadata_path(),
                f"{self._streaming_norms_path()}.tmp",
                f"{self._streaming_norms_metadata_path()}.tmp",
            ):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

    def _load_streaming_exact_norms(self, count: int) -> Optional[np.ndarray]:
        if count <= 0 or self._memmap_dim is None:
            return None
        norms_path = self._streaming_norms_path()
        meta_path = self._streaming_norms_metadata_path()
        signature = self._streaming_exact_vector_signature()
        if signature is None or not os.path.exists(norms_path) or not os.path.exists(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            expected_size = count * np.dtype(np.float32).itemsize
            if (
                meta.get("schema_version") != 1
                or int(meta.get("count", -1)) != count
                or int(meta.get("dim", -1)) != int(self._memmap_dim)
                or int(meta.get("vector_size", -1)) != signature["vector_size"]
                or int(meta.get("vector_mtime_ns", -1)) != signature["vector_mtime_ns"]
                or os.path.getsize(norms_path) != expected_size
            ):
                return None
            norms = np.memmap(norms_path, dtype=np.float32, mode="r", shape=(count,))
            inv_norms = np.zeros(count, dtype=np.float32)
            np.divide(1.0, norms, out=inv_norms, where=norms > 0.0)
        except Exception as exc:
            logger.debug("Could not load streaming_exact norm sidecar: %s", exc)
            return None
        self._streaming_exact_norms = norms
        self._streaming_exact_inv_norms = inv_norms
        return norms

    def _write_streaming_exact_norms(self, norms: np.ndarray) -> None:
        if not (self._streaming_exact_enabled or self._native_hnsw_enabled) or self._memmap_dim is None:
            return
        signature = self._streaming_exact_vector_signature()
        if signature is None:
            return
        norms_path = self._streaming_norms_path()
        meta_path = self._streaming_norms_metadata_path()
        os.makedirs(os.path.dirname(os.path.abspath(norms_path)), exist_ok=True)
        try:
            tmp_norms = f"{norms_path}.tmp"
            np.asarray(norms, dtype=np.float32).tofile(tmp_norms)
            os.replace(tmp_norms, norms_path)
            meta = {
                "schema_version": 1,
                "count": int(norms.shape[0]),
                "dim": int(self._memmap_dim),
                **signature,
            }
            tmp_meta = f"{meta_path}.tmp"
            with open(tmp_meta, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, sort_keys=True)
            os.replace(tmp_meta, meta_path)
        except Exception as exc:
            logger.debug("Could not write streaming_exact norm sidecar: %s", exc)

    def _streaming_exact_chunk_size(self, dim: int, count: int) -> int:
        if count <= 0:
            return 0
        target_bytes = 384 * 1024 * 1024
        row_bytes = max(1, int(dim)) * np.dtype(np.float32).itemsize
        return max(4096, min(int(count), target_bytes // row_bytes, 262144))

    def _ensure_streaming_exact_norms(self, chunk_size: int = 65536) -> Optional[np.ndarray]:
        if not (self._streaming_exact_enabled or self._native_hnsw_enabled) or self._memmap is None or self._memmap_dim is None:
            return None
        count = len(self._doc_ids)
        if (
            self._streaming_exact_norms is not None
            and self._streaming_exact_norms.shape[0] == count
        ):
            return self._streaming_exact_norms
        loaded = self._load_streaming_exact_norms(count)
        if loaded is not None:
            return loaded
        norms = np.zeros(count, dtype=np.float32)
        with self._lock_file(shared=True):
            self._ensure_memmap()
            if self._memmap is None:
                return None
            for start in range(0, count, chunk_size):
                end = min(start + chunk_size, count)
                rows = np.asarray(self._memmap[start:end], dtype=np.float32)
                norms[start:end] = np.linalg.norm(rows, axis=1).astype(np.float32, copy=False)
        self._streaming_exact_norms = norms
        inv_norms = np.zeros_like(norms, dtype=np.float32)
        np.divide(1.0, norms, out=inv_norms, where=norms > 0.0)
        self._streaming_exact_inv_norms = inv_norms
        self._write_streaming_exact_norms(norms)
        return norms

    def _invalidate_hnswlib_index(self, remove_sidecar: bool = False) -> None:
        self._hnswlib_index = None
        self._hnswlib_dim = None
        self._hnswlib_count = 0
        self._hnswlib_sidecar_meta = None
        self._hnswlib_sidecars_dirty = bool(self._native_hnsw_enabled and self._doc_ids)
        if remove_sidecar:
            for path in (
                self._hnswlib_index_path(),
                self._hnswlib_metadata_path(),
                f"{self._hnswlib_index_path()}.tmp",
                f"{self._hnswlib_metadata_path()}.tmp",
            ):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

    def _hnswlib_config_dict(
        self,
        dim: int,
        count: int,
        max_elements: int,
        m: Optional[int] = None,
        ef_construction: Optional[int] = None,
        ef_search: Optional[int] = None,
        candidate_limit: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        signature = self._streaming_exact_vector_signature()
        if signature is None:
            return None
        config = {
            "version": 1,
            "collection": self.collection,
            "dim": int(dim),
            "count": int(count),
            "max_elements": int(max_elements),
            "space": "cosine",
            "m": int(m or self._hnsw_m),
            "ef_construction": int(ef_construction or self._hnsw_ef_construction),
            "ef_search": int(ef_search or self._hnsw_ef_search),
            "candidate_cap": int(candidate_limit or self._hnsw_rerank_candidate_cap),
            **signature,
        }
        config["config_hash"] = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return config

    def _write_hnswlib_metadata(self, config: Dict[str, Any]) -> None:
        tmp_path = self._hnswlib_metadata_path() + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, sort_keys=True, indent=2)
        os.replace(tmp_path, self._hnswlib_metadata_path())
        self._hnswlib_sidecar_meta = dict(config)
        self._hnsw_m = int(config["m"])
        self._hnsw_ef_construction = int(config["ef_construction"])
        self._hnsw_ef_search = int(config["ef_search"])
        self._hnsw_rerank_candidate_cap = int(config["candidate_cap"])

    def _load_hnswlib_metadata(self, dim: int) -> Optional[Dict[str, Any]]:
        path = self._hnswlib_metadata_path()
        signature = self._streaming_exact_vector_signature()
        if signature is None or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        expected = dict(config)
        config_hash = expected.pop("config_hash", None)
        recomputed = hashlib.sha256(
            json.dumps(expected, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        if config_hash != recomputed:
            logger.warning("hnswlib metadata hash mismatch. Falling back to exact search.")
            return None
        if (
            config.get("version") != 1
            or config.get("collection") != self.collection
            or int(config.get("dim", -1)) != int(dim)
            or int(config.get("count", -1)) != len(self._doc_ids)
            or int(config.get("vector_size", -1)) != signature["vector_size"]
            or int(config.get("vector_mtime_ns", -1)) != signature["vector_mtime_ns"]
            or int(config.get("m", 0)) <= 0
            or int(config.get("ef_construction", 0)) <= 0
            or int(config.get("ef_search", 0)) <= 0
        ):
            return None
        return config

    def _open_hnswlib_index(self, dim: int) -> bool:
        if not self._native_hnsw_enabled or not _HNSWLIB_AVAILABLE or hnswlib is None:
            return False
        if (
            self._hnswlib_index is not None
            and self._hnswlib_dim == dim
            and self._hnswlib_count == len(self._doc_ids)
            and not self._hnswlib_sidecars_dirty
        ):
            return True
        index_path = self._hnswlib_index_path()
        config = self._load_hnswlib_metadata(dim)
        if config is None or not os.path.exists(index_path):
            return False
        try:
            index = hnswlib.Index(space="cosine", dim=dim)
            try:
                index.load_index(index_path, max_elements=max(int(config["max_elements"]), len(self._doc_ids)))
            except TypeError:
                index.load_index(index_path)
            index.set_ef(int(config["ef_search"]))
        except Exception as exc:
            logger.warning("Could not open hnswlib sidecar. Falling back to exact search: %s", exc)
            return False
        self._hnswlib_index = index
        self._hnswlib_dim = dim
        self._hnswlib_count = len(self._doc_ids)
        self._hnswlib_sidecar_meta = dict(config)
        self._hnswlib_sidecars_dirty = False
        self._hnsw_m = int(config["m"])
        self._hnsw_ef_construction = int(config["ef_construction"])
        self._hnsw_ef_search = int(config["ef_search"])
        self._hnsw_rerank_candidate_cap = int(config["candidate_cap"])
        return True

    def build_hnswlib_sidecar(
        self,
        m: Optional[int] = None,
        ef_construction: Optional[int] = None,
        ef_search: Optional[int] = None,
        candidate_limit: Optional[int] = None,
        chunk_size: int = 65536,
    ) -> Dict[str, Any]:
        """Bulk-build the optional native hnswlib candidate sidecar."""
        if not self._native_hnsw_enabled:
            return {}
        if not _HNSWLIB_AVAILABLE or hnswlib is None:
            self._invalidate_hnswlib_index(remove_sidecar=False)
            return {
                "available": False,
                "reason": "hnswlib_unavailable",
            }
        if self._store_drosophila:
            return {
                "available": False,
                "reason": "dense_vectors_required",
            }
        count = len(self._doc_ids)
        if count == 0 or self._memmap is None or self._memmap_dim is None:
            self._invalidate_hnswlib_index(remove_sidecar=True)
            return {}
        dim = int(self._memmap_dim)
        m = int(m or self._hnsw_m)
        ef_construction = int(ef_construction or self._hnsw_ef_construction)
        ef_search = int(ef_search or self._hnsw_ef_search)
        candidate_limit = int(candidate_limit or self._hnsw_rerank_candidate_cap)
        max_elements = max(self.max_entries if self.max_entries > 0 else count, count)
        config = self._hnswlib_config_dict(
            dim=dim,
            count=count,
            max_elements=max_elements,
            m=m,
            ef_construction=ef_construction,
            ef_search=ef_search,
            candidate_limit=candidate_limit,
        )
        if config is None:
            return {
                "available": False,
                "reason": "vector_signature_unavailable",
            }

        index_path = self._hnswlib_index_path()
        os.makedirs(os.path.dirname(os.path.abspath(index_path)), exist_ok=True)
        self._invalidate_hnswlib_index(remove_sidecar=True)
        try:
            index = hnswlib.Index(space="cosine", dim=dim)
            index.init_index(max_elements=max_elements, ef_construction=ef_construction, M=m)
            index.set_ef(ef_search)
            with self._lock_file(shared=True):
                self._ensure_memmap()
                for start in range(0, count, max(1, int(chunk_size))):
                    end = min(start + max(1, int(chunk_size)), count)
                    rows = np.asarray(self._memmap[start:end], dtype=np.float32)
                    labels = np.arange(start, end, dtype=np.int64)
                    index.add_items(rows, labels)
            index.save_index(index_path)
        except Exception as exc:
            self._invalidate_hnswlib_index(remove_sidecar=True)
            logger.warning("Could not build hnswlib sidecar. Falling back to exact search: %s", exc)
            return {
                "available": False,
                "reason": "build_failed",
                "error": str(exc),
            }

        self._hnswlib_index = index
        self._hnswlib_dim = dim
        self._hnswlib_count = count
        self._hnswlib_sidecars_dirty = False
        self._write_hnswlib_metadata(config)
        return {
            **config,
            "available": True,
            "index_path": index_path,
            "metadata_path": self._hnswlib_metadata_path(),
        }

    def _ensure_hnswlib_index(self, dim: int) -> bool:
        if not self._native_hnsw_enabled or len(self._doc_ids) == 0:
            return False
        if not _HNSWLIB_AVAILABLE or hnswlib is None:
            return False
        if self._open_hnswlib_index(dim):
            return True
        meta = self.build_hnswlib_sidecar(chunk_size=65536)
        return bool(meta.get("available"))

    @contextmanager
    def defer_diskann_sidecar_build(self):
        """Defer expensive DiskANN sidecar rebuilds until a bulk load finishes."""
        old_value = self._defer_diskann_sidecar_build
        self._defer_diskann_sidecar_build = True
        try:
            yield
        finally:
            self._defer_diskann_sidecar_build = old_value
            if (
                self._diskann_rerank_enabled
                and self._diskann_sidecars_dirty
                and not self._defer_diskann_sidecar_build
            ):
                self.build_diskann_sidecars()
                self._diskann_sidecars_dirty = False

    def _diskann_metadata_path(self) -> str:
        return f"{self.db_path}_{self.collection}_diskann_meta.json"

    def _diskann_centroids_path(self) -> str:
        return f"{self.db_path}_{self.collection}_diskann_centroids.npy"

    def _diskann_partition_offsets_path(self) -> str:
        return f"{self.db_path}_{self.collection}_diskann_partition_offsets.npy"

    def _diskann_partition_indices_path(self) -> str:
        return f"{self.db_path}_{self.collection}_diskann_partition_indices.bin"

    def _diskann_assignments_path(self) -> str:
        return f"{self.db_path}_{self.collection}_diskann_assignments.bin"

    def _diskann_target_partition_count(self, count: int) -> int:
        if count <= 0:
            return 0
        return min(256, max(16, int(np.ceil(np.sqrt(float(count)) / 4.0))))

    def _diskann_config_dict(
        self,
        dim: int,
        count: int,
        partition_count: Optional[int] = None,
        probe_count: Optional[int] = None,
        candidate_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        partitions = partition_count or self._diskann_target_partition_count(count)
        probes = probe_count or min(self._diskann_probe_count, max(1, partitions))
        candidate_cap = candidate_limit or self._diskann_rerank_candidate_cap
        config = {
            "version": 2,
            "collection": self.collection,
            "dim": int(dim),
            "count": int(count),
            "partition_count": int(partitions),
            "probe_count": int(probes),
            "candidate_cap": int(candidate_cap),
            "graph_degree": int(self._diskann_graph_degree),
            "dtype": "float16_unit_vectors",
        }
        config["config_hash"] = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return config

    def _write_diskann_metadata(self, config: Dict[str, Any]) -> None:
        tmp_path = self._diskann_metadata_path() + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, sort_keys=True, indent=2)
        os.replace(tmp_path, self._diskann_metadata_path())
        self._diskann_sidecar_meta = dict(config)
        self._diskann_partition_count = int(config["partition_count"])
        self._diskann_probe_count = int(config["probe_count"])
        self._diskann_rerank_candidate_cap = int(config["candidate_cap"])

    def _load_diskann_metadata(self, dim: int) -> Optional[Dict[str, Any]]:
        path = self._diskann_metadata_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        expected = dict(config)
        config_hash = expected.pop("config_hash", None)
        recomputed = hashlib.sha256(
            json.dumps(expected, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        if config_hash != recomputed:
            logger.warning("DiskANN metadata hash mismatch. Falling back to exact search.")
            return None
        if (
            config.get("version") != 2
            or config.get("collection") != self.collection
            or int(config.get("dim", -1)) != int(dim)
            or int(config.get("count", -1)) != len(self._doc_ids)
            or int(config.get("partition_count", 0)) <= 0
        ):
            return None
        return config

    def _init_rerank_memmap(self, dim: int, initial_capacity: Optional[int] = None):
        """Initialize fp16 sidecar vectors for exact rerank after FlyHash filtering."""
        if not self._flyhash_rerank_enabled:
            return
        filepath = self._rerank_memmap_path()
        self._rerank_memmap_dim = dim
        if initial_capacity is None:
            initial_capacity = self.max_entries if self.max_entries > 0 else 100
        self._rerank_memmap_capacity = max(1, int(initial_capacity))

        with self._lock_file(shared=False):
            if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
                m = np.memmap(
                    filepath,
                    dtype='float16',
                    mode='w+',
                    shape=(self._rerank_memmap_capacity, self._rerank_memmap_dim),
                )
                m[:] = 0
                m.flush()
                del m

            self._rerank_memmap = np.memmap(
                filepath,
                dtype='float16',
                mode='r+',
                shape=(self._rerank_memmap_capacity, self._rerank_memmap_dim),
            )

    def _open_rerank_memmap(self, dim: int) -> bool:
        """Open an existing rerank sidecar using query/insert dimension."""
        if not self._flyhash_rerank_enabled:
            return False
        if self._rerank_memmap is not None and self._rerank_memmap_dim == dim:
            return True
        filepath = self._rerank_memmap_path()
        if not os.path.exists(filepath):
            return False
        row_bytes = dim * 2
        actual_size = os.path.getsize(filepath)
        if row_bytes <= 0 or actual_size <= 0 or actual_size % row_bytes != 0:
            logger.warning("FlyHash rerank sidecar size mismatch. Falling back to Hamming ranking.")
            return False
        capacity = actual_size // row_bytes
        if capacity < len(self._doc_ids):
            logger.warning("FlyHash rerank sidecar is undersized. Falling back to Hamming ranking.")
            return False
        self._rerank_memmap_dim = dim
        self._rerank_memmap_capacity = capacity
        self._rerank_memmap = np.memmap(
            filepath,
            dtype='float16',
            mode='r+',
            shape=(capacity, dim),
        )
        return True

    def _grow_rerank_memmap(self):
        if self._rerank_memmap is None or self._rerank_memmap_dim is None:
            return
        with self._lock_file(shared=False):
            new_capacity = self._rerank_memmap_capacity * 2
            filepath = self._rerank_memmap_path()
            self._rerank_memmap.flush()
            del self._rerank_memmap
            self._rerank_memmap = None
            with open(filepath, "r+b") as f:
                f.truncate(new_capacity * self._rerank_memmap_dim * 2)
            self._rerank_memmap_capacity = new_capacity
            self._rerank_memmap = np.memmap(
                filepath,
                dtype='float16',
                mode='r+',
                shape=(self._rerank_memmap_capacity, self._rerank_memmap_dim),
            )

    def _set_rerank_vector_at(self, idx: int, vec: torch.Tensor):
        if not self._flyhash_rerank_enabled:
            return
        dim = vec.view(-1).shape[0]
        if self._rerank_memmap is None:
            self._init_rerank_memmap(dim)
        if self._rerank_memmap_dim != dim:
            logger.warning("FlyHash rerank sidecar dimension mismatch. Skipping sidecar write.")
            return
        while idx >= self._rerank_memmap_capacity:
            self._grow_rerank_memmap()
        np_vec = vec.detach().cpu().float().numpy().astype(np.float16).reshape(-1)
        with self._lock_file(shared=False):
            self._rerank_memmap[idx] = np_vec
            self._rerank_memmap.flush()

    def _get_rerank_vector_at(self, idx: int, dim: int) -> Optional[torch.Tensor]:
        if not self._open_rerank_memmap(dim):
            return None
        if idx < 0 or idx >= self._rerank_memmap_capacity:
            return None
        with self._lock_file(shared=True):
            vec_np = np.array(self._rerank_memmap[idx], dtype=np.float32)
        return torch.from_numpy(vec_np)

    def _init_pq_memmap(self, dim: int, initial_capacity: Optional[int] = None):
        """Initialize int8 PQ-lite sidecar codes for compressed candidate scans."""
        if not self._pq_rerank_enabled:
            return
        filepath = self._pq_memmap_path()
        self._pq_memmap_dim = dim
        if initial_capacity is None:
            initial_capacity = self.max_entries if self.max_entries > 0 else 100
        self._pq_memmap_capacity = max(1, int(initial_capacity))

        with self._lock_file(shared=False):
            if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
                m = np.memmap(
                    filepath,
                    dtype='int8',
                    mode='w+',
                    shape=(self._pq_memmap_capacity, self._pq_memmap_dim),
                )
                m[:] = 0
                m.flush()
                del m

            self._pq_memmap = np.memmap(
                filepath,
                dtype='int8',
                mode='r+',
                shape=(self._pq_memmap_capacity, self._pq_memmap_dim),
            )

    def _open_pq_memmap(self, dim: int) -> bool:
        """Open an existing PQ-lite sidecar using query/insert dimension."""
        if not self._pq_rerank_enabled:
            return False
        if self._pq_memmap is not None and self._pq_memmap_dim == dim:
            return True
        filepath = self._pq_memmap_path()
        if not os.path.exists(filepath):
            return False
        row_bytes = dim
        actual_size = os.path.getsize(filepath)
        if row_bytes <= 0 or actual_size <= 0 or actual_size % row_bytes != 0:
            logger.warning("PQ rerank sidecar size mismatch. Falling back to exact search.")
            return False
        capacity = actual_size // row_bytes
        if capacity < len(self._doc_ids):
            logger.warning("PQ rerank sidecar is undersized. Falling back to exact search.")
            return False
        self._pq_memmap_dim = dim
        self._pq_memmap_capacity = capacity
        self._pq_memmap = np.memmap(
            filepath,
            dtype='int8',
            mode='r+',
            shape=(capacity, dim),
        )
        return True

    def _grow_pq_memmap(self):
        if self._pq_memmap is None or self._pq_memmap_dim is None:
            return
        with self._lock_file(shared=False):
            new_capacity = self._pq_memmap_capacity * 2
            filepath = self._pq_memmap_path()
            self._pq_memmap.flush()
            del self._pq_memmap
            self._pq_memmap = None
            with open(filepath, "r+b") as f:
                f.truncate(new_capacity * self._pq_memmap_dim)
            self._pq_memmap_capacity = new_capacity
            self._pq_memmap = np.memmap(
                filepath,
                dtype='int8',
                mode='r+',
                shape=(self._pq_memmap_capacity, self._pq_memmap_dim),
            )

    @staticmethod
    def _pq_encode_vector(vec: torch.Tensor) -> np.ndarray:
        """Encode a dense vector as a normalized int8 PQ-lite code."""
        flat = vec.detach().cpu().view(-1).float()
        norm = torch.linalg.norm(flat)
        if norm.item() <= 1e-8:
            return np.zeros(flat.shape[0], dtype=np.int8)
        normalized = flat / norm
        encoded = torch.round(torch.clamp(normalized, -1.0, 1.0) * 127.0).to(torch.int8)
        return encoded.numpy().astype(np.int8, copy=False)

    def _set_pq_code_at(self, idx: int, vec: torch.Tensor):
        if not self._pq_rerank_enabled:
            return
        dim = vec.view(-1).shape[0]
        if self._pq_memmap is None:
            self._init_pq_memmap(dim)
        if self._pq_memmap_dim != dim:
            logger.warning("PQ rerank sidecar dimension mismatch. Skipping sidecar write.")
            return
        while idx >= self._pq_memmap_capacity:
            self._grow_pq_memmap()
        np_code = self._pq_encode_vector(vec).reshape(-1)
        with self._lock_file(shared=False):
            self._pq_memmap[idx] = np_code
            self._pq_memmap.flush()

    def _close_diskann_memmaps(self):
        for attr in (
            "_diskann_vector_memmap",
            "_diskann_graph_memmap",
            "_diskann_partition_indices_memmap",
            "_diskann_assignments_memmap",
        ):
            memmap = getattr(self, attr, None)
            if memmap is not None:
                try:
                    memmap.flush()
                except Exception:
                    pass
                try:
                    del memmap
                except Exception:
                    pass
                setattr(self, attr, None)
        self._diskann_centroids = None
        self._diskann_partition_offsets = None
        self._diskann_sidecar_meta = None

    def _init_diskann_memmaps(self, dim: int, initial_capacity: Optional[int] = None):
        """Initialize DiskANN-inspired fp16 vector and Vamana-lite graph sidecars."""
        if not self._diskann_rerank_enabled:
            return
        vector_path = self._diskann_vector_memmap_path()
        graph_path = self._diskann_graph_memmap_path()
        self._diskann_vector_memmap_dim = dim
        if initial_capacity is None:
            initial_capacity = self.max_entries if self.max_entries > 0 else 100
        capacity = max(1, int(initial_capacity))
        self._diskann_vector_memmap_capacity = capacity
        self._diskann_graph_memmap_capacity = capacity

        with self._lock_file(shared=False):
            os.makedirs(os.path.dirname(os.path.abspath(vector_path)), exist_ok=True)
            if not os.path.exists(vector_path) or os.path.getsize(vector_path) == 0:
                vectors = np.memmap(vector_path, dtype='float16', mode='w+', shape=(capacity, dim))
                vectors[:] = 0
                vectors.flush()
                del vectors
            if not os.path.exists(graph_path) or os.path.getsize(graph_path) == 0:
                graph = np.memmap(
                    graph_path,
                    dtype='uint32',
                    mode='w+',
                    shape=(capacity, self._diskann_graph_degree),
                )
                graph[:] = self._diskann_empty_neighbor
                graph.flush()
                del graph

            self._diskann_vector_memmap = np.memmap(
                vector_path,
                dtype='float16',
                mode='r+',
                shape=(capacity, dim),
            )
            self._diskann_graph_memmap = np.memmap(
                graph_path,
                dtype='uint32',
                mode='r+',
                shape=(capacity, self._diskann_graph_degree),
            )

    def _open_diskann_memmaps(self, dim: int) -> bool:
        """Open existing DiskANN-inspired sidecars for the current collection."""
        if not self._diskann_rerank_enabled:
            return False
        if (
            self._diskann_vector_memmap is not None
            and self._diskann_graph_memmap is not None
            and self._diskann_centroids is not None
            and self._diskann_partition_offsets is not None
            and self._diskann_partition_indices_memmap is not None
            and self._diskann_vector_memmap_dim == dim
        ):
            return True

        config = self._load_diskann_metadata(dim)
        if config is None:
            return False

        vector_path = self._diskann_vector_memmap_path()
        graph_path = self._diskann_graph_memmap_path()
        centroids_path = self._diskann_centroids_path()
        offsets_path = self._diskann_partition_offsets_path()
        indices_path = self._diskann_partition_indices_path()
        assignments_path = self._diskann_assignments_path()
        if not all(os.path.exists(path) for path in (
            vector_path, graph_path, centroids_path, offsets_path, indices_path, assignments_path
        )):
            return False

        vector_row_bytes = dim * 2
        graph_row_bytes = self._diskann_graph_degree * 4
        index_row_bytes = 4
        try:
            vector_size = os.path.getsize(vector_path)
            graph_size = os.path.getsize(graph_path)
            indices_size = os.path.getsize(indices_path)
            assignments_size = os.path.getsize(assignments_path)
        except OSError:
            return False

        if (
            vector_row_bytes <= 0
            or graph_row_bytes <= 0
            or vector_size <= 0
            or graph_size <= 0
            or vector_size % vector_row_bytes != 0
            or graph_size % graph_row_bytes != 0
            or indices_size != len(self._doc_ids) * index_row_bytes
            or assignments_size != len(self._doc_ids) * index_row_bytes
        ):
            logger.warning("DiskANN sidecar size mismatch. Falling back to exact search.")
            return False

        vector_capacity = vector_size // vector_row_bytes
        graph_capacity = graph_size // graph_row_bytes
        capacity = min(vector_capacity, graph_capacity)
        if capacity < len(self._doc_ids):
            logger.warning("DiskANN sidecar is undersized. Falling back to exact search.")
            return False

        self._diskann_vector_memmap_dim = dim
        self._diskann_vector_memmap_capacity = capacity
        self._diskann_graph_memmap_capacity = capacity
        self._diskann_vector_memmap = np.memmap(
            vector_path,
            dtype='float16',
            mode='r+',
            shape=(vector_capacity, dim),
        )
        self._diskann_graph_memmap = np.memmap(
            graph_path,
            dtype='uint32',
            mode='r+',
            shape=(graph_capacity, self._diskann_graph_degree),
        )
        try:
            centroids = np.load(centroids_path, allow_pickle=False)
            offsets = np.load(offsets_path, allow_pickle=False)
        except Exception:
            return False
        partition_count = int(config["partition_count"])
        if centroids.shape != (partition_count, dim) or offsets.shape[0] != partition_count + 1:
            return False
        self._diskann_centroids = centroids.astype(np.float32, copy=False)
        self._diskann_partition_offsets = offsets.astype(np.uint64, copy=False)
        self._diskann_partition_indices_memmap = np.memmap(
            indices_path,
            dtype='uint32',
            mode='r+',
            shape=(len(self._doc_ids),),
        )
        self._diskann_assignments_memmap = np.memmap(
            assignments_path,
            dtype='uint32',
            mode='r+',
            shape=(len(self._doc_ids),),
        )
        self._diskann_sidecar_meta = config
        self._diskann_partition_count = partition_count
        self._diskann_probe_count = int(config["probe_count"])
        self._diskann_rerank_candidate_cap = int(config["candidate_cap"])
        return True

    def _grow_diskann_memmaps(self):
        if (
            self._diskann_vector_memmap is None
            or self._diskann_graph_memmap is None
            or self._diskann_vector_memmap_dim is None
        ):
            return
        with self._lock_file(shared=False):
            old_capacity = self._diskann_vector_memmap_capacity
            new_capacity = old_capacity * 2
            vector_path = self._diskann_vector_memmap_path()
            graph_path = self._diskann_graph_memmap_path()
            self._diskann_vector_memmap.flush()
            self._diskann_graph_memmap.flush()
            del self._diskann_vector_memmap
            del self._diskann_graph_memmap
            self._diskann_vector_memmap = None
            self._diskann_graph_memmap = None
            with open(vector_path, "r+b") as f:
                f.truncate(new_capacity * self._diskann_vector_memmap_dim * 2)
            with open(graph_path, "r+b") as f:
                f.truncate(new_capacity * self._diskann_graph_degree * 4)
            self._diskann_vector_memmap_capacity = new_capacity
            self._diskann_graph_memmap_capacity = new_capacity
            self._diskann_vector_memmap = np.memmap(
                vector_path,
                dtype='float16',
                mode='r+',
                shape=(new_capacity, self._diskann_vector_memmap_dim),
            )
            self._diskann_graph_memmap = np.memmap(
                graph_path,
                dtype='uint32',
                mode='r+',
                shape=(new_capacity, self._diskann_graph_degree),
            )
            self._diskann_vector_memmap[old_capacity:new_capacity] = 0
            self._diskann_graph_memmap[old_capacity:new_capacity] = self._diskann_empty_neighbor
            self._diskann_vector_memmap.flush()
            self._diskann_graph_memmap.flush()

    @staticmethod
    def _diskann_encode_vector(vec: torch.Tensor) -> np.ndarray:
        """Encode a dense vector as an fp16 unit vector for disk-native scans."""
        flat = vec.detach().cpu().view(-1).float()
        norm = torch.linalg.norm(flat)
        if norm.item() <= 1e-8:
            return np.zeros(flat.shape[0], dtype=np.float16)
        return (flat / norm).numpy().astype(np.float16, copy=False)

    def _set_diskann_vector_at(self, idx: int, vec: torch.Tensor):
        if not self._diskann_rerank_enabled:
            return
        dim = vec.view(-1).shape[0]
        if self._diskann_vector_memmap is None or self._diskann_graph_memmap is None:
            self._init_diskann_memmaps(dim)
        if self._diskann_vector_memmap_dim != dim:
            logger.warning("DiskANN sidecar dimension mismatch. Skipping sidecar write.")
            return
        while idx >= self._diskann_vector_memmap_capacity:
            self._grow_diskann_memmaps()
        np_vec = self._diskann_encode_vector(vec).reshape(-1)
        with self._lock_file(shared=False):
            self._diskann_vector_memmap[idx] = np_vec
            self._diskann_vector_memmap.flush()

    def _set_diskann_neighbors_at(self, idx: int, neighbors: List[int]):
        if not self._diskann_rerank_enabled or self._diskann_graph_memmap is None:
            return
        row = np.full(self._diskann_graph_degree, self._diskann_empty_neighbor, dtype=np.uint32)
        trimmed = [int(n) for n in neighbors[:self._diskann_graph_degree] if n >= 0]
        if trimmed:
            row[:len(trimmed)] = np.asarray(trimmed, dtype=np.uint32)
        self._diskann_graph_memmap[idx] = row

    def _refresh_diskann_node(self, idx: int):
        """Connect one node to nearest earlier nodes and opportunistically add reciprocals."""
        if (
            not self._diskann_rerank_enabled
            or self._diskann_vector_memmap is None
            or self._diskann_graph_memmap is None
            or idx < 0
        ):
            return
        if idx == 0:
            with self._lock_file(shared=False):
                self._set_diskann_neighbors_at(idx, [])
                self._diskann_graph_memmap.flush()
            return

        with self._lock_file(shared=False):
            query = np.asarray(self._diskann_vector_memmap[idx], dtype=np.float32)
            previous = np.asarray(self._diskann_vector_memmap[:idx], dtype=np.float32)
            if previous.size == 0 or query.shape[0] != previous.shape[1]:
                self._set_diskann_neighbors_at(idx, [])
                self._diskann_graph_memmap.flush()
                return
            scores = previous @ query
            degree = min(self._diskann_graph_degree, idx)
            candidate_idx = np.argsort(scores, kind='stable')[::-1][:degree]
            neighbors = [int(n) for n in candidate_idx]
            self._set_diskann_neighbors_at(idx, neighbors)

            for neighbor in neighbors:
                row = np.asarray(self._diskann_graph_memmap[neighbor], dtype=np.uint32).copy()
                if np.uint32(idx) in row:
                    continue
                empty_slots = np.where(row == self._diskann_empty_neighbor)[0]
                if empty_slots.size:
                    row[int(empty_slots[0])] = np.uint32(idx)
                    self._diskann_graph_memmap[neighbor] = row

            self._diskann_graph_memmap.flush()

    def _rebuild_diskann_sidecars(self):
        """Rebuild dense fp16 vectors and graph sidecars from canonical stored vectors."""
        self.build_diskann_sidecars()

    def build_diskann_sidecars(
        self,
        partition_count: Optional[int] = None,
        probe_count: Optional[int] = None,
        candidate_limit: Optional[int] = None,
        chunk_size: int = 4096,
        kmeans_iterations: int = 1,
    ) -> Dict[str, Any]:
        """Bulk-build DiskANN-inspired sidecars with bounded memory."""
        if not self._diskann_rerank_enabled:
            return {}
        count = len(self._doc_ids)
        if count == 0:
            self._close_diskann_memmaps()
            return {}

        dim = self._get_vector_at(0).view(-1).shape[0]
        partitions = min(partition_count or self._diskann_target_partition_count(count), count)
        probes = min(probe_count or self._diskann_probe_count, max(1, partitions))
        candidate_cap = candidate_limit or self._diskann_rerank_candidate_cap
        capacity = max(self.max_entries if self.max_entries > 0 else 100, count)
        vector_path = self._diskann_vector_memmap_path()
        graph_path = self._diskann_graph_memmap_path()
        indices_path = self._diskann_partition_indices_path()
        assignments_path = self._diskann_assignments_path()
        centroids_path = self._diskann_centroids_path()
        offsets_path = self._diskann_partition_offsets_path()
        self._close_diskann_memmaps()
        for path in (vector_path, graph_path, indices_path, assignments_path, centroids_path, offsets_path, self._diskann_metadata_path()):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self._init_diskann_memmaps(dim, initial_capacity=capacity)

        for start in range(0, count, chunk_size):
            end = min(start + chunk_size, count)
            rows = [self._diskann_encode_vector(self._get_vector_at(idx)) for idx in range(start, end)]
            self._diskann_vector_memmap[start:end] = np.asarray(rows, dtype=np.float16)
        self._diskann_vector_memmap.flush()

        seed_indices = np.linspace(0, count - 1, num=partitions, dtype=np.int64)
        centroids = np.asarray(self._diskann_vector_memmap[seed_indices], dtype=np.float32)
        centroid_norms = np.linalg.norm(centroids, axis=1, keepdims=True)
        centroid_norms = np.where(centroid_norms == 0, 1.0, centroid_norms)
        centroids = centroids / centroid_norms

        assignments = np.memmap(assignments_path, dtype='uint32', mode='w+', shape=(count,))
        for _ in range(max(1, int(kmeans_iterations))):
            sums = np.zeros((partitions, dim), dtype=np.float64)
            counts = np.zeros(partitions, dtype=np.int64)
            for start in range(0, count, chunk_size):
                end = min(start + chunk_size, count)
                rows = np.asarray(self._diskann_vector_memmap[start:end], dtype=np.float32)
                scores = rows @ centroids.T
                labels = np.argmax(scores, axis=1).astype(np.uint32, copy=False)
                assignments[start:end] = labels
                for part in np.unique(labels):
                    mask = labels == part
                    sums[int(part)] += rows[mask].sum(axis=0, dtype=np.float64)
                    counts[int(part)] += int(mask.sum())
            non_empty = counts > 0
            if np.any(non_empty):
                centroids[non_empty] = (sums[non_empty] / counts[non_empty, None]).astype(np.float32)
                centroid_norms = np.linalg.norm(centroids, axis=1, keepdims=True)
                centroid_norms = np.where(centroid_norms == 0, 1.0, centroid_norms)
                centroids = centroids / centroid_norms
        assignments.flush()

        assignment_values = np.asarray(assignments[:count], dtype=np.uint32)
        sorted_indices = np.argsort(assignment_values, kind='stable').astype(np.uint32, copy=False)
        partition_counts = np.bincount(assignment_values, minlength=partitions).astype(np.uint64)
        offsets = np.zeros(partitions + 1, dtype=np.uint64)
        offsets[1:] = np.cumsum(partition_counts, dtype=np.uint64)
        partition_indices = np.memmap(indices_path, dtype='uint32', mode='w+', shape=(count,))
        partition_indices[:] = sorted_indices
        partition_indices.flush()

        np.save(centroids_path, centroids.astype(np.float32, copy=False), allow_pickle=False)
        np.save(offsets_path, offsets, allow_pickle=False)

        with self._lock_file(shared=False):
            self._diskann_graph_memmap[:] = self._diskann_empty_neighbor
            half = max(1, self._diskann_graph_degree // 2)
            for part in range(partitions):
                start = int(offsets[part])
                end = int(offsets[part + 1])
                ids = sorted_indices[start:end]
                size = end - start
                if size <= 1:
                    continue
                for local_pos, global_idx in enumerate(ids):
                    left = max(0, local_pos - half)
                    right = min(size, local_pos + half + 1)
                    neighbors = [int(idx) for idx in ids[left:right] if int(idx) != int(global_idx)]
                    self._set_diskann_neighbors_at(int(global_idx), neighbors)
            self._diskann_graph_memmap.flush()

        config = self._diskann_config_dict(
            dim=dim,
            count=count,
            partition_count=partitions,
            probe_count=probes,
            candidate_limit=candidate_cap,
        )
        self._write_diskann_metadata(config)
        self._close_diskann_memmaps()
        self._open_diskann_memmaps(dim)
        self._diskann_sidecars_dirty = False
        return dict(config)

    def _set_vector_at(self, idx: int, vec: torch.Tensor, flush: bool = True):
        """Write a PyTorch tensor to the memmap at a given row index, growing capacity if needed."""
        if idx < 0:
            raise IndexError("Index must be non-negative.")
        dim = vec.view(-1).shape[0]
        if self._memmap is None:
            self._init_memmap(dim)
            
        while idx >= self._memmap_capacity:
            self._grow_memmap()
            
        if self._store_drosophila:
            np_vec = vec.detach().cpu().numpy().astype(np.uint8).reshape(-1)
        else:
            np_vec = vec.detach().cpu().float().numpy().reshape(-1)
        
        with self._lock_file(shared=False):
            self._ensure_memmap()
            self._memmap[idx] = np_vec
            if flush:
                try:
                    self._memmap.flush()
                except OSError as e:
                    # Raise IOError on write failure (e.g. disk full)
                    raise IOError(f"Failed to flush vector data to disk (disk full?): {e}")

    def _get_vector_at(self, idx: int) -> torch.Tensor:
        """Copy the vector at a row index from the memmap and wrap it as a PyTorch tensor."""
        if idx < 0:
            raise IndexError("Index must be non-negative.")
        if self._memmap is None:
            raise ValueError("Memmap is not initialized.")
        if idx >= self._memmap_capacity:
            raise IndexError(f"Index {idx} out of range for capacity {self._memmap_capacity}.")
            
        dtype = np.uint8 if self._store_drosophila else np.float32
        
        with self._lock_file(shared=True):
            self._ensure_memmap()
            # Copy to avoid holding a view on the memmap
            vec_np = np.array(self._memmap[idx], dtype=dtype)
            
        return torch.from_numpy(vec_np)


    @contextmanager
    def _connect(self):
        """Enforce PRAGMA secure_delete = FAST on all SQLite connections."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA secure_delete = FAST;")
            conn.execute("PRAGMA journal_mode = WAL;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    text_hash TEXT,
                    document TEXT,
                    vector_blob BLOB NOT NULL,
                    metadata_json TEXT DEFAULT '{}',
                    entropy REAL DEFAULT 0.0,
                    collection TEXT NOT NULL DEFAULT 'default',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(doc_id, collection)
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_collection ON vectors(collection)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_doc_id ON vectors(doc_id)')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS collection_revision (
                    name TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS deleted_ids (
                    collection TEXT NOT NULL, doc_id TEXT NOT NULL,
                    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (collection, doc_id)
                )
            ''')
            conn.execute('''
                CREATE TRIGGER IF NOT EXISTS vectors_reject_deleted_id
                BEFORE INSERT ON vectors
                WHEN EXISTS (SELECT 1 FROM deleted_ids
                             WHERE collection = NEW.collection AND doc_id = NEW.doc_id)
                BEGIN SELECT RAISE(ABORT, 'document ID was deleted'); END
            ''')
            for name, operation, reference in (
                ("insert", "INSERT", "NEW"),
                ("update", "UPDATE OF document, vector_blob, metadata_json, created_at", "NEW"),
                ("delete", "DELETE", "OLD"),
            ):
                conn.execute(f'''
                    CREATE TRIGGER IF NOT EXISTS vectors_revision_{name}
                    AFTER {operation} ON vectors
                    BEGIN
                        INSERT INTO collection_revision (name, revision)
                        VALUES ({reference}.collection, 1)
                        ON CONFLICT(name) DO UPDATE SET revision = revision + 1;
                    END
                ''')
            conn.execute('''
                CREATE TRIGGER IF NOT EXISTS vectors_remember_deleted_id
                AFTER DELETE ON vectors
                BEGIN
                    INSERT OR IGNORE INTO deleted_ids (collection, doc_id)
                    VALUES (OLD.collection, OLD.doc_id);
                END
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS collection_meta (
                    name TEXT PRIMARY KEY,
                    embedding_dim INTEGER,
                    embedding_model TEXT,
                    encrypted_key_blob TEXT,
                    vector_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS sparse_index (
                    doc_id TEXT NOT NULL,
                    term TEXT NOT NULL,
                    frequency INTEGER NOT NULL,
                    collection TEXT NOT NULL DEFAULT 'default',
                    PRIMARY KEY(doc_id, term, collection)
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_sparse_term ON sparse_index(term, collection)')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS doc_lengths (
                    doc_id TEXT NOT NULL,
                    length INTEGER NOT NULL,
                    collection TEXT NOT NULL DEFAULT 'default',
                    PRIMARY KEY(doc_id, collection)
                )
            ''')
            # Schema migration: add encrypted_key_blob if missing (existing DBs)
            try:
                conn.execute('ALTER TABLE collection_meta ADD COLUMN encrypted_key_blob TEXT')
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute('ALTER TABLE collection_meta ADD COLUMN holographic_state BLOB')
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute('ALTER TABLE collection_meta ADD COLUMN embedding_model TEXT')
            except sqlite3.OperationalError:
                pass  # Column already exists
            conn.commit()

    def revision(self) -> int:
        """Current committed row revision for this collection."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision FROM collection_revision WHERE name = ?", (self.collection,)
            ).fetchone()
        return int(row[0]) if row else 0

    def get_records(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Read canonical rows by ID, preserving input order and omitting missing IDs."""
        if len(ids) > 500:
            raise ValueError("get_records accepts at most 500 IDs")
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT doc_id, document, metadata_json, collection, created_at, last_accessed "
                f"FROM vectors WHERE collection = ? AND doc_id IN ({marks})",
                [self.collection, *ids],
            ).fetchall()
        found = {row[0]: dict(zip(
            ("doc_id", "document", "metadata_json", "collection", "created_at", "last_accessed"), row
        )) for row in rows}
        return [found[doc_id] for doc_id in ids if doc_id in found]

    def scan_records(self, *, after_row_id: int = 0, limit: int = 500) -> tuple[List[Dict[str, Any]], int | None]:
        """Page canonical rows in stable insertion order. Caller rechecks scope."""
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("scan_records limit must be between 1 and 500")
        if not isinstance(after_row_id, int) or after_row_id < 0:
            raise ValueError("after_row_id must be a nonnegative integer")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, doc_id, document, metadata_json, collection, created_at, last_accessed "
                "FROM vectors WHERE collection = ? AND id > ? ORDER BY id LIMIT ?",
                (self.collection, after_row_id, limit),
            ).fetchall()
        records = [dict(zip(
            ("row_id", "doc_id", "document", "metadata_json", "collection", "created_at", "last_accessed"), row
        )) for row in rows]
        return records, (int(rows[-1][0]) if len(rows) == limit else None)

    def list_deleted_ids(self, *, after_id: str = "", limit: int = 500) -> tuple[List[str], str | None]:
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("list_deleted_ids limit must be between 1 and 500")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id FROM deleted_ids WHERE collection = ? AND doc_id > ? "
                "ORDER BY doc_id LIMIT ?", (self.collection, after_id, limit)
            ).fetchall()
        ids = [row[0] for row in rows]
        return ids, (ids[-1] if len(ids) == limit else None)

    def reject_deleted_ids(self, ids: List[str]) -> None:
        """Fail before changing sidecars when a caller retries a forgotten ID."""
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            if not batch:
                continue
            marks = ",".join("?" for _ in batch)
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT doc_id FROM deleted_ids WHERE collection = ? AND doc_id IN ({marks}) LIMIT 1",
                    [self.collection, *batch],
                ).fetchone()
            if row:
                raise ValueError(f"Document ID {row[0]} was deleted; use a new ID for a new capture")

    def claim_embedding_model(self, model: str, dim: int) -> None:
        """Prevent a collection from mixing vectors from different models."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT embedding_model, embedding_dim FROM collection_meta WHERE name = ?",
                (self.collection,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM vectors WHERE collection = ?", (self.collection,)
            ).fetchone()[0]
            if count and (not row or row[0] != model or row[1] != dim):
                raise ValueError(
                    f"Collection '{self.collection}' uses a different or unrecorded embedding model. "
                    "Rebuild its vectors before opening it with this model."
                )
            if not count:
                conn.execute(
                    """INSERT INTO collection_meta (name, embedding_model, embedding_dim)
                       VALUES (?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                           embedding_model = excluded.embedding_model,
                           embedding_dim = excluded.embedding_dim""",
                    (self.collection, model, dim),
                )

    def reject_unidentified_model(self) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT embedding_model FROM collection_meta WHERE name = ?", (self.collection,)
            ).fetchone()
        if row and row[0]:
            raise ValueError(
                f"Collection '{self.collection}' is pinned to embedding model '{row[0]}'. "
                "Pass the same embedding_model when opening it."
            )

    # ── Serialization ──────────────────────────────────────────────────────

    @staticmethod
    def _tensor_to_blob(tensor: torch.Tensor) -> bytes:
        """Convert tensor to fp16 numpy blob."""
        np_arr = tensor.detach().cpu().float().numpy().astype(np.float16)
        out = BytesIO()
        np.save(out, np_arr, allow_pickle=False)
        return out.getvalue()

    @staticmethod
    def _blob_to_tensor(blob: bytes) -> torch.Tensor:
        """Convert blob back to float32 tensor."""
        out = BytesIO(blob)
        np_arr = np.load(out, allow_pickle=False).astype(np.float32)
        return torch.from_numpy(np_arr)

    def _vector_to_blob(self, vector: torch.Tensor) -> bytes:
        if self._store_drosophila:
            if isinstance(vector, torch.Tensor):
                np_arr = vector.detach().cpu().numpy().astype(np.uint8)
            else:
                np_arr = np.asarray(vector, dtype=np.uint8)
            out = BytesIO()
            np.save(out, np_arr, allow_pickle=False)
            return out.getvalue()
        else:
            return self._tensor_to_blob(vector)

    def _blob_to_vector(self, blob: bytes) -> torch.Tensor:
        if self._store_drosophila:
            out = BytesIO(blob)
            np_arr = np.load(out, allow_pickle=False).astype(np.uint8)
            return torch.from_numpy(np_arr)
        else:
            return self._blob_to_tensor(blob)


    # ── FAISS ──────────────────────────────────────────────────────────────

    def _init_faiss_index(self, dim: int):
        """Initialize FAISS index (HNSW for large scale > 10k vectors, FlatIP for small scale)."""
        if not _FAISS_AVAILABLE:
            return
        import faiss
        if self._hnsw_rerank_enabled:
            self._faiss_index = faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT)
            try:
                self._faiss_index.hnsw.efSearch = 128
            except AttributeError:
                pass
            logger.debug("Initialized experimental FAISS IndexHNSWFlat rerank with dim=%d", dim)
        elif len(self._doc_ids) > 10000:
            self._faiss_index = faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT)
            logger.debug("Initialized FAISS IndexHNSWFlat with dim=%d", dim)
        else:
            self._faiss_index = faiss.IndexFlatIP(dim)
            logger.debug("Initialized FAISS IndexFlatIP with dim=%d", dim)
        self._faiss_dim = dim
        self._use_faiss = True

    def _add_to_faiss(self, tensor: torch.Tensor):
        """Add a single L2-normalized vector to the FAISS index."""
        if not self._use_faiss:
            return
        vec = tensor.view(1, -1).float().numpy()
        if vec.shape[1] != self._faiss_dim:
            return
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1e-8, norm)
        self._faiss_index.add(vec / norm)

    def _rebuild_faiss(self):
        """Rebuild FAISS index from scratch (after eviction, etc)."""
        if (
            not _FAISS_AVAILABLE
            or not self._doc_ids
            or self._native_hnsw_enabled
            or self._diskann_rerank_enabled
            or self._streaming_exact_enabled
            or self.engine in ("hyperbolic", "holographic")
        ):
            self._faiss_index = None
            self._use_faiss = False
            return
        dim = self._memmap_dim if self._memmap_dim is not None else self._get_vector_at(0).view(-1).shape[0]
        self._init_faiss_index(dim)
        for idx in range(len(self._doc_ids)):
            vec = self._get_vector_at(idx)
            self._add_to_faiss(vec)

    # ── Load ───────────────────────────────────────────────────────────────

    def _load_from_db(self, *, force_rebuild: bool = False):
        """Load all vectors for this collection into memory."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT doc_id, vector_blob, entropy FROM vectors WHERE collection = ? ORDER BY id",
                (self.collection,)
            )
            rows = cursor.fetchall()

        self._doc_ids = []
        self._entropies = []
        self._doc_id_set = set()

        for doc_id, blob, entropy in rows:
            if doc_id not in self._doc_id_set:
                self._doc_ids.append(doc_id)
                self._entropies.append(entropy if entropy is not None else 0.0)
                self._doc_id_set.add(doc_id)

        N = len(self._doc_ids)
        if force_rebuild and N == 0 and self._memmap is not None:
            with self._lock_file(shared=False):
                self._memmap[:] = 0
                self._memmap.flush()
        if N > 0:
            first_vec = self._blob_to_vector(rows[0][1])
            dim = first_vec.view(-1).shape[0]
            
            filepath = f"{self.db_path}_{self.collection}_vectors.bin"
            capacity = self.max_entries if self.max_entries > 0 else max(100, N)
            capacity = max(capacity, N)
            
            bytes_per_elem = 1 if self._store_drosophila else 4
            dtype = 'uint8' if self._store_drosophila else 'float32'
            row_bytes = dim * bytes_per_elem
            
            need_recreate = True
            if os.path.exists(filepath) and not force_rebuild:
                try:
                    actual_size = os.path.getsize(filepath)
                    if row_bytes > 0 and actual_size > 0 and actual_size % row_bytes == 0:
                        file_capacity = actual_size // row_bytes
                        if file_capacity >= N:
                            self._memmap_dim = dim
                            self._memmap_capacity = file_capacity
                            self._memmap = np.memmap(
                                filepath,
                                dtype=dtype,
                                mode='r+',
                                shape=(file_capacity, dim),
                            )
                            need_recreate = False
                        else:
                            logger.warning(
                                "Memmap file has capacity %d for %d rows. Recreating and self-healing.",
                                file_capacity,
                                N,
                            )
                    else:
                        logger.warning("Memmap file size mismatch. Recreating and self-healing.")
                except Exception as e:
                    logger.warning(f"Error opening memmap: {e}. Recreating.")
            
            if need_recreate:
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception:
                    pass
                self._init_memmap(dim, initial_capacity=capacity)
                for idx, (doc_id, blob, entropy) in enumerate(rows):
                    vec = self._blob_to_vector(blob)
                    self._set_vector_at(idx, vec)
                    
            if self._diskann_rerank_enabled:
                if not self._open_diskann_memmaps(dim):
                    self._rebuild_diskann_sidecars()
            if self._streaming_exact_enabled:
                self._ensure_streaming_exact_norms()
            if self._native_hnsw_enabled:
                self._open_hnswlib_index(dim)

            if (
                _FAISS_AVAILABLE
                and not self._store_drosophila
                and not self._native_hnsw_enabled
                and not self._diskann_rerank_enabled
                and not self._streaming_exact_enabled
                and self.engine not in ("hyperbolic", "holographic")
            ):
                self._init_faiss_index(dim)
                for idx in range(N):
                    vec = self._get_vector_at(idx)
                    self._add_to_faiss(vec)

        logger.debug("Loaded %d vectors for collection '%s' (FAISS: %s)",
                      len(self._doc_ids), self.collection, self._use_faiss)

    def _refresh_if_changed(self):
        """Refresh process-local vector IDs and sidecars after another writer commits."""
        current = self.revision()
        if current == getattr(self, "_observed_revision", current):
            return
        self._faiss_index = None
        self._use_faiss = False
        self._invalidate_streaming_exact_norms(remove_sidecar=True)
        if self._native_hnsw_enabled:
            self._invalidate_hnswlib_index(remove_sidecar=True)
        self._load_from_db(force_rebuild=True)
        self._observed_revision = self.revision()


    # ── Insert ─────────────────────────────────────────────────────────────

    def insert(self, doc_id: str, vector: torch.Tensor,
               document: str = "", metadata: Optional[Dict[str, Any]] = None):
        """Insert a single document vector."""
        self._refresh_if_changed()
        self.reject_deleted_ids([doc_id])
        if torch.isnan(vector).any() or torch.isinf(vector).any():
            raise ValueError("Input vector contains NaN or Inf values.")

        if doc_id in self._doc_id_set:
            return

        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM vectors WHERE doc_id = ? AND collection = ?",
                (doc_id, self.collection)
            )
            if cursor.fetchone():
                return

        if self.engine == "hyperbolic":
            from .hyperbolic import poincare_project
            vector = poincare_project(vector)

        if self.engine == "holographic":
            if self._holographic_state is None:
                dim = vector.view(-1).shape[0]
                self._load_holographic_state(dim)

        entropy = calculate_activation_entropy(vector)
        cpu_vec = vector.detach().cpu()
        text_hash = hashlib.sha256(document.encode()).hexdigest()[:16] if document else ""
        meta_json = json.dumps(metadata or {})

        if self.engine == "holographic":
            from .holographic import circular_convolution, generate_key_vector
            dim = cpu_vec.shape[0]
            k_i = generate_key_vector(doc_id, dim)
            c_i = circular_convolution(k_i, cpu_vec)
            self._holographic_state += c_i

        if self._store_drosophila:
            hasher = self._get_drosophila_hasher(cpu_vec.shape[0])
            hashed_np = hasher.hash(cpu_vec)
            store_tensor = torch.from_numpy(hashed_np)
        else:
            store_tensor = cpu_vec

        # Write to memmap first
        idx = len(self._doc_ids)
        self._set_vector_at(idx, store_tensor)
        self._invalidate_streaming_exact_norms()
        self._set_rerank_vector_at(idx, cpu_vec)
        self._set_pq_code_at(idx, cpu_vec)

        # SQLite
        blob = self._vector_to_blob(store_tensor)
        try:
            with self._connect() as conn:
                conn.execute(
                    '''INSERT INTO vectors
                       (doc_id, text_hash, document, vector_blob, metadata_json, entropy, collection)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (doc_id, text_hash, document, blob, meta_json, entropy, self.collection)
                )
                if document:
                    import re
                    from collections import Counter
                    terms = re.findall(r'\b[a-zA-Z0-9]+\b', document.lower())
                    doc_len = len(terms)
                    counts = Counter(terms)
                    conn.execute(
                        "INSERT OR REPLACE INTO doc_lengths (doc_id, length, collection) VALUES (?, ?, ?)",
                        (doc_id, doc_len, self.collection)
                    )
                    conn.executemany(
                        "INSERT OR REPLACE INTO sparse_index (doc_id, term, frequency, collection) VALUES (?, ?, ?, ?)",
                        [(doc_id, term, freq, self.collection) for term, freq in counts.items()]
                    )
                conn.commit()
        except sqlite3.IntegrityError:
            logger.debug("doc_id %s already exists in collection '%s'.", doc_id, self.collection)
            self._load_from_db(force_rebuild=True)
            self._observed_revision = self.revision()
            return

        if self.engine == "holographic":
            self._save_holographic_state()

        # Update in-memory metadata only after successful persistence
        self._doc_ids.append(doc_id)
        self._entropies.append(entropy)
        self._doc_id_set.add(doc_id)

        # FAISS
        if (
            self.engine not in ("hyperbolic", "holographic")
            and not self._store_drosophila
            and not self._native_hnsw_enabled
            and not self._diskann_rerank_enabled
            and not self._streaming_exact_enabled
        ):
            if self._faiss_index is None and _FAISS_AVAILABLE:
                dim = cpu_vec.view(-1).shape[0]
                self._init_faiss_index(dim)
            self._add_to_faiss(cpu_vec)

        if self._native_hnsw_enabled:
            self._invalidate_hnswlib_index(remove_sidecar=True)

        if self._diskann_rerank_enabled:
            self._diskann_sidecars_dirty = True
            if not self._defer_diskann_sidecar_build:
                self.build_diskann_sidecars()
                self._diskann_sidecars_dirty = False

        # LRU eviction
        if self.max_entries > 0 and len(self._doc_ids) > self.max_entries:
            self._evict_oldest()

    def insert_batch(self, doc_ids: List[str], vectors: List[torch.Tensor],
                     documents: Optional[List[str]] = None,
                     metadatas: Optional[List[Dict[str, Any]]] = None):
        """Insert multiple document vectors in a single transaction."""
        self._refresh_if_changed()
        self.reject_deleted_ids(doc_ids)
        for vector in vectors:
            if torch.isnan(vector).any() or torch.isinf(vector).any():
                raise ValueError("Input vector contains NaN or Inf values.")

        documents = documents or [""] * len(doc_ids)
        metadatas = metadatas or [{}] * len(doc_ids)

        if self.engine == "hyperbolic":
            from .hyperbolic import poincare_project
            vectors = [poincare_project(v) for v in vectors]

        if self.engine == "holographic" and self._holographic_state is None:
            if vectors:
                dim = vectors[0].view(-1).shape[0]
                self._load_holographic_state(dim)

        # Check SQLite to see which of the candidate doc_ids already exist in the database.
        existing_in_db = set()
        if doc_ids:
            with self._connect() as conn:
                placeholders = ",".join("?" * len(doc_ids))
                cursor = conn.execute(
                    f"SELECT doc_id FROM vectors WHERE doc_id IN ({placeholders}) AND collection = ?",
                    doc_ids + [self.collection]
                )
                existing_in_db = {r[0] for r in cursor.fetchall()}

        rows = []
        wrote_vector_memmap = False
        prepared_indices = []
        prepared_vectors = []
        prepared_doc_ids = []
        prepared_entropies = []
        
        current_idx = len(self._doc_ids)
        
        for doc_id, vector, document, metadata in zip(doc_ids, vectors, documents, metadatas):
            if doc_id in self._doc_id_set or doc_id in existing_in_db:
                continue

            entropy = calculate_activation_entropy(vector)
            cpu_vec = vector.detach().cpu()
            text_hash = hashlib.sha256(document.encode()).hexdigest()[:16] if document else ""
            meta_json = json.dumps(metadata)

            if self.engine == "holographic":
                from .holographic import circular_convolution, generate_key_vector
                dim = cpu_vec.shape[0]
                k_i = generate_key_vector(doc_id, dim)
                c_i = circular_convolution(k_i, cpu_vec)
                self._holographic_state += c_i

            if self._store_drosophila:
                hasher = self._get_drosophila_hasher(cpu_vec.shape[0])
                hashed_np = hasher.hash(cpu_vec)
                store_tensor = torch.from_numpy(hashed_np)
            else:
                store_tensor = cpu_vec

            # Write to memmap
            self._set_vector_at(current_idx, store_tensor, flush=False)
            wrote_vector_memmap = True
            self._invalidate_streaming_exact_norms()
            self._set_rerank_vector_at(current_idx, cpu_vec)
            self._set_pq_code_at(current_idx, cpu_vec)
            
            prepared_indices.append(current_idx)
            prepared_vectors.append(cpu_vec)
            prepared_doc_ids.append(doc_id)
            prepared_entropies.append(entropy)
            current_idx += 1

            blob = self._vector_to_blob(store_tensor)
            rows.append((doc_id, text_hash, document, blob, meta_json, entropy, self.collection))

        if rows:
            if wrote_vector_memmap and self._memmap is not None:
                try:
                    self._memmap.flush()
                except OSError as e:
                    raise IOError(f"Failed to flush vector data to disk (disk full?): {e}")

            try:
                with self._connect() as conn:
                    conn.executemany(
                        '''INSERT OR IGNORE INTO vectors
                           (doc_id, text_hash, document, vector_blob, metadata_json, entropy, collection)
                           VALUES (?, ?, ?, ?, ?, ?, ?)''',
                        rows
                    )
                    
                    # Compute sparse term frequency and document length for all inserted documents
                    sparse_rows = []
                    len_rows = []
                    import re
                    from collections import Counter
                    for doc_id, _, document, _, _, _, _ in rows:
                        if document:
                            terms = re.findall(r'\b[a-zA-Z0-9]+\b', document.lower())
                            doc_len = len(terms)
                            counts = Counter(terms)
                            len_rows.append((doc_id, doc_len, self.collection))
                            for term, freq in counts.items():
                                sparse_rows.append((doc_id, term, freq, self.collection))
                    
                    if len_rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO doc_lengths (doc_id, length, collection) VALUES (?, ?, ?)",
                            len_rows
                        )
                    if sparse_rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO sparse_index (doc_id, term, frequency, collection) VALUES (?, ?, ?, ?)",
                            sparse_rows
                        )
                    conn.commit()
            except Exception as e:
                logger.error("Batch insert failed: %s", e)
                try:
                    self._load_from_db(force_rebuild=True)
                    self._observed_revision = self.revision()
                except Exception:
                    logger.exception("Could not rebuild sidecar after failed insert")
                raise e

            if self.engine == "holographic":
                self._save_holographic_state()

            # Update in-memory and FAISS
            for doc_id, cpu_vec, entropy in zip(prepared_doc_ids, prepared_vectors, prepared_entropies):
                self._doc_ids.append(doc_id)
                self._entropies.append(entropy)
                self._doc_id_set.add(doc_id)

                if (
                    self.engine not in ("hyperbolic", "holographic")
                    and not self._store_drosophila
                    and not self._native_hnsw_enabled
                    and not self._diskann_rerank_enabled
                    and not self._streaming_exact_enabled
                ):
                    if self._faiss_index is None and _FAISS_AVAILABLE:
                        dim = cpu_vec.view(-1).shape[0]
                        self._init_faiss_index(dim)
                    self._add_to_faiss(cpu_vec)

            if self._native_hnsw_enabled and prepared_doc_ids:
                self._invalidate_hnswlib_index(remove_sidecar=True)

            if self._diskann_rerank_enabled:
                self._diskann_sidecars_dirty = True
            if self._diskann_rerank_enabled and not self._defer_diskann_sidecar_build:
                self.build_diskann_sidecars()
                self._diskann_sidecars_dirty = False

        # Eviction
        if self.max_entries > 0 and len(self._doc_ids) > self.max_entries:
            self._evict_oldest()


    # ── Search ─────────────────────────────────────────────────────────────

    def search(self, query_vector: torch.Tensor, n_results: int = 10,
               where: Optional[Dict[str, Any]] = None,
               candidate_ids: Optional[List[str]] = None,
               temperature: float = 0.0,
               query_text: Optional[str] = None) -> SearchResult:
        """
        Search for nearest vectors by cosine similarity or Hamming distance.
        Optionally filter by metadata via `where` dict.
        """
        self._refresh_if_changed()
        if n_results <= 0:
            raise ValueError("n_results must be strictly positive (greater than 0).")
        if torch.isnan(query_vector).any() or torch.isinf(query_vector).any():
            raise ValueError("Query vector contains NaN or Inf values.")

        if not isinstance(temperature, (int, float)):
            raise TypeError("temperature must be a float or int")
        if not (0.0 <= temperature <= 1.0):
            raise ValueError("temperature must be between 0.0 and 1.0")

        if not self._doc_ids:
            return SearchResult()

        if query_text is not None:
            # 1. Get dense search results (requesting more candidates to improve RRF)
            dense_res = self.search(
                query_vector=query_vector,
                n_results=max(n_results * 2, 50),
                where=where,
                candidate_ids=candidate_ids,
                temperature=temperature,
                query_text=None
            )
            # 2. Get BM25 sparse search results
            sparse_scores = self._search_bm25(query_text, candidate_ids, where)
            # Rank sparse results
            sparse_ranked = sorted(sparse_scores.keys(), key=lambda k: sparse_scores[k], reverse=True)
            
            # 3. Perform Reciprocal Rank Fusion (RRF)
            dense_ranked = dense_res.ids
            
            # Combine the doc_ids and calculate RRF scores
            k_rrf = 60.0
            rrf_scores = {}
            for rank, doc_id in enumerate(dense_ranked):
                rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k_rrf + float(rank + 1))
            for rank, doc_id in enumerate(sparse_ranked):
                rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k_rrf + float(rank + 1))
                
            # Sort all unique docs by descending RRF score
            sorted_docs = sorted(rrf_scores.keys(), key=lambda d: rrf_scores[d], reverse=True)
            top_k_docs = sorted_docs[:n_results]
            
            # 4. Construct final SearchResult
            result = SearchResult(
                ids=top_k_docs,
                scores=[rrf_scores[did] for did in top_k_docs],
                distances=[1.0 - rrf_scores[did] for did in top_k_docs],
                served_locally=dense_res.served_locally
            )
            self._hydrate_results(result)
            return result

        candidate_indices = None
        if candidate_ids is not None:
            candidate_indices = []
            for cid in candidate_ids:
                if cid in self._doc_id_set:
                    candidate_indices.append(self._doc_ids.index(cid))
        
        if where:
            metadata_indices = self._filter_by_metadata(where)
            if candidate_indices is not None:
                candidate_indices = list(set(candidate_indices) & set(metadata_indices))
            else:
                candidate_indices = metadata_indices

        # If constraints are specified and no indices match, return early
        if candidate_indices is not None and not candidate_indices:
            return SearchResult()

        query_entropy = calculate_activation_entropy(query_vector)

        if self.engine == "hyperbolic":
            query_vector = query_vector.detach().cpu()
            from .hyperbolic import poincare_project, poincare_distance
            
            # Thermodynamic search: perturbs query coordinates
            if temperature > 0.0:
                query_vector = query_vector.clone()
                noise = torch.randn_like(query_vector) * (0.05 * temperature)
                query_vector = query_vector + noise
            
            q_proj = poincare_project(query_vector)
            
            if self.drosophila_hash:
                return self._search_drosophila(q_proj, n_results, query_entropy, candidate_indices, temperature=temperature)
            
            indices_to_search = candidate_indices if candidate_indices is not None else range(len(self._doc_ids))
            if not indices_to_search:
                return SearchResult()
                
            stored_vectors = torch.stack([self._get_vector_at(idx) for idx in indices_to_search])
            distances = poincare_distance(q_proj, stored_vectors)
            
            sorted_distances, sorted_sub_indices = torch.sort(distances, descending=False)
            top_k_sub_indices = sorted_sub_indices[:n_results]
            top_k_distances = sorted_distances[:n_results]
            
            res_ids = []
            res_scores = []
            res_distances = []
            for sub_idx, dist in zip(top_k_sub_indices, top_k_distances):
                actual_idx = list(indices_to_search)[sub_idx.item()]
                doc_id = self._doc_ids[actual_idx]
                res_ids.append(doc_id)
                d_val = dist.item()
                res_distances.append(d_val)
                res_scores.append(1.0 / (1.0 + d_val))
                
            result = SearchResult(
                ids=res_ids,
                scores=res_scores,
                distances=res_distances,
                served_locally=True
            )
            self._hydrate_results(result)
            return result

        if self.engine == "holographic":
            query_vector = query_vector.detach().cpu()
            if self._holographic_state is None:
                first_vec = self._get_vector_at(0)
                dim = first_vec.view(-1).shape[0]
                self._load_holographic_state(dim)
                
            from .holographic import circular_correlation, generate_key_vector
            indices_to_search = candidate_indices if candidate_indices is not None else range(len(self._doc_ids))
            if not indices_to_search:
                return SearchResult()
                
            # Thermodynamic perturbation if not using drosophila hash
            if not self.drosophila_hash and temperature > 0.0:
                query_vector = query_vector.clone()
                noise = torch.randn_like(query_vector) * (0.05 * temperature)
                query_vector = query_vector + noise
                
            reconstructed_vectors = []
            doc_ids_to_search = [self._doc_ids[idx] for idx in indices_to_search]
            dim = self._holographic_state.shape[-1]
            for doc_id in doc_ids_to_search:
                k_i = generate_key_vector(doc_id, dim)
                v_i_prime = circular_correlation(self._holographic_state, k_i)
                reconstructed_vectors.append(v_i_prime)
                
            reconstructed_tensor = torch.stack(reconstructed_vectors)
            
            if self.drosophila_hash:
                from .drosophila import compute_hamming_distance
                hasher = self._get_drosophila_hasher(dim)
                hashed_query = hasher.hash(query_vector)
                
                if temperature > 0.0:
                    bits = np.unpackbits(hashed_query)
                    p = 0.5 * temperature
                    flip_mask = np.random.random(len(bits)) < p
                    bits = bits ^ flip_mask.astype(np.uint8)
                    hashed_query = np.packbits(bits)
                    
                reconstructed_hashes = [hasher.hash(v_p) for v_p in reconstructed_vectors]
                stored_vectors = np.array(reconstructed_hashes, dtype=np.uint8)
                
                distances = compute_hamming_distance(stored_vectors, hashed_query)
                sorted_local_idx = np.argsort(distances, kind='stable')
                
                result = SearchResult()
                for local_idx in sorted_local_idx:
                    if len(result.ids) >= n_results:
                        break
                    global_idx = list(indices_to_search)[local_idx]
                    dist_val = float(distances[local_idx])
                    sim_val = 1.0 - (dist_val / 10000.0)
                    
                    # Entropy proximity gate
                    cached_entropy = self._entropies[global_idx]
                    if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                        continue
                        
                    doc_id = self._doc_ids[global_idx]
                    result.ids.append(doc_id)
                    result.scores.append(sim_val)
                    result.distances.append(dist_val)
                    
                result.served_locally = True
                self._hydrate_results(result)
                return result
            else:
                q_norm = torch.norm(query_vector, p=2, dim=-1, keepdim=True)
                q_norm = torch.where(q_norm == 0, torch.ones_like(q_norm) * 1e-8, q_norm)
                q_normalized = query_vector / q_norm
                
                v_norms = torch.norm(reconstructed_tensor, p=2, dim=-1, keepdim=True)
                v_norms = torch.where(v_norms == 0, torch.ones_like(v_norms) * 1e-8, v_norms)
                v_normalized = reconstructed_tensor / v_norms
                
                similarities = torch.mv(v_normalized, q_normalized)
                sorted_similarities, sorted_sub_indices = torch.sort(similarities, descending=True)
                
                result = SearchResult()
                for sub_idx, sim in zip(sorted_sub_indices, sorted_similarities):
                    if len(result.ids) >= n_results:
                        break
                    global_idx = list(indices_to_search)[sub_idx.item()]
                    
                    # Entropy proximity gate
                    cached_entropy = self._entropies[global_idx]
                    if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                        continue
                        
                    doc_id = self._doc_ids[global_idx]
                    result.ids.append(doc_id)
                    result.scores.append(float(sim))
                    result.distances.append(1.0 - float(sim))
                    
                result.served_locally = True
                self._hydrate_results(result)
                return result

        # Coordinate perturbation for brute-force/FAISS when Drosophila is disabled
        if temperature > 0.0 and not self.drosophila_hash:
            query_vector = query_vector.clone()
            noise = torch.randn_like(query_vector) * (0.05 * temperature)
            query_vector = query_vector + noise
            norm = query_vector.norm()
            if norm >= 1.0:
                query_vector = query_vector / (norm + 1e-5)

        if self.drosophila_hash:
            return self._search_drosophila(query_vector, n_results, query_entropy, candidate_indices, temperature=temperature)

        if self._streaming_exact_enabled:
            return self._search_streaming_exact(
                query_vector,
                n_results,
                query_entropy,
                candidate_indices,
                temperature=temperature,
            )

        if self._diskann_rerank_enabled:
            reranked = self._search_diskann_rerank(
                query_vector=query_vector,
                n_results=n_results,
                query_entropy=query_entropy,
                candidate_indices=candidate_indices,
                temperature=temperature,
            )
            if reranked is not None:
                return reranked
            return self._search_bruteforce(
                query_vector,
                n_results,
                query_entropy,
                candidate_indices,
                temperature=temperature,
            )

        if self._pq_rerank_enabled:
            reranked = self._search_pq_rerank(
                query_vector=query_vector,
                n_results=n_results,
                query_entropy=query_entropy,
                candidate_indices=candidate_indices,
                temperature=temperature,
            )
            if reranked is not None:
                return reranked

        if self._native_hnsw_enabled:
            reranked = self._search_hnswlib_rerank(
                query_vector=query_vector,
                n_results=n_results,
                query_entropy=query_entropy,
                candidate_indices=candidate_indices,
                temperature=temperature,
            )
            if reranked is not None:
                return reranked
            return self._search_bruteforce(
                query_vector,
                n_results,
                query_entropy,
                candidate_indices,
                temperature=temperature,
            )

        if self._use_faiss and candidate_indices is None:
            if self._hnsw_rerank_enabled:
                return self._search_faiss_rerank(query_vector, n_results, query_entropy,
                                                 temperature=temperature)
            return self._search_faiss(query_vector, n_results, query_entropy, temperature=temperature)
        else:
            return self._search_bruteforce(query_vector, n_results, query_entropy,
                                           candidate_indices, temperature=temperature)

    def _search_drosophila(self, query_vector: torch.Tensor, n_results: int,
                           query_entropy: float,
                           candidate_indices: Optional[List[int]] = None,
                           temperature: float = 0.0) -> SearchResult:
        """Search using Drosophila hashing and Hamming distance."""
        # 1. Hash the query vector
        hasher = self._get_drosophila_hasher(query_vector.shape[0])
        hashed_query = hasher.hash(query_vector) # uint8 numpy array of shape (1250,)

        if temperature > 0.0:
            bits = np.unpackbits(hashed_query)
            p = 0.5 * temperature
            flip_mask = np.random.random(len(bits)) < p
            bits = bits ^ flip_mask.astype(np.uint8)
            hashed_query = np.packbits(bits)

        # 2. Determine indices to search
        if candidate_indices is not None:
            indices_to_search = candidate_indices
        else:
            indices_to_search = list(range(len(self._doc_ids)))

        if not indices_to_search:
            return SearchResult()

        # 3. Retrieve stored vectors from memmap
        with self._lock_file(shared=True):
            self._ensure_memmap()
            if self._memmap is None:
                self._init_memmap(1250)
            # Retrieve only the required rows from memmap
            stored_vectors = np.array([self._memmap[i] for i in indices_to_search], dtype=np.uint8)

        # 4. Compute Hamming distance
        from .drosophila import compute_hamming_distance
        distances = compute_hamming_distance(stored_vectors, hashed_query) # shape (len(indices_to_search),)

        # 5. Sort by ascending distance stably
        sorted_local_idx = np.argsort(distances, kind='stable')

        if self._flyhash_rerank_enabled:
            reranked = self._search_flyhash_rerank(
                query_vector=query_vector,
                n_results=n_results,
                query_entropy=query_entropy,
                indices_to_search=list(indices_to_search),
                sorted_local_idx=sorted_local_idx,
            )
            if reranked is not None:
                return reranked

        # 6. Build results
        result = SearchResult()
        for local_idx in sorted_local_idx:
            if len(result.ids) >= n_results:
                break
            
            global_idx = indices_to_search[local_idx]
            dist_val = float(distances[local_idx])
            
            # compute scores as 1.0 - (distance / 10000.0)
            sim_val = 1.0 - (dist_val / 10000.0)

            # Entropy proximity gate
            cached_entropy = self._entropies[global_idx]
            if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                continue

            doc_id = self._doc_ids[global_idx]
            result.ids.append(doc_id)
            result.scores.append(sim_val)
            result.distances.append(dist_val)

        self._hydrate_results(result)
        return result

    def _flyhash_rerank_candidate_count(self, n_results: int, available: int) -> int:
        candidate_count = max(
            n_results * self._flyhash_rerank_candidate_multiplier,
            self._flyhash_rerank_candidate_min,
        )
        candidate_count = min(candidate_count, self._flyhash_rerank_candidate_cap)
        return min(candidate_count, available)

    def _search_flyhash_rerank(self, query_vector: torch.Tensor, n_results: int,
                               query_entropy: float,
                               indices_to_search: List[int],
                               sorted_local_idx: np.ndarray) -> Optional[SearchResult]:
        dim = query_vector.view(-1).shape[0]
        if not self._open_rerank_memmap(dim):
            return None

        candidate_limit = self._flyhash_rerank_candidate_count(n_results, len(sorted_local_idx))
        candidate_indices = []
        for local_idx in sorted_local_idx:
            if len(candidate_indices) >= candidate_limit:
                break
            global_idx = indices_to_search[int(local_idx)]
            cached_entropy = self._entropies[global_idx]
            if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                continue
            candidate_indices.append(global_idx)

        if not candidate_indices:
            return SearchResult()

        vectors = []
        valid_indices = []
        for global_idx in candidate_indices:
            vec = self._get_rerank_vector_at(global_idx, dim)
            if vec is None:
                return None
            vectors.append(vec.view(-1))
            valid_indices.append(global_idx)

        flat_query = query_vector.detach().cpu().view(1, -1).float()
        keys = torch.stack(vectors).float()
        if flat_query.shape[1] != keys.shape[1]:
            return None

        similarities = torch.nn.functional.cosine_similarity(flat_query, keys, dim=1).tolist()
        sorted_candidate_idx = sorted(
            range(len(similarities)),
            key=lambda idx: (similarities[idx], -valid_indices[idx]),
            reverse=True,
        )

        result = SearchResult()
        for local_idx in sorted_candidate_idx:
            if len(result.ids) >= n_results:
                break
            global_idx = valid_indices[local_idx]
            sim_val = float(similarities[local_idx])
            doc_id = self._doc_ids[global_idx]
            result.ids.append(doc_id)
            result.scores.append(sim_val)
            result.distances.append(1.0 - sim_val)

        self._hydrate_results(result)
        return result

    def _pq_rerank_candidate_count(self, n_results: int, available: int) -> int:
        candidate_count = max(
            n_results * self._pq_rerank_candidate_multiplier,
            self._pq_rerank_candidate_min,
        )
        candidate_count = min(candidate_count, self._pq_rerank_candidate_cap)
        return min(candidate_count, available)

    def _search_pq_rerank(self, query_vector: torch.Tensor, n_results: int,
                          query_entropy: float,
                          candidate_indices: Optional[List[int]] = None,
                          temperature: float = 0.0) -> Optional[SearchResult]:
        """Use int8 PQ-lite codes for candidates, then exact-rerank stored vectors."""
        dim = query_vector.view(-1).shape[0]
        if not self._open_pq_memmap(dim):
            return None

        indices_to_search = candidate_indices if candidate_indices is not None else list(range(len(self._doc_ids)))
        if not indices_to_search:
            return SearchResult()

        with self._lock_file(shared=True):
            codes = np.array([self._pq_memmap[i] for i in indices_to_search], dtype=np.int16)
        query_code = self._pq_encode_vector(query_vector).astype(np.int16, copy=False)
        if codes.ndim != 2 or codes.shape[1] != query_code.shape[0]:
            return None

        approx_scores = codes @ query_code
        sorted_local_idx = sorted(
            range(len(indices_to_search)),
            key=lambda idx: (int(approx_scores[idx]), -indices_to_search[idx]),
            reverse=True,
        )

        candidate_limit = self._pq_rerank_candidate_count(n_results, len(sorted_local_idx))
        exact_candidate_indices = []
        for local_idx in sorted_local_idx:
            if len(exact_candidate_indices) >= candidate_limit:
                break
            global_idx = indices_to_search[local_idx]
            cached_entropy = self._entropies[global_idx]
            if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                continue
            exact_candidate_indices.append(global_idx)

        if not exact_candidate_indices:
            return SearchResult()

        return self._search_bruteforce(
            query_vector,
            n_results,
            query_entropy,
            exact_candidate_indices,
            temperature=temperature,
        )

    def _diskann_rerank_candidate_count(self, n_results: int, available: int) -> int:
        candidate_count = max(
            n_results * self._diskann_rerank_candidate_multiplier,
            self._diskann_rerank_candidate_min,
        )
        candidate_count = min(candidate_count, self._diskann_rerank_candidate_cap)
        return min(candidate_count, available)

    def _diskann_scan_candidates(
        self,
        query_vector: torch.Tensor,
        query_entropy: float,
        indices_to_search: Sequence[int],
        candidate_limit: int,
    ) -> List[int]:
        if self._diskann_vector_memmap is None:
            return []

        query_code = self._diskann_encode_vector(query_vector).astype(np.float32, copy=False)
        if self._diskann_vector_memmap_dim != query_code.shape[0]:
            return []
        indices_array = np.asarray(indices_to_search, dtype=np.int64)
        if indices_array.size == 0:
            return []
        total = len(self._doc_ids)
        indices_array = indices_array[(indices_array >= 0) & (indices_array < total)]
        if indices_array.size == 0:
            return []
        candidate_limit = min(int(candidate_limit), int(indices_array.size))
        if candidate_limit <= 0:
            return []

        best_scores = np.empty(0, dtype=np.float32)
        best_indices = np.empty(0, dtype=np.int64)
        chunk_size = max(1024, min(65536, 4_194_304 // max(1, query_code.shape[0])))

        with self._lock_file(shared=True):
            for start in range(0, indices_array.size, chunk_size):
                chunk_indices = indices_array[start:start + chunk_size]
                if chunk_indices.size == 0:
                    continue
                if (
                    chunk_indices.size > 1
                    and int(chunk_indices[-1]) - int(chunk_indices[0]) + 1 == chunk_indices.size
                    and np.all(np.diff(chunk_indices) == 1)
                ):
                    rows_view = self._diskann_vector_memmap[int(chunk_indices[0]):int(chunk_indices[-1]) + 1]
                else:
                    rows_view = self._diskann_vector_memmap[chunk_indices]
                rows = np.asarray(rows_view, dtype=np.float32)
                if rows.ndim != 2 or rows.shape[1] != query_code.shape[0]:
                    return []

                approx_scores = rows @ query_code
                if self.entropy_tolerance < 999.0:
                    entropies = np.fromiter(
                        (self._entropies[int(idx)] for idx in chunk_indices),
                        dtype=np.float64,
                        count=chunk_indices.size,
                    )
                    keep_mask = ~(np.abs(query_entropy - entropies) > self.entropy_tolerance)
                    if not np.all(keep_mask):
                        approx_scores = approx_scores[keep_mask]
                        chunk_indices = chunk_indices[keep_mask]
                if approx_scores.size == 0:
                    continue

                take = min(candidate_limit, approx_scores.size)
                if approx_scores.size > take:
                    local_keep = np.argpartition(approx_scores, -take)[-take:]
                else:
                    local_keep = np.arange(approx_scores.size)
                chunk_scores = approx_scores[local_keep].astype(np.float32, copy=False)
                chunk_best = chunk_indices[local_keep].astype(np.int64, copy=False)

                best_scores = np.concatenate([best_scores, chunk_scores])
                best_indices = np.concatenate([best_indices, chunk_best])
                if best_scores.size > candidate_limit:
                    keep = np.argpartition(best_scores, -candidate_limit)[-candidate_limit:]
                    best_scores = best_scores[keep]
                    best_indices = best_indices[keep]

        if best_scores.size == 0:
            return []
        order = np.lexsort((best_indices, -best_scores))
        return [int(idx) for idx in best_indices[order[:candidate_limit]]]

    def _diskann_partition_candidates(
        self,
        query_vector: torch.Tensor,
        query_entropy: float,
        candidate_limit: int,
    ) -> List[int]:
        if (
            self._diskann_centroids is None
            or self._diskann_partition_offsets is None
            or self._diskann_partition_indices_memmap is None
        ):
            return []

        query_code = self._diskann_encode_vector(query_vector).astype(np.float32, copy=False)
        if self._diskann_centroids.shape[1] != query_code.shape[0]:
            return []
        centroid_scores = self._diskann_centroids @ query_code
        probe_count = min(self._diskann_probe_count, centroid_scores.shape[0])
        if probe_count <= 0:
            return []
        selected_parts = np.argsort(centroid_scores, kind='stable')[::-1][:probe_count]
        if probe_count >= centroid_scores.shape[0]:
            return self._diskann_scan_candidates(
                query_vector,
                query_entropy,
                range(len(self._doc_ids)),
                candidate_limit,
            )

        candidate_pool = []
        with self._lock_file(shared=True):
            for part in selected_parts:
                start = int(self._diskann_partition_offsets[int(part)])
                end = int(self._diskann_partition_offsets[int(part) + 1])
                if end <= start:
                    continue
                candidate_pool.extend(int(idx) for idx in self._diskann_partition_indices_memmap[start:end])

            if self._diskann_graph_memmap is not None and candidate_pool:
                seed_count = min(len(candidate_pool), max(candidate_limit, self._diskann_beam_width))
                for idx in candidate_pool[:seed_count]:
                    row = np.asarray(self._diskann_graph_memmap[idx], dtype=np.uint32)
                    for neighbor in row:
                        neighbor_idx = int(neighbor)
                        if (
                            neighbor_idx == self._diskann_empty_neighbor
                            or neighbor_idx < 0
                            or neighbor_idx >= len(self._doc_ids)
                        ):
                            continue
                        candidate_pool.append(neighbor_idx)

        if not candidate_pool:
            return []
        if len(candidate_pool) > 1:
            candidate_pool = sorted(set(candidate_pool))
        return self._diskann_scan_candidates(
            query_vector,
            query_entropy,
            candidate_pool,
            candidate_limit,
        )

    def _diskann_graph_candidates(
        self,
        query_vector: torch.Tensor,
        query_entropy: float,
        candidate_limit: int,
    ) -> List[int]:
        if (
            not self._doc_ids
            or self._diskann_vector_memmap is None
            or self._diskann_graph_memmap is None
        ):
            return []

        import heapq

        total = len(self._doc_ids)
        query_code = self._diskann_encode_vector(query_vector).astype(np.float32, copy=False)
        entry_points = sorted({0, total // 2, total - 1})
        visited = set()
        scored = {}
        heap = []

        def score_idx(idx: int) -> float:
            if idx not in scored:
                row = np.asarray(self._diskann_vector_memmap[idx], dtype=np.float32)
                if row.shape[0] != query_code.shape[0]:
                    scored[idx] = -float("inf")
                else:
                    scored[idx] = float(np.dot(row, query_code))
            return scored[idx]

        with self._lock_file(shared=True):
            for idx in entry_points:
                heapq.heappush(heap, (-score_idx(idx), idx))

            while heap and len(visited) < min(self._diskann_visit_cap, total):
                _, idx = heapq.heappop(heap)
                if idx in visited or idx < 0 or idx >= total:
                    continue
                visited.add(idx)
                row = np.asarray(self._diskann_graph_memmap[idx], dtype=np.uint32)
                pushed = 0
                for neighbor in row:
                    neighbor_idx = int(neighbor)
                    if (
                        neighbor_idx == self._diskann_empty_neighbor
                        or neighbor_idx < 0
                        or neighbor_idx >= total
                        or neighbor_idx in visited
                    ):
                        continue
                    heapq.heappush(heap, (-score_idx(neighbor_idx), neighbor_idx))
                    pushed += 1
                    if pushed >= self._diskann_beam_width:
                        break

        ranked = sorted(
            visited,
            key=lambda idx: (scored.get(idx, -float("inf")), -idx),
            reverse=True,
        )
        candidates = []
        for global_idx in ranked:
            if len(candidates) >= candidate_limit:
                break
            cached_entropy = self._entropies[global_idx]
            if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                continue
            candidates.append(global_idx)
        return candidates

    def _search_diskann_rerank(self, query_vector: torch.Tensor, n_results: int,
                               query_entropy: float,
                               candidate_indices: Optional[List[int]] = None,
                               temperature: float = 0.0) -> Optional[SearchResult]:
        """Use DiskANN-inspired SSD sidecars for candidates, then exact-rerank stored vectors."""
        dim = query_vector.view(-1).shape[0]
        if not self._open_diskann_memmaps(dim):
            self._rebuild_diskann_sidecars()
            if not self._open_diskann_memmaps(dim):
                return None

        if candidate_indices is not None:
            indices_to_search = list(candidate_indices)
            available_count = len(indices_to_search)
        else:
            indices_to_search = None
            available_count = len(self._doc_ids)
        if available_count <= 0:
            return SearchResult()

        candidate_limit = self._diskann_rerank_candidate_count(n_results, available_count)
        if candidate_limit <= 0:
            return SearchResult()

        if candidate_indices is not None:
            exact_candidate_indices = self._diskann_scan_candidates(
                query_vector,
                query_entropy,
                indices_to_search,
                candidate_limit,
            )
        elif available_count <= self._diskann_full_scan_threshold:
            exact_candidate_indices = self._diskann_scan_candidates(
                query_vector,
                query_entropy,
                range(available_count),
                candidate_limit,
            )
        else:
            exact_candidate_indices = self._diskann_partition_candidates(
                query_vector,
                query_entropy,
                candidate_limit,
            )

        if not exact_candidate_indices:
            return SearchResult()

        exact_candidate_indices = sorted(set(exact_candidate_indices))
        return self._search_bruteforce(
            query_vector,
            n_results,
            query_entropy,
            exact_candidate_indices,
            temperature=temperature,
        )


    def _search_bm25(self, query_text: str,
                     candidate_ids: Optional[List[str]] = None,
                     where: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        """
        Compute BM25 score for documents matching the query_text.
        """
        import re
        import math
        from collections import Counter

        terms = re.findall(r'\b[a-zA-Z0-9]+\b', query_text.lower())
        if not terms:
            return {}

        allowed_docs = None
        if candidate_ids is not None:
            allowed_docs = set(candidate_ids)
        if where:
            metadata_indices = self._filter_by_metadata(where)
            metadata_docs = {self._doc_ids[idx] for idx in metadata_indices}
            if allowed_docs is not None:
                allowed_docs = allowed_docs & metadata_docs
            else:
                allowed_docs = metadata_docs

        if allowed_docs is not None and not allowed_docs:
            return {}

        with self._connect() as conn:
            cursor = conn.cursor()
            
            # Get total number of documents N in the collection
            cursor.execute(
                "SELECT COUNT(*) FROM doc_lengths WHERE collection = ?",
                (self.collection,)
            )
            row = cursor.fetchone()
            N = row[0] if row else 0
            if N == 0:
                return {}

            # Get average document length avgdl
            cursor.execute(
                "SELECT AVG(length) FROM doc_lengths WHERE collection = ?",
                (self.collection,)
            )
            row = cursor.fetchone()
            avgdl = row[0] if (row and row[0] is not None) else 1.0
            if avgdl <= 0:
                avgdl = 1.0

            # Get document frequencies for the query terms
            placeholders = ",".join("?" * len(terms))
            cursor.execute(
                f"SELECT term, COUNT(DISTINCT doc_id) FROM sparse_index "
                f"WHERE term IN ({placeholders}) AND collection = ? "
                f"GROUP BY term",
                terms + [self.collection]
            )
            df = {r[0]: r[1] for r in cursor.fetchall()}

            # Get matching document term frequencies and document lengths
            cursor.execute(
                f"SELECT s.doc_id, s.term, s.frequency, l.length "
                f"FROM sparse_index s "
                f"JOIN doc_lengths l ON s.doc_id = l.doc_id AND s.collection = l.collection "
                f"WHERE s.term IN ({placeholders}) AND s.collection = ?",
                terms + [self.collection]
            )
            rows = cursor.fetchall()

        scores = {}
        k1 = 1.5
        b = 0.75

        # Calculate IDF for each term
        idf = {}
        for term in terms:
            n_t = df.get(term, 0)
            # Standard BM25 IDF formula with a floor shift to avoid negative IDFs
            val = (N - n_t + 0.5) / (n_t + 0.5)
            if val < 0.0:
                val = 0.0
            idf[term] = math.log(val + 1.0)

        for doc_id, term, freq, doc_len in rows:
            if allowed_docs is not None and doc_id not in allowed_docs:
                continue

            tf = freq
            idf_val = idf.get(term, 0.0)
            numerator = tf * (k1 + 1.0)
            denominator = tf + k1 * (1.0 - b + b * (doc_len / avgdl))
            term_score = idf_val * (numerator / denominator)

            scores[doc_id] = scores.get(doc_id, 0.0) + term_score

        return scores


    def _hnsw_rerank_candidate_count(self, n_results: int) -> int:
        """Candidate count for experimental HNSW retrieval before exact rerank."""
        if self._native_hnsw_enabled:
            return min(max(n_results, self._hnsw_rerank_candidate_cap), len(self._doc_ids))
        candidate_count = max(
            n_results * self._hnsw_rerank_candidate_multiplier,
            self._hnsw_rerank_candidate_min,
        )
        candidate_count = min(candidate_count, self._hnsw_rerank_candidate_cap)
        return min(candidate_count, len(self._doc_ids))

    def _search_hnswlib_rerank(self, query_vector: torch.Tensor, n_results: int,
                               query_entropy: float,
                               candidate_indices: Optional[List[int]] = None,
                               temperature: float = 0.0) -> Optional[SearchResult]:
        """Use native hnswlib for candidates, then exact-rerank stored vectors."""
        if candidate_indices is not None:
            return self._search_streaming_exact(
                query_vector,
                n_results,
                query_entropy,
                candidate_indices,
                temperature=temperature,
            )
        dim = query_vector.view(-1).shape[0]
        if not self._ensure_hnswlib_index(dim):
            return None

        k = self._hnsw_rerank_candidate_count(n_results)
        if k <= 0:
            return SearchResult()

        vec = query_vector.view(1, -1).float().cpu().numpy()
        try:
            labels, _ = self._hnswlib_index.knn_query(vec, k=k)
        except Exception as exc:
            logger.debug("hnswlib candidate search failed; falling back to exact search: %s", exc)
            return None

        candidate_indices = []
        seen = set()
        for idx in labels[0]:
            idx = int(idx)
            if idx < 0 or idx >= len(self._doc_ids) or idx in seen:
                continue
            candidate_indices.append(idx)
            seen.add(idx)

        if not candidate_indices:
            return SearchResult()

        candidate_indices.sort()
        return self._search_streaming_exact(
            query_vector,
            n_results,
            query_entropy,
            candidate_indices,
            temperature=temperature,
        )

    def _search_faiss_rerank(self, query_vector: torch.Tensor, n_results: int,
                             query_entropy: float,
                             temperature: float = 0.0) -> SearchResult:
        """Use FAISS HNSW for candidates, then exact-rerank stored vectors."""
        vec = query_vector.view(1, -1).float().cpu().numpy()
        if vec.shape[1] != self._faiss_dim:
            return SearchResult()

        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1e-8, norm)
        vec_normed = vec / norm

        k = self._hnsw_rerank_candidate_count(n_results)
        if k <= 0:
            return SearchResult()

        try:
            _, indices = self._faiss_index.search(vec_normed, k)
        except Exception as exc:
            logger.debug("Experimental HNSW candidate search failed; falling back to exact search: %s", exc)
            return self._search_bruteforce(query_vector, n_results, query_entropy,
                                           temperature=temperature)

        candidate_indices = []
        seen = set()
        for idx in indices[0]:
            idx = int(idx)
            if idx < 0 or idx >= len(self._doc_ids) or idx in seen:
                continue
            candidate_indices.append(idx)
            seen.add(idx)

        if not candidate_indices:
            return SearchResult()

        candidate_indices.sort()
        return self._search_bruteforce(query_vector, n_results, query_entropy,
                                       candidate_indices, temperature=temperature)

    def _search_faiss(self, query_vector: torch.Tensor, n_results: int,
                      query_entropy: float,
                      temperature: float = 0.0) -> SearchResult:
        """FAISS-accelerated search."""
        vec = query_vector.view(1, -1).float().cpu().numpy()
        if vec.shape[1] != self._faiss_dim:
            return SearchResult()

        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1e-8, norm)
        vec_normed = vec / norm

        # Request extra candidates to allow entropy filtering
        k = min(n_results * 3, len(self._doc_ids))
        distances, indices = self._faiss_index.search(vec_normed, k)

        # Stably sort by similarity descending, using index as a tie-breaker
        pairs = list(zip(distances[0], indices[0]))
        sorted_pairs = sorted(pairs, key=lambda p: (float(p[0]), -int(p[1])), reverse=True)

        result = SearchResult()
        for sim, idx in sorted_pairs:
            if idx < 0:
                continue
            if len(result.ids) >= n_results:
                break

            # Entropy proximity gate
            cached_entropy = self._entropies[idx]
            if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                continue

            doc_id = self._doc_ids[idx]
            result.ids.append(doc_id)
            result.scores.append(float(sim))
            result.distances.append(1.0 - float(sim))

        # Fetch documents and metadata from SQLite
        self._hydrate_results(result)
        return result

    def _search_streaming_exact(self, query_vector: torch.Tensor, n_results: int,
                                query_entropy: float,
                                candidate_indices: Optional[List[int]] = None,
                                temperature: float = 0.0) -> SearchResult:
        """Chunked exact cosine scan over the dense vector memmap."""
        if self._memmap is None or self._memmap_dim is None or self._store_drosophila:
            return self._search_bruteforce(
                query_vector,
                n_results,
                query_entropy,
                candidate_indices,
                temperature=temperature,
            )

        query = query_vector.detach().cpu().view(-1).float().numpy().astype(np.float32, copy=False)
        if query.shape[0] != self._memmap_dim:
            return SearchResult()
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0.0:
            return SearchResult()

        total_count = len(self._doc_ids)
        if candidate_indices is None:
            indices_array = None
        else:
            indices_array = np.asarray(candidate_indices, dtype=np.int64)
            if indices_array.size:
                indices_array = indices_array[(indices_array >= 0) & (indices_array < total_count)]
        if (indices_array is not None and indices_array.size == 0) or total_count == 0:
            return SearchResult()

        cached_norms = self._ensure_streaming_exact_norms()
        cached_inv_norms = self._streaming_exact_inv_norms
        if (
            cached_norms is None
            or cached_inv_norms is None
            or cached_norms.shape[0] < len(self._doc_ids)
            or cached_inv_norms.shape[0] < len(self._doc_ids)
        ):
            return self._search_bruteforce(
                query_vector,
                n_results,
                query_entropy,
                candidate_indices,
                temperature=temperature,
            )

        scan_count = total_count if indices_array is None else int(indices_array.size)
        take_limit = min(n_results, scan_count)
        inv_query_norm = 1.0 / query_norm
        best_scores = np.empty(0, dtype=np.float32)
        best_indices = np.empty(0, dtype=np.int64)
        chunk_size = self._streaming_exact_chunk_size(query.shape[0], scan_count)

        with self._lock_file(shared=True):
            self._ensure_memmap()
            if self._memmap is None:
                return SearchResult()
            for start in range(0, scan_count, chunk_size):
                end = min(start + chunk_size, scan_count)
                if indices_array is None:
                    rows_view = self._memmap[start:end]
                    chunk_indices = np.arange(start, end, dtype=np.int64)
                else:
                    chunk_indices = indices_array[start:end]
                    if chunk_indices.size == 0:
                        continue
                    if (
                        chunk_indices.size > 1
                        and int(chunk_indices[-1]) - int(chunk_indices[0]) + 1 == chunk_indices.size
                        and np.all(np.diff(chunk_indices) == 1)
                    ):
                        rows_view = self._memmap[int(chunk_indices[0]):int(chunk_indices[-1]) + 1]
                    else:
                        rows_view = self._memmap[chunk_indices]
                rows = np.asarray(rows_view, dtype=np.float32)
                if rows.ndim != 2 or rows.shape[1] != query.shape[0]:
                    return SearchResult()

                dots = rows @ query
                scores = (dots * cached_inv_norms[chunk_indices] * inv_query_norm).astype(np.float32, copy=False)

                if self.entropy_tolerance < 999.0:
                    entropies = np.fromiter(
                        (self._entropies[int(idx)] for idx in chunk_indices),
                        dtype=np.float64,
                        count=chunk_indices.size,
                    )
                    keep_mask = ~(np.abs(query_entropy - entropies) > self.entropy_tolerance)
                    if not np.all(keep_mask):
                        scores = scores[keep_mask]
                        chunk_indices = chunk_indices[keep_mask]
                if scores.size == 0:
                    continue

                take = min(take_limit, scores.size)
                if scores.size > take:
                    local_keep = np.argpartition(scores, -take)[-take:]
                else:
                    local_keep = np.arange(scores.size)
                best_scores = np.concatenate([best_scores, scores[local_keep].astype(np.float32, copy=False)])
                best_indices = np.concatenate([best_indices, chunk_indices[local_keep].astype(np.int64, copy=False)])
                if best_scores.size > take_limit:
                    keep = np.argpartition(best_scores, -take_limit)[-take_limit:]
                    best_scores = best_scores[keep]
                    best_indices = best_indices[keep]

        result = SearchResult()
        if best_scores.size == 0:
            return result

        order = np.lexsort((best_indices, -best_scores))
        for idx in order[:take_limit]:
            global_idx = int(best_indices[idx])
            sim_val = float(best_scores[idx])
            doc_id = self._doc_ids[global_idx]
            result.ids.append(doc_id)
            result.scores.append(sim_val)
            result.distances.append(1.0 - sim_val)

        self._hydrate_results(result)
        return result

    def _search_bruteforce(self, query_vector: torch.Tensor, n_results: int,
                           query_entropy: float,
                           candidate_indices: Optional[List[int]] = None,
                           temperature: float = 0.0) -> SearchResult:
        """Brute-force cosine similarity search."""
        flat_query = query_vector.detach().cpu().view(1, -1).float()

        if candidate_indices is not None:
            indices_to_search = candidate_indices
        else:
            indices_to_search = list(range(len(self._doc_ids)))

        if not indices_to_search:
            return SearchResult()

        try:
            keys = torch.stack([self._get_vector_at(i).view(-1) for i in indices_to_search]).float()
        except Exception:
            return SearchResult()

        if flat_query.shape[1] != keys.shape[1]:
            return SearchResult()

        cos_sim = torch.nn.functional.cosine_similarity(flat_query, keys, dim=1)

        # Stable sort by similarity descending, using index as a tie-breaker
        sim_scores = cos_sim.tolist()
        sorted_local_idx = sorted(range(len(sim_scores)), key=lambda idx: (sim_scores[idx], -idx), reverse=True)

        result = SearchResult()
        for local_idx in sorted_local_idx:
            if len(result.ids) >= n_results:
                break
            sim_val = sim_scores[local_idx]
            global_idx = indices_to_search[local_idx]

            # Entropy proximity gate
            cached_entropy = self._entropies[global_idx]
            if abs(query_entropy - cached_entropy) > self.entropy_tolerance:
                continue

            doc_id = self._doc_ids[global_idx]
            result.ids.append(doc_id)
            result.scores.append(float(sim_val))
            result.distances.append(1.0 - float(sim_val))

        self._hydrate_results(result)
        return result

    def _hydrate_results(self, result: SearchResult):
        """Fetch document text and metadata from SQLite for result IDs."""
        if not result.ids:
            return
        with self._connect() as conn:
            placeholders = ",".join("?" * len(result.ids))
            cursor = conn.execute(
                f"SELECT doc_id, document, metadata_json FROM vectors "
                f"WHERE doc_id IN ({placeholders}) AND collection = ?",
                result.ids + [self.collection]
            )
            rows = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}

            # Update last_accessed
            conn.execute(
                f"UPDATE vectors SET last_accessed = CURRENT_TIMESTAMP "
                f"WHERE doc_id IN ({placeholders}) AND collection = ?",
                result.ids + [self.collection]
            )
            conn.commit()

        valid = [(index, doc_id) for index, doc_id in enumerate(result.ids) if doc_id in rows]
        result.ids = [doc_id for _, doc_id in valid]
        result.scores = [result.scores[index] for index, _ in valid if index < len(result.scores)]
        result.distances = [result.distances[index] for index, _ in valid if index < len(result.distances)]
        for _, doc_id in valid:
            result.documents.append(rows[doc_id][0] or "")
            try:
                result.metadatas.append(json.loads(rows[doc_id][1] or "{}"))
            except json.JSONDecodeError:
                result.metadatas.append({})

    def _filter_by_metadata(self, where: Dict[str, Any]) -> List[int]:
        """Filter vectors by metadata conditions. Returns matching indices."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT doc_id, metadata_json FROM vectors WHERE collection = ?",
                (self.collection,)
            )
            rows = cursor.fetchall()

        matching_doc_ids = set()
        for doc_id, meta_json in rows:
            try:
                meta = json.loads(meta_json or "{}")
            except json.JSONDecodeError:
                continue
            if all(meta.get(k) == v for k, v in where.items()):
                matching_doc_ids.add(doc_id)

        return [i for i, did in enumerate(self._doc_ids) if did in matching_doc_ids]

    # ── Delete ─────────────────────────────────────────────────────────────

    def delete(self, doc_ids: List[str]) -> int:
        """Delete documents by ID. Returns count deleted."""
        self._refresh_if_changed()
        to_delete = set(doc_ids) & self._doc_id_set
        if not to_delete:
            return 0

        # If holographic engine, retrieve the original vectors V_i from SQLite for doc_ids to delete,
        # compute C_i = circular_convolution(K_i, V_i), and subtract them from self._holographic_state.
        if self.engine == "holographic":
            to_delete_list = list(to_delete)
            with self._connect() as conn:
                placeholders = ",".join("?" * len(to_delete_list))
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT doc_id, vector_blob FROM vectors WHERE doc_id IN ({placeholders}) AND collection = ?",
                    to_delete_list + [self.collection]
                )
                rows = cursor.fetchall()
            
            if rows:
                first_blob = rows[0][1]
                first_vec = self._blob_to_vector(first_blob)
                dim = first_vec.view(-1).shape[0]
                if self._holographic_state is None:
                    self._load_holographic_state(dim)
                
                from .holographic import circular_convolution, generate_key_vector
                for doc_id, blob in rows:
                    v_i = self._blob_to_vector(blob)
                    k_i = generate_key_vector(doc_id, dim)
                    c_i = circular_convolution(k_i, v_i)
                    self._holographic_state -= c_i
                self._save_holographic_state()

        # Remove from SQLite
        with self._connect() as conn:
            placeholders = ",".join("?" * len(to_delete))
            conn.execute(
                f"DELETE FROM vectors WHERE doc_id IN ({placeholders}) AND collection = ?",
                list(to_delete) + [self.collection]
            )
            conn.execute(
                f"DELETE FROM sparse_index WHERE doc_id IN ({placeholders}) AND collection = ?",
                list(to_delete) + [self.collection]
            )
            conn.execute(
                f"DELETE FROM doc_lengths WHERE doc_id IN ({placeholders}) AND collection = ?",
                list(to_delete) + [self.collection]
            )
            conn.commit()

        # Rebuild in-memory state and compact memmap
        new_ids, new_ents = [], []
        write_idx = 0
        for i, (did, ent) in enumerate(zip(self._doc_ids, self._entropies)):
            if did not in to_delete:
                new_ids.append(did)
                new_ents.append(ent)
                if write_idx < i:
                    vec = self._get_vector_at(i)
                    self._set_vector_at(write_idx, vec)
                    if self._flyhash_rerank_enabled and self._rerank_memmap is not None:
                        rerank_vec = self._get_rerank_vector_at(i, self._rerank_memmap_dim)
                        if rerank_vec is not None:
                            self._set_rerank_vector_at(write_idx, rerank_vec)
                    if self._pq_rerank_enabled and self._pq_memmap is not None:
                        self._set_pq_code_at(write_idx, vec)
                write_idx += 1

        # Zero out remaining rows in memmap
        if self._memmap is not None:
            with self._lock_file(shared=False):
                self._ensure_memmap()
                for i in range(write_idx, len(self._doc_ids)):
                    if i < self._memmap_capacity:
                        self._memmap[i] = 0.0
                try:
                    self._memmap.flush()
                except Exception:
                    pass

        if self._flyhash_rerank_enabled and self._rerank_memmap is not None:
            with self._lock_file(shared=False):
                for i in range(write_idx, len(self._doc_ids)):
                    if i < self._rerank_memmap_capacity:
                        self._rerank_memmap[i] = 0.0
                try:
                    self._rerank_memmap.flush()
                except Exception:
                    pass

        if self._pq_rerank_enabled and self._pq_memmap is not None:
            with self._lock_file(shared=False):
                for i in range(write_idx, len(self._doc_ids)):
                    if i < self._pq_memmap_capacity:
                        self._pq_memmap[i] = 0
                try:
                    self._pq_memmap.flush()
                except Exception:
                    pass

        self._doc_ids = new_ids
        self._entropies = new_ents
        self._doc_id_set -= to_delete
        self._invalidate_streaming_exact_norms(remove_sidecar=True)
        if self._native_hnsw_enabled:
            self._invalidate_hnswlib_index(remove_sidecar=True)
        if self._diskann_rerank_enabled:
            self._rebuild_diskann_sidecars()
        self._rebuild_faiss()

        return len(to_delete)

    # ── Eviction ───────────────────────────────────────────────────────────

    def _evict_oldest(self):
        """Evict least-recently-accessed entries to stay under max_entries."""
        overshoot = len(self._doc_ids) - self.max_entries
        if overshoot <= 0:
            return

        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT doc_id FROM vectors WHERE collection = ? ORDER BY last_accessed ASC LIMIT ?",
                (self.collection, overshoot)
            )
            evict_ids = [r[0] for r in cursor.fetchall()]

        if evict_ids:
            self.delete(evict_ids)
            logger.debug("Evicted %d entries from collection '%s'", len(evict_ids), self.collection)

    # ── Utilities ──────────────────────────────────────────────────────────

    def count(self) -> int:
        """Number of vectors in this collection."""
        return len(self._doc_ids)

    def get_vector(self, doc_id: str) -> Optional[torch.Tensor]:
        """Retrieve a vector by document ID."""
        if doc_id not in self._doc_id_set:
            return None
        idx = self._doc_ids.index(doc_id)
        return self._get_vector_at(idx)

    def all_vectors(self) -> Tuple[List[str], List[torch.Tensor]]:
        """Return all (doc_ids, vectors) pairs."""
        vectors = [self._get_vector_at(i) for i in range(len(self._doc_ids))]
        return self._doc_ids[:], vectors

    def clear(self):
        """Remove all vectors from this collection."""
        if self.engine == "holographic":
            if self._holographic_state is not None:
                self._holographic_state.zero_()
            else:
                with self._connect() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT embedding_dim FROM collection_meta WHERE name = ?", (self.collection,))
                    row = cursor.fetchone()
                dim = row[0] if row else 768
                self._load_holographic_state(dim)
                if self._holographic_state is not None:
                    self._holographic_state.zero_()
            self._save_holographic_state()

        with self._connect() as conn:
            conn.execute("DELETE FROM vectors WHERE collection = ?", (self.collection,))
            conn.execute("DELETE FROM sparse_index WHERE collection = ?", (self.collection,))
            conn.execute("DELETE FROM doc_lengths WHERE collection = ?", (self.collection,))
            conn.commit()
        
        # Zero out the memmap
        if self._memmap is not None:
            with self._lock_file(shared=False):
                self._ensure_memmap()
                self._memmap[:] = 0.0
                try:
                    self._memmap.flush()
                except Exception:
                    pass

        if self._flyhash_rerank_enabled and self._rerank_memmap is not None:
            with self._lock_file(shared=False):
                self._rerank_memmap[:] = 0.0
                try:
                    self._rerank_memmap.flush()
                except Exception:
                    pass

        if self._pq_rerank_enabled and self._pq_memmap is not None:
            with self._lock_file(shared=False):
                self._pq_memmap[:] = 0
                try:
                    self._pq_memmap.flush()
                except Exception:
                    pass

        if self._diskann_rerank_enabled:
            if self._diskann_vector_memmap is not None:
                with self._lock_file(shared=False):
                    self._diskann_vector_memmap[:] = 0
                    try:
                        self._diskann_vector_memmap.flush()
                    except Exception:
                        pass
            if self._diskann_graph_memmap is not None:
                with self._lock_file(shared=False):
                    self._diskann_graph_memmap[:] = self._diskann_empty_neighbor
                    try:
                        self._diskann_graph_memmap.flush()
                    except Exception:
                        pass

        self._doc_ids.clear()
        self._entropies.clear()
        self._doc_id_set.clear()
        self._invalidate_streaming_exact_norms(remove_sidecar=True)
        if self._native_hnsw_enabled:
            self._invalidate_hnswlib_index(remove_sidecar=True)
        self._faiss_index = None
        self._use_faiss = False

    # ── Key Blob Storage (Envelope Encryption) ─────────────────────────────

    def _load_holographic_state(self, dim: int):
        from .holographic import validate_holographic_dimension
        validate_holographic_dimension(int(dim))
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT holographic_state FROM collection_meta WHERE name = ?",
                (self.collection,)
            )
            row = cursor.fetchone()
        if row and row[0] is not None:
            self._holographic_state = self._blob_to_tensor(row[0])
        else:
            self._holographic_state = torch.zeros(dim)

    def _save_holographic_state(self):
        if self._holographic_state is None:
            return
        blob = self._tensor_to_blob(self._holographic_state)
        dim = self._holographic_state.view(-1).shape[0]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO collection_meta (name, embedding_dim, holographic_state)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    holographic_state = excluded.holographic_state,
                    embedding_dim = excluded.embedding_dim
                """,
                (self.collection, dim, blob)
            )
            conn.commit()

    def shred_holographic_state(self):
        if self._holographic_state is not None:
            self._holographic_state.zero_()
            self._holographic_state = None
        
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT holographic_state FROM collection_meta WHERE name = ?",
                (self.collection,)
            )
            row = cursor.fetchone()
            if row and row[0] is not None:
                blob_len = len(row[0])
                import os as python_os
                random_bytes = python_os.urandom(blob_len)
                conn.execute(
                    "UPDATE collection_meta SET holographic_state = ? WHERE name = ?",
                    (random_bytes, self.collection)
                )
                conn.commit()
                conn.execute(
                    "UPDATE collection_meta SET holographic_state = NULL WHERE name = ?",
                    (self.collection,)
                )
                conn.commit()

    def store_key_blob(self, blob: str):
        """Store an encrypted key blob in collection_meta."""
        with self._connect() as conn:
            conn.execute(
                '''INSERT INTO collection_meta (name, encrypted_key_blob)
                   VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET encrypted_key_blob = excluded.encrypted_key_blob''',
                (self.collection, blob)
            )
            conn.commit()

    def get_key_blob(self) -> Optional[str]:
        """Retrieve the encrypted key blob from collection_meta, or None."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT encrypted_key_blob FROM collection_meta WHERE name = ?",
                (self.collection,)
            )
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
        return None

    def delete_key_blob(self):
        """
        Crypto-shred: overwrite, delete, and vacuum the key blob,
        making all encrypted vectors in this collection permanently irrecoverable.
        """
        blob = self.get_key_blob()
        if not blob:
            logger.warning("No key blob found to shred for collection '%s'.", self.collection)
            return

        # 1. Overwrite with random bytes of matching length
        import os
        random_bytes = os.urandom(len(blob))
        with self._connect() as conn:
            conn.execute(
                "UPDATE collection_meta SET encrypted_key_blob = ? WHERE name = ?",
                (random_bytes, self.collection)
            )
            conn.commit()

        # 2. Set database field to NULL/Delete and commit
        with self._connect() as conn:
            conn.execute(
                "UPDATE collection_meta SET encrypted_key_blob = NULL WHERE name = ?",
                (self.collection,)
            )
            conn.commit()

        # 3. Trigger VACUUM to purge WAL and free pages
        vacuum_conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            vacuum_conn.isolation_level = None
            vacuum_conn.execute("PRAGMA secure_delete = FAST;")
            vacuum_conn.execute("VACUUM;")
        finally:
            vacuum_conn.close()

        logger.info("Crypto-shredded key for collection '%s'. Vectors are now irrecoverable.",
                     self.collection)

    def export_all_records(self) -> List[Dict[str, Any]]:
        """Export all raw entries for backup."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT doc_id, document, vector_blob, metadata_json, entropy FROM vectors WHERE collection = ?",
                (self.collection,)
            )
            rows = cursor.fetchall()
        
        records = []
        for doc_id, document, vector_blob, metadata_json, entropy in rows:
            records.append({
                "doc_id": doc_id,
                "document": document,
                "vector_blob": b64encode(vector_blob).decode("utf-8"),
                "metadata_json": metadata_json,
                "entropy": entropy
            })
        return records

    def import_all_records(self, records: List[Dict[str, Any]]):
        """Bulk import records from backup."""
        with self._connect() as conn:
            for rec in records:
                doc_id = rec["doc_id"]
                document = rec["document"]
                vector_blob = b64decode(rec["vector_blob"])
                metadata_json = rec["metadata_json"]
                entropy = rec["entropy"]
                
                conn.execute(
                    '''INSERT OR REPLACE INTO vectors (doc_id, document, vector_blob, metadata_json, entropy, collection)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (doc_id, document, vector_blob, metadata_json, entropy, self.collection)
                )
            conn.commit()
        # Rebuild in-memory cache
        self._doc_ids.clear()
        self._vectors = []
        self._entropies.clear()
        self._doc_id_set.clear()
        self._invalidate_streaming_exact_norms(remove_sidecar=True)
        if self._native_hnsw_enabled:
            self._invalidate_hnswlib_index(remove_sidecar=True)
        self._faiss_index = None
        self._use_faiss = False

        filepath = f"{self.db_path}_{self.collection}_vectors.bin"
        if self._memmap is not None:
            try:
                self._memmap.flush()
            except Exception:
                pass
            del self._memmap
            self._memmap = None
        self._memmap_capacity = 0
        self._memmap_dim = None
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass

        self._load_from_db()

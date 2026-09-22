"""
LatticeDB — local SQLite document and vector store.

Optional Cayley rotations preserve vector distances for search; they do not
provide opaque vector encryption. Leech Lattice indexing is optional.

Usage:
    import latticeshadow_db.latticedb as latticedb

    db = latticedb.connect("my_vectors.db")
    db.add(documents=["The mitochondria is the powerhouse of the cell."])
    results = db.search("The mitochondria is the powerhouse of the cell.", n_results=5)
"""

from .collection import Collection
from .store import VectorStore, SearchResult
from .privacy import PrivacyEngine
from .indexer import LatticeIndexer
from .distiller import AutoDistiller
from .embedder import EmbeddingPipeline, EmbeddingFn
from .integrations import LatticeDBVectorStore, LatticeDBLlamaIndexStore
from .holographic import circular_convolution, circular_correlation, cleanup_to_codebook

__all__ = [
    "connect",
    "Collection",
    "VectorStore",
    "SearchResult",
    "PrivacyEngine",
    "LatticeIndexer",
    "AutoDistiller",
    "EmbeddingPipeline",
    "LatticeDBVectorStore",
    "LatticeDBLlamaIndexStore",
    "circular_convolution",
    "circular_correlation",
    "cleanup_to_codebook",
]


def connect(db_path: str = "latticedb.sqlite",
            collection: str = "default",
            embedding_fn: "EmbeddingFn | None" = None,
            cloud_fn: "EmbeddingFn | None" = None,
            embedding_dim: int = 768,
            privacy: bool = False,
            auto_distill: bool = False,
            lattice_index: bool = False,
            max_entries: int = 0,
            master_key: "str | None" = None,
            drosophila_hash: bool = False,
            engine: str = "default",
            device: str = "cpu",
            experimental_index: "str | None" = None,
            hnsw_m: int = 32,
            hnsw_ef_construction: int = 200,
            hnsw_ef_search: int = 128,
            hnsw_candidate_cap: int = 1000,
            embedding_model: "str | None" = None) -> Collection:
    """
    Connect to a LatticeDB database, creating it if it doesn't exist.

    Args:
        db_path: Path to the SQLite database file.
        collection: Name of the collection to use.
        embedding_fn: Callable text → Tensor for local embeddings.
        cloud_fn: Optional callable for cloud embeddings (enables distillation).
        embedding_dim: Dimension of the embedding vectors.
        embedding_model: Stable model identity. When set, the collection refuses
                         vectors created with a different or unknown model.
        privacy: If True, encrypt document text and rotate stored vectors.
                 The rotation preserves similarity geometry and is not opaque
                 vector encryption. AES-GCM wraps the rotation key.
        auto_distill: If True, enable automatic cloud→local distillation.
        lattice_index: If True, enable Leech Lattice coarse indexing.
        max_entries: Maximum entries before LRU eviction (0 = unlimited).
        master_key: Master passphrase for envelope encryption. Resolved via:
                    1. This parameter (explicit)
                    2. LATTICEDB_MASTER_KEY environment variable
                    3. OS Keychain (auto-generated)
        drosophila_hash: If True, enable Drosophila biological hashing & Hamming search.
        engine: The storage and search engine to use (e.g. "default", "hyperbolic", "holographic").
        device: Hardware device for tensor ops ("cpu", "mps", "cuda").
        experimental_index: Optional experimental index strategy. Supports
                            HNSW/FlyHash/PQ exact-rerank aliases,
                            "diskann_rerank", "streaming_exact", and
                            conservative "cascade_auto".

    Returns:
        A Collection instance ready for add/search/delete operations.

    Example:
        >>> db = latticedb.connect("vectors.db")
        >>> db.add(documents=["Patient has mild hypertension."])
        >>> results = db.search("Patient has mild hypertension.", n_results=3)
    """
    return Collection(
        name=collection,
        db_path=db_path,
        embedding_fn=embedding_fn,
        cloud_fn=cloud_fn,
        embedding_dim=embedding_dim,
        privacy_enabled=privacy,
        auto_distill=auto_distill,
        lattice_index=lattice_index,
        max_entries=max_entries,
        master_key=master_key,
        drosophila_hash=drosophila_hash,
        engine=engine,
        device=device,
        experimental_index=experimental_index,
        hnsw_m=hnsw_m,
        hnsw_ef_construction=hnsw_ef_construction,
        hnsw_ef_search=hnsw_ef_search,
        hnsw_candidate_cap=hnsw_candidate_cap,
        embedding_model=embedding_model,
    )

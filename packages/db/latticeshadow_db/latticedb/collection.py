"""
Collection — The primary user-facing interface for LatticeDB.

A Collection is a named set of documents with encrypted vector storage,
optional lattice indexing, and automatic distillation.
"""

import uuid
import hashlib
import torch
import logging
import urllib.request
import json
import numpy as np
from typing import Optional, List, Dict, Any, Callable
from pathlib import Path

from .store import VectorStore, SearchResult
from .privacy import PrivacyEngine
from .indexer import LatticeIndexer
from .distiller import AutoDistiller
from .embedder import EmbeddingPipeline, EmbeddingFn
from ..quant import MAX_LEECH_BLOCKS

logger = logging.getLogger("latticeshadow_db.latticedb.collection")


class Collection:
    """
    A named collection of documents with encrypted vector storage.

    Combines VectorStore, PrivacyEngine, LatticeIndexer, AutoDistiller,
    and EmbeddingPipeline into a unified, simple API.

    Example:
        >>> coll = Collection("my_docs", db_path="vectors.db")
        >>> coll.add(documents=["hello world"], metadatas=[{"tag": "test"}])
        >>> results = coll.search("hello", n_results=5)
    """

    def __init__(self, name: str, db_path: str = "latticedb.sqlite",
                 embedding_fn: Optional[EmbeddingFn] = None,
                 cloud_fn: Optional[EmbeddingFn] = None,
                 embedding_dim: int = 768,
                 privacy_enabled: bool = False,
                 auto_distill: bool = False,
                 lattice_index: bool = False,
                 max_entries: int = 0,
                 entropy_tolerance: float = 0.5,
                 master_key: Optional[str] = None,
                 drosophila_hash: bool = False,
                 engine: str = "default",
                 device: str = "cpu",
                 experimental_index: Optional[str] = None,
                 hnsw_m: int = 32,
                 hnsw_ef_construction: int = 200,
                 hnsw_ef_search: int = 128,
                 hnsw_candidate_cap: int = 1000,
                 diskann_graph_degree: int = 16,
                 diskann_probe_count: int = 8,
                 diskann_candidate_cap: int = 2000,
                 diskann_full_scan_threshold: int = 4096,
                 embedding_model: Optional[str] = None):
        """
        Args:
            name: Collection name (used for isolation within the database).
            db_path: Path to the SQLite database file.
            embedding_fn: Callable that converts text → Tensor. If None, uses
                          a deterministic hash-based fallback (for testing).
            cloud_fn: Optional cloud embedding function for distillation.
            embedding_dim: Dimension of the embedding vectors.
            embedding_model: Stable model identity used to prevent mixing vectors
                             from different models in one collection.
            privacy_enabled: If True, vectors are Cayley-rotated before storage.
                             The encrypted key is stored inside the database itself
                             (envelope encryption — no sidecar files).
            auto_distill: If True, enables automatic distillation training.
            lattice_index: If True, enables Leech Lattice coarse indexing.
            max_entries: Maximum entries before LRU eviction (0 = unlimited).
            entropy_tolerance: Max entropy delta for search validation.
            master_key: Master passphrase for envelope encryption. If None,
                        resolved via LATTICEDB_MASTER_KEY env var or OS keychain.
            drosophila_hash: If True, enable Drosophila biological hashing & Hamming search.
            engine: The storage and search engine to use (e.g. "default", "hyperbolic", "holographic").
            experimental_index: Optional experimental index strategy. Supports
                                HNSW/FlyHash/PQ exact-rerank aliases,
                                "diskann_rerank", "streaming_exact", and
                                conservative "cascade_auto".
        """
        self.name = name
        self.db_path = db_path
        self.engine = engine
        self.experimental_index = experimental_index

        # Core store
        self._store = VectorStore(
            db_path=db_path,
            collection=name,
            max_entries=max_entries,
            entropy_tolerance=entropy_tolerance,
            drosophila_hash=drosophila_hash,
            engine=engine,
            experimental_index=experimental_index,
            hnsw_m=hnsw_m,
            hnsw_ef_construction=hnsw_ef_construction,
            hnsw_ef_search=hnsw_ef_search,
            hnsw_candidate_cap=hnsw_candidate_cap,
            diskann_graph_degree=diskann_graph_degree,
            diskann_probe_count=diskann_probe_count,
            diskann_candidate_cap=diskann_candidate_cap,
            diskann_full_scan_threshold=diskann_full_scan_threshold,
        )


        # Embedding pipeline
        self._embedder = EmbeddingPipeline(
            local_fn=embedding_fn,
            cloud_fn=cloud_fn,
            dim=embedding_dim,
        )
        self._embedding_dim = embedding_dim
        if embedding_model:
            self._store.claim_embedding_model(embedding_model, embedding_dim)
        else:
            self._store.reject_unidentified_model()

        # Privacy engine (vault pattern: KEK/DEK envelope encryption)
        self._privacy_enabled = privacy_enabled
        self.master_key = master_key
        self.engine = engine
        self.device = device
        self._privacy: Optional[PrivacyEngine] = None
        if privacy_enabled:
            self._privacy = PrivacyEngine(
                dim=embedding_dim,
                master_key=master_key,
            )
            self._init_privacy_vault()

        # Lattice indexer
        self._lattice_enabled = lattice_index
        self._indexer: Optional[LatticeIndexer] = None
        if lattice_index:
            if embedding_dim % 32 != 0:
                raise ValueError(f"Lattice index embedding dimension ({embedding_dim}) must be divisible by 32.")
            max_lattice_dim = MAX_LEECH_BLOCKS * 24
            if embedding_dim > max_lattice_dim:
                raise ValueError(
                    f"Lattice index embedding dimension ({embedding_dim}) exceeds "
                    f"the configured limit ({max_lattice_dim})."
                )
            self._indexer = LatticeIndexer()

        # Auto-distiller
        self._distill_enabled = auto_distill
        self._distiller: Optional[AutoDistiller] = None
        if auto_distill:
            distill_encryption_key = None
            if self._privacy_enabled and self._privacy:
                distill_encryption_key = self._privacy.derive_data_key(b"distillation_pairs")
            self._distiller = AutoDistiller(
                dim=embedding_dim,
                db_path=db_path,
                collection=name,
                encryption_key=distill_encryption_key,
            )

    def _init_privacy_vault(self):
        """
        Initialize the privacy vault:
        - If a wrapped key exists in SQLite → unwrap it (requires correct master key)
        - If no key exists → generate a new one, wrap it, store in SQLite
        """
        existing_blob = self._store.get_key_blob()
        if existing_blob:
            # Existing vault — decrypt the DEK with our master key
            self._privacy.load_wrapped_key(existing_blob)
            logger.info("Unlocked privacy vault for collection '%s'.", self.name)
        else:
            # If no key exists but vectors already exist, the key was shredded or lost
            if self._store.count() > 0:
                raise PermissionError(
                    f"Collection '{self.name}' has stored vectors but no encryption key. "
                    "The key may have been crypto-shredded, making the data permanently unreadable."
                )
            # New vault — generate DEK, wrap with master key, store in SQLite
            wrapped_blob = self._privacy.generate_key()
            self._store.store_key_blob(wrapped_blob)
            logger.info("Created new privacy vault for collection '%s'.", self.name)

    # ── Add ────────────────────────────────────────────────────────────────

    def add(self, documents: List[str],
            metadatas: Optional[List[Dict[str, Any]]] = None,
            ids: Optional[List[str]] = None) -> List[str]:
        """
        Embed, optionally encrypt, and store documents.

        Args:
            documents: List of text strings to embed and store.
            metadatas: Optional list of metadata dicts (one per document).
            ids: Optional list of document IDs. Auto-generated if not provided.

        Returns:
            List of document IDs.
        """
        n = len(documents)
        metadatas = metadatas or [{}] * n
        ids = ids or [self._generate_id(doc) for doc in documents]

        # Embed all documents
        vectors = self._embedder.embed_batch(documents)

        # Optionally record cloud pairs for distillation
        if self._distill_enabled and self._distiller and self._embedder.has_cloud:
            for doc, local_vec in zip(documents, vectors):
                cloud_vec = self._embedder.embed_cloud(doc)
                if cloud_vec is not None:
                    self._distiller.record_pair(local_vec, cloud_vec)

        # Optionally encrypt vectors for storage
        store_vectors = vectors
        if self._privacy_enabled and self._privacy:
            store_vectors = [self._privacy.encrypt(v.unsqueeze(0)).squeeze(0)
                             for v in vectors]

        # Optionally index in lattice space
        if self._lattice_enabled and self._indexer:
            for doc_id, vec in zip(ids, vectors):
                self._indexer.index(doc_id, vec)

        # Store in vector store
        store_documents = documents
        if self._privacy_enabled and self._privacy:
            store_documents = [self._privacy.encrypt_document(doc) for doc in documents]

        self._store.insert_batch(
            doc_ids=ids,
            vectors=store_vectors,
            documents=store_documents,
            metadatas=metadatas,
        )

        return ids

    def get_records(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Return stored canonical rows; encrypted text remains encrypted here."""
        return self._store.get_records(ids)

    def scan_records(self, *, after_row_id: int = 0, limit: int = 500):
        return self._store.scan_records(after_row_id=after_row_id, limit=limit)

    def revision(self) -> int:
        return self._store.revision()

    def repair_status(self) -> Dict[str, Any]:
        """Report a committed record whose derived index still needs repair."""
        return self._store.repair_status()

    def mark_repair_needed(self, error: str) -> None:
        self._store.mark_repair_needed(error)

    # ── Search ─────────────────────────────────────────────────────────────

    def search(self, query: str, n_results: int = 10,
               where: Optional[Dict[str, Any]] = None,
               temperature: float = 0.0,
               hybrid: bool = False,
               candidate_ids: Optional[List[str]] = None) -> SearchResult:
        """
        Semantic search over stored documents.

        Args:
            query: Search query text.
            n_results: Number of results to return.
            where: Optional metadata filter (exact match on all keys).
            temperature: Search temperature.

        Returns:
            SearchResult with ids, documents, metadatas, scores.
        """
        if n_results <= 0:
            raise ValueError("n_results must be strictly positive (greater than 0).")
        if not isinstance(temperature, (int, float)):
            raise TypeError("temperature must be a float or int")
        if not (0.0 <= temperature <= 1.0):
            raise ValueError("temperature must be between 0.0 and 1.0")

        # Embed the query
        query_vec = self._embedder.embed(query)

        # Check if distiller can serve locally
        served_locally = False
        if self._distill_enabled and self._distiller and self._distiller.is_graduated:
            # Use distilled prediction for better quality
            predicted = self._distiller.predict(query_vec)
            if predicted is not None:
                query_vec = predicted
                served_locally = True

        # Encrypt query if privacy is enabled (search on encrypted space)
        search_vec = query_vec
        if self._privacy_enabled and self._privacy:
            search_vec = self._privacy.encrypt(query_vec.unsqueeze(0)).squeeze(0)

        # Use lattice coarse filter if enabled
        candidate_ids = None
        if self._lattice_enabled and self._indexer and self._indexer.count() > 0:
            # Get 3x candidates from lattice, then fine-rank with FAISS
            lattice_results = self._indexer.search(query_vec, k=n_results * 3)
            if lattice_results:
                candidate_ids = [doc_id for doc_id, _ in lattice_results]

        # Fine-rank with vector store
        query_text = query if hybrid else None
        result = self._store.search(search_vec, n_results=n_results,
                                    where=where, candidate_ids=candidate_ids,
                                    temperature=temperature,
                                    query_text=query_text)

        if self._privacy_enabled and self._privacy and result.documents:
            result.documents = [self._privacy.decrypt_document(doc) for doc in result.documents]

        result.served_locally = served_locally
        return result

    # ── Delete ─────────────────────────────────────────────────────────────

    def delete(self, ids: List[str]) -> int:
        """
        Delete documents by ID.

        Args:
            ids: List of document IDs to delete.

        Returns:
            Number of documents deleted.
        """
        # Remove from lattice index
        if self._lattice_enabled and self._indexer:
            for doc_id in ids:
                self._indexer.remove(doc_id)

        return self._store.delete(ids)

    def consolidate_rem_sleep(self, num_clusters: Optional[int] = None):
        """
        REM Sleep Database Consolidation.
        Clusters documents and summarises them using a local LM Studio instance,
        then evicts the original documents and stores the summaries.
        """
        # Query all documents and vector blobs in the collection
        with self._store._connect() as conn:
            cursor = conn.execute(
                "SELECT doc_id, document, vector_blob FROM vectors WHERE collection = ?",
                (self.name,)
            )
            rows = cursor.fetchall()

        doc_ids = []
        documents = []
        vectors = []
        for doc_id, document, blob in rows:
            if not document or not document.strip():
                continue
            if self._privacy_enabled and self._privacy:
                document = self._privacy.decrypt_document(document)
            doc_ids.append(doc_id)
            documents.append(document)
            vector = self._store._blob_to_vector(blob)
            vectors.append(vector)

        if len(doc_ids) < 2:
            # Not enough valid documents to cluster
            return

        if num_clusters is not None:
            if num_clusters <= 0 or num_clusters > len(doc_ids):
                raise ValueError(f"num_clusters ({num_clusters}) must be strictly positive and less than or equal to the number of valid documents ({len(doc_ids)}).")

        # Decrypt vectors if privacy is enabled
        decrypted_vectors = []
        for vector in vectors:
            if self._privacy_enabled and self._privacy and not self._store.drosophila_hash:
                # Decrypt each vector
                vector = self._privacy.decrypt(vector.unsqueeze(0)).squeeze(0)
            decrypted_vectors.append(vector)

        # Convert/unpack to numpy representations
        representations = []
        for vector in decrypted_vectors:
            if self._store.drosophila_hash:
                rep = np.unpackbits(vector.cpu().numpy() if isinstance(vector, torch.Tensor) else np.asarray(vector)).astype(np.float32)
            else:
                rep = vector.detach().cpu().numpy() if isinstance(vector, torch.Tensor) else np.asarray(vector)
            representations.append(rep)
        representations = np.vstack(representations)

        # Clustering
        labels = None
        if num_clusters is None:
            try:
                import hdbscan
                clusterer = hdbscan.HDBSCAN(min_cluster_size=2)
                labels = clusterer.fit_predict(representations)
            except (ImportError, Exception):
                pass

        if labels is None:
            k = num_clusters if num_clusters is not None else max(1, len(representations) // 3)
            k = min(k, len(representations))
            try:
                from sklearn.cluster import KMeans
                kmeans = KMeans(n_clusters=k, random_state=42)
                labels = kmeans.fit_predict(representations)
            except (ImportError, Exception):
                # Custom pure-NumPy KMeans fallback
                np.random.seed(42)
                indices = np.random.choice(len(representations), k, replace=False)
                centroids = representations[indices]
                x_sq = np.sum(representations ** 2, axis=1, keepdims=True)  # (N, 1)
                for _ in range(10):
                    y_sq = np.sum(centroids ** 2, axis=1)  # (k,)
                    xy = np.dot(representations, centroids.T)  # (N, k)
                    dist_sq = x_sq + y_sq - 2.0 * xy
                    labels = np.argmin(dist_sq, axis=1)
                    new_centroids = []
                    for j in range(k):
                        points = representations[labels == j]
                        if len(points) > 0:
                            new_centroids.append(points.mean(axis=0))
                        else:
                            new_centroids.append(centroids[j])
                    centroids = np.array(new_centroids)

        # Group indices by cluster
        from collections import defaultdict
        clusters = defaultdict(list)
        for idx, label in enumerate(labels):
            if label >= 0:
                clusters[label].append(idx)

        # Consolidate each cluster
        for label, idxs in clusters.items():
            if len(idxs) <= 1:
                continue

            cluster_doc_ids = [doc_ids[i] for i in idxs]
            cluster_texts = [documents[i] for i in idxs]

            # Filter out empty or whitespace-only documents
            cluster_texts = [txt for txt in cluster_texts if txt and txt.strip()]
            if len(cluster_texts) <= 1:
                continue

            text_content = "\n\n".join(cluster_texts)

            # Call local LM Studio API
            url = "http://localhost:1234/v1/chat/completions"
            headers = {"Content-Type": "application/json"}
            payload = {
                "messages": [
                    {"role": "system", "content": "You are a database consolidation agent. Summarize the following group of related documents into a single clear, cohesive, and consolidated summary document that retains all key facts, entities, and context."},
                    {"role": "user", "content": f"Please consolidate these documents:\n\n{text_content}"}
                ],
                "temperature": 0.3
            }

            summary_text = None
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    resp_data = json.loads(response.read().decode("utf-8"))
                    summary_text = resp_data["choices"][0]["message"]["content"]
            except Exception:
                summary_text = "Consolidated Summary:\n" + "\n".join(cluster_texts)

            if not summary_text or not summary_text.strip():
                summary_text = "Consolidated Summary:\n" + "\n".join(cluster_texts)

            # Embed summary and add to collection
            self.add(documents=[summary_text])

            # Evict original documents
            self.delete(cluster_doc_ids)

    consolidate = consolidate_rem_sleep

    # ── Memory Helpers (for LLM dream cycle + CLI) ────────────────────────

    def get_undreamed(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get clips that haven't been processed by the LLM dream cycle yet.
        Returns list of dicts with 'doc_id', 'document', 'metadata'.
        """
        with self._store._connect() as conn:
            cursor = conn.execute(
                """SELECT doc_id, document, metadata_json FROM vectors
                   WHERE collection = ?
                   ORDER BY created_at ASC""",
                (self.name,)
            )
            results = []
            for row in cursor:
                doc_id, document, meta_json = row
                meta = json.loads(meta_json) if meta_json else {}
                if meta.get("dreamed"):
                    continue
                # Decrypt document if privacy is enabled
                doc_text = document
                if self._privacy and document and document.startswith("enc:"):
                    try:
                        doc_text = self._privacy.decrypt_document(document)
                    except Exception:
                        doc_text = document
                results.append({
                    "doc_id": doc_id,
                    "document": doc_text,
                    "metadata": meta,
                })
                if len(results) >= limit:
                    break
            return results

    def update_metadata(self, doc_id: str, updates: dict) -> bool:
        """
        Merge updates into an existing document's metadata.
        Returns True if the document was found and updated.
        """
        with self._store._connect() as conn:
            cursor = conn.execute(
                "SELECT metadata_json FROM vectors WHERE doc_id = ? AND collection = ?",
                (doc_id, self.name),
            )
            row = cursor.fetchone()
            if not row:
                return False
            meta = json.loads(row[0]) if row[0] else {}
            meta.update(updates)
            conn.execute(
                "UPDATE vectors SET metadata_json = ? WHERE doc_id = ? AND collection = ?",
                (json.dumps(meta), doc_id, self.name),
            )
            conn.commit()
            return True

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get the most recent clips, decrypted."""
        with self._store._connect() as conn:
            cursor = conn.execute(
                """SELECT doc_id, document, metadata_json, created_at FROM vectors
                   WHERE collection = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (self.name, limit),
            )
            results = []
            for row in cursor:
                doc_id, document, meta_json, created_at = row
                doc_text = document
                if self._privacy and document and document.startswith("enc:"):
                    try:
                        doc_text = self._privacy.decrypt_document(document)
                    except Exception:
                        doc_text = document
                meta = json.loads(meta_json) if meta_json else {}
                results.append({
                    "doc_id": doc_id,
                    "document": doc_text,
                    "metadata": meta,
                    "created_at": created_at,
                })
            return results

    def get_today(self) -> List[Dict[str, Any]]:
        """Get all clips from today, decrypted."""
        with self._store._connect() as conn:
            cursor = conn.execute(
                """SELECT doc_id, document, metadata_json, created_at FROM vectors
                   WHERE collection = ? AND date(created_at) = date('now')
                   ORDER BY created_at ASC""",
                (self.name,),
            )
            results = []
            for row in cursor:
                doc_id, document, meta_json, created_at = row
                doc_text = document
                if self._privacy and document and document.startswith("enc:"):
                    try:
                        doc_text = self._privacy.decrypt_document(document)
                    except Exception:
                        doc_text = document
                meta = json.loads(meta_json) if meta_json else {}
                results.append({
                    "doc_id": doc_id,
                    "document": doc_text,
                    "metadata": meta,
                    "created_at": created_at,
                })
            return results

    # ── Utilities ──────────────────────────────────────────────────────────

    def count(self) -> int:
        """Number of documents in this collection."""
        return self._store.count()

    def distill(self, epochs: int = 10) -> float:
        """
        Manually trigger distillation training.

        Returns:
            Training loss (MSE). Returns inf if no distiller configured.
        """
        if not self._distiller:
            return float('inf')
        return self._distiller.train(epochs=epochs)

    def clear(self):
        """Remove all documents from this collection."""
        self._store.clear()
        if self._indexer:
            self._indexer.clear()

    def rotate_master_key(self, new_master_key: str):
        """
        O(1) password rotation: re-wraps the ~262KB DEK under a new master key.
        Does NOT re-encrypt any stored vectors.

        Args:
            new_master_key: New master passphrase.
        """
        if not self._privacy:
            logger.warning("Privacy not enabled for collection '%s'.", self.name)
            return
        old_blob = self._store.get_key_blob()
        if not old_blob:
            logger.warning("No key blob found for collection '%s'.", self.name)
            return
        new_blob = self._privacy.rewrap_key(old_blob, new_master_key)
        self._store.store_key_blob(new_blob)
        # Update the engine's master key for subsequent operations
        self._privacy._master_key_bytes = new_master_key.encode("utf-8")
        logger.info("Master key rotated for collection '%s'.", self.name)

    def circular_convolution(self, a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
        """Forward circular convolution to holographic module."""
        from .holographic import circular_convolution
        return circular_convolution(a, b)

    def circular_correlation(self, c: torch.Tensor, a: torch.Tensor) -> torch.Tensor:
        """Forward circular correlation to holographic module."""
        from .holographic import circular_correlation
        return circular_correlation(c, a)

    def crypto_shred(self):
        """
        Crypto-shred this collection: permanently destroy the encryption key.
        All encrypted vectors become irrecoverable noise.
        Useful for GDPR 'Right to be Forgotten' compliance.
        """
        if self.engine == "holographic":
            self._store.shred_holographic_state()

        if not self._privacy:
            if self.engine == "holographic":
                return
            logger.warning("Privacy not enabled for collection '%s'.", self.name)
            return
        
        # 1. Zero out in-memory DEK matrices and KEK
        self._privacy.shred()
        
        # 2. Overwrite, delete, and vacuum DB storage
        self._store.delete_key_blob()

        self._distiller = None
        self._distill_enabled = False
        self._privacy = None
        self._privacy_enabled = False
        logger.info("Collection '%s' crypto-shredded.", self.name)

    def export_data(self) -> Dict[str, Any]:
        """
        Export all documents, metadatas, IDs, and the encrypted key blob
        as a serializable dictionary for backup/migration.
        """
        records = self._store.export_all_records()
        return {
            "collection": self.name,
            "embedding_dim": self._embedding_dim,
            "encrypted_key_blob": self._store.get_key_blob(),
            "records": records,
        }

    def import_data(self, data: Dict[str, Any], overwrite: bool = False):
        """
        Import collections, records, and encryption keys from an exported dict.
        """
        if overwrite:
            self.clear()
        
        # Restore key blob
        if data.get("encrypted_key_blob"):
            self._store.store_key_blob(data["encrypted_key_blob"])
            if self._privacy_enabled and self._privacy:
                self._privacy.load_wrapped_key(data["encrypted_key_blob"])

        # Insert records directly
        self._store.import_all_records(data["records"])

    @property
    def is_graduated(self) -> bool:
        """True if the distiller has graduated (can bypass cloud)."""
        return bool(self._distiller and self._distiller.is_graduated)

    @property
    def privacy_enabled(self) -> bool:
        """True if privacy mode is active."""
        return self._privacy_enabled

    @staticmethod
    def _generate_id(document: str) -> str:
        """Generate a deterministic document ID from content hash + UUID suffix."""
        content_hash = hashlib.sha256(document.encode()).hexdigest()[:12]
        suffix = uuid.uuid4().hex[:8]
        return f"doc_{content_hash}_{suffix}"

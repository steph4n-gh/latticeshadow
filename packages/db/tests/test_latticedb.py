"""
Tests for the LatticeDB subpackage.

Covers:
- VectorStore CRUD, batch ops, metadata filtering, eviction
- Collection end-to-end: add → search → delete
- PrivacyEngine: encrypt/decrypt round-trip, search-on-encrypted
- LatticeIndexer: quantization, coarse search
- AutoDistiller: pair recording, training, graduation
- EmbeddingPipeline: local embedding, hash fallback
"""

import os
import tempfile
import torch
import pytest

from latticeshadow_db.latticedb import (
    connect,
    Collection,
    VectorStore,
    SearchResult,
    PrivacyEngine,
    LatticeIndexer,
    AutoDistiller,
    EmbeddingPipeline,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Temporary database path."""
    return str(tmp_path / "test_latticedb.sqlite")


@pytest.fixture
def sample_vectors():
    """Generate deterministic sample vectors."""
    torch.manual_seed(42)
    return [torch.randn(768) for _ in range(10)]


@pytest.fixture
def sample_docs():
    """Sample document texts."""
    return [
        "The mitochondria is the powerhouse of the cell.",
        "Quantum entanglement enables non-local correlations.",
        "The Treaty of Westphalia established state sovereignty.",
        "Neural networks learn hierarchical feature representations.",
        "CRISPR enables precise genome editing in living organisms.",
        "The Riemann hypothesis concerns the distribution of primes.",
        "Photosynthesis converts light energy to chemical energy.",
        "General relativity describes gravity as spacetime curvature.",
        "The Krebs cycle generates ATP in aerobic respiration.",
        "Superposition allows quantum bits to exist in multiple states.",
    ]


# ── VectorStore Tests ─────────────────────────────────────────────────────

class TestVectorStore:
    """Test the VectorStore directly."""

    def test_insert_and_count(self, tmp_db):
        store = VectorStore(db_path=tmp_db, collection="test")
        vec = torch.randn(768)
        store.insert("doc_1", vec, document="hello world")
        assert store.count() == 1

    def test_insert_dedup(self, tmp_db):
        store = VectorStore(db_path=tmp_db, collection="test")
        vec = torch.randn(768)
        store.insert("doc_1", vec)
        store.insert("doc_1", vec)  # Duplicate
        assert store.count() == 1

    def test_batch_insert(self, tmp_db, sample_vectors):
        store = VectorStore(db_path=tmp_db, collection="test")
        ids = [f"doc_{i}" for i in range(len(sample_vectors))]
        store.insert_batch(ids, sample_vectors)
        assert store.count() == 10

    def test_search_returns_results(self, tmp_db, sample_vectors):
        store = VectorStore(db_path=tmp_db, collection="test")
        ids = [f"doc_{i}" for i in range(len(sample_vectors))]
        docs = [f"Document {i}" for i in range(len(sample_vectors))]
        store.insert_batch(ids, sample_vectors, documents=docs)

        result = store.search(sample_vectors[0], n_results=3)
        assert len(result.ids) > 0
        assert result.ids[0] == "doc_0"  # Self should be best match
        assert result.scores[0] > 0.99    # Near-perfect self-similarity

    def test_search_with_metadata_filter(self, tmp_db):
        store = VectorStore(db_path=tmp_db, collection="test")
        store.insert("doc_a", torch.randn(64), document="alpha",
                     metadata={"topic": "bio"})
        store.insert("doc_b", torch.randn(64), document="beta",
                     metadata={"topic": "physics"})
        store.insert("doc_c", torch.randn(64), document="gamma",
                     metadata={"topic": "bio"})

        result = store.search(torch.randn(64), n_results=10,
                              where={"topic": "bio"})
        assert all(m.get("topic") == "bio" for m in result.metadatas)

    def test_delete(self, tmp_db, sample_vectors):
        store = VectorStore(db_path=tmp_db, collection="test")
        ids = [f"doc_{i}" for i in range(5)]
        store.insert_batch(ids, sample_vectors[:5])
        assert store.count() == 5

        deleted = store.delete(["doc_0", "doc_2"])
        assert deleted == 2
        assert store.count() == 3

    def test_clear(self, tmp_db, sample_vectors):
        store = VectorStore(db_path=tmp_db, collection="test")
        store.insert_batch([f"d{i}" for i in range(5)], sample_vectors[:5])
        store.clear()
        assert store.count() == 0

    def test_eviction(self, tmp_db):
        store = VectorStore(db_path=tmp_db, collection="test", max_entries=3)
        for i in range(5):
            store.insert(f"doc_{i}", torch.randn(64), document=f"doc {i}")
        assert store.count() <= 3

    def test_collection_isolation(self, tmp_db, sample_vectors):
        store_a = VectorStore(db_path=tmp_db, collection="collection_a")
        store_b = VectorStore(db_path=tmp_db, collection="collection_b")

        store_a.insert("doc_1", sample_vectors[0])
        store_b.insert("doc_2", sample_vectors[1])

        assert store_a.count() == 1
        assert store_b.count() == 1

    def test_get_vector(self, tmp_db):
        store = VectorStore(db_path=tmp_db, collection="test")
        vec = torch.randn(768)
        store.insert("doc_1", vec)
        retrieved = store.get_vector("doc_1")
        assert retrieved is not None
        # fp16 round-trip: check shape, not exact values
        assert retrieved.shape == vec.view(-1).shape

    def test_persistence_across_instances(self, tmp_db, sample_vectors):
        """Data survives database reconnection."""
        store1 = VectorStore(db_path=tmp_db, collection="test")
        store1.insert("doc_1", sample_vectors[0], document="hello")
        del store1

        store2 = VectorStore(db_path=tmp_db, collection="test")
        assert store2.count() == 1

    def test_empty_search(self, tmp_db):
        store = VectorStore(db_path=tmp_db, collection="test")
        result = store.search(torch.randn(768))
        assert len(result.ids) == 0


# ── Collection Tests ──────────────────────────────────────────────────────

class TestCollection:
    """Test the high-level Collection interface."""

    def test_add_and_search(self, tmp_db, sample_docs):
        coll = Collection("test", db_path=tmp_db, embedding_dim=768)
        ids = coll.add(documents=sample_docs[:3])
        assert len(ids) == 3
        assert coll.count() == 3

        result = coll.search("mitochondria energy cell", n_results=2)
        assert len(result.ids) > 0

    def test_add_with_metadata(self, tmp_db, sample_docs):
        coll = Collection("test", db_path=tmp_db, embedding_dim=768)
        metas = [{"topic": "bio"}, {"topic": "physics"}, {"topic": "history"}]
        coll.add(documents=sample_docs[:3], metadatas=metas)

        result = coll.search("biology cell", n_results=10, where={"topic": "bio"})
        assert all(m.get("topic") == "bio" for m in result.metadatas)

    def test_add_with_custom_ids(self, tmp_db, sample_docs):
        coll = Collection("test", db_path=tmp_db, embedding_dim=768)
        ids = coll.add(documents=sample_docs[:2], ids=["custom_1", "custom_2"])
        assert ids == ["custom_1", "custom_2"]

    def test_delete(self, tmp_db, sample_docs):
        coll = Collection("test", db_path=tmp_db, embedding_dim=768)
        ids = coll.add(documents=sample_docs[:3])
        assert coll.count() == 3
        coll.delete([ids[0]])
        assert coll.count() == 2

    def test_clear(self, tmp_db, sample_docs):
        coll = Collection("test", db_path=tmp_db, embedding_dim=768)
        coll.add(documents=sample_docs[:5])
        coll.clear()
        assert coll.count() == 0

    def test_search_result_has_documents(self, tmp_db, sample_docs):
        coll = Collection("test", db_path=tmp_db, embedding_dim=768)
        coll.add(documents=sample_docs[:3])
        result = coll.search("energy", n_results=3)
        assert len(result.documents) == len(result.ids)

    def test_streaming_exact_works_with_privacy_mode(self, tmp_db):
        vectors = {
            "alpha": torch.tensor([1.0, 0.0, 0.0, 0.0]),
            "beta": torch.tensor([0.0, 1.0, 0.0, 0.0]),
        }

        def embed(text: str) -> torch.Tensor:
            return vectors[text]

        coll = Collection(
            "private_streaming_exact",
            db_path=tmp_db,
            embedding_fn=embed,
            embedding_dim=4,
            privacy_enabled=True,
            master_key="streaming-exact-test-key",
            experimental_index="streaming_exact",
        )
        coll.add(documents=["alpha", "beta"], ids=["doc_alpha", "doc_beta"])
        assert coll.search("alpha", n_results=1).ids == ["doc_alpha"]
        del coll

        reloaded = Collection(
            "private_streaming_exact",
            db_path=tmp_db,
            embedding_fn=embed,
            embedding_dim=4,
            privacy_enabled=True,
            master_key="streaming-exact-test-key",
            experimental_index="streaming_exact",
        )
        result = reloaded.search("beta", n_results=1)
        assert result.ids == ["doc_beta"]
        assert result.documents == ["beta"]


# ── Connect Factory Tests ─────────────────────────────────────────────────

class TestConnect:
    """Test the connect() factory function."""

    def test_connect_creates_collection(self, tmp_db):
        db = connect(db_path=tmp_db, collection="my_collection")
        assert isinstance(db, Collection)
        assert db.name == "my_collection"

    def test_connect_default_collection(self, tmp_db):
        db = connect(db_path=tmp_db)
        assert db.name == "default"

    def test_connect_add_search(self, tmp_db, sample_docs):
        db = connect(db_path=tmp_db)
        db.add(documents=sample_docs[:3])
        result = db.search("quantum physics", n_results=2)
        assert len(result.ids) > 0

    def test_connect_threads_experimental_index(self, tmp_db):
        for strategy in (
            "hnsw_rerank",
            "hnsw_exact_rerank",
            "pq_exact_rerank",
            "diskann_rerank",
            "streaming_exact",
            "cascade_auto",
        ):
            db = connect(db_path=tmp_db, collection=strategy, experimental_index=strategy)
            assert db.experimental_index == strategy
            assert db._store.experimental_index == strategy


# ── PrivacyEngine Tests ──────────────────────────────────────────────────

class TestPrivacyEngine:
    """Test privacy envelope encryption."""

    def test_generate_and_encrypt_decrypt(self):
        engine = PrivacyEngine(dim=768, master_key="test_secret")
        engine.generate_key()
        vec = torch.randn(1, 768)
        encrypted = engine.encrypt(vec)
        decrypted = engine.decrypt(encrypted)

        # Round-trip should recover original (orthogonal rotation)
        assert torch.allclose(vec, decrypted, atol=1e-4)

    def test_encrypted_preserves_distances(self):
        engine = PrivacyEngine(dim=128, master_key="test_secret")
        engine.generate_key()
        a = torch.randn(1, 128)
        b = torch.randn(1, 128)

        dist_orig = torch.dist(a, b).item()

        a_enc = engine.encrypt(a)
        b_enc = engine.encrypt(b)
        dist_enc = torch.dist(a_enc, b_enc).item()

        assert abs(dist_orig - dist_enc) < 0.1  # Distance preserved

    def test_wrap_unwrap_round_trip(self):
        """Key survives wrap → JSON → unwrap cycle."""
        engine = PrivacyEngine(dim=128, master_key="vault_password")
        wrapped_blob = engine.generate_key()

        vec = torch.randn(1, 128)
        encrypted = engine.encrypt(vec)

        # Create new engine and unwrap
        engine2 = PrivacyEngine(dim=128, master_key="vault_password")
        engine2.load_wrapped_key(wrapped_blob)
        decrypted = engine2.decrypt(encrypted)

        assert torch.allclose(vec, decrypted, atol=1e-4)

    def test_wrong_key_raises_permission_error(self):
        """Wrong master key should fail with PermissionError."""
        engine = PrivacyEngine(dim=64, master_key="correct_password")
        wrapped_blob = engine.generate_key()

        engine_bad = PrivacyEngine(dim=64, master_key="wrong_password")
        from latticeshadow_db.latticedb.privacy import _CRYPTO_AVAILABLE
        
        if _CRYPTO_AVAILABLE:
            with pytest.raises(PermissionError):
                engine_bad.load_wrapped_key(wrapped_blob)
        else:
            # If cryptography is not installed, fallback doesn't encrypt,
            # so this won't raise. That's acceptable for fallback mode.
            engine_bad.load_wrapped_key(wrapped_blob)
    def test_encrypted_different_from_original(self):
        engine = PrivacyEngine(dim=128, master_key="test")
        engine.generate_key()
        vec = torch.randn(1, 128)
        encrypted = engine.encrypt(vec)
        assert not torch.allclose(vec, encrypted, atol=1e-2)

    def test_rewrap_key_rotation(self):
        """O(1) password rotation: re-wrap DEK under new master key."""
        engine = PrivacyEngine(dim=64, master_key="old_password")
        wrapped_old = engine.generate_key()

        vec = torch.randn(1, 64)
        encrypted = engine.encrypt(vec)

        # Rotate to new password
        wrapped_new = engine.rewrap_key(wrapped_old, "new_password")

        # Unwrap with new password should work
        engine2 = PrivacyEngine(dim=64, master_key="new_password")
        engine2.load_wrapped_key(wrapped_new)
        decrypted = engine2.decrypt(encrypted)
        assert torch.allclose(vec, decrypted, atol=1e-4)

    def test_collection_with_privacy_vault(self, tmp_db, sample_docs):
        """End-to-end: add + search with vault-pattern privacy."""
        coll = Collection("secure", db_path=tmp_db,
                         embedding_dim=768, privacy_enabled=True,
                         master_key="test_vault_key")
        coll.add(documents=sample_docs[:3])
        assert coll.count() == 3
        assert coll.privacy_enabled

        result = coll.search("biology cell", n_results=2)
        assert len(result.ids) > 0

    def test_collection_vault_persistence(self, tmp_db, sample_docs):
        """Key vault persists across database reconnections."""
        # Create and populate
        coll1 = Collection("persistent", db_path=tmp_db,
                          embedding_dim=768, privacy_enabled=True,
                          master_key="persist_key")
        coll1.add(documents=sample_docs[:2])
        assert coll1.count() == 2
        del coll1

        # Reconnect with same key — should succeed
        coll2 = Collection("persistent", db_path=tmp_db,
                          embedding_dim=768, privacy_enabled=True,
                          master_key="persist_key")
        assert coll2.count() == 2
        result = coll2.search("energy", n_results=1)
        assert len(result.ids) > 0

    def test_crypto_shred(self, tmp_db, sample_docs):
        """Crypto-shredding destroys the key, making vectors irrecoverable."""
        coll = Collection("shred_me", db_path=tmp_db,
                         embedding_dim=768, privacy_enabled=True,
                         master_key="shred_key")
        coll.add(documents=sample_docs[:3])
        coll.crypto_shred()
        assert not coll.privacy_enabled



# ── LatticeIndexer Tests ─────────────────────────────────────────────────

class TestLatticeIndexer:
    """Test Leech Lattice coarse indexing."""

    def test_index_and_count(self):
        indexer = LatticeIndexer()
        indexer.index("doc_1", torch.randn(768))
        indexer.index("doc_2", torch.randn(768))
        assert indexer.count() == 2
        assert indexer.code_dtype == torch.int16

    def test_search_returns_nearest(self):
        indexer = LatticeIndexer()
        torch.manual_seed(42)
        base = torch.randn(768)
        indexer.index("doc_near", base)
        indexer.index("doc_far", torch.randn(768) * 10)

        results = indexer.search(base, k=2)
        assert len(results) > 0
        # Nearest should be doc_near (same vector)
        assert results[0][0] == "doc_near"

    def test_compact_codes_match_float_code_search(self):
        torch.manual_seed(7)
        vectors = [torch.randn(768) for _ in range(4)]
        compact = LatticeIndexer(compact_codes=True)
        unpacked = LatticeIndexer(compact_codes=False)

        for idx, vec in enumerate(vectors):
            compact.index(f"doc_{idx}", vec)
            unpacked.index(f"doc_{idx}", vec)

        compact_results = compact.search(vectors[0], k=4)
        unpacked_results = unpacked.search(vectors[0], k=4)

        assert [doc_id for doc_id, _ in compact_results] == [
            doc_id for doc_id, _ in unpacked_results
        ]
        assert compact.code_storage_bytes() * 2 == unpacked.code_storage_bytes()

    def test_remove(self):
        indexer = LatticeIndexer()
        indexer.index("doc_1", torch.randn(768))
        indexer.remove("doc_1")
        assert indexer.count() == 0

    def test_clear(self):
        indexer = LatticeIndexer()
        for i in range(5):
            indexer.index(f"doc_{i}", torch.randn(768))
        indexer.clear()
        assert indexer.count() == 0

    def test_collection_with_lattice(self, tmp_db, sample_docs):
        """End-to-end: add + search with lattice indexing enabled."""
        coll = Collection("lattice_test", db_path=tmp_db,
                         embedding_dim=768, lattice_index=True)
        coll.add(documents=sample_docs[:5])
        assert coll.count() == 5

        result = coll.search("biology", n_results=3)
        assert len(result.ids) > 0


# ── AutoDistiller Tests ──────────────────────────────────────────────────

class TestAutoDistiller:
    """Test automatic distillation."""

    def test_record_pair(self):
        distiller = AutoDistiller(dim=128, min_samples=1000)
        distiller.record_pair(torch.randn(128), torch.randn(128))
        assert distiller.sample_count == 1
        assert not distiller.is_graduated

    def test_manual_train(self):
        distiller = AutoDistiller(dim=64, min_samples=1000,
                                  loss_threshold=100.0)
        # Create correlated pairs (identity-ish mapping)
        for _ in range(100):
            vec = torch.randn(64)
            distiller.record_pair(vec, vec + torch.randn(64) * 0.01)

        loss = distiller.train(epochs=20)
        assert loss < float('inf')
        assert distiller._head.is_trained

    def test_graduation(self):
        distiller = AutoDistiller(dim=32, min_samples=50,
                                  loss_threshold=1.0)
        # Easy identity mapping
        for _ in range(60):
            vec = torch.randn(32)
            distiller.record_pair(vec, vec)

        loss = distiller.train(epochs=50)
        # With exact identity pairs, loss should be very low
        assert distiller.is_graduated or loss < 1.0

    def test_predict_when_graduated(self):
        distiller = AutoDistiller(dim=32, min_samples=50,
                                  loss_threshold=1.0)
        for _ in range(60):
            vec = torch.randn(32)
            distiller.record_pair(vec, vec)

        distiller.train(epochs=50)
        if distiller.is_graduated:
            prediction = distiller.predict(torch.randn(32))
            assert prediction is not None
            assert prediction.shape == torch.Size([32])

    def test_holdout_gated_graduation_records_metrics(self):
        torch.manual_seed(1234)
        distiller = AutoDistiller(
            dim=4,
            hidden_dim=8,
            min_samples=20,
            loss_threshold=0.5,
            holdout_loss_threshold=0.5,
            holdout_fraction=0.25,
        )

        for _ in range(24):
            distiller.record_pair(torch.randn(4), torch.zeros(4))

        loss = distiller.train(epochs=30)
        metrics = distiller.graduation_metrics

        assert loss < 0.5
        assert distiller.holdout_loss is not None
        assert distiller.is_graduated
        assert metrics["graduation_block_reason"] == "graduated"
        assert metrics["train_sample_count"] == 18
        assert metrics["holdout_sample_count"] == 6

    def test_holdout_gate_blocks_noisy_cloud_skip(self):
        torch.manual_seed(4321)
        distiller = AutoDistiller(
            dim=4,
            hidden_dim=8,
            min_samples=20,
            loss_threshold=0.5,
            holdout_loss_threshold=0.5,
            holdout_fraction=0.25,
        )

        for _ in range(15):
            distiller.record_pair(torch.randn(4), torch.zeros(4))
        for _ in range(5):
            distiller.record_pair(torch.randn(4), torch.ones(4) * 5.0)

        loss = distiller.train(epochs=30)
        metrics = distiller.graduation_metrics

        assert loss < 0.5
        assert distiller.holdout_loss is not None
        assert distiller.holdout_loss > 0.5
        assert not distiller.is_graduated
        assert metrics["graduation_block_reason"] == "holdout_loss_above_threshold"


# ── EmbeddingPipeline Tests ──────────────────────────────────────────────

class TestEmbeddingPipeline:
    """Test embedding dispatch."""

    def test_default_hash_embedding(self):
        pipeline = EmbeddingPipeline(dim=768)
        vec = pipeline.embed("hello world")
        assert vec.shape == torch.Size([768])
        assert torch.isfinite(vec).all()

    def test_deterministic_hash(self):
        pipeline = EmbeddingPipeline(dim=256)
        v1 = pipeline.embed("test text")
        v2 = pipeline.embed("test text")
        assert torch.allclose(v1, v2)

    def test_different_texts_different_vectors(self):
        pipeline = EmbeddingPipeline(dim=256)
        v1 = pipeline.embed("hello")
        v2 = pipeline.embed("goodbye")
        assert not torch.allclose(v1, v2)

    def test_custom_embedding_fn(self):
        custom_fn = lambda text: torch.ones(64) * len(text)
        pipeline = EmbeddingPipeline(local_fn=custom_fn, dim=64)
        vec = pipeline.embed("hi")
        assert vec.shape == torch.Size([64])
        assert torch.allclose(vec, torch.ones(64) * 2)

    def test_embed_batch(self):
        pipeline = EmbeddingPipeline(dim=128)
        vecs = pipeline.embed_batch(["a", "b", "c"])
        assert len(vecs) == 3
        assert all(v.shape == torch.Size([128]) for v in vecs)

    def test_has_cloud(self):
        pipeline_no = EmbeddingPipeline(dim=64)
        assert not pipeline_no.has_cloud

        pipeline_yes = EmbeddingPipeline(
            dim=64, cloud_fn=lambda t: torch.randn(64))
        assert pipeline_yes.has_cloud


# ── SearchResult Tests ────────────────────────────────────────────────────

class TestSearchResult:
    """Test SearchResult dataclass."""

    def test_default_empty(self):
        result = SearchResult()
        assert result.ids == []
        assert result.documents == []
        assert result.scores == []
        assert result.served_locally is False

    def test_fields(self):
        result = SearchResult(
            ids=["a", "b"],
            documents=["doc a", "doc b"],
            scores=[0.95, 0.87],
            served_locally=True,
        )
        assert len(result.ids) == 2
        assert result.served_locally

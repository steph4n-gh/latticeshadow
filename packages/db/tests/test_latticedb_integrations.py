"""
Integration tests for LatticeDB integrations.

Covers:
- EmbeddingPipeline factory methods (from_model, from_openai)
- LatticeDBVectorStore (LangChain wrapper)
- LatticeDBLlamaIndexStore (LlamaIndex wrapper)
"""

import sys
import os
import torch
import numpy as np
import pytest
from unittest.mock import MagicMock, patch

# Setup mocked modules BEFORE importing integrations to simulate external packages presence
mock_doc = MagicMock()
mock_vs = MagicMock()
mock_emb = MagicMock()

# Setup class bases
class MockVectorStore:
    def __init__(self, *args, **kwargs):
        pass
    def as_retriever(self, *args, **kwargs):
        return "mock_retriever"

class MockEmbeddings:
    pass

class MockDocument:
    def __init__(self, page_content, metadata=None):
        self.page_content = page_content
        self.metadata = metadata or {}

mock_vs.VectorStore = MockVectorStore
mock_emb.Embeddings = MockEmbeddings
mock_doc.Document = MockDocument

sys.modules["langchain_core.vectorstores"] = mock_vs
sys.modules["langchain_core.embeddings"] = mock_emb
sys.modules["langchain_core.documents"] = mock_doc

# Setup sentence_transformers mock
mock_st = MagicMock()
mock_st_class = MagicMock()
mock_st.SentenceTransformer = mock_st_class
sys.modules["sentence_transformers"] = mock_st

# Force reload integrations if it has already been imported
import importlib
if "latticeshadow_db.latticedb.integrations" in sys.modules:
    importlib.reload(sys.modules["latticeshadow_db.latticedb.integrations"])

from latticeshadow_db.latticedb.integrations import LatticeDBVectorStore, LatticeDBLlamaIndexStore
from latticeshadow_db.latticedb import (
    connect,
    Collection,
    EmbeddingPipeline,
)


# ── EmbeddingPipeline integration tests ─────────────────────────────────────

class TestEmbeddingPipelineIntegration:
    """Test factory methods of the EmbeddingPipeline."""

    def test_from_model(self):
        # Setup mock model behavior
        mock_model = MagicMock()
        
        # side_effect to handle both single and list inputs
        def mock_encode(inputs, **kwargs):
            if isinstance(inputs, str):
                return np.ones(384)
            return np.ones((len(inputs), 384))
            
        mock_model.encode.side_effect = mock_encode
        mock_st_class.return_value = mock_model

        pipeline = EmbeddingPipeline.from_model("all-MiniLM-L6-v2")
        assert pipeline.dim == 384
        assert hasattr(pipeline, "_model_instance")

        # Test embed
        vec = pipeline.embed("hello")
        assert vec.shape == torch.Size([384])
        assert torch.allclose(vec, torch.ones(384))

        # Test embed_batch
        vecs = pipeline.embed_batch(["hello", "world"])
        assert len(vecs) == 2
        assert vecs[0].shape == torch.Size([384])

    @patch("urllib.request.urlopen")
    def test_from_openai(self, mock_urlopen):
        # Setup mock OpenAI HTTP response
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"data": [{"embedding": [0.1, 0.2, 0.3]}]}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        pipeline = EmbeddingPipeline.from_openai(api_key="sk-test", model_name="text-embedding-3-small")
        assert pipeline.dim == 1536

        vec = pipeline.embed("hello")
        assert vec is not None
        assert vec.shape == torch.Size([3])


# ── LangChain integration tests ─────────────────────────────────────────────

class TestLangChainIntegration:
    """Test the LangChain VectorStore wrapper."""

    def test_vector_store_methods(self, tmp_path):
        db_path = str(tmp_path / "langchain_test.sqlite")
        
        # Setup LangChain Embeddings Mock
        mock_embeddings = MagicMock()
        mock_embeddings.embed_query.return_value = [0.1] * 768

        # Mock the document constructor inside mock_doc
        store = LatticeDBVectorStore.from_texts(
            texts=["LangChain is awesome.", "Vector stores are handy."],
            embedding=mock_embeddings,
            db_path=db_path,
            collection_name="langchain_col",
        )

        assert store.embeddings == mock_embeddings

        # Search
        results = store.similarity_search("LangChain", k=2)
        assert len(results) == 2
        assert results[0].page_content == "LangChain is awesome."


# ── LlamaIndex integration tests ────────────────────────────────────────────

class TestLlamaIndexIntegration:
    """Test the LlamaIndex VectorStore wrapper."""

    def test_llama_index_methods(self, tmp_path):
        db_path = str(tmp_path / "llamaindex_test.sqlite")
        collection = connect(db_path=db_path, collection="llama_col")
        store = LatticeDBLlamaIndexStore(collection)

        # Mock Node objects
        mock_node_1 = MagicMock()
        mock_node_1.get_content.return_value = "LlamaIndex integration"
        mock_node_1.metadata = {"tag": "llm"}
        mock_node_1.node_id = "node_a"

        mock_node_2 = MagicMock()
        mock_node_2.get_content.return_value = "Second node"
        mock_node_2.metadata = {"tag": "docs"}
        mock_node_2.node_id = "node_b"

        # Add nodes
        ids = store.add([mock_node_1, mock_node_2])
        assert ids == ["node_a", "node_b"]
        assert collection.count() == 2

        # Query mock
        mock_query = MagicMock()
        mock_query.similarity_top_k = 1
        mock_query.query_str = "LlamaIndex"

        result = store.query(mock_query)
        assert len(result.ids) == 1
        assert result.ids[0] == "node_a"

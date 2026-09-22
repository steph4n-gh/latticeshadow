"""
integrations.py — LangChain and LlamaIndex adapters for LatticeDB.
"""

import torch
from typing import Any, Iterable, List, Optional, Tuple, Type, Dict

# ── LangChain Integration ──────────────────────────────────────────────────

try:
    from langchain_core.vectorstores import VectorStore
    from langchain_core.embeddings import Embeddings
    from langchain_core.documents import Document
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    # Fallback placeholders to prevent import errors when LangChain is not installed
    class VectorStore: pass
    class Embeddings: pass
    class Document: pass
    _LANGCHAIN_AVAILABLE = False


class LatticeDBVectorStore(VectorStore):
    """
    LangChain VectorStore implementation for LatticeDB.
    """

    def __init__(self, collection: Any, embeddings: Optional[Embeddings] = None):
        """
        Args:
            collection: The LatticeDB Collection instance.
            embeddings: Optional LangChain Embeddings instance. If provided,
                        overrides the collection's internal embedding pipeline.
        """
        if not _LANGCHAIN_AVAILABLE:
            raise ImportError(
                "langchain-core is required to use LatticeDBVectorStore. "
                "Install it with: pip install langchain-core"
            )
        self._collection = collection
        self._embeddings = embeddings

        # If LangChain embeddings are provided, wrap and set as collection's embedding function
        if embeddings is not None:
            def local_fn(text: str) -> torch.Tensor:
                return torch.tensor(embeddings.embed_query(text), dtype=torch.float32)
            self._collection._embedder._local_fn = local_fn

    @property
    def embeddings(self) -> Optional[Embeddings]:
        return self._embeddings

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Add texts and metadatas to the store."""
        return self._collection.add(documents=list(texts), metadatas=metadatas)

    def similarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Document]:
        """Search for similar documents."""
        result = self._collection.search(query, n_results=k)
        docs = []
        for doc, meta in zip(result.documents, result.metadatas):
            docs.append(Document(page_content=doc, metadata=meta))
        return docs

    def similarity_search_with_score(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        """Search and return with relevance scores."""
        result = self._collection.search(query, n_results=k)
        pairs = []
        for doc, meta, score in zip(result.documents, result.metadatas, result.scores):
            pairs.append((Document(page_content=doc, metadata=meta), score))
        return pairs

    @classmethod
    def from_texts(
        cls: Type["LatticeDBVectorStore"],
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        db_path: str = "latticedb.sqlite",
        collection_name: str = "default",
        privacy: bool = False,
        master_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "LatticeDBVectorStore":
        """Initialize from texts and embeddings."""
        import latticeshadow_db.latticedb as latticedb

        def embedding_fn(text: str) -> torch.Tensor:
            return torch.tensor(embedding.embed_query(text), dtype=torch.float32)

        collection = latticedb.connect(
            db_path=db_path,
            collection=collection_name,
            embedding_fn=embedding_fn,
            privacy=privacy,
            master_key=master_key,
            **kwargs
        )
        vector_store = cls(collection, embedding)
        vector_store.add_texts(texts, metadatas=metadatas)
        return vector_store


# ── LlamaIndex Integration ─────────────────────────────────────────────────

try:
    from llama_index.core.vector_stores.types import BaseOutputParser
    # Basic LlamaIndex structure imports
    _LLAMAINDEX_AVAILABLE = True
except ImportError:
    _LLAMAINDEX_AVAILABLE = False


class LatticeDBLlamaIndexStore:
    """
    LlamaIndex VectorStore implementation for LatticeDB.
    """

    def __init__(self, collection: Any):
        self._collection = collection

    @property
    def client(self) -> Any:
        return self._collection

    def add(self, nodes: List[Any]) -> List[str]:
        """Add nodes to the index."""
        docs = [node.get_content() for node in nodes]
        metadatas = [node.metadata for node in nodes]
        ids = [node.node_id for node in nodes]
        return self._collection.add(documents=docs, metadatas=metadatas, ids=ids)

    def delete(self, ref_doc_id: str, **delete_kwargs: Any):
        """Delete nodes by reference document ID."""
        self._collection.delete([ref_doc_id])

    def query(self, query: Any, **kwargs: Any) -> Any:
        """Query the index."""
        # query is a VectorStoreQuery object in LlamaIndex
        k = getattr(query, "similarity_top_k", 4)
        query_str = getattr(query, "query_str", "")
        
        result = self._collection.search(query_str, n_results=k)

        # LlamaIndex expects a VectorStoreQueryResult
        try:
            from llama_index.core.vector_stores import VectorStoreQueryResult
        except ImportError:
            # Fallback mock result class if not fully installed
            class VectorStoreQueryResult:
                def __init__(self, nodes, similarities, ids):
                    self.nodes = nodes
                    self.similarities = similarities
                    self.ids = ids

        nodes = []
        try:
            from llama_index.core.schema import TextNode
            for doc, meta in zip(result.documents, result.metadatas):
                nodes.append(TextNode(text=doc, metadata=meta))
        except ImportError:
            nodes = result.documents

        return VectorStoreQueryResult(
            nodes=nodes,
            similarities=result.scores,
            ids=result.ids,
        )

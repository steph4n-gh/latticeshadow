"""
EmbeddingPipeline — Pluggable text-to-vector embedding dispatch.

Provides:
- Pluggable local embedding function (any callable text → Tensor)
- Optional cloud embedding with Procrustes alignment
- Default fallback using simple hash-based embedding for testing
- SentenceTransformer model loading factory
- OpenAI embeddings API integration
"""

import torch
import hashlib
import logging
from typing import Callable, Optional, List, Any

logger = logging.getLogger("latticeshadow_db.latticedb.embedder")

# Type alias for embedding functions
EmbeddingFn = Callable[[str], torch.Tensor]


def _default_hash_embedding(text: str, dim: int = 768) -> torch.Tensor:
    """
    Deterministic hash-based embedding for testing/fallback.
    Produces a unit-normalized vector from text content.
    NOT semantically meaningful — use a real model in production.
    """
    h = hashlib.sha256(text.encode()).digest()
    # Extend hash to fill dimension
    extended = b""
    for i in range((dim * 4 // len(h)) + 1):
        extended += hashlib.sha256(h + i.to_bytes(4, 'big')).digest()
    values = []
    for i in range(dim):
        byte_val = extended[i * 4] | (extended[i * 4 + 1] << 8)
        values.append((byte_val / 65535.0) * 2.0 - 1.0)
    vec = torch.tensor(values, dtype=torch.float32)
    return vec / (vec.norm() + 1e-8)


class EmbeddingPipeline:
    """
    Dispatches text → vector conversion.
    Supports pluggable local and cloud embedding functions.
    """

    def __init__(self, local_fn: Optional[EmbeddingFn] = None,
                 cloud_fn: Optional[EmbeddingFn] = None,
                 dim: int = 768):
        """
        Args:
            local_fn: Function that takes text and returns a Tensor.
                      If None, uses a deterministic hash-based fallback.
            cloud_fn: Optional cloud embedding function for alignment training.
            dim: Embedding dimension (used for default hash embedding).
        """
        self.dim = dim
        self._local_fn = local_fn or (lambda text: _default_hash_embedding(text, dim))
        self._cloud_fn = cloud_fn

    @classmethod
    def from_model(cls, model_name: str, **kwargs) -> "EmbeddingPipeline":
        """
        Create an EmbeddingPipeline using a local HuggingFace/SentenceTransformer model.
        """
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(
                "sentence-transformers is required to use from_model(). "
                "Install it with: pip install sentence-transformers"
            ) from e
        model = SentenceTransformer(model_name, **kwargs)
        
        # Get actual model dimension
        dummy_emb = model.encode("test")
        dim = dummy_emb.shape[0]

        def local_fn(text: str) -> torch.Tensor:
            return torch.tensor(model.encode(text), dtype=torch.float32)

        pipeline = cls(local_fn=local_fn, dim=dim)
        pipeline._model_instance = model
        return pipeline

    @classmethod
    def from_openai(cls, api_key: str, model_name: str = "text-embedding-3-small") -> "EmbeddingPipeline":
        """
        Create an EmbeddingPipeline using OpenAI's Embeddings API.
        Does not require the 'openai' library (uses standard library urllib).
        """
        # Determine dimension based on model name
        dim = 1536
        if "3-large" in model_name:
            dim = 3072
        elif "ada" in model_name:
            dim = 1536

        def local_fn(text: str) -> torch.Tensor:
            import urllib.request
            import json
            url = "https://api.openai.com/v1/embeddings"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            data = json.dumps({
                "input": text,
                "model": model_name,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req) as response:
                res = json.loads(response.read().decode("utf-8"))
                embedding = res["data"][0]["embedding"]
                return torch.tensor(embedding, dtype=torch.float32)

        return cls(local_fn=local_fn, dim=dim)

    def embed(self, data: Any) -> torch.Tensor:
        """Embed text, images, or pre-computed tensors."""
        import os
        if isinstance(data, torch.Tensor):
            return data.detach()
        
        # If input is an image path, load PIL Image if available
        if isinstance(data, str) and data.endswith((".png", ".jpg", ".jpeg", ".webp")):
            try:
                from PIL import Image
                if os.path.exists(data):
                    data = Image.open(data)
            except ImportError:
                pass

        vec = self._local_fn(data)
        if not isinstance(vec, torch.Tensor):
            vec = torch.tensor(vec, dtype=torch.float32)
        return vec.detach()

    def embed_batch(self, data_list: List[Any]) -> List[torch.Tensor]:
        """Embed multiple inputs efficiently."""
        if hasattr(self, "_model_instance"):
            embeddings = self._model_instance.encode(data_list)
            return [torch.tensor(e, dtype=torch.float32) for e in embeddings]
        return [self.embed(item) for item in data_list]

    def embed_cloud(self, data: Any) -> Optional[torch.Tensor]:
        """
        Embed using the cloud function (if configured).
        Returns None if no cloud function is set.
        """
        if self._cloud_fn is None:
            return None
        vec = self._cloud_fn(data)
        if not isinstance(vec, torch.Tensor):
            vec = torch.tensor(vec, dtype=torch.float32)
        return vec.detach()

    @property
    def has_cloud(self) -> bool:
        """True if a cloud embedding function is configured."""
        return self._cloud_fn is not None

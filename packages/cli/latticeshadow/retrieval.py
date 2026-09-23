"""Ephemeral ranking over canonical, decrypted events.

The caller owns vault revision checks, scope filtering and canonical hydration.
This index holds plaintext and embeddings in process memory only. No term list or
model vector is persisted by this module.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np


_TOKENS = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_STOP = frozenset("a an and are as at be by can did do does for from how i in is it my of on or our should the this to was we were what when where which who why with".split())


def _words(text: str) -> frozenset[str]:
    return frozenset(word for word in _TOKENS.findall(text.lower()) if word not in _STOP)


def _default_encode(texts: Sequence[str]) -> np.ndarray:
    from latticeshadow.vaults import EMBEDDING_DIM, _local_model, embed_text
    import os

    if os.environ.get("LATTICESHADOW_EMBEDDING_MODEL") == "hash":
        return np.asarray([embed_text(text).numpy() for text in texts], dtype=np.float32)
    return np.asarray(
        _local_model().encode(list(texts), normalize_embeddings=True, truncate_dim=EMBEDDING_DIM),
        dtype=np.float32,
    )


class RetrievalIndex:
    """Revision-aware in-memory lexical and exact-vector index.

    `refresh` accepts all canonical events. The caller must pass only eligible
    IDs to `rank`; filtering by project/source/time happens before either score.
    Repeated refreshes reuse embeddings of unchanged ID/text pairs.
    """

    def __init__(self, encode: Callable[[Sequence[str]], np.ndarray] | None = None):
        self._encode = encode or _default_encode
        self.revision: int | None = None
        self._events: dict[str, str] = {}
        self._tokens: dict[str, frozenset[str]] = {}
        self._vectors: dict[str, np.ndarray] = {}

    def clear(self) -> None:
        """Drop cached plaintext and vectors, for policy revocation or shutdown."""
        self._events.clear()
        self._tokens.clear()
        self._vectors.clear()
        self.revision = None

    def refresh(self, events: Iterable[Mapping[str, Any]], revision: int) -> None:
        if self.revision == revision:
            return
        rows = list(events)
        incoming = {str(row["id"]): str(row["text"]) for row in rows}
        if len(incoming) != len(rows):
            raise ValueError("duplicate event ID in canonical scan")
        changed = [doc_id for doc_id, text in incoming.items() if self._events.get(doc_id) != text]
        vectors = {doc_id: self._vectors[doc_id] for doc_id, text in incoming.items()
                   if self._events.get(doc_id) == text}
        if changed:
            encoded = np.asarray(self._encode([incoming[doc_id] for doc_id in changed]), dtype=np.float32)
            if encoded.ndim != 2 or encoded.shape[0] != len(changed):
                raise ValueError("embedding function returned an unexpected shape")
            if not np.isfinite(encoded).all():
                raise ValueError("embedding function returned a nonfinite vector")
            for doc_id, vector in zip(changed, encoded):
                norm = float(np.linalg.norm(vector))
                if norm <= 0:
                    raise ValueError("embedding function returned a zero vector")
                vectors[doc_id] = vector / norm
        tokens = {doc_id: _words(text) for doc_id, text in incoming.items()}
        self._vectors = vectors
        self._tokens = tokens
        self._events = incoming
        self.revision = revision

    def rank(self, query: str, candidate_ids: Iterable[str], limit: int = 10) -> list[tuple[str, float]]:
        if not query.strip():
            return []
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        ids = list(dict.fromkeys(candidate_ids))
        if not ids:
            return []
        missing = [doc_id for doc_id in ids if doc_id not in self._events]
        if missing:
            raise ValueError("candidate ID missing from current retrieval index")
        qvector = np.asarray(self._encode([query]), dtype=np.float32)
        if qvector.ndim != 2 or qvector.shape[0] != 1 or qvector.shape[1] != len(self._vectors[ids[0]]):
            raise ValueError("query embedding has an unexpected shape")
        query_norm = float(np.linalg.norm(qvector[0]))
        if not np.isfinite(qvector).all() or query_norm <= 0:
            raise ValueError("query embedding is invalid")
        similarities = [float(np.dot(self._vectors[doc_id], qvector[0] / query_norm)) for doc_id in ids]
        dense_order = sorted(range(len(ids)), key=lambda i: (-similarities[i], ids[i]))
        # Scope-local IDF: excluded documents do not influence lexical ranking.
        df = Counter(token for doc_id in ids for token in self._tokens[doc_id])
        qtokens = _words(query)
        lexical = {
            doc_id: sum(math.log((len(ids) + 1) / (df[token] + 1)) + 1 for token in qtokens & self._tokens[doc_id])
            for doc_id in ids
        }
        lexical_order = sorted((doc_id for doc_id in ids if lexical[doc_id] > 0),
                               key=lambda doc_id: (-lexical[doc_id], doc_id))
        scores = {ids[index]: 1 / (60 + rank) for rank, index in enumerate(dense_order, 1)}
        for rank, doc_id in enumerate(lexical_order, 1):
            scores[doc_id] += 1 / (60 + rank)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]

import pytest
import numpy as np
import torch
from unittest.mock import MagicMock

tda_module = pytest.importorskip(
    "latticeshadow.tda",
    reason="TopologicalLoopDetector lives in the downstream latticeshadow-cli package.",
)
TopologicalLoopDetector = tda_module.TopologicalLoopDetector

class TestTDALoopDetector:
    def test_loop_detection_no_loop(self):
        # 1. Generate 5 random embeddings (no structure)
        np.random.seed(42)
        embeddings = [np.random.randn(128) for _ in range(6)]
        for i in range(len(embeddings)):
            embeddings[i] = embeddings[i] / np.linalg.norm(embeddings[i])

        detector = TopologicalLoopDetector(max_history=6, distance_threshold=0.3)
        dist_matrix = detector.calculate_distance_matrix(embeddings)
        intervals = detector.compute_persistence_intervals(dist_matrix)

        # Random embeddings should not have highly persistent 1-cycles
        has_persistent_loop = False
        for birth, death, u, v in intervals:
            if death - birth >= 0.3:
                has_persistent_loop = True
        assert not has_persistent_loop

    def test_loop_detection_with_perfect_loop(self):
        # 2. Generate a clean loop: A -> B -> C -> D -> A -> B -> C -> D
        # Using points on a 2D unit circle
        a = np.array([1.0, 0.0])
        b = np.array([0.0, 1.0])
        c = np.array([-1.0, 0.0])
        d = np.array([0.0, -1.0])

        embeddings = [a, b, c, d, a, b, c, d]

        detector = TopologicalLoopDetector(max_history=8, distance_threshold=0.3)
        dist_matrix = detector.calculate_distance_matrix(embeddings)
        intervals = detector.compute_persistence_intervals(dist_matrix)

        # There should be a 1-cycle that is born and persists (death is 2.0, birth is 1.0)
        has_persistent_loop = False
        for birth, death, u, v in intervals:
            span = death - birth
            if span >= 0.3:
                has_persistent_loop = True
        assert has_persistent_loop

    def test_detect_loop_vault_integration(self):
        # Mock vault
        vault = MagicMock()
        vault.name = "clipboard"
        
        # Mock SQLite connection
        conn = MagicMock()
        vault._store._connect.return_value.__enter__.return_value = conn
        
        # 8 documents (loop: A, B, C, D, A, B, C, D)
        a = np.array([1.0, 0.0])
        b = np.array([0.0, 1.0])
        c = np.array([-1.0, 0.0])
        d = np.array([0.0, -1.0])
        
        # Mock blob conversion
        def mock_blob_to_vector(blob):
            idx = int(blob.decode())
            vecs = [a, b, c, d, a, b, c, d]
            return torch.from_numpy(vecs[idx])
        vault._store._blob_to_vector = mock_blob_to_vector

        # Mock query return
        conn.execute.return_value.fetchall.return_value = [
            ("clip_1", "doc1", b"0"),
            ("clip_2", "doc2", b"1"),
            ("clip_3", "doc3", b"2"),
            ("clip_4", "doc4", b"3"),
            ("clip_5", "doc1", b"4"),
            ("clip_6", "doc2", b"5"),
            ("clip_7", "doc3", b"6"),
            ("clip_8", "doc4", b"7"),
        ]

        detector = TopologicalLoopDetector(max_history=8, distance_threshold=0.3)
        is_loop, loop_docs = detector.detect_loop(vault)
        assert is_loop is True
        assert len(loop_docs) > 0
        assert all(doc in ["doc1", "doc2", "doc3", "doc4"] for doc in loop_docs)

import numpy as np
import os
import json
from datetime import datetime
from typing import List, Tuple, Dict, Set

class TopologicalLoopDetector:
    def __init__(self, max_history: int = 20, distance_threshold: float = 0.3):
        """
        Args:
            max_history: Max number of recent documents/commands to analyze.
            distance_threshold: Threshold to determine if a cycle persists.
        """
        self.max_history = max_history
        self.distance_threshold = distance_threshold

    def calculate_distance_matrix(self, embeddings: List[np.ndarray]) -> np.ndarray:
        """Calculate pairwise cosine distances."""
        n = len(embeddings)
        dist_matrix = np.zeros((n, n))
        for i in range(n):
            for j in range(i + 1, n):
                norm_i = np.linalg.norm(embeddings[i])
                norm_j = np.linalg.norm(embeddings[j])
                if norm_i == 0 or norm_j == 0:
                    sim = 0.0
                else:
                    sim = np.dot(embeddings[i], embeddings[j]) / (norm_i * norm_j)
                # Cosine distance
                dist = 1.0 - sim
                dist_matrix[i, j] = dist
                dist_matrix[j, i] = dist
        return dist_matrix

    def compute_persistence_intervals(self, dist_matrix: np.ndarray) -> List[Tuple[float, float]]:
        """
        Computes 1-dimensional persistent homology intervals using boundary matrix reduction.
        Returns a list of birth-death pairs for 1-cycles (loops).
        """
        n = dist_matrix.shape[0]
        if n < 4:
            return []

        # 1. Collect and sort simplices by birth time
        # 0-simplices (vertices)
        vertices = [(0.0, (i,)) for i in range(n)]
        
        # 1-simplices (edges)
        edges = []
        for i in range(n):
            for j in range(i + 1, n):
                birth = dist_matrix[i, j]
                edges.append((birth, (i, j)))
        edges.sort(key=lambda x: (x[0], x[1]))

        # 2-simplices (triangles)
        triangles = []
        for i in range(n):
            for j in range(i + 1, n):
                for k in range(j + 1, n):
                    birth = max(dist_matrix[i, j], dist_matrix[j, k], dist_matrix[i, k])
                    triangles.append((birth, (i, j, k)))
        triangles.sort(key=lambda x: (x[0], x[1]))

        # Combine all simplices into a single ordered list
        # Order: 0-simplices, then 1-simplices, then 2-simplices
        ordered_simplices = []
        simplex_to_idx = {}

        for birth, simp in vertices:
            simplex_to_idx[simp] = len(ordered_simplices)
            ordered_simplices.append((0, birth, simp))

        for birth, simp in edges:
            simplex_to_idx[simp] = len(ordered_simplices)
            ordered_simplices.append((1, birth, simp))

        for birth, simp in triangles:
            simplex_to_idx[simp] = len(ordered_simplices)
            ordered_simplices.append((2, birth, simp))

        num_simplices = len(ordered_simplices)
        
        # 2. Build the boundary matrix column representations (sparse set of active boundary indices)
        boundary = [set() for _ in range(num_simplices)]
        for idx, (dim, birth, simp) in enumerate(ordered_simplices):
            if dim == 0:
                continue
            elif dim == 1:
                # Boundary of (u, v) is (u,) and (v,)
                boundary[idx].add(simplex_to_idx[(simp[0],)])
                boundary[idx].add(simplex_to_idx[(simp[1],)])
            elif dim == 2:
                # Boundary of (u, v, w) is (u, v), (v, w), and (u, w)
                boundary[idx].add(simplex_to_idx[(simp[0], simp[1])])
                boundary[idx].add(simplex_to_idx[(simp[1], simp[2])])
                boundary[idx].add(simplex_to_idx[(simp[0], simp[2])])

        # 3. Perform boundary matrix reduction (column addition in Z_2)
        # pivot_to_col maps a row index (pivot) to the column index that has that pivot
        pivot_to_col = {}
        
        # Track births of 1-cycles
        # An edge creates a 1-cycle if its boundary reduces to empty but it is not a boundary of any triangle
        # We track paired edges (which die) and unpaired edges (which live to infinity)
        births = {}  # col_idx -> birth_time

        for col in range(num_simplices):
            dim, birth, simp = ordered_simplices[col]
            
            # Reduce column
            while boundary[col]:
                pivot = max(boundary[col])
                if pivot in pivot_to_col:
                    prev_col = pivot_to_col[pivot]
                    boundary[col] = boundary[col] ^ boundary[prev_col]  # Symmetric difference (XOR)
                else:
                    break
            
            # If not reduced to empty, it has a pivot
            if boundary[col]:
                pivot = max(boundary[col])
                pivot_to_col[pivot] = col
                
                # This col (which is a higher-dim simplex) kills the cycle born at pivot
                pivot_dim, pivot_birth, pivot_simp = ordered_simplices[pivot]
                if pivot_dim == 1 and dim == 2:
                    # A 1-cycle born at pivot_birth dies at birth
                    births[pivot] = (pivot_birth, birth)
            else:
                # Column is empty. If it's a 1-simplex (edge), it created a 1-cycle (born)
                if dim == 1:
                    births[col] = (birth, float('inf'))

        # Exclude pairs where the cycle dies, or extract actual intervals
        intervals = []
        for col_idx, (birth, _) in births.items():
            dim, _, simp = ordered_simplices[col_idx]
            if dim != 1:
                continue
            u, v = simp
            # If the edge became a pivot, it died. Otherwise it persists or died at some triangle's hand
            # Let's check if this edge index was a pivot in pivot_to_col
            if col_idx in pivot_to_col:
                # It died at the hand of the column that pivoted on it
                killer_col = pivot_to_col[col_idx]
                death_time = ordered_simplices[killer_col][1]
                if death_time > birth:
                    intervals.append((birth, death_time, u, v))
            else:
                intervals.append((birth, float('inf'), u, v))

        return intervals

    def detect_loop(self, vault) -> Tuple[bool, List[str]]:
        """
        Check if the recent stream of clipboard events indicates a cognitive debugging loop.
        Returns a tuple of (is_loop, list_of_loop_documents).
        """
        # Fetch the last N clips/commands
        with vault._store._connect() as conn:
            cursor = conn.execute(
                f"SELECT doc_id, document, vector_blob FROM vectors "
                f"WHERE collection = ? "
                f"ORDER BY created_at DESC LIMIT ?",
                (vault.name, self.max_history)
            )
            rows = cursor.fetchall()

        if len(rows) < 5:
            return False, []

        # Reconstruct vectors
        embeddings = []
        for doc_id, doc, blob in rows:
            vec = vault._store._blob_to_vector(blob).detach().cpu().numpy()
            embeddings.append(vec)

        # Calculate TDA persistent homology
        dist_matrix = self.calculate_distance_matrix(embeddings)
        intervals = self.compute_persistence_intervals(dist_matrix)

        # A loop is detected if there is a persistent 1-cycle with birth-death span > threshold
        for birth, death, u, v in intervals:
            span = death - birth
            if span >= self.distance_threshold:
                # Collect the documents between u and v
                start_idx = min(u, v)
                end_idx = max(u, v)
                loop_docs = [rows[i][1] for i in range(start_idx, end_idx + 1) if i < len(rows)]
                return True, loop_docs
        return False, []

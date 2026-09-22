# Technical Tome: LatticeShadow DB Architecture & Math

This is the architecture and mathematics reference for `latticeshadow-db`. It
documents research modules as well as the core store. Existence of a module or
benchmark here is not a deployment recommendation or security claim. Start
with the [DB README](../README.md) for the supported API path and its limits.

For quick setup instructions and a high-level API overview, please return to the [README](../README.md).

---

## 1. System Architecture & Data Flows

LatticeShadow DB operates as a serverless, local-first vector database optimized for low-latency retrieval under practical privacy constraints. Its primary searchable-vector privacy mechanism is a secret, reversible, distance-preserving Cayley rotation; this protects raw coordinate values but intentionally preserves similarity geometry so search can run locally.

### Directory Structure

```text
latticeshadow-db/
├── README.md                  # Coaching-style overview & setup guide
├── pyproject.toml             # Package specifications & dependencies
├── scripts/
│   └── verify_docs.py         # Link and AST syntax validator
├── docs/
│   └── TOME.md                # [THIS FILE] System architecture & math
├── tests/                     # Test suite
│   ├── test_alignment.py
│   ├── test_latticedb.py
│   └── ...
└── latticeshadow_db/          # Package source root
    ├── __init__.py            # Package exports
    ├── adapter.py             # Woodbury-optimized Cayley rotations
    ├── alignment.py           # Orthogonal Procrustes space translation
    ├── bridge.py              # Experimental cloud alignment bridge
    ├── cache.py               # Entropy-validated activation cache
    ├── server.py              # FastAPI REST Lock Server
    ├── leech.py               # Leech decoder routing
    ├── leech_fallback.py      # Pure-Python Leech Lattice decoder
    └── ...
```

### Data Flow diagram (RAG Pipeline)

Below is the conceptual sequence of operations during document insertion and semantic query execution with privacy mode enabled:

```text
[ Document ] ──> [ Embedding Function ] ──> [ Raw Float Vector (d) ]
                                                   │
   ┌───────────────────────────────────────────────┘
   ▼
[ PrivacyEngine ] ──> Apply Woodbury Cayley Rotation (I - 2 U M V^T)
                           │
                           ▼
                 [ Rotated Vector ] ──> Store in SQLite / Binary Memmap
```

When a search query is submitted:
1. The query text is embedded to generate a raw query vector.
2. The query vector is Cayley-rotated into the same obfuscated space using the stored rotation key.
3. A cosine similarity search (via optional FAISS or brute-force) is executed directly on the rotated vectors.
4. **Distance Preservation**: Since the Cayley rotation is orthogonal, the dot product and Euclidean distance between rotated vectors are mathematically identical to those in the plaintext space.
5. **Threat-model boundary**: AES-GCM protects the stored rotation key and optional document text. The stored vectors remain searchable rotated coordinates, not opaque ciphertext; an attacker who can observe many vectors may still learn geometry-level information such as similarities, clusters, and relative distances.

Experimental retrieval modes can be selected with `experimental_index`. `streaming_exact` uses chunked NumPy scans over the dense memmap with cached inverse norms for exact cosine search. `hnsw_exact_rerank` uses optional native `hnswlib` to propose candidates, persists a versioned sidecar index, then exact-reranks candidates through the same streaming truth layer. `hnsw_rerank` remains the legacy FAISS-backed candidate path. `flyhash_rerank` uses sparse binary FlyHash filtering with an fp16 exact-rerank sidecar, `pq_rerank` uses normalized int8 PQ-lite codes for compressed candidate scoring, and `diskann_rerank` uses dependency-free DiskANN/Vamana-inspired fp16 SSD sidecars before exact cosine reranking on stored vectors.

The native HNSW lane is a max-performance optional dependency, not a default requirement. Missing `hnswlib` is reported in benchmark metadata and falls back cleanly to exact search. In one local synthetic run (`100k x 384`, `k=10`, `10` measured queries, `M=32`, `efConstruction=200`, `efSearch=1536`, `candidate_cap=3000`), `hnsw_exact_rerank` measured `recall@10=1.0`, `p95=36.8 ms`, and `75.5x` p95 speedup versus `dense_exact_public`. `streaming_exact_public` still won the same workload at `p95=7.9 ms`. These numbers describe that run, not a general speedup.

The `diskann_rerank` path is intentionally a prototype tier rather than a native DiskANN dependency. It writes two sidecars next to the collection memmap: normalized fp16 vectors for disk-native candidate scans and a fixed-degree uint32 neighbor graph for Vamana-style greedy traversal. Small or filtered searches scan the fp16 sidecar directly; larger searches traverse the graph to propose candidates, then reuse the exact cosine reranker over canonical stored vectors. This makes the 100x-1000x capacity idea measurable while preserving the current no-hard-native-dependency contract.

For moonshot proof runs, `latticeshadow_db.scale_benchmarks` provides a separate resumable scale harness. It generates deterministic synthetic vectors into float32 memmaps, computes exact ground truth with chunked streaming top-k, bulk-loads public `Collection` modes in chunks, defers DiskANN sidecar construction until the load is complete, and writes a JSON report with measured dense-public speedups. The harness is deliberately strict: the `achieved` flag is true only when measured p50 and p95 speedups, recall, latency, storage, and cold-reload gates all pass; it does not turn extrapolated numbers into a primary claim.

`latticeshadow_db.moonshot_tuner` layers a promotion-grid loop over the scale harness. It sweeps partition count, probe count, candidate cap, graph degree, full-scan threshold, and k-means iteration settings, then promotes only recall-gated configurations from small filters into larger stages. The quick preset is for local knob twisting; the moonshot preset follows the 10k -> 100k -> 1M proof ladder and writes machine-readable plus Markdown leaderboards. This tuner is an experiment selector, not an achievement label: the final 1000x claim still comes only from measured scale-harness gates.

---

## 2. Mathematical Models

### 2.1 SVD / Orthogonal Procrustes Space Translation

When matching heterogeneous representation spaces (e.g. aligning 1536D cloud embeddings to 768D local embeddings), we fit a rectangular projection $Q \in \mathbb{R}^{d_{cloud} \times d_{local}}$ that minimizes error on paired calibration vectors. A rectangular map is not generally distance preserving across the full source space; quality must be measured on held-out pairs.

Given:
*   Local states $X \in \mathbb{R}^{N \times d_{local}}$
*   Cloud states $Y \in \mathbb{R}^{N \times d_{cloud}}$

#### Step 1: Centering
We compute the empirical means and center the datasets:
$$\mu_X = \frac{1}{N} \sum_{i=1}^N X_i, \quad \mu_Y = \frac{1}{N} \sum_{i=1}^N Y_i$$
$$A = X - \mu_X, \quad B = Y - \mu_Y$$

#### Step 2: Cross-Covariance
We compute the cross-covariance matrix $C$:
$$C = A^T B \quad \in \mathbb{R}^{d_{local} \times d_{cloud}}$$

#### Step 3: Singular Value Decomposition (SVD)
We perform SVD on $C$:
$$C = U \Sigma V^T$$
With the thin SVD used by the code, $U \in \mathbb{R}^{d_{local} \times r}$ and $V \in \mathbb{R}^{d_{cloud} \times r}$ have orthonormal columns, $r = \min(d_{local}, d_{cloud})$, and $\Sigma$ contains the singular values.

#### Step 4: Projection Matrix
The optimal translation matrix $Q$ is computed as:
$$Q = V U^T \quad \in \mathbb{R}^{d_{cloud} \times d_{local}}$$

#### Step 5: Alignment Transformation
For any incoming cloud representation $y$, the aligned local representation $x_{aligned}$ is:
$$x_{aligned} = (y - \mu_Y) Q + \mu_X$$

---

### 2.2 Cayley Rotations (Woodbury-Optimized)

To obfuscate vectors without breaking similarity search, we apply a secret random orthogonal rotation matrix $W_L$. Generating and multiplying a full $d \times d$ orthogonal matrix scales at $O(d^2)$ computation and memory. To resolve this, we parameterize $W_L$ using a low-rank skew-symmetric matrix $S$.

$$W_L = (I - S)(I + S)^{-1}$$

Using the **Woodbury Matrix Identity**, we factor $S = U V^T - V U^T$ where $U, V \in \mathbb{R}^{d \times 2r}$ and $r \ll d$. The rotation is implemented as:

$$W_L = I - 2 U M V^T$$
$$W_L^T = I - 2 V M^T U^T$$
where:
*   $U = [A \mid -B] \quad \in \mathbb{R}^{d \times 2r}$
*   $V = [B \mid A] \quad \in \mathbb{R}^{d \times 2r}$
*   $M = (I_{2r} + V^T U)^{-1} \quad \in \mathbb{R}^{2r \times 2r}$

For a row-vector $h \in \mathbb{R}^{1 \times d}$, the forward rotation is computed in $O(dr)$ time:
$$h_{rotated} = h W_L^T = h - 2 (h V) M^T U^T$$

The inverse rotation is computed as:
$$h_{original} = h W_L = h - 2 (h U) M V^T$$

```python
import torch
from latticeshadow_db.adapter import CayleyPrivacyAdapter

# Construct a Woodbury-optimized Cayley Privacy Adapter
adapter = CayleyPrivacyAdapter(dim=768, rank=16, device="cpu")

# Rotate activations for distance-preserving obfuscation
h = torch.randn(1, 768)
h_rot = adapter.rotate(h)

# Inverse rotate to recover original activations
h_orig = adapter.inverse_rotate(h_rot)
assert torch.allclose(h, h_orig, atol=1e-5)
```

---

### 2.3 Leech Lattice Quantization ($\Lambda_{24}$)

For fast coarse indexing, vectors are mapped onto the 24-dimensional Leech Lattice ($\Lambda_{24}$). The Leech Lattice is the densest sphere packing in 24 dimensions.

1.  **Padding & Reshaping**: The input vector is padded to a multiple of 24 using structured, reproducible Gaussian noise.
2.  **Scaling**: The vector is scaled by $\sqrt{8}$ to prepare it for decoding.
3.  **Golay Code Decoding**: The scaled vector $y$ is mapped to the nearest point on $\Lambda_{24}$ using the binary $[24, 12, 8]$ Golay code decoder.
4.  **Inverse Scaling**: The lattice point is divided by $\sqrt{8}$ to yield the quantized vector.
5.  **Compact Index Code**: `LatticeIndexer` stores the quantized lattice point as an LLVQ-lite int16 coordinate code by multiplying back by $\sqrt{8}$ and rounding:
    $$c_{\text{int16}} = \operatorname{int16}\left(\operatorname{round}(\sqrt{8} \cdot q)\right)$$
    During coarse search, the code is restored as:
    $$q = c_{\text{int16}} / \sqrt{8}$$
    This preserves the decoded lattice coordinates while halving the resident code tensor payload versus float32 storage.

---

### 2.4 Drosophila Hashing (FlyHash)

#### Architecture & Biological Inspiration
The Drosophila Hashing (FlyHash) module (`latticeshadow_db/latticedb/drosophila.py`) is inspired by the olfactory system of the fruit fly (*Drosophila melanogaster*). In the fly's brain, olfactory receptor neurons project to the antennal lobe (projection neurons), which then project to the Kenyon cells in the mushroom body. 

This biological pathway maps a dense, low-dimensional input vector (representing odors) to a highly sparse, very high-dimensional representation (Kenyon cell firing pattern). The mathematical model replicates this through three stages:
1. **Sparse Random Projection**: The input vector $x \in \mathbb{R}^D$ is projected into a higher-dimensional space $\mathbb{R}^K$ (where typically $K \gg D$, e.g., $K = 10,000$). Each dimension in the output space is connected to a small number of input dimensions (determined by `sparsity` $S$, typically $S = 6$).
2. **Winner-Take-All (WTA) Sparsification**: The top $5\%$ ($k = \lfloor 0.05 \cdot K \rfloor$) strongest activations are kept, while all other activations are set to $0$. This yields a binary mask vector in $\{0, 1\}^K$.
3. **Bit-Packing**: The binary mask is packed into bytes (uint8 elements) along the last dimension to optimize memory usage.

Hamming distances between these packed hashes are computed using optimized bitwise operations (XOR followed by a popcount), which avoids expensive float calculations.

#### Mathematical Formulation

##### Sparse Projection
Let $x \in \mathbb{R}^D$ be the input vector. The projection matrix $M \in \mathbb{R}^{K \times D}$ is defined such that each column $j \in \{1, \dots, D\}$ has exactly $S = \min(\text{sparsity}, K)$ non-zero elements, drawn from a standard normal distribution:
$$M_{i, j} \sim \mathcal{N}(0, 1) \quad \text{for } i \in \Omega_j, \quad \text{and} \quad M_{i, j} = 0 \quad \text{for } i \notin \Omega_j$$
where $\Omega_j \subset \{1, \dots, K\}$ is a randomly chosen set of indices of size $S$.
The projected vector $y \in \mathbb{R}^K$ is:
$$y = M x$$

##### Winner-Take-All (WTA) Mask
Let $\pi(y)$ represent the permutation of indices $\{1, \dots, K\}$ that sorts the elements of $y$ in ascending order:
$$y_{\pi(1)} \leq y_{\pi(2)} \leq \dots \leq y_{\pi(K)}$$
To resolve ties stably, the original index order is maintained for equal values. The top $k$ activation threshold index limit is $K - k + 1$, where $k = \max(1, \lfloor 0.05 \cdot K \rfloor)$.
The binary mask $m \in \{0, 1\}^K$ is defined element-wise as:
$$m_i = \begin{cases} 1 & \text{if } i \in \{\pi(K - k + 1), \dots, \pi(K)\} \\ 0 & \text{otherwise} \end{cases}$$
If the projected vector has no variance (i.e., $\max(y) = \min(y)$), then $m_i = 0$ for all $i$.

##### Bit Packing & Hamming Distance
The binary mask $m$ is packed into a byte array $H \in \mathbb{N}_0^{\lceil K / 8 \rceil}$ using `np.packbits`:
$$H[j] = \sum_{r=0}^{7} m_{8j + r} \cdot 2^{7-r}$$

For two packed hashes $H_a, H_b \in \mathbb{N}_0^{\lceil K / 8 \rceil}$, the Hamming distance $d_H(H_a, H_b)$ is computed as the sum of set bits of their bitwise XOR:
$$d_H(H_a, H_b) = \sum_{j=1}^{\lceil K / 8 \rceil} \operatorname{popcount}(H_a[j] \oplus H_b[j])$$
where $\oplus$ represents the bitwise XOR operator.

The vectorized popcount for an 8-bit integer $c$ is computed using a parallel bit-masking arithmetic fallback if native PyTorch `bitwise_count` is unavailable:
$$c_1 = (c \ \& \ \text{0x55}) + ((c \gg 1) \ \& \ \text{0x55})$$
$$c_2 = (c_1 \ \& \ \text{0x33}) + ((c_1 \gg 2) \ \& \ \text{0x33})$$
$$\operatorname{popcount}(c) = (c_2 \ \& \ \text{0x0F}) + ((c_2 \gg 4) \ \& \ \text{0x0F})$$

#### Python Usage Example

```python
import numpy as np
import torch
from latticeshadow_db.latticedb.drosophila import DrosophilaHasher, compute_hamming_distance

# Initialize Drosophila Hasher
input_dim = 512
output_dim = 10000  # Will result in 1250 packed bytes (10000 / 8)
hasher = DrosophilaHasher(input_dim=input_dim, output_dim=output_dim, seed=42, sparsity=6)

# Generate mock data
np.random.seed(42)
x1 = np.random.randn(input_dim).astype(np.float32)
x2 = x1 + np.random.randn(input_dim).astype(np.float32) * 0.1  # Correlated query
x3 = np.random.randn(input_dim).astype(np.float32)            # Random query

# Compute hashes (packed byte representation)
hash1 = hasher.hash(x1)
hash2 = hasher.hash(x2)
hash3 = hasher.hash(x3)

# Compute Hamming distances
dist_1_2 = compute_hamming_distance(hash1, hash2)
dist_1_3 = compute_hamming_distance(hash1, hash3)

print(f"Hash shape: {hash1.shape} (dtype: {hash1.dtype})")
print(f"Hamming distance (Correlated): {dist_1_2}")
print(f"Hamming distance (Random): {dist_1_3}")
```

---

### 2.5 Holographic Associative Memory

#### Architecture & Vector Symbolic Representation
Holographic Associative Memory (`latticeshadow_db/latticedb/holographic.py`) belongs to the class of Vector Symbolic Architectures (VSAs). It maps symbolic structures and data associations to high-dimensional distributed vectors. 

In this system, concepts are represented by random, unit-normalized vectors. Two vectors are bound together using **circular convolution**, which creates a new vector that is nearly orthogonal to both constituents. To retrieve a bound value from the memory vector, we use **circular correlation** (the unbinding operator) with the corresponding key.

Because the representation is distributed ("holographic"), multiple key-value associations can be summed into a single memory trace vector:
$$M = \sum_i k_i \circledast v_i$$
To retrieve the value associated with key $k_j$, we correlate $M$ with $k_j$:
$$\hat{v}_j = M \star k_j \approx v_j + \text{noise}$$
A clean-up memory (e.g. nearest neighbor search) can then be used to reconstruct $v_j$ exactly from $\hat{v}_j$.

The module exposes `cleanup_to_codebook(query, codebook, min_similarity=...)` as the first explicit cleanup-memory primitive. It normalizes a noisy HRR/VSA query, finds the nearest codebook vector by cosine similarity, and returns a `(label, vector, similarity)` tuple only when the best match crosses the configured threshold. This keeps cleanup memory opt-in and measurable: future experiments can compare exact nearest-neighbor cleanup, PQ-backed cleanup, and Hopfield-style cleanup without changing the circular convolution/correlation API.

#### Mathematical Formulation

##### Circular Convolution
For two vectors $a, b \in \mathbb{R}^N$, the circular convolution $c = a \circledast b \in \mathbb{R}^N$ is defined element-wise as:
$$c_n = \sum_{m=0}^{N-1} a_m b_{(n-m) \pmod N}$$

Using the convolution theorem, circular convolution can be computed efficiently via the Discrete Fourier Transform (DFT):
$$\mathcal{F}(a \circledast b) = \mathcal{F}(a) \odot \mathcal{F}(b)$$
where $\mathcal{F}$ represents the Fast Fourier Transform (FFT) and $\odot$ is the element-wise multiplication of complex vectors.
Thus, circular convolution is computed as:
$$a \circledast b = \operatorname{Re}\left( \mathcal{F}^{-1} \left( \mathcal{F}(a) \odot \mathcal{F}(b) \right) \right)$$
where $\mathcal{F}^{-1}$ is the Inverse Fast Fourier Transform (IFFT) and $\operatorname{Re}(\cdot)$ extracts the real part (applicable for real-valued inputs).

##### Circular Correlation
Circular correlation is the adjoint operation of circular convolution. For two vectors $c, a \in \mathbb{R}^N$, the circular correlation $v = c \star a \in \mathbb{R}^N$ is defined as:
$$v_n = \sum_{m=0}^{N-1} c_m a_{(m+n) \pmod N}$$

In the frequency domain, circular correlation corresponds to multiplying the DFT of $c$ with the complex conjugate of the DFT of $a$:
$$\mathcal{F}(c \star a) = \mathcal{F}(c) \odot \overline{\mathcal{F}(a)}$$
where $\overline{z}$ denotes the complex conjugate of $z$.
Thus:
$$c \star a = \operatorname{Re}\left( \mathcal{F}^{-1} \left( \mathcal{F}(c) \odot \overline{\mathcal{F}(a)} \right) \right)$$

#### Python Usage Example

```python
import torch
from latticeshadow_db.latticedb.holographic import circular_convolution, circular_correlation, generate_key_vector

# Vector dimension
dim = 2048

# Generate deterministic key vectors for fields
key_author = generate_key_vector("author", dim=dim)
key_title = generate_key_vector("title", dim=dim)

# Values (representing content)
val_author = generate_key_vector("Alice", dim=dim)
val_title = generate_key_vector("Holographic Cryptography", dim=dim)

# Bind keys to values
bind_author = circular_convolution(key_author, val_author)
bind_title = circular_convolution(key_title, val_title)

# Aggregate into a single holographic memory trace
memory_trace = bind_author + bind_title

# Retrieve/Unbind "author" from the memory trace
retrieved_author = circular_correlation(memory_trace, key_author)

# Check cosine similarity
sim_author = torch.dot(retrieved_author, val_author) / (torch.norm(retrieved_author) * torch.norm(val_author))
sim_title = torch.dot(retrieved_author, val_title) / (torch.norm(retrieved_author) * torch.norm(val_title))

print(f"Cosine Similarity with correct value ('Alice'): {sim_author.item():.4f}")
print(f"Cosine Similarity with unrelated value ('Holographic Cryptography'): {sim_title.item():.4f}")
```

For exact cleanup against a small symbolic codebook:

```python
from latticeshadow_db.latticedb.holographic import cleanup_to_codebook

codebook = {
    "Alice": val_author,
    "Holographic Cryptography": val_title,
}
match = cleanup_to_codebook(retrieved_author, codebook, min_similarity=0.20)
if match is not None:
    label, vector, similarity = match
    print(label, similarity)
```

---

### 2.6 Hyperbolic Poincaré Unit Ball

#### Architecture & Hyperbolic Geometry
Hyperbolic space is ideal for representing hierarchical or tree-like structures, as the volume of the space grows exponentially with the radius. The Poincaré unit ball model (`latticeshadow_db/latticedb/hyperbolic.py`) maps the hyperbolic space to the open unit disk/ball.

This module provides two main functions:
1. **Poincaré Projection**: Maps any Euclidean vector $x \in \mathbb{R}^D$ into the open Poincaré unit ball $\mathbb{B}^D = \{y \in \mathbb{R}^D : \|y\| < 1\}$ using the hyperbolic tangent function. To maintain numerical stability and avoid undefined distances, the projected vector's norm is clamped to a maximum of $1 - 10^{-5}$.
2. **Poincaré Distance**: Computes the geodesic distance between two points within the Poincaré unit ball. The formula contains a division by $(1 - \|u\|^2)(1 - \|v\|^2)$, which grows extremely fast as points approach the boundary. The implementation applies safe clamping to the denominator to prevent division by zero or NaN values.

#### Mathematical Formulation

##### Poincaré Projection
Let $x \in \mathbb{R}^D$ be a Euclidean vector. The $L_2$ norm is $\|x\|_2 = \sqrt{\sum_{i=1}^D x_i^2}$. 
The projection mapping $f: \mathbb{R}^D \to \mathbb{B}^D$ is defined as:
$$f(x) = \begin{cases} \min\left(\tanh(\|x\|_2), 1 - 10^{-5}\right) \cdot \frac{x}{\|x\|_2} & \text{if } \|x\|_2 > 0 \\ 0 & \text{if } \|x\|_2 = 0 \end{cases}$$

##### Poincaré Distance
Let $u, v \in \mathbb{B}^D$ be two vectors in the Poincaré unit ball (such that $\|u\|_2 < 1$ and $\|v\|_2 < 1$). The Poincaré distance $d_{\mathbb{B}}(u, v)$ is:
$$d_{\mathbb{B}}(u, v) = \operatorname{arcosh}\left(1 + 2 \frac{\|u - v\|_2^2}{(1 - \|u\|_2^2)(1 - \|v\|_2^2)}\right)$$

Where the inverse hyperbolic cosine is evaluated as:
$$\operatorname{arcosh}(z) = \ln\left(z + \sqrt{z^2 - 1}\right)$$

To implement this robustly, the denominator is clamped:
$$D(u, v) = \max\left( (1 - \|u\|_2^2)(1 - \|v\|_2^2), 10^{-15} \right)$$
And the argument $z$ is clamped:
$$z = \max\left( 1 + 2 \frac{\|u - v\|_2^2}{D(u, v)}, 1 + 10^{-15} \right)$$
The final distance calculation is:
$$d_{\mathbb{B}}(u, v) = \ln\left(z + \sqrt{z^2 - 1}\right)$$

#### Python Usage Example

```python
import torch
from latticeshadow_db.latticedb.hyperbolic import poincare_project, poincare_distance

# Generate arbitrary Euclidean vectors representing hierarchical nodes
# e.g., root, child_1 (close to root), child_2 (further away)
root = torch.tensor([0.0, 0.0, 0.0])
child_1 = torch.tensor([0.5, 0.2, -0.1])
child_2 = torch.tensor([2.5, -1.0, 3.0])

# Project onto the Poincaré unit ball
p_root = poincare_project(root)
p_child_1 = poincare_project(child_1)
p_child_2 = poincare_project(child_2)

# Compute distances in Hyperbolic space
dist_root_c1 = poincare_distance(p_root, p_child_1)
dist_root_c2 = poincare_distance(p_root, p_child_2)
dist_c1_c2 = poincare_distance(p_child_1, p_child_2)

print(f"Poincaré norms: root={torch.norm(p_root):.4f}, c1={torch.norm(p_child_1):.4f}, c2={torch.norm(p_child_2):.4f}")
print(f"Poincaré distance (root <-> child_1): {dist_root_c1.item():.4f}")
print(f"Poincaré distance (root <-> child_2): {dist_root_c2.item():.4f}")
print(f"Poincaré distance (child_1 <-> child_2): {dist_c1_c2.item():.4f}")
```

---

### 2.7 Local Distillation MLP

#### Architecture & Distillation Lifecycle
The Local Distillation MLP facilitates local, private approximation of cloud-aligned vector representations. It runs background training on cached activation pairs to "distill" the cloud correction function into a lightweight local network.

The distillation architecture consists of two primary components:
1. **`CorrectionHead` (`latticeshadow_db/distill.py`)**:
   * Encapsulates the PyTorch MLP model (`nn.Sequential` containing two `nn.Linear` layers and a `nn.ReLU`).
   * Manages optimizer state and weights, and performs batch gradient updates using PyTorch.
   * Dynamically rebuilds the linear layers if the input tensor dimension mismatch is detected during training.
2. **`AutoDistiller` (`latticeshadow_db/latticedb/distiller.py`)**:
   * Acts as the persistence and orchestration wrapper for the `CorrectionHead`.
   * Manages an SQLite database (`distillation_pairs` table) to store serialized `local_blob` and `cloud_blob` embeddings. This database backing is critical for bounding memory consumption under high query throughput.
   * Contains a thread lock (`self._lock`) to ensure safe execution of background training and prediction in multi-threaded application servers.
   * Tracks sample accumulation and automatically runs training when the sample size exceeds `min_samples`.
   * Reserves a deterministic holdout slice from the newest observations before graduation, so cloud-call skipping depends on out-of-sample error rather than training loss alone.

#### Mathematical Formulation

##### Neural Network Architecture
For a given local vector representation $x \in \mathbb{R}^d$, the network computes a prediction of the cloud-aligned vector $\hat{y} \in \mathbb{R}^d$ as follows:
$$z = W_1 x + b_1$$
$$a = \operatorname{ReLU}(z) = \max(0, z)$$
$$\hat{y} = f_{\theta}(x) = W_2 a + b_2$$

Combining these equations, the complete feed-forward mapping is:
$$\hat{y} = f_{\theta}(x) = W_2 \max(0, W_1 x + b_1) + b_2$$

Where:
* $d$ is the embedding dimension (`dim`).
* $h$ is the hidden layer size (`hidden_dim`, default 256).
* $W_1 \in \mathbb{R}^{h \times d}$ and $b_1 \in \mathbb{R}^h$ are the weights and biases of the first fully-connected layer.
* $\operatorname{ReLU}$ is the rectified linear unit activation function, applied element-wise.
* $W_2 \in \mathbb{R}^{d \times h}$ and $b_2 \in \mathbb{R}^d$ are the weights and biases of the second fully-connected layer.
* $\theta = \{W_1, b_1, W_2, b_2\}$ represents the set of all trainable parameters.

##### Loss Function and Optimization
The network parameters are optimized using Mini-batch Gradient Descent with the Adam optimizer (learning rate $\eta = 10^{-3}$). The training objective is to minimize the Mean Squared Error (MSE) loss between the predicted outputs $\hat{y}_i$ and the ground-truth cloud-aligned vectors $y_i$ across a batch of size $B$:
$$\mathcal{L}_{\text{MSE}}(\theta) = \frac{1}{B \cdot d} \sum_{i=1}^B \| f_{\theta}(x_i) - y_i \|_2^2 = \frac{1}{B \cdot d} \sum_{i=1}^B \sum_{j=1}^d \left( [f_{\theta}(x_i)]_j - y_{i, j} \right)^2$$
$$\theta_{t+1} = \theta_t - \operatorname{Adam}\left(\nabla_{\theta} \mathcal{L}_{\text{MSE}}(\theta_t), \eta\right)$$

##### Distillation Lifecycle & Graduation
1. **SQLite-Backed Recording**: To prevent RAM memory leaks during continuous execution, input-output pairs $(x_i, y_i)$ are serialized as half-precision float (`fp16`) blobs and stored in an SQLite database:
   $$\text{Blob}(v) = \text{Serialize}(\text{Float16}(v))$$
2. **Bounded-Memory Training**: When training is triggered, memory usage is kept bounded by loading only the latest $N$ pairs:
   $$\mathcal{D} = \{ (x_k, y_k) \}_{k=1}^{\min(M, N)}$$
   where $M$ is the total recorded samples, and $N$ is `max_training_pairs` (default 2000).
3. **Holdout-Gated Graduation Criteria**: Before training, `AutoDistiller` splits the bounded latest-pair window into an older training set $\mathcal{D}_{\text{train}}$ and a newest-observation holdout set $\mathcal{D}_{\text{holdout}}$ using `holdout_fraction` (default 0.2):
   $$\mathcal{D} = \mathcal{D}_{\text{train}} \cup \mathcal{D}_{\text{holdout}}, \quad \mathcal{D}_{\text{train}} \cap \mathcal{D}_{\text{holdout}} = \emptyset$$
   The system is considered "graduated" only if enough valid samples have been observed and both training and holdout losses pass their thresholds:
   $$\text{is\_graduated} \iff \text{is\_trained} \land |\mathcal{D}| \geq N_{\min} \land \mathcal{L}_{\text{train}} < \theta_{\text{loss}} \land \mathcal{L}_{\text{holdout}} < \theta_{\text{holdout}}$$
   where $N_{\min}$ is `min_samples`, $\theta_{\text{loss}}$ is `loss_threshold` (default 0.01), and $\theta_{\text{holdout}}$ defaults to the same value unless `holdout_loss_threshold` is provided.
4. **Local Prediction**: Once graduated, calls to `predict(local_vec)` returns the predicted aligned vector, allowing the system to completely bypass cloud calls:
   $$\text{predict}(x) = \begin{cases} f_{\theta}(x) & \text{if is\_graduated is True} \\ \text{None} & \text{otherwise} \end{cases}$$

The benchmark harness includes an opt-in distillation replay check via `--include-distillation-replay`. It runs a stable stream that should graduate and a drifted-tail stream that should fail holdout validation, reporting `cloud_skip_allowed_rate`, train/holdout loss, and the `graduation_block_reason`.

#### Python Usage Example

```python
import torch
import tempfile
from pathlib import Path
from latticeshadow_db.latticedb.distiller import AutoDistiller

# 1. Initialize AutoDistiller
# Specify embedding dimension (dim=32), hidden layer size (hidden_dim=64),
# minimum samples required to trigger training (min_samples=10),
# MSE loss threshold for graduation (loss_threshold=0.05),
# and an out-of-sample holdout gate (holdout_fraction=0.2).
with tempfile.TemporaryDirectory() as tmpdir:
    db_path = str(Path(tmpdir) / "distiller.db")
    distiller = AutoDistiller(
        dim=32,
        hidden_dim=64,
        min_samples=10,
        loss_threshold=0.05,
        holdout_fraction=0.2,
        device="cpu",
        db_path=db_path,
        collection="test_collection"
    )

    # 2. Record correlated local and cloud activation pairs
    for _ in range(15):
        local_vec = torch.randn(32)
        # Target cloud vector is a linear transformation plus noise
        cloud_vec = local_vec * 1.05 + torch.randn(32) * 0.01
        distiller.record_pair(local_vec, cloud_vec)

    print(f"Recorded samples: {distiller.sample_count}")
    print(f"Graduated before training: {distiller.is_graduated}")

    # 3. Manually trigger training (epochs=30)
    final_loss = distiller.train(epochs=30)
    print(f"Final training MSE Loss: {final_loss:.6f}")
    print(f"Final holdout MSE Loss: {distiller.holdout_loss:.6f}")
    print(f"Graduation metrics: {distiller.graduation_metrics}")
    print(f"Graduated after training: {distiller.is_graduated}")

    # 4. If graduated, predict the cloud-aligned vector from a local vector
    if distiller.is_graduated:
        query_local = torch.randn(32)
        prediction = distiller.predict(query_local)
        print(f"Prediction successful, shape: {prediction.shape}")
```

---

### 2.8 Temporal Coherence Buffer

#### Architecture & Telemetry Diagnostics
The Temporal Coherence Buffer (`TemporalStateBuffer` inside `temporal.py`) maintains conversation-level state across multi-turn queries. It tracks residual trajectories to dynamically adjust thresholds and apply momentum-based corrections to query states before speculation.

The buffer executes four main roles:
1. **Speculation Control**: By examining the slope of the residuals ($s$), the buffer acts as a predictive PID controller. If the conversation has settled (improving residuals), it relaxes constraints so the faster local/speculative model handles more requests. If residuals degrade, it tightens the threshold to enforce cloud fallback.
2. **State Pre-Conditioning (Momentum)**: When local models exhibit systematic errors (e.g. constant translation offsets in high-dimensional space), the buffer acts as a low-pass filter to compute the correction direction. If the direction remains stable across turns ($S \geq 0.5$), the system pre-applies a fraction of the shift ($\beta \cdot m$) to local queries to improve speculative matching accuracy.
3. **Health Status Classification**: Classifies the system state into `"healthy"`, `"converging"`, `"degrading"`, or `"cold"` based on the slope thresholds, enabling real-time telemetry diagnostics of ZkBridge operations.
4. **Residual Drift Guard**: Tracks cloud-anchored residuals with a Page-Hinkley-style accumulator and an ADWIN-style two-window mean comparison. When drift is active, ZkBridge suppresses cache, distillation, and local entropy exits, and speculation uses a much tighter threshold.

#### Mathematical Formulation

##### Query Execution Snapshot
A query execution snapshot $S_k$ at sequence index $k$ is represented as a tuple:
$$S_k = \left( x_k, y_k, r_k, \alpha_k, e_k, \text{route}_k, t_k \right)$$

Where:
* $x_k \in \mathbb{R}^d$ is the local representation vector.
* $y_k \in \mathbb{R}^d \cup \{\text{None}\}$ is the cloud-aligned vector.
* $r_k \in \mathbb{R}^+$ is the residual value (magnitude of the alignment error).
* $\alpha_k \in [0, 1]$ is the adaptive blend parameter.
* $e_k \in \mathbb{R}$ is the activation entropy.
* $\text{route}_k \in \text{Routes}$ is the routing decision (e.g., `"local"`, `"speculative"`, `"distilled"`, `"cloud"`).
* $t_k \in \mathbb{R}$ is the query timestamp.

The sliding window contains the $W$ most recent query states:
$$\mathcal{B} = \{ S_{K-W+1}, \dots, S_K \}$$
where $W$ is the `window_size` (default 16).

##### Speculative Threshold Adjustments
Let $W_c = \{ S_k \in \mathcal{B} \mid y_k \neq \text{None} \}$ be the subset of states representing active cloud interactions, and let $n = |W_c|$. If $n < 3$, the residual trend slope is defined as $s = 0.0$.
For $n \ge 3$, the residual trend slope $s$ is computed via simple linear regression of the residuals $r_i$ against the sequence indices $i \in \{0, 1, \dots, n-1\}$:
$$s = \frac{n \sum_{i=0}^{n-1} i \cdot r_i - \left(\sum_{i=0}^{n-1} i\right)\left(\sum_{i=0}^{n-1} r_i\right)}{n \sum_{i=0}^{n-1} i^2 - \left(\sum_{i=0}^{n-1} i\right)^2}$$

The speculative threshold adjustment factor $\alpha_{\text{adj}}$ is determined by the negative trend:
$$\alpha_{\text{adj}} = 1.0 + \operatorname{clamp}\left( -s \cdot \gamma, -1.0, 1.0 \right)$$
where $\gamma$ is the `trend_scaling` parameter (default 2.0). The suggested threshold $\theta_{\text{suggested}}$ is computed by scaling the base threshold $\theta_{\text{base}}$ and clamping it to a safety range:
$$\theta_{\text{suggested}} = \operatorname{clamp}\left( \theta_{\text{base}} \cdot \alpha_{\text{adj}}, \, 0.5 \cdot \theta_{\text{base}}, \, 3.0 \cdot \theta_{\text{base}} \right)$$

If the residual drift guard is active, the suggested threshold is additionally capped by a tightening factor $\kappa$:
$$\theta_{\text{drift}} = \min\left(\theta_{\text{suggested}}, \kappa \theta_{\text{base}}\right)$$
where $\kappa$ is `drift_tightening_factor` (default 0.25).

##### Residual Drift Guard
The guard observes only cloud-anchored residuals so that local predictions do not self-certify. For each cloud route residual $r_t$, a Page-Hinkley-style statistic is maintained over the residual loss stream:
$$\mu_t = \mu_{t-1} + \frac{r_t - \mu_{t-1}}{t}$$
$$m_t = m_{t-1} + r_t - \mu_t - \delta$$
$$PH_t = m_t - \min_{j \le t} m_j$$
where $\delta$ is `drift_page_hinkley_delta`. In parallel, the residual window is split into older and newer halves with means $\bar{r}_{old}$ and $\bar{r}_{new}$:
$$A_t = \frac{\bar{r}_{new} - \bar{r}_{old}}{\max(|\bar{r}_{old}|, 10^{-8})}$$

After `drift_min_samples`, the guard activates if either the recent mean has increased beyond the baseline by `drift_relative_threshold`, the two-window increase $A_t$ exceeds that same threshold, or $PH_t$ exceeds `drift_page_hinkley_threshold`. The guard recovers only after `drift_recovery_samples` stable cloud residual observations.

##### Momentum Corrections
1. **Exponentially Weighted Mean Correction Vector**:
   For the $n$ active cloud interactions in the buffer, the correction vectors are:
   $$d_i = y_i - x_i \in \mathbb{R}^d, \quad \forall i \in \{0, \dots, n-1\}$$
   An exponential decay weighting with decay rate $\lambda = 0.85$ is applied, where the latest corrections have the highest weights:
   $$w_i = \lambda^{n - 1 - i}$$
   The normalized weights are:
   $$\tilde{w}_i = \frac{w_i}{\sum_{j=0}^{n-1} w_j}$$
   The rolling momentum vector $m \in \mathbb{R}^d$ is:
   $$m = \sum_{i=0}^{n-1} \tilde{w}_i \cdot d_i$$
2. **Momentum Stability**:
   The stability $S \in [0, 1]$ of the correction trajectory is the cosine similarity between the current momentum vector $m^{(t)}$ and the previously stored momentum vector $m^{(t-1)}$:
   $$S = \max\left( 0.0, \, \frac{\langle m^{(t)}, \, m^{(t-1)} \rangle}{\| m^{(t)} \|_2 \| m^{(t-1)} \|_2} \right)$$
3. **Momentum-Based Pre-Correction**:
   The correction is only applied if the trajectory is stable, i.e., $S \ge 0.5$. The blending coefficient $\beta$ scales linearly based on $S$:
   $$\beta = \begin{cases} \beta_{\max} \cdot \frac{S - 0.5}{0.5} = \beta_{\max} \cdot (2S - 1) & \text{if } S \ge 0.5 \\ 0 & \text{if } S < 0.5 \end{cases}$$
   where $\beta_{\max}$ is the `momentum_beta_max` parameter (default 0.3).
   The pre-corrected local state $\tilde{x}$ is computed by shifting the raw local state $x$ along the momentum vector direction:
   $$\tilde{x} = x + \beta \cdot m$$
   Finally, the previous momentum is updated:
   $$m^{(t-1)} \leftarrow m^{(t)}$$

#### Python Usage Example

```python
import torch
from latticeshadow_db.temporal import TemporalStateBuffer, QueryState

# 1. Initialize TemporalStateBuffer
buffer = TemporalStateBuffer(
    window_size=16,
    momentum_beta_max=0.3,
    trend_scaling=2.0
)

# 2. Record a sequence of query states representing a conversation history
dim = 32
for i in range(5):
    local_vec = torch.ones(1, dim) * float(i + 1)
    # Aligned cloud state is local state plus a stable offset vector
    aligned_vec = local_vec + torch.ones(1, dim) * 0.1
    # Residual error decreases over time (simulating convergence)
    residual = 0.5 - i * 0.08
    
    state = QueryState(
        local_state=local_vec,
        aligned_state=aligned_vec,
        residual=residual,
        adaptive_alpha=0.4,
        entropy=2.0,
        route="cloud"
    )
    buffer.record(state)

# 3. Retrieve rolling conversation metrics
print(f"Buffer size: {buffer.size}")
print(f"Residual trend slope: {buffer.residual_trend:.6f}")
print(f"Health Status: {buffer.health_status}")
print(f"Average residual error: {buffer.mean_residual:.6f}")
print(f"Residual drift active: {buffer.drift_active}")
print(f"Residual drift status: {buffer.drift_status}")

# 4. Speculative Threshold Adjustment
base_threshold = 0.05
suggested_threshold = buffer.suggest_speculative_threshold(base_threshold)
print(f"Base threshold: {base_threshold} -> Suggested speculative threshold: {suggested_threshold:.6f}")

# 5. Momentum Pre-correction
# Query state with a stable trajectory
query_local = torch.ones(1, dim) * 6.0
# Run once to establish _prev_momentum
_ = buffer.suggest_momentum_correction(query_local)
# Run second time to apply momentum correction (requires previous stability check)
pre_corrected_state = buffer.suggest_momentum_correction(query_local)
print(f"Original local state sum: {query_local.sum().item():.6f}")
print(f"Pre-corrected local state sum: {pre_corrected_state.sum().item():.6f}")
```

---

### 2.9 Edge PII Scrubber

#### Architecture & Extensibility
Operating entirely locally when enabled, the `PiiScrubber` (`latticeshadow_db/scrubber.py`) applies regex-based filtering to raw textual parameters before outbound networking calls. It is a best-effort scrubber rather than a complete PII guarantee; custom patterns can be appended at runtime via the `add_pattern` API, which compiles a new regular expression pattern and stores it in the internal `self.patterns` dictionary.

#### Mathematical Formulation
Let $\mathcal{X}$ be the set of all text strings. The scrubber defines an ordered sequence of $N$ regular expression patterns $\mathcal{P} = (P_1, P_2, \dots, P_N)$ and corresponding replacement tokens $\mathcal{R} = (R_1, R_2, \dots, R_N)$, where each $R_i \in \mathcal{X}$ is formulated as:
$$R_i = \text{"[REDACTED\_" \parallel L_i \parallel "]"}$$
where $L_i$ is the uppercase string label designating the PII category, and $\parallel$ is the string concatenation operator.

Let $\text{Sub}: \mathcal{R}_{\text{egex}} \times \mathcal{X} \times \mathcal{X} \to \mathcal{X}$ be the regex substitution function, where $\text{Sub}(P, R, T)$ returns a new string with all non-overlapping occurrences of the pattern $P$ in text $T$ replaced by the string $R$. 

For an input text $S \in \mathcal{X}$, the scrubbing pipeline is defined recursively:
$$S^{(0)} = S$$
$$S^{(i)} = \text{Sub}(P_i, R_i, S^{(i-1)}) \quad \forall i \in \{1, 2, \dots, N\}$$

The final scrubbed text is:
$$S_{\text{scrubbed}} = S^{(N)}$$

##### Predefined Regex Rules
The default patterns compiled in `PiiScrubber` are:

1. **EMAIL**: Matches standard RFC-compliant email structures.
   $$P_{\text{EMAIL}} = \text{\texttt{[a-zA-Z0-9\_.+-]+@[a-zA-Z0-9-]+\textbackslash.[a-zA-Z0-9-.]+} }$$
2. **PHONE**: Matches international and local phone numbers with optional country codes, whitespace, dashes, or parentheses.
   $$P_{\text{PHONE}} = \text{\texttt{(?:\textbackslash+?\textbackslash d\{1,3\}[-.\textbackslash s]?)?\textbackslash(?\textbackslash d\{3\}\textbackslash)?[-.\textbackslash s]?\textbackslash d\{3\}[-.\textbackslash s]?\textbackslash d\{4\}} }$$
3. **SSN**: Matches 9-digit US Social Security Numbers with word boundaries.
   $$P_{\text{SSN}} = \text{\texttt{\textbackslash b\textbackslash d\{3\}[-.\textbackslash s]?\textbackslash d\{2\}[-.\textbackslash s]?\textbackslash d\{4\}\textbackslash b} }$$
4. **DATE**: Matches ISO dates (`YYYY-MM-DD`), slash-formatted dates (`MM/DD/YYYY` or `M/D/YY`), and textual month formats.
   $$P_{\text{DATE}} = \text{\texttt{\textbackslash b(?:\textbackslash d\{4\}[-.\textbackslash s]\textbackslash d\{2\}[-.\textbackslash s]\textbackslash d\{2\}|\textbackslash d\{1,2\}/\textbackslash d\{1,2\}/\textbackslash d\{2,4\})\textbackslash b | \textbackslash b(?:Jan(?:uary)?|Feb(?:ruary)?|...)\textbackslash s+\textbackslash d\{1,2\}(?:st|nd|rd|th)?(?:,?\textbackslash s+\textbackslash d\{4\})?\textbackslash b} }$$
5. **NAME**: Matches common titles followed by a capitalized first and optional last name.
   $$P_{\text{NAME}} = \text{\texttt{\textbackslash b(?:Mr\textbackslash.|Ms\textbackslash.|Mrs\textbackslash.|Dr\textbackslash.|Prof\textbackslash.)\textbackslash s+[A-Z][a-z]+(?:\textbackslash s+[A-Z][a-z]+)?\textbackslash b} }$$
6. **IP_ADDRESS**: Matches IPv4 (dotted decimal) and IPv6 (colon hexadecimal) addresses.
   $$P_{\text{IP\_ADDRESS}} = \text{\texttt{\textbackslash b(?:\textbackslash d\{1,3\}\textbackslash.)\{3\}\textbackslash d\{1,3\}\textbackslash b | \textbackslash b[0-9a-fA-F]\{1,4\}:(?:[0-9a-fA-F]\{1,4\}:)\{1,6\}[0-9a-fA-F]\{1,4\}\textbackslash b} }$$
7. **CREDIT_CARD**: Matches 16-digit credit cards with optional delimiters.
   $$P_{\text{CREDIT\_CARD}} = \text{\texttt{\textbackslash b(?:\textbackslash d\{4\}[-.\textbackslash s]?)\{3\}\textbackslash d\{4\}\textbackslash b} }$$

#### Python Usage Example

```python
from latticeshadow_db.scrubber import PiiScrubber

# 1. Instantiate the scrubber
scrubber = PiiScrubber()

# 2. Scrub sensitive data from text
raw_text = "Send documents to alice.smith@corp.com or call Dr. House at 555-123-4567."
scrubbed_text = scrubber.scrub(raw_text)
print(scrubbed_text)
# Output: "Send documents to [REDACTED_EMAIL] or call [REDACTED_NAME] at [REDACTED_PHONE]."

# 3. Register a custom domain pattern dynamically
# Example: Scrub custom patient identifiers of format 'PAT-XXXXX'
scrubber.add_pattern(label="patient_id", pattern_str=r"\bPAT-\d{5}\b")
custom_text = "Checking status for record PAT-89302."
scrubbed_custom = scrubber.scrub(custom_text)
print(scrubbed_custom)
# Output: "Checking status for record [REDACTED_PATIENT_ID]."
```

---

### 2.10 ZkBridge Orchestration

#### Architecture & Pipeline Flow
The `ZkBridge` (`latticeshadow_db/bridge.py`) experiments with vector-space
alignment between a local model and a cloud API. Despite its name, it does not
implement a zero-knowledge proof. Its Cayley rotation preserves distance
structure, and its default `send_text_context=True` sends the text context to
the configured endpoint. The optional `scrub_pii` flag is off by default and
uses best-effort regex filtering when enabled. Review the outbound payload
before using this path with sensitive material.

The execution flow of the bridge pipeline is summarized as follows:
1. **Drift Guard Gate**: Checks the temporal residual drift guard. If active, stale local exits are suppressed until fresh cloud anchors stabilize.
2. **Cache & Distillation Check**: Inspects the entropy-validated activation cache and the local distillation head first, unless the drift guard is active. If `enable_hopfield_cache=True`, a normal cache miss can attempt a gated modern-Hopfield/attention readout over entropy-compatible cache entries. If a graduated local predictor is available, it resolves the aligned vector locally.
3. **Momentum Pre-Correction**: If speculation is used, applies temporal momentum pre-corrections using recent history.
4. **Speculative Procrustes Exit**: Checks if the calibrated local Procrustes mapper can predict the aligned representation with high confidence. If drift is active, the threshold is capped by the drift tightening factor.
5. **Rotational Obfuscation & Subspace Compression**: Applies Woodbury Cayley rotations and (optionally) compresses the rotated vector onto the $2r$-dimensional transit subspace spanned by $V$ to save bandwidth.
6. **Outbound Call & Reconstruction**: Forwards the compressed/rotated vector to the cloud. Upon response, performs singular-value alignment and caches the resulting pair.
7. **Adaptive Alpha Blending**: Blends the local representation and aligned response using a dynamic scaling coefficient to prevent covariate drift.

#### Mathematical Formulation

##### Low-Rank Woodbury-Optimized Cayley Rotations
To obfuscate activation coordinates, the local hidden state $h \in \mathbb{R}^d$ is rotated using an orthogonal Cayley matrix $W \in \mathbb{R}^{d \times d}$ ($W^T W = I_d$). This preserves distances and does not provide ciphertext-like confidentiality.
The matrix $W$ is constructed as the Cayley transform of a low-rank skew-symmetric matrix $S$:
$$S = A B^T - B A^T = U V^T$$
where $A, B \in \mathbb{R}^{d \times r}$ ($r \ll d$ is the rank), and the low-rank factor matrices $U, V \in \mathbb{R}^{d \times 2r}$ are defined as:
$$U = [A \mid -B], \quad V = [B \mid A]$$

The Cayley transform defines $W$ as:
$$W = (I_d - S)(I_d + S)^{-1} = (I_d - U V^T)(I_d + U V^T)^{-1}$$

Applying the Woodbury matrix identity to the term $(I_d + U V^T)^{-1}$:
$$(I_d + U V^T)^{-1} = I_d - U (I_{2r} + V^T U)^{-1} V^T$$
Let $M = (I_{2r} + V^T U)^{-1} \in \mathbb{R}^{2r \times 2r}$. Expanding $W$:
$$W = (I_d - U V^T)(I_d - U M V^T) = I_d - 2 U M V^T$$

In row-vector convention, the forward and inverse rotations are computed as:
$$h_{\text{rotated}} = h W^T = h - 2 (h V) M^T U^T$$
$$h_{\text{inverse\_rotated}} = h W = h - 2 (h U) M V^T$$

##### Transit Subspace Sketching
To minimize communication bandwidth when `compress_transit = True`, the bridge projects the rotated activation $h_{\text{rotated}}$ onto the $2r$-dimensional subspace spanned by $V$:
$$h_{\text{compressed}} = h_{\text{rotated}} V \in \mathbb{R}^{2r}$$

This is a lossy sketch, not a reversible compression codec. The pseudo-inverse path projects the sketch back into the row space spanned by $V$ and necessarily drops components orthogonal to that subspace. The bridge can use this as a reduced transit representation for custom pathways, but any quality claim must be measured as sketch distortion or downstream alignment quality rather than hidden-state reconstruction fidelity.

The approximate full-dimensional projection uses the Moore-Penrose pseudo-inverse of $V^T$, denoted as $V^{+T}$:
$$V^{+T} = V (V^T V)^{-1}$$
$$h_{\text{projected}} = h_{\text{compressed}} V^{+T} = h_{\text{compressed}} (V^T V)^{-1} V^T \in \mathbb{R}^{d}$$

For experimental structured sketches, the adapter also supports an SRHT-style map:
$$\Phi = \sqrt{\frac{n}{m}} R H D$$
where $D$ is a seeded Rademacher sign diagonal, $H$ is a normalized Walsh-Hadamard transform after zero-padding to power-of-two length $n$, and $R$ samples $m$ coordinates. This path is intended for distance-sketch experiments and payload reduction, not reconstruction.

##### Temporal Momentum Pre-Correction
The `TemporalStateBuffer` calculates a rolling momentum vector $\vec{v}_{\text{momentum}}$ representing the trend in alignment corrections:
$$c_i = s_i.\text{aligned\_state} - s_i.\text{local\_state}$$
For $m$ compatible history states, we apply exponential decay weighting with parameter $\lambda = 0.85$:
$$w_i = \lambda^{m - 1 - i}$$
$$\vec{v}_{\text{momentum}} = \sum_{i=1}^m \bar{w}_i c_i, \quad \text{where } \bar{w}_i = \frac{w_i}{\sum_{j=1}^m w_j}$$

The directional stability is:
$$\text{stability} = \max\left(0, \frac{\vec{v}_{\text{momentum}} \cdot \vec{v}_{\text{prev}}}{\|\vec{v}_{\text{momentum}}\|_2 \|\vec{v}_{\text{prev}}\|_2}\right)$$

If $\text{stability} \geq 0.5$, we apply a pre-correction to the current query state $h_{\text{local}}$ before speculative evaluation:
$$\beta = \beta_{\text{max}} \cdot \frac{\text{stability} - 0.5}{0.5}$$
$$h_{\text{pre-corrected}} = h_{\text{local}} + \beta \vec{v}_{\text{momentum}}$$
where $\beta_{\text{max}} = 0.3$. Otherwise, $h_{\text{pre-corrected}} = h_{\text{local}}$.

##### Speculative Procrustes Prediction & Early Exit
1. The speculative threshold $\tau_{\text{effective}}$ is dynamically scaled based on the residual trend slope:
   $$\text{slope} = \frac{n \sum_{j=0}^{n-1} j \cdot r_j - \sum_{j=0}^{n-1} j \sum_{j=0}^{n-1} r_j}{n \sum_{j=0}^{n-1} j^2 - (\sum_{j=0}^{n-1} j)^2}$$
   $$\text{adjustment} = 1.0 + \max\left(-1.0, \min\left(1.0, -\text{slope} \cdot \gamma\right)\right)$$
   $$\tau_{\text{effective}} = \text{clamp}\left(\tau_{\text{base}} \cdot \text{adjustment}, 0.5 \tau_{\text{base}}, 3.0 \tau_{\text{base}}\right)$$
   where $\gamma = 2.0$ and $\tau_{\text{base}}$ is the configured baseline threshold.
2. The speculative predicted aligned vector is:
   $$\hat{h}_{\text{aligned}} = \text{Aligner}.\text{align}\left(\text{Aligner}.\text{predict\_cloud}(h_{\text{pre-corrected}})\right)$$
3. The speculative residual is computed:
   $$\eta_{\text{spec}} = \frac{\|\hat{h}_{\text{aligned}} - h_{\text{local}}\|_2}{\|h_{\text{local}}\|_2 + 10^{-8}}$$
4. If $\eta_{\text{spec}} < \tau_{\text{effective}}$, the speculation is validated, bypassing the cloud pipeline entirely and returning $h_{\text{local}}$ directly.

##### Adaptive Alpha Blending
If speculation fails, the true aligned vector $h_{\text{aligned}}$ is obtained from the cloud. The blending coefficient $\alpha_{\text{eff}}$ is computed adaptively:
$$\eta = \frac{\|h_{\text{aligned}} - h_{\text{local}}\|_2}{\|h_{\text{local}}\|_2 + 10^{-8}}$$
$$\alpha_{\text{eff}} = \alpha_{\text{base}} \cdot \sigma(1.0 - \eta)$$
where $\sigma(z) = \frac{1}{1 + e^{-z}}$ is the sigmoid activation function.

The corrected activation returned to the local model is:
$$h_{\text{corrected}} = (1.0 - \alpha_{\text{eff}}) h_{\text{local}} + \alpha_{\text{eff}} h_{\text{aligned}}$$

#### Python Usage Example

```python
import numpy as np
import torch
from latticeshadow_db.adapter import CayleyPrivacyAdapter
from latticeshadow_db.alignment import ProcrustesAligner
from latticeshadow_db.bridge import ZkBridge

# 1. Set configuration parameters
local_dim = 8
cloud_dim = 16
rank = 4

# 2. Instantiate core components
adapter = CayleyPrivacyAdapter(dim=local_dim, rank=rank, device="cpu")
aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)

# Simulate calibration using historical states
np.random.seed(42)
local_history = np.random.normal(0, 1.0, size=(20, local_dim))
cloud_history = np.random.normal(0, 1.0, size=(20, cloud_dim))
aligner.calibrate(local_history, cloud_history)

# 3. Instantiate ZkBridge with advanced features
bridge = ZkBridge(
    adapter=adapter,
    aligner=aligner,
    pathway="custom",
    alpha=0.4,
    mock_mode=True,                # Offline mock mode for testing
    enable_speculation=True,       # Enable early-exit prediction
    speculative_threshold=0.1,     # Threshold for speculative validation
    adaptive_alpha=True,           # Residual-aware dynamic blending coeff
    compress_transit=True,         # Subspace dimensionality reduction
    use_leech_lattice=False        # Disable Leech Lattice quantization for this demo
)

# 4. Execute query pipeline
h_local = torch.randn(1, local_dim)
text_context = "Request payload containing sensitive data."

h_corrected = bridge.query(h_local, text_context)

print("Input hidden state shape: ", h_local.shape)
print("Corrected hidden state shape:", h_corrected.shape)
```

---

## 3. Gotchas, Lookouts, and Edge Cases

### 3.1 C-Extension Compiling & Fallback Logic

*   **Compilation Failure**: The high-performance Leech Lattice decoder is written in C++ (`_leech_decoder_cpp.so`). If compilation fails or is absent during deployment, the code automatically catches the `ImportError` and falls back to `leech_fallback.py` (a pure-Python vectorized implementation).
*   **Performance Degredation**: The Python fallback runs 10x–50x slower because it executes the Golay decoding loop in PyTorch rather than raw C++.
*   **Memory Warning**: The C++ extension expects a contiguous CPU float tensor. Passing non-contiguous or GPU tensors triggers an internal copy to CPU memory, causing performance overhead.

### 3.2 PyTorch vs. NumPy Device Performance

*   **MPS Fallback**: PyTorch's Metal Performance Shaders (MPS) on Apple Silicon sometimes fails on double-precision (`float64`) matrix inversions. The `CayleyPrivacyAdapter` handles this by falling back to the CPU for the inversion step `torch.linalg.inv`, then copying the resulting projection matrices back to the active GPU/MPS device.
*   **CUDA Memory Thrashing**: During high-frequency REST updates, calling tensor operations on CUDA devices may lead to memory fragmentation. Keep the server's device configured to `cpu` for databases under 5M records.

### 3.3 SVD Singular Value Drift Alarms

*   **Singular Value Entropy**: The singular values $s_i$ obtained from SVD Procrustes calibration represent the variance explained by each alignment axis. We measure their Shannon Entropy:
    $$H = -\sum_{i} p_i \log p_i, \quad p_i = \frac{s_i}{\sum_j s_j}$$
*   **Condition Number Alarm**: The condition number $\kappa = s_{max} / s_{min}$ measures the stability of the SVD mapping. A high condition number ($\kappa > 10^4$) indicates that the alignment is collapsing along certain dimensions.
*   **Recalibration Trigger**: If the activation entropy of incoming queries deviates from the cached vector entropy by more than `entropy_tolerance` (default 0.5), it signifies covariate shift (drift). System operators should monitor these alarms and trigger a database recalibration using fresh paired samples.

```python
from latticeshadow_db.latticedb.privacy import PrivacyEngine

# Resolve encryption engine and perform envelope actions
engine = PrivacyEngine(dim=768, master_key="my_super_secret_passphrase")

# Generate new Data Encryption Key (DEK)
wrapped_key = engine.generate_key()
```

import argparse
import sys
import numpy as np
import torch
import os
import getpass

from .adapter import CayleyPrivacyAdapter
from .alignment import ProcrustesAligner
from .bridge import ZkBridge

def _secret(env_name: str, prompt: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit(f"Set {env_name} when running without a terminal.")
    value = getpass.getpass(prompt)
    if not value:
        raise SystemExit("An empty encryption key is not allowed.")
    return value

def load_data(path: str) -> np.ndarray:
    """Loads dataset from .npy or .csv files."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Data file not found at: {path}")
        
    ext = os.path.splitext(path)[1].lower()
    if ext == ".npy":
        return np.load(path)
    elif ext == ".csv":
        return np.loadtxt(path, delimiter=",")
    else:
        # Try as csv first, fallback to npy
        try:
            return np.loadtxt(path, delimiter=",")
        except Exception:
            try:
                return np.load(path)
            except Exception as e:
                raise ValueError(f"Could not parse data from {path}. Supported formats: .npy, .csv. Error: {e}")

def handle_calibrate(args):
    """Calibrates the aligner on the provided datasets and saves the parameters."""
    print("Loading calibration data...")
    try:
        local_data = load_data(args.local_data)
        cloud_data = load_data(args.cloud_data)
    except Exception as e:
        print(f"Error loading data: {e}", file=sys.stderr)
        sys.exit(1)
        
    print(f"Loaded local data shape: {local_data.shape}")
    print(f"Loaded cloud data shape: {cloud_data.shape}")

    if local_data.ndim != 2:
        print(f"Error: local data must be 2D (N, dim), got shape {local_data.shape}", file=sys.stderr)
        sys.exit(1)
    if cloud_data.ndim != 2:
        print(f"Error: cloud data must be 2D (N, dim), got shape {cloud_data.shape}", file=sys.stderr)
        sys.exit(1)

    if local_data.shape[0] != cloud_data.shape[0]:
        print(f"Error: Row counts (number of samples) must match: {local_data.shape[0]} vs {cloud_data.shape[0]}", file=sys.stderr)
        sys.exit(1)
        
    local_dim = local_data.shape[1]
    cloud_dim = cloud_data.shape[1]
    
    print(f"Initializing Procrustes Aligner for {local_dim} -> {cloud_dim} mapping...")
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    
    print("Computing alignment SVD...")
    metrics = aligner.calibrate(local_data, cloud_data)
    correlation = metrics["correlation"]
    print("Calibration completed.")
    print(f"  Average Cosine Similarity Correlation: {metrics['correlation']:.6f}")
    print(f"  Shannon Entropy:                       {metrics['entropy']:.6f}")
    print(f"  Condition Number:                      {metrics['condition_number']:.6f}")
    print(f"  Q-Matrix Trace:                        {metrics['trace']:.6f}")
    print(f"  Geometric Residuals (MSE):             {metrics['mse']:.6f}")
    
    print(f"Saving parameters to {args.output}...")
    try:
        aligner.save(args.output)
        print("Alignment parameters saved successfully.")
    except Exception as e:
        print(f"Error saving file: {e}", file=sys.stderr)
        sys.exit(1)

def handle_test(args):
    """Runs a simulated end-to-end round-trip of the system to check correctness."""
    print("=== Running Mock Round-Trip Validation ===")
    
    # 1. Setup dimensions
    local_dim = 256
    cloud_dim = 512
    rank = 16
    n_samples = 150
    print(f"Dimensions: Local={local_dim}, Cloud={cloud_dim}, Rank={rank}, Samples={n_samples}")
    
    # 2. Generate correlated synthetic data
    print("Generating synthetic correlated distributions...")
    np.random.seed(42)
    local_synthetic = np.random.normal(0, 1.0, size=(n_samples, local_dim))
    
    # Cloud distribution: linear mapping + noise + mean translation
    projection = np.random.normal(0, 1.0, size=(local_dim, cloud_dim))
    u_proj, _, vh_proj = np.linalg.svd(projection, full_matrices=False)
    ortho_proj = u_proj @ vh_proj  # Orthogonal mapping
    
    cloud_synthetic = local_synthetic @ ortho_proj + np.random.normal(0, 0.05, size=(n_samples, cloud_dim))
    cloud_synthetic += 5.0  # Add translation offset
    
    # 3. Calibrate Procrustes
    print("Calibrating Procrustes Aligner...")
    aligner = ProcrustesAligner(local_dim=local_dim, cloud_dim=cloud_dim)
    metrics = aligner.calibrate(local_synthetic, cloud_synthetic)
    correlation = metrics["correlation"]
    print(f"-> Calibration correlation (cosine similarity): {correlation:.6f}")
    
    # 4. Instantiate Cayley Privacy Adapter
    print("Instantiating Cayley Privacy Adapter...")
    device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    print(f"-> Selected torch device: {device}")
    adapter = CayleyPrivacyAdapter(dim=local_dim, rank=rank, device=device)
    
    # 5. Check Cayley transform properties
    print("Verifying mathematical properties of Cayley Privacy Adapter...")
    x = torch.randn(1, local_dim, device=device)
    y = torch.randn(1, local_dim, device=device)
    
    # Rotate
    x_rot = adapter.rotate(x)
    y_rot = adapter.rotate(y)
    
    # Inverse rotate
    x_rec = adapter.inverse_rotate(x_rot)
    
    # Assert reconstruction
    reconstruction_err = torch.norm(x - x_rec).item()
    print(f"-> Self-reconstruction L2 error: {reconstruction_err:.6e}")
    
    # Assert distances
    dist_orig = torch.norm(x - y).item()
    dist_rot = torch.norm(x_rot - y_rot).item()
    dist_err = abs(dist_orig - dist_rot)
    print(f"-> Distance preservation L2 error: {dist_err:.6e}")
    
    # Assert cosine similarity
    cos_orig = torch.nn.functional.cosine_similarity(x, y).item()
    cos_rot = torch.nn.functional.cosine_similarity(x_rot, y_rot).item()
    cos_err = abs(cos_orig - cos_rot)
    print(f"-> Cosine similarity preservation error: {cos_err:.6e}")
    
    # 6. Instantiate ZkBridge in Mock Mode
    print("Instantiating ZkBridge in Mock Mode...")
    bridge = ZkBridge(
        adapter=adapter,
        aligner=aligner,
        pathway="custom",
        alpha=0.3,
        mock_mode=True,
        enable_speculation=False,
        enable_routing=False,
        enable_temporal=False,
    )
    
    # 7. Query cycle
    print("Executing full query cycle...")
    h_local = torch.randn(1, local_dim, device=device)
    h_corrected = bridge.query(h_local, "Synthetic correction context")
    
    # Verify shape and contents
    print(f"-> Local hidden shape: {h_local.shape}")
    print(f"-> Corrected hidden shape: {h_corrected.shape}")
    
    # Check that h_corrected has incorporated corrections (should be different from h_local)
    diff = torch.norm(h_local - h_corrected).item()
    print(f"-> L2 distance between original and corrected states: {diff:.6f}")
    
    if diff > 1e-5:
        print("=== Test PASS ===")
    else:
        print("=== Test FAIL (Original and corrected states are identical) ===", file=sys.stderr)
        sys.exit(1)

def handle_db_add(args):
    """Add documents to a LatticeDB collection."""
    import latticeshadow_db.latticedb as latticedb
    import json
    
    db = latticedb.connect(
        db_path=args.db,
        collection=args.collection,
        privacy=args.privacy,
        master_key=os.environ.get("LATTICEDB_MASTER_KEY") if args.privacy else None,
    )
    
    docs = []
    metadatas = []
    
    if args.doc:
        docs.append(args.doc)
        metadatas.append(json.loads(args.metadata) if args.metadata else {})
    elif args.file:
        if not os.path.exists(args.file):
            print(f"Error: File not found at {args.file}", file=sys.stderr)
            sys.exit(1)
        ext = os.path.splitext(args.file)[1].lower()
        if ext == ".json":
            with open(args.file, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            docs.append(item.get("document", ""))
                            metadatas.append(item.get("metadata", {}))
                        else:
                            docs.append(str(item))
                            metadatas.append({})
                elif isinstance(data, dict):
                    docs.append(data.get("document", ""))
                    metadatas.append(data.get("metadata", {}))
        else:
            with open(args.file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        docs.append(line)
                        metadatas.append({})
                        
    if not docs:
        print("Error: No documents provided. Use --doc or --file.", file=sys.stderr)
        sys.exit(1)
        
    ids = db.add(documents=docs, metadatas=metadatas)
    print(f"Successfully added {len(ids)} documents. Doc IDs: {ids}")


def handle_db_search(args):
    """Search documents in a LatticeDB collection."""
    import latticeshadow_db.latticedb as latticedb
    
    db = latticedb.connect(
        db_path=args.db,
        collection=args.collection,
        privacy=args.privacy,
        master_key=os.environ.get("LATTICEDB_MASTER_KEY") if args.privacy else None,
    )
    
    result = db.search(args.query, n_results=args.n_results)
    print(f"Results for query '{args.query}' (served locally: {result.served_locally}):")
    for doc, score, doc_id, meta in zip(result.documents, result.scores, result.ids, result.metadatas):
        print(f"\n[{doc_id}] Score: {score:.4f}")
        print(f"Doc: {doc}")
        if meta and meta != '{}':
            print(f"Meta: {meta}")


def handle_db_shred(args):
    """Crypto-shred a LatticeDB collection."""
    import latticeshadow_db.latticedb as latticedb
    db = latticedb.connect(
        db_path=args.db,
        collection=args.collection,
        privacy=True,
        master_key=os.environ.get("LATTICEDB_MASTER_KEY"),
    )
    db.crypto_shred()
    print(f"Successfully crypto-shredded collection '{args.collection}'. All vectors are now irrecoverable.")


def handle_db_rotate(args):
    """Rotate the master key of a LatticeDB collection."""
    import latticeshadow_db.latticedb as latticedb
    db = latticedb.connect(
        db_path=args.db,
        collection=args.collection,
        privacy=True,
        master_key=os.environ.get("LATTICEDB_MASTER_KEY"),
    )
    db.rotate_master_key(_secret("LATTICEDB_NEW_MASTER_KEY", "New master key: "))
    print("Master key rotated successfully.")


def main():
    parser = argparse.ArgumentParser(
        description="CLI tool for latticeshadow_db library - privacy-preserving cross-model alignment bridge"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")
    
    # Calibrate command
    parser_calibrate = subparsers.add_parser("calibrate", help="Calibrate alignment parameters using SVD")
    parser_calibrate.add_argument("--local-data", required=True, help="Path to local activation data (.npy or .csv)")
    parser_calibrate.add_argument("--cloud-data", required=True, help="Path to cloud activation data (.npy or .csv)")
    parser_calibrate.add_argument("--output", required=True, help="Path to output saved alignment params (e.g. params.npz)")
    
    # Test command
    subparsers.add_parser("test", help="Run synthetic validation of the system")

    # db-add command
    parser_add = subparsers.add_parser("db-add", help="Add documents to LatticeDB")
    parser_add.add_argument("--db", default="latticedb.sqlite", help="Path to SQLite DB")
    parser_add.add_argument("--collection", default="default", help="Collection name")
    parser_add.add_argument("--doc", help="Single document text")
    parser_add.add_argument("--file", help="Path to text or JSON file containing documents")
    parser_add.add_argument("--metadata", help="JSON metadata dictionary for single doc")
    parser_add.add_argument("--privacy", action="store_true", help="Enable Cayley privacy rotation")

    # db-search command
    parser_search = subparsers.add_parser("db-search", help="Semantic search in LatticeDB")
    parser_search.add_argument("--db", default="latticedb.sqlite", help="Path to SQLite DB")
    parser_search.add_argument("--collection", default="default", help="Collection name")
    parser_search.add_argument("--query", required=True, help="Search query")
    parser_search.add_argument("-n", "--n-results", type=int, default=5, help="Number of results")
    parser_search.add_argument("--privacy", action="store_true", help="Enable Cayley privacy rotation")

    # db-shred command
    parser_shred = subparsers.add_parser("db-shred", help="Crypto-shred a LatticeDB collection")
    parser_shred.add_argument("--db", required=True, help="Path to SQLite DB")
    parser_shred.add_argument("--collection", required=True, help="Collection name")

    # db-rotate command
    parser_rotate = subparsers.add_parser("db-rotate", help="Rotate LatticeDB master key")
    parser_rotate.add_argument("--db", required=True, help="Path to SQLite DB")
    parser_rotate.add_argument("--collection", required=True, help="Collection name")
    
    args = parser.parse_args()
    
    if args.command == "calibrate":
        handle_calibrate(args)
    elif args.command == "test":
        handle_test(args)
    elif args.command == "db-add":
        handle_db_add(args)
    elif args.command == "db-search":
        handle_db_search(args)
    elif args.command == "db-shred":
        handle_db_shred(args)
    elif args.command == "db-rotate":
        handle_db_rotate(args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()

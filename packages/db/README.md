# LatticeShadow DB

`latticeshadow-db` is the SQLite-backed storage and retrieval library used by
[LatticeShadow CLI](../cli/README.md). It also works on its own, including on
systems without macOS. The library is a development prototype; the simple
`add` / `search` / `delete` path is the best place to start.

> **A crucial distinction:** With no embedding function, the DB uses a
> deterministic hash vector for testing. That makes an example runnable, but
> it does **not** make related sentences semantically close. Supply an embedding
> function to get meaningful natural-language retrieval.

## Try the core API

From the repository root, create an environment with `make setup-db` and then
activate it with `source .venv/bin/activate`. Alternatively, install this package
in an existing Python 3.9+ environment with `pip install -e ./packages/db`.

```python
from latticeshadow_db.latticedb import connect

db = connect(db_path="notes.sqlite", collection="demo")
db.add(
    documents=["The deployment checklist lives in docs/RELEASE.md"],
    ids=["release-checklist"],
)
results = db.search("The deployment checklist lives in docs/RELEASE.md", n_results=1)
print(results.ids, results.documents)
db.delete(["release-checklist"])
```

This exact-text example uses the hash fallback. For semantic search, install a
model library of your choice and pass a callable as `embedding_fn`. It must
return a vector of the configured `embedding_dim`; keep the same model and
dimension for every read and write to a collection. The macOS CLI uses a pinned
local model and records its identity so it can refuse mixed-model collections.

```python
from sentence_transformers import SentenceTransformer
from latticeshadow_db.latticedb import connect
import torch

model = SentenceTransformer(
    "sentence-transformers/static-retrieval-mrl-en-v1",
    revision="f60985c706f192d45d218078e49e5a8b6f15283a",
    truncate_dim=128,
)
db = connect(
    db_path="semantic-notes.sqlite",
    collection="notes",
    embedding_fn=lambda value: torch.as_tensor(model.encode(value)).float(),
    embedding_dim=128,
    embedding_model="static-retrieval-mrl-en-v1@f60985c7:128",
)
db.add(documents=["Release tests must pass before tagging."], ids=["release-tests"])
print(db.search("What should I do before a release?", n_results=1).documents)
```

The second example needs the optional `sentence-transformers` package and
downloads model files on first use. Model quality and retrieval quality depend
on the chosen model and your data.

## Privacy and limits

`privacy=True` encrypts stored document text and applies a secret, reversible
Cayley rotation to searchable vectors. The key is envelope-encrypted with
AES-GCM. Because the rotation preserves distances, anyone who obtains the
stored vectors can still infer similarity, clusters, and relative distances.
Document IDs, collection names, timestamps, and metadata are stored in readable
form, so do not put secrets in those fields. Experimental indexes may write
additional sidecar representations. This is not opaque vector encryption or a
zero-knowledge proof. Use an explicit
`master_key` when you need predictable key recovery across environments, and
protect database files and keys as sensitive data.

The optional Leech lattice index, alignment and distillation modules, and
retrieval strategies such as HNSW, FlyHash, PQ-lite, and DiskANN-inspired
sidecars are research paths. Their speed, recall, compression, and privacy
properties depend on the workload and configuration; none is a general
performance or security guarantee. Optional server endpoints add a network
boundary and should be evaluated separately before deployment.

## Find your way around

- [Root guide](../../README.md): product goal, setup, and what works today.
- [Technical tome](docs/TOME.md): architecture, algorithms, and experiments.
- [`latticeshadow_db/latticedb/`](latticeshadow_db/latticedb/): the core API and storage code.
- [`tests/`](tests/): runnable API and regression examples.
- [`scripts/`](scripts/): documentation checker and benchmark helpers.

From the repository root, run `make test-db` for the standard DB suite and
`make docs` to check links and Python examples. Benchmarks generate local
artifacts; they are experiments, not release acceptance tests.

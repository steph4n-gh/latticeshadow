"""SQLite rows remain authoritative when vector-sidecar work is interrupted."""
import sqlite3

import torch

from latticeshadow_db.latticedb.collection import Collection


def test_reopen_rebuilds_same_sized_sidecar_after_committed_delete(tmp_path):
    path = str(tmp_path / "interrupted.sqlite")
    embed = lambda text: torch.tensor([1.0, 0.0]) if text == "alpha" else torch.tensor([0.0, 1.0])
    original = Collection("docs", db_path=path, embedding_fn=embed, embedding_dim=2)
    original.add(["alpha", "beta"], ids=["a", "b"])
    with sqlite3.connect(path) as conn:
        blob = conn.execute("SELECT vector_blob FROM vectors WHERE doc_id = 'b'").fetchone()[0]
        conn.execute("DELETE FROM vectors WHERE doc_id = 'a'")
    # Mimics a process dying after the canonical transaction but before compacting
    # its already allocated vector sidecar.
    reopened = Collection("docs", db_path=path, embedding_fn=embed, embedding_dim=2)
    expected = reopened._store._blob_to_vector(blob)
    assert torch.equal(reopened._store._get_vector_at(0), expected)
    assert reopened.search("beta", n_results=1).ids == ["b"]
    assert reopened._store.list_deleted_ids()[0] == ["a"]

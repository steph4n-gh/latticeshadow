import sqlite3

import pytest

from latticeshadow.rebuild import rebuild_embeddings
from latticeshadow.vaults import embedding_model_id, open_hot_vault, open_main_vault
from latticeshadow_db.latticedb import connect


def test_legacy_vectors_require_explicit_rebuild(tmp_path):
    db_path = str(tmp_path / "memory.sqlite")
    key = "disposable-test-key"
    old = connect(db_path=db_path, collection="clipboard", embedding_dim=128,
                  privacy=True, drosophila_hash=True, master_key=key)
    old.add(documents=["restore the project invoice"], ids=["clip_123"],
            metadatas=[{"source": "clipboard"}])
    hot = connect(db_path=db_path, collection="clipboard_hot", embedding_dim=128,
                  privacy=True, experimental_index="streaming_exact", master_key=key)
    hot.add(documents=["restore the project invoice"], ids=["clip_123"],
            metadatas=[{"source": "clipboard"}])
    with sqlite3.connect(db_path) as conn:
        original_timestamp = conn.execute(
            "SELECT created_at FROM vectors WHERE collection = 'clipboard'"
        ).fetchone()[0]

    with pytest.raises(ValueError, match="Rebuild"):
        open_main_vault(db_path, key)
    count, backup = rebuild_embeddings(db_path, key)
    assert count == 2
    assert backup is not None

    reopened = open_main_vault(db_path, key)
    reopened_hot = open_hot_vault(db_path, key)
    assert reopened.search("restore the project invoice", n_results=1).ids == ["clip_123"]
    assert reopened_hot.search("restore the project invoice", n_results=1).ids == ["clip_123"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT embedding_model FROM collection_meta WHERE name = 'clipboard'").fetchone()[0] == embedding_model_id()
        row = conn.execute("SELECT doc_id, metadata_json, created_at FROM vectors WHERE collection = 'clipboard'").fetchone()
        assert row[0] == "clip_123"
        assert "clipboard" in row[1]
        assert row[2] == original_timestamp
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT embedding_model FROM collection_meta WHERE name = 'clipboard'").fetchone()[0] is None
    repeated_count, repeated_backup = rebuild_embeddings(db_path, key)
    assert repeated_count == 2
    assert repeated_backup and repeated_backup != backup
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT embedding_model FROM collection_meta WHERE name = 'clipboard'").fetchone()[0] is None
    assert open_main_vault(db_path, key).search("restore the project invoice", n_results=1).ids == ["clip_123"]


def test_failed_rebuild_keeps_original_vectors(tmp_path, monkeypatch):
    db_path = str(tmp_path / "memory.sqlite")
    old = connect(db_path=db_path, collection="clipboard", embedding_dim=128,
                  privacy=True, drosophila_hash=True, master_key="key")
    old.add(documents=["keep this event"], ids=["clip_1"])

    def fail(_text):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("latticeshadow.rebuild.embed_text", fail)
    with pytest.raises(RuntimeError, match="model unavailable"):
        rebuild_embeddings(db_path, "key")
    assert old.search("keep this event", n_results=1).ids == ["clip_1"]
    assert not list(tmp_path.glob("*.bak"))


def test_rebuild_accepts_pre_migration_schema(tmp_path):
    db_path = str(tmp_path / "old.sqlite")
    old = connect(db_path=db_path, collection="clipboard", embedding_dim=128,
                  privacy=True, drosophila_hash=True, master_key="key")
    old.add(documents=["legacy event"], ids=["clip_legacy"])
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE collection_meta DROP COLUMN embedding_model")

    count, backup = rebuild_embeddings(db_path, "key")
    assert count == 1
    assert backup
    assert open_main_vault(db_path, "key").search("legacy event", n_results=1).ids == ["clip_legacy"]

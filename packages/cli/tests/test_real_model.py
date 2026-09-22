"""Small labeled corpus for the pinned, local retrieval model."""

import pytest

from latticeshadow.vaults import open_hot_vault, open_main_vault
from latticeshadow.rebuild import rebuild_embeddings
from latticeshadow_db.latticedb import connect

pytestmark = pytest.mark.model


def test_paraphrased_recall_in_both_vaults(tmp_path, monkeypatch):
    monkeypatch.delenv("LATTICESHADOW_EMBEDDING_MODEL", raising=False)
    records = {
        "tests": "Run pytest before making a release to catch regressions.",
        "daemon": "Restart the background daemon with launchctl load after updating its plist.",
        "invoice": "The September invoice is due on Friday afternoon.",
        "backup": "Back up the SQLite database before changing its schema.",
        "docs": "The SQLite Python reference is at https://docs.python.org/3/library/sqlite3.html",
        "sync": "Use rsync -av to copy build assets to the staging server.",
        "meeting": "Meet Ava at 2 PM to review the design mockups.",
        "key": "Rotate the production API key before the next deployment.",
    }
    cases = {
        "tests": "How should I check for regressions before shipping?",
        "daemon": "How do I restart the background service?",
        "invoice": "When is the bill due?",
        "backup": "What should I do before a database migration?",
        "docs": "Where is the Python SQLite documentation?",
        "sync": "How can I transfer build files to the test server?",
        "meeting": "Who am I seeing to discuss the mockups?",
        "key": "Which secret must be rotated before deployment?",
    }
    db_path = str(tmp_path / "recall.sqlite")
    for vault in (open_main_vault(db_path, "disposable-key"),
                  open_hot_vault(db_path, "disposable-key")):
        vault.add(documents=list(records.values()), ids=list(records))
        for expected, query in cases.items():
            assert vault.search(query, n_results=1).ids == [expected]


def test_rebuild_moves_legacy_hash_vectors_to_local_model(tmp_path, monkeypatch):
    monkeypatch.delenv("LATTICESHADOW_EMBEDDING_MODEL", raising=False)
    db_path = str(tmp_path / "legacy.sqlite")
    documents = ["Back up SQLite before changing the schema.",
                 "Meet Ava at 2 PM to review the mockups.",
                 "Use rsync to copy build assets to staging."]
    ids = ["backup", "meeting", "sync"]
    for collection, hashed in (("clipboard", True), ("clipboard_hot", False)):
        vault = connect(db_path=db_path, collection=collection, embedding_dim=128,
                        privacy=True, drosophila_hash=hashed,
                        experimental_index="streaming_exact" if not hashed else None,
                        master_key="disposable-key")
        vault.add(documents=documents, ids=ids)
    count, backup = rebuild_embeddings(db_path, "disposable-key")
    assert count == 6
    assert backup
    for vault in (open_main_vault(db_path, "disposable-key"),
                  open_hot_vault(db_path, "disposable-key")):
        assert vault.search("What should I do before a database migration?", n_results=1).ids == ["backup"]

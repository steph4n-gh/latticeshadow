"""
Tests for backup export/import and multi-modal inputs in LatticeDB.
"""

import os
import torch
import pytest

from latticeshadow_db.latticedb import connect, Collection


def test_backup_export_import(tmp_path):
    """Verify collection backup export and import works cleanly."""
    db_file_1 = str(tmp_path / "source.sqlite")
    db_file_2 = str(tmp_path / "dest.sqlite")

    # 1. Connect and populate source database (encrypted)
    db1 = connect(db_path=db_file_1, collection="my_col", privacy=True, master_key="secret")
    db1.add(
        documents=["Patient records.", "Treatment plan."],
        metadatas=[{"dept": "cardiology"}, {"dept": "oncology"}]
    )
    assert db1.count() == 2

    # Export data
    export_dict = db1.export_data()
    assert export_dict["collection"] == "my_col"
    assert len(export_dict["records"]) == 2
    assert export_dict["encrypted_key_blob"] is not None

    # 2. Connect to destination and import
    db2 = connect(db_path=db_file_2, collection="my_col", privacy=True, master_key="secret")
    assert db2.count() == 0

    db2.import_data(export_dict)
    assert db2.count() == 2

    # Query destination to verify decryption/search works
    res = db2.search("patient department", n_results=1)
    assert len(res.ids) == 1
    assert res.documents[0] == "Patient records."
    assert res.metadatas[0]["dept"] == "cardiology"


def test_precomputed_tensor_embedding(tmp_path):
    """Verify that we can directly pass pre-computed tensors to add()."""
    db_file = str(tmp_path / "precomputed.sqlite")
    db = connect(db_path=db_file, embedding_dim=128)
    
    # Check that we can pass a tensor directly
    vec = torch.randn(128)
    
    # We bypass embedder text call by setting local_fn to return the same tensor
    db._embedder._local_fn = lambda x: vec
    
    ids = db.add(documents=["mock_doc"])
    assert len(ids) == 1
    
    # Test getting back vector
    retrieved = db._store.get_vector(ids[0])
    assert retrieved is not None
    assert retrieved.shape == torch.Size([128])

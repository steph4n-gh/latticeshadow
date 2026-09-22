import pytest

from latticeshadow_db.latticedb import connect


def test_collection_rejects_changed_or_unrecorded_embedding_model(tmp_path):
    db_path = str(tmp_path / "vectors.sqlite")
    first = connect(db_path=db_path, collection="notes", embedding_dim=4,
                    embedding_model="model-a")
    first.add(documents=["run the tests"], ids=["one"])

    assert connect(db_path=db_path, collection="notes", embedding_dim=4,
                   embedding_model="model-a").count() == 1
    with pytest.raises(ValueError, match="different or unrecorded embedding model"):
        connect(db_path=db_path, collection="notes", embedding_dim=4,
                embedding_model="model-b")
    with pytest.raises(ValueError, match="different or unrecorded embedding model"):
        connect(db_path=db_path, collection="notes", embedding_dim=8,
                embedding_model="model-a")
    with pytest.raises(ValueError, match="Pass the same embedding_model"):
        connect(db_path=db_path, collection="notes", embedding_dim=4)

    legacy = connect(db_path=db_path, collection="legacy", embedding_dim=4)
    legacy.add(documents=["older note"], ids=["older"])
    with pytest.raises(ValueError, match="different or unrecorded embedding model"):
        connect(db_path=db_path, collection="legacy", embedding_dim=4,
                embedding_model="model-a")

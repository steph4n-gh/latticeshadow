"""Keep standard tests independent of model downloads and personal data."""

import pytest


@pytest.fixture(autouse=True)
def use_test_embeddings(monkeypatch):
    monkeypatch.setenv("LATTICESHADOW_EMBEDDING_MODEL", "hash")

from types import SimpleNamespace

from latticeshadow import vaults


def test_bundled_model_uses_local_snapshot_and_keeps_pinned_identity(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("LATTICESHADOW_BUNDLED_MODEL", str(tmp_path / "model"))
    monkeypatch.delenv("LATTICESHADOW_EMBEDDING_MODEL", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=lambda *args, **kwargs: calls.append((args, kwargs)) or object()))
    vaults._load_model.cache_clear()
    try:
        vaults._local_model()
        assert calls == [((str(tmp_path / "model"),), {
            "local_files_only": True, "truncate_dim": vaults.EMBEDDING_DIM, "device": "cpu"})]
        assert vaults.embedding_model_id().startswith(vaults.EMBEDDING_MODEL + "@" + vaults.EMBEDDING_REVISION)
    finally:
        vaults._load_model.cache_clear()

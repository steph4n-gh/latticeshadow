import importlib
import asyncio

import pytest


def test_server_app_imports():
    server = importlib.import_module("latticeshadow_db.server")

    assert server.app.title == "LatticeDB REST Lock Server"


def test_server_uvicorn_target_points_to_package_module():
    source = importlib.import_module("latticeshadow_db.server")

    assert hasattr(source, "app")


def test_server_collection_config_accepts_experimental_index():
    server = importlib.import_module("latticeshadow_db.server")

    for strategy in (
        "hnsw_rerank",
        "hnsw_exact_rerank",
        "flyhash_rerank",
        "flyhash_exact_rerank",
        "pq_rerank",
        "pq_exact_rerank",
        "flyhash_pq_rerank",
        "diskann_rerank",
        "streaming_exact",
        "cascade_auto",
    ):
        config = server.CollectionConfig(experimental_index=strategy)
        assert config.experimental_index == strategy
    tuned = server.CollectionConfig(
        experimental_index="hnsw_exact_rerank",
        hnsw_m=48,
        hnsw_ef_construction=300,
        hnsw_ef_search=256,
        hnsw_candidate_cap=2000,
    )
    assert tuned.hnsw_m == 48
    assert tuned.hnsw_ef_search == 256


def test_server_collection_cache_keys_include_experimental_index(tmp_path, monkeypatch):
    server = importlib.import_module("latticeshadow_db.server")
    monkeypatch.setattr(server, "SERVER_DB_ROOT", tmp_path)

    async def run():
        async with server._cache_lock:
            server._collections_cache.clear()

        base_kwargs = dict(
            collection_name="cache_test",
            db_path="cache.sqlite",
            embedding_dim=8,
            privacy=False,
            auto_distill=False,
            lattice_index=False,
            max_entries=0,
            drosophila_hash=False,
            engine="default",
            master_key="test-key",
        )
        default_collection = await server.get_cached_collection(**base_kwargs)
        hnsw_collection = await server.get_cached_collection(
            **base_kwargs,
            experimental_index="hnsw_rerank",
        )
        tuned_hnsw_collection = await server.get_cached_collection(
            **base_kwargs,
            experimental_index="hnsw_exact_rerank",
            hnsw_ef_search=256,
        )

        return default_collection, hnsw_collection, tuned_hnsw_collection, len(server._collections_cache)

    default_collection, hnsw_collection, tuned_hnsw_collection, cache_size = asyncio.run(run())
    assert default_collection is not hnsw_collection
    assert tuned_hnsw_collection is not hnsw_collection
    assert default_collection.experimental_index is None
    assert hnsw_collection.experimental_index == "hnsw_rerank"
    assert tuned_hnsw_collection.experimental_index == "hnsw_exact_rerank"
    assert tuned_hnsw_collection._store._hnsw_ef_search == 256
    assert cache_size == 3


def test_server_rejects_absolute_or_traversing_db_paths(tmp_path, monkeypatch):
    server = importlib.import_module("latticeshadow_db.server")
    monkeypatch.setattr(server, "SERVER_DB_ROOT", tmp_path / "server-root")

    with pytest.raises(server.HTTPException) as absolute_exc:
        server.resolve_server_db_path(str(tmp_path / "outside.sqlite"))
    assert absolute_exc.value.status_code == 400

    with pytest.raises(server.HTTPException) as traversal_exc:
        server.resolve_server_db_path("../outside.sqlite")
    assert traversal_exc.value.status_code == 400


def test_server_resolves_relative_db_paths_under_storage_root(tmp_path, monkeypatch):
    server = importlib.import_module("latticeshadow_db.server")
    root = tmp_path / "server-root"
    monkeypatch.setattr(server, "SERVER_DB_ROOT", root)

    resolved = server.resolve_server_db_path("tenant/cache.sqlite")

    assert resolved == str(root.resolve() / "tenant" / "cache.sqlite")
    assert (root / "tenant").is_dir()


def test_server_collection_config_bounds_embedding_dim():
    server = importlib.import_module("latticeshadow_db.server")

    with pytest.raises(Exception):
        server.CollectionConfig(embedding_dim=server.MAX_SERVER_EMBEDDING_DIM + 1)

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from latticeshadow_db import cloud_server, server
from latticeshadow_db.http_limits import NonlocalTlsGuard, RequestBodyLimit


def test_db_api_token_is_distinct_from_encryption_key(monkeypatch):
    monkeypatch.setenv("LATTICEDB_MASTER_KEY", "encryption-key")
    monkeypatch.setenv("LATTICEDB_API_TOKEN", "rest-token")
    seen = []

    async def fake_collection(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(count=lambda: 7)

    monkeypatch.setattr(server, "get_cached_collection", fake_collection)
    client = TestClient(server.app)
    url = "/api/collections/example/count"

    assert client.post(url, json={}, headers={"Authorization": "Bearer encryption-key"}).status_code == 401
    response = client.post(url, json={}, headers={"Authorization": "Bearer rest-token"})
    assert response.status_code == 200
    assert response.json()["count"] == 7
    assert seen[0]["master_key"] == "encryption-key"

    monkeypatch.setenv("LATTICEDB_API_TOKEN", "encryption-key")
    assert client.post(url, json={}, headers={"Authorization": "Bearer encryption-key"}).status_code == 503


def test_rest_rotation_fails_before_touching_collection(monkeypatch):
    monkeypatch.setenv("LATTICEDB_MASTER_KEY", "encryption-key")
    monkeypatch.setenv("LATTICEDB_API_TOKEN", "rest-token")

    async def unexpected_collection(**kwargs):
        raise AssertionError("rotation opened a collection")

    monkeypatch.setattr(server, "get_cached_collection", unexpected_collection)
    response = TestClient(server.app).post(
        "/api/collections/example/rotate",
        headers={"Authorization": "Bearer rest-token"},
    )
    assert response.status_code == 501
    assert "DB CLI" in response.json()["detail"]


def test_shred_invalidates_only_affected_cached_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("LATTICEDB_MASTER_KEY", "encryption-key")
    monkeypatch.setenv("LATTICEDB_API_TOKEN", "rest-token")
    monkeypatch.setattr(server, "SERVER_DB_ROOT", tmp_path)

    async def fake_collection(**kwargs):
        return SimpleNamespace(crypto_shred=lambda: None)

    monkeypatch.setattr(server, "get_cached_collection", fake_collection)
    db_path = str(tmp_path / "cache.sqlite")
    affected = (db_path, "target", "settings")
    untouched = (db_path, "other", "settings")
    server._collections_cache.clear()
    server._collections_cache[affected] = object()
    server._collections_cache[untouched] = object()
    try:
        response = TestClient(server.app).post(
            "/api/collections/target/shred",
            json={"config": {"db_path": "cache.sqlite"}},
            headers={"Authorization": "Bearer rest-token"},
        )
        assert response.status_code == 200
        assert affected not in server._collections_cache
        assert untouched in server._collections_cache
    finally:
        server._collections_cache.clear()


def test_fastapi_servers_reject_oversized_bodies_before_auth(monkeypatch):
    monkeypatch.delenv("LATTICEDB_API_TOKEN", raising=False)
    oversized = {"Content-Length": str(8 * 1024 * 1024 + 1)}
    response = TestClient(server.app).post("/api/collections/example/add", content=b"{}", headers=oversized)
    assert response.status_code == 413

    monkeypatch.setitem(cloud_server.CONFIG, "api_key", None)
    response = TestClient(cloud_server.app).post("/v1/align", content=b"{}", headers=oversized)
    assert response.status_code == 413


def test_body_limit_checks_streamed_bytes_without_content_length():
    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    middleware = RequestBodyLimit(inner, max_bytes=4)
    chunks = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"45", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "headers": [], "method": "POST"}, receive, send))
    assert not called
    assert sent[0]["status"] == 413


def test_nonlocal_http_request_is_rejected_before_application():
    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "scheme": "http", "client": ("192.0.2.10", 3000), "method": "POST"}
    asyncio.run(NonlocalTlsGuard(inner)(scope, receive, send))
    assert not called
    assert sent[0]["status"] == 403


def test_nonlocal_servers_require_tls(monkeypatch):
    monkeypatch.setenv("LATTICEDB_HOST", "0.0.0.0")
    monkeypatch.delenv("LATTICEDB_SSL_KEYFILE", raising=False)
    monkeypatch.delenv("LATTICEDB_SSL_CERTFILE", raising=False)
    with pytest.raises(SystemExit, match="requires"):
        server.main()

    monkeypatch.setitem(cloud_server.CONFIG, "api_key", "cloud-token")
    monkeypatch.setattr(sys, "argv", ["cloud-server", "--host", "0.0.0.0"])
    monkeypatch.delenv("ZK_CLOUD_SSL_KEYFILE", raising=False)
    monkeypatch.delenv("ZK_CLOUD_SSL_CERTFILE", raising=False)
    with pytest.raises(SystemExit):
        cloud_server.main()


def test_cloud_server_defaults_to_loopback(monkeypatch):
    monkeypatch.setitem(cloud_server.CONFIG, "api_key", "cloud-token")
    monkeypatch.setattr(sys, "argv", ["cloud-server"])
    monkeypatch.delenv("ZK_CLOUD_SSL_KEYFILE", raising=False)
    monkeypatch.delenv("ZK_CLOUD_SSL_CERTFILE", raising=False)
    import uvicorn

    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    cloud_server.main()
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["limit_concurrency"] == 32


def test_db_server_limits_concurrent_requests(monkeypatch):
    monkeypatch.setenv("LATTICEDB_HOST", "127.0.0.1")
    import uvicorn

    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    server.main()
    assert run.call_args.kwargs["limit_concurrency"] == 32

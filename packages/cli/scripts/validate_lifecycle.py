#!/usr/bin/env python3
"""Synthetic lifecycle, fault, and performance checks for an isolated vault.

No personal clipboard, history, Keychain entry, or default data directory is read.
The long profiles are deliberately opt-in. Reports contain timings and counts,
never remembered text, keys, or local paths.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import select
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import deque


ROOT = Path(__file__).resolve().parents[3]
KEY = "synthetic-lifecycle-key"
PASSPHRASE = "synthetic portable backup passphrase"
STAMP = "2026-09-17T14:03:00Z"
SYNTHETIC = (
    "deployment api returned 503; retry after restarting the local proxy",
    "look up incident INC-4827 and compare the cache headers",
    "python -m pytest packages/cli/tests/test_timeline_contract.py",
    "https://example.invalid/runbooks/restart-proxy",
)


def _imports():
    from latticeshadow.backup import export_backup, restore_backup
    from latticeshadow.timeline import add_event, fetch_events, forget_events, get_events, search_events
    from latticeshadow.vaults import open_main_vault, vault_file_paths
    return export_backup, restore_backup, add_event, fetch_events, forget_events, get_events, search_events, open_main_vault, vault_file_paths


def _env(*, model: str = "hash") -> dict[str, str]:
    env = os.environ.copy()
    env["LATTICESHADOW_EMBEDDING_MODEL"] = model
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "packages" / "cli"), str(ROOT / "packages" / "db"), env.get("PYTHONPATH", "")))
    return env


def _worker(db: Path, action: str, doc_id: str) -> None:
    _, _, add_event, _, forget_events, get_events, search_events, open_main_vault, _ = _imports()
    vault = open_main_vault(str(db), KEY, device="cpu")
    if action == "add":
        if os.environ.get("LS_VALIDATION_BARRIER"):
            barrier = Path(os.environ["LS_VALIDATION_BARRIER"])
            barrier.with_name(f"{barrier.name}.{os.getpid()}.ready").touch()
            deadline = time.monotonic() + 30
            while not barrier.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("concurrent writer barrier was not released")
                time.sleep(0.01)
        add_event(vault, "note", SYNTHETIC[0], source="manual", timestamp=STAMP,
                  project="synthetic", doc_id=doc_id)
    elif action == "precommit-crash":
        def stop(*_args, **_kwargs):
            os._exit(43)
        vault._store.insert_batch = stop
        add_event(vault, "note", SYNTHETIC[0], source="manual", timestamp=STAMP,
                  project="synthetic", doc_id=doc_id)
    elif action in ("postcommit-crash", "delete-crash"):
        def stop(_revision):
            os._exit(44)
        vault._store._write_vector_manifest = stop
        if action == "postcommit-crash":
            add_event(vault, "note", SYNTHETIC[0], source="manual", timestamp=STAMP,
                      project="synthetic", doc_id=doc_id)
        else:
            forget_events(vault, [doc_id])
    elif action == "delete":
        result = forget_events(vault, [doc_id])
        if result["canonical_deleted"] != 1 or result["cleanup_errors"]:
            raise AssertionError(result)
    elif action == "read":
        assert len(get_events(vault, [doc_id])) == 1
    elif action == "rebuild-crash":
        import latticeshadow.rebuild as rebuild
        def stop(_text):
            os._exit(46)
        rebuild.embed_text = stop
        rebuild.rebuild_embeddings(str(db), KEY)
    elif action == "restore-crash":
        import latticeshadow.backup as backup
        original_open = backup.open_main_vault
        def interrupted_open(*args, **kwargs):
            destination = original_open(*args, **kwargs)
            original_add = destination.add
            def interrupted_add(*add_args, **add_kwargs):
                original_add(*add_args, **add_kwargs)
                os._exit(45)
            destination.add = interrupted_add
            return destination
        backup.open_main_vault = interrupted_open
        backup.restore_backup(doc_id, str(db.parent / "interrupted.sqlite"),
                              PASSPHRASE, "destination-key")
    else:
        raise ValueError(f"unsupported worker action: {action}")


def _run_worker(db: Path, action: str, doc_id: str, *, expected: int = 0) -> None:
    result = subprocess.run(
        [sys.executable, __file__, "_worker", str(db), action, doc_id],
        env=_env(), capture_output=True, text=True, timeout=120,
    )
    if result.returncode != expected:
        raise AssertionError(f"worker {action} exited {result.returncode}, expected {expected}: {result.stderr[-1200:]}")


def _assert_missing(vault, doc_id: str) -> None:
    _, _, _, _, _, get_events, search_events, _, _ = _imports()
    assert get_events(vault, [doc_id]) == []
    assert all(row["id"] != doc_id for row in search_events(vault, "deployment proxy"))


def _concurrent_writers(db: Path) -> None:
    barrier = db.parent / "writer-barrier"
    env = _env()
    env["LS_VALIDATION_BARRIER"] = str(barrier)
    processes = [subprocess.Popen(
        [sys.executable, __file__, "_worker", str(db), "add", f"parallel-{i}"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for i in range(2)]
    try:
        deadline = time.monotonic() + 30
        while len(list(db.parent.glob("writer-barrier.*.ready"))) < 2:
            if time.monotonic() > deadline:
                raise TimeoutError("concurrent writers did not reach their barrier")
            time.sleep(0.01)
        barrier.touch()
        for process in processes:
            _, error = process.communicate(timeout=120)
            if process.returncode:
                raise AssertionError(f"concurrent writer exited {process.returncode}: {error[-1200:]}")
    finally:
        barrier.unlink(missing_ok=True)
        for ready in db.parent.glob("writer-barrier.*.ready"):
            ready.unlink(missing_ok=True)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def _mcp_journey(directory: Path) -> None:
    """Exercise CLI grant commands and a real stdio server over one synthetic vault."""
    _, _, add_event, _, forget_events, get_events, _, open_main_vault, _ = _imports()
    destination = directory / "mcp-vault"
    destination.mkdir(mode=0o700)
    key = "a" * 64
    key_file = destination / ".key"
    key_file.write_text(key, encoding="ascii")
    key_file.chmod(0o600)
    db = destination / "shadow.sqlite"
    vault = open_main_vault(str(db), key, device="cpu")
    allowed = add_event(vault, "note", "deploy proxy after api_key=example-secret-value",
                        source="manual", project="synthetic", doc_id="synthetic-citation")
    excluded = add_event(vault, "note", "finance deploy proxy",
                         source="manual", project="finance", doc_id="excluded-citation")
    cli = [sys.executable, "-m", "latticeshadow.shadow_cli", "mcp"]
    env = _env()
    home = directory / "empty-home"
    home.mkdir(mode=0o700)
    env["HOME"] = str(home)
    created = subprocess.run(
        [*cli, "grant", "create", "--project", "synthetic", "--source", "manual",
         "--limit", "5", "--vault-dir", str(destination)],
        env=env, capture_output=True, text=True, timeout=120, check=True,
    )
    grant = json.loads(created.stdout)
    preview = subprocess.run(
        [*cli, "grant", "preview", grant["id"], "--vault-dir", str(destination)],
        env=env, capture_output=True, text=True, timeout=120, check=True,
    )
    assert json.loads(preview.stdout)["count"] == 1

    def start_server():
        return subprocess.Popen(
            [*cli, "serve", "--grant", grant["id"], "--vault-dir", str(destination)],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def ask(server, request_id: int, method: str, params: dict | None = None) -> dict:
        assert server.stdin is not None and server.stdout is not None
        server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                       "method": method, "params": params or {}}) + "\n")
        server.stdin.flush()
        readable, _, _ = select.select([server.stdout], [], [], 30)
        if not readable:
            raise TimeoutError(f"MCP {method} did not reply")
        line = server.stdout.readline()
        if not line:
            raise AssertionError(f"MCP {method} closed without a reply")
        return json.loads(line)

    def close(server):
        assert server.stdin is not None
        server.stdin.close()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)
        if server.returncode:
            assert server.stderr is not None
            raise AssertionError(f"MCP server exited {server.returncode}: {server.stderr.read()[-1200:]}")

    uri = "latticeshadow://event/synthetic-citation"
    server = start_server()
    try:
        response = ask(server, 1, "initialize", {"protocolVersion": "2025-06-18",
                        "capabilities": {}, "clientInfo": {"name": "lifecycle-validation", "version": "1"}})
        assert response["result"]["protocolVersion"] == "2025-06-18"
        recalled = ask(server, 2, "tools/call", {"name": "latticeshadow.recall",
                       "arguments": {"query": "deploy proxy", "limit": 5}})
        events = recalled["result"]["structuredContent"]["events"]
        assert [event["id"] for event in events] == [allowed]
        assert events[0]["citation"] == uri
        assert events[0]["redacted"] is True
        assert "example-secret-value" not in json.dumps(events)
        assert excluded not in json.dumps(events)
        resolved = ask(server, 3, "resources/read", {"uri": uri})
        assert json.loads(resolved["result"]["contents"][0]["text"])["event"]["id"] == allowed
        assert forget_events(vault, [allowed])["canonical_deleted"] == 1
        assert not get_events(vault, [allowed])
        after = ask(server, 4, "resources/read", {"uri": uri})
        assert after["error"] == {"code": -32602, "message": "Event unavailable"}
    finally:
        close(server)
    restarted = start_server()
    try:
        after_restart = ask(restarted, 5, "resources/read", {"uri": uri})
        assert after_restart["error"] == {"code": -32602, "message": "Event unavailable"}
    finally:
        close(restarted)


def lifecycle(directory: Path) -> dict:
    export_backup, restore_backup, add_event, fetch_events, forget_events, get_events, search_events, open_main_vault, vault_file_paths = _imports()
    db = directory / "lifecycle.sqlite"
    vault = open_main_vault(str(db), KEY, device="cpu")
    checks: list[str] = []

    # A reader opened before another process changes the vault must observe both
    # the new row and its later deletion.
    _run_worker(db, "add", "separate-writer")
    assert get_events(vault, ["separate-writer"])[0]["timestamp"].startswith("2026-09-17T14:03:00")
    assert search_events(vault, "deployment proxy")[0]["id"] == "separate-writer"
    _run_worker(db, "delete", "separate-writer")
    _assert_missing(vault, "separate-writer")
    checks.append("existing-reader-separate-writer-and-delete")

    _concurrent_writers(db)
    assert {event["id"] for event in get_events(vault, ["parallel-0", "parallel-1"])} == {
        "parallel-0", "parallel-1"}
    assert {event["id"] for event in search_events(vault, "deployment proxy")} >= {
        "parallel-0", "parallel-1"}
    forget_events(vault, ["parallel-0", "parallel-1"])
    checks.append("concurrent-writers-visible-to-existing-reader")

    _run_worker(db, "precommit-crash", "precommit", expected=43)
    _assert_missing(open_main_vault(str(db), KEY, device="cpu"), "precommit")
    checks.append("crash-before-canonical-commit")

    _run_worker(db, "postcommit-crash", "postcommit", expected=44)
    reopened = open_main_vault(str(db), KEY, device="cpu")
    assert get_events(reopened, ["postcommit"])[0]["text"] == SYNTHETIC[0]
    assert reopened.repair_status()["needed"] is False
    assert add_event(reopened, "note", SYNTHETIC[0], source="manual", timestamp=STAMP,
                     project="synthetic", doc_id="postcommit") == "postcommit"
    assert reopened.count() == 1
    checks.append("crash-after-canonical-commit-before-manifest-and-idempotent-retry")

    _run_worker(db, "delete-crash", "postcommit", expected=44)
    reopened = open_main_vault(str(db), KEY, device="cpu")
    _assert_missing(reopened, "postcommit")
    try:
        add_event(reopened, "note", SYNTHETIC[0], source="manual", doc_id="postcommit")
    except ValueError as exc:
        assert "deleted" in str(exc)
    else:
        raise AssertionError("deleted ID was accepted")
    checks.append("crash-after-delete-commit-no-resurrection")

    kept = add_event(reopened, "note", SYNTHETIC[1], source="manual", project="synthetic")
    sidecar = Path(vault_file_paths(str(db))[3])
    assert sidecar.exists()
    sidecar.unlink()
    reopened = open_main_vault(str(db), KEY, device="cpu")
    assert get_events(reopened, [kept])[0]["text"] == SYNTHETIC[1]
    checks.append("missing-derived-vector-file-rebuilt")

    _run_worker(db, "rebuild-crash", kept, expected=46)
    assert get_events(open_main_vault(str(db), KEY, device="cpu"), [kept])[0]["text"] == SYNTHETIC[1]
    for staging in directory.glob(".shadow-rebuild-*.sqlite"):
        for path in vault_file_paths(str(staging)):
            Path(path).unlink(missing_ok=True)
    checks.append("interrupted-rebuild-leaves-source-readable")

    try:
        open_main_vault(str(db), "wrong-synthetic-key", device="cpu")
    except PermissionError:
        pass
    else:
        raise AssertionError("wrong key opened existing vault")
    assert get_events(reopened, [kept])
    checks.append("wrong-key-leaves-source-readable")

    archive = directory / "portable.lsb"
    export_backup(reopened, archive, PASSPHRASE)
    _run_worker(db, "restore-crash", str(archive), expected=45)
    assert not (directory / "interrupted.sqlite").exists()
    assert get_events(reopened, [kept])[0]["text"] == SYNTHETIC[1]
    for staging in directory.glob(".shadow-restore-*.sqlite"):
        for path in vault_file_paths(str(staging)):
            Path(path).unlink(missing_ok=True)
    checks.append("interrupted-restore-leaves-source-readable")
    target = directory / "recovered.sqlite"
    try:
        restore_backup(archive, target, "wrong passphrase", "destination-key")
    except ValueError as exc:
        assert "authentication" in str(exc)
    else:
        raise AssertionError("wrong passphrase restored archive")
    assert not target.exists()
    damaged = directory / "damaged.lsb"
    bytes_ = bytearray(archive.read_bytes())
    bytes_[-1] ^= 1
    damaged.write_bytes(bytes_)
    try:
        restore_backup(damaged, target, PASSPHRASE, "destination-key")
    except ValueError as exc:
        assert "authentication" in str(exc)
    else:
        raise AssertionError("damaged archive restored")
    assert not target.exists()
    restore_backup(archive, target, PASSPHRASE, "destination-key")
    restored = open_main_vault(str(target), "destination-key", device="cpu")
    assert get_events(restored, [kept])[0]["text"] == SYNTHETIC[1]
    _assert_missing(restored, "postcommit")
    assert fetch_events(reopened)["events"][0]["id"] == kept
    checks.append("backup-authentication-and-fresh-destination-reopen")

    # Fail before SQLite commit: an injected ENOSPC at the store boundary should
    # leave the source readable. This is synthetic fault injection, not a full disk.
    before = reopened.count()
    original_insert = reopened._store.insert_batch
    def no_space(*_args, **_kwargs):
        raise OSError(28, "synthetic no space left on device")
    reopened._store.insert_batch = no_space
    try:
        add_event(reopened, "note", SYNTHETIC[2], source="manual", doc_id="disk-fault")
    except OSError as exc:
        assert exc.errno == 28
    else:
        raise AssertionError("injected disk fault reported success")
    finally:
        reopened._store.insert_batch = original_insert
    assert reopened.count() == before and get_events(reopened, [kept])
    _assert_missing(reopened, "disk-fault")
    checks.append("synthetic-enospc-before-commit")

    _mcp_journey(directory)
    checks.append("cli-grant-and-mcp-citation-forget-restart")

    return {"checks": checks, "count": len(checks), "status": "passed"}


def _samples(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {"p50_ms": 0.0, "p95_ms": 0.0}
    return {"p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 2)}


def _resources() -> dict:
    import psutil
    process = psutil.Process()
    return {"rss_mib": round(process.memory_info().rss / 1048576, 1),
            "fds": process.num_fds() if hasattr(process, "num_fds") else process.num_handles(),
            "threads": process.num_threads(), "children": len(process.children())}


def _size_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.iterdir() if path.is_file())


def performance(directory: Path, *, count: int, warmup: int, queries: int) -> dict:
    if os.environ.get("LATTICESHADOW_EMBEDDING_MODEL") == "hash":
        raise ValueError("performance evidence requires the pinned real model, not hash embeddings")
    _, _, _, _, _, _, search_events, open_main_vault, _ = _imports()
    from latticeshadow.vaults import EMBEDDING_MODEL, EMBEDDING_REVISION, embed_text
    from latticeshadow.timeline import _retrieval_cache, _utc
    from evaluation.scenarios import SCENARIOS
    db = directory / "performance.sqlite"
    t0 = time.perf_counter()
    embed_text("synthetic model initialization")
    model_seconds = time.perf_counter() - t0
    t0 = time.perf_counter()
    vault = open_main_vault(str(db), KEY, device="cpu")
    vault_open_seconds = time.perf_counter() - t0
    now = _utc(datetime.now(timezone.utc))
    authored = [case.record for case in SCENARIOS]
    prompts = [question for case in SCENARIOS if case.answerable for question in case.questions]
    t0 = time.perf_counter()
    for start in range(0, count, 64):
        stop = min(count, start + 64)
        documents = [f"{authored[i % len(authored)]} Synthetic event {i:05d}." for i in range(start, stop)]
        ids = [f"benchmark-{i:05d}" for i in range(start, stop)]
        metadata = [{"event_type": "note", "source": "manual", "project": "synthetic",
                     "timestamp": now, "captured_at": now, "timestamp_inferred": False}
                    for _ in ids]
        vault.add(documents=documents, ids=ids, metadatas=metadata)
    ingestion_seconds = time.perf_counter() - t0
    scopes = {"projects": ["synthetic"], "sources": ["manual"]}
    cold_start = time.perf_counter()
    assert search_events(vault, prompts[0], scope=scopes, limit=5)
    first_query_ms = (time.perf_counter() - cold_start) * 1000
    cache = _retrieval_cache(vault)
    candidate_ids = list(cache["scope_fields"])
    embedding_ms = []
    ranking_ms = []
    total_ms = []
    for i in range(warmup + queries):
        prompt = prompts[i % len(prompts)]
        t0 = time.perf_counter()
        embed_text(prompt)
        embedding = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        cache["index"].rank(prompt, candidate_ids, limit=5)
        ranking = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        assert search_events(vault, prompt, scope=scopes, limit=5)
        total = (time.perf_counter() - t0) * 1000
        if i >= warmup:
            embedding_ms.append(embedding)
            ranking_ms.append(ranking)
            total_ms.append(total)
    return {"events": count, "model": EMBEDDING_MODEL, "model_revision": EMBEDDING_REVISION,
            "authored_scenarios_repeated": len(authored),
            "cold_model_init_seconds": round(model_seconds, 2),
            "vault_open_seconds": round(vault_open_seconds, 2),
            "ingestion_seconds": round(ingestion_seconds, 2),
            "first_query_including_index_seconds": round(first_query_ms / 1000, 2),
            "warm_embedding": _samples(embedding_ms), "warm_ranker": _samples(ranking_ms),
            "warm_total_query": _samples(total_ms),
            "warmup_queries": warmup, "measured_queries": queries,
            "resources": _resources(), "vault_disk_mib": round(_size_bytes(directory) / 1048576, 1),
            "model_source": "bundled" if os.environ.get("LATTICESHADOW_BUNDLED_MODEL") else "hub_or_cache",
            "download_seconds": None,
            "note": "total query includes scope filtering, ephemeral ranking and canonical hydration; model initialization may include download when model_source is hub_or_cache, and download is not measured separately"}


def soak(directory: Path, *, seconds: int, interval: int, operation_interval: float,
         max_live: int, progress: Path) -> dict:
    _, _, add_event, _, forget_events, get_events, search_events, open_main_vault, _ = _imports()
    vault = open_main_vault(str(directory / "soak.sqlite"), KEY, device="cpu")
    rng = random.Random(0x1A771CE)
    live: list[str] = []
    deleted: deque[str] = deque(maxlen=256)
    samples: list[dict] = []
    started = time.monotonic()
    next_sample = started
    operations = {"capture": 0, "query": 0, "delete": 0, "restart": 0}
    with progress.open("w", encoding="utf-8") as log:
        while time.monotonic() - started < seconds:
            i = sum(operations.values())
            if len(live) >= max_live:
                victim = live.pop(0)
                result = forget_events(vault, [victim])
                assert result["canonical_deleted"] == 1 and not result["cleanup_errors"]
                deleted.append(victim)
                operations["delete"] += 1
            elif i % 41 == 0 and live:
                vault = open_main_vault(str(directory / "soak.sqlite"), KEY, device="cpu")
                operations["restart"] += 1
                assert get_events(vault, [live[-1]])
                if operations["restart"] % 10 == 0:
                    _run_worker(directory / "soak.sqlite", "read", live[-1])
            elif i % 11 == 0 and live:
                victim = live.pop(0)
                result = forget_events(vault, [victim])
                assert result["canonical_deleted"] == 1 and not result["cleanup_errors"]
                assert not get_events(vault, [victim])
                deleted.append(victim)
                operations["delete"] += 1
            elif i % 3 == 0 and live:
                search_events(vault, "deployment proxy", scope={"projects": ["synthetic"]})
                operations["query"] += 1
            else:
                text = SYNTHETIC[rng.randrange(len(SYNTHETIC))]
                live.append(add_event(vault, "note", text, source="manual", project="synthetic"))
                operations["capture"] += 1
            now = time.monotonic()
            if now >= next_sample:
                sample = {"elapsed_seconds": round(now - started, 1), "operations": dict(operations),
                          "live_events": len(live), "resources": _resources(),
                          "vault_disk_mib": round(_size_bytes(directory) / 1048576, 1)}
                samples.append(sample)
                log.write(json.dumps(sample, sort_keys=True) + "\n")
                log.flush()
                os.fsync(log.fileno())
                next_sample = now + interval
            time.sleep(operation_interval)
        vault = open_main_vault(str(directory / "soak.sqlite"), KEY, device="cpu")
        for start in range(0, len(live), 500):
            batch = live[start:start + 500]
            assert {row["id"] for row in get_events(vault, batch)} == set(batch)
        assert get_events(vault, deleted) == []
    maxima = {field: max(sample["resources"][field] for sample in samples)
              for field in ("fds", "threads", "children")}
    baseline = samples[0]["resources"]
    growth = {field: maxima[field] - baseline[field] for field in maxima}
    assert growth["fds"] <= 8 and growth["threads"] <= 8 and growth["children"] <= 2, growth
    return {"requested_seconds": seconds, "elapsed_seconds": round(time.monotonic() - started, 1),
            "operations": operations, "samples": len(samples), "first_resources": samples[0]["resources"],
            "last_resources": samples[-1]["resources"], "resource_growth_max": growth,
            "max_live_events": max_live, "operation_interval_seconds": operation_interval,
            "verified_deleted_sample": len(deleted),
            "progress_log": progress.name, "status": "passed"}


def _commit() -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _report(profile: str, detail: dict, elapsed: float, started_utc: str) -> dict:
    return {"profile": profile, "status": detail.get("status", "passed"),
            "commit": _commit(), "started_utc": started_utc,
            "elapsed_seconds": round(elapsed, 2), "system": platform.system(),
            "os_release": platform.release(), "machine": platform.machine(),
            "python": platform.python_version(), "cpu": platform.processor(), "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="profile", required=True)
    for profile in ("lifecycle", "performance", "soak-smoke", "soak-candidate"):
        command = commands.add_parser(profile)
        command.add_argument("--report", type=Path, help="write a sanitized JSON summary")
        command.add_argument("--data-dir", type=Path, help="empty disposable directory; retained after the run")
        if profile == "performance":
            command.add_argument("--events", type=int, default=10_000)
            command.add_argument("--warmup", type=int, default=10)
            command.add_argument("--queries", type=int, default=100)
        if profile.startswith("soak"):
            command.add_argument("--seconds", type=int, default=900 if profile == "soak-smoke" else 7200)
            command.add_argument("--sample-interval", type=int, default=30)
            command.add_argument("--operation-interval", type=float, default=0.5)
            command.add_argument("--max-live-events", type=int, default=256)
    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("db", type=Path)
    worker.add_argument("action")
    worker.add_argument("doc_id")
    args = parser.parse_args(argv)
    if args.profile == "_worker":
        _worker(args.db, args.action, args.doc_id)
        return 0
    if args.profile in ("lifecycle", "soak-smoke", "soak-candidate"):
        os.environ["LATTICESHADOW_EMBEDDING_MODEL"] = "hash"
    if args.profile == "performance" and (args.events < 1 or args.warmup < 0 or args.queries < 1):
        parser.error("performance requires positive events/queries and nonnegative warmup")
    if args.profile.startswith("soak") and (args.seconds < 1 or args.sample_interval < 1 or
                                             args.operation_interval <= 0 or not 1 <= args.max_live_events <= 1000):
        parser.error("soak duration, intervals and 1-1000 live events must be positive")
    temporary = None
    if args.data_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="latticeshadow-validation-")
        directory = Path(temporary.name)
    else:
        directory = args.data_dir
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(directory.iterdir()):
            parser.error("--data-dir must be empty")
    started_utc = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    try:
        if args.profile == "lifecycle":
            detail = lifecycle(directory)
        elif args.profile == "performance":
            detail = performance(directory, count=args.events, warmup=args.warmup, queries=args.queries)
        else:
            progress = directory / "soak-progress.jsonl"
            detail = soak(directory, seconds=args.seconds, interval=args.sample_interval,
                          operation_interval=args.operation_interval,
                          max_live=args.max_live_events, progress=progress)
        report = _report(args.profile, detail, time.perf_counter() - start, started_utc)
        serialized = json.dumps(report, indent=2, sort_keys=True)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

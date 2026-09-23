import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock


class FakePrivacy:
    def decrypt_document(self, text):
        return text.removeprefix("enc:")


class FakeStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)

    def _connect(self):
        return sqlite3.connect(self.db_path)


class FakeVault:
    name = "clipboard"

    def __init__(self, db_path):
        self._store = FakeStore(db_path)
        self._privacy = FakePrivacy()
        self.search_calls = []
        self.deleted = []
        self.added = []

    def _records(self, sql, params):
        with self._store._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        keys = ("rowid", "doc_id", "document", "metadata_json", "collection",
                "created_at", "last_accessed")
        return [dict(zip(keys, row)) for row in rows]

    def scan_records(self, after_row_id=0, limit=500):
        records = self._records(
            "SELECT rowid, doc_id, document, metadata_json, collection, created_at, last_accessed "
            "FROM vectors WHERE collection = ? AND rowid > ? ORDER BY rowid LIMIT ?",
            (self.name, after_row_id, limit + 1),
        )
        page = records[:limit]
        return page, page[-1]["rowid"] if len(records) > limit else None

    def get_records(self, ids):
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self._records(
            "SELECT rowid, doc_id, document, metadata_json, collection, created_at, last_accessed "
            f"FROM vectors WHERE collection = ? AND doc_id IN ({placeholders})",
            (self.name, *ids),
        )

    def search(self, query, n_results=10, hybrid=False, candidate_ids=None):
        self.search_calls.append((query, n_results, hybrid))
        return SimpleNamespace(
            ids=["clip_1"],
            documents=["API_KEY=sk-proj-example1234567890"],
            metadatas=[{"source": "clipboard"}],
            scores=[0.99],
        )

    def delete(self, ids):
        self.deleted.extend(ids)
        return len(ids)

    def add(self, documents, ids, metadatas):
        self.added.append((documents, ids, metadatas))
        return ids


def _make_timeline_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE vectors (
                doc_id TEXT,
                document TEXT,
                metadata_json TEXT,
                collection TEXT,
                created_at TEXT,
                last_accessed TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO vectors VALUES (?, ?, ?, ?, ?, ?)",
            (
                "clip_1",
                "enc:API_KEY=sk-proj-example1234567890",
                json.dumps({"source": "clipboard"}),
                "clipboard",
                "2026-07-01 10:00:00",
                "2026-07-01 10:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO vectors VALUES (?, ?, ?, ?, ?, ?)",
            (
                "cmd_2",
                "pytest -q",
                json.dumps({"source": "terminal"}),
                "clipboard",
                "2026-07-01 10:01:00",
                "2026-07-01 10:01:00",
            ),
        )


def test_timeline_fetch_and_search_normalizes_existing_rows(tmp_path):
    from latticeshadow.timeline import add_event, fetch_events, search_events

    db_path = tmp_path / "shadow.sqlite"
    _make_timeline_db(db_path)
    vault = FakeVault(db_path)

    terminal = fetch_events(vault, limit=5, scope={"sources": ["terminal"]})["events"]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "terminal"
    assert terminal[0]["text"] == "pytest -q"

    matches = search_events(vault, "api key", limit=3)
    assert vault.search_calls == [("api key", 3, True)]
    assert matches[0]["id"] == "clip_1"
    assert matches[0]["text"].startswith("API_KEY=")
    assert matches[0]["score"] == 0.99

    doc_id = add_event(vault, "url", "https://example.com", metadata={"url": "https://example.com"})
    assert doc_id.startswith("url_")
    assert vault.added[0][2][0]["event_type"] == "url"


def test_mcp_recall_redacts_sensitive_text(tmp_path):
    from latticeshadow.mcp_server import handle_request

    db_path = tmp_path / "shadow.sqlite"
    _make_timeline_db(db_path)
    vault = FakeVault(db_path)

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "latticeshadow.recall",
                "arguments": {"query": "api key", "limit": 3},
            },
        },
        lambda: vault,
        str(db_path),
        str(tmp_path),
    )

    text = response["result"]["content"][0]["text"]
    assert "[REDACTED]" in text
    assert "sk-proj" not in text


def test_mcp_lists_tools_without_opening_vault(tmp_path):
    from latticeshadow.mcp_server import handle_request

    called = False

    def vault_factory():
        nonlocal called
        called = True

    response = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        vault_factory,
        str(tmp_path / "shadow.sqlite"),
        str(tmp_path),
    )

    assert called is False
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "latticeshadow.recall" in names
    assert "latticeshadow.forget" in names


def test_mcp_forget_uses_supplied_forgetter(tmp_path):
    from latticeshadow.mcp_server import handle_request

    seen = []

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "latticeshadow.forget",
                "arguments": {"ids": ["clip_1"], "confirm": "FORGET"},
            },
        },
        lambda: None,
        str(tmp_path / "shadow.sqlite"),
        str(tmp_path),
        forgetter=lambda ids: seen.append(ids) or {"deleted": 1, "hot_deleted": 1},
    )

    assert seen == [["clip_1"]]
    text = response["result"]["content"][0]["text"]
    assert '"hot_deleted": 1' in text


def test_mcp_create_repair_proposal_tool(tmp_path):
    from latticeshadow.mcp_server import handle_request

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "latticeshadow.create_repair_proposal",
                "arguments": {"summary": "Run tests", "command": "pytest -q", "risk": "low"},
            },
        },
        lambda: None,
        str(tmp_path / "shadow.sqlite"),
        str(tmp_path),
    )

    text = response["result"]["content"][0]["text"]
    assert '"status": "pending"' in text
    assert (tmp_path / "repair_queue.jsonl").exists()


def test_summarizer_extracts_without_provider():
    from latticeshadow.summarizer import summarize_events

    payload = summarize_events(
        [
            {"id": "cmd_1", "type": "terminal", "text": "pytest -q"},
            {"id": "clip_1", "type": "clipboard", "text": "api_key=sk-proj-example1234567890"},
        ],
        prefer_foundation=False,
    )

    assert payload["provider"] == "extractive"
    assert "[SENSITIVE CONTENT OMITTED]" in payload["summary"] or "terminal=1" in payload["summary"]


def test_signed_audit_log_verifies_and_detects_tamper(tmp_path):
    from latticeshadow.audit_log import SignedAuditLog

    audit = SignedAuditLog(data_dir=str(tmp_path))
    audit.append("capture", {"doc_id": "clip_1", "content_hash": "abc"})
    assert audit.verify()[0] is True

    path = tmp_path / ".audit_log.jsonl"
    text = path.read_text(encoding="utf-8").replace('"abc"', '"tampered"')
    path.write_text(text, encoding="utf-8")
    assert SignedAuditLog(data_dir=str(tmp_path)).verify()[0] is False


def test_consent_set_updates_surface_config(tmp_path, monkeypatch):
    from latticeshadow import config as cfg
    from latticeshadow.consent import consent_status, set_consent

    monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))

    set_consent("ambient_context", True)
    status = consent_status()

    assert status["surfaces"]["ambient_context"]["consented"] is True
    assert cfg.get("inputs.ambient_context") is True


def test_repair_queue_status_lifecycle(tmp_path):
    from latticeshadow.repair_queue import create_repair_proposal, list_repairs, update_repair_status

    proposal = create_repair_proposal(
        "Inspect before mutate",
        source="test",
        command="pytest -q",
        risk="low",
        data_dir=str(tmp_path),
    )
    assert list_repairs(data_dir=str(tmp_path))[0]["id"] == proposal["id"]

    updated = update_repair_status(proposal["id"], "approved", data_dir=str(tmp_path))
    assert updated["status"] == "approved"


def test_native_intent_contract_maps_to_shadow_commands():
    from latticeshadow.native_bridge import intent_contract

    contract = intent_contract()
    commands = {intent["name"]: intent["command"][0] for intent in contract["intents"]}

    assert commands["RecallMemory"] == "timeline"
    assert commands["ForgetMemory"] == "forget"
    assert contract["foundation_bridge"]["environment"] == "LATTICESHADOW_FOUNDATION_MODELS_CMD"


def test_native_package_declares_foundation_bridge():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    package = os.path.join(root, "native", "Package.swift")
    bridge = os.path.join(root, "native", "LatticeShadowFoundationBridge", "main.swift")

    with open(package, "r", encoding="utf-8") as handle:
        package_text = handle.read()
    with open(bridge, "r", encoding="utf-8") as handle:
        bridge_text = handle.read()

    assert "latticeshadow-foundation-bridge" in package_text
    assert "LatticeShadowFoundationBridge" in package_text
    assert "LanguageModelSession" in bridge_text
    assert "extractiveSummary" in bridge_text


def test_device_trust_store_verifies_and_rejects_replay(tmp_path):
    from latticeshadow.trust import DeviceTrustStore

    local = DeviceTrustStore(data_dir=str(tmp_path / "local"))
    remote = DeviceTrustStore(data_dir=str(tmp_path / "remote"))
    local.trust_device("remote", remote.public_key_hex(), label="Remote")

    payload = remote.sign_payload({"type": "SYNC", "node_id": "remote", "content": "hello"})

    assert local.verify_payload(payload) == (True, "verified")
    assert local.verify_payload(payload)[0] is False


def test_privacy_report_flags_insecure_sidecar_mode(tmp_path, monkeypatch):
    from latticeshadow import config as cfg
    from latticeshadow.moonshot import generate_privacy_report

    monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))

    db_path = tmp_path / "shadow.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE vectors (doc_id TEXT, document TEXT, collection TEXT)")
        conn.execute("INSERT INTO vectors VALUES (?, ?, ?)", ("clip_1", "enc:safe", "clipboard"))

    sidecar = tmp_path / "shadow.sqlite_clipboard_vectors.bin"
    sidecar.write_text("sidecar", encoding="utf-8")
    os.chmod(sidecar, 0o644)

    report = generate_privacy_report(str(db_path), data_dir=str(tmp_path))

    assert report["status"] == "warn"
    assert report["device_trust"]["identity_exists"] is False
    assert any(str(sidecar) in issue for issue in report["issues"])


def test_moonshot_bench_smoke():
    from latticeshadow.moonshot import run_moonshot_bench

    report = run_moonshot_bench(count=4, dim=128, queries=2, master_key="test-moonshot")

    assert report["schema"] == "latticeshadow.moonshot_bench.v1"
    assert report["count"] == 4
    assert report["hot_streaming_exact"]["cold_open_count"] == 4
    assert "gates" in report["hot_streaming_exact"]
    assert "sidecar_bytes" in report
    assert "privacy_leakage" in report
    assert "ux_task_time_ms" in report
    assert report["storage_bytes"] > 0


def test_privacy_leakage_harness_smoke():
    from latticeshadow.moonshot import run_privacy_leakage_harness

    report = run_privacy_leakage_harness(count=8, dim=128, queries=2, master_key="test-privacy")

    assert report["schema"] == "latticeshadow.privacy_leakage_harness.v1"
    assert report["sidecar_plaintext_scan"]["status"] in ("ok", "warn")
    assert report["known_plaintext_procrustes"]["status"] in ("ok", "skipped")
    assert "private_recall_at_10" in report["knn_overlap"]


def test_cli_forget_deletes_hot_mirror_when_present(tmp_path, monkeypatch, capsys):
    from latticeshadow import shadow_cli

    main_vault = MagicMock()
    hot_vault = MagicMock()
    main_vault.delete.return_value = 1
    hot_vault.delete.return_value = 1

    monkeypatch.setattr(shadow_cli, "get_vault", lambda: main_vault)
    monkeypatch.setattr(shadow_cli, "hot_collection_exists", lambda db_path: True)
    monkeypatch.setattr(shadow_cli, "open_hot_vault", lambda **kwargs: hot_vault)
    monkeypatch.setattr(shadow_cli, "get_or_create_master_key", lambda: "master")
    monkeypatch.setattr(shadow_cli, "get_db_path", lambda: str(tmp_path / "shadow.sqlite"))
    monkeypatch.setattr(shadow_cli, "get_log_dir", lambda: str(tmp_path))
    monkeypatch.setattr(shadow_cli.config, "get_device", lambda: "cpu")
    def fake_forget(vault, ids):
        vault.delete(ids)
        return {"canonical_deleted": 1, "derived_invalidated": True,
                "cleanup_errors": []}
    monkeypatch.setattr("latticeshadow.timeline.forget_events", fake_forget)

    args = SimpleNamespace(id=["clip_1"], query=None, source=None, limit=10, yes=True)
    shadow_cli.do_forget(args)

    main_vault.delete.assert_called_once_with(["clip_1"])
    hot_vault.delete.assert_called_once_with(["clip_1"])
    assert "Hot index deleted 1" in capsys.readouterr().out

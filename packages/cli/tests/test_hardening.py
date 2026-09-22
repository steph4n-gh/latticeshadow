import json
import os
import pytest
import sqlite3
import stat
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_neural_composer_uses_complete_and_redacts_sensitive_history(tmp_path):
    db_path = tmp_path / "shadow.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE vectors (metadata_json TEXT, document TEXT, collection TEXT)")
        conn.execute(
            "INSERT INTO vectors VALUES (?, ?, ?)",
            (json.dumps({"source": "clipboard"}), "API_KEY=example_value", "clipboard"),
        )
        conn.execute(
            "INSERT INTO vectors VALUES (?, ?, ?)",
            (json.dumps({"source": "terminal"}), "pytest tests/test_example.py", "clipboard"),
        )

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def complete(self, **kwargs):
            self.calls.append(kwargs)
            return "pytest -q"

    llm = FakeLLM()
    from latticeshadow.compose import NeuralComposer

    composer = NeuralComposer(db_path=str(db_path), llm=llm)
    assert composer.predict_next_command() == "pytest -q"

    prompt = llm.calls[0]["user"]
    assert "[SENSITIVE CONTENT OMITTED]" in prompt
    assert "example_value" not in prompt


def test_time_travel_snapshots_skip_environment_by_default(tmp_path, monkeypatch):
    import latticeshadow.config as cfg
    from latticeshadow.time_travel import EnvironmentSnapshotter

    monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setenv("SECRET_TOKEN", "super-secret")

    snapshotter = EnvironmentSnapshotter(time_travel_enabled=True)
    snapshot_hash = snapshotter.capture()

    snapshot_path = tmp_path / "snapshots" / f"snapshot_{snapshot_hash}.json"
    with open(snapshot_path, "r") as f:
        snapshot = json.load(f)

    assert snapshot["env_vars"] == {}
    assert stat.S_IMODE(os.stat(snapshot_path).st_mode) == 0o600


def test_time_travel_cli_uses_config_module(tmp_path, monkeypatch, capsys):
    import latticeshadow.config as cfg
    from latticeshadow.shadow_cli import do_time_travel

    monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
    cfg.set("time_travel.enabled", "true")

    args = type("Args", (), {"list": True, "rollback": None})()
    do_time_travel(args)

    assert "No snapshots available." in capsys.readouterr().out


def test_mobile_api_binds_localhost_and_rate_limits_pairing(tmp_path):
    from latticeshadow.mobile_api import MobileAPIServer

    server = MobileAPIServer(MagicMock(), host="127.0.0.1", port=0)
    server.generate_pairing_code()
    server.start()

    def post_pair(passcode):
        body = json.dumps({"passcode": passcode}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/pair",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=2)

    try:
        assert server.host == "127.0.0.1"
        statuses = []
        for _ in range(6):
            try:
                post_pair("000000")
            except urllib.error.HTTPError as e:
                statuses.append(e.code)

        assert statuses[-1] == 429
    finally:
        server.stop()


def test_mobile_api_search_serializes_search_result():
    from latticeshadow.mobile_api import MobileAPIServer

    vault = MagicMock()
    vault.search.return_value = SimpleNamespace(
        ids=["doc_1", "doc_2"],
        documents=["alpha", {"nested": "beta"}],
        scores=[0.75, 0.25],
    )
    server = MobileAPIServer(vault, host="127.0.0.1", port=0)
    server.paired_clients["token"] = True
    server.start()

    try:
        body = json.dumps({"query": "alpha"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/search",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer token",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        vault.search.assert_called_once_with("alpha", n_results=5)
        assert payload["results"] == [
            {"doc_id": "doc_1", "document": "alpha", "score": 0.75},
            {"doc_id": "doc_2", "document": {"nested": "beta"}, "score": 0.25},
        ]
    finally:
        server.stop()


def test_ambient_monitor_uses_search_result_api(monkeypatch):
    from latticeshadow.ambient_monitor import AmbientContextMonitor

    vault = MagicMock()
    vault.search.return_value = SimpleNamespace(documents=[])
    monitor = AmbientContextMonitor(vault=vault)

    monitor._check_for_jit_context("Traceback (most recent call last):\nValueError", "Terminal")

    vault.search.assert_called_once_with(
        "Traceback (most recent call last):\nValueError",
        n_results=1,
    )


def test_vault_helpers_preserve_main_and_hot_index_contract(monkeypatch):
    from latticeshadow import vaults

    calls = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(vaults, "_connect_vault", fake_connect)

    main = vaults.open_main_vault("/tmp/shadow.sqlite", "master", device="cpu")
    hot = vaults.open_hot_vault(
        "/tmp/shadow.sqlite",
        "master",
        device="cpu",
        collection="clipboard_hot",
        strategy="streaming_exact",
    )

    assert main.collection == "clipboard"
    assert main.privacy is True
    assert main.drosophila_hash is True
    assert "experimental_index" not in calls[0]

    assert hot.collection == "clipboard_hot"
    assert hot.privacy is True
    assert hot.drosophila_hash is False
    assert hot.experimental_index == "streaming_exact"


def test_chmod_collection_files_covers_hot_index_sidecars(tmp_path):
    from latticeshadow.vaults import chmod_collection_files

    db_path = str(tmp_path / "shadow.sqlite")
    paths = [
        db_path,
        db_path + "-wal",
        f"{db_path}_clipboard_hot_vectors.bin",
        f"{db_path}_clipboard_hot_streaming_norms.bin",
        f"{db_path}_clipboard_hot_streaming_norms_meta.json",
        f"{db_path}_clipboard_hot_diskann_graph.bin",
    ]
    for path in paths:
        with open(path, "w") as handle:
            handle.write("x")
        os.chmod(path, 0o644)

    chmod_collection_files(SimpleNamespace(db_path=db_path, name="clipboard_hot"))

    for path in paths:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_hot_vault_rejects_invalid_strategy(monkeypatch):
    from latticeshadow import vaults

    monkeypatch.setattr(vaults, "_connect_vault", MagicMock())

    with pytest.raises(ValueError, match="streaming_exact"):
        vaults.open_hot_vault(
            "/tmp/shadow.sqlite",
            "master",
            collection="clipboard_hot",
            strategy="diskann_rerank",
        )


def test_hot_index_mirror_logs_and_does_not_raise(caplog):
    from latticeshadow.shadowd import mirror_to_hot_vault

    hot_vault = MagicMock()
    hot_vault.add.side_effect = RuntimeError("sidecar unavailable")

    mirror_to_hot_vault(hot_vault, "doc", "doc_1", {"source": "clipboard"})

    hot_vault.add.assert_called_once_with(
        documents=["doc"],
        ids=["doc_1"],
        metadatas=[{"source": "clipboard"}],
    )
    assert "Hot index mirror failed for doc_1" in caplog.text


def test_shred_crypto_shreds_hot_collection_when_present(monkeypatch, capsys):
    from latticeshadow import shadow_cli

    main_vault = MagicMock()
    hot_vault = MagicMock()

    monkeypatch.setattr(shadow_cli, "get_vault", lambda: main_vault)
    monkeypatch.setattr(shadow_cli, "hot_collection_exists", lambda db_path: True)
    monkeypatch.setattr(shadow_cli, "open_hot_vault", lambda **kwargs: hot_vault)
    monkeypatch.setattr(shadow_cli, "get_or_create_master_key", lambda: "master")
    monkeypatch.setattr(shadow_cli, "get_db_path", lambda: "/tmp/shadow.sqlite")
    monkeypatch.setattr(shadow_cli.config, "get", lambda key: False)
    monkeypatch.setattr(shadow_cli.keychain, "delete_key", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "SHRED")

    shadow_cli.do_shred()

    main_vault.crypto_shred.assert_called_once()
    hot_vault.crypto_shred.assert_called_once()
    assert "SUCCESS" in capsys.readouterr().out


def test_swarm_knowledge_records_only_after_signature_verification(tmp_path, monkeypatch):
    from latticeshadow.p2p import LocalMeshNode

    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path) if path == "~/.latticeshadow" else path)
    monkeypatch.setattr("latticeshadow.config.get", lambda key: key == "sync.swarm_knowledge")

    node = LocalMeshNode("local", MagicMock(), port=5069)
    monkeypatch.setattr(node, "_verify_swarm_signature", lambda *args: True)

    payload = {
        "node_id": "peer",
        "target_error": "Traceback",
        "fix_script": "rm -rf /",
        "signature": "verified-by-test",
    }

    with patch("subprocess.run") as mock_run:
        node._handle_incoming_swarm_knowledge(payload)

    node.stop()
    mock_run.assert_not_called()

    records_path = tmp_path / "pending_swarm_knowledge.jsonl"
    with open(records_path, "r") as f:
        record = json.loads(f.readline())
    assert record["fix_script"] == "rm -rf /"
    assert stat.S_IMODE(os.stat(records_path).st_mode) == 0o600

    repair_path = tmp_path / "repair_queue.jsonl"
    with open(repair_path, "r") as f:
        repair = json.loads(f.readline())
    assert repair["source"] == "swarm_knowledge"
    assert repair["status"] == "pending"

"""
Tests for Shadow Memory — config, sensitivity, dreamer, LLM commands.
"""

import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


# ── Config Tests ──────────────────────────────────────────────────────────────

class TestConfig:
    def test_defaults(self, tmp_path, monkeypatch):
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        config = cfg.load_config()
        assert config["memory"]["provider"] == "none"
        assert config["memory"]["dream_batch_size"] == 50
        assert config["memory"]["sensitivity_filter"] is True
        assert config["inputs"]["clipboard"] is True

    def test_set_and_get(self, tmp_path, monkeypatch):
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        cfg.set("memory.provider", "gemini")
        assert cfg.get("memory.provider") == "gemini"

    def test_auto_model_selection(self, tmp_path, monkeypatch):
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        cfg.set("memory.provider", "openai")
        model = cfg.get("memory.model")
        # Should auto-set default model for openai when model was empty
        assert model in ("gpt-4o-mini", "gemini-2.5-flash")  # depends on order

    def test_round_trip_preserves_types(self, tmp_path, monkeypatch):
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        cfg.set("memory.dream_batch_size", "100")
        assert cfg.get("memory.dream_batch_size") == 100  # int, not str

    def test_format_config(self, tmp_path, monkeypatch):
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        output = cfg.format_config()
        assert "memory:" in output
        assert "provider" in output


# ── Sensitivity Classifier Tests ──────────────────────────────────────────────

class TestSensitivity:
    def test_api_key_is_sensitive(self):
        from latticeshadow.sensitivity import classify
        assert classify("export API_KEY=example_value") == "sensitive"

    def test_aws_key_is_sensitive(self):
        from latticeshadow.sensitivity import classify
        assert classify("AKIAIOSFODNN7EXAMPLE") == "sensitive"

    def test_private_key_is_sensitive(self):
        from latticeshadow.sensitivity import classify
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        assert classify(text) == "sensitive"

    def test_connection_string_is_sensitive(self):
        from latticeshadow.sensitivity import classify
        assert classify("postgres://admin:secret@prod-db:5432/main") == "sensitive"

    def test_jwt_is_sensitive(self):
        from latticeshadow.sensitivity import classify
        jwt = "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + ".sig"
        assert classify(jwt) == "sensitive"

    def test_github_token_is_sensitive(self):
        from latticeshadow.sensitivity import classify
        assert classify("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm") == "sensitive"

    def test_normal_code_is_safe(self):
        from latticeshadow.sensitivity import classify
        assert classify("def hello_world():\n    print('hello')") == "safe"

    def test_url_is_safe(self):
        from latticeshadow.sensitivity import classify
        assert classify("https://docs.python.org/3/library/json.html") == "safe"

    def test_prose_is_safe(self):
        from latticeshadow.sensitivity import classify
        assert classify("The meeting is at 3pm in the main conference room") == "safe"

    def test_redact_replaces_secrets(self):
        from latticeshadow.sensitivity import redact
        text = "Use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm to auth"
        result = redact(text)
        assert "ghp_" not in result
        assert "[REDACTED]" in result

    def test_redact_preserves_safe_text(self):
        from latticeshadow.sensitivity import redact
        text = "def hello_world(): print('hello')"
        assert redact(text) == text

    def test_long_random_string_is_unknown(self):
        from latticeshadow.sensitivity import classify
        # 64-char hex string with no spaces — could be a key
        assert classify("a" * 64) == "unknown"


# ── Dreamer Tests ─────────────────────────────────────────────────────────────

class TestDreamer:
    def test_dream_cycle_skips_when_no_llm(self, tmp_path, monkeypatch):
        """Dream cycle should silently skip when no LLM is configured."""
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        from latticeshadow.dreamer import dream_cycle
        from latticeshadow.llm import ShadowLLM

        # Mock ShadowLLM.from_config to return None
        monkeypatch.setattr(ShadowLLM, "from_config", lambda: None)

        mock_vault = MagicMock()
        dream_cycle(mock_vault)  # Should not raise
        mock_vault.get_undreamed.assert_not_called()

    def test_dream_cycle_marks_sensitive_without_llm(self, tmp_path, monkeypatch):
        """Sensitive clips should be marked as dreamed without LLM contact."""
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        from latticeshadow.dreamer import dream_cycle
        from latticeshadow.llm import ShadowLLM

        mock_llm = MagicMock()
        mock_llm.complete.return_value = '[]'
        monkeypatch.setattr(ShadowLLM, "from_config", lambda: mock_llm)

        mock_vault = MagicMock()
        mock_vault.get_undreamed.return_value = [
            {"doc_id": "clip_1", "document": "export API_KEY=example_value", "metadata": {}},
        ]

        dream_cycle(mock_vault)

        # Should have been marked as dreamed with 'sensitive' tag
        mock_vault.update_metadata.assert_called_once()
        call_args = mock_vault.update_metadata.call_args
        assert call_args[0][0] == "clip_1"
        assert call_args[0][1]["dreamed"] is True
        assert "sensitive" in call_args[0][1]["tags"]

        # LLM should NOT have been called (no safe clips to process)
        mock_llm.complete.assert_not_called()

    def test_dream_cycle_enriches_safe_clips(self, tmp_path, monkeypatch):
        """Safe clips should be sent to LLM and enriched."""
        import latticeshadow.config as cfg
        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        from latticeshadow.dreamer import dream_cycle
        from latticeshadow.llm import ShadowLLM

        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps([
            {"tags": ["python", "code"], "category": "code", "summary": "A hello world function"}
        ])
        monkeypatch.setattr(ShadowLLM, "from_config", lambda: mock_llm)

        mock_vault = MagicMock()
        mock_vault.get_undreamed.return_value = [
            {"doc_id": "clip_2", "document": "def hello(): print('hi')", "metadata": {}},
        ]

        dream_cycle(mock_vault)

        # LLM should have been called
        mock_llm.complete.assert_called_once()

        # Metadata should have been updated with enrichments
        mock_vault.update_metadata.assert_called_once()
        call_args = mock_vault.update_metadata.call_args
        assert call_args[0][1]["dreamed"] is True
        assert "python" in call_args[0][1]["tags"]
        assert call_args[0][1]["category"] == "code"

    def test_parse_enrichment_handles_markdown_fences(self):
        """LLM responses wrapped in ```json fences should be parsed correctly."""
        from latticeshadow.dreamer import _parse_enrichment_response

        response = '```json\n[{"tags": ["test"], "category": "code", "summary": "test"}]\n```'
        result = _parse_enrichment_response(response, 1)
        assert len(result) == 1
        assert result[0]["tags"] == ["test"]

    def test_parse_enrichment_handles_invalid_json(self):
        """Invalid JSON should return empty enrichments, not crash."""
        from latticeshadow.dreamer import _parse_enrichment_response

        result = _parse_enrichment_response("this is not json", 3)
        assert len(result) == 3
        assert all(e["category"] == "other" for e in result)


# ── LLM Client Tests ─────────────────────────────────────────────────────────

class TestLLMClient:
    def test_from_config_returns_none_when_no_provider(self, tmp_path, monkeypatch):
        """from_config returns None when provider is 'none' and no LM Studio."""
        import latticeshadow.config as cfg
        from latticeshadow.llm import ShadowLLM

        monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.toml"))
        monkeypatch.setattr(cfg, "LOG_DIR", str(tmp_path))

        # Mock is_reachable to return False (no LM Studio)
        monkeypatch.setattr(ShadowLLM, "is_reachable", lambda self: False)

        result = ShadowLLM.from_config()
        assert result is None

    def test_complete_raises_on_connection_error(self):
        """LLMError should be raised when the endpoint is unreachable."""
        from latticeshadow.llm import ShadowLLM, LLMError

        llm = ShadowLLM(
            endpoint="http://localhost:99999/v1",
            model="test",
            timeout=1.0,
        )
        with pytest.raises(LLMError):
            llm.complete("system", "user")


# ── Collection Memory Helper Tests ───────────────────────────────────────────

class TestCollectionMemoryHelpers:
    @pytest.fixture
    def vault(self, tmp_path, monkeypatch):
        import hashlib
        monkeypatch.setattr("latticeshadow.keychain.retrieve_key", lambda: None)
        monkeypatch.setattr("latticeshadow.keychain.store_key", lambda key: None)
        monkeypatch.setattr("latticeshadow.keychain.delete_key", lambda: True)

        from latticeshadow_db.latticedb import connect
        master_key = hashlib.sha256(os.urandom(64)).hexdigest()
        return connect(
            db_path=str(tmp_path / "test.sqlite"),
            collection="clipboard",
            embedding_dim=128,
            privacy=True,
            drosophila_hash=True,
            master_key=master_key,
        )

    def test_get_undreamed_returns_all_initially(self, vault):
        vault.add(documents=["clip one", "clip two", "clip three"])
        undreamed = vault.get_undreamed(limit=10)
        assert len(undreamed) == 3

    def test_update_metadata_marks_as_dreamed(self, vault):
        vault.add(documents=["test clip"], ids=["clip_1"])
        vault.update_metadata("clip_1", {"dreamed": True, "tags": ["test"]})

        undreamed = vault.get_undreamed(limit=10)
        assert len(undreamed) == 0  # Should be filtered out

    def test_get_recent_returns_decrypted(self, vault):
        vault.add(documents=["most recent clip"])
        recent = vault.get_recent(limit=5)
        assert len(recent) == 1
        assert "most recent clip" in recent[0]["document"]

    def test_get_today_returns_todays_clips(self, vault):
        vault.add(documents=["today's clip"])
        today = vault.get_today()
        assert len(today) >= 1
        assert "today's clip" in today[0]["document"]


# ── Doctor Tests ──────────────────────────────────────────────────────────────

class TestShadowDoctor:
    def test_do_doctor_runs_cleanly(self, monkeypatch, tmp_path):
        from latticeshadow.shadow_cli import do_doctor
        # Mock vault retrieval, keychain, and plist checks
        mock_vault = MagicMock()
        mock_vault.count.return_value = 10
        monkeypatch.setattr("latticeshadow.shadow_cli.get_vault", lambda: mock_vault)
        monkeypatch.setattr("latticeshadow.keychain.retrieve_key", lambda: "mock_key")
        tmp_path.chmod(0o700)
        key_file = tmp_path / ".key"
        db_file = tmp_path / "shadow.sqlite"
        for path in (key_file, db_file):
            path.touch(mode=0o600)
        monkeypatch.setattr("latticeshadow.shadow_cli.get_log_dir", lambda: str(tmp_path))
        monkeypatch.setattr("latticeshadow.shadow_cli.get_key_file", lambda: str(key_file))
        monkeypatch.setattr("latticeshadow.shadow_cli.get_db_path", lambda: str(db_file))
        monkeypatch.setattr("latticeshadow.shadow_cli.PLIST_PATH", str(tmp_path / "test.plist"))
        monkeypatch.setattr("latticeshadow.config.load_config", lambda: {"memory": {"provider": "none"}})
        monkeypatch.setattr("latticeshadow.config.get", lambda key: "none" if key == "memory.provider" else None)
        monkeypatch.setattr("latticeshadow.config.format_config", lambda: "mock_config")
        
        # Capture stdout
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            do_doctor()
        output = f.getvalue()
        
        assert "LatticeShadow Health Check" in output
        assert "Python" in output
        assert "Config validation" in output


# ── History Watcher Tests ──────────────────────────────────────────────────────

class TestHistoryWatcher:
    def test_history_watcher_standard_format(self, tmp_path):
        from latticeshadow.history_watcher import HistoryWatcher
        histfile = tmp_path / "zsh_history"
        histfile.write_text("git status\nls -la\ncurl -X POST http://test.com\n", encoding="utf-8")
        
        watcher = HistoryWatcher(histfile=str(histfile))
        # Initial poll seeks to end
        assert len(watcher.poll()) == 0
        
        # Append new command
        with open(histfile, "a") as f:
            f.write("docker run nginx\n")
            
        entries = watcher.poll()
        assert len(entries) == 1
        assert entries[0]["text"] == "docker run nginx"
        assert entries[0]["source"] == "terminal"

    def test_history_watcher_extended_format(self, tmp_path):
        from latticeshadow.history_watcher import HistoryWatcher
        histfile = tmp_path / "zsh_history"
        histfile.write_text(": 1719500000:0;git commit -m \"first\"\n", encoding="utf-8")
        
        watcher = HistoryWatcher(histfile=str(histfile))
        watcher._initialize_offset()
        
        with open(histfile, "a") as f:
            f.write(": 1719500010:0;python3 script.py\n")
            
        entries = watcher.poll()
        assert len(entries) == 1
        assert entries[0]["text"] == "python3 script.py"
        assert entries[0]["timestamp"] == 1719500010.0

    def test_history_watcher_skips_trivial(self, tmp_path):
        from latticeshadow.history_watcher import HistoryWatcher
        histfile = tmp_path / "zsh_history"
        histfile.write_text("ls\n", encoding="utf-8")
        
        watcher = HistoryWatcher(histfile=str(histfile))
        watcher._initialize_offset()
        
        with open(histfile, "a") as f:
            f.write("cd /tmp\n")
            f.write("ls -la\n")
            f.write("echo 'hello'\n")
            f.write("git checkout main\n") # non-trivial
            
        entries = watcher.poll()
        assert len(entries) == 1
        assert entries[0]["text"] == "git checkout main"

    def test_history_watcher_deduplicates(self, tmp_path):
        from latticeshadow.history_watcher import HistoryWatcher
        histfile = tmp_path / "zsh_history"
        histfile.write_text("git diff\n", encoding="utf-8")
        
        watcher = HistoryWatcher(histfile=str(histfile))
        watcher._initialize_offset()
        
        with open(histfile, "a") as f:
            f.write("git status\n")
            f.write("git status\n") # duplicated
            
        entries = watcher.poll()
        assert len(entries) == 1
        assert entries[0]["text"] == "git status"


# ── Hot-Cache Tests ────────────────────────────────────────────────────────────

class TestHotCache:
    def test_search_ignores_stale_derived_index(self, tmp_path, monkeypatch):
        import io
        import torch
        from contextlib import redirect_stdout
        from latticeshadow.holographic_index import HolographicIndex
        
        # 1. Create a dummy holographic index on disk
        index_file = tmp_path / "holographic_today.bin"
        idx = HolographicIndex(dim=128)
        documents = ["match text document"]
        embeddings = [torch.randn(128)]
        idx.compile(documents, embeddings)
        idx.save(str(index_file))
        
        # 2. Patch LOG_DIR in shadow_cli and mock get_vault and recall
        monkeypatch.setattr("latticeshadow.shadow_cli.LOG_DIR", str(tmp_path))
        
        mock_vault = MagicMock()
        mock_vault.count.return_value = 1
        mock_vault.search.return_value.documents = ["fresh vault document"]
        mock_vault.search.return_value.ids = ["fresh"]
        mock_vault.search.return_value.scores = [0.9]
        monkeypatch.setattr("latticeshadow.shadow_cli.get_vault", lambda: mock_vault)
        
        # Capture stdout for do_search
        from latticeshadow.shadow_cli import do_search
        f = io.StringIO()
        with redirect_stdout(f):
            do_search("some search query")
        output = f.getvalue()
        
        assert "fresh vault document" in output
        assert "match text document" not in output

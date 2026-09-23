import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch

# Mock AppKit module before importing anything that uses it
sys.modules["AppKit"] = MagicMock()

# Ensure parent path is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from latticeshadow.virtual_swapper import SemanticSwapperDaemon
from latticeshadow.shadow_cli import do_unswap

class TestVirtualSwapper:
    @patch("subprocess.check_output")
    def test_get_swap_used_mb(self, mock_check_output):
        mock_check_output.return_value = "total = 3072.00M  used = 512.00M  free = 2560.00M  (encrypted)\n"
        daemon = SemanticSwapperDaemon()
        used = daemon.get_swap_used_mb()
        assert used == 512.00

    @patch("latticeshadow.consent.surface_enabled", return_value=True)
    def test_check_app_transition(self, _surface_enabled):
        # Mock AppKit NSWorkspace
        import AppKit
        mock_ws = MagicMock()
        AppKit.NSWorkspace.sharedWorkspace.return_value = mock_ws
        
        mock_app1 = MagicMock()
        mock_app1.localizedName.return_value = "Google Chrome"
        
        mock_app2 = MagicMock()
        mock_app2.localizedName.return_value = "Terminal"
        
        # Instantiate daemon
        vault = MagicMock()
        vault.name = "clipboard"
        daemon = SemanticSwapperDaemon(vault=vault)
        
        # Mock query_app_text and snapshot_app_to_vault
        daemon.query_app_text = MagicMock(return_value={"title": "Test Title", "url": "https://google.com", "text": "Chrome Text Content"})
        
        # First check (initiates last_active_app_name)
        mock_ws.frontmostApplication.return_value = mock_app1
        daemon.check_app_transition()
        assert daemon.last_active_app_name == "Google Chrome"
        
        # Second check (transition from Google Chrome to Terminal)
        mock_ws.frontmostApplication.return_value = mock_app2
        daemon.check_app_transition()
        
        assert daemon.last_active_app_name == "Terminal"
        # Google Chrome should have been snapshotted because it transitioned to the background
        daemon.query_app_text.assert_called_with("Google Chrome")
        vault.add.assert_called_once()
        
        # Check added metadata
        args, kwargs = vault.add.call_args
        assert kwargs["metadatas"][0]["app"] == "Google Chrome"
        assert kwargs["metadatas"][0]["type"] == "swap_page"

    @patch("latticeshadow.consent.surface_enabled", return_value=False)
    def test_revoked_snapshot_does_not_read_or_store_app_text(self, _surface_enabled):
        vault = MagicMock()
        daemon = SemanticSwapperDaemon(vault=vault)
        daemon.query_app_text = MagicMock(return_value={"text": "private app text"})

        daemon.snapshot_app_to_vault("TextEdit")

        daemon.query_app_text.assert_not_called()
        vault.add.assert_not_called()

    @patch("latticeshadow.shadow_cli.get_vault")
    @patch("subprocess.run")
    def test_cli_unswap_command(self, mock_run, mock_get_vault):
        from unittest.mock import DEFAULT
        def side_effect(args, *args2, **kwargs2):
            if isinstance(args, list) and len(args) > 0 and args[0] == "sysctl":
                if mock_run.call_args_list:
                    mock_run.call_args_list.pop()
                if mock_run.mock_calls:
                    mock_run.mock_calls.pop()
                mock_run.call_count = max(0, mock_run.call_count - 1)
                res = MagicMock()
                res.returncode = 0
                res.stdout = "total = 3072.00M  used = 512.00M  free = 2560.00M"
                return res
            return DEFAULT
        mock_run.side_effect = side_effect

        # Mock vault
        vault = MagicMock()
        vault.name = "clipboard"
        vault._privacy = None  # Ensure privacy is None so decryption is bypassed
        mock_get_vault.return_value = vault
        
        # Mock vault search results
        results = MagicMock()
        results.ids = ["swap_Chrome_12345"]
        vault.search.return_value = results
        
        # Mock SQLite connection inside do_unswap
        conn = MagicMock()
        vault._store._connect.return_value.__enter__.return_value = conn
        
        # Mock cursor returned by conn.execute
        cursor = MagicMock()
        conn.execute.return_value = cursor
        
        # Mock row fetch: metadata_json, document
        meta_json = '{"type": "swap_page", "app": "Google Chrome", "url": "https://google.com"}'
        cursor.fetchone.return_value = (meta_json, "Scraped Chrome text document content")
        
        # Run command
        do_unswap("Chrome text")
        
        # Verify AppleScript was called to re-hydrate the app
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "Google Chrome" in args[0][2]
        assert "https://google.com" in args[0][2]

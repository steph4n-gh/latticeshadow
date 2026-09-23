import os
import sys
import time
import stat
import pytest
import plistlib
import threading
import subprocess
import sqlite3
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

# Define MockPasteboard to isolate macOS NSPasteboard calls in sandbox
class MockPasteboard:
    def __init__(self):
        self._change_count = 0
        self._content = None
        self._types = []  # simulated pasteboard types (for concealed-type filtering)

    def changeCount(self):
        return self._change_count

    def stringForType_(self, pb_type):
        return self._content

    def availableTypeFromArray_(self, types_list):
        """Return the first matching type, or None if no match (mirrors NSPasteboard API)."""
        for t in types_list:
            if t in self._types:
                return t
        return None

    def set_content(self, content, types=None):
        self._content = content
        self._types = types or []
        self._change_count += 1

@contextmanager
def patch_general_pasteboard(mock_pb):
    """
    Safely patches AppKit.NSPasteboard on the AppKit module.
    Restores the original NSPasteboard class via assignment to avoid PyObjC selector assignment errors.
    """
    import AppKit
    orig_NSPasteboard = AppKit.NSPasteboard
    
    class MockNSPasteboard:
        @classmethod
        def generalPasteboard(cls):
            return mock_pb
            
    try:
        AppKit.NSPasteboard = MockNSPasteboard
        yield
    finally:
        AppKit.NSPasteboard = orig_NSPasteboard

@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    """
    Sets up a completely isolated, mocked home directory environment
    for latticeshadow testing, patching all file/shell/OS dependencies.
    """
    # Import AppKit here to ensure it's available or mocked
    try:
        import AppKit
    except ImportError:
        AppKit = MagicMock()
        sys.modules["AppKit"] = AppKit

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    
    # Mock expanduser to redirect to our temporary home directory
    orig_expanduser = os.path.expanduser
    def mock_expanduser(path):
        if path.startswith("~"):
            return path.replace("~", str(home_dir), 1)
        return orig_expanduser(path)
    monkeypatch.setattr(os.path, "expanduser", mock_expanduser)
    
    # Reload/Import shadow modules so they pick up the mocked paths
    if "latticeshadow.config" in sys.modules:
        import importlib
        importlib.reload(sys.modules["latticeshadow.config"])
    if "latticeshadow.shadow_cli" in sys.modules:
        import importlib
        importlib.reload(sys.modules["latticeshadow.shadow_cli"])
    if "latticeshadow.shadowd" in sys.modules:
        import importlib
        importlib.reload(sys.modules["latticeshadow.shadowd"])
        
    import latticeshadow.shadow_cli as shadow_cli
    import latticeshadow.shadowd as shadowd
    
    log_dir = home_dir / ".latticeshadow"
    plist_path = home_dir / "Library" / "LaunchAgents" / "com.latticedb.shadow.plist"
    zshrc_path = home_dir / ".zshrc"
    
    # Override global constants in both modules
    monkeypatch.setattr(shadow_cli, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(shadow_cli, "KEY_FILE", str(log_dir / ".key"))
    monkeypatch.setattr(shadow_cli, "DB_PATH", str(log_dir / "shadow.sqlite"))
    monkeypatch.setattr(shadow_cli, "PLIST_PATH", str(plist_path))
    
    monkeypatch.setattr(shadowd, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(shadowd, "KEY_FILE", str(log_dir / ".key"))
    monkeypatch.setattr(shadowd, "DB_PATH", str(log_dir / "shadow.sqlite"))
    
    # Mock the keychain module to avoid touching the real macOS Keychain
    monkeypatch.setattr("latticeshadow.keychain.retrieve_key", lambda: None)
    monkeypatch.setattr("latticeshadow.keychain.store_key", lambda key: None)
    monkeypatch.setattr("latticeshadow.keychain.delete_key", lambda: True)

    # Ensure daemon running state is reset
    shadowd._running = True
    
    return {
        "home": home_dir,
        "log_dir": log_dir,
        "key_file": log_dir / ".key",
        "db_path": log_dir / "shadow.sqlite",
        "plist_path": plist_path,
        "zshrc_path": zshrc_path,
        "shadow_cli": shadow_cli,
        "shadowd": shadowd
    }

@pytest.fixture
def mock_subprocess_run():
    from unittest.mock import DEFAULT
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
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
        yield mock_run

def run_cli(cli, args, mock_input=None):
    """Helper to run CLI commands with patched sys.argv and mocked input."""
    with patch("sys.argv", ["shadow"] + args), \
         patch("builtins.input", return_value=mock_input or ""):
        cli.main()


def choose_capture_sources(clipboard=True, terminal_history=False):
    from latticeshadow.consent import set_consent

    set_consent("clipboard", clipboard)
    set_consent("terminal_history", terminal_history)

# --- CLI Command Lifecycle Tests ---

def test_capture_requires_explicit_choices_before_enable(setup_test_env, monkeypatch):
    from latticeshadow import config, consent

    cli = setup_test_env["shadow_cli"]
    shadowd = setup_test_env["shadowd"]
    assert config.get("inputs.clipboard") is False
    assert config.get("inputs.terminal_history") is False
    assert consent.pending_capture_sources() == ["clipboard", "terminal_history"]

    monkeypatch.setattr(cli, "get_or_create_master_key", lambda: pytest.fail("enable passed consent gate"))
    with pytest.raises(SystemExit, match="Choose capture sources before starting"):
        cli.do_enable()
    shadowd.run_daemon()
    assert not setup_test_env["db_path"].exists()

    choose_capture_sources(clipboard=False, terminal_history=False)
    assert consent.pending_capture_sources() == []
    assert consent.capture_enabled("clipboard") is False
    assert consent.capture_enabled("terminal_history") is False


def test_legacy_capture_settings_require_confirmation_without_rewriting_them(setup_test_env):
    from latticeshadow import config, consent

    config.set("inputs.clipboard", "true")
    config.set("inputs.terminal_history", "false")
    assert consent.pending_capture_sources() == ["clipboard", "terminal_history"]
    assert consent.capture_enabled("clipboard") is False
    assert config.get("inputs.clipboard") is True

    consent.set_consent("clipboard", True)
    consent.set_consent("terminal_history", False)
    assert consent.pending_capture_sources() == []
    assert consent.capture_enabled("clipboard") is True

    # A direct edit to config.toml cannot silently expand capture after consent.
    config.set("inputs.terminal_history", "true")
    assert consent.pending_capture_sources() == ["terminal_history"]
    assert consent.capture_enabled("terminal_history") is False


def test_config_command_records_capture_choice(setup_test_env):
    from latticeshadow import consent

    cli = setup_test_env["shadow_cli"]
    run_cli(cli, ["config", "set", "inputs.clipboard", "true"])
    assert consent.capture_enabled("clipboard") is True
    with pytest.raises(SystemExit, match=r"Use on\|off"):
        run_cli(cli, ["config", "set", "inputs.terminal_history", "maybe"])
    assert consent.pending_capture_sources() == ["terminal_history"]


def test_search_distinguishes_empty_results_from_errors(setup_test_env, monkeypatch, capsys):
    cli = setup_test_env["shadow_cli"]
    vault = MagicMock()
    monkeypatch.setattr(cli, "get_vault", lambda: vault)

    vault.count.return_value = 0
    cli.do_search("missing")
    assert "No matching memories found." in capsys.readouterr().out

    vault.count.return_value = 1
    vault.search.side_effect = RuntimeError("test search failure")
    with pytest.raises(SystemExit, match="Search failed: test search failure"):
        cli.do_search("broken")

    monkeypatch.setattr(cli, "get_vault", lambda: (_ for _ in ()).throw(RuntimeError("test open failure")))
    with pytest.raises(SystemExit, match="Search failed while opening the vault: test open failure"):
        cli.do_search("broken")

def test_cli_remember_initializes_store_without_capture(setup_test_env):
    cli = setup_test_env["shadow_cli"]

    run_cli(cli, ["remember", "terminal", "Check release tests"])

    assert os.path.exists(setup_test_env["db_path"])
    assert stat.S_IMODE(os.stat(setup_test_env["log_dir"]).st_mode) == 0o700
    assert not os.path.exists(setup_test_env["plist_path"])
    assert not os.path.exists(setup_test_env["zshrc_path"])
    from latticeshadow.timeline import fetch_events

    events = fetch_events(cli.get_vault())
    assert len(events) == 1
    assert events[0]["text"] == "Check release tests"


def test_cli_install(setup_test_env, mock_subprocess_run, capsys):
    cli = setup_test_env["shadow_cli"]
    
    assert not os.path.exists(setup_test_env["key_file"])
    assert not os.path.exists(setup_test_env["plist_path"])
    assert not os.path.exists(setup_test_env["zshrc_path"])
    
    run_cli(cli, ["install"])
    
    out, err = capsys.readouterr()
    assert "Install complete" in out
    
    # Verify key file was created with 0o600 permissions
    assert os.path.exists(setup_test_env["key_file"])
    mode_key = stat.S_IMODE(os.stat(setup_test_env["key_file"]).st_mode)
    assert mode_key == 0o600
    
    # Verify log dir has 0o700 permissions
    assert os.path.exists(setup_test_env["log_dir"])
    mode_dir = stat.S_IMODE(os.stat(setup_test_env["log_dir"]).st_mode)
    assert mode_dir == 0o700
    
    # Verify plist was written correctly
    assert os.path.exists(setup_test_env["plist_path"])
    with open(setup_test_env["plist_path"], "rb") as f:
        plist_data = plistlib.load(f)
    assert plist_data["Label"] == cli.PLIST_LABEL
    assert plist_data["RunAtLoad"] is True
    assert plist_data["KeepAlive"] == {"SuccessfulExit": False}
    mock_subprocess_run.assert_any_call(
        ["launchctl", "disable", f"gui/{os.getuid()}/{cli.PLIST_LABEL}"],
        capture_output=True,
        text=True,
    )
    assert not (setup_test_env["log_dir"] / "latticeshadow.zsh").exists()
    assert not setup_test_env["zshrc_path"].exists()

def test_cli_install_idempotency(setup_test_env, mock_subprocess_run, capsys):
    cli = setup_test_env["shadow_cli"]
    
    # Run install first time
    run_cli(cli, ["install"])
    first_plist = setup_test_env["plist_path"].read_bytes()
        
    # Run install second time
    run_cli(cli, ["install"])
    assert first_plist == setup_test_env["plist_path"].read_bytes()
    assert not setup_test_env["zshrc_path"].exists()


def test_install_refreshes_integrity_baseline_after_upgrade(setup_test_env, mock_subprocess_run):
    from latticeshadow import integrity

    cli = setup_test_env["shadow_cli"]
    cli.do_install()
    assert integrity.verify_integrity() == (True, [])

    stale = integrity.read_manifest()
    stale["shadowd.py"] = "0" * 64
    integrity.write_manifest(stale)
    choose_capture_sources(clipboard=True)
    with pytest.raises(SystemExit, match="Daemon source changed"):
        cli.do_enable()

    cli.do_install()
    assert integrity.verify_integrity() == (True, [])


def test_cli_enable_disable(setup_test_env, mock_subprocess_run):
    cli = setup_test_env["shadow_cli"]
    choose_capture_sources(clipboard=True)
    
    # Create a dummy plist file to pass existence checks
    os.makedirs(os.path.dirname(setup_test_env["plist_path"]), exist_ok=True)
    with open(setup_test_env["plist_path"], "w") as f:
        f.write("dummy plist")
        
    # Test enable
    run_cli(cli, ["enable"])
    assert mock_subprocess_run.call_count >= 2
    calls = [c[0][0] for c in mock_subprocess_run.call_args_list]
    assert any("unload" in cmd for cmd in calls)
    assert any("load" in cmd for cmd in calls)
    assert any("enable" in cmd for cmd in calls)

    # Test disable
    mock_subprocess_run.reset_mock()
    run_cli(cli, ["disable"])
    assert mock_subprocess_run.call_count >= 1
    calls = [c[0][0] for c in mock_subprocess_run.call_args_list]
    assert any("unload" in cmd for cmd in calls)
    assert any("disable" in cmd for cmd in calls)


def test_cli_enable_reports_launch_failure(setup_test_env, mock_subprocess_run, capsys):
    cli = setup_test_env["shadow_cli"]
    choose_capture_sources(clipboard=True)
    os.makedirs(setup_test_env["plist_path"].parent, exist_ok=True)
    setup_test_env["plist_path"].write_text("invalid plist")
    mock_subprocess_run.return_value.returncode = 5
    mock_subprocess_run.return_value.stderr = "Invalid property list"
    with pytest.raises(SystemExit, match="Failed to start LatticeShadow"):
        cli.do_enable()
    assert "enabled and started" not in capsys.readouterr().out


def test_cli_enable_rolls_back_login_state_when_load_fails(setup_test_env, mock_subprocess_run):
    cli = setup_test_env["shadow_cli"]
    choose_capture_sources(clipboard=True)
    setup_test_env["plist_path"].parent.mkdir(parents=True, exist_ok=True)
    setup_test_env["plist_path"].write_text("invalid plist")

    def launch_result(args, **kwargs):
        result = MagicMock()
        result.returncode = 5 if args[1] == "load" else 0
        result.stderr = "Invalid property list" if result.returncode else ""
        return result

    mock_subprocess_run.side_effect = launch_result
    with pytest.raises(SystemExit, match="Failed to start LatticeShadow: Invalid property list"):
        cli.do_enable()
    calls = [call.args[0][1] for call in mock_subprocess_run.call_args_list]
    assert calls == ["unload", "enable", "load", "disable"]


def test_cli_install_refreshes_old_checkout(setup_test_env, mock_subprocess_run):
    cli = setup_test_env["shadow_cli"]
    setup_test_env["zshrc_path"].write_text(
        "# personal settings\n"
        f"alias shadow='/old/checkout/python old_cli.py'  {cli.ALIAS_MARKER}\n"
        "source /old/checkout/plugin.zsh  # LATTICESHADOW_ZSH\n"
    )
    cli.do_install()
    updated = setup_test_env["zshrc_path"].read_text()
    assert "/old/checkout" not in updated
    assert "# personal settings" in updated
    assert "LATTICESHADOW_ZSH" not in updated


def test_shell_integration_is_explicit_and_reversible(setup_test_env, mock_subprocess_run):
    cli = setup_test_env["shadow_cli"]
    zshrc = setup_test_env["zshrc_path"]
    original = "# personal settings\nbindkey '^I' expand-or-complete\n"
    zshrc.write_text(original)
    cli.do_install()
    assert zshrc.read_text() == original

    run_cli(cli, ["shell", "enable"])
    first = zshrc.read_text()
    assert first.count(cli.SHELL_MARKER) == 1
    assert (setup_test_env["log_dir"] / "latticeshadow.zsh").is_file()
    run_cli(cli, ["shell", "enable"])
    assert zshrc.read_text() == first
    cli.do_install()
    assert zshrc.read_text() == first
    run_cli(cli, ["shell", "disable"])
    assert zshrc.read_text() == original
    assert not (setup_test_env["log_dir"] / "latticeshadow.zsh").exists()


def test_rebuild_command_requires_stopped_daemon_and_migrates_disposable_store(setup_test_env, capsys):
    cli = setup_test_env["shadow_cli"]
    key = cli.get_or_create_master_key()
    legacy = cli.connect(
        db_path=setup_test_env["db_path"], collection="clipboard",
        embedding_dim=128, privacy=True, drosophila_hash=True, master_key=key,
    )
    legacy.add(documents=["recover this saved event"], ids=["clip_1"])
    with patch("subprocess.run") as launchctl:
        launchctl.return_value.returncode = 0
        with pytest.raises(SystemExit, match="Stop LatticeShadow first"):
            run_cli(cli, ["rebuild-index", "--yes"])
        launchctl.return_value.returncode = 1
        run_cli(cli, ["rebuild-index", "--yes"])
    assert "Rebuilt 1 stored vector" in capsys.readouterr().out
    assert cli.open_main_vault(str(setup_test_env["db_path"]), key).search(
        "recover this saved event", n_results=1
    ).ids == ["clip_1"]


def test_cli_remove_preserves_key_when_data_kept(setup_test_env, mock_subprocess_run):
    cli = setup_test_env["shadow_cli"]
    os.makedirs(setup_test_env["log_dir"], exist_ok=True)
    setup_test_env["key_file"].write_text("saved-key")
    setup_test_env["db_path"].write_text("saved-data")
    with patch.object(cli.keychain, "delete_key") as delete_key:
        run_cli(cli, ["remove"], mock_input="n")
    delete_key.assert_not_called()
    assert setup_test_env["key_file"].read_text() == "saved-key"
    assert setup_test_env["db_path"].read_text() == "saved-data"

def test_cli_status(setup_test_env, mock_subprocess_run, capsys):
    cli = setup_test_env["shadow_cli"]
    
    # Mock launchctl to return stopped status
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stderr = ""
    mock_res.stdout = "other.job.label\n"
    mock_subprocess_run.return_value = mock_res
    
    run_cli(cli, ["status"])
    out, err = capsys.readouterr()
    assert "STOPPED" in out
    assert "Database: Not initialized yet." in out
    
    # Mock launchctl to return stopped status with the service listed but PID as '-'
    mock_res.stdout = f"- 0 {cli.PLIST_LABEL}\n"
    mock_subprocess_run.return_value = mock_res
    run_cli(cli, ["status"])
    out, err = capsys.readouterr()
    assert "STOPPED" in out
    
    # Generate master key and save it so status gets the correct key
    master_key = cli.get_or_create_master_key()
    
    # Initialize database
    vault = cli.connect(
        db_path=setup_test_env["db_path"],
        collection="clipboard",
        embedding_dim=128,
        privacy=True,
        drosophila_hash=True,
        master_key=master_key
    )
    vault.add(documents=["status test entry"])
    
    # Mock launchctl to return running status
    mock_res.stdout = f"{cli.PLIST_LABEL}\n"
    mock_subprocess_run.return_value = mock_res
    
    run_cli(cli, ["status"])
    out, err = capsys.readouterr()
    assert "RUNNING" in out
    assert str(setup_test_env["db_path"]) in out
    assert "Entries:  1" in out


def test_capture_pause_persists_without_changing_source_choices(setup_test_env):
    from latticeshadow import config, consent

    cli = setup_test_env["shadow_cli"]
    choose_capture_sources(clipboard=True, terminal_history=False)
    assert consent.capture_enabled("clipboard") is True

    run_cli(cli, ["pause"])
    assert config.get("inputs.paused") is True
    assert config.get("inputs.clipboard") is True
    assert consent.capture_enabled("clipboard") is False
    assert consent.consent_status()["surfaces"]["clipboard"]["needs_consent"] is False

    # Simulate a new process reading the config after restart.
    import importlib
    importlib.reload(config)
    assert consent.consent_status()["paused"] is True
    assert consent.capture_enabled("clipboard") is False

    run_cli(cli, ["resume"])
    assert config.get("inputs.paused") is False
    assert consent.capture_enabled("clipboard") is True


def test_resume_requires_recorded_source_choices(setup_test_env):
    from latticeshadow import config, consent

    cli = setup_test_env["shadow_cli"]
    run_cli(cli, ["pause"])
    with pytest.raises(SystemExit, match="Choose capture sources"):
        run_cli(cli, ["resume"])
    assert config.get("inputs.paused") is True
    assert consent.capture_enabled("clipboard") is False


def test_interrupted_config_write_preserves_capture_choices(setup_test_env, monkeypatch):
    from latticeshadow import config, consent

    choose_capture_sources(clipboard=True, terminal_history=False)
    original = (setup_test_env["log_dir"] / "config.toml").read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated power loss before replacement")

    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated power loss"):
        consent.set_paused(True)

    assert (setup_test_env["log_dir"] / "config.toml").read_bytes() == original
    assert consent.capture_enabled("clipboard") is True
    assert not list(setup_test_env["log_dir"].glob(".config-*.tmp"))


def test_capture_status_uses_real_daemon_and_pause_state(setup_test_env, mock_subprocess_run):
    from latticeshadow import consent
    from latticeshadow.capture_state import get_status

    choose_capture_sources(clipboard=True, terminal_history=False)
    mock_subprocess_run.return_value.stdout = "321 0 com.latticedb.shadow\n"
    status = get_status()
    assert status["state"] == "capturing"
    assert status["sources"]["clipboard"]["capturing"] is True

    consent.set_paused(True)
    status = get_status()
    assert status["state"] == "paused"
    assert status["sources"]["clipboard"]["capturing"] is False

    mock_subprocess_run.return_value.returncode = 1
    mock_subprocess_run.return_value.stderr = "launchctl unavailable"
    status = get_status()
    assert status["daemon_running"] is None
    assert status["state"] == "unavailable"


def test_optional_services_require_recorded_consent(setup_test_env):
    from latticeshadow import config, consent

    for name in ("ambient_context", "mobile_api", "mesh_sync", "swarm_knowledge", "icloud_sync",
                 "immune_scan", "semantic_swapper", "auto_doctor"):
        key = consent.SURFACES[name]["config_key"]
        config.set(key, "true")
        assert consent.surface_enabled(name) is False
        consent.set_consent(name, True)
        assert consent.surface_enabled(name) is True

    consent.set_paused(True)
    assert consent.surface_enabled("ambient_context") is False
    assert consent.surface_enabled("semantic_swapper") is False
    assert consent.surface_enabled("immune_scan") is True


def test_capture_wizard_prompts_only_for_sources(setup_test_env):
    from latticeshadow import consent

    prompts = []
    answers = iter(("y", "n"))
    consent.run_wizard(
        input_fn=lambda prompt: (prompts.append(prompt), next(answers))[1],
        output_fn=lambda _message: None,
    )
    assert len(prompts) == 2
    assert consent.capture_enabled("clipboard") is True
    assert consent.capture_enabled("terminal_history") is False
    assert consent.surface_enabled("immune_scan") is False

def test_cli_search_paste(setup_test_env, capsys, mock_subprocess_run):
    cli = setup_test_env["shadow_cli"]
    
    master_key = cli.get_or_create_master_key()
    
    vault = cli.open_main_vault(
        db_path=setup_test_env["db_path"],
        master_key=master_key
    )
    vault.add(documents=["This is specific clipboard text"], ids=["clip_1719500000000"])
    
    # Test search CLI command
    run_cli(cli, ["search", "specific"])
    out, err = capsys.readouterr()
    assert "This is specific clipboard text" in out
    
    # Test paste CLI command (copies to pbcopy)
    run_cli(cli, ["paste", "specific"])
    mock_subprocess_run.assert_called_once()
    called_args = mock_subprocess_run.call_args[0][0]
    called_kwargs = mock_subprocess_run.call_args[1]
    assert called_args == ["pbcopy"]
    assert called_kwargs["input"] == b"This is specific clipboard text"

def test_cli_sleep(setup_test_env, capsys):
    cli = setup_test_env["shadow_cli"]
    
    master_key = cli.get_or_create_master_key()
    
    vault = cli.open_main_vault(
        db_path=setup_test_env["db_path"],
        master_key=master_key
    )
    vault.add(documents=["doc1", "doc2"])
    
    with patch("latticeshadow_db.latticedb.collection.Collection.consolidate") as mock_consolidate:
        run_cli(cli, ["sleep"])
        mock_consolidate.assert_called_once()

def test_cli_shred(setup_test_env, capsys):
    cli = setup_test_env["shadow_cli"]
    
    master_key = cli.get_or_create_master_key()
    
    vault = cli.open_main_vault(
        db_path=setup_test_env["db_path"],
        master_key=master_key
    )
    vault.add(documents=["confidential history"])
    
    # Shred with confirmation
    run_cli(cli, ["shred"], mock_input="SHRED")
    out, err = capsys.readouterr()
    assert "SUCCESS" in out
    
    # Connection should now fail
    with pytest.raises(SystemExit):
        cli.get_vault()

def test_cli_remove(setup_test_env, mock_subprocess_run, capsys):
    cli = setup_test_env["shadow_cli"]
    
    # Write dummy files to clean up
    os.makedirs(setup_test_env["log_dir"], exist_ok=True)
    with open(setup_test_env["key_file"], "w") as f:
        f.write("key")
    with open(setup_test_env["db_path"], "w") as f:
        f.write("db")
    os.makedirs(os.path.dirname(setup_test_env["plist_path"]), exist_ok=True)
    with open(setup_test_env["plist_path"], "w") as f:
        f.write("plist")
    with open(setup_test_env["zshrc_path"], "w") as f:
        f.write(f"some settings\nalias shadow='...'  {cli.ALIAS_MARKER}\n")
        
    # Remove with deleting data directories (input 'y')
    run_cli(cli, ["remove"], mock_input="y")
    
    assert not os.path.exists(setup_test_env["plist_path"])
    assert not os.path.exists(setup_test_env["log_dir"])
    with open(setup_test_env["zshrc_path"], "r") as f:
        zshrc_content = f.read()
    assert cli.ALIAS_MARKER not in zshrc_content

# --- Security and Permissions Tests ---

def test_security_permissions(setup_test_env, mock_subprocess_run):
    cli = setup_test_env["shadow_cli"]
    
    # Perform install to create dirs/files
    run_cli(cli, ["install"])
    
    # 1. Directory ~/.latticeshadow permissions must be chmod 700
    assert os.path.exists(setup_test_env["log_dir"])
    mode_dir = stat.S_IMODE(os.stat(setup_test_env["log_dir"]).st_mode)
    assert mode_dir == 0o700
    
    # 2. Key file ~/.latticeshadow/.key permissions must be chmod 600
    assert os.path.exists(setup_test_env["key_file"])
    mode_key = stat.S_IMODE(os.stat(setup_test_env["key_file"]).st_mode)
    assert mode_key == 0o600
    
    master_key = cli.get_or_create_master_key()
    
    # Connect and insert to initialize DB file
    vault = cli.connect(
        db_path=setup_test_env["db_path"],
        collection="clipboard",
        embedding_dim=128,
        privacy=True,
        drosophila_hash=True,
        master_key=master_key
    )
    
    # 3. Database file shadow.sqlite permissions must be chmod 600
    assert os.path.exists(setup_test_env["db_path"])
    mode_db = stat.S_IMODE(os.stat(setup_test_env["db_path"]).st_mode)
    assert mode_db == 0o600

def test_document_encryption_direct_db(setup_test_env):
    cli = setup_test_env["shadow_cli"]
    
    master_key = cli.get_or_create_master_key()
    
    vault = cli.connect(
        db_path=setup_test_env["db_path"],
        collection="clipboard",
        embedding_dim=128,
        privacy=True,
        drosophila_hash=True,
        master_key=master_key
    )
    
    secret_text = "HighlyConfidentialPassword123"
    vault.add(documents=[secret_text])
    
    # Query database directly using SQLite connection
    conn = sqlite3.connect(setup_test_env["db_path"])
    cursor = conn.cursor()
    cursor.execute("SELECT document FROM vectors")
    rows = cursor.fetchall()
    conn.close()
    
    assert len(rows) > 0
    for (doc_content,) in rows:
        # Plaintext must not be present in database
        assert secret_text not in doc_content
        # Must be encrypted and formatted with prefix
        assert doc_content.startswith("enc:")

# --- Edge Cases Tests ---

def test_edge_case_empty_db_no_traceback(setup_test_env, capsys):
    cli = setup_test_env["shadow_cli"]
    
    # Empty DB file
    with open(setup_test_env["db_path"], "w") as f:
        pass
        
    with patch("sys.argv", ["shadow", "search", "test"]):
        try:
            cli.main()
        except SystemExit:
            pass
            
    out, err = capsys.readouterr()
    assert "Traceback" not in out
    assert "Traceback" not in err
    
    with patch("sys.argv", ["shadow", "paste", "test"]):
        try:
            cli.main()
        except SystemExit:
            pass
            
    out, err = capsys.readouterr()
    assert "Traceback" not in out
    assert "Traceback" not in err

def test_edge_case_non_utf8_binary_graceful(setup_test_env, monkeypatch):
    shadowd = setup_test_env["shadowd"]
    choose_capture_sources(clipboard=True)
    monkeypatch.setattr(shadowd, "POLL_INTERVAL", 0.01)
    
    mock_pb = MockPasteboard()
    with patch_general_pasteboard(mock_pb):
        t = threading.Thread(target=shadowd.run_daemon)
        t.start()
        
        try:
            # Lone surrogate (invalid UTF-8 sequence)
            mock_pb.set_content("\ud800")
            time.sleep(0.2)
            
            # Non-text (None)
            mock_pb.set_content(None)
            time.sleep(0.2)
            
            assert t.is_alive()
        finally:
            shadowd._running = False
            t.join(timeout=1.0)

def test_edge_case_extremely_long_content(setup_test_env, monkeypatch):
    shadowd = setup_test_env["shadowd"]
    choose_capture_sources(clipboard=True)
    monkeypatch.setattr(shadowd, "POLL_INTERVAL", 0.01)
    
    mock_pb = MockPasteboard()
    with patch_general_pasteboard(mock_pb):
        t = threading.Thread(target=shadowd.run_daemon)
        t.start()
        
        try:
            # Exceeds max content bytes
            mock_pb.set_content("x" * (shadowd.MAX_CONTENT_BYTES + 1000))
            time.sleep(0.2)
            
            # Within max but very long
            mock_pb.set_content("y" * 100_000)
            time.sleep(0.2)
            
            assert t.is_alive()
        finally:
            shadowd._running = False
            t.join(timeout=1.0)

# --- Shred Daemon Behavior Tests ---

def test_shred_active_daemon_exit(setup_test_env, monkeypatch):
    shadowd = setup_test_env["shadowd"]
    cli = setup_test_env["shadow_cli"]
    choose_capture_sources(clipboard=True)
    
    monkeypatch.setattr(shadowd, "POLL_INTERVAL", 0.01)
    
    mock_pb = MockPasteboard()
    with patch_general_pasteboard(mock_pb):
        master_key = cli.get_or_create_master_key()
        
        vault = cli.open_main_vault(
            db_path=setup_test_env["db_path"],
            master_key=master_key
        )
        vault.add(documents=["test key"])
        
        t = threading.Thread(target=shadowd.run_daemon)
        t.start()
        
        try:
            time.sleep(0.2)
            assert t.is_alive()
            
            # Shred the vault
            vault.crypto_shred()
            
            # Also simulate key file removal
            if os.path.exists(setup_test_env["key_file"]):
                os.remove(setup_test_env["key_file"])
                
            # Daemon must detect key/db shredding and exit cleanly and immediately
            t.join(timeout=2.0)
            assert not t.is_alive()
        finally:
            shadowd._running = False
            t.join(timeout=1.0)

# --- Deduplication Tests ---

def test_deduplication(setup_test_env, monkeypatch):
    shadowd = setup_test_env["shadowd"]
    cli = setup_test_env["shadow_cli"]
    choose_capture_sources(clipboard=True)
    
    monkeypatch.setattr(shadowd, "POLL_INTERVAL", 0.01)
    
    mock_pb = MockPasteboard()
    with patch_general_pasteboard(mock_pb):
        t = threading.Thread(target=shadowd.run_daemon)
        t.start()
        
        try:
            mock_pb.set_content("check duplicate entry")
            time.sleep(0.2)
            
            mock_pb.set_content("check duplicate entry")
            time.sleep(0.2)
            
            mock_pb.set_content("different content")
            time.sleep(0.2)
            
            master_key = cli.get_or_create_master_key()
            
            vault = cli.open_main_vault(
                db_path=setup_test_env["db_path"],
                master_key=master_key
            )
            
            # Wait up to 3 seconds for the daemon thread to commit both records
            for _ in range(30):
                if vault.count() == 2:
                    break
                time.sleep(0.1)
                
            assert vault.count() == 2
        finally:
            shadowd._running = False
            t.join(timeout=1.0)


def test_clipboard_capture_respects_disabled_input(setup_test_env, monkeypatch):
    shadowd = setup_test_env["shadowd"]
    cli = setup_test_env["shadow_cli"]

    monkeypatch.setattr(shadowd, "POLL_INTERVAL", 0.01)
    choose_capture_sources(clipboard=False)

    mock_pb = MockPasteboard()
    with patch_general_pasteboard(mock_pb):
        t = threading.Thread(target=shadowd.run_daemon)
        t.start()

        try:
            mock_pb.set_content("do not capture this")
            time.sleep(0.3)

            master_key = cli.get_or_create_master_key()
            vault = cli.open_main_vault(
                db_path=setup_test_env["db_path"],
                master_key=master_key,
            )
            assert vault.count() == 0
        finally:
            shadowd._running = False
            t.join(timeout=1.0)


def test_clipboard_revocation_skips_changes_while_daemon_runs(setup_test_env, monkeypatch):
    from latticeshadow import config, consent

    shadowd = setup_test_env["shadowd"]
    cli = setup_test_env["shadow_cli"]
    monkeypatch.setattr(shadowd, "POLL_INTERVAL", 0.01)
    choose_capture_sources(clipboard=True)
    mock_pb = MockPasteboard()

    with patch_general_pasteboard(mock_pb):
        thread = threading.Thread(target=shadowd.run_daemon)
        thread.start()
        try:
            time.sleep(0.2)
            mock_pb.set_content("first captured clipboard entry")
            deadline = time.monotonic() + 4
            count = 0
            while time.monotonic() < deadline:
                if setup_test_env["db_path"].exists():
                    with sqlite3.connect(setup_test_env["db_path"]) as conn:
                        count = conn.execute(
                            "SELECT COUNT(*) FROM vectors WHERE collection = 'clipboard'"
                        ).fetchone()[0]
                    if count:
                        break
                time.sleep(0.05)
            assert count == 1

            # Even a raw config edit cannot authorize capture that consent disallows.
            config.set("inputs.clipboard", "false")
            assert consent.capture_enabled("clipboard") is False
            mock_pb.set_content("never captured clipboard entry")
            time.sleep(0.2)
            config.set("inputs.clipboard", "true")
            time.sleep(0.2)
            vault = cli.get_vault()
            assert vault.count() == 1
        finally:
            shadowd._running = False
            thread.join(timeout=2)


def test_clipboard_memory_survives_restart_and_forget_removes_both_indexes(setup_test_env, monkeypatch, capsys):
    """Use a disposable pasteboard and store for the complete memory path."""
    from latticeshadow.timeline import fetch_events, search_events
    from latticeshadow.vaults import open_hot_vault, open_main_vault

    cli = setup_test_env["shadow_cli"]
    shadowd = setup_test_env["shadowd"]
    choose_capture_sources(clipboard=True)
    shadowd.config.set("memory.hot_index_enabled", "true")
    monkeypatch.setattr(shadowd, "POLL_INTERVAL", 0.01)
    content = "Deploy the invoice service with rsync"
    mock_pb = MockPasteboard()

    with patch_general_pasteboard(mock_pb):
        shadowd._running = True
        thread = threading.Thread(target=shadowd.run_daemon)
        thread.start()
        try:
            time.sleep(0.2)
            assert thread.is_alive()
            mock_pb.set_content(content)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if setup_test_env["db_path"].exists():
                    with sqlite3.connect(setup_test_env["db_path"]) as conn:
                        count = conn.execute(
                            "SELECT COUNT(*) FROM vectors WHERE collection = 'clipboard'"
                        ).fetchone()[0]
                    if count:
                        break
                time.sleep(0.05)
            assert count == 1
        finally:
            shadowd._running = False
            thread.join(timeout=2)
    assert not thread.is_alive()

    key = cli.get_or_create_master_key()
    main = open_main_vault(str(setup_test_env["db_path"]), key)
    hot = open_hot_vault(str(setup_test_env["db_path"]), key)
    event = fetch_events(main, limit=1)[0]
    assert event["text"] == content
    assert search_events(main, content, limit=1)[0]["id"] == event["id"]
    assert hot.search(content, n_results=1).ids == [event["id"]]

    with patch("subprocess.run") as copy:
        run_cli(cli, ["paste", content])
    copy.assert_called_once()
    assert copy.call_args.kwargs["input"] == content.encode()
    derived = setup_test_env["log_dir"] / "holographic_today.bin"
    derived.write_bytes(b"derived-index")
    run_cli(cli, ["forget", "--id", event["id"], "--yes"])
    capsys.readouterr()
    assert not derived.exists()

    reopened_main = open_main_vault(str(setup_test_env["db_path"]), key)
    reopened_hot = open_hot_vault(str(setup_test_env["db_path"]), key)
    assert fetch_events(reopened_main, limit=10) == []
    assert reopened_main.count() == 0
    assert reopened_hot.count() == 0

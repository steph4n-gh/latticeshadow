import os
import sys
import time
import json
import struct
import socket
import pytest
import stat
from unittest.mock import patch, MagicMock

# Ensure parent path is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ── Mock Pasteboard for AppKit isolation ─────────────────────────────────────

class MockPasteboardItem:
    def __init__(self, types_dict):
        self._types_dict = types_dict

    def types(self):
        return list(self._types_dict.keys())

class MockPasteboard:
    def __init__(self):
        self.items = []
        self.clear_count = 0
        self.declared_types = []

    def pasteboardItems(self):
        return self.items

    def dataForType_(self, ptype):
        for item in self.items:
            if ptype in item._types_dict:
                return item._types_dict[ptype]
        return None

    def stringForType_(self, ptype):
        for item in self.items:
            if ptype in item._types_dict:
                val = item._types_dict[ptype]
                if isinstance(val, bytes):
                    return val.decode("utf-8")
                return str(val)
        return None

    def clearContents(self):
        self.items = []
        self.clear_count += 1
        self.declared_types = []

    def setString_forType_(self, string, ptype):
        self.items.append(MockPasteboardItem({ptype: string}))

    def declareTypes_owner_(self, types, owner):
        self.declared_types.extend(types)

    def setData_forType_(self, data, ptype):
        if not self.items:
            self.items.append(MockPasteboardItem({}))
        self.items[-1]._types_dict[ptype] = data

# ── Test Environment Fixture ──────────────────────────────────────────────────

@pytest.fixture
def setup_test_env(tmp_path, monkeypatch):
    try:
        import AppKit
    except ImportError:
        AppKit = MagicMock()
        sys.modules["AppKit"] = AppKit

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    
    orig_expanduser = os.path.expanduser
    def mock_expanduser(path):
        if path.startswith("~"):
            return path.replace("~", str(home_dir), 1)
        return orig_expanduser(path)
    monkeypatch.setattr(os.path, "expanduser", mock_expanduser)
    
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
    log_dir.mkdir(exist_ok=True)
    plist_path = home_dir / "Library" / "LaunchAgents" / "com.latticedb.shadow.plist"
    zshrc_path = home_dir / ".zshrc"
    
    monkeypatch.setattr(shadow_cli, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(shadow_cli, "KEY_FILE", str(log_dir / ".key"))
    monkeypatch.setattr(shadow_cli, "DB_PATH", str(log_dir / "shadow.sqlite"))
    monkeypatch.setattr(shadow_cli, "PLIST_PATH", str(plist_path))
    
    monkeypatch.setattr(shadowd, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(shadowd, "KEY_FILE", str(log_dir / ".key"))
    monkeypatch.setattr(shadowd, "DB_PATH", str(log_dir / "shadow.sqlite"))
    
    monkeypatch.setattr("latticeshadow.keychain.retrieve_key", lambda: None)
    monkeypatch.setattr("latticeshadow.keychain.store_key", lambda key: None)
    monkeypatch.setattr("latticeshadow.keychain.delete_key", lambda: True)

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

def run_cli(cli, args, mock_input=None):
    with patch("sys.argv", ["shadow"] + args), \
         patch("builtins.input", return_value=mock_input or ""):
        cli.main()

# ── Test Cases ────────────────────────────────────────────────────────────────

def test_p2p_mesh_sync(setup_test_env, monkeypatch):
    """
    Start a LocalMeshNode with a mock vault and register a mock sync callback.
    Send a simulated 'SYNC' TCP command payload.
    Verify that it processes the payload, writes to the local General Pasteboard,
    adds it to the vault, and does not broadcast a loop copy back.
    The socket timeout bounds the wait; functional behavior is the assertion.
    """
    from latticeshadow.p2p import LocalMeshNode
    from latticeshadow.trust import DeviceTrustStore
    import AppKit

    # Setup mock vault
    mock_vault = MagicMock()
    mock_vault.name = "clipboard"
    mock_vault._privacy.decrypt_document.side_effect = lambda x: x
    mock_vault._privacy.encrypt_document.side_effect = lambda x: x
    
    # Setup mock pasteboard
    mock_pb = MockPasteboard()
    class MockNSPasteboard:
        @classmethod
        def generalPasteboard(cls):
            return mock_pb
    monkeypatch.setattr(AppKit, "NSPasteboard", MockNSPasteboard)
    
    # Initialize LocalMeshNode on dynamic/safe port
    node = LocalMeshNode("test_mesh_node", mock_vault, port=5059)
    remote_store = DeviceTrustStore(data_dir=str(setup_test_env["log_dir"] / "remote"))
    node.trust_store.trust_device("remote_laptop", remote_store.public_key_hex(), label="Remote Laptop")
    
    # Register mock sync callback
    mock_callback = MagicMock()
    if hasattr(node, "register_sync_callback"):
        node.register_sync_callback(mock_callback)
    elif hasattr(node, "on_sync"):
        node.on_sync = mock_callback
    else:
        node.sync_callback = mock_callback

    # Mock broadcast to verify loop copy prevention
    if hasattr(node, "broadcast_sync"):
        monkeypatch.setattr(node, "broadcast_sync", MagicMock())
    if hasattr(node, "broadcast_search"):
        monkeypatch.setattr(node, "broadcast_search", MagicMock())

    # Start node
    node.start()
    
    try:
        # Connect and send SYNC payload
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(("127.0.0.1", node.tcp_port))
        
        payload = {
            "type": "SYNC",
            "node_id": "remote_laptop",
            "content": "sync_test_content"
        }
        payload = remote_store.sign_payload(payload)
        payload_bytes = json.dumps(payload).encode("utf-8")
        header = struct.pack("!I", len(payload_bytes))
        sock.sendall(header + payload_bytes)
        
        # Wait for potential TCP handler closure
        try:
            sock.recv(1024)
        except Exception:
            pass
        sock.close()
        
        # Verify callback triggered
        if hasattr(node, "register_sync_callback") or hasattr(node, "on_sync"):
            assert mock_callback.called, "Sync callback was not triggered"
            
        # Verify it writes to the local General Pasteboard
        assert mock_pb.stringForType_(AppKit.NSPasteboardTypeString) == "sync_test_content", \
            "Payload was not written to local pasteboard"
            
        # Verify it adds to the vault
        assert mock_vault.add.called or mock_vault.store.called, "Payload was not saved to vault"
        
        # Verify it does not broadcast a loop copy back
        if hasattr(node, "broadcast_sync"):
            assert not node.broadcast_sync.called, "Loop copy broadcast back was triggered"
        if hasattr(node, "broadcast_search"):
            assert not node.broadcast_search.called, "Loop copy broadcast back was triggered"
            
    finally:
        node.stop()

def test_ambient_context_ghost_paste(setup_test_env, capsys, monkeypatch):
    """
    Verify that 'shadow get-ghost-paste' correctly reads and outputs the serialized JSON or raw string
    from '~/.latticeshadow/.ambient_context'.
    Verify that 'shadow ghost-paste "query"' retrieves a vault match, temporarily writes it to the pasteboard,
    triggers the Cmd+V keystroke AppleScript command via subprocess, and restores the original pasteboard
    (verifying backup and restore of rich pasteboard types).
    """
    cli = setup_test_env["shadow_cli"]
    log_dir = setup_test_env["log_dir"]
    ambient_context_path = os.path.join(log_dir, ".ambient_context")
    
    # ── Part 1: shadow get-ghost-paste ──
    expected_context = '{"app": "Terminal", "buffer": "git diff"}'
    with open(ambient_context_path, "w") as f:
        f.write(expected_context)
    os.chmod(ambient_context_path, 0o600)
    
    # Run CLI command
    try:
        run_cli(cli, ["get-ghost-paste"])
        out, err = capsys.readouterr()
        assert expected_context in out, "get-ghost-paste output did not match ambient context content"
    except SystemExit as e:
        # CLI command may fail/exit because it is unimplemented. That's expected for now.
        if e.code != 0:
            raise AssertionError("get-ghost-paste exited with error status") from e
            
    # ── Part 2: shadow ghost-paste "query" ──
    # Mock vault search
    mock_vault = MagicMock()
    mock_vault.search.return_value = MagicMock(documents=["target_ghost_content"])
    monkeypatch.setattr(cli, "get_vault", lambda: mock_vault)
    
    # Populate pasteboard with rich data items
    import AppKit
    mock_pb = MockPasteboard()
    rich_item_data = {
        AppKit.NSPasteboardTypeString: b"original_string_data",
        AppKit.NSPasteboardTypePNG: b"fake_png_binary_data"
    }
    mock_pb.items.append(MockPasteboardItem(rich_item_data))
    
    class MockNSPasteboard:
        @classmethod
        def generalPasteboard(cls):
            return mock_pb
    monkeypatch.setattr(AppKit, "NSPasteboard", MockNSPasteboard)
    
    # Mock subprocess to capture AppleScript keystroke triggers
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0)
        
        try:
            run_cli(cli, ["ghost-paste", "query"])
        except SystemExit as e:
            if e.code != 0:
                raise AssertionError("ghost-paste exited with error status") from e
                
        # Verify keystroke AppleScript was executed
        assert mock_sub.called
        args_list = [call[0][0] for call in mock_sub.call_args_list]
        applescript_triggered = False
        for args in args_list:
            if "osascript" in args and any("keystroke" in arg and "v" in arg for arg in args):
                applescript_triggered = True
        assert applescript_triggered, "AppleScript Cmd+V keystroke was not simulated"
        
        # Verify original rich pasteboard types were restored
        restored_string = mock_pb.stringForType_(AppKit.NSPasteboardTypeString)
        restored_png = mock_pb.dataForType_(AppKit.NSPasteboardTypePNG)
        assert restored_string == "original_string_data"
        assert restored_png == b"fake_png_binary_data"

def test_shell_loop_prefilling(setup_test_env, capsys, monkeypatch):
    """
    Verify that 'shadow get-loop-fix' correctly reads and outputs the active fix command
    from '~/.latticeshadow/.speculative_fix'.
    Simulate a command loop in terminal history polling inside the daemon, verify it triggers
    the TopologicalLoopDetector, generates a speculative fix command, and writes it to
    the '.speculative_fix' file.
    """
    cli = setup_test_env["shadow_cli"]
    shadowd = setup_test_env["shadowd"]
    log_dir = setup_test_env["log_dir"]
    speculative_fix_path = os.path.join(log_dir, ".speculative_fix")
    
    # ── Part 1: shadow get-loop-fix ──
    expected_fix = "pip install --upgrade pip"
    with open(speculative_fix_path, "w") as f:
        f.write(expected_fix)
    os.chmod(speculative_fix_path, 0o600)
    
    try:
        run_cli(cli, ["get-loop-fix"])
        out, err = capsys.readouterr()
        assert expected_fix in out, "get-loop-fix output did not match speculative fix content"
    except SystemExit as e:
        if e.code != 0:
            raise AssertionError("get-loop-fix exited with error status") from e
            
    # ── Part 2: Daemon Loop Detection and Speculative Fix Prefilling ──
    # Mock history watcher to return a loop sequence
    mock_history_watcher = MagicMock()
    mock_history_watcher.poll.side_effect = [
        [{"text": "pytest tests/test_ghost_paste.py", "timestamp": time.time()}],
        [{"text": "pytest tests/test_ghost_paste.py", "timestamp": time.time()}],
        [{"text": "pytest tests/test_ghost_paste.py", "timestamp": time.time()}]
    ]
    
    # Mock topological loop detector to trigger
    from latticeshadow.tda import TopologicalLoopDetector
    monkeypatch.setattr(TopologicalLoopDetector, "detect_loop", lambda self, vault: (True, ["mock_error_doc"]))
    
    # Mock the AppKit pasteboard
    import AppKit
    mock_pb = MockPasteboard()
    class MockNSPasteboard:
        @classmethod
        def generalPasteboard(cls):
            return mock_pb
    monkeypatch.setattr(AppKit, "NSPasteboard", MockNSPasteboard)
    
    # Set the clipboard to a traceback/error message
    error_msg = "Traceback (most recent call last):\nFile \"test_ghost_paste.py\", line 10\nAssertionError: pytest tests/test_ghost_paste.py failed"
    mock_pb.setString_forType_(error_msg, AppKit.NSPasteboardTypeString)
    
    # Initialize a real vault in the test environment
    real_vault = shadowd.get_vault()
    
    # Populate the real vault database with:
    # 1. The past error event matching the clipboard
    real_vault.add(
        documents=[error_msg],
        ids=["past_error_1"],
        metadatas=[{"source": "clipboard"}]
    )
    
    # 2. The subsequent command that resolves/follows the error
    real_vault.add(
        documents=["pytest tests/test_ghost_paste.py"],
        ids=["past_cmd_1"],
        metadatas=[{"source": "terminal"}]
    )
    
    # 3. Align the timestamps explicitly in SQLite:
    # Set the past error time to 10 seconds ago, and command to now (ensuring it is within 5 minutes)
    with real_vault._store._connect() as conn:
        conn.execute("UPDATE vectors SET created_at = datetime('now', '-10 seconds') WHERE doc_id = 'past_error_1'")
        conn.execute("UPDATE vectors SET created_at = datetime('now') WHERE doc_id = 'past_cmd_1'")
        conn.commit()
        
    # Clear speculative fix file if it exists
    if os.path.exists(speculative_fix_path):
        os.remove(speculative_fix_path)
        
    iteration = 0
    def mock_sleep(secs):
        nonlocal iteration
        iteration += 1
        if iteration >= 1:
            raise KeyboardInterrupt("Stop daemon")
            
    monkeypatch.setattr(time, "sleep", mock_sleep)
    
    # Patch get_vault in shadowd to return our pre-populated real vault
    monkeypatch.setattr(shadowd, "get_vault", lambda: real_vault)
    monkeypatch.setattr(shadowd, "MobileAPIServer", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(shadowd, "LocalMeshNode", lambda *args, **kwargs: MagicMock())
    
    # Mock history watcher instantiation
    class MockWatcher:
        def __init__(self, *args, **kwargs):
            pass
        def poll(self):
            return [{"text": "pytest tests/test_ghost_paste.py", "timestamp": time.time()}]
            
    monkeypatch.setattr("latticeshadow.history_watcher.TerminalHistoryWatcher", MockWatcher)
    
    try:
        shadowd.run_daemon()
    except KeyboardInterrupt:
        pass
        
    # Verify that the file .speculative_fix was written with the genuine query result
    assert os.path.exists(speculative_fix_path), "Speculative fix file was not created by the daemon"
    with open(speculative_fix_path, "r") as f:
        written_fix = f.read().strip()
    assert written_fix == "pytest tests/test_ghost_paste.py", f"Expected 'pytest tests/test_ghost_paste.py', got '{written_fix}'"

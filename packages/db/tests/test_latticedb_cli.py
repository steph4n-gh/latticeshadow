"""
Unit tests for the LatticeDB CLI subcommands in cli.py.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock
from latticeshadow_db.cli import main
from latticeshadow_db.cli import _secret


def test_cli_db_add_single(tmp_path):
    """Test db-add CLI command with a single document."""
    db_file = str(tmp_path / "cli_test.sqlite")
    
    with patch("sys.argv", [
        "zk-bridge", "db-add",
        "--db", db_file,
        "--collection", "cli_coll",
        "--doc", "This is a CLI test document.",
        "--metadata", '{"author": "pytest"}'
    ]):
        main()

    # Verify document was inserted
    import latticeshadow_db.latticedb as latticedb
    db = latticedb.connect(db_path=db_file, collection="cli_coll")
    assert db.count() == 1
    
    # Test db-search CLI command
    with patch("sys.argv", [
        "zk-bridge", "db-search",
        "--db", db_file,
        "--collection", "cli_coll",
        "--query", "test",
        "-n", "1"
    ]), patch("sys.stdout.write") as mock_write:
        main()
        # Verify search was triggered
        assert mock_write.called


def test_cli_db_add_file(tmp_path):
    """Test db-add CLI command loading docs from a text file."""
    db_file = str(tmp_path / "cli_test_file.sqlite")
    text_file = str(tmp_path / "docs.txt")
    
    with open(text_file, "w") as f:
        f.write("Line one\nLine two\nLine three\n")
        
    with patch("sys.argv", [
        "zk-bridge", "db-add",
        "--db", db_file,
        "--collection", "file_coll",
        "--file", text_file
    ]):
        main()
        
    import latticeshadow_db.latticedb as latticedb
    db = latticedb.connect(db_path=db_file, collection="file_coll")
    assert db.count() == 3


def test_cli_db_rotate_and_shred(tmp_path, monkeypatch):
    """Test CLI commands for key rotation and crypto shredding."""
    db_file = str(tmp_path / "cli_secure.sqlite")
    
    # 1. Add document with privacy
    monkeypatch.setenv("LATTICEDB_MASTER_KEY", "my-secret-password")
    with patch("sys.argv", [
        "zk-bridge", "db-add",
        "--db", db_file,
        "--collection", "secure_coll",
        "--doc", "Secret data",
        "--privacy",
    ]):
        main()

    # 2. Rotate master key
    with patch("sys.argv", [
        "zk-bridge", "db-rotate",
        "--db", db_file,
        "--collection", "secure_coll",
    ]):
        monkeypatch.setenv("LATTICEDB_NEW_MASTER_KEY", "my-new-password")
        main()

    # Verify we can connect with new key
    import latticeshadow_db.latticedb as latticedb
    db = latticedb.connect(db_path=db_file, collection="secure_coll", privacy=True, master_key="my-new-password")
    assert db.count() == 1

    # 3. Crypto shred
    monkeypatch.setenv("LATTICEDB_MASTER_KEY", "my-new-password")
    with patch("sys.argv", [
        "zk-bridge", "db-shred",
        "--db", db_file,
        "--collection", "secure_coll",
    ]):
        main()
        
    # Verify collection can no longer be decrypted/accessed with key
    # (should raise or not have privacy enabled)
    # The collection instance will be uninitialized or fail unlock
    with pytest.raises(PermissionError):
        latticedb.connect(db_path=db_file, collection="secure_coll", privacy=True, master_key="my-new-password")


def test_cli_requires_new_key_environment_without_terminal(monkeypatch):
    monkeypatch.delenv("LATTICEDB_NEW_MASTER_KEY", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="LATTICEDB_NEW_MASTER_KEY"):
        _secret("LATTICEDB_NEW_MASTER_KEY", "New master key: ")


def test_cli_rotation_can_use_existing_keyring_key(monkeypatch, tmp_path):
    monkeypatch.delenv("LATTICEDB_MASTER_KEY", raising=False)
    monkeypatch.setenv("LATTICEDB_NEW_MASTER_KEY", "replacement-key")
    fake_db = MagicMock()
    with patch("latticeshadow_db.latticedb.connect", return_value=fake_db) as connect_mock:
        with patch("sys.argv", [
            "zk-bridge", "db-rotate", "--db", str(tmp_path / "vault.sqlite"),
            "--collection", "secure_coll",
        ]):
            main()

    assert connect_mock.call_args.kwargs["master_key"] is None
    fake_db.rotate_master_key.assert_called_once_with("replacement-key")

import os
import json
import time
import pytest
from unittest.mock import MagicMock, patch
import hashlib
from base64 import b64encode, b64decode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from latticeshadow.sync import ICloudSyncEngine


@pytest.fixture
def mock_vault(tmp_path):
    vault = MagicMock()
    vault.name = "clipboard"
    
    # Mock privacy engine with dummy master key
    privacy = MagicMock()
    privacy._master_key_bytes = b"dummy_master_key_bytes_32_bytes_len"
    
    # Mock encryption/decryption as pass-through for test simplicity
    privacy.encrypt_document.side_effect = lambda x: f"enc_doc:{x}"
    privacy.decrypt_document.side_effect = lambda x: x.replace("enc_doc:", "") if isinstance(x, str) else x
    vault._privacy = privacy
    
    # Mock store _connect and collection_id
    store = MagicMock()
    store.collection_id = "coll_123"
    
    # Simple list database backend in memory for mock
    db_data = [
        ("clip_1", "enc_doc:first document content", '{"source": "clipboard"}'),
        ("clip_2", "enc_doc:second document content", '{"source": "clipboard"}'),
    ]
    
    # Mock connection execute results
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = db_data
    conn.cursor.return_value = cursor
    store._connect.return_value.__enter__.return_value = conn
    
    vault._store = store
    return vault


def test_icloud_sync_export(mock_vault, tmp_path):
    # Set up engine with mock vault and redirected sync paths
    engine = ICloudSyncEngine(mock_vault, device_id="test_macbook")
    engine.log_dir = str(tmp_path / "log")
    os.makedirs(engine.log_dir, exist_ok=True) # Ensure directory exists
    engine.last_sync_file = os.path.join(engine.log_dir, ".last_sync_id")
    engine.processed_file = os.path.join(engine.log_dir, ".processed_packets")
    
    # Redirect sync packets dir to tmp_path
    sync_dir = str(tmp_path / "icloud" / "sync_packets")
    os.makedirs(sync_dir, exist_ok=True)
    engine.get_sync_dir = lambda: sync_dir
    
    # 1. Export packets
    engine.export_packets()
    
    # Verify files created in sync directory
    files = os.listdir(sync_dir)
    assert len(files) == 1
    assert files[0].endswith(".enc")
    assert files[0].startswith("test_macbook_")
    
    # Verify last sync id saved
    assert os.path.exists(engine.last_sync_file)
    with open(engine.last_sync_file, "r") as f:
        assert f.read().strip() == "clip_2"


def test_icloud_sync_import(mock_vault, tmp_path):
    engine = ICloudSyncEngine(mock_vault, device_id="test_imac")
    engine.log_dir = str(tmp_path / "log")
    os.makedirs(engine.log_dir, exist_ok=True) # Ensure directory exists
    engine.last_sync_file = os.path.join(engine.log_dir, ".last_sync_id")
    engine.processed_file = os.path.join(engine.log_dir, ".processed_packets")
    
    sync_dir = str(tmp_path / "icloud" / "sync_packets")
    os.makedirs(sync_dir, exist_ok=True)
    engine.get_sync_dir = lambda: sync_dir
    
    # 1. Create a mock packet from a peer device ("peer_device")
    peer_entries = [
        {"id": "clip_3", "document": "third document plaintext", "metadata": {"source": "clipboard"}}
    ]
    payload = json.dumps(peer_entries)
    sync_key = hashlib.sha256(engine.master_key_bytes + b"latticeshadow_sync").digest()
    aesgcm = AESGCM(sync_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, payload.encode("utf-8"), None)
    packet_data = b64encode(nonce + ciphertext).decode("utf-8")
    
    packet_filename = "peer_device_12345.enc"
    with open(os.path.join(sync_dir, packet_filename), "w") as f:
        f.write(packet_data)
        
    # Mock duplicate check to return not found
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn.cursor.return_value = cursor
    mock_vault._store._connect.return_value.__enter__.return_value = conn
    
    # 2. Import packets
    engine.import_packets()
    
    # Verify it added to vault
    mock_vault.add.assert_called_once_with(
        documents=["third document plaintext"],
        ids=["clip_3"],
        metadatas=[{"source": "clipboard"}]
    )
    
    # Verify file is marked processed
    processed = engine.get_processed_packets()
    assert packet_filename in processed

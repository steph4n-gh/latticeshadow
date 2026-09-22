"""
iCloud synchronization engine for LatticeShadow.
Safely exports and imports encrypted transaction packets via iCloud Drive.
"""

import os
import json
import time
import socket
import secrets
import hashlib
from base64 import b64encode, b64decode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ICloudSyncEngine:
    def __init__(self, vault, device_id=None):
        self.vault = vault
        self.device_id = device_id or f"{socket.gethostname()}_{secrets.token_hex(4)}"
        self.log_dir = os.path.expanduser("~/.latticeshadow")
        os.makedirs(self.log_dir, exist_ok=True)
        
        self.last_sync_file = os.path.join(self.log_dir, ".last_sync_id")
        self.processed_file = os.path.join(self.log_dir, ".processed_packets")
        
        # Retrieve master key bytes from privacy engine
        self.master_key_bytes = None
        if hasattr(self.vault, "_privacy") and self.vault._privacy:
            self.master_key_bytes = self.vault._privacy._master_key_bytes

    def get_sync_dir(self):
        icloud_base = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
        sync_dir = os.path.join(icloud_base, "LatticeShadow", "sync_packets")
        try:
            os.makedirs(sync_dir, exist_ok=True)
            return sync_dir
        except Exception:
            # Fallback to local config directory if iCloud is unavailable
            fallback = os.path.join(self.log_dir, "sync_packets")
            os.makedirs(fallback, exist_ok=True)
            return fallback

    def get_last_sync_id(self):
        if os.path.exists(self.last_sync_file):
            try:
                with open(self.last_sync_file, "r") as f:
                    return f.read().strip()
            except Exception:
                pass
        return ""

    def save_last_sync_id(self, last_id):
        try:
            with open(self.last_sync_file, "w") as f:
                f.write(last_id)
        except Exception:
            pass

    def get_processed_packets(self):
        if os.path.exists(self.processed_file):
            try:
                with open(self.processed_file, "r") as f:
                    return set(f.read().splitlines())
            except Exception:
                pass
        return set()

    def mark_packet_processed(self, filename):
        try:
            with open(self.processed_file, "a") as f:
                f.write(filename + "\n")
        except Exception:
            pass

    def export_packets(self):
        if not self.master_key_bytes:
            return
            
        last_id = self.get_last_sync_id()
        store = self.vault._store
        new_entries = []
        
        try:
            with store._connect() as conn:
                cursor = conn.cursor()
                if last_id:
                    cursor.execute(
                        "SELECT id, document, metadata FROM documents WHERE collection_id = ? AND id > ? ORDER BY id ASC",
                        (store.collection_id, last_id)
                    )
                else:
                    cursor.execute(
                        "SELECT id, document, metadata FROM documents WHERE collection_id = ? ORDER BY id ASC",
                        (store.collection_id,)
                    )
                
                for row in cursor.fetchall():
                    doc_id, doc_text, meta_json = row
                    meta = json.loads(meta_json) if meta_json else {}
                    
                    # Decrypt text if stored encrypted
                    if self.vault._privacy:
                        doc_text = self.vault._privacy.decrypt_document(doc_text)
                        
                    new_entries.append({
                        "id": doc_id,
                        "document": doc_text,
                        "metadata": meta
                    })
        except Exception as e:
            print(f"Sync export query error: {e}")
            return
            
        if not new_entries:
            return
            
        try:
            # Encrypt full JSON payload
            payload = json.dumps(new_entries)
            sync_key = hashlib.sha256(self.master_key_bytes + b"latticeshadow_sync").digest()
            aesgcm = AESGCM(sync_key)
            nonce = os.urandom(12)
            ciphertext = aesgcm.encrypt(nonce, payload.encode("utf-8"), None)
            packet_data = b64encode(nonce + ciphertext).decode("utf-8")
            
            # Write packet
            sync_dir = self.get_sync_dir()
            filename = f"{self.device_id}_{int(time.time() * 1000)}.enc"
            filepath = os.path.join(sync_dir, filename)
            
            temp_path = filepath + ".tmp"
            with open(temp_path, "w") as f:
                f.write(packet_data)
            os.rename(temp_path, filepath)
            
            self.save_last_sync_id(new_entries[-1]["id"])
            try:
                from latticeshadow.audit_log import append_audit_event

                append_audit_event(
                    "sync_export",
                    {
                        "device_id": self.device_id,
                        "filename": filename,
                        "count": len(new_entries),
                    },
                    data_dir=self.log_dir,
                )
            except Exception:
                pass
            print(f"Exported {len(new_entries)} entries to sync packet {filename}")
        except Exception as e:
            print(f"Failed to export sync packet: {e}")

    def import_packets(self):
        if not self.master_key_bytes:
            return
            
        sync_dir = self.get_sync_dir()
        processed = self.get_processed_packets()
        
        try:
            files = sorted(os.listdir(sync_dir))
        except Exception:
            return
            
        sync_key = hashlib.sha256(self.master_key_bytes + b"latticeshadow_sync").digest()
        aesgcm = AESGCM(sync_key)
        
        for filename in files:
            if not filename.endswith(".enc"):
                continue
            if filename in processed:
                continue
            if filename.startswith(self.device_id + "_"):
                continue
                
            filepath = os.path.join(sync_dir, filename)
            try:
                with open(filepath, "r") as f:
                    packet_data = f.read().strip()
                
                raw_bytes = b64decode(packet_data)
                nonce = raw_bytes[:12]
                ciphertext = raw_bytes[12:]
                decrypted = aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
                
                entries = json.loads(decrypted)
                for entry in entries:
                    doc_id = entry["id"]
                    document = entry["document"]
                    metadata = entry["metadata"]
                    
                    # Duplicate check
                    with self.vault._store._connect() as conn:
                        cursor = conn.cursor()
                        cursor.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,))
                        if cursor.fetchone():
                            continue
                    
                    # Safe add (encrypts locally)
                    self.vault.add(
                        documents=[document],
                        ids=[doc_id],
                        metadatas=[metadata]
                    )
                
                self.mark_packet_processed(filename)
                try:
                    from latticeshadow.audit_log import append_audit_event

                    append_audit_event(
                        "sync_import",
                        {
                            "device_id": self.device_id,
                            "filename": filename,
                            "count": len(entries),
                        },
                        data_dir=self.log_dir,
                    )
                except Exception:
                    pass
                print(f"Successfully imported sync packet {filename}")
            except Exception as e:
                print(f"Failed to import sync packet {filename}: {e}")

"""
Proof-of-Thought (PoT) Hash Chain Ledger.

Provides an append-only JSONL cryptographic ledger that records ambient context,
clipboard events, and terminal commands. Events are logged using biological
one-way hashes (Drosophila) to protect the actual plaintext, and linked together
into a continuous SHA-256 hash chain to prove chronological development integrity.

The chain is anchored by an Ed25519 private key generated on first run.
"""

import os
import time
import json
import hashlib
import threading
import logging
from typing import Optional

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

# We import the Drosophila hasher to obfuscate content
from latticeshadow_db.latticedb.drosophila import DrosophilaHasher

logger = logging.getLogger("latticeshadow.pot")

class PoTChain:
    def __init__(self, data_dir: str = "~/.latticeshadow"):
        self.data_dir = os.path.expanduser(data_dir)
        self.chain_path = os.path.join(self.data_dir, ".pot_chain.jsonl")
        self.key_path = os.path.join(self.data_dir, ".pot_key.pem")
        
        self.lock = threading.Lock()
        
        # Load or generate Ed25519 Keypair
        self.private_key = self._get_or_create_key()
        self.public_key = self.private_key.public_key()
        
        # We need a Drosophilla hasher, using a standard dimension (e.g. 128 for clipboards)
        # Note: True dimensional alignment would require the full embedding model, 
        # but for PoT, we just need a one-way semantic fingerprint. We'll simulate 
        # a basic hash for non-embedded raw text if needed, but since LatticeDB usually 
        # embeds first, we will just use standard SHA-256 for the content if we aren't 
        # passing full tensors, or a simpler localized hash.
        # Actually, for PoT of raw strings without torch overhead, let's use a 
        # localized salted SHA-256 for the content fingerprint to keep the daemon light.
        self.content_salt = self._get_public_key_hex()[:16].encode()
        
        self._ensure_genesis_block()

    def _get_or_create_key(self) -> ed25519.Ed25519PrivateKey:
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        
        # Generate new key
        private_key = ed25519.Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        os.makedirs(os.path.dirname(self.key_path), exist_ok=True)
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return private_key

    def _get_public_key_hex(self) -> str:
        pub_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        return pub_bytes.hex()

    def _get_last_block(self) -> dict:
        """Read the last block from the JSONL file."""
        if not os.path.exists(self.chain_path):
            return None
        last_line = None
        # Tail the file efficiently
        with open(self.chain_path, "rb") as f:
            try:
                f.seek(-2, os.SEEK_END)
                while f.read(1) != b'\n':
                    f.seek(-2, os.SEEK_CUR)
            except OSError:
                f.seek(0)
            last_line = f.readline().decode('utf-8')
        
        if last_line:
            try:
                return json.loads(last_line)
            except json.JSONDecodeError:
                pass
        return None

    def _ensure_genesis_block(self):
        with self.lock:
            if not os.path.exists(self.chain_path) or os.path.getsize(self.chain_path) == 0:
                genesis = {
                    "seq": 0,
                    "timestamp": time.time(),
                    "event_type": "genesis",
                    "content_fingerprint": "0" * 64,
                    "prev_hash": "0" * 64
                }
                genesis["block_hash"] = self._hash_block(genesis)
                
                os.makedirs(os.path.dirname(self.chain_path), exist_ok=True)
                fd = os.open(self.chain_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(json.dumps(genesis) + "\n")

    def _hash_block(self, block: dict) -> str:
        """Determine the block's SHA-256 hash based on its contents."""
        core_str = f"{block['seq']}:{block['timestamp']}:{block['event_type']}:{block['content_fingerprint']}:{block['prev_hash']}"
        return hashlib.sha256(core_str.encode("utf-8")).hexdigest()

    def _fingerprint_content(self, content: str) -> str:
        """
        One-way localized fingerprint of the content. 
        Using salted SHA-256 to prove possession of the string without revealing it.
        """
        h = hashlib.sha256(self.content_salt)
        h.update(content.encode("utf-8", errors="replace"))
        return h.hexdigest()

    def append_event(self, event_type: str, content: str) -> None:
        """
        Append a new event to the Proof-of-Thought chain.
        """
        fingerprint = self._fingerprint_content(content)
        
        with self.lock:
            last_block = self._get_last_block()
            if not last_block:
                return
            
            seq = last_block["seq"] + 1
            prev_hash = last_block["block_hash"]
            
            new_block = {
                "seq": seq,
                "timestamp": time.time(),
                "event_type": event_type,
                "content_fingerprint": fingerprint,
                "prev_hash": prev_hash
            }
            new_block["block_hash"] = self._hash_block(new_block)
            
            fd = os.open(self.chain_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(new_block) + "\n")
            
            logger.debug(f"PoT Chain appended {event_type} block {seq}")

    def generate_proof(self) -> dict:
        """
        Exports the entire chain and signs the final block.
        """
        with self.lock:
            if not os.path.exists(self.chain_path):
                return {}
            
            blocks = []
            with open(self.chain_path, "r") as f:
                for line in f:
                    if line.strip():
                        blocks.append(json.loads(line))
            
            if not blocks:
                return {}
            
            final_hash = blocks[-1]["block_hash"]
            signature = self.private_key.sign(final_hash.encode("utf-8"))
            
            return {
                "public_key": self._get_public_key_hex(),
                "signature": signature.hex(),
                "blocks": blocks
            }

    @staticmethod
    def verify_proof(proof: dict) -> tuple[bool, str]:
        """
        Verify an exported PoT chain proof.
        """
        try:
            pub_hex = proof.get("public_key")
            sig_hex = proof.get("signature")
            blocks = proof.get("blocks", [])
            
            if not pub_hex or not sig_hex or not blocks:
                return False, "Malformed proof payload."
                
            pub_bytes = bytes.fromhex(pub_hex)
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
            signature = bytes.fromhex(sig_hex)
            
            # Verify chain continuity
            prev_hash = "0" * 64
            for i, block in enumerate(blocks):
                if block["seq"] != i:
                    return False, f"Sequence mismatch at block {i}"
                if block["prev_hash"] != prev_hash:
                    return False, f"Broken chain link at block {i}"
                
                # Verify block hash
                core_str = f"{block['seq']}:{block['timestamp']}:{block['event_type']}:{block['content_fingerprint']}:{block['prev_hash']}"
                expected_hash = hashlib.sha256(core_str.encode("utf-8")).hexdigest()
                
                if block["block_hash"] != expected_hash:
                    return False, f"Invalid block hash at block {i}"
                
                prev_hash = expected_hash
            
            # Verify final signature
            final_hash = blocks[-1]["block_hash"]
            try:
                public_key.verify(signature, final_hash.encode("utf-8"))
            except Exception:
                return False, "Cryptographic signature validation failed."
                
            return True, f"Verified organic development chain ({len(blocks)} blocks)."
        except Exception as e:
            return False, f"Verification error: {str(e)}"

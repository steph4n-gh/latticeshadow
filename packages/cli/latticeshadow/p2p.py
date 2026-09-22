import os
import sys
import json
import time
import socket
import struct
import threading
import hashlib
from typing import List, Tuple, Dict, Callable
import numpy as np

# Ensure parent path is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from latticeshadow import homomorphic
from latticeshadow.trust import DeviceTrustStore

def generate_distance_snark_proof(a_sum: np.ndarray, b_sum: int, threshold: int) -> dict:
    """
    Generate a mock Groth16 zk-SNARK proof verifying that the homomorphically calculated
    distance does not exceed the threshold.
    """
    # Create a deterministic proof string
    proof_str = f"Groth16_Proof:{a_sum.tobytes().hex()}:{b_sum}:{threshold}"
    h = hashlib.sha256(proof_str.encode("utf-8")).hexdigest()
    
    # Format the proof like a real Groth16 proof
    return {
        "pi_a": [h[:16], h[16:32]],
        "pi_b": [[h[32:48], h[48:64]], [h[64:80], h[80:96]]],
        "pi_c": [h[96:112], h[112:128]],
        "public_inputs": [int(b_sum), int(threshold)]
    }

def verify_distance_snark_proof(proof: dict, a_sum: np.ndarray, b_sum: int, threshold: int) -> bool:
    """
    Verify the simulated Groth16 zk-SNARK proof.
    """
    if not proof or "pi_a" not in proof or "public_inputs" not in proof:
        return False
    proof_str = f"Groth16_Proof:{a_sum.tobytes().hex()}:{b_sum}:{threshold}"
    expected_h = hashlib.sha256(proof_str.encode("utf-8")).hexdigest()
    
    if proof["public_inputs"] != [int(b_sum), int(threshold)]:
        return False
    
    reconstructed_a = proof["pi_a"][0] + proof["pi_a"][1]
    return expected_h.startswith(reconstructed_a)

MULTICAST_GRP = "224.0.0.1"
MULTICAST_PORT = 5050

class LocalMeshNode:
    def __init__(self, node_id: str, vault, port: int = MULTICAST_PORT, data_dir: str | None = None):
        self.node_id = node_id
        self.vault = vault
        self.port = port
        self.peers: Dict[str, Tuple[str, int, float]] = {}  # node_id -> (ip, tcp_port, last_seen)
        self.running = False
        self.on_sync_callback = None
        
        # Sockets
        self.multicast_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.multicast_send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        
        # TCP Server (for receiving queries)
        self.tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_server.bind(("", 0))  # Bind to dynamic port to prevent conflicts
        self.tcp_port = self.tcp_server.getsockname()[1]
        
        self.listener_thread = None
        self.heartbeat_thread = None
        self.tcp_thread = None
        self.multicast_recv = None
        if data_dir is None:
            try:
                from latticeshadow import config

                data_dir = config.get_data_dir()
            except Exception:
                data_dir = "~/.latticeshadow"
        self.trust_store = DeviceTrustStore(data_dir=data_dir)

    def start(self):
        self.running = True
        self.multicast_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.multicast_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.multicast_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        self.multicast_recv.bind(("", self.port))
        
        # Join multicast group
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GRP), socket.INADDR_ANY)
        self.multicast_recv.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        # Start threads
        self.listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener_thread.start()
        
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        
        self.tcp_server.listen(5)
        self.tcp_thread = threading.Thread(target=self._tcp_accept_loop, daemon=True)
        self.tcp_thread.start()

    def stop(self):
        self.running = False
        if self.multicast_recv:
            try:
                self.multicast_recv.close()
            except Exception:
                pass
        if self.tcp_server:
            try:
                self.tcp_server.close()
            except Exception:
                pass

    def _heartbeat_loop(self):
        while self.running:
            try:
                payload = {
                    "type": "HELO",
                    "node_id": self.node_id,
                    "tcp_port": self.tcp_port
                }
                data = json.dumps(payload).encode("utf-8")
                self.multicast_send.sendto(data, (MULTICAST_GRP, self.port))
                # Send to loopback for local testing/discovery
                self.multicast_send.sendto(data, ("127.0.0.1", self.port))
            except Exception:
                pass
            time.sleep(1)  # Faster heartbeats during testing/discovery

    def _listen_loop(self):
        while self.running:
            try:
                data, addr = self.multicast_recv.recvfrom(65535)
                if not data:
                    continue
                payload = json.loads(data.decode("utf-8"))
                
                # Filter out own messages
                if payload.get("node_id") == self.node_id:
                    continue
                
                p_type = payload.get("type")
                if p_type == "HELO":
                    peer_id = payload["node_id"]
                    self.peers[peer_id] = (addr[0], payload["tcp_port"], time.time())
            except Exception:
                pass

    def _tcp_accept_loop(self):
        while self.running:
            try:
                conn, addr = self.tcp_server.accept()
                t = threading.Thread(target=self._handle_tcp_connection, args=(conn,), daemon=True)
                t.start()
            except Exception:
                pass

    def _handle_tcp_connection(self, conn: socket.socket):
        try:
            # Read JSON payload length (4 bytes prefix)
            raw_len = conn.recv(4)
            if not raw_len:
                return
            msg_len = struct.unpack("!I", raw_len)[0]
            
            # Read complete JSON payload
            data = bytearray()
            while len(data) < msg_len:
                packet = conn.recv(msg_len - len(data))
                if not packet:
                    break
                data.extend(packet)
            
            payload = json.loads(data.decode("utf-8"))
            p_type = payload.get("type")
            
            if p_type == "QUERY":
                responses = self._handle_incoming_query(payload)
                # Send back responses over same TCP connection
                resp_data = json.dumps(responses).encode("utf-8")
                conn.sendall(struct.pack("!I", len(resp_data)) + resp_data)
            elif p_type == "SYNC":
                self._handle_incoming_sync(payload)
            elif p_type == "SWARM_KNOWLEDGE":
                self._handle_incoming_swarm_knowledge(payload)
        except Exception:
            pass
        finally:
            conn.close()

    def _handle_incoming_query(self, payload: dict) -> List[dict]:
        """Perform homomorphic distance calculation and return matching payloads."""
        results = []
        try:
            enc_a = np.array(payload["enc_a"], dtype=np.uint32)
            enc_b = np.array(payload["enc_b"], dtype=np.uint32)
            
            # Fetch all local Drosophila binary hashes
            with self.vault._store._connect() as conn:
                cursor = conn.execute(
                    "SELECT doc_id, document, text_hash FROM vectors WHERE collection = ?",
                    (self.vault.name,)
                )
                rows = cursor.fetchall()
            
            for doc_id, doc, text_hash in rows:
                bitmask = np.zeros(128, dtype=np.uint32)
                h_bytes = bytes.fromhex(text_hash) if text_hash else os.urandom(16)
                for byte_idx, byte in enumerate(h_bytes[:16]):
                    for bit_idx in range(8):
                        bitmask[byte_idx * 8 + bit_idx] = (byte >> bit_idx) & 1

                # Calculate homomorphic distance
                a_sum, b_sum = homomorphic.evaluate_distance_homomorphically(enc_a, enc_b, bitmask)
                
                # Generate zk-SNARK proof of distance evaluation (mock threshold 35)
                proof = generate_distance_snark_proof(a_sum, int(b_sum), 35)
                
                results.append({
                    "node_id": self.node_id,
                    "doc_id": doc_id,
                    "document": doc,  # Encrypted AES ciphertext
                    "a_sum": a_sum.tolist(),
                    "b_sum": int(b_sum),
                    "proof": proof
                })
        except Exception:
            pass
        return results

    def broadcast_search(self, query_bitmask: np.ndarray, secret_key: np.ndarray, threshold: int = 35) -> List[dict]:
        """
        Send a homomorphically encrypted search query to all discovered peers.
        """
        enc_a, enc_b = homomorphic.encrypt_bitmask(secret_key, query_bitmask)
        
        payload = {
            "type": "QUERY",
            "node_id": self.node_id,
            "enc_a": enc_a.tolist(),
            "enc_b": enc_b.tolist()
        }
        
        payload_data = json.dumps(payload).encode("utf-8")
        header = struct.pack("!I", len(payload_data))
        
        matches = []
        
        # Query active peers
        active_peers = list(self.peers.items())
        for peer_id, (ip, tcp_port, last_seen) in active_peers:
            if time.time() - last_seen > 10:
                continue
                
            try:
                # Log simulated CoreBluetooth search
                print(f"[CoreBluetooth BLE] Advertising query service UUID 0xFD84. Connecting to peer {peer_id}...")
                
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((ip, tcp_port))
                
                sock.sendall(header + payload_data)
                
                raw_len = sock.recv(4)
                if not raw_len:
                    sock.close()
                    continue
                msg_len = struct.unpack("!I", raw_len)[0]
                
                data = bytearray()
                while len(data) < msg_len:
                    packet = sock.recv(msg_len - len(data))
                    if not packet:
                        break
                    data.extend(packet)
                
                responses = json.loads(data.decode("utf-8"))
                for resp in responses:
                    a_sum = np.array(resp["a_sum"], dtype=np.uint32)
                    b_sum = resp["b_sum"]
                    proof = resp.get("proof")
                    
                    # Verify the zk-SNARK proof before decrypting
                    if proof and verify_distance_snark_proof(proof, a_sum, b_sum, 35):
                        dist = homomorphic.decrypt_distance(secret_key, a_sum, b_sum)
                        if dist <= threshold:
                            try:
                                if hasattr(self.vault, "_privacy") and self.vault._privacy:
                                    decrypted_doc = self.vault._privacy.decrypt_document(resp["document"])
                                else:
                                    decrypted_doc = resp["document"]
                            except Exception:
                                decrypted_doc = resp["document"]
                                
                            matches.append({
                                "node_id": resp["node_id"],
                                "doc_id": resp["doc_id"],
                                "document": decrypted_doc,
                                "distance": dist,
                                "proof_valid": True
                            })
                sock.close()
            except Exception:
                pass
                
        return matches

    @property
    def sync_callback(self):
        return self.on_sync_callback

    @sync_callback.setter
    def sync_callback(self, cb):
        self.on_sync_callback = cb

    @property
    def on_sync(self):
        return self.on_sync_callback

    @on_sync.setter
    def on_sync(self, cb):
        self.on_sync_callback = cb

    def register_sync_callback(self, cb):
        self.on_sync_callback = cb

    def broadcast_sync(self, content: str, doc_id: str):
        """
        Encrypt content using vault privacy and broadcast to all active peers asynchronously.
        """
        try:
            encrypted_content = self.vault._privacy.encrypt_document(content)
        except Exception:
            encrypted_content = content

        payload = {
            "type": "SYNC",
            "node_id": self.node_id,
            "doc_id": doc_id,
            "content": encrypted_content
        }
        payload = self.trust_store.sign_payload(payload)
        payload_data = json.dumps(payload).encode("utf-8")
        header = struct.pack("!I", len(payload_data))

        active_peers = list(self.peers.items())
        for peer_id, (ip, tcp_port, last_seen) in active_peers:
            if time.time() - last_seen > 10:
                continue

            def send_to_peer(peer_ip, peer_port):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect((peer_ip, peer_port))
                    sock.sendall(header + payload_data)
                    sock.close()
                except Exception:
                    pass

            t = threading.Thread(target=send_to_peer, args=(ip, tcp_port), daemon=True)
            t.start()

    def _handle_incoming_sync(self, payload: dict):
        """
        Decrypt and verify the sync payload, add it to the local db, and trigger callback.
        """
        try:
            sender_node_id = payload.get("node_id", "unknown")
            doc_id = payload.get("doc_id") or f"clip_{int(time.time() * 1000)}"
            encrypted_content = payload.get("content", "")

            from latticeshadow import config
            if config.get("sync.mesh_trust_required"):
                verified, reason = self.trust_store.verify_payload(payload)
                if not verified:
                    print(f"[Mesh Sync] Rejected untrusted sync from {sender_node_id}: {reason}")
                    return

            decrypted_content = encrypted_content
            if hasattr(self.vault, "_privacy") and self.vault._privacy:
                try:
                    decrypted_content = self.vault._privacy.decrypt_document(encrypted_content)
                except Exception:
                    pass

            if encrypted_content.startswith("enc:") and decrypted_content.startswith("enc:"):
                return  # Decryption failed

            # Add to local database
            self.vault.add(
                documents=[decrypted_content],
                ids=[doc_id],
                metadatas=[{"source": f"p2p_{sender_node_id}"}]
            )

            # Write to macOS General Pasteboard
            try:
                import AppKit
                pb = AppKit.NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(decrypted_content, AppKit.NSPasteboardTypeString)
            except Exception:
                pass

            # Invoke callback
            if self.on_sync_callback:
                self.on_sync_callback(decrypted_content, sender_node_id)
        except Exception:
            pass

    def broadcast_swarm_knowledge(self, target_error: str, fix_script: str, signature: str):
        """
        Broadcast a Swarm Knowledge (antibody) to all active peers over TCP.
        """
        payload = {
            "type": "SWARM_KNOWLEDGE",
            "node_id": self.node_id,
            "target_error": target_error,
            "fix_script": fix_script,
            "signature": signature
        }
        payload = self.trust_store.sign_payload(payload)
        payload_data = json.dumps(payload).encode("utf-8")
        header = struct.pack("!I", len(payload_data))

        active_peers = list(self.peers.items())
        for peer_id, (ip, tcp_port, last_seen) in active_peers:
            if time.time() - last_seen > 10:
                continue

            def send_to_peer(peer_ip, peer_port):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect((peer_ip, peer_port))
                    sock.sendall(header + payload_data)
                    sock.close()
                except Exception:
                    pass

            t = threading.Thread(target=send_to_peer, args=(ip, tcp_port), daemon=True)
            t.start()
            
    def _handle_incoming_swarm_knowledge(self, payload: dict):
        """
        Validate and record a Swarm Knowledge payload.

        This path is intentionally record-only. Early prototypes executed
        received fixes directly, but network-delivered shell execution is too
        risky without a real paired-device signature scheme and sandbox.
        """
        try:
            sender_node_id = payload.get("node_id")
            target_error = payload.get("target_error")
            fix_script = payload.get("fix_script")
            signature = payload.get("signature")
            
            if not target_error or not fix_script or not signature:
                return

            from latticeshadow import config
            if not config.get("sync.swarm_knowledge"):
                return

            if not self._verify_swarm_signature(sender_node_id, target_error, fix_script, signature, payload):
                print(f"[Swarm Knowledge] Rejected unsigned fix from {sender_node_id}.")
                return

            self._record_swarm_suggestion(payload)
        except Exception as e:
            print(f"[Swarm Knowledge] Error handling payload: {e}")

    def _verify_swarm_signature(
        self,
        sender_node_id: str,
        target_error: str,
        fix_script: str,
        signature: str,
        payload: dict | None = None,
    ) -> bool:
        """
        Verify a trusted paired-device signature for swarm knowledge.
        """
        if payload is None:
            return False
        verified, _ = self.trust_store.verify_payload(payload)
        return verified

    def _record_swarm_suggestion(self, payload: dict):
        """Persist a verified swarm suggestion for explicit user review."""
        data_dir = os.path.expanduser("~/.latticeshadow")
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        path = os.path.join(data_dir, "pending_swarm_knowledge.jsonl")
        record = {
            "received_at": time.time(),
            "node_id": payload.get("node_id"),
            "target_error": payload.get("target_error"),
            "fix_script": payload.get("fix_script"),
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        try:
            from latticeshadow.repair_queue import create_repair_proposal

            create_repair_proposal(
                summary="Verified swarm repair suggestion",
                source="swarm_knowledge",
                command=payload.get("fix_script"),
                risk="high",
                provenance={
                    "node_id": payload.get("node_id"),
                    "target_error_hash": hashlib.sha256(
                        str(payload.get("target_error") or "").encode("utf-8", errors="replace")
                    ).hexdigest(),
                },
                data_dir=data_dir,
            )
        except Exception:
            pass

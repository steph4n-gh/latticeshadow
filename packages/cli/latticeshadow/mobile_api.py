import os
import sys
import json
import secrets
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, List

# Ensure parent path is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

MAX_REQUEST_BYTES = 64 * 1024
PAIR_ATTEMPT_WINDOW_SECONDS = 60
MAX_PAIR_ATTEMPTS = 5

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class MobileAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default console logging
        pass

    def _send_response(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        server = self.server  # type: MobileAPIServer
        
        # Parse and validate Content-Length safely
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_response(400, {"error": "Invalid Content-Length header"})
            return
        if content_length > MAX_REQUEST_BYTES:
            self._send_response(413, {"error": "Request body too large"})
            return

        # Read and decode body safely
        try:
            raw_body = self.rfile.read(content_length)
            body_str = raw_body.decode("utf-8")
        except Exception:
            self._send_response(400, {"error": "Failed to read request body or invalid UTF-8 encoding"})
            return

        # Parse JSON safely
        try:
            body = json.loads(body_str) if body_str else {}
            if not isinstance(body, dict):
                self._send_response(400, {"error": "Request body must be a JSON object"})
                return
        except json.JSONDecodeError as e:
            self._send_response(400, {"error": f"Invalid JSON body: {str(e)}"})
            return

        if self.path == "/pair":
            passcode = body.get("passcode")
            if not passcode:
                self._send_response(400, {"error": "Missing passcode parameter"})
                return
            
            # Thread-safe check and modification of pairing state
            with server.lock:
                now = time.time()
                client_ip = self.client_address[0]
                attempts = [
                    ts for ts in server.failed_pair_attempts.get(client_ip, [])
                    if now - ts < PAIR_ATTEMPT_WINDOW_SECONDS
                ]
                if len(attempts) >= MAX_PAIR_ATTEMPTS:
                    server.failed_pair_attempts[client_ip] = attempts
                    self._send_response(429, {"error": "Too many pairing attempts"})
                    return

                if not server.pairing_code or passcode != server.pairing_code:
                    attempts.append(now)
                    server.failed_pair_attempts[client_ip] = attempts
                    self._send_response(401, {"error": "Invalid passcode"})
                    return
                
                # Pair successfully: generate pairing key
                pairing_token = secrets.token_hex(32)
                server.paired_clients[pairing_token] = True
                server.failed_pair_attempts.pop(client_ip, None)
                
                # Disable pairing code after first successful pair
                server.pairing_code = None
                
            self._send_response(200, {
                "status": "paired",
                "token": pairing_token
            })
                
        elif self.path == "/search":
            # Authenticate token strictly and conform to standard Bearer token scheme
            auth_header = self.headers.get("Authorization", "")
            if not auth_header.lower().startswith("bearer "):
                self._send_response(401, {"error": "Unauthorized. Missing or invalid Authorization header scheme."})
                return
            
            token = auth_header[7:].strip()
            if not token or token not in server.paired_clients:
                self._send_response(401, {"error": "Unauthorized. Client not paired."})
                return
                
            try:
                query = body.get("query", "")
                
                # Run search against local vault
                # Limit to 5 results for mobile view
                results = server.vault.search(query, n_results=5)
                
                hits = []
                ids = getattr(results, "ids", []) or []
                documents = getattr(results, "documents", []) or []
                scores = getattr(results, "scores", []) or []
                for idx, doc_val in enumerate(documents):
                    # Coerce document representation to string if it is not a basic primitive
                    if not isinstance(doc_val, (dict, list, str, int, float, bool, type(None))):
                        doc_val = str(doc_val)

                    hits.append({
                        "doc_id": ids[idx] if idx < len(ids) else "",
                        "document": doc_val,
                        "score": float(scores[idx]) if idx < len(scores) else 1.0
                    })
                    
                self._send_response(200, {"results": hits})
            except Exception as e:
                self._send_response(500, {"error": "Internal search error occurred"})
        else:
            self._send_response(404, {"error": "Not found"})

class MobileAPIServer:
    def __init__(self, vault, host: str = "127.0.0.1", port: int = 5052):
        self.vault = vault
        self.host = host
        self.port = port
        self.server = None
        self.thread = None
        self.pairing_code = None
        self.paired_clients: Dict[str, bool] = {}
        self.failed_pair_attempts: Dict[str, List[float]] = {}
        self.lock = threading.Lock()  # Ensure thread safety for pairing

    def generate_pairing_code(self) -> str:
        """Generate a temporary 6-digit numeric pairing code."""
        self.pairing_code = "".join(secrets.choice("0123456789") for _ in range(6))
        return self.pairing_code

    def start(self):
        self.server = ThreadingHTTPServer((self.host, self.port), MobileAPIHandler)
        self.host, self.port = self.server.server_address[:2]
        # Pass self references to the handler via server properties
        self.server.vault = self.vault
        self.server.pairing_code = self.pairing_code
        self.server.paired_clients = self.paired_clients
        self.server.failed_pair_attempts = self.failed_pair_attempts
        self.server.lock = self.lock
        
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.write_icloud_handshake()

    def write_icloud_handshake(self):
        """Write configuration profile to iCloud for zero-touch configuration."""
        try:
            import socket
            
            # Resolve iCloud directory path
            icloud_dir = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/LatticeShadow")
            if not os.path.exists(icloud_dir):
                return
                
            # Resolve local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
            except Exception:
                local_ip = "127.0.0.1"
            finally:
                s.close()
                
            handshake_path = os.path.join(icloud_dir, "handshake.json")
            handshake_data = {
                "ip": local_ip,
                "port": self.port,
                "pairing_code": self.pairing_code,
                "timestamp": time.time()
            }
            
            temp_path = handshake_path + ".tmp"
            with open(temp_path, "w") as f:
                json.dump(handshake_data, f)
            os.replace(temp_path, handshake_path)
        except Exception:
            pass

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

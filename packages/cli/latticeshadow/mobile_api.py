import os
import sys
import json
import secrets
import time
import threading
import ipaddress
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, List

# Ensure parent path is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

MAX_REQUEST_BYTES = 64 * 1024
PAIR_ATTEMPT_WINDOW_SECONDS = 60
MAX_PAIR_ATTEMPTS = 5
MAX_CONNECTIONS = 16
SOCKET_TIMEOUT_SECONDS = 10

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs):
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(SOCKET_TIMEOUT_SECONDS)
        return request, address

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

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
        if content_length < 0:
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
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise ValueError("Mobile API host must be a numeric loopback address") from exc
        if address.version != 4 or not address.is_loopback:
            raise ValueError("Mobile API requires an IPv4 loopback host")
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

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
